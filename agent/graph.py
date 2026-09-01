####
## LangGraph Triage Workflow for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict
from uuid import UUID, uuid4


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Add repo root before importing project packages when this file is executed by path.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.state import (
    Alert,
    ApprovalActionType,
    ApprovalGatedAction,
    EvidenceItem,
    EvidenceType,
    Hypothesis,
    LlmRuntimeSummary,
    TriageReport,
    TriageState,
)
from agent.checkpointing import (
    CHECKPOINT_MODE_OFF,
    build_checkpoint_config,
    checkpoint_exists,
    load_checkpoint_settings,
    open_checkpoint_saver,
    replay_historical_checkpoint_branch,
    resume_checkpointed_graph,
)
from agent.display import build_alert_one_liner, build_alert_ref, build_alert_title, build_report_id
from agent.llm.client import LlmResponse, run_llm_task
from agent.nodes import TriageNodeFactory
from agent.planning.evidence import EVIDENCE_PLANNING_ROUTE, build_evidence_plan_for_state
from agent.reasoning.hypotheses import HYPOTHESIS_FRAMING_ROUTE, frame_hypotheses_for_state
from agent.supervisor.policy import (
    assess_incident_complexity,
    resolve_report_reasoning_policy,
)
from agent.tools.audit_log import build_audit_idempotency_key, write_agent_audit_event
from agent.tools.s3 import resolve_artifacts_bucket
from pipelines.common.clickhouse import (
    DEFAULT_CLICKHOUSE_HOST,
    DEFAULT_CLICKHOUSE_PORT,
    build_clickhouse_client,
    format_date_literal,
    validate_qualified_table_name,
)
from pipelines.common.logging import logger
from pipelines.seeding.upload_to_s3 import resolve_s3_endpoint


# --- Defining Constants
TOOL_NAME                 = "langgraph_triage"
DEFAULT_CONFIDENCE_TARGET = 0.70
DEFAULT_MAX_EVIDENCE_LOOP = 2
DEFAULT_REPORT_PREFIX     = "agent-reports"
REPORT_NARRATIVE_ROUTE    = "triage_reasoning"
MAX_INVESTIGATION_ERRORS  = 10
MAX_INVESTIGATION_ERROR_LENGTH = 1_000


# --- Defining Classes
class GraphState(TypedDict):
    """
    LangGraph state wrapper.

    Attributes:
        state: Pydantic triage state used by the agent workflow.
    """

    state: TriageState


@dataclass(frozen=True)
class TriageRuntimeConfig:
    """
    Runtime dependencies and overrides for one triage graph execution.

    Attributes:
        manifest_path: Optional local dbt manifest path.
        manifest_s3_uri: Optional S3 URI for dbt manifest.json.
        s3_endpoint_url: Optional S3 endpoint override.
        artifacts_bucket: Optional report artifact bucket override.
        artifacts_prefix: S3 prefix for report artifacts.
        clickhouse_host: Optional ClickHouse host override.
        clickhouse_port: Optional ClickHouse HTTP port override.
    """

    manifest_path: str | None      = None
    manifest_s3_uri: str | None    = None
    s3_endpoint_url: str | None    = None
    artifacts_bucket: str | None   = None
    artifacts_prefix: str          = DEFAULT_REPORT_PREFIX
    clickhouse_host: str | None    = None
    clickhouse_port: int | None    = None


# --- Defining Functions
def get_state(graph_state: GraphState) -> TriageState:
    """
    Extract the Pydantic triage state from the LangGraph wrapper.

    Args:
        graph_state: LangGraph state dictionary.

    Returns:
        Current TriageState instance.
    """
    return graph_state["state"]


def build_runtime_contract_payload(config: TriageRuntimeConfig) -> dict[str, Any]:
    """
    Build a non-secret runtime contract for checkpoint replay validation.

    Args:
        config: Triage runtime configuration selected for the graph.

    Returns:
        Canonically serializable evidence and side-effect target values.
    """
    clickhouse_host = config.clickhouse_host or os.getenv(
        "CLICKHOUSE_HOST",
        DEFAULT_CLICKHOUSE_HOST,
    )
    clickhouse_port = int(
        config.clickhouse_port
        or os.getenv("CLICKHOUSE_HTTP_PORT", str(DEFAULT_CLICKHOUSE_PORT))
    )

    return {
        "manifest_path": str(config.manifest_path or ""),
        "manifest_s3_uri": str(config.manifest_s3_uri or ""),
        "s3_endpoint_url": resolve_s3_endpoint(config.s3_endpoint_url),
        "artifacts_bucket": resolve_artifacts_bucket(config.artifacts_bucket),
        "artifacts_prefix": config.artifacts_prefix.strip("/"),
        "clickhouse_host": str(clickhouse_host),
        "clickhouse_port": clickhouse_port,
        "clickhouse_database": os.getenv("CLICKHOUSE_DB", "dq"),
        "clickhouse_user": os.getenv("CLICKHOUSE_USER", "default"),
    }


def build_runtime_contract_hash(config: TriageRuntimeConfig) -> str:
    """
    Hash the effective runtime contract used by checkpointed triage.

    Args:
        config: Triage runtime configuration selected for the graph.

    Returns:
        Lowercase SHA-256 digest over canonical non-secret runtime values.
    """
    payload = build_runtime_contract_payload(config)
    canonical_payload = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )

    return hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()


def validate_historical_replay_state(
    state: TriageState,
    requested_alert_id: str | UUID | None,
    requested_alert_key: str | None,
    runtime_contract_hash: str,
) -> None:
    """
    Validate checkpoint identity and runtime targets before historical replay.

    Args:
        state: Triage state loaded from the selected source checkpoint.
        requested_alert_id: Optional operator-requested alert UUID.
        requested_alert_key: Optional operator-requested stable alert key.
        runtime_contract_hash: Hash for the current runtime configuration.

    Returns:
        None.

    Raises:
        ValueError: If alert identity or runtime configuration does not match.
    """
    persisted_alert_id = state.alert.alert_id if state.alert else state.alert_id
    persisted_alert_key = state.alert.alert_key if state.alert else state.alert_key

    if requested_alert_id and str(persisted_alert_id or "") != str(requested_alert_id):
        raise ValueError("Historical checkpoint alert_id does not match the replay request.")

    if requested_alert_key and persisted_alert_key != requested_alert_key.strip():
        raise ValueError("Historical checkpoint alert_key does not match the replay request.")

    if not state.runtime_contract_hash:
        raise ValueError("Historical checkpoint predates the runtime contract and cannot be replayed safely.")

    if state.runtime_contract_hash != runtime_contract_hash:
        raise ValueError("Historical checkpoint runtime contract does not match the current configuration.")


