####
## Supervisor Mode Comparison for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Compare single-handoff and fan-out outcomes without overstating agent quality."""

# --- Importing Libraries
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agent.evaluation.life import (
    LifeEvaluationReport,
    build_life_artifact_keys,
    evaluate_life_report,
    normalize_evaluation_run_id,
)
from agent.evaluation.triage import (
    load_json_report,
    load_yaml_file,
    resolve_scenario_path,
    validate_scenario_config,
)
from agent.tools.audit_log import (
    build_audit_idempotency_key,
    write_agent_audit_event,
)
from agent.tools.s3 import (
    put_json_artifact,
    put_text_artifact,
    resolve_artifacts_bucket,
)
from pipelines.common.clickhouse import build_clickhouse_client
from pipelines.common.logging import logger


# --- Defining Constants
DEFAULT_COMPARISON_ARTIFACT_PREFIX = "agent-life-comparisons"
SUPERVISOR_AUDIT_TOOL              = "control_plane_supervisor"
SUPERVISOR_COMPARISON_ACTION       = "life_supervisor_comparison_completed"

COMPARISON_DECISIONS = (
    "keep_single",
    "fanout_candidate",
    "insufficient_evidence",
)

EVALUATION_STATUS_RANK = {
    "fail": 0,
    "review": 1,
    "pass": 2,
}

CHECK_STATUS_RANK = {
    "fail": 0,
    "review": 1,
    "pass": 2,
}


# --- Defining Models
class SupervisorModeSnapshot(BaseModel):
    """
    Store the bounded audit evidence for one supervisor execution mode.

    Attributes:
        parent_run_id: Stable supervisor correlation UUID.
        execution_mode: Single-handoff or fan-out execution mode.
        requested_intent: Intent retained by the supervisor audit.
        alert_key: Stable alert identity shared by both comparison arms.
        qualified_name: Optional exact warehouse asset supplied to the run.
        terminal_status: Parent supervisor terminal status.
        worker_count: Number of completed or attempted specialist handoffs.
        completed_worker_count: Successful or partial worker outcomes.
        optional_failure_count: Optional worker failures retained by fan-in.
        required_failure_count: Required worker failures that block conclusions.
        max_concurrency: Policy concurrency ceiling for the run.
        evidence_reference_count: Sum of retained worker evidence references.
        triage_confidence: Confidence reported by the incident triage worker.
        model_call_count: Aggregate external provider calls.
        token_usage: Aggregate model token usage.
        estimated_cost_usd: Aggregate estimated provider cost.
        duration_ms: Parent execution duration from the final audit event.
        report_s3_uri: JSON triage report URI used for deterministic evaluation.
    """

    model_config = ConfigDict(extra="forbid")

    parent_run_id: UUID
    execution_mode: Literal["single", "fanout"]
    requested_intent: str                 = ""
    alert_key: str                        = ""
    qualified_name: str                   = ""
    terminal_status: str
    worker_count: int                     = Field(ge=0, le=10)
    completed_worker_count: int           = Field(ge=0, le=10)
    optional_failure_count: int           = Field(ge=0, le=10)
    required_failure_count: int           = Field(ge=0, le=10)
    max_concurrency: int                  = Field(ge=1, le=3)
    evidence_reference_count: int         = Field(ge=0, le=1_000)
    triage_confidence: float              = Field(ge=0.0, le=1.0)
    model_call_count: int                 = Field(ge=0, le=10)
    token_usage: int                      = Field(ge=0, le=64_000)
    estimated_cost_usd: float             = Field(ge=0.0, le=0.15)
    duration_ms: int                      = Field(ge=0, le=900_000)
    report_s3_uri: str                    = ""


