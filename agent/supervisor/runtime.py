####
## Control Plane Supervisor Runtime for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Execute one policy-selected specialist with budgets, audit, and failure isolation."""

# --- Importing Libraries
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import NAMESPACE_URL, UUID, uuid5

from agent.context.models import (
    DEFAULT_RUN_CONTEXT_RETENTION_DAYS,
    IncidentMemoryRecord,
    RunContextEvent,
    RunContextPhase,
    build_incident_memory_record,
    build_run_context_event,
)
from agent.context.store import (
    ensure_agent_context_tables,
    persist_incident_memory,
    persist_run_context_event,
)
from agent.llm.config import external_llm_permission_scope
from agent.specialists.contracts import (
    AgentApprovalState,
    AgentResultEnvelope,
    AgentTaskEnvelope,
    AgentTaskStatus,
    ContextReference,
    ContextReferenceType,
    HandoffRecord,
    SupervisorState,
)
from agent.specialists.incident_triage import (
    IncidentTriageRuntimeConfig,
    run_incident_triage_agent,
)
from agent.specialists.metadata_lineage import (
    MetadataLineageRuntimeConfig,
    run_metadata_lineage_agent,
)
from agent.specialists.schema_drift import (
    SchemaDriftRuntimeConfig,
    run_schema_drift_agent,
)
from agent.specialists.sql_review import (
    SqlReviewRuntimeConfig,
    run_sql_review_agent,
)
from agent.specialists.registry import (
    AgentCapabilitySpec,
    INCIDENT_TRIAGE_SPECIALIST_NAME,
    METADATA_LINEAGE_SPECIALIST_NAME,
    SCHEMA_DRIFT_SPECIALIST_NAME,
    SQL_REVIEW_SPECIALIST_NAME,
    enforce_result_contract,
    get_agent_capability,
)
from agent.supervisor.models import (
    SupervisorRequest,
    SupervisorRoute,
    SupervisorRunResult,
)
from agent.supervisor.budgets import (
    SupervisorBudgetDecision,
    SupervisorBudgetExceeded,
    SupervisorBudgetVector,
    evaluate_post_handoff_budgets,
    evaluate_pre_handoff_budgets,
    require_budget_decision,
    supervisor_llm_budget_scope,
)
from agent.supervisor.routing import (
    SupervisorRoutingError,
    build_supervisor_handoff,
    resolve_supervisor_route,
)
from agent.supervisor.policy import (
    SupervisorRoutingPolicyDecision,
    evaluate_post_handoff_policy,
    evaluate_pre_handoff_policy,
)
from agent.supervisor.resilience import (
    CircuitBreakerPolicy,
    CircuitBreakerSnapshot,
    SupervisorCircuitOpen,
    SupervisorFailureCategory,
    SupervisorHardTimeout,
    SupervisorHardTimeoutUnavailable,
    SupervisorInvocationFailure,
    circuit_snapshot_payload,
    enforce_hard_deadline,
    is_retryable_exception,
    is_retryable_result,
    load_circuit_breaker_snapshot,
    require_circuit_allows,
    retry_backoff_seconds,
    retry_is_allowed,
)
from agent.tools.audit_log import hash_sql, write_agent_audit_event
from pipelines.common.clickhouse import build_clickhouse_client
from pipelines.common.logging import logger


# --- Defining Constants
SUPERVISOR_TOOL_NAME = "control_plane_supervisor"


# --- Defining Runtime Configuration
@dataclass(frozen=True)
class SupervisorRuntimeConfig:
    """
    Inject bounded specialist runners and append-only audit dependencies.

    Attributes:
        incident_runner: Incident Triage specialist callable.
        metadata_lineage_runner: Metadata and Lineage specialist callable.
        sql_review_runner: SQL Safety and Review specialist callable.
        schema_drift_runner: Schema Drift specialist callable.
        incident_config: Incident specialist runtime configuration.
        metadata_lineage_config: Metadata specialist runtime configuration.
        sql_review_config: SQL review specialist runtime configuration.
        schema_drift_config: Schema Drift specialist runtime configuration.
        audit_client_factory: ClickHouse client factory.
        audit_writer: Append-only audit event writer.
        context_schema_ensurer: Idempotent ClickHouse context DDL callable.
        context_event_writer: Temporary run-context persistence callable.
        incident_memory_writer: Durable incident-memory persistence callable.
        circuit_policy: Persistent audit-derived circuit thresholds and cooldown.
        circuit_snapshot_loader: Read-only circuit state loader.
        specialist_timeout_cap_seconds: Optional runtime cap that may only reduce a
            specialist task's declared timeout.
        sleep_callable: Injectable bounded retry backoff function.
        context_retention_days: TTL period for temporary run context.
        clickhouse_host: Optional ClickHouse host override.
        clickhouse_port: Optional ClickHouse HTTP port override.
    """

    incident_runner: Callable[..., AgentResultEnvelope] = run_incident_triage_agent
    metadata_lineage_runner: Callable[..., AgentResultEnvelope] = run_metadata_lineage_agent
    sql_review_runner: Callable[..., AgentResultEnvelope] = run_sql_review_agent
    schema_drift_runner: Callable[..., AgentResultEnvelope] = run_schema_drift_agent
    incident_config: IncidentTriageRuntimeConfig = field(
        default_factory=IncidentTriageRuntimeConfig
    )
    metadata_lineage_config: MetadataLineageRuntimeConfig = field(
        default_factory=MetadataLineageRuntimeConfig
    )
    sql_review_config: SqlReviewRuntimeConfig = field(
        default_factory=SqlReviewRuntimeConfig
    )
    schema_drift_config: SchemaDriftRuntimeConfig = field(
        default_factory=SchemaDriftRuntimeConfig
    )
    audit_client_factory: Callable[..., Any] = build_clickhouse_client
    audit_writer: Callable[..., UUID]        = write_agent_audit_event
    context_schema_ensurer: Callable[[Any], None] = ensure_agent_context_tables
    context_event_writer: Callable[..., UUID]     = persist_run_context_event
    incident_memory_writer: Callable[..., UUID]   = persist_incident_memory
    circuit_policy: CircuitBreakerPolicy          = field(default_factory=CircuitBreakerPolicy)
    circuit_snapshot_loader: Callable[..., CircuitBreakerSnapshot] = (
        load_circuit_breaker_snapshot
    )
    specialist_timeout_cap_seconds: int | None    = None
    sleep_callable: Callable[[float], None]        = time.sleep
    context_retention_days: int                   = DEFAULT_RUN_CONTEXT_RETENTION_DAYS
    clickhouse_host: str | None              = None
    clickhouse_port: int | None              = None

    def __post_init__(self) -> None:
        """
        Reject invalid runtime timeout caps before any specialist is invoked.

        Returns:
            None.

        Raises:
            ValueError: If the optional timeout cap is outside the task contract.
        """
        if (
            self.specialist_timeout_cap_seconds is not None
            and not 1 <= self.specialist_timeout_cap_seconds <= 300
        ):
            raise ValueError("specialist_timeout_cap_seconds must be between 1 and 300.")


# --- Defining Correlation Helpers
def derive_supervisor_parent_run_id(external_run_id: str) -> UUID:
    """
    Derive a stable parent UUID from one Airflow or operator run ID.

    Args:
        external_run_id: Stable external run identifier.

    Returns:
        Deterministic supervisor correlation UUID.

    Raises:
        ValueError: If the external run identifier is blank.
    """
    normalized = external_run_id.strip()

    if not normalized:
        raise ValueError("Supervisor external_run_id cannot be blank.")

    return uuid5(NAMESPACE_URL, f"agentic-dq:control-plane-supervisor:{normalized}")


