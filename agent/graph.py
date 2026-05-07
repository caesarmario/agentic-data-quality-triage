####
## LangGraph Triage Workflow for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import argparse
import json
import sys
import time
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
    TriageReport,
    TriageState,
)
from agent.tools.alerts import load_alert
from agent.tools.audit_log import write_agent_audit_event
from agent.tools.clickhouse_sql import run_guarded_sql
from agent.tools.dbt_lineage import collect_dbt_lineage_evidence
from agent.tools.dq_history import collect_dq_history_evidence
from agent.tools.pipeline_runs import collect_pipeline_runs_evidence
from agent.tools.s3 import store_triage_report
from pipelines.common.clickhouse import build_clickhouse_client, format_date_literal, validate_qualified_table_name
from pipelines.common.logging import logger


# --- Defining Constants
TOOL_NAME                 = "langgraph_triage"
DEFAULT_CONFIDENCE_TARGET = 0.70
DEFAULT_MAX_EVIDENCE_LOOP = 2
DEFAULT_REPORT_PREFIX     = "agent-reports"


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

    if "row_count" in metric:
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
            target_dag_id="98_dag_dq_platform_backfill_dispatcher",
            start_date=state.alert.dt,
            end_date=state.alert.dt,
            parameters={
                "target_dag_id": "99_dag_dq_platform_daily_orchestrator",
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


def render_markdown_report(report: TriageReport) -> str:
    """
    Render the final triage report as Markdown.

    Args:
        report: Triage report model.

    Returns:
        Markdown report body.
    """
    alert = report.alert
    lines = [
        f"# Data Quality Triage Report - {alert.alert_key}",
        "",
        "## Summary",
        report.summary,
        "",
        "## Alert Context",
        f"- Alert Key: `{alert.alert_key}`",
        f"- Alert ID: `{alert.alert_id}`",
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
        "## Evidence Reviewed",
    ]

    for index, evidence in enumerate(report.evidence, start=1):
        lines.extend(
            [
                f"{index}. `{evidence.tool_name}` - {evidence.summary}",
                f"   - Evidence ID: `{evidence.evidence_id}`",
                f"   - Rows: `{evidence.row_count}`",
            ]
        )

    lines.extend(["", "## Hypotheses"])

    for index, hypothesis in enumerate(report.hypotheses, start=1):
        lines.extend(
            [
                f"{index}. {hypothesis.title}",
                f"   - Confidence: `{hypothesis.confidence:.2f}`",
                f"   - Category: `{hypothesis.root_cause_category}`",
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
        ]
    )

    return "\n".join(lines).strip() + "\n"


def build_report_from_state(state: TriageState) -> TriageReport:
    """
    Build the final TriageReport model from current state.

    Args:
        state: Current triage state.

    Returns:
        Final triage report with Markdown body populated.

    Raises:
        ValueError: If alert or hypotheses are missing.
    """
    if not state.alert:
        raise ValueError("Alert is required before finalizing a report.")

    if not state.hypotheses:
        raise ValueError("At least one hypothesis is required before finalizing a report.")

    top_hypothesis = state.top_hypothesis
    confidence     = top_hypothesis.confidence if top_hypothesis else 0.0

    summary = (
        f"{state.alert.severity} alert on {state.alert.table_name}.{state.alert.metric} "
        f"for {state.alert.dt}; most likely cause is {top_hypothesis.title if top_hypothesis else 'unknown'}."
    )
    impact = (
        "Downstream staging, mart, DQ reporting, and dashboard metrics for the affected date may be incomplete "
        "until the partition is regenerated, reloaded, and revalidated."
    )
    recommended_actions = []

    if top_hypothesis and top_hypothesis.recommended_action:
        recommended_actions.append(top_hypothesis.recommended_action)

    recommended_actions.append("Review the stored evidence before approving any mutating remediation action.")

    report = TriageReport(
        agent_run_id=state.agent_run_id,
        alert=state.alert,
        summary=summary,
        impact=impact,
        hypotheses=state.hypotheses,
        top_hypothesis=top_hypothesis,
        evidence=state.evidence,
        confidence=confidence,
        recommended_actions=recommended_actions,
        approval_gated_actions=build_approval_actions(state=state, top_hypothesis=top_hypothesis),
        residual_risks=[
            "The agent did not mutate production data; remediation still requires human approval.",
            "If source data is genuinely absent, backfill will not repair the issue without regenerating or restoring landing data.",
        ],
    )
    report.markdown_report = render_markdown_report(report)

    logger.info("Built triage report | agent_run_id=%s confidence=%.2f", state.agent_run_id, confidence)

    return report


def build_triage_graph(config: TriageRuntimeConfig):
    """
    Build the LangGraph workflow for evidence-driven triage.

    Args:
        config: Runtime dependencies and optional connection overrides.

    Returns:
        Compiled LangGraph application.
    """
    from langgraph.graph import END, StateGraph

    def load_alert_node(graph_state: GraphState) -> GraphState:
        """
        Load alert context from ClickHouse.

        Args:
            graph_state: Current graph state.

        Returns:
            Updated graph state with alert populated.
        """
        state       = get_state(graph_state)
        state.alert = load_alert(
            alert_id=str(state.alert_id) if state.alert_id else None,
            alert_key=state.alert_key or None,
            agent_run_id=state.agent_run_id,
            clickhouse_host=config.clickhouse_host,
            clickhouse_port=config.clickhouse_port,
        )

        logger.info("Graph loaded alert | agent_run_id=%s alert_key=%s", state.agent_run_id, state.alert.alert_key)

        return {"state": state}

    def gather_context_node(graph_state: GraphState) -> GraphState:
        """
        Gather baseline evidence using deterministic tools.

        Args:
            graph_state: Current graph state.

        Returns:
            Updated graph state with evidence appended.
        """
        state = get_state(graph_state)

        if not state.alert:
            raise ValueError("Alert must be loaded before gathering context.")

        evidence_builders = [
            ("current_partition_row_count", collect_current_partition_evidence),
            ("dq_history", collect_dq_history_for_state),
            ("pipeline_runs", collect_pipeline_runs_for_state),
            ("dbt_lineage", collect_lineage_evidence),
        ]

        for name, builder in evidence_builders:
            try:
                state.add_evidence(builder(state))

            except Exception as exc:
                append_error(state, f"{name} failed: {exc}")

        return {"state": state}

    def collect_current_partition_evidence(state: TriageState) -> EvidenceItem:
        """
        Collect current partition row count evidence.

        Args:
            state: Current triage state.

        Returns:
            EvidenceItem with one row_count result.
        """
        if not state.alert:
            raise ValueError("Alert must be loaded before partition evidence.")

        result = run_guarded_sql(
            sql=current_partition_sql(state.alert),
            agent_run_id=state.agent_run_id,
            alert_id=state.alert.alert_id,
            alert_key=state.alert.alert_key,
            hard_limit=10,
            require_date_filter=True,
            clickhouse_host=config.clickhouse_host,
            clickhouse_port=config.clickhouse_port,
        )
        row_count = int(result.rows[0].get("row_count", 0)) if result.rows else 0
        summary   = f"Current partition row count for {state.alert.table_name} on {state.alert.dt} is {row_count}."

        return sql_result_to_evidence(
            result=result,
            description="Current partition row count for the alert table/date.",
            summary=summary,
        )

    def collect_dq_history_for_state(state: TriageState) -> EvidenceItem:
        """
        Collect DQ history evidence for the loaded alert.

        Args:
            state: Current triage state.

        Returns:
            EvidenceItem with recent DQ check history.
        """
        if not state.alert:
            raise ValueError("Alert must be loaded before DQ history evidence.")

        return collect_dq_history_evidence(
            alert=state.alert,
            agent_run_id=state.agent_run_id,
            clickhouse_host=config.clickhouse_host,
            clickhouse_port=config.clickhouse_port,
        )

    def collect_pipeline_runs_for_state(state: TriageState) -> EvidenceItem:
        """
        Collect pipeline run evidence for the loaded alert.

        Args:
            state: Current triage state.

        Returns:
            EvidenceItem with recent pipeline run history.
        """
        if not state.alert:
            raise ValueError("Alert must be loaded before pipeline run evidence.")

        return collect_pipeline_runs_evidence(
            alert=state.alert,
            agent_run_id=state.agent_run_id,
            clickhouse_host=config.clickhouse_host,
            clickhouse_port=config.clickhouse_port,
        )

    def collect_lineage_evidence(state: TriageState) -> EvidenceItem:
        """
        Collect dbt lineage evidence for the affected table.

        Args:
            state: Current triage state.

        Returns:
            EvidenceItem with lineage rows.
        """
        if not state.alert:
            raise ValueError("Alert must be loaded before lineage evidence.")

        return collect_dbt_lineage_evidence(
            alert=state.alert,
            agent_run_id=state.agent_run_id,
            manifest_path=config.manifest_path,
            manifest_s3_uri=config.manifest_s3_uri,
            endpoint_url=config.s3_endpoint_url,
            clickhouse_host=config.clickhouse_host,
            clickhouse_port=config.clickhouse_port,
        )

    def generate_hypotheses_node(graph_state: GraphState) -> GraphState:
        """
        Generate hypotheses from the current alert and evidence.

        Args:
            graph_state: Current graph state.

        Returns:
            Updated graph state with ranked hypotheses.
        """
        state            = get_state(graph_state)
        state.hypotheses = build_hypotheses_for_state(state)

        logger.info("Generated hypotheses | agent_run_id=%s count=%d", state.agent_run_id, len(state.hypotheses))

        return {"state": state}

    def rank_hypotheses_node(graph_state: GraphState) -> GraphState:
        """
        Rank hypotheses by confidence.

        Args:
            graph_state: Current graph state.

        Returns:
            Updated graph state with hypotheses sorted descending.
        """
        state            = get_state(graph_state)
        state.hypotheses = sorted(state.hypotheses, key=lambda item: item.confidence, reverse=True)
        top              = state.top_hypothesis

        logger.info(
            "Ranked hypotheses | agent_run_id=%s top=%s confidence=%s",
            state.agent_run_id,
            top.title if top else "none",
            top.confidence if top else None,
        )

        return {"state": state}

    def collect_extra_evidence_node(graph_state: GraphState) -> GraphState:
        """
        Collect additional evidence when confidence is below threshold.

        Args:
            graph_state: Current graph state.

        Returns:
            Updated graph state with extra evidence appended.
        """
        state = get_state(graph_state)

        if not state.alert:
            raise ValueError("Alert must be loaded before extra evidence.")

        try:
            result = run_guarded_sql(
                sql=recent_partition_sql(state.alert),
                agent_run_id=state.agent_run_id,
                alert_id=state.alert.alert_id,
                alert_key=state.alert.alert_key,
                hard_limit=20,
                require_date_filter=True,
                clickhouse_host=config.clickhouse_host,
                clickhouse_port=config.clickhouse_port,
            )
            summary = f"Recent partition row counts collected for {state.alert.table_name} around {state.alert.dt}."
            state.add_evidence(
                sql_result_to_evidence(
                    result=result,
                    description="Recent partition row counts for bounded confidence improvement.",
                    summary=summary,
                )
            )

        except Exception as exc:
            append_error(state, f"extra_partition_evidence failed: {exc}")

        state.evidence_iterations += 1
        logger.info(
            "Extra evidence loop completed | agent_run_id=%s iteration=%d",
            state.agent_run_id,
            state.evidence_iterations,
        )

        return {"state": state}

    def finalize_report_node(graph_state: GraphState) -> GraphState:
        """
        Finalize the in-memory triage report.

        Args:
            graph_state: Current graph state.

        Returns:
            Updated graph state with report populated.
        """
        state        = get_state(graph_state)
        state.report = build_report_from_state(state)

        return {"state": state}

    def store_report_node(graph_state: GraphState) -> GraphState:
        """
        Store Markdown and JSON report artifacts to S3.

        Args:
            graph_state: Current graph state.

        Returns:
            Updated graph state with report S3 URIs populated.
        """
        state = get_state(graph_state)

        if not state.report:
            raise ValueError("Report must be finalized before storing artifacts.")

        storage_result = store_triage_report(
            report=state.report,
            bucket=config.artifacts_bucket,
            prefix=config.artifacts_prefix,
            endpoint_url=config.s3_endpoint_url,
            clickhouse_host=config.clickhouse_host,
            clickhouse_port=config.clickhouse_port,
        )
        state.add_evidence(
            EvidenceItem(
                evidence_type=EvidenceType.ARTIFACT,
                tool_name="s3_artifacts",
                description="Stored final Markdown and JSON triage report artifacts.",
                rows=[storage_result],
                row_count=2,
                summary=f"Stored report artifacts at {storage_result['markdown_report_s3_uri']}.",
                s3_uri=storage_result["markdown_report_s3_uri"],
            )
        )

        logger.info("Stored report node completed | agent_run_id=%s", state.agent_run_id)

        return {"state": state}

    def write_final_audit_node(graph_state: GraphState) -> GraphState:
        """
        Write the final triage completion audit event.

        Args:
            graph_state: Current graph state.

        Returns:
            Current graph state after audit logging.
        """
        state = get_state(graph_state)

        if not state.alert or not state.report:
            raise ValueError("Alert and report are required before final audit logging.")

        client       = build_clickhouse_client(host=config.clickhouse_host, port=config.clickhouse_port)
        started_at   = time.monotonic()
        duration_ms  = int((time.monotonic() - started_at) * 1000)

        write_agent_audit_event(
            client=client,
            action="triage_completed",
            status="success",
            agent_run_id=state.agent_run_id,
            alert_id=state.alert.alert_id,
            alert_key=state.alert.alert_key,
            tool_name=TOOL_NAME,
            duration_ms=duration_ms,
            input_payload={"alert_key": state.alert.alert_key},
            output_payload={
                "confidence": state.report.confidence,
                "hypothesis_count": len(state.hypotheses),
                "evidence_count": len(state.evidence),
                "errors": state.errors,
            },
            row_count=len(state.evidence),
            report_s3_uri=state.report.markdown_report_s3_uri,
        )

        logger.info("Triage completion audited | agent_run_id=%s", state.agent_run_id)

        return {"state": state}

    def route_after_rank(graph_state: GraphState) -> str:
        """
        Decide whether the workflow needs another evidence collection loop.

        Args:
            graph_state: Current graph state.

        Returns:
            Next route label.
        """
        state = get_state(graph_state)

        if state.should_collect_more_evidence:
            return "collect_extra_evidence"

        return "finalize_report"

    workflow = StateGraph(GraphState)

    workflow.add_node("load_alert", load_alert_node)
    workflow.add_node("gather_context", gather_context_node)
    workflow.add_node("generate_hypotheses", generate_hypotheses_node)
    workflow.add_node("rank_hypotheses", rank_hypotheses_node)
    workflow.add_node("collect_extra_evidence", collect_extra_evidence_node)
    workflow.add_node("finalize_report", finalize_report_node)
    workflow.add_node("store_report", store_report_node)
    workflow.add_node("write_final_audit", write_final_audit_node)

    workflow.set_entry_point("load_alert")
    workflow.add_edge("load_alert", "gather_context")
    workflow.add_edge("gather_context", "generate_hypotheses")
    workflow.add_edge("generate_hypotheses", "rank_hypotheses")
    workflow.add_conditional_edges(
        "rank_hypotheses",
        route_after_rank,
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

    return workflow.compile()


def run_triage(
    alert_id: str | UUID | None = None,
    alert_key: str | None = None,
    confidence_threshold: float = DEFAULT_CONFIDENCE_TARGET,
    max_evidence_iterations: int = DEFAULT_MAX_EVIDENCE_LOOP,
    config: TriageRuntimeConfig | None = None,
) -> TriageReport:
    """
    Run one bounded LangGraph triage workflow for an alert.

    Args:
        alert_id: Optional alert UUID.
        alert_key: Optional stable alert key.
        confidence_threshold: Confidence needed before skipping extra evidence.
        max_evidence_iterations: Maximum extra evidence loop count.
        config: Optional runtime config.

    Returns:
        Final triage report with S3 URIs populated.

    Raises:
        ValueError: If no alert identifier is provided or report generation fails.
    """
    if not alert_id and not alert_key:
        raise ValueError("Provide alert_id or alert_key to run triage.")

    runtime_config = config or TriageRuntimeConfig()
    state          = TriageState(
        agent_run_id=uuid4(),
        alert_id=UUID(str(alert_id)) if alert_id else None,
        alert_key=alert_key or "",
        confidence_threshold=confidence_threshold,
        max_evidence_iterations=max_evidence_iterations,
    )
    graph          = build_triage_graph(runtime_config)

    logger.info("Starting triage graph | agent_run_id=%s alert_key=%s", state.agent_run_id, alert_key)

    result      = graph.invoke({"state": state})
    final_state = result["state"]

    if not final_state.report:
        raise ValueError("Triage graph completed without a report.")

    logger.info(
        "Triage graph completed | agent_run_id=%s confidence=%.2f markdown_uri=%s",
        final_state.agent_run_id,
        final_state.report.confidence,
        final_state.report.markdown_report_s3_uri,
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