class SupervisorModeScore(BaseModel):
    """
    Store deterministic LIFE score evidence for one execution mode.

    Attributes:
        eval_status: LIFE pass, review, or fail status.
        passed_check_count: Number of deterministic checks that passed.
        total_check_count: Total deterministic checks executed.
        failed_checks: Non-passing deterministic check names.
        failure_categories: Reliability failure categories found.
        expected_evidence_status: Status of the scenario evidence coverage check.
        check_statuses: Status of every named deterministic LIFE check.
        confidence: Confidence from the source triage report.
        source_report_sha256: Hash proving which report payload was evaluated.
    """

    model_config = ConfigDict(extra="forbid")

    eval_status: Literal["pass", "review", "fail"]
    passed_check_count: int                = Field(ge=0)
    total_check_count: int                 = Field(ge=0)
    failed_checks: list[str]               = Field(default_factory=list)
    failure_categories: list[str]          = Field(default_factory=list)
    expected_evidence_status: Literal["pass", "review", "fail", "unavailable"]
    check_statuses: dict[str, Literal["pass", "review", "fail"]] = Field(default_factory=dict)
    confidence: float                      = Field(ge=0.0, le=1.0)
    source_report_sha256: str              = Field(min_length=64, max_length=64)


class SupervisorModeComparisonReport(BaseModel):
    """
    Persist one auditable single-versus-fan-out reliability comparison.

    Attributes:
        comparison_run_id: Stable Airflow and artifact correlation identifier.
        scenario_id: Ground-truth scenario used for both report evaluations.
        alert_key: Shared alert identity.
        single: Single-handoff audit snapshot.
        fanout: Fan-out audit snapshot.
        single_score: LIFE score for the single-handoff report when available.
        fanout_score: LIFE score for the fan-out triage report when available.
        quality_delta: Fan-out minus single deterministic check-pass ratio.
        confidence_delta: Fan-out minus single report confidence.
        evidence_reference_delta: Fan-out minus single retained evidence references.
        latency_delta_ms: Fan-out minus single parent duration.
        estimated_cost_delta_usd: Fan-out minus single estimated provider cost.
        decision: Keep single, review fan-out as a candidate, or reject comparison evidence.
        measurable_benefit: Whether a report-quality gate improved measurably.
        decision_reasons: Explainable deterministic decision facts.
        requires_human_review: Always true before any runtime default can change.
        summary: Operator-facing result.
        markdown_report: Persisted Markdown representation.
        json_report_s3_uri: Comparison JSON artifact URI.
        markdown_report_s3_uri: Comparison Markdown artifact URI.
        created_at: UTC comparison timestamp.
    """

    model_config = ConfigDict(extra="forbid")

    comparison_run_id: str
    scenario_id: str
    alert_key: str
    single: SupervisorModeSnapshot
    fanout: SupervisorModeSnapshot
    single_score: SupervisorModeScore | None = None
    fanout_score: SupervisorModeScore | None = None
    quality_delta: float                    = 0.0
    confidence_delta: float                 = 0.0
    evidence_reference_delta: int           = 0
    latency_delta_ms: int                   = 0
    estimated_cost_delta_usd: float         = 0.0
    decision: Literal[
        "keep_single",
        "fanout_candidate",
        "insufficient_evidence",
    ]
    measurable_benefit: bool                = False
    decision_reasons: list[str]             = Field(default_factory=list)
    requires_human_review: bool             = True
    summary: str
    markdown_report: str                    = ""
    json_report_s3_uri: str                 = ""
    markdown_report_s3_uri: str             = ""
    created_at: datetime                    = Field(default_factory=lambda: datetime.now(timezone.utc))


# --- Defining Parsing Helpers
def parse_json_object(value: str, field_name: str) -> dict[str, Any]:
    """
    Parse one ClickHouse JSON audit field into an object.

    Args:
        value: Serialized JSON text.
        field_name: Field label used in validation errors.

    Returns:
        Parsed dictionary.

    Raises:
        ValueError: If the JSON is malformed or is not an object.
    """
    try:
        payload = json.loads(value or "{}")

    except json.JSONDecodeError as exc:
        raise ValueError(f"Supervisor {field_name} contains malformed JSON.") from exc

    if not isinstance(payload, dict):
        raise ValueError(f"Supervisor {field_name} must contain a JSON object.")

    return payload