def append_error(state: TriageState, message: str) -> None:
    """
    Add a non-fatal workflow error to the triage state.

    Args:
        state: Current triage state.
        message: Error message to record.

    Returns:
        None.
    """
    logger.warning("Recording non-fatal triage error | agent_run_id=%s error=%s", state.agent_run_id, message)
    state.errors.append(message)


def normalize_investigation_errors(errors: list[str]) -> list[str]:
    """
    Normalize non-fatal investigation errors for reports and handoff contracts.

    Args:
        errors: Raw graph errors collected from optional tools or narrative routes.

    Returns:
        Unique, single-line, bounded errors safe for persisted operator artifacts.
    """
    normalized_errors: list[str] = []

    for raw_error in errors:
        normalized = " ".join(str(raw_error or "").split())[:MAX_INVESTIGATION_ERROR_LENGTH]

        if normalized and normalized not in normalized_errors:
            normalized_errors.append(normalized)

        if len(normalized_errors) >= MAX_INVESTIGATION_ERRORS:
            break

    return normalized_errors


def current_partition_sql(alert: Alert) -> str:
    """
    Build a guarded SQL query for the current alert partition row count.

    Args:
        alert: Alert being investigated.

    Returns:
        SQL statement that counts rows for alert.table_name and alert.dt.

    Raises:
        ValueError: If alert.dt is missing or the table name is unsafe.
    """
    if alert.dt is None:
        raise ValueError("Alert dt is required for partition evidence.")

    table_name = validate_qualified_table_name(alert.table_name)

    return f"""
        SELECT
            count() AS row_count
        FROM {table_name}
        WHERE dt = {format_date_literal(alert.dt)}
        LIMIT 1
    """


def recent_partition_sql(alert: Alert, lookback_days: int = 7) -> str:
    """
    Build a guarded SQL query for recent partition row counts.

    Args:
        alert: Alert being investigated.
        lookback_days: Number of days before alert.dt to include.

    Returns:
        SQL statement that returns row counts by dt for recent partitions.

    Raises:
        ValueError: If alert.dt is missing or the table name is unsafe.
    """
    if alert.dt is None:
        raise ValueError("Alert dt is required for recent partition evidence.")

    table_name    = validate_qualified_table_name(alert.table_name)
    safe_lookback = max(1, min(lookback_days, 30))

    return f"""
        SELECT
            dt,
            count() AS row_count
        FROM {table_name}
        WHERE dt >= {format_date_literal(alert.dt)} - INTERVAL {safe_lookback} DAY
          AND dt <= {format_date_literal(alert.dt)}
        GROUP BY dt
        ORDER BY dt DESC
        LIMIT {safe_lookback + 1}
    """


def sql_result_to_evidence(
    result: Any,
    description: str,
    summary: str,
) -> EvidenceItem:
    """
    Convert a guarded SQL result into a triage evidence item.

    Args:
        result: SqlExecutionResult returned by the guarded SQL tool.
        description: Human-readable reason for collecting this query.
        summary: Human-readable finding from the query.

    Returns:
        EvidenceItem representing the SQL result.
    """
    return EvidenceItem(
        evidence_type=EvidenceType.SQL_RESULT,
        tool_name="clickhouse_sql",
        description=description,
        query=result.executed_sql,
        rows=result.rows,
        row_count=result.row_count,
        summary=summary,
    )


def partition_row_count_from_evidence(state: TriageState) -> int | None:
    """
    Extract the current partition row count from collected SQL evidence.

    Args:
        state: Current triage state.

    Returns:
        Row count when available, otherwise None.
    """
    for evidence in state.evidence:
        if evidence.tool_name != "clickhouse_sql":
            continue

        if "Current partition row count" not in evidence.description:
            continue

        if not evidence.rows:
            return None

        return int(evidence.rows[0].get("row_count", 0))

    return None


def evidence_ids(state: TriageState, tool_names: set[str] | None = None) -> list[str]:
    """
    Select evidence ids for hypothesis support references.

    Args:
        state: Current triage state.
        tool_names: Optional set of tool names to include.

    Returns:
        List of evidence ids.
    """
    selected = []

    for evidence in state.evidence:
        if tool_names and evidence.tool_name not in tool_names:
            continue

        selected.append(evidence.evidence_id)

    return selected


def has_pipeline_success_for_alert_dt(state: TriageState) -> bool:
    """
    Check whether pipeline run evidence includes a successful run for the alert date.

    Args:
        state: Current triage state.

    Returns:
        True when at least one successful pipeline row matches alert.dt.
    """
    if not state.alert or not state.alert.dt:
        return False

    target_dt = state.alert.dt.isoformat()

    for evidence in state.evidence:
        if evidence.tool_name != "pipeline_runs":
            continue

        for row in evidence.rows:
            partition_dt = str(row.get("partition_dt") or row.get("logical_date") or "")
            status       = str(row.get("status") or "").lower()

            if target_dt in partition_dt and status == "success":
                return True

    return False


def build_missing_partition_hypothesis(state: TriageState) -> Hypothesis:
    """
    Build the primary missing-partition hypothesis for empty row count alerts.

    Args:
        state: Current triage state.

    Returns:
        Evidence-backed missing partition hypothesis.
    """
    alert             = state.alert
    row_count         = partition_row_count_from_evidence(state)
    pipeline_success  = has_pipeline_success_for_alert_dt(state)
    confidence        = 0.74

    if row_count == 0:
        confidence += 0.10

    if not pipeline_success:
        confidence += 0.04

    confidence = min(confidence, 0.92)

    description = (
        f"The alert partition for {alert.table_name if alert else 'the target table'} appears empty or missing. "
        "This points to a landing/load/backfill gap rather than a downstream-only mart issue."
    )

    return Hypothesis(
        title="Missing or empty ClickHouse partition",
        description=description,
        likelihood=confidence,
        confidence=confidence,
        root_cause_category="missing_partition",
        supporting_evidence_ids=evidence_ids(state),
        recommended_action="Backfill the affected date through the daily landing, load, dbt, DQ, and alert workflow.",
    )


def build_freshness_hypothesis(state: TriageState) -> Hypothesis:
    """
    Build the primary freshness hypothesis for stale or missing latest-day alerts.

    Args:
        state: Current triage state.

    Returns:
        Evidence-backed freshness hypothesis.
    """
    row_count  = partition_row_count_from_evidence(state)
    confidence = 0.78

    if row_count == 0:
        confidence += 0.07

    return Hypothesis(
        title="Missing latest-day data",
        description="The affected table does not appear to have complete data for the expected business date.",
        likelihood=min(confidence, 0.90),
        confidence=min(confidence, 0.90),
        root_cause_category="freshness_gap",
        supporting_evidence_ids=evidence_ids(state),
        recommended_action="Validate landing and raw load for the missing date, then rerun downstream dbt and DQ jobs.",
    )


