####
## Incident Triage Specialist for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Wrap the existing LangGraph triage workflow in one bounded specialist contract."""

# --- Importing Libraries
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent.graph import (
    DEFAULT_CONFIDENCE_TARGET,
    DEFAULT_MAX_EVIDENCE_LOOP,
    TriageRuntimeConfig,
    run_triage,
)
from agent.llm.config import ModelRoutingConfig, load_model_routing_config
from agent.specialists.contracts import (
    AgentModelRoute,
    AgentResultEnvelope,
    AgentRiskTier,
    AgentTaskEnvelope,
    AgentTaskStatus,
    ContextReference,
    ContextReferenceType,
    EvidenceReference,
)
from agent.specialists.registry import (
    INCIDENT_TRIAGE_SPECIALIST_NAME,
    enforce_task_capability,
    required_tools_for_task,
)
from agent.state import TriageReport, parse_json_object
from agent.tools.audit_log import write_agent_audit_event
from agent.tools.clickhouse_sql import rows_to_dicts
from pipelines.common.clickhouse import build_clickhouse_client, quote_sql_literal
from pipelines.common.logging import logger


# --- Defining Constants
INCIDENT_TRIAGE_TASK_TYPE   = "triage_alert"
DEFAULT_TRIAGE_TOKEN_BUDGET = 16_384
DEFAULT_TRIAGE_MODEL_CALL_BUDGET = 3
DEFAULT_TRIAGE_COST_BUDGET_USD   = 0.05
MAX_AUDIT_USAGE_EVENTS      = 100


# --- Defining Specialist Models
class IncidentTriageTaskInput(BaseModel):
    """
    Validate the bounded input accepted by the Incident Triage Agent.

    Attributes:
        alert_id: Optional ClickHouse alert UUID.
        alert_key: Optional system alert key or human-facing Alert Ref.
        confidence_threshold: Confidence target for the evidence loop.
        max_evidence_iterations: Maximum bounded extra-evidence iterations.
        manifest_s3_uri: Optional system-owned dbt manifest artifact.
        artifacts_bucket: Optional report artifact bucket override.
        artifacts_prefix: Report artifact prefix.
    """

    model_config = ConfigDict(extra="forbid")

    alert_id: str                     = Field(default="", max_length=36)
    alert_key: str                    = Field(default="", max_length=500)
    confidence_threshold: float       = Field(
        default=DEFAULT_CONFIDENCE_TARGET,
        ge=0.10,
        le=0.95,
    )
    max_evidence_iterations: int      = Field(
        default=DEFAULT_MAX_EVIDENCE_LOOP,
        ge=0,
        le=5,
    )
    manifest_s3_uri: str              = Field(default="", max_length=2_048)
    artifacts_bucket: str             = Field(default="", max_length=100)
    artifacts_prefix: str             = Field(default="agent-reports", max_length=200)

    @field_validator(
        "alert_id",
        "alert_key",
        "manifest_s3_uri",
        "artifacts_bucket",
        "artifacts_prefix",
    )
    @classmethod
    def normalize_text(cls, value: str) -> str:
        """
        Normalize single-line task input text.

        Args:
            value: Raw task input.

        Returns:
            Trimmed single-line value.

        Raises:
            ValueError: If the value contains a line break.
        """
        normalized = value.strip()

        if "\n" in normalized or "\r" in normalized:
            raise ValueError("Incident triage task values must be single-line strings.")

        return normalized

    @model_validator(mode="after")
    def validate_identifier_and_storage(self) -> "IncidentTriageTaskInput":
        """
        Require one alert identity and validate optional S3 settings.

        Returns:
            Current validated task input.

        Raises:
            ValueError: If alert identity or artifact settings are ambiguous.
        """
        if bool(self.alert_id) == bool(self.alert_key):
            raise ValueError("Provide exactly one of alert_id or alert_key for incident triage.")

        if self.manifest_s3_uri and not self.manifest_s3_uri.startswith("s3://"):
            raise ValueError("manifest_s3_uri must use the s3:// scheme.")

        if "/" in self.artifacts_bucket or "\\" in self.artifacts_bucket:
            raise ValueError("artifacts_bucket must be a bucket name, not a path.")

        if self.artifacts_prefix.startswith("/") or ".." in self.artifacts_prefix.split("/"):
            raise ValueError("artifacts_prefix must be a relative S3 prefix without traversal.")

        return self