def report_json_uri(report_s3_uri: str) -> str:
    """
    Normalize a supervisor Markdown or JSON report reference to report.json.

    Args:
        report_s3_uri: Triage report artifact URI retained in the audit log.

    Returns:
        JSON report URI, or an empty string when no report exists.

    Raises:
        ValueError: If the retained URI does not identify a report artifact.
    """
    normalized = report_s3_uri.strip()

    if not normalized:
        return ""

    if normalized.endswith("/report.md"):
        return normalized.removesuffix("/report.md") + "/report.json"

    if normalized.endswith("/report.json"):
        return normalized

    raise ValueError("Supervisor comparison accepts only report.md or report.json artifacts.")


def audit_rows_for_parent(client: Any, parent_run_id: UUID) -> list[dict[str, Any]]:
    """
    Load bounded supervisor lifecycle rows for one parent run.

    Args:
        client: ClickHouse client.
        parent_run_id: Stable supervisor parent UUID.

    Returns:
        Ordered audit row dictionaries.
    """
    result = client.query(
        """
            SELECT
                ts,
                action,
                status,
                ifNull(duration_ms, 0),
                input_json,
                output_json,
                report_s3_uri,
                alert_key
            FROM dq.agent_audit_log
            WHERE agent_run_id = {parent_run_id:UUID}
              AND tool_name = {tool_name:String}
              AND action IN (
                  'supervisor_handoff_completed',
                  'supervisor_aggregation_completed',
                  'supervisor_final_decision'
              )
            ORDER BY ts ASC
            LIMIT 100
        """,
        parameters={
            "parent_run_id": str(parent_run_id),
            "tool_name": SUPERVISOR_AUDIT_TOOL,
        },
    )

    return [
        {
            "ts": row[0],
            "action": str(row[1]),
            "status": str(row[2]),
            "duration_ms": int(row[3] or 0),
            "input": parse_json_object(str(row[4]), "input_json"),
            "output": parse_json_object(str(row[5]), "output_json"),
            "report_s3_uri": str(row[6] or ""),
            "alert_key": str(row[7] or ""),
        }
        for row in result.result_rows
    ]