def build_segment_hypothesis(state: TriageState) -> Hypothesis:
    """
    Build the primary segment-coverage hypothesis for country/channel gaps.

    Args:
        state: Current triage state.

    Returns:
        Evidence-backed segment hypothesis.
    """
    return Hypothesis(
        title="Missing segment coverage",
        description="One or more expected country/channel segments are missing from the generated or loaded orders data.",
        likelihood=0.72,
        confidence=0.72,
        root_cause_category="missing_segment",
        supporting_evidence_ids=evidence_ids(state),
        recommended_action="Compare segment counts against the seeding config and regenerate/backfill the affected partition if needed.",
    )


def schema_drift_rows(state: TriageState) -> list[dict[str, Any]]:
    """
    Return bounded schema finding rows already collected by the guarded tool.

    Args:
        state: Current triage state.

    Returns:
        Combined schema drift finding rows from state evidence.
    """
    return [
        row
        for evidence in state.evidence
        if evidence.tool_name == "schema_drift"
        for row in evidence.rows
    ]


def build_schema_drift_hypotheses(state: TriageState) -> list[Hypothesis]:
    """
    Build policy-owned hypotheses from exact persisted schema evidence.

    Args:
        state: Current triage state containing a schema drift alert.

    Returns:
        Ranked schema-change hypotheses with deterministic confidence.
    """
    rows = schema_drift_rows(state)

    if not rows:
        return [
            Hypothesis(
                title="Schema change evidence is unavailable",
                description=(
                    "The alert identifies a schema contract change, but the exact persisted detector evidence "
                    "could not be loaded for this investigation."
                ),
                likelihood=0.45,
                confidence=0.45,
                root_cause_category="schema_evidence_unavailable",
                supporting_evidence_ids=evidence_ids(state, {"pipeline_runs", "dbt_lineage"}),
                recommended_action=(
                    "Review the schema-drift tool audit, then rerun deterministic schema detection before "
                    "approving any contract or warehouse change."
                ),
            )
        ]

    finding_types   = sorted({str(row.get("check_type") or "unknown") for row in rows})
    changed_columns = sorted({str(row.get("column_name") or "<table>") for row in rows})
    is_breaking     = any(
        str(row.get("status") or "").lower() == "fail"
        or str(row.get("severity") or "").lower() == "critical"
        for row in rows
    )
    primary_title    = (
        "Breaking schema contract change detected"
        if is_breaking
        else "Additive schema contract change needs review"
    )
    primary_category = "breaking_schema_change" if is_breaking else "additive_schema_change"

    return [
        Hypothesis(
            title=primary_title,
            description=(
                f"Persisted schema evidence confirms {len(rows)} visible contract finding(s). "
                f"Finding types: {finding_types}. Affected columns: {changed_columns}."
            ),
            likelihood=0.94,
            confidence=0.94,
            root_cause_category=primary_category,
            supporting_evidence_ids=evidence_ids(state, {"schema_drift", "dbt_lineage"}),
            recommended_action=(
                "Review the producer schema change and lineage impact, then use a human-approved compatibility "
                "or contract migration plan. Do not alter the warehouse schema automatically."
            ),
        ),
        Hypothesis(
            title="Schema contract may be behind an intentional producer change",
            description=(
                "The physical schema may have changed intentionally, but the current contract and downstream "
                "consumer expectations have not yet been reviewed together."
            ),
            likelihood=0.35,
            confidence=0.35,
            root_cause_category="schema_contract_review",
            supporting_evidence_ids=evidence_ids(state, {"schema_drift"}),
            recommended_action=(
                "Confirm producer intent and consumer compatibility before proposing a versioned contract update."
            ),
        ),
    ]


def build_generic_hypotheses(state: TriageState) -> list[Hypothesis]:
    """
    Build fallback hypotheses when the alert metric is not yet classified.

    Args:
        state: Current triage state.

    Returns:
        Ranked fallback hypotheses.
    """
    return [
        Hypothesis(
            title="Upstream or load-layer data quality issue",
            description="The alert is supported by DQ evidence, but the current rule needs additional context before assigning a narrow root cause.",
            likelihood=0.62,
            confidence=0.62,
            root_cause_category="unknown_data_issue",
            supporting_evidence_ids=evidence_ids(state),
            recommended_action="Review DQ history, recent pipeline runs, and source partition evidence before approving remediation.",
        ),
        Hypothesis(
            title="DQ rule threshold needs review",
            description="The alert may be valid, but the expected/threshold value should be checked against recent baseline behavior.",
            likelihood=0.38,
            confidence=0.38,
            root_cause_category="threshold_review",
            supporting_evidence_ids=evidence_ids(state, {"dq_history"}),
            recommended_action="Inspect the DQ contract and recent baseline before changing thresholds.",
        ),
    ]


def build_hypotheses_for_state(state: TriageState) -> list[Hypothesis]:
    """
    Generate candidate hypotheses from the current alert and evidence.

    Args:
        state: Current triage state.

    Returns:
        Ranked hypothesis list.
    """
    if not state.alert:
        raise ValueError("Alert must be loaded before generating hypotheses.")

    metric = state.alert.metric.lower()

    if state.alert.is_schema_drift:
        hypotheses = build_schema_drift_hypotheses(state)

    elif "row_count" in metric:
        hypotheses = [
            build_missing_partition_hypothesis(state),
            Hypothesis(
                title="Late-arriving upstream data",
                description="The source file or events may arrive after the DQ check runs, leaving the current partition temporarily empty.",
                likelihood=0.52,
                confidence=0.52,
                root_cause_category="late_arriving",
                supporting_evidence_ids=evidence_ids(state, {"pipeline_runs", "dq_history"}),
                recommended_action="Check whether the landing object appears later, then rerun the idempotent load for the date.",
            ),
        ]

    elif "freshness" in metric:
        hypotheses = [build_freshness_hypothesis(state)]

    elif "segment" in metric or "coverage" in metric:
        hypotheses = [build_segment_hypothesis(state)]

    else:
        hypotheses = build_generic_hypotheses(state)

    return sorted(hypotheses, key=lambda item: item.confidence, reverse=True)