class IncidentTriageAgentOutput(BaseModel):
    """
    Return a compact incident result while full reports remain in S3.

    Attributes:
        child_agent_run_id: Existing triage graph run identifier.
        report_id: Human-facing report identifier.
        alert_key: Stable system alert key.
        alert_display_id: Human-facing Alert Ref.
        severity: Alert severity.
        summary: Report executive summary.
        impact: Report impact assessment.
        confidence: Final deterministic confidence.
        top_hypothesis: Compact top-hypothesis context.
        evidence_count: Number of deterministic evidence items.
        complexity_tier: Deterministic low, moderate, or high incident complexity.
        complexity_score: Additive deterministic complexity score.
        complexity_reason_codes: Stable facts that contributed to complexity.
        investigation_errors: Bounded non-fatal evidence gaps retained by the report.
        recommended_actions: Bounded non-mutating recommendations.
        approval_gated_action_count: Number of actions awaiting human approval.
        requested_model_routes: Model routes requested by the child graph.
        executed_model_routes: Model routes that returned usable output.
        model_providers: Providers retained by completed route audits.
        model_names: Models retained by completed route audits.
        fallback_reasons: Sanitized provider fallback reasons.
        actual_model_route: Highest capability proven by successful execution.
        strong_review_requested: Whether confidence or complexity requested strong reasoning.
        strong_review_satisfied: Whether the strong route returned external output.
        markdown_report_s3_uri: Markdown report artifact.
        json_report_s3_uri: JSON report artifact.
    """

    model_config = ConfigDict(extra="forbid")

    child_agent_run_id: str
    report_id: str
    alert_key: str
    alert_display_id: str
    severity: str
    summary: str                            = Field(max_length=4_000)
    impact: str                             = Field(max_length=4_000)
    confidence: float                       = Field(ge=0.0, le=1.0)
    top_hypothesis: dict[str, Any]           = Field(default_factory=dict)
    evidence_count: int                     = Field(ge=0)
    complexity_tier: str                    = Field(
        default="low",
        pattern=r"^(low|moderate|high)$",
    )
    complexity_score: int                   = Field(default=0, ge=0, le=100)
    complexity_reason_codes: list[str]      = Field(default_factory=list, max_length=20)
    investigation_errors: list[str]         = Field(default_factory=list, max_length=20)
    recommended_actions: list[str]          = Field(default_factory=list, max_length=10)
    approval_gated_action_count: int        = Field(ge=0)
    requested_model_routes: list[str]       = Field(default_factory=list, max_length=20)
    executed_model_routes: list[str]        = Field(default_factory=list, max_length=20)
    model_providers: list[str]              = Field(default_factory=list, max_length=20)
    model_names: list[str]                  = Field(default_factory=list, max_length=20)
    fallback_reasons: list[str]             = Field(default_factory=list, max_length=20)
    actual_model_route: AgentModelRoute     = AgentModelRoute.NO_LLM_FALLBACK
    strong_review_requested: bool           = False
    strong_review_satisfied: bool           = False
    markdown_report_s3_uri: str
    json_report_s3_uri: str


class LlmUsageSummary(BaseModel):
    """Aggregate audited model usage and actual capability from one triage graph run."""

    model_config = ConfigDict(extra="forbid")

    event_count: int              = 0
    external_model_calls: int     = 0
    token_usage: int              = 0
    estimated_cost_usd: float     = 0.0
    requested_routes: list[str]   = Field(default_factory=list, max_length=20)
    executed_routes: list[str]    = Field(default_factory=list, max_length=20)
    attempted_routes: list[str]   = Field(default_factory=list, max_length=40)
    providers: list[str]          = Field(default_factory=list, max_length=20)
    models: list[str]             = Field(default_factory=list, max_length=20)
    fallback_reasons: list[str]   = Field(default_factory=list, max_length=20)
    actual_model_route: AgentModelRoute = AgentModelRoute.NO_LLM_FALLBACK
    strong_review_requested: bool = False
    strong_review_satisfied: bool = False