def build_supervisor_mode_snapshot(
    parent_run_id: UUID | str,
    clickhouse_client: Any | None = None,
) -> SupervisorModeSnapshot:
    """
    Build one comparison snapshot from retained supervisor audit rows.

    Args:
        parent_run_id: Stable supervisor parent UUID.
        clickhouse_client: Optional injected ClickHouse client.

    Returns:
        Typed supervisor mode snapshot.

    Raises:
        ValueError: If required terminal or mode evidence is missing.
    """
    normalized_parent = UUID(str(parent_run_id))
    client            = clickhouse_client or build_clickhouse_client()
    rows              = audit_rows_for_parent(client, normalized_parent)
    final_rows        = [row for row in rows if row["action"] == "supervisor_final_decision"]
    handoff_rows      = [row for row in rows if row["action"] == "supervisor_handoff_completed"]
    aggregation_rows  = [row for row in rows if row["action"] == "supervisor_aggregation_completed"]

    if not final_rows:
        raise ValueError(f"Supervisor final decision is missing for parent run {normalized_parent}.")

    final_row    = final_rows[-1]
    final_input  = final_row["input"]
    final_output = final_row["output"]
    mode_value   = str(final_input.get("execution_mode") or "").strip().lower()

    if not mode_value:
        mode_value = "fanout" if aggregation_rows else "single"

    if mode_value not in {"single", "fanout"}:
        raise ValueError(f"Unsupported supervisor execution mode in audit: {mode_value}")

    triage_rows = [
        row
        for row in handoff_rows
        if str(row["output"].get("selected_specialist") or "") == "incident_triage_agent"
        and row["report_s3_uri"]
    ]
    triage_row    = triage_rows[-1] if triage_rows else None
    aggregation   = aggregation_rows[-1]["output"].get("resilience", {}) if aggregation_rows else {}
    final_metrics = final_output.get("resilience", {}) if mode_value == "fanout" else final_output

    if not isinstance(aggregation, dict):
        aggregation = {}

    if not isinstance(final_metrics, dict):
        final_metrics = {}

    completed_count = int(aggregation.get("completed_count", 0) or 0)
    optional_failed = int(aggregation.get("optional_failure_count", 0) or 0)
    required_failed = int(aggregation.get("required_failure_count", 0) or 0)
    worker_count     = int(
        final_metrics.get("worker_count")
        or len(handoff_rows)
        or (1 if mode_value == "single" else 0)
    )

    if mode_value == "single":
        completed_count = 1 if handoff_rows and final_row["status"] in {"success", "partial"} else 0

    snapshot = SupervisorModeSnapshot(
        parent_run_id=normalized_parent,
        execution_mode=mode_value,
        requested_intent=str(final_input.get("requested_intent") or ""),
        alert_key=final_row["alert_key"],
        qualified_name=str(final_input.get("qualified_name") or ""),
        terminal_status=final_row["status"],
        worker_count=worker_count,
        completed_worker_count=completed_count,
        optional_failure_count=optional_failed,
        required_failure_count=required_failed,
        max_concurrency=int(final_input.get("max_concurrency", 1) or 1),
        evidence_reference_count=sum(
            int(row["output"].get("evidence_reference_count", 0) or 0)
            for row in handoff_rows
        ),
        triage_confidence=float(
            (triage_row or {}).get("output", {}).get("confidence", 0.0) or 0.0
        ),
        model_call_count=int(final_metrics.get("model_call_count", 0) or 0),
        token_usage=int(final_metrics.get("token_usage", 0) or 0),
        estimated_cost_usd=float(final_metrics.get("estimated_cost_usd", 0.0) or 0.0),
        duration_ms=final_row["duration_ms"],
        report_s3_uri=report_json_uri(
            str((triage_row or {}).get("report_s3_uri") or "")
        ),
    )

    logger.info(
        "Built supervisor comparison snapshot | parent_run_id=%s mode=%s status=%s workers=%d evidence=%d report=%s",
        snapshot.parent_run_id,
        snapshot.execution_mode,
        snapshot.terminal_status,
        snapshot.worker_count,
        snapshot.evidence_reference_count,
        bool(snapshot.report_s3_uri),
    )

    return snapshot


# --- Defining Evaluation Helpers
def mode_score(
    evaluation: LifeEvaluationReport,
    source_report: dict[str, Any],
) -> SupervisorModeScore:
    """
    Convert a LIFE evaluation into compact comparable mode evidence.

    Args:
        evaluation: Deterministic LIFE report evaluation.
        source_report: Source triage report used to extract confidence.

    Returns:
        Typed comparison score.
    """
    expected_check = next(
        (check for check in evaluation.checks if check.name == "expected_evidence"),
        None,
    )
    passed_count = sum(check.status == "pass" for check in evaluation.checks)

    return SupervisorModeScore(
        eval_status=evaluation.eval_status,
        passed_check_count=passed_count,
        total_check_count=len(evaluation.checks),
        failed_checks=evaluation.failed_checks,
        failure_categories=evaluation.failure_categories,
        expected_evidence_status=(expected_check.status if expected_check else "unavailable"),
        check_statuses={check.name: check.status for check in evaluation.checks},
        confidence=float(source_report.get("confidence", 0.0) or 0.0),
        source_report_sha256=evaluation.source_report_sha256,
    )