def build_approval_actions(state: TriageState, top_hypothesis: Hypothesis | None) -> list[ApprovalGatedAction]:
    """
    Build approval-gated remediation actions for the final report.

    Args:
        state: Current triage state.
        top_hypothesis: Highest-confidence hypothesis.

    Returns:
        List of approval-gated actions.
    """
    if not state.alert or not state.alert.dt or not top_hypothesis:
        return []

    if top_hypothesis.root_cause_category not in {"missing_partition", "freshness_gap", "late_arriving"}:
        return []

    return [
        ApprovalGatedAction(
            action_type=ApprovalActionType.BACKFILL,
            reason="Backfill is recommended because the evidence points to an incomplete or missing date partition.",
            target_dag_id="90_dag_dq_platform_backfill_dispatcher",
            start_date=state.alert.dt,
            end_date=state.alert.dt,
            parameters={
                "target_dag_id": "00_dag_dq_platform_daily_orchestrator",
                "run_mode": "backfill",
                "run_seed": True,
                "run_load": True,
                "run_dbt": True,
                "run_dq": True,
                "run_triage": False,
                "requested_by": "agentic_triage",
                "reason": top_hypothesis.title,
            },
        )
    ]


def build_report_narrative_context(state: TriageState) -> dict[str, Any]:
    """
    Build bounded context for optional LLM-assisted report narrative.

    Args:
        state: Current triage state.

    Returns:
        Dictionary with alert, top hypothesis, compact evidence summaries, and errors.
    """
    alert                  = state.alert
    top_hypothesis         = state.top_hypothesis
    complexity_assessment = assess_incident_complexity(state)
    evidence_rows          = [
        {
            "tool_name": item.tool_name,
            "summary": item.summary,
            "row_count": item.row_count,
        }
        for item in state.evidence[:8]
    ]

    return {
        "alert": alert.model_dump(mode="json") if alert else {},
        "top_hypothesis": top_hypothesis.model_dump(mode="json") if top_hypothesis else {},
        "evidence": evidence_rows,
        "confidence": top_hypothesis.confidence if top_hypothesis else 0.0,
        "complexity_assessment": complexity_assessment.model_dump(mode="json"),
        "errors": state.errors,
    }


def build_report_narrative_prompt(state: TriageState) -> str:
    """
    Build the prompt for optional LLM-assisted report narrative.

    Args:
        state: Current triage state.

    Returns:
        Prompt text for the routed LLM client.
    """
    alert      = state.alert
    top        = state.top_hypothesis
    complexity = assess_incident_complexity(state)

    return (
        "Write a concise senior data engineering triage narrative for this DQ alert. "
        "Use only the provided evidence. Mention the likely root cause, likely impact, "
        "and why remediation must stay approval-gated. "
        f"Alert table={alert.table_name if alert else 'unknown'}, "
        f"metric={alert.metric if alert else 'unknown'}, "
        f"dt={alert.dt if alert else 'unknown'}, "
        f"top_hypothesis={top.title if top else 'unknown'}, "
        f"complexity_tier={complexity.tier}, "
        f"complexity_reasons={list(complexity.reason_codes)}."
    )


def build_llm_report_narrative(state: TriageState) -> LlmResponse:
    """
    Build optional LLM-assisted narrative text for the final report.

    Args:
        state: Current triage state.

    Returns:
        LlmResponse with content, model, token, fallback, and cost metadata.
    """
    top_confidence         = state.top_hypothesis.confidence if state.top_hypothesis else 0.0
    complexity_assessment = assess_incident_complexity(state)
    route_policy           = resolve_report_reasoning_policy(
        confidence=top_confidence,
        confidence_threshold=state.confidence_threshold,
        complexity_assessment=complexity_assessment,
    )

    logger.info(
        "Building optional LLM report narrative | agent_run_id=%s route=%s reason=%s complexity=%s score=%d",
        state.agent_run_id,
        route_policy.provider_route,
        route_policy.reason_code.value,
        complexity_assessment.tier,
        complexity_assessment.score,
    )

    return run_llm_task(
        route_name=route_policy.provider_route,
        prompt=build_report_narrative_prompt(state=state),
        system_prompt=(
            "You are a data reliability copilot. Keep the answer evidence-driven, concise, "
            "and safe for an incident report. Do not invent facts."
        ),
        context=build_report_narrative_context(state=state),
        agent_run_id=state.agent_run_id,
    )


def llm_response_to_evidence(response: LlmResponse) -> EvidenceItem:
    """
    Convert an LLM routing response into auditable report evidence.

    Args:
        response: Routed LLM response.

    Returns:
        EvidenceItem that captures route, provider, model, token, and cost metadata.
    """
    return EvidenceItem(
        evidence_type=EvidenceType.NOTE,
        tool_name="llm_router",
        description="Optional LLM-assisted report narrative generated from collected evidence.",
        rows=[
            {
                "route_name": response.route_name,
                "requested_route": response.metadata.get("requested_route", response.route_name),
                "executed_route": response.metadata.get("executed_route", response.route_name),
                "attempted_routes": response.metadata.get("attempted_routes", [response.route_name]),
                "provider": response.provider,
                "model": response.model,
                "used_heuristic": response.used_heuristic,
                "fallback_reason": response.fallback_reason,
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
                "estimated_cost_usd": response.estimated_cost_usd,
                "duration_ms": response.duration_ms,
                "structured_output_requested": response.metadata.get("structured_output_requested", False),
                "structured_output_mode": response.metadata.get("structured_output_mode", ""),
                "structured_output_status": response.metadata.get("structured_output_status", ""),
                "structured_output_provider_fallback": response.metadata.get(
                    "structured_output_provider_fallback",
                    False,
                ),
                "provider_failures": response.metadata.get("provider_failures", []),
            }
        ],
        row_count=1,
        summary=(
            f"LLM route {response.route_name} used provider {response.provider}/{response.model} "
            f"with estimated cost USD {response.estimated_cost_usd:.8f}."
        ),
    )