# --- Defining Audit Helpers
def write_supervisor_audit(
    config: SupervisorRuntimeConfig,
    client: Any,
    parent_run_id: UUID,
    request: SupervisorRequest,
    action: str,
    status: str,
    route: SupervisorRoute | None = None,
    task: AgentTaskEnvelope | None = None,
    result: AgentResultEnvelope | None = None,
    context_event_id: UUID | None = None,
    incident_memory_id: UUID | None = None,
    approval_state: AgentApprovalState | None = None,
    budget_decision: SupervisorBudgetDecision | None = None,
    routing_policy_decision: SupervisorRoutingPolicyDecision | None = None,
    resilience_payload: dict[str, Any] | None = None,
    duration_ms: int | None = None,
    error_message: str = "",
) -> None:
    """
    Persist one bounded parent-run supervisor decision.

    Args:
        config: Supervisor runtime containing the audit writer.
        client: ClickHouse audit client.
        parent_run_id: Stable parent run UUID.
        request: Typed operator request.
        action: Stable supervisor action.
        status: running, success, partial, blocked, or failed.
        route: Optional deterministic route decision.
        task: Optional specialist handoff.
        result: Optional terminal specialist result.
        context_event_id: Optional persisted run-context event UUID.
        incident_memory_id: Optional durable incident-memory UUID.
        approval_state: Optional explicit human approval state for terminal decisions.
        budget_decision: Optional deterministic budget-policy decision.
        routing_policy_decision: Optional model, risk, review, and approval policy decision.
        resilience_payload: Optional bounded timeout, retry, or circuit evidence.
        duration_ms: Optional parent-run duration.
        error_message: Sanitized failure detail.

    Returns:
        None.
    """
    output_payload: dict[str, Any] = {}

    if route:
        output_payload.update(
            {
                "resolved_intent": route.intent.value,
                "selected_specialist": route.specialist_name,
                "task_type": route.task_type,
                "routing_rationale": route.rationale,
            }
        )

    if task:
        output_payload.update(
            {
                "task_id": str(task.task_id),
                "risk_tier": task.risk_tier.value,
                "model_route": task.model_route.value,
                "allowed_tools": list(task.allowed_tools),
                "task_model_call_budget": task.model_call_budget,
                "task_token_budget": task.token_budget,
                "task_estimated_cost_budget_usd": task.estimated_cost_budget_usd,
                "task_timeout_seconds": task.timeout_seconds,
            }
        )

    if result:
        output_payload.update(
            {
                "result_status": result.status.value,
                "confidence": result.confidence,
                "actual_model_route": result.model_route.value,
                "model_call_count": result.model_call_count,
                "token_usage": result.token_usage,
                "estimated_cost_usd": result.estimated_cost_usd,
                "specialist_duration_ms": result.duration_ms,
                "evidence_reference_count": len(result.evidence_references),
                "requires_human_approval": result.requires_human_approval,
            }
        )

        for output_key in (
            "decision",
            "proposal_sql_hash",
            "query_risk_level",
            "execution_performed",
            "assessment",
            "impact_level",
            "source_schema_run_id",
            "finding_count",
            "impacted_asset_count",
            "impacted_test_count",
            "complexity_tier",
            "complexity_score",
            "complexity_reason_codes",
            "investigation_errors",
        ):
            if output_key in result.structured_output:
                output_payload[output_key] = result.structured_output[output_key]

        investigation_errors = result.structured_output.get("investigation_errors")

        if isinstance(investigation_errors, list):
            output_payload["investigation_error_count"] = len(investigation_errors)

    if context_event_id:
        output_payload["context_event_id"] = str(context_event_id)

    if incident_memory_id:
        output_payload["incident_memory_id"] = str(incident_memory_id)

    if approval_state:
        output_payload["approval_state"] = approval_state.value

    if budget_decision:
        output_payload["budget_decision"] = budget_decision.model_dump(mode="json")

    if routing_policy_decision:
        output_payload["routing_policy"] = routing_policy_decision.model_dump(mode="json")

    if resilience_payload:
        output_payload["resilience"] = resilience_payload

    config.audit_writer(
        client=client,
        action=action,
        status=status,
        agent_run_id=parent_run_id,
        alert_key=request.alert_key,
        actor=request.requester,
        tool_name=SUPERVISOR_TOOL_NAME,
        duration_ms=duration_ms,
        input_payload={
            "requested_intent": request.intent.value,
            "has_alert_reference": bool(request.alert_id or request.alert_key),
            "qualified_name": request.qualified_name,
            "has_search_query": bool(request.query or request.question),
            "has_sql_proposal": bool(request.sql_proposal),
            "proposal_sql_hash": hash_sql(request.sql_proposal) if request.sql_proposal else "",
            "sql_hard_limit": request.sql_hard_limit,
            "sql_require_date_filter": request.sql_require_date_filter,
            "sql_max_scan_bytes": request.sql_max_scan_bytes,
            "has_schema_run_id": bool(request.schema_run_id),
            "source_schema_run_id": request.schema_run_id,
            "schema_finding_limit": request.schema_finding_limit,
            "execution_mode": request.execution_mode.value,
            "max_workers": request.max_workers,
            "max_concurrency": request.max_concurrency,
            "allow_external_llm": request.allow_external_llm,
            "max_handoffs": request.max_handoffs,
            "max_retries": request.max_retries,
            "max_model_calls": request.max_model_calls,
            "token_budget": request.token_budget,
            "estimated_cost_budget_usd": request.estimated_cost_budget_usd,
            "latency_budget_ms": request.latency_budget_ms,
        },
        output_payload=output_payload,
        error_message=error_message,
        sql=request.sql_proposal,
        report_s3_uri=(
            str(result.structured_output.get("markdown_report_s3_uri", ""))
            if result
            else ""
        ),
    )


# --- Defining Policy Helpers
def sanitize_supervisor_error(exc: BaseException) -> str:
    """
    Build a bounded, single-line supervisor failure message.

    Args:
        exc: Routing, policy, specialist, audit, or hard-timeout failure.

    Returns:
        Sanitized error type and message.
    """
    return f"{type(exc).__name__}: {' '.join(str(exc).split())[:1_500]}"


def normalize_optional_alert_uuid(value: str) -> UUID | None:
    """
    Normalize an optional supervisor alert UUID.

    Args:
        value: Optional UUID text from the supervisor request.

    Returns:
        UUID when present, otherwise None.

    Raises:
        ValueError: If a non-empty alert ID is not a UUID.
    """
    return UUID(value) if value else None


def resolve_context_alert_identity(
    request: SupervisorRequest,
    result: AgentResultEnvelope | None = None,
) -> tuple[UUID | None, str, str]:
    """
    Resolve canonical and human-facing alert identities for context persistence.

    Args:
        request: Original bounded supervisor request.
        result: Optional specialist result with normalized alert identity fields.

    Returns:
        Tuple containing alert UUID, canonical system key, and human Alert Ref.
    """
    output           = result.structured_output if result else {}
    resolved_key     = str(output.get("alert_key", "") or "").strip()
    resolved_display = str(output.get("alert_display_id", "") or "").strip()
    request_reference = request.alert_key.strip()

    if not resolved_key and "|" in request_reference:
        resolved_key = request_reference

    if not resolved_display and request_reference.upper().startswith("DQ-"):
        resolved_display = request_reference

    return normalize_optional_alert_uuid(request.alert_id), resolved_key, resolved_display


def build_started_context_facts(request: SupervisorRequest) -> dict[str, Any]:
    """
    Build non-sensitive request facts for the first run-context event.

    Args:
        request: Typed supervisor request.

    Returns:
        Bounded facts without questions, raw SQL, prompts, or credentials.
    """
    return {
        "requested_intent": request.intent.value,
        "has_alert_reference": bool(request.alert_id or request.alert_key),
        "qualified_name": request.qualified_name,
        "has_search_query": bool(request.query),
        "has_sql_proposal": bool(request.sql_proposal),
        "proposal_sql_hash": hash_sql(request.sql_proposal) if request.sql_proposal else "",
        "has_schema_run_id": bool(request.schema_run_id),
        "source_schema_run_id": request.schema_run_id,
        "execution_mode": request.execution_mode.value,
        "max_workers": request.max_workers,
        "max_concurrency": request.max_concurrency,
        "allow_external_llm": request.allow_external_llm,
        "max_handoffs": request.max_handoffs,
        "max_retries": request.max_retries,
        "max_model_calls": request.max_model_calls,
        "token_budget": request.token_budget,
        "estimated_cost_budget_usd": request.estimated_cost_budget_usd,
        "latency_budget_ms": request.latency_budget_ms,
    }