def check_regressions(single: SupervisorModeScore, fanout: SupervisorModeScore) -> list[str]:
    """
    Find deterministic check outcomes that became worse under fan-out.

    Args:
        single: Single-handoff LIFE score.
        fanout: Fan-out LIFE score.

    Returns:
        Names of checks whose status rank regressed.
    """
    regressions = []

    for check_name, fanout_status in fanout.check_statuses.items():
        single_status = single.check_statuses.get(check_name)

        if single_status is None:
            continue

        if CHECK_STATUS_RANK[fanout_status] < CHECK_STATUS_RANK[single_status]:
            regressions.append(check_name)

    return sorted(regressions)


def quality_ratio(score: SupervisorModeScore) -> float:
    """
    Calculate the deterministic LIFE check-pass ratio.

    Args:
        score: Mode score.

    Returns:
        Pass ratio between zero and one.
    """
    if score.total_check_count == 0:
        return 0.0

    return score.passed_check_count / score.total_check_count


def decide_comparison(
    single_snapshot: SupervisorModeSnapshot,
    fanout_snapshot: SupervisorModeSnapshot,
    single_score: SupervisorModeScore | None,
    fanout_score: SupervisorModeScore | None,
) -> tuple[str, bool, list[str]]:
    """
    Apply the conservative fan-out promotion policy.

    Args:
        single_snapshot: Single-handoff audit evidence.
        fanout_snapshot: Fan-out audit evidence.
        single_score: LIFE report score for single mode, if available.
        fanout_score: LIFE report score for fan-out mode, if available.

    Returns:
        Decision, measurable-benefit flag, and explainable reasons.
    """
    reasons: list[str] = []

    if single_score is None or fanout_score is None:
        return (
            "insufficient_evidence",
            False,
            ["Both modes must retain a triage report before report quality can be compared."],
        )

    if fanout_snapshot.terminal_status in {"blocked", "failed"}:
        return (
            "keep_single",
            False,
            [f"Fan-out terminal status is {fanout_snapshot.terminal_status}."],
        )

    if fanout_snapshot.required_failure_count > 0:
        return (
            "keep_single",
            False,
            ["Fan-out contains at least one required worker failure."],
        )

    regressions = check_regressions(single_score, fanout_score)

    if regressions:
        return (
            "keep_single",
            False,
            [f"Fan-out introduced non-passing checks: {', '.join(regressions)}."],
        )

    single_rank = EVALUATION_STATUS_RANK[single_score.eval_status]
    fanout_rank = EVALUATION_STATUS_RANK[fanout_score.eval_status]

    if fanout_rank > single_rank:
        reasons.append(
            f"LIFE status improved from {single_score.eval_status} to {fanout_score.eval_status}."
        )

    if len(fanout_score.failed_checks) < len(single_score.failed_checks):
        reasons.append("Fan-out reduced the number of non-passing LIFE checks.")

    single_evidence_rank = CHECK_STATUS_RANK.get(single_score.expected_evidence_status, -1)
    fanout_evidence_rank = CHECK_STATUS_RANK.get(fanout_score.expected_evidence_status, -1)

    if fanout_evidence_rank > single_evidence_rank:
        reasons.append("The triage report evidence-coverage check improved under fan-out.")

    confidence_delta = fanout_score.confidence - single_score.confidence

    if confidence_delta >= 0.05:
        reasons.append(f"Report confidence improved by {confidence_delta:.2f}.")

    report_quality_benefit = bool(reasons)

    if report_quality_benefit:
        return "fanout_candidate", True, reasons

    if fanout_snapshot.evidence_reference_count > single_snapshot.evidence_reference_count:
        reasons.append(
            "Fan-out retained more aggregate references, but they did not improve the evaluated triage report."
        )

    reasons.append("No measurable triage-report quality improvement was proven; ties keep single mode.")

    return "keep_single", False, reasons