@dataclass(frozen=True)
class IncidentTriageRuntimeConfig:
    """
    Inject the existing triage runner and deterministic audit dependencies.

    Attributes:
        triage_runner: Existing agent.graph.run_triage callable.
        audit_client_factory: ClickHouse client factory for handoff and usage audit.
        audit_writer: Append-only audit event writer.
        triage_config: Existing triage runtime connection and artifact settings.
        clickhouse_host: Optional ClickHouse host override.
        clickhouse_port: Optional ClickHouse HTTP port override.
    """

    triage_runner: Callable[..., TriageReport] = run_triage
    audit_client_factory: Callable[..., Any]   = build_clickhouse_client
    audit_writer: Callable[..., UUID]          = write_agent_audit_event
    triage_config: TriageRuntimeConfig         = field(default_factory=TriageRuntimeConfig)
    clickhouse_host: str | None                = None
    clickhouse_port: int | None                = None


# --- Defining Task Helpers
def validate_incident_triage_task(task: AgentTaskEnvelope) -> IncidentTriageTaskInput:
    """
    Enforce capability policy and validate specialist-specific input.

    Args:
        task: Typed task handoff supplied by the supervisor.

    Returns:
        Validated IncidentTriageTaskInput.

    Raises:
        PermissionError: If the handoff violates capability policy.
        ValueError: If task input is invalid.
    """
    enforce_task_capability(task)

    if task.specialist_name != INCIDENT_TRIAGE_SPECIALIST_NAME:
        raise PermissionError("Incident triage received a task for another specialist.")

    if task.task_type != INCIDENT_TRIAGE_TASK_TYPE:
        raise PermissionError(f"Unsupported incident triage task: {task.task_type}")

    task_input = IncidentTriageTaskInput.model_validate(task.input_payload)

    logger.info(
        "Validated incident triage handoff | task_id=%s alert_reference=%s",
        task.task_id,
        task_input.alert_key or task_input.alert_id,
    )

    return task_input


def build_incident_triage_task(
    parent_run_id: UUID,
    alert_id: str = "",
    alert_key: str = "",
    confidence_threshold: float = DEFAULT_CONFIDENCE_TARGET,
    max_evidence_iterations: int = DEFAULT_MAX_EVIDENCE_LOOP,
    manifest_s3_uri: str = "",
    artifacts_bucket: str = "",
    artifacts_prefix: str = "agent-reports",
    requester: str = "control_plane",
) -> AgentTaskEnvelope:
    """
    Build one policy-compliant handoff to the existing triage graph.

    Args:
        parent_run_id: Parent supervisor correlation UUID.
        alert_id: Optional alert UUID.
        alert_key: Optional system alert key or Alert Ref.
        confidence_threshold: Confidence target for the evidence loop.
        max_evidence_iterations: Maximum extra-evidence iterations.
        manifest_s3_uri: Optional dbt manifest S3 artifact.
        artifacts_bucket: Optional report bucket override.
        artifacts_prefix: Report artifact prefix.
        requester: Interface or system requesting the handoff.

    Returns:
        Validated AgentTaskEnvelope with an exact tool allowlist.
    """
    task_input = IncidentTriageTaskInput(
        alert_id=alert_id,
        alert_key=alert_key,
        confidence_threshold=confidence_threshold,
        max_evidence_iterations=max_evidence_iterations,
        manifest_s3_uri=manifest_s3_uri,
        artifacts_bucket=artifacts_bucket,
        artifacts_prefix=artifacts_prefix,
    )
    alert_reference = task_input.alert_key or task_input.alert_id
    context_references = [
        ContextReference(
            reference_type=ContextReferenceType.ALERT,
            reference=alert_reference,
            description="Alert selected for bounded evidence-driven incident triage.",
        )
    ]

    if task_input.manifest_s3_uri:
        context_references.append(
            ContextReference(
                reference_type=ContextReferenceType.S3_ARTIFACT,
                reference=task_input.manifest_s3_uri,
                description="dbt manifest artifact used for lineage evidence.",
            )
        )

    task = AgentTaskEnvelope(
        parent_run_id=parent_run_id,
        specialist_name=INCIDENT_TRIAGE_SPECIALIST_NAME,
        task_type=INCIDENT_TRIAGE_TASK_TYPE,
        risk_tier=AgentRiskTier.MEDIUM,
        allowed_tools=required_tools_for_task(
            specialist_name=INCIDENT_TRIAGE_SPECIALIST_NAME,
            task_type=INCIDENT_TRIAGE_TASK_TYPE,
        ),
        context_references=context_references,
        model_route=AgentModelRoute.DEEPTHINK_LLM,
        model_call_budget=DEFAULT_TRIAGE_MODEL_CALL_BUDGET,
        token_budget=DEFAULT_TRIAGE_TOKEN_BUDGET,
        estimated_cost_budget_usd=DEFAULT_TRIAGE_COST_BUDGET_USD,
        timeout_seconds=300,
        requester=requester,
        alert_key=task_input.alert_key,
        input_payload=task_input.model_dump(mode="json"),
    )

    validate_incident_triage_task(task)

    logger.info(
        "Built incident triage handoff | task_id=%s parent_run_id=%s alert_reference=%s",
        task.task_id,
        task.parent_run_id,
        alert_reference,
    )

    return task


