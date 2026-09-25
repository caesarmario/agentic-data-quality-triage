####
## Bounded Evidence Worker Adapter for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Collect purpose-limited deterministic evidence before one strict interpretation call."""

# --- Importing Libraries
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any, Callable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent.llm.config import external_llm_runtime_allowed, resolve_external_llm_enabled
from agent.specialists.contracts import (
    AgentModelRoute,
    AgentResultEnvelope,
    AgentRiskTier,
    AgentTaskEnvelope,
    AgentTaskStatus,
    EvidenceReference,
)
from agent.specialists.evidence_interpretation import (
    DeterministicEvidence,
    DqHistoryInterpretationInput,
    MetadataLineageImpactInterpretationInput,
    PipelineRunInterpretationInput,
    interpret_dq_history,
    interpret_metadata_lineage_impact,
    interpret_pipeline_run,
)
from agent.specialists.registry import (
    EVIDENCE_INTERPRETATION_TASKS,
    enforce_task_capability,
    required_tools_for_task,
)
from agent.supervisor.budgets import active_supervisor_llm_budget
from agent.tools.alerts import load_alert
from agent.tools.audit_log import build_llm_route_audit_payload, write_agent_audit_event
from agent.tools.dbt_lineage import fetch_dbt_blast_radius
from agent.tools.dq_history import fetch_dq_history
from agent.tools.metadata_catalog import get_metadata_asset
from agent.tools.pipeline_runs import fetch_pipeline_runs
from pipelines.common.alert_identity import is_alert_ref
from pipelines.common.clickhouse import build_clickhouse_client
from pipelines.common.logging import logger


# --- Defining Worker Policy
EVIDENCE_WORKER_RISK_TIER = AgentRiskTier.LOW
EVIDENCE_WORKER_MODEL_ROUTE = AgentModelRoute.QUICKTHINK_LLM
EVIDENCE_WORKER_MODEL_CALL_BUDGET = 1
EVIDENCE_WORKER_TOKEN_BUDGET = 8_192
EVIDENCE_WORKER_COST_BUDGET_USD = 0.016
EVIDENCE_WORKER_TIMEOUT_SECONDS = 60
EVIDENCE_LOOKBACK_DAYS = 30
EVIDENCE_ROW_LIMIT = 20


# --- Defining Typed Inputs And Runtime Dependencies
class EvidenceWorkerTaskInput(BaseModel):
    """Accept only an alert key and an optional read-only manifest reference."""

    model_config = ConfigDict(extra="forbid")

    alert_key: str = Field(min_length=1, max_length=500)
    manifest_s3_uri: str = Field(default="", max_length=2_048)

    @field_validator("alert_key", "manifest_s3_uri")
    @classmethod
    def validate_single_line_text(cls, value: str) -> str:
        """Normalize one bounded input field without accepting multiline payloads."""
        normalized = value.strip()

        if "\n" in normalized or "\r" in normalized:
            raise ValueError("Evidence worker input values must be single-line strings.")

        return normalized

    @model_validator(mode="after")
    def validate_manifest_reference(self) -> "EvidenceWorkerTaskInput":
        """Allow only a read-only S3 URI when a manifest is supplied."""
        if self.manifest_s3_uri and not self.manifest_s3_uri.startswith("s3://"):
            raise ValueError("manifest_s3_uri must use the s3:// scheme.")

        return self