def validate_comparison_identity(
    single_snapshot: SupervisorModeSnapshot,
    fanout_snapshot: SupervisorModeSnapshot,
) -> None:
    """
    Require both comparison arms to represent the same bounded incident.

    Args:
        single_snapshot: Single-handoff audit snapshot.
        fanout_snapshot: Fan-out audit snapshot.

    Returns:
        None.

    Raises:
        ValueError: If modes, intent, alert, or explicit asset context differ.
    """
    if single_snapshot.execution_mode != "single":
        raise ValueError("The single comparison parent must use execution_mode=single.")

    if fanout_snapshot.execution_mode != "fanout":
        raise ValueError("The fan-out comparison parent must use execution_mode=fanout.")

    if not single_snapshot.alert_key or single_snapshot.alert_key != fanout_snapshot.alert_key:
        raise ValueError("Comparison parents must carry the same non-empty alert key.")

    if single_snapshot.requested_intent != fanout_snapshot.requested_intent:
        raise ValueError("Comparison parents must carry the same requested intent.")

    if single_snapshot.requested_intent != "triage_alert":
        raise ValueError("Supervisor quality comparison currently supports triage_alert only.")

    if (
        single_snapshot.qualified_name
        and fanout_snapshot.qualified_name
        and single_snapshot.qualified_name != fanout_snapshot.qualified_name
    ):
        raise ValueError("Comparison parents contain different explicit warehouse assets.")


def build_supervisor_mode_comparison(
    scenario_id: str,
    single_parent_run_id: UUID | str,
    fanout_parent_run_id: UUID | str,
    comparison_run_id: str,
    clickhouse_client: Any | None = None,
    endpoint_url: str | None = None,
) -> tuple[SupervisorModeComparisonReport, dict[str, Any], dict[str, Any]]:
    """
    Build one same-alert, same-ground-truth supervisor mode comparison.

    Args:
        scenario_id: Allowlisted incident ground-truth scenario.
        single_parent_run_id: Parent UUID for the single-handoff run.
        fanout_parent_run_id: Parent UUID for the fan-out run.
        comparison_run_id: Stable Airflow and artifact correlation identifier.
        clickhouse_client: Optional injected ClickHouse client.
        endpoint_url: Optional S3-compatible endpoint override.

    Returns:
        Comparison report plus both source report payloads.
    """
    run_id       = normalize_evaluation_run_id(comparison_run_id)
    client       = clickhouse_client or build_clickhouse_client()
    scenario_path = resolve_scenario_path(scenario_id)
    scenario      = load_yaml_file(scenario_path)
    validate_scenario_config(path=scenario_path, scenario=scenario)

    if str(scenario.get("scenario_id") or "") != scenario_id:
        raise ValueError("Scenario id does not match the selected ground-truth file.")

    single_snapshot = build_supervisor_mode_snapshot(single_parent_run_id, client)
    fanout_snapshot = build_supervisor_mode_snapshot(fanout_parent_run_id, client)
    validate_comparison_identity(single_snapshot, fanout_snapshot)

    source_reports: dict[str, dict[str, Any]] = {"single": {}, "fanout": {}}
    scores: dict[str, SupervisorModeScore | None] = {"single": None, "fanout": None}

    for label, snapshot in (("single", single_snapshot), ("fanout", fanout_snapshot)):
        if not snapshot.report_s3_uri:
            continue

        source_report = load_json_report(report_s3_uri=snapshot.report_s3_uri)
        evaluation = evaluate_life_report(
            scenario=scenario,
            report=source_report,
            report_s3_uri=snapshot.report_s3_uri,
            evaluation_run_id=f"{run_id}-{label}",
        )
        source_reports[label] = source_report
        scores[label]         = mode_score(evaluation, source_report)

    single_score = scores["single"]
    fanout_score = scores["fanout"]
    decision, measurable_benefit, reasons = decide_comparison(
        single_snapshot=single_snapshot,
        fanout_snapshot=fanout_snapshot,
        single_score=single_score,
        fanout_score=fanout_score,
    )
    quality_delta = (
        quality_ratio(fanout_score) - quality_ratio(single_score)
        if single_score and fanout_score
        else 0.0
    )
    confidence_delta = (
        fanout_score.confidence - single_score.confidence
        if single_score and fanout_score
        else 0.0
    )
    summary = (
        f"Supervisor comparison decision is {decision}. "
        f"Fan-out quality delta={quality_delta:.3f}, confidence delta={confidence_delta:.3f}, "
        f"and evidence-reference delta="
        f"{fanout_snapshot.evidence_reference_count - single_snapshot.evidence_reference_count}."
    )
    report = SupervisorModeComparisonReport(
        comparison_run_id=run_id,
        scenario_id=scenario_id,
        alert_key=single_snapshot.alert_key,
        single=single_snapshot,
        fanout=fanout_snapshot,
        single_score=single_score,
        fanout_score=fanout_score,
        quality_delta=quality_delta,
        confidence_delta=confidence_delta,
        evidence_reference_delta=(
            fanout_snapshot.evidence_reference_count
            - single_snapshot.evidence_reference_count
        ),
        latency_delta_ms=fanout_snapshot.duration_ms - single_snapshot.duration_ms,
        estimated_cost_delta_usd=(
            fanout_snapshot.estimated_cost_usd
            - single_snapshot.estimated_cost_usd
        ),
        decision=decision,
        measurable_benefit=measurable_benefit,
        decision_reasons=reasons,
        requires_human_review=True,
        summary=summary,
    )
    report.markdown_report = render_supervisor_mode_comparison(report)

    logger.info(
        "Built supervisor mode comparison | run_id=%s alert_key=%s decision=%s quality_delta=%.3f evidence_delta=%d",
        report.comparison_run_id,
        report.alert_key,
        report.decision,
        report.quality_delta,
        report.evidence_reference_delta,
    )

    return report, source_reports["single"], source_reports["fanout"]