# --- Defining Usage And Output Helpers
def build_llm_usage_sql(child_agent_run_id: UUID | str) -> str:
    """
    Build a fixed, exact audit query for model usage from one child run.

    Args:
        child_agent_run_id: Existing triage graph run UUID.

    Returns:
        Read-only ClickHouse query with exact UUID predicate and hard LIMIT.
    """
    run_literal = quote_sql_literal(str(child_agent_run_id))

    return f"""
        SELECT
            output_json
        FROM dq.agent_audit_log
        WHERE agent_run_id = toUUID({run_literal})
          AND action = 'llm_route_completed'
        ORDER BY ts ASC
        LIMIT {MAX_AUDIT_USAGE_EVENTS}
    """


def append_unique_text(target: list[str], raw_value: Any, max_length: int = 500) -> None:
    """
    Append one normalized non-empty audit value without introducing duplicates.

    Args:
        target: Mutable ordered list receiving the normalized value.
        raw_value: JSON-like audit value.
        max_length: Maximum retained string length.

    Returns:
        None.
    """
    value = str(raw_value or "").strip()[:max_length]

    if value and value not in target:
        target.append(value)


def route_reasoning_tier(config: ModelRoutingConfig, route_name: str) -> str:
    """
    Return one configured reasoning tier without accepting unknown route names.

    Args:
        config: Validated model routing configuration.
        route_name: Audited provider route name.

    Returns:
        none, cheap, mid, or strong when configured; otherwise an empty string.
    """
    route = config.routes.get(route_name)

    return str(route.reasoning_tier) if route else ""