@dataclass(frozen=True)
class EvidenceWorkerRuntimeConfig:
    """Inject read-only tools, interpretation functions, and append-only audit dependencies."""

    alert_loader: Callable[..., Any] = load_alert
    dq_history_fetcher: Callable[..., Any] = fetch_dq_history
    pipeline_runs_fetcher: Callable[..., Any] = fetch_pipeline_runs
    metadata_getter: Callable[..., Any] = get_metadata_asset
    blast_radius_fetcher: Callable[..., Any] = fetch_dbt_blast_radius
    dq_history_interpreter: Callable[..., Any] = interpret_dq_history
    pipeline_run_interpreter: Callable[..., Any] = interpret_pipeline_run
    metadata_lineage_interpreter: Callable[..., Any] = interpret_metadata_lineage_impact
    external_llm_allowed: Callable[[], bool] = external_llm_runtime_allowed
    external_llm_enabled: Callable[[], bool] = resolve_external_llm_enabled
    budget_getter: Callable[[], Any | None] = active_supervisor_llm_budget
    audit_client_factory: Callable[..., Any] = build_clickhouse_client
    audit_writer: Callable[..., UUID] = write_agent_audit_event
    clickhouse_host: str | None = None
    clickhouse_port: int | None = None


# --- Defining Task Validation
def _require_nonzero_uuid(value: UUID, field_name: str) -> None:
    """Reject the nil UUID because it cannot correlate a bounded handoff safely."""
    if value.int == 0:
        raise ValueError(f"Evidence worker {field_name} must not be the nil UUID.")


def validate_evidence_worker_task(task: AgentTaskEnvelope) -> EvidenceWorkerTaskInput:
    """Enforce exact worker capability, identity, input, and execution ceilings."""
    _require_nonzero_uuid(task.task_id, "task_id")
    _require_nonzero_uuid(task.parent_run_id, "parent_run_id")

    expected_specialist = EVIDENCE_INTERPRETATION_TASKS.get(task.task_type)

    if expected_specialist is None:
        raise PermissionError(f"Unsupported evidence worker task: {task.task_type}")

    if task.specialist_name != expected_specialist:
        raise PermissionError("Evidence worker task type is assigned to another specialist.")

    registry_tools = required_tools_for_task(task.specialist_name, task.task_type)

    if task.allowed_tools != registry_tools:
        raise PermissionError("Evidence worker requires the exact task tool allowlist.")

    if task.context_references:
        raise PermissionError("Evidence worker does not accept shared context references.")

    if task.risk_tier != EVIDENCE_WORKER_RISK_TIER:
        raise PermissionError("Evidence worker requires low risk tier.")

    if task.model_route != EVIDENCE_WORKER_MODEL_ROUTE:
        raise PermissionError("Evidence worker requires the quickthinkllm route.")

    if (
        task.model_call_budget != EVIDENCE_WORKER_MODEL_CALL_BUDGET
        or task.token_budget != EVIDENCE_WORKER_TOKEN_BUDGET
        or task.estimated_cost_budget_usd != EVIDENCE_WORKER_COST_BUDGET_USD
        or task.timeout_seconds != EVIDENCE_WORKER_TIMEOUT_SECONDS
    ):
        raise PermissionError("Evidence worker execution limits do not match policy.")

    enforce_task_capability(task)
    task_input = EvidenceWorkerTaskInput.model_validate(task.input_payload)

    if task_input.alert_key != task.alert_key:
        raise ValueError("Evidence worker input alert_key must match envelope alert_key.")

    logger.info(
        "Validated evidence worker handoff | task_id=%s parent_run_id=%s task_type=%s",
        task.task_id,
        task.parent_run_id,
        task.task_type,
    )

    return task_input