# --- Defining Artifact Helpers
def build_comparison_artifact_keys(
    comparison_run_id: str,
    prefix: str = DEFAULT_COMPARISON_ARTIFACT_PREFIX,
) -> tuple[str, str]:
    """
    Build deterministic comparison JSON and Markdown artifact keys.

    Args:
        comparison_run_id: Stable comparison correlation identifier.
        prefix: Path-safe artifact prefix.

    Returns:
        JSON and Markdown object keys.
    """
    life_json_key, _ = build_life_artifact_keys(comparison_run_id, prefix=prefix)
    base_key         = life_json_key.removesuffix("/life_report.json")

    return f"{base_key}/comparison.json", f"{base_key}/comparison.md"


def render_supervisor_mode_comparison(report: SupervisorModeComparisonReport) -> str:
    """
    Render one operator-friendly supervisor mode comparison.

    Args:
        report: Typed comparison report.

    Returns:
        Markdown artifact text.
    """
    lines = [
        "# Single vs Fan-Out Reliability Comparison",
        "",
        f"Comparison Run: `{report.comparison_run_id}`",
        f"Scenario: `{report.scenario_id}`",
        f"Alert: `{report.alert_key}`",
        f"Decision: `{report.decision.upper()}`",
        "",
        "## Quick Read",
        report.summary,
        "",
        "## Measured Deltas",
        f"- LIFE quality ratio: `{report.quality_delta:+.3f}`",
        f"- Report confidence: `{report.confidence_delta:+.3f}`",
        f"- Retained evidence references: `{report.evidence_reference_delta:+d}`",
        f"- Parent latency: `{report.latency_delta_ms:+d} ms`",
        f"- Estimated provider cost: `${report.estimated_cost_delta_usd:+.6f}`",
        "",
        "## Decision Evidence",
    ]

    lines.extend(f"- {reason}" for reason in report.decision_reasons)
    lines.extend(
        [
            "",
            "## Safety Boundary",
            "This comparison cannot enable fan-out or change routing policy automatically.",
            "A human must review a fanout_candidate before any separate configuration change.",
            "",
            "## Source Runs",
            f"- Single parent: `{report.single.parent_run_id}`",
            f"- Fan-out parent: `{report.fanout.parent_run_id}`",
        ]
    )

    return "\n".join(lines) + "\n"