def load_llm_usage_summary(
    client: Any,
    child_agent_run_id: UUID | str,
    routing_config: ModelRoutingConfig | None = None,
) -> LlmUsageSummary:
    """
    Aggregate persisted model route usage for the existing triage graph.

    Args:
        client: clickhouse-connect compatible client.
        child_agent_run_id: Existing triage graph run UUID.
        routing_config: Optional validated route configuration for tests or overrides.

    Returns:
        LlmUsageSummary containing calls, routes, providers, fallbacks, tokens, cost,
        and the highest capability proven by successful execution.
    """
    result = client.query(build_llm_usage_sql(child_agent_run_id))
    rows   = rows_to_dicts(
        columns=list(result.column_names or []),
        rows=result.result_rows,
    )
    payloads = [parse_json_object(row.get("output_json")) for row in rows]
    config = routing_config or load_model_routing_config()
    external_payloads = [
        payload
        for payload in payloads
        if not bool(payload.get("used_heuristic", False))
        and str(payload.get("provider", "")).lower() != "heuristic"
    ]
    external_calls = len(external_payloads)
    token_usage = sum(
        int(payload.get("input_tokens", 0) or 0)
        + int(payload.get("output_tokens", 0) or 0)
        for payload in external_payloads
    )
    estimated_cost = sum(
        float(payload.get("estimated_cost_usd", 0.0) or 0.0)
        for payload in external_payloads
    )
    requested_routes: list[str] = []
    executed_routes: list[str]  = []
    attempted_routes: list[str] = []
    providers: list[str]        = []
    models: list[str]           = []
    fallback_reasons: list[str] = []

    for payload in payloads:
        append_unique_text(requested_routes, payload.get("requested_route"), max_length=120)
        append_unique_text(executed_routes, payload.get("executed_route"), max_length=120)
        append_unique_text(providers, payload.get("provider"), max_length=120)
        append_unique_text(models, payload.get("model"), max_length=200)
        append_unique_text(fallback_reasons, payload.get("fallback_reason"), max_length=500)

        raw_attempted_routes = payload.get("attempted_routes", [])

        if isinstance(raw_attempted_routes, list):
            for route_name in raw_attempted_routes:
                append_unique_text(attempted_routes, route_name, max_length=120)

    strong_review_requested = any(
        route_reasoning_tier(config, route_name) == "strong"
        for route_name in requested_routes
    )
    strong_review_satisfied = any(
        route_reasoning_tier(config, str(payload.get("executed_route", ""))) == "strong"
        for payload in external_payloads
    )

    if strong_review_satisfied:
        actual_model_route = AgentModelRoute.DEEPTHINK_LLM

    elif external_payloads:
        # Cheap and mid provider routes are normal bounded reasoning. They do not
        # satisfy a strong-review requirement even when the task ceiling is deepthink.
        actual_model_route = AgentModelRoute.QUICKTHINK_LLM

    else:
        actual_model_route = AgentModelRoute.NO_LLM_FALLBACK

    summary = LlmUsageSummary(
        event_count=len(payloads),
        external_model_calls=external_calls,
        token_usage=token_usage,
        estimated_cost_usd=estimated_cost,
        requested_routes=requested_routes,
        executed_routes=executed_routes,
        attempted_routes=attempted_routes,
        providers=providers,
        models=models,
        fallback_reasons=fallback_reasons,
        actual_model_route=actual_model_route,
        strong_review_requested=strong_review_requested,
        strong_review_satisfied=strong_review_satisfied,
    )

    logger.info(
        "Aggregated incident triage LLM usage | child_agent_run_id=%s events=%d external_calls=%d actual_route=%s strong_requested=%s strong_satisfied=%s tokens=%d cost=%.8f",
        child_agent_run_id,
        summary.event_count,
        summary.external_model_calls,
        summary.actual_model_route.value,
        summary.strong_review_requested,
        summary.strong_review_satisfied,
        summary.token_usage,
        summary.estimated_cost_usd,
    )

    return summary