def build_route_context_facts(route: SupervisorRoute, task: AgentTaskEnvelope) -> dict[str, Any]:
    """
    Build policy-owned route facts for explicit cross-agent context.

    Args:
        route: Deterministic supervisor route.
        task: Policy-bounded handoff envelope.

    Returns:
        Bounded route, risk, model capability, and tool-count facts.
    """
    return {
        "resolved_intent": route.intent.value,
        "selected_specialist": route.specialist_name,
        "task_type": route.task_type,
        "risk_tier": task.risk_tier.value,
        "model_route": task.model_route.value,
        "allowed_tool_count": len(task.allowed_tools),
        "model_call_budget": task.model_call_budget,
        "token_budget": task.token_budget,
        "estimated_cost_budget_usd": task.estimated_cost_budget_usd,
        "timeout_seconds": task.timeout_seconds,
    }


def build_result_context_facts(
    route: SupervisorRoute,
    result: AgentResultEnvelope,
) -> dict[str, Any]:
    """
    Build a safe durable subset of one specialist decision.

    Args:
        route: Deterministic supervisor route.
        result: Terminal specialist result.

    Returns:
        Bounded decision facts without raw evidence rows or hidden reasoning.
    """
    facts: dict[str, Any] = {
        "resolved_intent": route.intent.value,
        "result_status": result.status.value,
        "confidence": result.confidence,
        "model_call_count": result.model_call_count,
        "token_usage": result.token_usage,
        "estimated_cost_usd": result.estimated_cost_usd,
        "duration_ms": result.duration_ms,
        "requires_human_approval": result.requires_human_approval,
        "evidence_reference_count": len(result.evidence_references),
    }

    for output_key in (
        "decision",
        "proposal_sql_hash",
        "query_risk_level",
        "execution_performed",
        "assessment",
        "impact_level",
        "source_schema_run_id",
        "finding_count",
        "impacted_asset_count",
        "impacted_test_count",
        "report_id",
        "complexity_tier",
        "complexity_score",
        "complexity_reason_codes",
        "investigation_errors",
    ):
        if output_key in result.structured_output:
            facts[output_key] = result.structured_output[output_key]

    investigation_errors = result.structured_output.get("investigation_errors")

    if isinstance(investigation_errors, list):
        facts["investigation_error_count"] = len(investigation_errors)

    top_hypothesis = result.structured_output.get("top_hypothesis")

    if isinstance(top_hypothesis, dict):
        facts["top_hypothesis_category"] = str(top_hypothesis.get("category", ""))[:80]

    return facts


def attach_run_context_reference(
    task: AgentTaskEnvelope,
    context_event_id: UUID,
) -> AgentTaskEnvelope:
    """
    Attach one explicit persisted context reference to a specialist handoff.

    Args:
        task: Existing policy-built specialist task.
        context_event_id: Persisted started-context event UUID.

    Returns:
        Revalidated task containing one additional run-context reference.
    """
    reference = ContextReference(
        reference_type=ContextReferenceType.RUN_CONTEXT,
        reference=f"run-context:{context_event_id}",
        description="Persisted run-scoped context for this bounded supervisor investigation.",
    )
    payload = task.model_dump(mode="python")
    payload["context_references"] = [*task.context_references, reference]

    return AgentTaskEnvelope.model_validate(payload)


def context_report_uri(result: AgentResultEnvelope | None) -> str:
    """
    Resolve the primary persisted report URI from one specialist result.

    Args:
        result: Optional terminal specialist result.

    Returns:
        Markdown URI first, then JSON URI, otherwise an empty string.
    """
    if result is None:
        return ""

    return str(
        result.structured_output.get("markdown_report_s3_uri")
        or result.structured_output.get("json_report_s3_uri")
        or ""
    )


def persist_supervisor_context_event(
    config: SupervisorRuntimeConfig,
    client: Any,
    parent_run_id: UUID,
    external_run_id: str,
    request: SupervisorRequest,
    phase: RunContextPhase,
    status: AgentTaskStatus,
    route: SupervisorRoute | None = None,
    task: AgentTaskEnvelope | None = None,
    result: AgentResultEnvelope | None = None,
    approval_state: AgentApprovalState = AgentApprovalState.NOT_REQUIRED,
    decision_facts: dict[str, Any] | None = None,
) -> RunContextEvent:
    """
    Build and persist one temporary context event through injected storage.

    Args:
        config: Supervisor runtime persistence dependencies.
        client: ClickHouse context client.
        parent_run_id: Stable parent run UUID.
        external_run_id: Airflow or operator run ID.
        request: Original supervisor request.
        phase: Context lifecycle phase.
        status: Current parent or specialist status.
        route: Optional deterministic route.
        task: Optional bounded specialist handoff.
        result: Optional specialist result.
        approval_state: Current human approval state.
        decision_facts: Explicit safe policy facts for this phase.

    Returns:
        Persisted RunContextEvent.
    """
    alert_id, alert_key, alert_display_id = resolve_context_alert_identity(request, result)
    event = build_run_context_event(
        parent_run_id=parent_run_id,
        external_run_id=external_run_id,
        phase=phase,
        requester=request.requester,
        status=status,
        selected_specialist=route.specialist_name if route else "",
        task_type=route.task_type if route else "",
        task_id=task.task_id if task else None,
        alert_id=alert_id,
        alert_key=alert_key,
        alert_display_id=alert_display_id,
        context_references=list(task.context_references) if task else [],
        evidence_references=list(result.evidence_references) if result else [],
        decision_facts=decision_facts,
        report_s3_uri=context_report_uri(result),
        approval_state=approval_state,
        retention_days=config.context_retention_days,
    )
    persisted_id = config.context_event_writer(client=client, event=event)

    if UUID(str(persisted_id)) != event.context_event_id:
        raise RuntimeError("Context writer returned a different event identifier.")

    return event


def persist_supervisor_incident_memory(
    config: SupervisorRuntimeConfig,
    client: Any,
    parent_run_id: UUID,
    request: SupervisorRequest,
    route: SupervisorRoute,
    result: AgentResultEnvelope,
    final_response: str,
    approval_state: AgentApprovalState,
) -> IncidentMemoryRecord | None:
    """
    Persist one durable incident outcome only when an alert identity is available.

    Args:
        config: Supervisor runtime persistence dependencies.
        client: ClickHouse context client.
        parent_run_id: Stable parent run UUID.
        request: Original supervisor request.
        route: Deterministic route used for the handoff.
        result: Terminal specialist result.
        final_response: Bounded operator-facing supervisor response.
        approval_state: Current human approval state.

    Returns:
        Persisted IncidentMemoryRecord, or None for non-incident requests.
    """
    alert_id, alert_key, alert_display_id = resolve_context_alert_identity(request, result)

    if not any((alert_id, alert_key, alert_display_id)):
        return None

    record = build_incident_memory_record(
        parent_run_id=parent_run_id,
        outcome_status=result.status,
        specialist_name=route.specialist_name,
        task_type=route.task_type,
        summary=final_response,
        alert_id=alert_id,
        alert_key=alert_key,
        alert_display_id=alert_display_id,
        evidence_references=list(result.evidence_references),
        decision_facts=build_result_context_facts(route, result),
        report_s3_uri=context_report_uri(result),
        approval_state=approval_state,
    )
    persisted_id = config.incident_memory_writer(client=client, record=record)

    if UUID(str(persisted_id)) != record.memory_id:
        raise RuntimeError("Incident-memory writer returned a different record identifier.")

    return record


def build_initial_supervisor_state(
    parent_run_id: UUID,
    request: SupervisorRequest,
) -> SupervisorState:
    """
    Build one explicit parent state from caller-supplied deterministic budgets.

    Args:
        parent_run_id: Stable parent run UUID.
        request: Typed supervisor request.

    Returns:
        Empty SupervisorState with enforced run budgets.
    """
    return SupervisorState(
        parent_run_id=parent_run_id,
        max_handoffs=request.max_handoffs,
        max_retries=request.max_retries,
        max_model_calls=request.max_model_calls,
        token_budget=request.token_budget,
        estimated_cost_budget_usd=request.estimated_cost_budget_usd,
        latency_budget_ms=request.latency_budget_ms,
    )