def render_markdown_report(report: TriageReport) -> str:
    """
    Render the final triage report as Markdown.

    Args:
        report: Triage report model.

    Returns:
        Markdown report body.
    """
    alert       = report.alert
    alert_ref   = alert.alert_display_id or build_alert_ref(alert.alert_key, alert.dt)
    issue_title = build_alert_title(alert)
    issue_text  = build_alert_one_liner(alert)
    report_id   = report.report_id or build_report_id(report.agent_run_id, alert.alert_key)

    lines = [
        f"# {issue_title}",
        "",
        f"Report ID: `{report_id}`",
        f"Alert Ref: `{alert_ref}`",
        "",
        "## Summary",
        report.summary,
        "",
        "## Plain-English Readout",
        issue_text,
        "",
        "## Alert Context",
        f"- Severity: `{alert.severity}`",
        f"- Status: `{alert.status}`",
        f"- Table: `{alert.table_name}`",
        f"- Metric: `{alert.metric}`",
        f"- Date: `{alert.dt}`",
        f"- Observed: `{alert.observed_value}`",
        f"- Expected: `{alert.expected_value}`",
        "",
        "## Impact",
        report.impact,
        "",
        "## Reasoning Complexity",
    ]

    if report.complexity_assessment:
        lines.extend(
            [
                f"- Tier: `{report.complexity_assessment.tier}`",
                f"- Score: `{report.complexity_assessment.score}`",
                (
                    "- Strong Reasoning Required: "
                    f"`{report.complexity_assessment.strong_reasoning_required}`"
                ),
                f"- Reasons: `{list(report.complexity_assessment.reason_codes)}`",
                (
                    "- Deterministic Evidence Types: "
                    f"`{list(report.complexity_assessment.deterministic_evidence_types)}`"
                ),
            ]
        )

    else:
        lines.append("- Complexity assessment was not available for this report.")

    lines.extend(
        [
            "",
            "## LLM Runtime",
            f"- Route Events: `{report.llm_runtime.route_event_count}`",
            f"- Requested Routes: `{report.llm_runtime.requested_routes}`",
            f"- Executed Routes: `{report.llm_runtime.executed_routes}`",
            f"- Providers: `{report.llm_runtime.providers}`",
            f"- Models: `{report.llm_runtime.models}`",
            f"- External Model Used: `{report.llm_runtime.external_model_used}`",
            f"- Heuristic Fallback Used: `{report.llm_runtime.heuristic_fallback_used}`",
            f"- Input / Output Tokens: `{report.llm_runtime.input_tokens}` / `{report.llm_runtime.output_tokens}`",
            f"- Estimated Cost (USD): `{report.llm_runtime.estimated_cost_usd:.8f}`",
            f"- Duration (ms): `{report.llm_runtime.duration_ms}`",
            f"- Fallback Reasons: `{report.llm_runtime.fallback_reasons}`",
            "",
            "## Evidence Plan",
        ]
    )

    if report.evidence_plan:
        lines.extend(
            [
                f"- Planner Source: `{report.evidence_plan.planner_source}`",
                f"- Investigation Question: {report.evidence_plan.investigation_question}",
                f"- Model Route: `{report.evidence_plan.llm_route or 'none'}`",
                f"- Policy Added: `{report.evidence_plan.policy_added_categories}`",
                f"- Policy Priority Adjustments: `{report.evidence_plan.policy_adjusted_categories}`",
            ]
        )

        for request in report.evidence_plan.requests:
            lines.append(
                f"- `{request.category}` priority `{request.priority}` required `{request.required}`: {request.reason}"
            )
    else:
        lines.append("- No evidence plan was recorded; deterministic baseline collectors were used.")

    lines.extend(["", "## Evidence Reviewed"])

    for index, evidence in enumerate(report.evidence, start=1):
        lines.extend(
            [
                f"{index}. `{evidence.tool_name}` - {evidence.summary}",
                f"   - Evidence ID: `{evidence.evidence_id}`",
                f"   - Rows: `{evidence.row_count}`",
            ]
        )

    lines.extend(["", "## Investigation Gaps"])

    if report.investigation_errors:
        for error in report.investigation_errors:
            lines.append(f"- {error}")

    else:
        lines.append("- No non-fatal evidence collection gaps were retained.")

    lines.extend(["", "## Hypothesis Framing"])

    if report.hypothesis_framing:
        lines.extend(
            [
                f"- Source: `{report.hypothesis_framing.source}`",
                f"- Model Route: `{report.hypothesis_framing.requested_route or 'none'}`",
                f"- Provider: `{report.hypothesis_framing.provider or 'deterministic fallback'}`",
                f"- Model: `{report.hypothesis_framing.model or 'none'}`",
                f"- Accepted Categories: `{report.hypothesis_framing.accepted_categories}`",
                f"- Policy Adjustments: `{report.hypothesis_framing.policy_adjustments}`",
            ]
        )
    else:
        lines.append("- Deterministic hypothesis wording was used without model framing metadata.")

    lines.extend(["", "## Hypotheses"])

    for index, hypothesis in enumerate(report.hypotheses, start=1):
        lines.extend(
            [
                f"{index}. {hypothesis.title}",
                f"   - Confidence: `{hypothesis.confidence:.2f}`",
                f"   - Category: `{hypothesis.root_cause_category}`",
                f"   - Wording Source: `{hypothesis.framing_source}`",
                f"   - Why: {hypothesis.description}",
                f"   - Action: {hypothesis.recommended_action}",
            ]
        )

    top_title = report.top_hypothesis.title if report.top_hypothesis else "Unknown"

    lines.extend(
        [
            "",
            "## Most Likely Root Cause",
            f"{top_title} with confidence `{report.confidence:.2f}`.",
            "",
            "## Recommended Actions",
        ]
    )

    for action in report.recommended_actions:
        lines.append(f"- {action}")

    lines.extend(["", "## Approval-Gated Actions"])

    if report.approval_gated_actions:
        for action in report.approval_gated_actions:
            lines.extend(
                [
                    f"- Action: `{action.action_type}`",
                    f"  - Target DAG: `{action.target_dag_id}`",
                    f"  - Date Range: `{action.start_date}` to `{action.end_date}`",
                    f"  - Reason: {action.reason}",
                ]
            )
    else:
        lines.append("- No approval-gated action recommended.")

    lines.extend(["", "## Residual Risks"])

    for risk in report.residual_risks:
        lines.append(f"- {risk}")

    lines.extend(
        [
            "",
            "## Artifacts",
            "- Markdown Report: `{{MARKDOWN_REPORT_S3_URI}}`",
            "- JSON Report: `{{JSON_REPORT_S3_URI}}`",
            "",
            "## Technical Reference",
            f"- System Alert Key: `{alert.alert_key}`",
            f"- ClickHouse Alert ID: `{alert.alert_id}`",
            f"- Agent Run ID: `{report.agent_run_id}`",
        ]
    )

    return "\n".join(lines).strip() + "\n"


def append_unique_runtime_value(values: list[str], raw_value: Any) -> None:
    """
    Append one bounded non-empty runtime value while preserving event order.

    Args:
        values: Mutable output collection.
        raw_value: Provider, model, route, or fallback value from evidence.

    Returns:
        None.
    """
    normalized = " ".join(str(raw_value or "").split())[:500]

    if normalized and normalized not in values and len(values) < 20:
        values.append(normalized)