def build_incident_triage_output(
    report: TriageReport,
    usage: LlmUsageSummary,
) -> IncidentTriageAgentOutput:
    """
    Convert a full S3-backed triage report into bounded supervisor output.

    Args:
        report: Existing triage graph report.
        usage: Audited route, provider, fallback, token, and capability summary.

    Returns:
        Compact IncidentTriageAgentOutput.
    """
    top_hypothesis: dict[str, Any] = {}
    complexity                     = report.complexity_assessment
    complexity_tier               = (
        str(getattr(complexity.tier, "value", complexity.tier))
        if complexity
        else "low"
    )

    if report.top_hypothesis:
        top_hypothesis = {
            "hypothesis_id": report.top_hypothesis.hypothesis_id,
            "category": report.top_hypothesis.root_cause_category,
            "title": report.top_hypothesis.title,
            "confidence": report.top_hypothesis.confidence,
            "recommended_action": report.top_hypothesis.recommended_action,
        }

    return IncidentTriageAgentOutput(
        child_agent_run_id=str(report.agent_run_id),
        report_id=report.report_id,
        alert_key=report.alert.alert_key,
        alert_display_id=report.alert.alert_display_id,
        severity=str(report.alert.severity),
        summary=report.summary,
        impact=report.impact,
        confidence=report.confidence,
        top_hypothesis=top_hypothesis,
        evidence_count=len(report.evidence),
        complexity_tier=complexity_tier,
        complexity_score=complexity.score if complexity else 0,
        complexity_reason_codes=(
            list(complexity.reason_codes)
            if complexity
            else []
        ),
        investigation_errors=report.investigation_errors,
        recommended_actions=report.recommended_actions[:10],
        approval_gated_action_count=len(report.approval_gated_actions),
        requested_model_routes=usage.requested_routes,
        executed_model_routes=usage.executed_routes,
        model_providers=usage.providers,
        model_names=usage.models,
        fallback_reasons=usage.fallback_reasons,
        actual_model_route=usage.actual_model_route,
        strong_review_requested=usage.strong_review_requested,
        strong_review_satisfied=usage.strong_review_satisfied,
        markdown_report_s3_uri=report.markdown_report_s3_uri,
        json_report_s3_uri=report.json_report_s3_uri,
    )


def build_incident_evidence_references(report: TriageReport) -> list[EvidenceReference]:
    """
    Convert report evidence into bounded cross-agent references.

    Args:
        report: Existing triage graph report.

    Returns:
        EvidenceReference entries without raw rows or SQL text.
    """
    references = [
        EvidenceReference(
            evidence_type=str(item.evidence_type),
            source_tool=item.tool_name,
            reference=item.evidence_id,
            summary=(item.summary or item.description or "Evidence collected by triage.")[:1_000],
        )
        for item in report.evidence[:98]
    ]

    if report.json_report_s3_uri:
        references.append(
            EvidenceReference(
                evidence_type="triage_report_json",
                source_tool="s3_artifacts",
                reference=report.json_report_s3_uri,
                summary=f"Structured triage report {report.report_id}.",
            )
        )

    if report.markdown_report_s3_uri:
        references.append(
            EvidenceReference(
                evidence_type="triage_report_markdown",
                source_tool="s3_artifacts",
                reference=report.markdown_report_s3_uri,
                summary=f"Operator-readable triage report {report.report_id}.",
            )
        )

    return references


def sanitize_incident_triage_error(exc: Exception) -> str:
    """
    Build a bounded, single-line specialist failure message.

    Args:
        exc: Policy, tool, or triage exception.

    Returns:
        Sanitized error type and message.
    """
    message = " ".join(str(exc).split())[:1_500]

    return f"{type(exc).__name__}: {message}"


def write_incident_handoff_audit(
    config: IncidentTriageRuntimeConfig,
    client: Any,
    task: AgentTaskEnvelope,
    action: str,
    status: str,
    duration_ms: int | None = None,
    output_payload: dict[str, Any] | None = None,
    error_message: str = "",
    report_s3_uri: str = "",
) -> None:
    """
    Persist one parent-correlated incident specialist lifecycle event.

    Args:
        config: Runtime containing the append-only audit writer.
        client: ClickHouse audit client.
        task: Source specialist task.
        action: Stable lifecycle action.
        status: running, success, partial, blocked, or failed.
        duration_ms: Optional elapsed duration.
        output_payload: Bounded completion metadata.
        error_message: Sanitized failure detail.
        report_s3_uri: Optional report artifact URI.

    Returns:
        None.
    """
    config.audit_writer(
        client=client,
        action=action,
        status=status,
        agent_run_id=task.parent_run_id,
        alert_key=task.alert_key,
        actor=task.requester,
        tool_name=INCIDENT_TRIAGE_SPECIALIST_NAME,
        duration_ms=duration_ms,
        input_payload={
            "task_id": str(task.task_id),
            "task_type": task.task_type,
            "risk_tier": task.risk_tier.value,
            "allowed_tools": list(task.allowed_tools),
            "requested_model_route": task.model_route.value,
            "model_call_budget": task.model_call_budget,
            "token_budget": task.token_budget,
            "estimated_cost_budget_usd": task.estimated_cost_budget_usd,
            "timeout_seconds": task.timeout_seconds,
            "requester": task.requester,
        },
        output_payload=output_payload or {},
        error_message=error_message,
        report_s3_uri=report_s3_uri,
    )