def reconcile_specialist_result_usage(
    result: AgentResultEnvelope,
    llm_usage: SupervisorBudgetVector,
) -> AgentResultEnvelope:
    """
    Reconcile specialist-reported usage with the supervisor provider ledger.

    Args:
        result: Specialist result built from child audit evidence.
        llm_usage: Run-scoped provider ledger snapshot.

    Returns:
        Revalidated result containing the conservative maximum for each usage field.
    """
    payload = result.model_dump(mode="python")
    payload["model_call_count"] = max(
        result.model_call_count,
        llm_usage.model_calls,
    )
    payload["token_usage"] = max(result.token_usage, llm_usage.tokens)
    payload["estimated_cost_usd"] = max(
        result.estimated_cost_usd,
        llm_usage.estimated_cost_usd,
    )

    reconciled = AgentResultEnvelope.model_validate(payload)

    logger.info(
        "Reconciled specialist usage with supervisor ledger | task_id=%s model_calls=%d tokens=%d estimated_cost_usd=%.8f",
        reconciled.task_id,
        reconciled.model_call_count,
        reconciled.token_usage,
        reconciled.estimated_cost_usd,
    )

    return reconciled


def build_budget_audit_summary(
    decision: SupervisorBudgetDecision | None,
) -> dict[str, Any]:
    """
    Build a compact JSON-safe budget summary for operator-facing run output.

    Args:
        decision: Latest pre-handoff or post-handoff policy decision.

    Returns:
        Empty mapping when no decision exists, otherwise its stable summary.
    """
    if decision is None:
        return {}

    return {
        "stage": decision.stage.value,
        "allowed": decision.allowed,
        "violations": list(decision.violations),
        "limits": decision.limits.model_dump(mode="json"),
        "usage": decision.usage.model_dump(mode="json"),
        "remaining": decision.remaining.model_dump(mode="json"),
    }


def build_resilience_audit_summary(
    circuit_snapshot: CircuitBreakerSnapshot | None,
    attempt_count: int,
    retry_count: int,
    failure_category: SupervisorFailureCategory | None = None,
) -> dict[str, Any]:
    """
    Build compact circuit, timeout, and retry evidence for operator output.

    Args:
        circuit_snapshot: Optional persistent circuit decision.
        attempt_count: Total specialist attempts started.
        retry_count: Retries actually executed.
        failure_category: Optional normalized terminal failure category.

    Returns:
        JSON-safe resilience summary.
    """
    return {
        "attempt_count": attempt_count,
        "retry_count": retry_count,
        "failure_category": failure_category.value if failure_category else "",
        "circuit": circuit_snapshot_payload(circuit_snapshot),
    }


# --- Defining Resilient Invocation Models
@dataclass(frozen=True)
class SpecialistInvocationOutcome:
    """
    Return one terminal specialist result with explicit attempt accounting.

    Attributes:
        result: Terminal structured specialist result.
        retry_count: Number of retries actually executed.
        attempt_count: Total specialist attempts including the initial attempt.
    """

    result: AgentResultEnvelope
    retry_count: int
    attempt_count: int


# --- Defining Resilient Invocation Helpers
def apply_specialist_timeout_cap(
    task: AgentTaskEnvelope,
    timeout_cap_seconds: int | None,
) -> AgentTaskEnvelope:
    """
    Reduce a specialist timeout for stricter runtime environments or smoke tests.

    Args:
        task: Policy-built specialist task.
        timeout_cap_seconds: Optional upper bound that cannot increase the declared timeout.

    Returns:
        Original task or validated copy with a lower timeout.
    """
    if timeout_cap_seconds is None or timeout_cap_seconds >= task.timeout_seconds:
        return task

    capped_task = AgentTaskEnvelope.model_validate(
        {
            **task.model_dump(mode="python"),
            "timeout_seconds": timeout_cap_seconds,
        }
    )

    logger.info(
        "Applied specialist timeout cap | task_id=%s declared_seconds=%d capped_seconds=%d",
        task.task_id,
        task.timeout_seconds,
        capped_task.timeout_seconds,
    )

    return capped_task


def build_attempt_resilience_payload(
    capability: AgentCapabilitySpec,
    attempt_number: int,
    retries_consumed: int,
    max_retries: int,
    deadline_monotonic: float,
    failure_category: SupervisorFailureCategory | None = None,
    retry_scheduled: bool = False,
    backoff_seconds: float = 0.0,
) -> dict[str, Any]:
    """
    Build bounded timeout and retry evidence for one supervisor audit event.

    Args:
        capability: Registry capability controlling retry permission.
        attempt_number: One-based specialist attempt.
        retries_consumed: Retries already executed.
        max_retries: Parent retry budget.
        deadline_monotonic: Shared absolute specialist deadline.
        failure_category: Optional normalized failure category.
        retry_scheduled: Whether another attempt was approved.
        backoff_seconds: Deterministic delay before the approved retry.

    Returns:
        JSON-safe audit payload without raw exception content.
    """
    return {
        "attempt_number": attempt_number,
        "retries_consumed": retries_consumed,
        "max_retries": max_retries,
        "retry_safe": capability.retry_safe,
        "retry_scheduled": retry_scheduled,
        "backoff_ms": int(backoff_seconds * 1_000),
        "deadline_remaining_ms": max(
            0,
            int((deadline_monotonic - time.monotonic()) * 1_000),
        ),
        "failure_category": failure_category.value if failure_category else "",
    }


def resolve_retry_schedule(
    capability: AgentCapabilitySpec,
    retries_consumed: int,
    max_retries: int,
    deadline_monotonic: float,
) -> tuple[bool, float, bool]:
    """
    Decide whether policy and remaining deadline permit one retry.

    Args:
        capability: Registry capability controlling retry permission.
        retries_consumed: Retries already executed for this handoff.
        max_retries: Parent retry budget.
        deadline_monotonic: Shared absolute specialist deadline.

    Returns:
        Tuple containing retry approval, backoff seconds, and deadline rejection.
    """
    if not retry_is_allowed(
        capability=capability,
        retries_consumed=retries_consumed,
        max_retries=max_retries,
    ):
        return False, 0.0, False

    backoff_seconds  = retry_backoff_seconds(retries_consumed)
    deadline_blocked = time.monotonic() + backoff_seconds >= deadline_monotonic

    return not deadline_blocked, backoff_seconds, deadline_blocked