def build_evidence_worker_task(
    parent_run_id: UUID,
    task_type: str,
    alert_key: str,
    manifest_s3_uri: str = "",
    requester: str = "control_plane",
) -> AgentTaskEnvelope:
    """Build one exact-capability worker handoff for a single evidence interpretation."""
    normalized_task_type = task_type.strip().lower()
    specialist_name = EVIDENCE_INTERPRETATION_TASKS.get(normalized_task_type)

    if specialist_name is None:
        raise ValueError(f"Unsupported evidence worker task: {task_type}")

    task_input = EvidenceWorkerTaskInput(
        alert_key=alert_key,
        manifest_s3_uri=manifest_s3_uri,
    )
    task = AgentTaskEnvelope(
        parent_run_id=parent_run_id,
        specialist_name=specialist_name,
        task_type=normalized_task_type,
        risk_tier=EVIDENCE_WORKER_RISK_TIER,
        allowed_tools=required_tools_for_task(specialist_name, normalized_task_type),
        context_references=[],
        model_route=EVIDENCE_WORKER_MODEL_ROUTE,
        model_call_budget=EVIDENCE_WORKER_MODEL_CALL_BUDGET,
        token_budget=EVIDENCE_WORKER_TOKEN_BUDGET,
        estimated_cost_budget_usd=EVIDENCE_WORKER_COST_BUDGET_USD,
        timeout_seconds=EVIDENCE_WORKER_TIMEOUT_SECONDS,
        requester=requester,
        alert_key=task_input.alert_key,
        input_payload=task_input.model_dump(mode="json"),
    )
    validate_evidence_worker_task(task)

    logger.info(
        "Built evidence worker handoff | task_id=%s parent_run_id=%s task_type=%s",
        task.task_id,
        task.parent_run_id,
        task.task_type,
    )

    return task


# --- Defining Sanitized Deterministic Evidence
def _safe_text(value: Any, maximum: int = 255) -> str:
    """Normalize one public fact value into a bounded single-line string."""
    normalized = " ".join(str(value or "").split())

    return normalized[:maximum]


def _safe_scalar(value: Any) -> str | int | float | bool | None:
    """Convert selected scalar facts without forwarding nested source payloads."""
    if value is None or isinstance(value, (bool, int, float)):
        return value

    if isinstance(value, (date, datetime)):
        return value.isoformat()

    return _safe_text(value)


def _whitelist_rows(
    rows: Any,
    fields: tuple[str, ...],
) -> list[dict[str, str | int | float | bool | None]]:
    """Retain only named public fields from a bounded list of deterministic rows."""
    if not isinstance(rows, list):
        return []

    sanitized_rows: list[dict[str, str | int | float | bool | None]] = []

    for row in rows[:EVIDENCE_ROW_LIMIT]:
        if not isinstance(row, dict):
            continue

        sanitized = {
            field: _safe_scalar(row[field])
            for field in fields
            if field in row
        }

        if sanitized:
            sanitized_rows.append(sanitized)

    return sanitized_rows