# --- Defining Specialist Runtime
def run_incident_triage_agent(
    task: AgentTaskEnvelope,
    config: IncidentTriageRuntimeConfig | None = None,
) -> AgentResultEnvelope:
    """
    Run the existing triage graph through a failure-isolated specialist boundary.

    Args:
        task: Typed supervisor-to-specialist handoff.
        config: Optional runtime dependency overrides.

    Returns:
        Terminal AgentResultEnvelope. Triage exceptions do not escape this boundary.
    """
    runtime = config or IncidentTriageRuntimeConfig()
    started = time.monotonic()
    client: Any | None = None

    try:
        client = runtime.audit_client_factory(
            host=runtime.clickhouse_host,
            port=runtime.clickhouse_port,
        )

        try:
            task_input = validate_incident_triage_task(task)

        except (LookupError, PermissionError, ValueError) as exc:
            duration_ms   = int((time.monotonic() - started) * 1_000)
            error_message = sanitize_incident_triage_error(exc)
            blocked = AgentResultEnvelope(
                task_id=task.task_id,
                parent_run_id=task.parent_run_id,
                specialist_name=task.specialist_name,
                task_type=task.task_type,
                status=AgentTaskStatus.BLOCKED,
                model_route=AgentModelRoute.NO_LLM_FALLBACK,
                duration_ms=duration_ms,
                errors=[error_message],
                recommended_next_step="Review the rejected handoff policy before retrying triage.",
            )
            write_incident_handoff_audit(
                config=runtime,
                client=client,
                task=task,
                action="specialist_handoff_rejected",
                status="blocked",
                duration_ms=duration_ms,
                output_payload={"result_status": blocked.status.value},
                error_message=error_message,
            )

            return blocked

        write_incident_handoff_audit(
            config=runtime,
            client=client,
            task=task,
            action="specialist_handoff_started",
            status="running",
        )

        triage_config = TriageRuntimeConfig(
            manifest_path=runtime.triage_config.manifest_path,
            manifest_s3_uri=(
                task_input.manifest_s3_uri
                or runtime.triage_config.manifest_s3_uri
            ),
            s3_endpoint_url=runtime.triage_config.s3_endpoint_url,
            artifacts_bucket=(
                task_input.artifacts_bucket
                or runtime.triage_config.artifacts_bucket
            ),
            artifacts_prefix=(
                task_input.artifacts_prefix
                or runtime.triage_config.artifacts_prefix
            ),
            clickhouse_host=(
                runtime.clickhouse_host
                or runtime.triage_config.clickhouse_host
            ),
            clickhouse_port=(
                runtime.clickhouse_port
                or runtime.triage_config.clickhouse_port
            ),
        )
        report = runtime.triage_runner(
            alert_id=task_input.alert_id or None,
            alert_key=task_input.alert_key or None,
            confidence_threshold=task_input.confidence_threshold,
            max_evidence_iterations=task_input.max_evidence_iterations,
            config=triage_config,
        )
        usage       = load_llm_usage_summary(client=client, child_agent_run_id=report.agent_run_id)
        duration_ms = int((time.monotonic() - started) * 1_000)
        output      = build_incident_triage_output(report=report, usage=usage)
        result_status = (
            AgentTaskStatus.PARTIAL
            if output.investigation_errors
            else AgentTaskStatus.SUCCESS
        )
        requires_human_approval = bool(
            report.approval_gated_actions
            or output.investigation_errors
        )
        final_result = AgentResultEnvelope(
            task_id=task.task_id,
            parent_run_id=task.parent_run_id,
            specialist_name=task.specialist_name,
            task_type=task.task_type,
            status=result_status,
            evidence_references=build_incident_evidence_references(report),
            structured_output=output.model_dump(mode="json"),
            confidence=report.confidence,
            model_route=usage.actual_model_route,
            model_call_count=usage.external_model_calls,
            token_usage=usage.token_usage,
            estimated_cost_usd=usage.estimated_cost_usd,
            duration_ms=duration_ms,
            errors=output.investigation_errors,
            recommended_next_step=(
                "Resolve the retained evidence gaps before approving any proposed remediation."
                if output.investigation_errors
                else (
                    "Review the report evidence and explicitly approve any proposed remediation."
                    if report.approval_gated_actions
                    else "Review the persisted report and monitor the alert lifecycle."
                )
            ),
            requires_human_approval=requires_human_approval,
        )
        write_incident_handoff_audit(
            config=runtime,
            client=client,
            task=task,
            action="specialist_handoff_completed",
            status=final_result.status.value,
            duration_ms=duration_ms,
            output_payload={
                "result_status": final_result.status.value,
                "child_agent_run_id": str(report.agent_run_id),
                "report_id": report.report_id,
                "alert_display_id": report.alert.alert_display_id,
                "confidence": final_result.confidence,
                "evidence_reference_count": len(final_result.evidence_references),
                "complexity_tier": output.complexity_tier,
                "complexity_score": output.complexity_score,
                "complexity_reason_codes": output.complexity_reason_codes,
                "investigation_errors": output.investigation_errors,
                "investigation_error_count": len(output.investigation_errors),
                "model_route": final_result.model_route.value,
                "requested_model_routes": usage.requested_routes,
                "executed_model_routes": usage.executed_routes,
                "attempted_model_routes": usage.attempted_routes,
                "model_providers": usage.providers,
                "model_names": usage.models,
                "fallback_reasons": usage.fallback_reasons,
                "strong_review_requested": usage.strong_review_requested,
                "strong_review_satisfied": usage.strong_review_satisfied,
                "llm_audit_event_count": usage.event_count,
                "external_model_calls": usage.external_model_calls,
                "model_call_count": final_result.model_call_count,
                "token_usage": final_result.token_usage,
                "estimated_cost_usd": final_result.estimated_cost_usd,
                "requires_human_approval": final_result.requires_human_approval,
            },
            error_message="; ".join(output.investigation_errors)[:2_000],
            report_s3_uri=report.markdown_report_s3_uri,
        )

        logger.info(
            "Incident Triage Agent completed | task_id=%s child_agent_run_id=%s report_id=%s status=%s confidence=%.2f errors=%d",
            task.task_id,
            report.agent_run_id,
            report.report_id,
            final_result.status.value,
            report.confidence,
            len(final_result.errors),
        )

        return final_result

    except Exception as exc:
        duration_ms   = int((time.monotonic() - started) * 1_000)
        error_message = sanitize_incident_triage_error(exc)
        failed = AgentResultEnvelope(
            task_id=task.task_id,
            parent_run_id=task.parent_run_id,
            specialist_name=task.specialist_name,
            task_type=task.task_type,
            status=AgentTaskStatus.FAILED,
            model_route=AgentModelRoute.NO_LLM_FALLBACK,
            duration_ms=duration_ms,
            errors=[error_message],
            recommended_next_step=(
                "Inspect the child triage and specialist audit trail before retrying."
            ),
        )

        if client is not None:
            try:
                write_incident_handoff_audit(
                    config=runtime,
                    client=client,
                    task=task,
                    action="specialist_handoff_failed",
                    status="failed",
                    duration_ms=duration_ms,
                    output_payload={"result_status": failed.status.value},
                    error_message=error_message,
                )

            except Exception:
                logger.exception(
                    "Failed to persist Incident Triage Agent failure audit | task_id=%s",
                    task.task_id,
                )

        logger.exception(
            "Incident Triage Agent failed without propagating | task_id=%s parent_run_id=%s",
            task.task_id,
            task.parent_run_id,
        )

        return failed