def invoke_specialist_with_resilience(
    task: AgentTaskEnvelope,
    request: SupervisorRequest,
    route: SupervisorRoute,
    state: SupervisorState,
    config: SupervisorRuntimeConfig,
    client: Any,
    parent_run_id: UUID,
    deadline_monotonic: float,
) -> SpecialistInvocationOutcome:
    """
    Execute one specialist with hard deadline and capability-bounded retries.

    Args:
        task: Validated specialist handoff.
        request: Parent operator request used for audit correlation.
        route: Deterministic supervisor route.
        state: Parent state containing retry budget.
        config: Runtime dependencies and retry sleeper.
        client: ClickHouse audit client.
        parent_run_id: Stable parent correlation UUID.
        deadline_monotonic: Absolute deadline shared by every attempt.

    Returns:
        Terminal result plus actual attempt and retry counts.

    Raises:
        SupervisorHardTimeout: If an attempt or retry window exhausts the deadline.
        SupervisorHardTimeoutUnavailable: If hard cancellation is unavailable.
        Exception: If a non-envelope specialist failure cannot be safely retried.
    """
    capability      = get_agent_capability(task.specialist_name)
    retries_consumed = 0
    attempt_number   = 1

    if state.max_retries > 0 and not capability.retry_safe:
        raise PermissionError(
            f"Specialist {task.specialist_name} is not eligible for automatic retries."
        )

    while True:
        write_supervisor_audit(
            config=config,
            client=client,
            parent_run_id=parent_run_id,
            request=request,
            action="supervisor_specialist_attempt_started",
            status="running",
            route=route,
            task=task,
            resilience_payload=build_attempt_resilience_payload(
                capability=capability,
                attempt_number=attempt_number,
                retries_consumed=retries_consumed,
                max_retries=state.max_retries,
                deadline_monotonic=deadline_monotonic,
            ),
        )

        try:
            with enforce_hard_deadline(
                deadline_monotonic=deadline_monotonic,
                specialist_name=task.specialist_name,
                attempt_number=attempt_number,
            ):
                candidate = invoke_selected_specialist(task=task, config=config)
                candidate = enforce_result_contract(task=task, result=candidate)

        except SupervisorHardTimeout as exc:
            # The absolute task deadline has already elapsed. A retry cannot fit
            # inside the same budget and must not be advertised as scheduled.
            retry_allowed = False
            write_supervisor_audit(
                config=config,
                client=client,
                parent_run_id=parent_run_id,
                request=request,
                action="supervisor_specialist_attempt_timed_out",
                status="timed_out",
                route=route,
                task=task,
                resilience_payload=build_attempt_resilience_payload(
                    capability=capability,
                    attempt_number=attempt_number,
                    retries_consumed=retries_consumed,
                    max_retries=state.max_retries,
                    deadline_monotonic=deadline_monotonic,
                    failure_category=SupervisorFailureCategory.HARD_TIMEOUT,
                    retry_scheduled=retry_allowed,
                ),
                error_message=sanitize_supervisor_error(exc),
            )

            if not retry_allowed:
                raise SupervisorInvocationFailure(
                    category=SupervisorFailureCategory.HARD_TIMEOUT,
                    retry_count=retries_consumed,
                    attempt_count=attempt_number,
                    cause=exc,
                ) from exc

        except Exception as exc:
            failure_category = (
                SupervisorFailureCategory.TRANSIENT_FAILURE
                if is_retryable_exception(exc)
                else SupervisorFailureCategory.SPECIALIST_FAILED
            )
            retry_allowed      = False
            backoff_seconds    = 0.0
            deadline_rejected  = False

            if failure_category == SupervisorFailureCategory.TRANSIENT_FAILURE:
                retry_allowed, backoff_seconds, deadline_rejected = resolve_retry_schedule(
                    capability=capability,
                    retries_consumed=retries_consumed,
                    max_retries=state.max_retries,
                    deadline_monotonic=deadline_monotonic,
                )

            write_supervisor_audit(
                config=config,
                client=client,
                parent_run_id=parent_run_id,
                request=request,
                action="supervisor_specialist_attempt_failed",
                status="failed",
                route=route,
                task=task,
                resilience_payload=build_attempt_resilience_payload(
                    capability=capability,
                    attempt_number=attempt_number,
                    retries_consumed=retries_consumed,
                    max_retries=state.max_retries,
                    deadline_monotonic=deadline_monotonic,
                    failure_category=failure_category,
                    retry_scheduled=retry_allowed,
                ),
                error_message=sanitize_supervisor_error(exc),
            )

            if not retry_allowed:
                category = (
                    SupervisorFailureCategory.UNAVAILABLE_TIMER
                    if isinstance(exc, SupervisorHardTimeoutUnavailable)
                    else (
                        SupervisorFailureCategory.HARD_TIMEOUT
                        if deadline_rejected
                        else failure_category
                    )
                )

                raise SupervisorInvocationFailure(
                    category=category,
                    retry_count=retries_consumed,
                    attempt_count=attempt_number,
                    cause=exc,
                ) from exc

        else:
            retryable_result = is_retryable_result(candidate)
            retry_allowed    = False
            backoff_seconds  = 0.0

            if retryable_result:
                retry_allowed, backoff_seconds, _ = resolve_retry_schedule(
                    capability=capability,
                    retries_consumed=retries_consumed,
                    max_retries=state.max_retries,
                    deadline_monotonic=deadline_monotonic,
                )

            if not retryable_result:
                write_supervisor_audit(
                    config=config,
                    client=client,
                    parent_run_id=parent_run_id,
                    request=request,
                    action="supervisor_specialist_attempt_completed",
                    status=candidate.status.value,
                    route=route,
                    task=task,
                    result=candidate,
                    resilience_payload=build_attempt_resilience_payload(
                        capability=capability,
                        attempt_number=attempt_number,
                        retries_consumed=retries_consumed,
                        max_retries=state.max_retries,
                        deadline_monotonic=deadline_monotonic,
                    ),
                )

                return SpecialistInvocationOutcome(
                    result=candidate,
                    retry_count=retries_consumed,
                    attempt_count=attempt_number,
                )

            error_message = "; ".join(candidate.errors)[:2_000]
            write_supervisor_audit(
                config=config,
                client=client,
                parent_run_id=parent_run_id,
                request=request,
                action="supervisor_specialist_attempt_failed",
                status="failed",
                route=route,
                task=task,
                result=candidate,
                resilience_payload=build_attempt_resilience_payload(
                    capability=capability,
                    attempt_number=attempt_number,
                    retries_consumed=retries_consumed,
                    max_retries=state.max_retries,
                    deadline_monotonic=deadline_monotonic,
                    failure_category=SupervisorFailureCategory.TRANSIENT_FAILURE,
                    retry_scheduled=retry_allowed,
                ),
                error_message=error_message,
            )

            if not retry_allowed:
                return SpecialistInvocationOutcome(
                    result=candidate,
                    retry_count=retries_consumed,
                    attempt_count=attempt_number,
                )

        write_supervisor_audit(
            config=config,
            client=client,
            parent_run_id=parent_run_id,
            request=request,
            action="supervisor_specialist_retry_scheduled",
            status="running",
            route=route,
            task=task,
            resilience_payload=build_attempt_resilience_payload(
                capability=capability,
                attempt_number=attempt_number,
                retries_consumed=retries_consumed,
                max_retries=state.max_retries,
                deadline_monotonic=deadline_monotonic,
                failure_category=SupervisorFailureCategory.TRANSIENT_FAILURE,
                retry_scheduled=True,
                backoff_seconds=backoff_seconds,
            ),
        )
        config.sleep_callable(backoff_seconds)
        retries_consumed += 1
        attempt_number   += 1


def invoke_selected_specialist(
    task: AgentTaskEnvelope,
    config: SupervisorRuntimeConfig,
) -> AgentResultEnvelope:
    """
    Invoke exactly one registered specialist from deterministic route policy.

    Args:
        task: Validated specialist handoff.
        config: Runtime dependency configuration.

    Returns:
        Terminal AgentResultEnvelope.

    Raises:
        SupervisorRoutingError: If no runtime runner exists for the specialist.
    """
    if task.specialist_name == INCIDENT_TRIAGE_SPECIALIST_NAME:
        return config.incident_runner(task=task, config=config.incident_config)

    if task.specialist_name == METADATA_LINEAGE_SPECIALIST_NAME:
        return config.metadata_lineage_runner(
            task=task,
            config=config.metadata_lineage_config,
        )

    if task.specialist_name == SQL_REVIEW_SPECIALIST_NAME:
        return config.sql_review_runner(
            task=task,
            config=config.sql_review_config,
        )

    if task.specialist_name == SCHEMA_DRIFT_SPECIALIST_NAME:
        return config.schema_drift_runner(
            task=task,
            config=config.schema_drift_config,
        )

    raise SupervisorRoutingError(
        f"No runtime runner is registered for specialist={task.specialist_name}."
    )


def build_final_response(
    route: SupervisorRoute,
    result: AgentResultEnvelope,
) -> str:
    """
    Build a deterministic operator response from one specialist result.

    Args:
        route: Deterministic supervisor route.
        result: Terminal specialist result.

    Returns:
        Bounded human-readable parent response.
    """
    if result.status == AgentTaskStatus.SUCCESS:
        summary = str(result.structured_output.get("summary", "")).strip()

        return (
            f"{route.specialist_name} completed {route.task_type}. "
            f"{summary or result.recommended_next_step}"
        )[:20_000]

    error_text = result.errors[0] if result.errors else "No specialist error was retained."

    return (
        f"{route.specialist_name} ended with status {result.status.value}. "
        f"{error_text} {result.recommended_next_step}"
    )[:20_000]


def terminal_approval_state(
    result: AgentResultEnvelope,
    routing_policy_decision: SupervisorRoutingPolicyDecision | None = None,
) -> AgentApprovalState:
    """
    Map specialist recommendations to explicit human approval state.

    Args:
        result: Terminal specialist result.
        routing_policy_decision: Optional terminal risk and review policy decision.

    Returns:
        PENDING when an action requires approval, otherwise NOT_REQUIRED.
    """
    if (
        result.requires_human_approval
        or (
            routing_policy_decision is not None
            and routing_policy_decision.human_approval_required
        )
    ):
        return AgentApprovalState.PENDING

    return AgentApprovalState.NOT_REQUIRED