def build_llm_runtime_summary(evidence_items: list[EvidenceItem]) -> LlmRuntimeSummary:
    """
    Aggregate sanitized LLM route evidence for report and audit observability.

    Args:
        evidence_items: Evidence retained by the current triage state.

    Returns:
        LlmRuntimeSummary with bounded route, provider, token, cost, and fallback facts.
    """
    requested_routes: list[str] = []
    executed_routes: list[str]  = []
    providers: list[str]        = []
    models: list[str]           = []
    fallback_reasons: list[str] = []

    route_event_count       = 0
    input_tokens            = 0
    output_tokens           = 0
    estimated_cost_usd      = 0.0
    duration_ms             = 0
    external_model_used     = False
    heuristic_fallback_used = False

    for evidence in evidence_items:
        if evidence.tool_name != "llm_router":
            continue

        for row in evidence.rows:
            if not isinstance(row, dict):
                continue

            route_event_count += 1

            append_unique_runtime_value(requested_routes, row.get("requested_route"))
            append_unique_runtime_value(executed_routes, row.get("executed_route"))
            append_unique_runtime_value(providers, row.get("provider"))
            append_unique_runtime_value(models, row.get("model"))
            append_unique_runtime_value(fallback_reasons, row.get("fallback_reason"))

            provider = str(row.get("provider", "") or "").strip().lower()

            external_model_used     = external_model_used or provider not in {"", "heuristic"}
            heuristic_fallback_used = heuristic_fallback_used or bool(row.get("used_heuristic"))
            input_tokens           += max(0, int(row.get("input_tokens", 0) or 0))
            output_tokens          += max(0, int(row.get("output_tokens", 0) or 0))
            estimated_cost_usd     += max(0.0, float(row.get("estimated_cost_usd", 0.0) or 0.0))
            duration_ms            += max(0, int(row.get("duration_ms", 0) or 0))

    return LlmRuntimeSummary(
        route_event_count=route_event_count,
        requested_routes=requested_routes,
        executed_routes=executed_routes,
        providers=providers,
        models=models,
        external_model_used=external_model_used,
        heuristic_fallback_used=heuristic_fallback_used,
        fallback_reasons=fallback_reasons,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        estimated_cost_usd=round(estimated_cost_usd, 10),
        duration_ms=duration_ms,
    )


def build_report_from_state(state: TriageState, llm_narrative: LlmResponse | None = None) -> TriageReport:
    """
    Build the final TriageReport model from current state.

    Args:
        state: Current triage state.
        llm_narrative: Optional routed LLM narrative response.

    Returns:
        Final triage report with Markdown body populated.

    Raises:
        ValueError: If alert or hypotheses are missing.
    """
    if not state.alert:
        raise ValueError("Alert is required before finalizing a report.")

    if not state.hypotheses:
        raise ValueError("At least one hypothesis is required before finalizing a report.")

    top_hypothesis         = state.top_hypothesis
    confidence             = top_hypothesis.confidence if top_hypothesis else 0.0
    complexity_assessment = assess_incident_complexity(state)
    report_id              = build_report_id(state.agent_run_id, state.alert.alert_key)
    issue_title            = build_alert_title(state.alert)

    summary = (
        f"{state.alert.severity} alert: {issue_title}. "
        f"Most likely cause is {top_hypothesis.title if top_hypothesis else 'unknown'}."
    )
    impact = (
        "Downstream staging, mart, DQ reporting, and dashboard metrics for the affected date may be incomplete "
        "until the partition is regenerated, reloaded, and revalidated."
    )
    recommended_actions = []

    if top_hypothesis and top_hypothesis.recommended_action:
        recommended_actions.append(top_hypothesis.recommended_action)

    if llm_narrative and llm_narrative.content:
        recommended_actions.append(f"LLM-assisted narrative: {llm_narrative.content}")

    recommended_actions.append("Review the stored evidence before approving any mutating remediation action.")
    investigation_errors = normalize_investigation_errors(state.errors)
    llm_runtime          = build_llm_runtime_summary(state.evidence)

    report = TriageReport(
        agent_run_id=state.agent_run_id,
        alert=state.alert,
        summary=summary,
        impact=impact,
        hypotheses=state.hypotheses,
        top_hypothesis=top_hypothesis,
        evidence=state.evidence,
        evidence_plan=state.evidence_plan,
        hypothesis_framing=state.hypothesis_framing,
        llm_runtime=llm_runtime,
        complexity_assessment=complexity_assessment,
        investigation_errors=investigation_errors,
        confidence=confidence,
        recommended_actions=recommended_actions,
        approval_gated_actions=build_approval_actions(state=state, top_hypothesis=top_hypothesis),
        residual_risks=[
            "The agent did not mutate production data; remediation still requires human approval.",
            "If source data is genuinely absent, backfill will not repair the issue without regenerating or restoring landing data.",
            *(
                ["One or more investigation dependencies failed; treat the diagnosis as partial until those evidence gaps are resolved."]
                if investigation_errors
                else []
            ),
        ],
        report_id=report_id,
    )
    report.markdown_report = render_markdown_report(report)

    logger.info("Built triage report | agent_run_id=%s confidence=%.2f", state.agent_run_id, confidence)

    return report


def build_triage_graph(
    config: TriageRuntimeConfig,
    checkpointer: Any | None = None,
):
    """
    Build the LangGraph workflow for evidence-driven triage.

    Args:
        config: Runtime dependencies and optional connection overrides.
        checkpointer: Optional LangGraph saver used for persistent state.

    Returns:
        Compiled LangGraph application.
    """
    from langgraph.graph import END, StateGraph

    nodes = TriageNodeFactory(
        config=config,
        append_error=append_error,
        current_partition_sql=current_partition_sql,
        recent_partition_sql=recent_partition_sql,
        sql_result_to_evidence=sql_result_to_evidence,
        build_evidence_plan_for_state=build_evidence_plan_for_state,
        build_hypotheses_for_state=build_hypotheses_for_state,
        frame_hypotheses_for_state=frame_hypotheses_for_state,
        build_llm_report_narrative=build_llm_report_narrative,
        llm_response_to_evidence=llm_response_to_evidence,
        build_report_from_state=build_report_from_state,
        evidence_planning_route_name=EVIDENCE_PLANNING_ROUTE,
        hypothesis_framing_route_name=HYPOTHESIS_FRAMING_ROUTE,
        llm_route_name=REPORT_NARRATIVE_ROUTE,
        tool_name=TOOL_NAME,
    )

    workflow = StateGraph(GraphState)

    workflow.add_node("load_alert", nodes.load_alert_node)
    workflow.add_node("plan_evidence", nodes.plan_evidence_node)
    workflow.add_node("gather_context", nodes.gather_context_node)
    workflow.add_node("generate_hypotheses", nodes.generate_hypotheses_node)
    workflow.add_node("rank_hypotheses", nodes.rank_hypotheses_node)
    workflow.add_node("collect_extra_evidence", nodes.collect_extra_evidence_node)
    workflow.add_node("finalize_report", nodes.finalize_report_node)
    workflow.add_node("store_report", nodes.store_report_node)
    workflow.add_node("write_final_audit", nodes.write_final_audit_node)

    workflow.set_entry_point("load_alert")
    workflow.add_edge("load_alert", "plan_evidence")
    workflow.add_edge("plan_evidence", "gather_context")
    workflow.add_edge("gather_context", "generate_hypotheses")
    workflow.add_edge("generate_hypotheses", "rank_hypotheses")
    workflow.add_conditional_edges(
        "rank_hypotheses",
        nodes.route_after_rank,
        {
            "collect_extra_evidence": "collect_extra_evidence",
            "finalize_report": "finalize_report",
        },
    )
    workflow.add_edge("collect_extra_evidence", "generate_hypotheses")
    workflow.add_edge("finalize_report", "store_report")
    workflow.add_edge("store_report", "write_final_audit")
    workflow.add_edge("write_final_audit", END)

    logger.info("LangGraph triage workflow compiled")

    return workflow.compile(checkpointer=checkpointer)