def persist_supervisor_mode_comparison(
    report: SupervisorModeComparisonReport,
    bucket: str | None = None,
    prefix: str = DEFAULT_COMPARISON_ARTIFACT_PREFIX,
    endpoint_url: str | None = None,
    clickhouse_client: Any | None = None,
) -> SupervisorModeComparisonReport:
    """
    Persist comparison artifacts and one replay-safe audit event.

    Args:
        report: Typed comparison result.
        bucket: Optional artifacts bucket override.
        prefix: Path-safe comparison artifact prefix.
        endpoint_url: Optional S3-compatible endpoint override.
        clickhouse_client: Optional injected ClickHouse client.

    Returns:
        Report containing persisted artifact URIs.
    """
    resolved_bucket        = resolve_artifacts_bucket(bucket)
    json_key, markdown_key = build_comparison_artifact_keys(
        comparison_run_id=report.comparison_run_id,
        prefix=prefix,
    )
    persisted = report.model_copy(
        update={
            "json_report_s3_uri": f"s3://{resolved_bucket}/{json_key}",
            "markdown_report_s3_uri": f"s3://{resolved_bucket}/{markdown_key}",
        }
    )
    persisted.markdown_report = render_supervisor_mode_comparison(persisted)

    put_text_artifact(
        bucket=resolved_bucket,
        key=markdown_key,
        text=persisted.markdown_report,
        content_type="text/markdown; charset=utf-8",
        endpoint_url=endpoint_url,
    )
    put_json_artifact(
        bucket=resolved_bucket,
        key=json_key,
        payload=persisted.model_dump(mode="json"),
        endpoint_url=endpoint_url,
    )

    client          = clickhouse_client or build_clickhouse_client()
    idempotency_key = build_audit_idempotency_key(
        SUPERVISOR_COMPARISON_ACTION,
        persisted.comparison_run_id,
        persisted.single.parent_run_id,
        persisted.fanout.parent_run_id,
        persisted.scenario_id,
    )
    write_agent_audit_event(
        client=client,
        action=SUPERVISOR_COMPARISON_ACTION,
        status="success",
        agent_run_id=persisted.fanout.parent_run_id,
        alert_key=persisted.alert_key,
        actor="life_evaluator",
        tool_name="life_supervisor_comparison",
        input_payload={
            "comparison_run_id": persisted.comparison_run_id,
            "scenario_id": persisted.scenario_id,
            "single_parent_run_id": str(persisted.single.parent_run_id),
            "fanout_parent_run_id": str(persisted.fanout.parent_run_id),
        },
        output_payload={
            "comparison_run_id": persisted.comparison_run_id,
            "decision": persisted.decision,
            "measurable_benefit": persisted.measurable_benefit,
            "quality_delta": persisted.quality_delta,
            "confidence_delta": persisted.confidence_delta,
            "evidence_reference_delta": persisted.evidence_reference_delta,
            "latency_delta_ms": persisted.latency_delta_ms,
            "estimated_cost_delta_usd": persisted.estimated_cost_delta_usd,
            "requires_human_review": persisted.requires_human_review,
            "json_report_s3_uri": persisted.json_report_s3_uri,
            "markdown_report_s3_uri": persisted.markdown_report_s3_uri,
        },
        row_count=2,
        report_s3_uri=persisted.json_report_s3_uri,
        idempotency_key=idempotency_key,
    )

    logger.info(
        "Persisted supervisor mode comparison | run_id=%s decision=%s json_uri=%s",
        persisted.comparison_run_id,
        persisted.decision,
        persisted.json_report_s3_uri,
    )

    return persisted