# --- Defining Supervisor Runtime
def run_control_plane_supervisor(
    request: SupervisorRequest,
    external_run_id: str,
    config: SupervisorRuntimeConfig | None = None,
) -> SupervisorRunResult:
    """
    Route and execute one bounded specialist handoff with parent-run auditability.

    Args:
        request: Typed operator request.
        external_run_id: Stable Airflow or operator run identifier.
        config: Optional runtime dependency overrides.

    Returns:
        SupervisorRunResult containing explicit state and failure-isolation evidence.
    """
    runtime       = config or SupervisorRuntimeConfig()
    parent_run_id = derive_supervisor_parent_run_id(external_run_id)
    state         = build_initial_supervisor_state(parent_run_id, request)
    started       = time.monotonic()
    client        = runtime.audit_client_factory(
        host=runtime.clickhouse_host,
        port=runtime.clickhouse_port,
    )
    route: SupervisorRoute | None              = None
    task: AgentTaskEnvelope | None             = None
    result: AgentResultEnvelope | None         = None
    terminal_record: HandoffRecord | None      = None
    latest_budget_decision: SupervisorBudgetDecision | None = None
    pre_routing_policy: SupervisorRoutingPolicyDecision | None = None
    post_routing_policy: SupervisorRoutingPolicyDecision | None = None
    circuit_snapshot: CircuitBreakerSnapshot | None = None
    retry_count                              = 0
    attempt_count                            = 0
    outcome_audit_written                    = False
    context_event_ids: list[UUID]              = []
    incident_memory_ids: list[UUID]            = []
    context_tables_ready                       = False
    current_stage                              = "initialize_context"

    try:
        runtime.context_schema_ensurer(client)
        context_tables_ready = True

        started_context = persist_supervisor_context_event(
            config=runtime,
            client=client,
            parent_run_id=parent_run_id,
            external_run_id=external_run_id,
            request=request,
            phase=RunContextPhase.STARTED,
            status=AgentTaskStatus.RUNNING,
            decision_facts=build_started_context_facts(request),
        )
        context_event_ids.append(started_context.context_event_id)
        write_supervisor_audit(
            config=runtime,
            client=client,
            parent_run_id=parent_run_id,
            request=request,
            action="supervisor_run_started",
            status="running",
            context_event_id=started_context.context_event_id,
        )

        current_stage = "resolve_route"
        route = resolve_supervisor_route(request)
        write_supervisor_audit(
            config=runtime,
            client=client,
            parent_run_id=parent_run_id,
            request=request,
            action="supervisor_intent_classified",
            status="success",
            route=route,
            context_event_id=started_context.context_event_id,
        )
        current_stage = "build_handoff"
        task = build_supervisor_handoff(
            request=request,
            route=route,
            parent_run_id=parent_run_id,
        )
        task = apply_specialist_timeout_cap(
            task=task,
            timeout_cap_seconds=runtime.specialist_timeout_cap_seconds,
        )
        pre_routing_policy = evaluate_pre_handoff_policy(task)
        current_stage = "evaluate_pre_handoff_budget"
        latest_budget_decision = evaluate_pre_handoff_budgets(
            task=task,
            state=state,
            elapsed_ms=int((time.monotonic() - started) * 1_000),
        )
        write_supervisor_audit(
            config=runtime,
            client=client,
            parent_run_id=parent_run_id,
            request=request,
            action="supervisor_budget_prechecked",
            status="success" if latest_budget_decision.allowed else "blocked",
            route=route,
            task=task,
            budget_decision=latest_budget_decision,
            routing_policy_decision=pre_routing_policy,
        )
        require_budget_decision(latest_budget_decision)
        current_stage = "evaluate_circuit_breaker"
        circuit_snapshot = runtime.circuit_snapshot_loader(
            client=client,
            specialist_name=task.specialist_name,
            policy=runtime.circuit_policy,
        )
        write_supervisor_audit(
            config=runtime,
            client=client,
            parent_run_id=parent_run_id,
            request=request,
            action="supervisor_circuit_checked",
            status="success" if circuit_snapshot.request_allowed else "blocked",
            route=route,
            task=task,
            resilience_payload={
                "circuit": circuit_snapshot_payload(circuit_snapshot),
            },
        )

        if not circuit_snapshot.request_allowed:
            write_supervisor_audit(
                config=runtime,
                client=client,
                parent_run_id=parent_run_id,
                request=request,
                action="supervisor_circuit_opened",
                status="blocked",
                route=route,
                task=task,
                resilience_payload={
                    "circuit": circuit_snapshot_payload(circuit_snapshot),
                    "failure_category": SupervisorFailureCategory.CIRCUIT_OPEN.value,
                },
            )

        require_circuit_allows(circuit_snapshot)
        task = attach_run_context_reference(
            task=task,
            context_event_id=started_context.context_event_id,
        )
        routed_context = persist_supervisor_context_event(
            config=runtime,
            client=client,
            parent_run_id=parent_run_id,
            external_run_id=external_run_id,
            request=request,
            phase=RunContextPhase.ROUTED,
            status=AgentTaskStatus.RUNNING,
            route=route,
            task=task,
            decision_facts={
                **build_route_context_facts(route, task),
                "routing_policy": pre_routing_policy.model_dump(mode="json"),
            },
        )
        context_event_ids.append(routed_context.context_event_id)
        write_supervisor_audit(
            config=runtime,
            client=client,
            parent_run_id=parent_run_id,
            request=request,
            action="supervisor_route_selected",
            status="success",
            route=route,
            task=task,
            context_event_id=routed_context.context_event_id,
            routing_policy_decision=pre_routing_policy,
        )

        state.active_task = task
        current_stage     = "invoke_specialist"
        parent_deadline   = started + (state.latency_budget_ms / 1_000)
        task_deadline     = time.monotonic() + task.timeout_seconds
        deadline          = min(parent_deadline, task_deadline)
        write_supervisor_audit(
            config=runtime,
            client=client,
            parent_run_id=parent_run_id,
            request=request,
            action="supervisor_handoff_started",
            status="running",
            route=route,
            task=task,
            resilience_payload=build_resilience_audit_summary(
                circuit_snapshot=circuit_snapshot,
                attempt_count=0,
                retry_count=0,
            ),
        )

        # The context-local ledger is visible to every nested provider attempt,
        # including configured provider fallback and schema-compatibility retry.
        with external_llm_permission_scope(request.allow_external_llm):
            with supervisor_llm_budget_scope(
                max_model_calls=task.model_call_budget,
                token_budget=task.token_budget,
                estimated_cost_budget_usd=task.estimated_cost_budget_usd,
                deadline_monotonic=deadline,
            ) as llm_ledger:
                invocation = invoke_specialist_with_resilience(
                    task=task,
                    request=request,
                    route=route,
                    state=state,
                    config=runtime,
                    client=client,
                    parent_run_id=parent_run_id,
                    deadline_monotonic=deadline,
                )
                result        = invocation.result
                retry_count   = invocation.retry_count
                attempt_count = invocation.attempt_count
                llm_usage = llm_ledger.snapshot(
                    latency_ms=int((time.monotonic() - started) * 1_000),
                )

        result = reconcile_specialist_result_usage(
            result=result,
            llm_usage=llm_usage,
        )
        post_routing_policy = evaluate_post_handoff_policy(
            task=task,
            result=result,
        )
        terminal_record = HandoffRecord(
            task_id=task.task_id,
            specialist_name=task.specialist_name,
            task_type=task.task_type,
            status=result.status,
            completed_at=datetime.now(timezone.utc),
            duration_ms=result.duration_ms,
            retry_count=retry_count,
            error_message="; ".join(result.errors)[:2_000],
        )
        write_supervisor_audit(
            config=runtime,
            client=client,
            parent_run_id=parent_run_id,
            request=request,
            action="supervisor_specialist_outcome",
            status=result.status.value,
            route=route,
            task=task,
            result=result,
            routing_policy_decision=post_routing_policy,
            resilience_payload={
                "attempt_count": attempt_count,
                "retry_count": retry_count,
                "circuit": circuit_snapshot_payload(circuit_snapshot),
            },
            duration_ms=result.duration_ms,
            error_message="; ".join(result.errors)[:2_000],
        )
        outcome_audit_written = True
        write_supervisor_audit(
            config=runtime,
            client=client,
            parent_run_id=parent_run_id,
            request=request,
            action="supervisor_handoff_completed",
            status=result.status.value,
            route=route,
            task=task,
            result=result,
            routing_policy_decision=post_routing_policy,
            resilience_payload=build_resilience_audit_summary(
                circuit_snapshot=circuit_snapshot,
                attempt_count=attempt_count,
                retry_count=retry_count,
            ),
            duration_ms=result.duration_ms,
            error_message="; ".join(result.errors)[:2_000],
        )
        current_stage = "evaluate_post_handoff_budget"
        latest_budget_decision = evaluate_post_handoff_budgets(
            state=state,
            record=terminal_record,
            result=result,
            elapsed_ms=int((time.monotonic() - started) * 1_000),
        )
        write_supervisor_audit(
            config=runtime,
            client=client,
            parent_run_id=parent_run_id,
            request=request,
            action="supervisor_budget_reconciled",
            status="success" if latest_budget_decision.allowed else "blocked",
            route=route,
            task=task,
            result=result,
            budget_decision=latest_budget_decision,
            routing_policy_decision=post_routing_policy,
        )
        require_budget_decision(latest_budget_decision)

        final_response = build_final_response(route=route, result=result)

        if (
            post_routing_policy.human_approval_required
            and not result.requires_human_approval
        ):
            final_response = (
                f"{final_response} Human review is required because the terminal routing "
                "policy did not receive sufficient strong-review evidence."
            )[:20_000]

        approval_state = terminal_approval_state(
            result=result,
            routing_policy_decision=post_routing_policy,
        )
        current_stage  = "persist_final_context"
        completed_context = persist_supervisor_context_event(
            config=runtime,
            client=client,
            parent_run_id=parent_run_id,
            external_run_id=external_run_id,
            request=request,
            phase=RunContextPhase.COMPLETED,
            status=result.status,
            route=route,
            task=task,
            result=result,
            approval_state=approval_state,
            decision_facts={
                **build_result_context_facts(route, result),
                "routing_policy": post_routing_policy.model_dump(mode="json"),
            },
        )
        context_event_ids.append(completed_context.context_event_id)
        incident_memory = persist_supervisor_incident_memory(
            config=runtime,
            client=client,
            parent_run_id=parent_run_id,
            request=request,
            route=route,
            result=result,
            final_response=final_response,
            approval_state=approval_state,
        )

        if incident_memory:
            incident_memory_ids.append(incident_memory.memory_id)

        # Re-validate the complete snapshot so aggregate token, cost, latency, and
        # handoff budgets cannot be bypassed by mutating Pydantic lists in place.
        completed_state = SupervisorState(
            parent_run_id=state.parent_run_id,
            active_task=None,
            handoff_history=[terminal_record],
            specialist_results=[result],
            run_context_event_ids=context_event_ids,
            incident_memory_ids=incident_memory_ids,
            approval_state=approval_state,
            max_handoffs=state.max_handoffs,
            max_retries=state.max_retries,
            max_model_calls=state.max_model_calls,
            token_budget=state.token_budget,
            estimated_cost_budget_usd=state.estimated_cost_budget_usd,
            latency_budget_ms=state.latency_budget_ms,
            errors=list(result.errors),
            final_response=final_response,
        )
        duration_ms = int((time.monotonic() - started) * 1_000)
        write_supervisor_audit(
            config=runtime,
            client=client,
            parent_run_id=parent_run_id,
            request=request,
            action="supervisor_final_decision",
            status=result.status.value,
            route=route,
            task=task,
            result=result,
            context_event_id=completed_context.context_event_id,
            incident_memory_id=(incident_memory.memory_id if incident_memory else None),
            approval_state=approval_state,
            budget_decision=latest_budget_decision,
            routing_policy_decision=post_routing_policy,
            resilience_payload=build_resilience_audit_summary(
                circuit_snapshot=circuit_snapshot,
                attempt_count=attempt_count,
                retry_count=retry_count,
            ),
            duration_ms=duration_ms,
            error_message="; ".join(result.errors)[:2_000],
        )

        logger.info(
            "Control Plane Supervisor completed | parent_run_id=%s intent=%s specialist=%s status=%s handoffs=1",
            parent_run_id,
            route.intent.value,
            route.specialist_name,
            result.status.value,
        )

        return SupervisorRunResult(
            status=result.status,
            parent_run_id=parent_run_id,
            requested_intent=request.intent,
            resolved_intent=route.intent,
            selected_specialist=route.specialist_name,
            task_type=route.task_type,
            task_id=task.task_id,
            final_response=final_response,
            supervisor_state=completed_state,
            failure_isolated=result.status in {
                AgentTaskStatus.PARTIAL,
                AgentTaskStatus.BLOCKED,
                AgentTaskStatus.FAILED,
            },
            audit_summary={
                "supervisor_decisions": 5,
                "specialist_handoffs": 1,
                "parent_run_id": str(parent_run_id),
                "run_context_event_count": len(context_event_ids),
                "incident_memory_count": len(incident_memory_ids),
                "budget": build_budget_audit_summary(latest_budget_decision),
                "routing_policy": {
                    "pre_handoff": (
                        pre_routing_policy.model_dump(mode="json")
                        if pre_routing_policy
                        else {}
                    ),
                    "post_evidence": post_routing_policy.model_dump(mode="json"),
                },
                "resilience": build_resilience_audit_summary(
                    circuit_snapshot=circuit_snapshot,
                    attempt_count=attempt_count,
                    retry_count=retry_count,
                ),
            },
        )

    except (SupervisorHardTimeout, Exception) as exc:
        duration_ms    = int((time.monotonic() - started) * 1_000)
        error_message  = sanitize_supervisor_error(exc)
        budget_failure = isinstance(exc, SupervisorBudgetExceeded)
        resilience_failure = isinstance(exc, SupervisorInvocationFailure)
        circuit_failure    = isinstance(exc, SupervisorCircuitOpen)
        failure_category: SupervisorFailureCategory | None = None

        if resilience_failure:
            retry_count     = exc.retry_count
            attempt_count   = exc.attempt_count
            failure_category = exc.category

        elif circuit_failure:
            circuit_snapshot = exc.snapshot
            failure_category = SupervisorFailureCategory.CIRCUIT_OPEN

        elif isinstance(exc, SupervisorHardTimeout):
            attempt_count    = max(1, attempt_count)
            failure_category = SupervisorFailureCategory.HARD_TIMEOUT

        elif not budget_failure:
            failure_category = SupervisorFailureCategory.SPECIALIST_FAILED

        if budget_failure:
            latest_budget_decision = exc.decision
            failure_category       = SupervisorFailureCategory.POLICY_BLOCKED

        parent_status = (
            AgentTaskStatus.PARTIAL
            if result and result.status in {AgentTaskStatus.SUCCESS, AgentTaskStatus.PARTIAL}
            else AgentTaskStatus.BLOCKED
        )
        approval_state = (
            terminal_approval_state(result)
            if result and not budget_failure
            else AgentApprovalState.NOT_REQUIRED
        )
        if budget_failure:
            violations = ", ".join(latest_budget_decision.violations)
            final_response = (
                "The supervisor stopped this request because its approved run budget was "
                f"exceeded at {latest_budget_decision.stage.value}: {violations}. "
                "No remediation was executed. Review the retained audit evidence before "
                "changing the budget or retrying."
            )[:20_000]

        elif failure_category == SupervisorFailureCategory.HARD_TIMEOUT:
            final_response = (
                f"The supervisor interrupted {route.specialist_name if route else 'the specialist'} "
                "after its hard deadline. No timed-out output was accepted and no remediation "
                "was executed. Review attempt and timeout audit evidence before retrying."
            )[:20_000]

        elif failure_category == SupervisorFailureCategory.CIRCUIT_OPEN:
            final_response = (
                f"The supervisor blocked {route.specialist_name if route else 'the specialist'} "
                "because its recent failure circuit is open. No handoff or remediation was "
                "executed; wait for the audited cooldown before a half-open probe."
            )[:20_000]

        else:
            final_response = (
                "The specialist completed, but the supervisor could not persist all required "
                f"control-plane context. {error_message}"
                if result
                else "The supervisor blocked this request before another specialist could run. "
                + error_message
            )[:20_000]

        blocked_context: RunContextEvent | None = None
        incident_memory: IncidentMemoryRecord | None = None

        if context_tables_ready:
            try:
                blocked_context = persist_supervisor_context_event(
                    config=runtime,
                    client=client,
                    parent_run_id=parent_run_id,
                    external_run_id=external_run_id,
                    request=request,
                    phase=RunContextPhase.BLOCKED,
                    status=parent_status,
                    route=route,
                    task=task,
                    result=None if budget_failure else result,
                    approval_state=approval_state,
                    decision_facts={
                        "failure_stage": current_stage,
                        "error_type": type(exc).__name__,
                        "specialist_completed": result is not None,
                        "budget": build_budget_audit_summary(latest_budget_decision),
                        "resilience": build_resilience_audit_summary(
                            circuit_snapshot=circuit_snapshot,
                            attempt_count=attempt_count,
                            retry_count=retry_count,
                            failure_category=failure_category,
                        ),
                    },
                )
                context_event_ids.append(blocked_context.context_event_id)

                if route and result and not budget_failure:
                    incident_memory = persist_supervisor_incident_memory(
                        config=runtime,
                        client=client,
                        parent_run_id=parent_run_id,
                        request=request,
                        route=route,
                        result=result,
                        final_response=final_response,
                        approval_state=approval_state,
                    )

                    if incident_memory:
                        incident_memory_ids.append(incident_memory.memory_id)
            except Exception:
                logger.exception(
                    "Failed to persist blocked supervisor context | parent_run_id=%s stage=%s",
                    parent_run_id,
                    current_stage,
                )

        handoff_attempted = bool(
            terminal_record
            or current_stage
            in {
                "invoke_specialist",
                "evaluate_post_handoff_budget",
                "persist_final_context",
            }
        )
        handoff_rejected = bool(
            task
            and not handoff_attempted
            and (budget_failure or circuit_failure)
        )
        blocked_handoff = terminal_record

        if handoff_rejected:
            try:
                write_supervisor_audit(
                    config=runtime,
                    client=client,
                    parent_run_id=parent_run_id,
                    request=request,
                    action="supervisor_handoff_rejected",
                    status="blocked",
                    route=route,
                    task=task,
                    context_event_id=(
                        blocked_context.context_event_id
                        if blocked_context
                        else None
                    ),
                    approval_state=approval_state,
                    budget_decision=latest_budget_decision,
                    resilience_payload=build_resilience_audit_summary(
                        circuit_snapshot=circuit_snapshot,
                        attempt_count=0,
                        retry_count=0,
                        failure_category=failure_category,
                    ),
                    duration_ms=duration_ms,
                    error_message=error_message,
                )

            except Exception:
                logger.exception(
                    "Failed to persist rejected handoff audit | parent_run_id=%s stage=%s",
                    parent_run_id,
                    current_stage,
                )

        if task and handoff_attempted and blocked_handoff is None:
            blocked_handoff = HandoffRecord(
                task_id=task.task_id,
                specialist_name=task.specialist_name,
                task_type=task.task_type,
                status=result.status if result else AgentTaskStatus.BLOCKED,
                completed_at=datetime.now(timezone.utc),
                duration_ms=duration_ms,
                retry_count=retry_count,
                error_message=error_message,
            )

        blocked_state = SupervisorState(
            parent_run_id=state.parent_run_id,
            active_task=None,
            handoff_history=[blocked_handoff] if blocked_handoff else [],
            # Over-budget specialist output remains in append-only audit evidence,
            # but is not accepted into parent state or durable incident memory.
            specialist_results=(
                [result]
                if result and not budget_failure
                else []
            ),
            run_context_event_ids=context_event_ids,
            incident_memory_ids=incident_memory_ids,
            approval_state=approval_state,
            max_handoffs=state.max_handoffs,
            max_retries=state.max_retries,
            max_model_calls=state.max_model_calls,
            token_budget=state.token_budget,
            estimated_cost_budget_usd=state.estimated_cost_budget_usd,
            latency_budget_ms=state.latency_budget_ms,
            errors=[error_message],
            final_response=final_response,
        )

        if task and handoff_attempted and terminal_record is None:
            try:
                write_supervisor_audit(
                    config=runtime,
                    client=client,
                    parent_run_id=parent_run_id,
                    request=request,
                    action="supervisor_handoff_failed",
                    status=(
                        "timed_out"
                        if failure_category == SupervisorFailureCategory.HARD_TIMEOUT
                        else "failed"
                    ),
                    route=route,
                    task=task,
                    approval_state=approval_state,
                    resilience_payload=build_resilience_audit_summary(
                        circuit_snapshot=circuit_snapshot,
                        attempt_count=attempt_count,
                        retry_count=retry_count,
                        failure_category=failure_category,
                    ),
                    duration_ms=duration_ms,
                    error_message=error_message,
                )

            except Exception:
                logger.exception(
                    "Failed to persist failed handoff audit | parent_run_id=%s stage=%s",
                    parent_run_id,
                    current_stage,
                )

        if task and handoff_attempted and not outcome_audit_written:
            outcome_status = (
                "timed_out"
                if failure_category == SupervisorFailureCategory.HARD_TIMEOUT
                else "failed"
            )

            try:
                write_supervisor_audit(
                    config=runtime,
                    client=client,
                    parent_run_id=parent_run_id,
                    request=request,
                    action="supervisor_specialist_outcome",
                    status=outcome_status,
                    route=route,
                    task=task,
                    resilience_payload=build_resilience_audit_summary(
                        circuit_snapshot=circuit_snapshot,
                        attempt_count=attempt_count,
                        retry_count=retry_count,
                        failure_category=failure_category,
                    ),
                    duration_ms=duration_ms,
                    error_message=error_message,
                )
                outcome_audit_written = True

            except Exception:
                logger.exception(
                    "Failed to persist terminal specialist outcome | parent_run_id=%s",
                    parent_run_id,
                )

        if budget_failure:
            try:
                write_supervisor_audit(
                    config=runtime,
                    client=client,
                    parent_run_id=parent_run_id,
                    request=request,
                    action="supervisor_budget_exceeded",
                    status=parent_status.value,
                    route=route,
                    task=task,
                    result=result,
                    context_event_id=(
                        blocked_context.context_event_id
                        if blocked_context
                        else None
                    ),
                    budget_decision=latest_budget_decision,
                    duration_ms=duration_ms,
                    error_message=error_message,
                )

            except Exception:
                logger.exception(
                    "Failed to persist supervisor budget failure audit | parent_run_id=%s stage=%s",
                    parent_run_id,
                    current_stage,
                )

        write_supervisor_audit(
            config=runtime,
            client=client,
            parent_run_id=parent_run_id,
            request=request,
            action="supervisor_final_decision",
            status=parent_status.value,
            route=route,
            task=task,
            result=result,
            context_event_id=(blocked_context.context_event_id if blocked_context else None),
            incident_memory_id=(incident_memory.memory_id if incident_memory else None),
            approval_state=approval_state,
            budget_decision=latest_budget_decision,
            resilience_payload=build_resilience_audit_summary(
                circuit_snapshot=circuit_snapshot,
                attempt_count=attempt_count,
                retry_count=retry_count,
                failure_category=failure_category,
            ),
            duration_ms=duration_ms,
            error_message=error_message,
        )

        logger.warning(
            "Control Plane Supervisor contained failure | parent_run_id=%s status=%s stage=%s error=%s",
            parent_run_id,
            parent_status.value,
            current_stage,
            error_message,
        )

        return SupervisorRunResult(
            status=parent_status,
            parent_run_id=parent_run_id,
            requested_intent=request.intent,
            resolved_intent=route.intent if route else None,
            selected_specialist=route.specialist_name if route else "",
            task_type=route.task_type if route else "",
            task_id=task.task_id if task else None,
            final_response=blocked_state.final_response,
            supervisor_state=blocked_state,
            failure_isolated=True,
            audit_summary={
                "supervisor_decisions": 1,
                "specialist_handoffs": 1 if blocked_handoff else 0,
                "parent_run_id": str(parent_run_id),
                "run_context_event_count": len(context_event_ids),
                "incident_memory_count": len(incident_memory_ids),
                "budget": build_budget_audit_summary(latest_budget_decision),
                "resilience": build_resilience_audit_summary(
                    circuit_snapshot=circuit_snapshot,
                    attempt_count=attempt_count,
                    retry_count=retry_count,
                    failure_category=failure_category,
                ),
            },
        )