def coerce_final_triage_state(result: dict[str, Any]) -> TriageState:
    """
    Coerce a LangGraph result into the project's typed triage state.

    Args:
        result: LangGraph output or persisted checkpoint values.

    Returns:
        Validated TriageState instance.

    Raises:
        ValueError: If the graph result does not contain a state channel.
        pydantic.ValidationError: If persisted state does not match the contract.
    """
    raw_state = result.get("state")

    if raw_state is None:
        raise ValueError("Triage graph result does not contain state.")

    if isinstance(raw_state, TriageState):
        return raw_state

    return TriageState.model_validate(raw_state)


def run_triage(
    alert_id: str | UUID | None = None,
    alert_key: str | None = None,
    confidence_threshold: float = DEFAULT_CONFIDENCE_TARGET,
    max_evidence_iterations: int = DEFAULT_MAX_EVIDENCE_LOOP,
    config: TriageRuntimeConfig | None = None,
    checkpoint_mode: str | None = None,
    checkpoint_sqlite_path: str | None = None,
    checkpoint_busy_timeout_ms: int | None = None,
    checkpoint_thread_id: str | None = None,
    checkpoint_resume: bool = False,
    checkpoint_replay_id: str | None = None,
    checkpoint_replay_request_id: str | None = None,
) -> TriageReport:
    """
    Run one bounded LangGraph triage workflow for an alert.

    Args:
        alert_id: Optional alert UUID.
        alert_key: Optional stable alert key.
        confidence_threshold: Confidence needed before skipping extra evidence.
        max_evidence_iterations: Maximum extra evidence loop count.
        config: Optional runtime config.
        checkpoint_mode: Optional checkpoint mode override. Defaults to environment or off.
        checkpoint_sqlite_path: Optional absolute SQLite checkpoint path.
        checkpoint_busy_timeout_ms: Optional SQLite lock timeout in milliseconds.
        checkpoint_thread_id: Required stable thread id when checkpointing is enabled.
        checkpoint_resume: Resume the latest persisted state instead of starting a new thread.
        checkpoint_replay_id: Exact historical checkpoint selected for branched replay.
        checkpoint_replay_request_id: Stable request id used to derive the replay child thread.

    Returns:
        Final triage report with S3 URIs populated.

    Raises:
        ValueError: If identifiers, checkpoint settings, or report generation are invalid.
    """
    if not alert_id and not alert_key:
        raise ValueError("Provide alert_id or alert_key to run triage.")

    runtime_config      = config or TriageRuntimeConfig()
    runtime_contract_hash = build_runtime_contract_hash(runtime_config)
    checkpoint_settings = load_checkpoint_settings(
        mode=checkpoint_mode,
        sqlite_path=checkpoint_sqlite_path,
        busy_timeout_ms=checkpoint_busy_timeout_ms,
    )
    state = TriageState(
        agent_run_id=uuid4(),
        alert_id=UUID(str(alert_id)) if alert_id else None,
        alert_key=alert_key or "",
        runtime_contract_hash=runtime_contract_hash,
        confidence_threshold=confidence_threshold,
        max_evidence_iterations=max_evidence_iterations,
    )
    replay_checkpoint_id = (checkpoint_replay_id or "").strip()
    replay_request_id    = (checkpoint_replay_request_id or "").strip()

    if bool(replay_checkpoint_id) != bool(replay_request_id):
        raise ValueError(
            "checkpoint_replay_id and checkpoint_replay_request_id must be provided together."
        )

    if checkpoint_settings.enabled and not checkpoint_thread_id:
        raise ValueError("checkpoint_thread_id is required when persistent checkpointing is enabled.")

    if checkpoint_resume and not checkpoint_settings.enabled:
        raise ValueError("checkpoint_resume requires an enabled checkpoint backend.")

    if replay_checkpoint_id and not checkpoint_settings.enabled:
        raise ValueError("Historical checkpoint replay requires an enabled checkpoint backend.")

    if checkpoint_resume and replay_checkpoint_id:
        raise ValueError("Use latest checkpoint resume or historical replay, not both.")

    if checkpoint_settings.mode == CHECKPOINT_MODE_OFF and checkpoint_thread_id:
        logger.info("Ignoring checkpoint thread id because checkpoint mode is off")

    logger.info(
        "Starting triage graph | agent_run_id=%s alert_key=%s checkpoint_mode=%s "
        "checkpoint_thread_id=%s resume=%s replay_checkpoint_id=%s",
        state.agent_run_id,
        alert_key,
        checkpoint_settings.mode,
        checkpoint_thread_id or "disabled",
        checkpoint_resume,
        replay_checkpoint_id or "none",
    )

    replay_metadata = None
    source_state    = None

    with open_checkpoint_saver(checkpoint_settings) as checkpointer:
        graph = build_triage_graph(runtime_config, checkpointer=checkpointer)

        if checkpointer is None:
            result = graph.invoke({"state": state})

        else:
            graph_config = build_checkpoint_config(checkpoint_thread_id or "")

            if replay_checkpoint_id:
                source_config = build_checkpoint_config(
                    thread_id=checkpoint_thread_id or "",
                    checkpoint_id=replay_checkpoint_id,
                )

                if not checkpoint_exists(checkpointer=checkpointer, config=source_config):
                    raise ValueError("Historical checkpoint does not exist in the selected source thread.")

                source_snapshot = graph.get_state(source_config)
                source_state    = coerce_final_triage_state(dict(source_snapshot.values))
                validate_historical_replay_state(
                    state=source_state,
                    requested_alert_id=alert_id,
                    requested_alert_key=alert_key,
                    runtime_contract_hash=runtime_contract_hash,
                )
                result, replay_metadata = replay_historical_checkpoint_branch(
                    graph=graph,
                    checkpointer=checkpointer,
                    source_thread_id=checkpoint_thread_id or "",
                    source_checkpoint_id=replay_checkpoint_id,
                    replay_request_id=replay_request_id,
                )

            elif checkpoint_resume:
                result, executed_pending_nodes = resume_checkpointed_graph(
                    graph=graph,
                    checkpointer=checkpointer,
                    config=graph_config,
                )
                logger.info(
                    "Checkpoint resume resolved | thread_id=%s executed_pending_nodes=%s",
                    checkpoint_thread_id,
                    executed_pending_nodes,
                )

            else:
                if checkpoint_exists(checkpointer=checkpointer, config=graph_config):
                    raise ValueError(
                        "Checkpoint thread already exists; use checkpoint_resume or select a new run namespace."
                    )

                result = graph.invoke({"state": state}, config=graph_config)

    final_state = coerce_final_triage_state(dict(result))

    if not final_state.report:
        raise ValueError("Triage graph completed without a report.")

    if replay_metadata:
        if source_state is None or final_state.agent_run_id != source_state.agent_run_id:
            raise ValueError("Historical replay changed the persisted agent run identity.")

        replay_audit_key = build_audit_idempotency_key(
            "historical_checkpoint_replay_completed",
            replay_metadata.source_thread_id,
            replay_metadata.source_checkpoint_id,
            replay_metadata.replay_thread_id,
            final_state.agent_run_id,
            final_state.report.markdown_report_s3_uri,
        )
        audit_client = build_clickhouse_client(
            host=runtime_config.clickhouse_host,
            port=runtime_config.clickhouse_port,
        )
        write_agent_audit_event(
            client=audit_client,
            action="historical_checkpoint_replay_completed",
            status="success",
            agent_run_id=final_state.agent_run_id,
            alert_id=final_state.alert.alert_id if final_state.alert else None,
            alert_key=final_state.alert.alert_key if final_state.alert else final_state.alert_key,
            tool_name="checkpoint_replay",
            input_payload={
                "source_thread_id": replay_metadata.source_thread_id,
                "source_checkpoint_id": replay_metadata.source_checkpoint_id,
                "replay_request_id": replay_request_id,
                "runtime_contract_hash": runtime_contract_hash,
            },
            output_payload={
                "replay_thread_id": replay_metadata.replay_thread_id,
                "source_writer_node": replay_metadata.source_writer_node,
                "source_next_nodes": list(replay_metadata.source_next_nodes),
                "executed_pending_nodes": replay_metadata.executed_pending_nodes,
                "report_id": final_state.report.report_id,
            },
            row_count=len(final_state.evidence),
            report_s3_uri=final_state.report.markdown_report_s3_uri,
            idempotency_key=replay_audit_key,
        )

    logger.info(
        "Triage graph completed | agent_run_id=%s confidence=%.2f markdown_uri=%s checkpoint_thread_id=%s",
        final_state.agent_run_id,
        final_state.report.confidence,
        final_state.report.markdown_report_s3_uri,
        checkpoint_thread_id or "disabled",
    )

    return final_state.report