def _status_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Derive status counts from the sanitized rows instead of trusting metadata extras."""
    counts: dict[str, int] = {}

    for row in rows:
        status = _safe_text(row.get("status", "unknown"), maximum=80).lower() or "unknown"
        counts[status] = counts.get(status, 0) + 1

    return counts


def _require_alert_scope(alert: Any, task: AgentTaskEnvelope) -> tuple[str, str, date]:
    """Read the authoritative alert identity and reject a mismatched or incomplete alert."""
    alert_key = _safe_text(getattr(alert, "alert_key", ""), maximum=500)
    table_name = _safe_text(getattr(alert, "table_name", ""), maximum=255)
    alert_dt = getattr(alert, "dt", None)

    display_id = _safe_text(getattr(alert, "alert_display_id", ""), maximum=100)
    requested_key = task.alert_key.strip()
    matches_display_id = (
        is_alert_ref(requested_key)
        and requested_key.upper() == display_id.upper()
    )
    if not alert_key or (alert_key != requested_key and not matches_display_id):
        raise ValueError("Authoritative alert key does not match the evidence worker envelope.")

    if not table_name or not isinstance(alert_dt, date):
        raise ValueError("Authoritative alert must provide a table_name and business date.")

    return alert_key, table_name, alert_dt


def _build_dq_history_evidence(
    alert: Any,
    runtime: EvidenceWorkerRuntimeConfig,
    task: AgentTaskEnvelope,
) -> tuple[list[DeterministicEvidence], list[EvidenceReference], DqHistoryInterpretationInput]:
    """Fetch and sanitize bounded DQ history for the authoritative alert scope."""
    _, table_name, alert_dt = _require_alert_scope(alert, task)
    check_name = _safe_text(getattr(alert, "metric", ""), maximum=255)

    if not check_name:
        raise ValueError("Authoritative alert must provide a metric for DQ history.")

    result = runtime.dq_history_fetcher(
        table_name=table_name,
        dt=alert_dt,
        check_name=check_name,
        lookback_days=EVIDENCE_LOOKBACK_DAYS,
        limit=EVIDENCE_ROW_LIMIT,
        agent_run_id=task.parent_run_id,
        alert_key=task.alert_key,
        clickhouse_host=runtime.clickhouse_host,
        clickhouse_port=runtime.clickhouse_port,
    )
    raw_rows = result.rows if hasattr(result, "rows") else result.get("rows", [])
    rows = _whitelist_rows(
        raw_rows,
        ("dt", "check_name", "status", "observed_value", "expected_value", "threshold_value"),
    )
    evidence = DeterministicEvidence(
        evidence_id=f"dq_history:{task.task_id}",
        evidence_type="dq_history",
        source_tool="dq_history",
        summary=(
            f"Collected {len(rows)} bounded DQ history rows for {table_name}.{check_name} "
            f"over the preceding {EVIDENCE_LOOKBACK_DAYS} days."
        ),
        facts={"row_count": len(rows), "status_counts": _status_counts(rows), "rows": rows},
    )
    reference = EvidenceReference(
        evidence_type="dq_history",
        source_tool="dq_history",
        reference=f"alert:{task.alert_key}",
        summary=evidence.summary,
    )
    request = DqHistoryInterpretationInput(
        asset_name=table_name,
        check_name=check_name,
        evaluation_window=f"{EVIDENCE_LOOKBACK_DAYS}-day window ending {alert_dt.isoformat()}",
        evidence=[evidence],
    )

    return [evidence], [reference], request


def _build_pipeline_run_evidence(
    alert: Any,
    runtime: EvidenceWorkerRuntimeConfig,
    task: AgentTaskEnvelope,
) -> tuple[list[DeterministicEvidence], list[EvidenceReference], PipelineRunInterpretationInput]:
    """Fetch and sanitize bounded pipeline execution evidence for the alert date."""
    _, table_name, alert_dt = _require_alert_scope(alert, task)
    result = runtime.pipeline_runs_fetcher(
        dt=alert_dt,
        lookback_days=EVIDENCE_LOOKBACK_DAYS,
        job_name=None,
        limit=EVIDENCE_ROW_LIMIT,
        agent_run_id=task.parent_run_id,
        alert_key=task.alert_key,
        clickhouse_host=runtime.clickhouse_host,
        clickhouse_port=runtime.clickhouse_port,
    )
    raw_rows = result.rows if hasattr(result, "rows") else result.get("rows", [])
    rows = _whitelist_rows(
        raw_rows,
        (
            "run_id",
            "job_name",
            "dag_id",
            "task_id",
            "logical_date",
            "partition_dt",
            "status",
            "started_at",
            "ended_at",
            "duration_ms",
            "rows_read",
            "rows_written",
            "target_table",
        ),
    )
    evidence = DeterministicEvidence(
        evidence_id=f"pipeline_run:{task.task_id}",
        evidence_type="pipeline_run",
        source_tool="pipeline_runs",
        summary=(
            f"Collected {len(rows)} bounded recent platform pipeline-run rows for the "
            f"alert window ending {alert_dt.isoformat()}."
        ),
        facts={"row_count": len(rows), "status_counts": _status_counts(rows), "runs": rows},
    )
    reference = EvidenceReference(
        evidence_type="pipeline_run",
        source_tool="pipeline_runs",
        reference=f"alert:{task.alert_key}",
        summary=evidence.summary,
    )
    request = PipelineRunInterpretationInput(
        pipeline_name="recent platform pipeline runs",
        evaluation_window=f"{EVIDENCE_LOOKBACK_DAYS}-day window ending {alert_dt.isoformat()}",
        evidence=[evidence],
    )

    return [evidence], [reference], request


def _build_metadata_lineage_evidence(
    alert: Any,
    runtime: EvidenceWorkerRuntimeConfig,
    task: AgentTaskEnvelope,
    manifest_s3_uri: str,
) -> tuple[
    list[DeterministicEvidence],
    list[EvidenceReference],
    MetadataLineageImpactInterpretationInput,
]:
    """Fetch and sanitize authoritative metadata plus bounded dbt impact evidence."""
    _, table_name, _ = _require_alert_scope(alert, task)
    metadata = runtime.metadata_getter(
        qualified_name=table_name,
        agent_run_id=task.parent_run_id,
        alert_key=task.alert_key,
        clickhouse_host=runtime.clickhouse_host,
        clickhouse_port=runtime.clickhouse_port,
    )
    if not isinstance(metadata, dict):
        raise ValueError("Metadata catalog returned an unsupported result.")

    metadata_facts = {
        field: _safe_scalar(metadata[field])
        for field in (
            "qualified_name",
            "database_name",
            "table_name",
            "dataset",
            "domain",
            "data_layer",
            "technical_owner",
            "business_owner",
            "grain",
            "refresh_frequency",
            "criticality",
            "sensitivity",
            "contains_pii",
            "certification_status",
            "lifecycle_status",
        )
        if field in metadata
    }
    blast_radius = runtime.blast_radius_fetcher(
        table_name=table_name,
        agent_run_id=task.parent_run_id,
        alert_key=task.alert_key,
        manifest_s3_uri=manifest_s3_uri or None,
        clickhouse_host=runtime.clickhouse_host,
        clickhouse_port=runtime.clickhouse_port,
    )
    if not isinstance(blast_radius, dict):
        raise ValueError("dbt blast-radius tool returned an unsupported result.")

    impact_facts = {
        field: _safe_scalar(blast_radius[field])
        for field in (
            "matched",
            "max_depth",
            "max_nodes",
            "max_depth_reached",
            "truncated",
            "total_impacted_nodes",
            "impacted_asset_count",
            "impacted_test_count",
            "unresolved_node_count",
        )
        if field in blast_radius
    }
    if isinstance(blast_radius.get("resource_type_counts"), dict):
        impact_facts["resource_type_counts"] = {
            _safe_text(key, maximum=80): int(value)
            for key, value in blast_radius["resource_type_counts"].items()
            if isinstance(value, int)
        }

    metadata_evidence = DeterministicEvidence(
        evidence_id=f"metadata_asset:{task.task_id}",
        evidence_type="metadata_asset",
        source_tool="metadata_catalog",
        summary=f"Collected allowlisted metadata fields for {table_name}.",
        facts=metadata_facts,
    )
    impact_evidence = DeterministicEvidence(
        evidence_id=f"blast_radius:{task.task_id}",
        evidence_type="blast_radius",
        source_tool="dbt_blast_radius",
        summary=f"Collected bounded downstream dbt impact counts for {table_name}.",
        facts=impact_facts,
    )
    references = [
        EvidenceReference(
            evidence_type="metadata_asset",
            source_tool="metadata_catalog",
            reference=table_name,
            summary=metadata_evidence.summary,
        ),
        EvidenceReference(
            evidence_type="blast_radius",
            source_tool="dbt_blast_radius",
            reference=table_name,
            summary=impact_evidence.summary,
        ),
    ]
    request = MetadataLineageImpactInterpretationInput(
        asset_name=table_name,
        change_summary=f"Data-quality alert for {table_name}.",
        evidence=[metadata_evidence, impact_evidence],
    )

    return [metadata_evidence, impact_evidence], references, request


# --- Defining Result And Audit Helpers
def _sanitize_error(exc: Exception, phase: str) -> str:
    """Avoid returning database errors, SQL, credentials, or provider payloads to operators."""
    return f"{type(exc).__name__}: evidence worker {phase} failed."


def _ledger_usage(ledger: Any | None, duration_ms: int) -> tuple[int, int, float]:
    """Read existing supervisor-ledger usage without creating an alternate accounting path."""
    if ledger is None or not hasattr(ledger, "snapshot"):
        return 0, 0, 0.0

    snapshot = ledger.snapshot(latency_ms=duration_ms)
    return (
        int(getattr(snapshot, "model_calls", 0)),
        int(getattr(snapshot, "tokens", 0)),
        float(getattr(snapshot, "estimated_cost_usd", 0.0)),
    )


def _execution_payload(
    *,
    provider: str = "",
    model: str = "",
    input_tokens: int = 0,
    output_tokens: int = 0,
    estimated_cost_usd: float = 0.0,
    model_calls: int = 0,
    external_interpretation: bool,
) -> dict[str, Any]:
    """Expose normalized route accounting as output metadata rather than model evidence."""
    return {
        "mode": "external_interpretation" if external_interpretation else "deterministic_only",
        "llm_provider": _safe_text(provider, maximum=80).lower(),
        "llm_model": _safe_text(model, maximum=255),
        "input_tokens": max(0, int(input_tokens)),
        "output_tokens": max(0, int(output_tokens)),
        "tokens": max(0, int(input_tokens)) + max(0, int(output_tokens)),
        "estimated_cost_usd": max(0.0, float(estimated_cost_usd)),
        "model_calls": max(0, int(model_calls)),
    }


def _build_result(
    task: AgentTaskEnvelope,
    *,
    status: AgentTaskStatus,
    evidence_references: list[EvidenceReference],
    structured_output: dict[str, Any],
    duration_ms: int,
    errors: list[str] | None = None,
    model_route: AgentModelRoute = AgentModelRoute.NO_LLM_FALLBACK,
    model_call_count: int = 0,
    token_usage: int = 0,
    estimated_cost_usd: float = 0.0,
) -> AgentResultEnvelope:
    """Build a terminal result that preserves source references but not raw evidence."""
    return AgentResultEnvelope(
        task_id=task.task_id,
        parent_run_id=task.parent_run_id,
        specialist_name=task.specialist_name,
        task_type=task.task_type,
        status=status,
        evidence_references=evidence_references,
        structured_output=structured_output,
        confidence=0.0,
        model_route=model_route,
        model_call_count=model_call_count,
        token_usage=token_usage,
        estimated_cost_usd=estimated_cost_usd,
        duration_ms=duration_ms,
        errors=errors or [],
        recommended_next_step="Review deterministic evidence references before any remediation.",
        requires_human_approval=False,
    )


def _write_handoff_audit(
    runtime: EvidenceWorkerRuntimeConfig,
    client: Any,
    task: AgentTaskEnvelope,
    action: str,
    status: str,
    *,
    duration_ms: int | None = None,
    output_payload: dict[str, Any] | None = None,
    error_message: str = "",
) -> None:
    """Persist a bounded, parent-correlated worker lifecycle event."""
    runtime.audit_writer(
        client=client,
        action=action,
        status=status,
        agent_run_id=task.parent_run_id,
        alert_key=task.alert_key,
        actor=task.requester,
        tool_name="evidence_worker",
        duration_ms=duration_ms,
        input_payload={
            "task_id": str(task.task_id),
            "parent_run_id": str(task.parent_run_id),
            "specialist_name": task.specialist_name,
            "task_type": task.task_type,
            "allowed_tools": list(task.allowed_tools),
            "timeout_seconds": task.timeout_seconds,
        },
        output_payload=output_payload or {},
        error_message=error_message,
    )


def _write_llm_completion_audit(
    runtime: EvidenceWorkerRuntimeConfig,
    client: Any,
    task: AgentTaskEnvelope,
    execution: Any,
) -> None:
    """Emit one sanitized route-completion audit record after a strict provider success."""
    response = SimpleNamespace(
        route_name=str(execution.executed_route),
        provider=str(execution.provider),
        model=str(execution.model),
        input_tokens=int(execution.input_tokens),
        output_tokens=int(execution.output_tokens),
        estimated_cost_usd=float(execution.estimated_cost_usd),
        used_heuristic=False,
        fallback_reason="",
        duration_ms=int(execution.duration_ms),
        metadata={
            "requested_route": "cheap_summary",
            "executed_route": str(execution.executed_route),
            "attempted_routes": list(execution.attempted_routes),
            "structured_output_requested": True,
            "structured_output_mode": "required",
            "structured_output_status": "validated",
        },
    )
    payload = build_llm_route_audit_payload(response)
    runtime.audit_writer(
        client=client,
        action="llm_route_completed",
        status="success",
        agent_run_id=task.parent_run_id,
        alert_key=task.alert_key,
        actor=task.requester,
        tool_name="llm_router",
        duration_ms=int(execution.duration_ms),
        input_payload={
            "task_id": str(task.task_id),
            "requested_route": payload["requested_route"],
        },
        output_payload=payload,
    )


# --- Running One Bounded Worker
def run_evidence_worker(
    task: AgentTaskEnvelope,
    config: EvidenceWorkerRuntimeConfig | None = None,
) -> AgentResultEnvelope:
    """Run deterministic collection and, only when allowed, exactly one strict interpretation."""
    runtime = config or EvidenceWorkerRuntimeConfig()
    started = time.monotonic()
    audit_client: Any | None = None
    references: list[EvidenceReference] = []
    ledger: Any | None = None

    try:
        audit_client = runtime.audit_client_factory(
            host=runtime.clickhouse_host,
            port=runtime.clickhouse_port,
        )
        try:
            task_input = validate_evidence_worker_task(task)
            ledger = runtime.budget_getter()

            if ledger is None:
                raise PermissionError("Evidence worker requires an active supervisor LLM budget scope.")

        except (LookupError, PermissionError, ValueError) as exc:
            duration_ms = int((time.monotonic() - started) * 1_000)
            error_message = _sanitize_error(exc, "task validation")
            result = _build_result(
                task,
                status=AgentTaskStatus.BLOCKED,
                evidence_references=[],
                structured_output={"execution": _execution_payload(external_interpretation=False)},
                duration_ms=duration_ms,
                errors=[error_message],
            )
            _write_handoff_audit(
                runtime,
                audit_client,
                task,
                "evidence_worker_rejected",
                "blocked",
                duration_ms=duration_ms,
                output_payload={"result_status": result.status.value},
                error_message=error_message,
            )
            return result

        _write_handoff_audit(runtime, audit_client, task, "evidence_worker_started", "running")
        alert = runtime.alert_loader(
            alert_key=task.alert_key,
            agent_run_id=task.parent_run_id,
            clickhouse_host=runtime.clickhouse_host,
            clickhouse_port=runtime.clickhouse_port,
        )

        if task.task_type == "interpret_dq_history":
            evidence, references, request = _build_dq_history_evidence(alert, runtime, task)
            interpreter = runtime.dq_history_interpreter
        elif task.task_type == "interpret_pipeline_run":
            evidence, references, request = _build_pipeline_run_evidence(alert, runtime, task)
            interpreter = runtime.pipeline_run_interpreter
        else:
            evidence, references, request = _build_metadata_lineage_evidence(
                alert,
                runtime,
                task,
                task_input.manifest_s3_uri,
            )
            interpreter = runtime.metadata_lineage_interpreter

        deterministic_output = {
            "authoritative_evidence_ids": [item.evidence_id for item in evidence],
            "authoritative_source_tools": [item.source_tool for item in evidence],
        }

        if not runtime.external_llm_allowed() or not runtime.external_llm_enabled():
            duration_ms = int((time.monotonic() - started) * 1_000)
            result = _build_result(
                task,
                status=AgentTaskStatus.SUCCESS,
                evidence_references=references,
                structured_output={
                    **deterministic_output,
                    "interpretation_status": "not_run_external_llm_disabled",
                    "execution": _execution_payload(external_interpretation=False),
                },
                duration_ms=duration_ms,
            )
            _write_handoff_audit(
                runtime,
                audit_client,
                task,
                "evidence_worker_completed",
                "success",
                duration_ms=duration_ms,
                output_payload={
                    "result_status": result.status.value,
                    "evidence_reference_count": len(references),
                    "model_calls": 0,
                },
            )
            return result

        interpretation_result = interpreter(
            request,
            agent_run_id=task.parent_run_id,
            strict_external=True,
        )
        execution = interpretation_result.execution
        _write_llm_completion_audit(runtime, audit_client, task, execution)
        duration_ms = int((time.monotonic() - started) * 1_000)
        model_calls, ledger_tokens, ledger_cost = _ledger_usage(ledger, duration_ms)
        input_tokens = int(execution.input_tokens)
        output_tokens = int(execution.output_tokens)
        token_usage = ledger_tokens or input_tokens + output_tokens
        estimated_cost = ledger_cost or float(execution.estimated_cost_usd)
        model_call_count = model_calls or 1
        result = _build_result(
            task,
            status=AgentTaskStatus.SUCCESS,
            evidence_references=references,
            structured_output={
                **deterministic_output,
                "interpretation": interpretation_result.model_dump(mode="json"),
                "execution": _execution_payload(
                    provider=execution.provider,
                    model=execution.model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    estimated_cost_usd=estimated_cost,
                    model_calls=model_call_count,
                    external_interpretation=True,
                ),
            },
            duration_ms=duration_ms,
            model_route=AgentModelRoute.QUICKTHINK_LLM,
            model_call_count=model_call_count,
            token_usage=token_usage,
            estimated_cost_usd=estimated_cost,
        )
        _write_handoff_audit(
            runtime,
            audit_client,
            task,
            "evidence_worker_completed",
            "success",
            duration_ms=duration_ms,
            output_payload={
                "result_status": result.status.value,
                "evidence_reference_count": len(references),
                "model_calls": model_call_count,
                "token_usage": token_usage,
                "estimated_cost_usd": estimated_cost,
            },
        )

        logger.info(
            "Evidence worker completed | task_id=%s task_type=%s evidence=%d provider=%s model=%s",
            task.task_id,
            task.task_type,
            len(references),
            execution.provider,
            execution.model,
        )

        return result

    except Exception as exc:
        duration_ms = int((time.monotonic() - started) * 1_000)
        model_calls, token_usage, estimated_cost = _ledger_usage(ledger, duration_ms)
        error_message = _sanitize_error(exc, "execution")
        model_route = (
            AgentModelRoute.QUICKTHINK_LLM
            if model_calls
            else AgentModelRoute.NO_LLM_FALLBACK
        )
        result = _build_result(
            task,
            status=AgentTaskStatus.FAILED,
            evidence_references=references,
            structured_output={
                "execution": _execution_payload(
                    input_tokens=token_usage,
                    estimated_cost_usd=estimated_cost,
                    model_calls=model_calls,
                    external_interpretation=bool(model_calls),
                ),
            },
            duration_ms=duration_ms,
            errors=[error_message],
            model_route=model_route,
            model_call_count=model_calls,
            token_usage=token_usage,
            estimated_cost_usd=estimated_cost,
        )

        if audit_client is not None:
            try:
                _write_handoff_audit(
                    runtime,
                    audit_client,
                    task,
                    "evidence_worker_failed",
                    "failed",
                    duration_ms=duration_ms,
                    output_payload={"result_status": result.status.value},
                    error_message=error_message,
                )
            except Exception as audit_exc:
                logger.error(
                    "Evidence worker failure audit could not be written | task_id=%s error_type=%s",
                    task.task_id,
                    type(audit_exc).__name__,
                )

        logger.error(
            "Evidence worker failed | task_id=%s task_type=%s error_type=%s",
            task.task_id,
            task.task_type,
            type(exc).__name__,
        )

        return result