def build_parser() -> argparse.ArgumentParser:
    """
    Build the command-line parser for one triage run.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(description="Run one LangGraph DQ alert triage workflow.")

    parser.add_argument("--alert-id", default=None, help="Optional ClickHouse alert UUID.")
    parser.add_argument("--alert-key", default=None, help="Optional stable alert key.")
    parser.add_argument("--confidence-threshold", type=float, default=DEFAULT_CONFIDENCE_TARGET, help="Confidence threshold.")
    parser.add_argument("--max-evidence-iterations", type=int, default=DEFAULT_MAX_EVIDENCE_LOOP, help="Maximum evidence loops.")
    parser.add_argument("--manifest-path", default=None, help="Optional local dbt manifest.json path.")
    parser.add_argument("--manifest-s3-uri", default=None, help="Optional S3 URI for dbt manifest.json.")
    parser.add_argument("--endpoint-url", default=None, help="Optional S3 endpoint URL override.")
    parser.add_argument("--artifacts-bucket", default=None, help="Optional artifacts bucket override.")
    parser.add_argument("--artifacts-prefix", default=DEFAULT_REPORT_PREFIX, help="S3 prefix for report artifacts.")
    parser.add_argument("--clickhouse-host", default=None, help="Optional ClickHouse host override.")
    parser.add_argument("--clickhouse-port", type=int, default=None, help="Optional ClickHouse HTTP port override.")
    parser.add_argument("--checkpoint-mode", default=None, help="Checkpoint mode: off or sqlite.")
    parser.add_argument("--checkpoint-sqlite-path", default=None, help="Absolute SQLite checkpoint path.")
    parser.add_argument("--checkpoint-thread-id", default=None, help="Stable checkpoint thread identifier.")
    parser.add_argument("--checkpoint-resume", action="store_true", help="Resume an existing checkpoint thread.")
    parser.add_argument(
        "--checkpoint-replay-id",
        default=None,
        help="Exact historical checkpoint id selected for branched replay.",
    )
    parser.add_argument(
        "--checkpoint-replay-request-id",
        default=None,
        help="Stable request id used to derive an idempotent replay child thread.",
    )

    return parser


def main() -> None:
    """
    Parse CLI arguments, run triage, and print a compact JSON summary.

    Returns:
        None.
    """
    parser = build_parser()
    args   = parser.parse_args()
    config = TriageRuntimeConfig(
        manifest_path=args.manifest_path,
        manifest_s3_uri=args.manifest_s3_uri,
        s3_endpoint_url=args.endpoint_url,
        artifacts_bucket=args.artifacts_bucket,
        artifacts_prefix=args.artifacts_prefix,
        clickhouse_host=args.clickhouse_host,
        clickhouse_port=args.clickhouse_port,
    )
    report = run_triage(
        alert_id=args.alert_id,
        alert_key=args.alert_key,
        confidence_threshold=args.confidence_threshold,
        max_evidence_iterations=args.max_evidence_iterations,
        config=config,
        checkpoint_mode=args.checkpoint_mode,
        checkpoint_sqlite_path=args.checkpoint_sqlite_path,
        checkpoint_thread_id=args.checkpoint_thread_id,
        checkpoint_resume=args.checkpoint_resume,
        checkpoint_replay_id=args.checkpoint_replay_id,
        checkpoint_replay_request_id=args.checkpoint_replay_request_id,
    )
    summary = {
        "status": "success",
        "agent_run_id": str(report.agent_run_id),
        "alert_key": report.alert.alert_key,
        "confidence": report.confidence,
        "top_hypothesis": report.top_hypothesis.title if report.top_hypothesis else None,
        "markdown_report_s3_uri": report.markdown_report_s3_uri,
        "json_report_s3_uri": report.json_report_s3_uri,
        "approval_gated_actions": [item.model_dump(mode="json") for item in report.approval_gated_actions],
    }

    print(json.dumps(summary, indent=2, default=str))


# --- Running CLI Entrypoint
if __name__ == "__main__":
    main()
