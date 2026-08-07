####
## LIFE Evaluation History Tool for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import json
import re
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent.tools.clickhouse_sql import rows_to_dicts
from pipelines.common.clickhouse import build_clickhouse_client, quote_sql_literal
from pipelines.common.logging import logger


# --- Defining Constants
AGENT_AUDIT_LOG_TABLE       = "dq.agent_audit_log"
LIFE_EVALUATION_ACTION      = "life_evaluation_completed"
DEFAULT_HISTORY_LIMIT       = 25
MAX_HISTORY_LIMIT           = 100
DEFAULT_HISTORY_LOOKBACK    = 30
MAX_HISTORY_LOOKBACK        = 365
ALLOWED_EVALUATION_STATUSES = {"pass", "review", "fail"}
SCENARIO_ID_PATTERN         = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


# --- Defining Data Models
class LifeEvaluationHistoryRecord(BaseModel):
    """
    Represent one sanitized LIFE evaluation summary from the audit log.

    Attributes:
        audit_id: Stable audit event identifier.
        evaluated_at: Audit event timestamp in an API-safe string form.
        alert_id: Optional source alert UUID.
        alert_key: Stable source alert key.
        agent_run_id: Source triage run identifier stored by the evaluator.
        audit_status: Persistence status recorded by the audit event.
        report_s3_uri: Primary persisted LIFE JSON artifact URI.
        run_id: LIFE evaluation run identifier.
        scenario_id: Ground-truth incident scenario identifier.
        eval_status: Pass, review, fail, or unknown when the payload is malformed.
        failed_checks: Non-passing deterministic check identifiers.
        failure_category: Highest-priority failure category.
        failure_categories: Every unique non-passing failure category.
        life_stage: LIFE stage represented by the evaluation.
        suggested_change_type: Bounded improvement proposal type.
        suggested_change_summary: Human-readable improvement proposal.
        requires_human_approval: Whether implementation requires explicit review.
        summary: Human-readable evaluation summary.
        source_report_sha256: Digest binding the evaluation to its source report.
        created_at: Evaluation timestamp stored inside the LIFE artifact.
        json_report_s3_uri: Persisted LIFE JSON artifact URI.
        markdown_report_s3_uri: Persisted LIFE Markdown artifact URI.
        payload_valid: Whether output_json matched the expected object contract.
        payload_error: Sanitized parsing error category when payload_valid is false.
    """

    model_config = ConfigDict(extra="forbid")

    audit_id: str
    evaluated_at: str
    alert_id: str                          = ""
    alert_key: str                         = ""
    agent_run_id: str                      = ""
    audit_status: str                      = ""
    report_s3_uri: str                     = ""
    run_id: str                            = ""
    scenario_id: str                       = ""
    eval_status: Literal["pass", "review", "fail", "unknown"] = "unknown"
    failed_checks: list[str]               = Field(default_factory=list)
    failure_category: str                  = ""
    failure_categories: list[str]          = Field(default_factory=list)
    life_stage: str                        = ""
    suggested_change_type: str             = ""
    suggested_change_summary: str          = ""
    requires_human_approval: bool          = False
    summary: str                           = ""
    source_report_sha256: str              = ""
    created_at: str                        = ""
    json_report_s3_uri: str                = ""
    markdown_report_s3_uri: str            = ""
    payload_valid: bool                    = True
    payload_error: str                     = ""


class LifeEvaluationHistoryResult(BaseModel):
    """
    Store one bounded LIFE evaluation history response.

    Attributes:
        status: Read operation status.
        row_count: Number of sanitized records returned.
        lookback_days: Mandatory audit timestamp window applied to the query.
        eval_status_filter: Optional evaluation status filter.
        scenario_id_filter: Optional incident scenario filter.
        duration_ms: ClickHouse query and normalization duration.
        rows: Ordered LIFE evaluation summary records.
    """

    model_config = ConfigDict(extra="forbid")

    status: str                                      = "success"
    row_count: int                                   = 0
    lookback_days: int                               = DEFAULT_HISTORY_LOOKBACK
    eval_status_filter: str                          = ""
    scenario_id_filter: str                          = ""
    duration_ms: int                                 = 0
    rows: list[LifeEvaluationHistoryRecord]          = Field(default_factory=list)


# --- Defining Validation Helpers
def normalize_evaluation_status(value: str | None) -> str | None:
    """
    Normalize an optional LIFE evaluation status filter.

    Args:
        value: Optional pass, review, or fail status.

    Returns:
        Normalized lowercase status, or None when no filter was supplied.

    Raises:
        ValueError: If the status is outside the allowlist.
    """
    normalized = (value or "").strip().lower()

    if not normalized:
        return None

    if normalized not in ALLOWED_EVALUATION_STATUSES:
        allowed = ", ".join(sorted(ALLOWED_EVALUATION_STATUSES))
        raise ValueError(f"Unsupported LIFE evaluation status: {value}. Allowed values: {allowed}")

    return normalized


def normalize_scenario_id(value: str | None) -> str | None:
    """
    Normalize an optional incident scenario identifier.

    Args:
        value: Optional lowercase scenario identifier.

    Returns:
        Normalized scenario identifier, or None when no filter was supplied.

    Raises:
        ValueError: If the identifier is malformed or unsafe for filtering.
    """
    normalized = (value or "").strip().lower()

    if not normalized:
        return None

    if not SCENARIO_ID_PATTERN.fullmatch(normalized):
        raise ValueError(
            "LIFE scenario_id must use lowercase letters, numbers, underscores, or hyphens."
        )

    return normalized


def normalize_text_list(value: Any) -> list[str]:
    """
    Convert an untrusted JSON value into a bounded list of non-empty strings.

    Args:
        value: Candidate JSON list value.

    Returns:
        List containing at most 100 normalized strings.
    """
    if not isinstance(value, list):
        return []

    return [str(item).strip()[:500] for item in value[:100] if str(item).strip()]


def parse_life_output_json(value: Any) -> tuple[dict[str, Any], str]:
    """
    Parse one LIFE audit output payload without exposing its raw contents.

    Args:
        value: output_json value returned by ClickHouse.

    Returns:
        Tuple containing a parsed object and an empty error, or an empty object
        and a sanitized error category.
    """
    if isinstance(value, dict):
        return value, ""

    try:
        payload = json.loads(str(value or ""))

    except (TypeError, ValueError, json.JSONDecodeError):
        return {}, "invalid_json"

    if not isinstance(payload, dict):
        return {}, "payload_not_object"

    return payload, ""


# --- Defining Query Helpers
def build_life_history_sql(
    eval_status: str | None = None,
    scenario_id: str | None = None,
    lookback_days: int = DEFAULT_HISTORY_LOOKBACK,
    limit: int = DEFAULT_HISTORY_LIMIT,
) -> str:
    """
    Build a bounded read-only query for recent LIFE evaluation audit events.

    Args:
        eval_status: Optional pass, review, or fail filter.
        scenario_id: Optional incident scenario identifier.
        lookback_days: Mandatory recent timestamp window in days.
        limit: Maximum audit events to return.

    Returns:
        ClickHouse SELECT statement with a date filter and hard LIMIT.
    """
    normalized_status   = normalize_evaluation_status(eval_status)
    normalized_scenario = normalize_scenario_id(scenario_id)
    safe_lookback       = max(1, min(int(lookback_days), MAX_HISTORY_LOOKBACK))
    safe_limit          = max(1, min(int(limit), MAX_HISTORY_LIMIT))
    filters             = [
        f"action = {quote_sql_literal(LIFE_EVALUATION_ACTION)}",
        f"ts >= now() - INTERVAL {safe_lookback} DAY",
    ]

    if normalized_status:
        filters.append(
            "JSONExtractString(output_json, 'eval_status') = "
            f"{quote_sql_literal(normalized_status)}"
        )

    if normalized_scenario:
        filters.append(
            "JSONExtractString(output_json, 'scenario_id') = "
            f"{quote_sql_literal(normalized_scenario)}"
        )

    where_clause = "\n          AND ".join(filters)

    return f"""
        SELECT
            audit_id,
            ts AS evaluated_at,
            alert_id,
            alert_key,
            agent_run_id,
            status AS audit_status,
            report_s3_uri,
            output_json
        FROM {AGENT_AUDIT_LOG_TABLE}
        WHERE {where_clause}
        ORDER BY ts DESC
        LIMIT {safe_limit}
    """


def life_history_record_from_row(row: dict[str, Any]) -> LifeEvaluationHistoryRecord:
    """
    Convert one audit row into a sanitized LIFE history record.

    Args:
        row: ClickHouse audit row containing output_json.

    Returns:
        Validated LIFE history record without raw audit payloads.
    """
    payload, payload_error = parse_life_output_json(row.get("output_json"))
    raw_eval_status        = str(payload.get("eval_status") or "").strip().lower()
    eval_status            = (
        raw_eval_status
        if raw_eval_status in ALLOWED_EVALUATION_STATUSES
        else "unknown"
    )

    if raw_eval_status and eval_status == "unknown" and not payload_error:
        payload_error = "invalid_eval_status"

    return LifeEvaluationHistoryRecord(
        audit_id=str(row.get("audit_id") or ""),
        evaluated_at=str(row.get("evaluated_at") or ""),
        alert_id=str(row.get("alert_id") or ""),
        alert_key=str(row.get("alert_key") or ""),
        agent_run_id=str(row.get("agent_run_id") or ""),
        audit_status=str(row.get("audit_status") or ""),
        report_s3_uri=str(row.get("report_s3_uri") or ""),
        run_id=str(payload.get("run_id") or ""),
        scenario_id=str(payload.get("scenario_id") or ""),
        eval_status=eval_status,
        failed_checks=normalize_text_list(payload.get("failed_checks")),
        failure_category=str(payload.get("failure_category") or "")[:200],
        failure_categories=normalize_text_list(payload.get("failure_categories")),
        life_stage=str(payload.get("life_stage") or "")[:200],
        suggested_change_type=str(payload.get("suggested_change_type") or "")[:200],
        suggested_change_summary=str(payload.get("suggested_change_summary") or "")[:2_000],
        requires_human_approval=payload.get("requires_human_approval") is True,
        summary=str(payload.get("summary") or "")[:2_000],
        source_report_sha256=str(payload.get("source_report_sha256") or "")[:64],
        created_at=str(payload.get("created_at") or ""),
        json_report_s3_uri=str(payload.get("json_report_s3_uri") or row.get("report_s3_uri") or ""),
        markdown_report_s3_uri=str(payload.get("markdown_report_s3_uri") or ""),
        payload_valid=not bool(payload_error),
        payload_error=payload_error,
    )


# --- Defining Public Tool
def list_life_evaluation_history(
    client: Any | None = None,
    eval_status: str | None = None,
    scenario_id: str | None = None,
    lookback_days: int = DEFAULT_HISTORY_LOOKBACK,
    limit: int = DEFAULT_HISTORY_LIMIT,
) -> LifeEvaluationHistoryResult:
    """
    Read recent LIFE evaluation summaries from ClickHouse audit history.

    Args:
        client: Optional ClickHouse client override.
        eval_status: Optional pass, review, or fail filter.
        scenario_id: Optional incident scenario identifier.
        lookback_days: Mandatory recent timestamp window in days.
        limit: Maximum records to return.

    Returns:
        Bounded and sanitized LIFE evaluation history result.
    """
    normalized_status   = normalize_evaluation_status(eval_status)
    normalized_scenario = normalize_scenario_id(scenario_id)
    safe_lookback       = max(1, min(int(lookback_days), MAX_HISTORY_LOOKBACK))
    safe_limit          = max(1, min(int(limit), MAX_HISTORY_LIMIT))
    sql                 = build_life_history_sql(
        eval_status=normalized_status,
        scenario_id=normalized_scenario,
        lookback_days=safe_lookback,
        limit=safe_limit,
    )
    clickhouse_client   = client or build_clickhouse_client()
    started_at          = time.monotonic()

    logger.info(
        "Reading LIFE evaluation history | eval_status=%s scenario_id=%s lookback_days=%d limit=%d",
        normalized_status or "all",
        normalized_scenario or "all",
        safe_lookback,
        safe_limit,
    )

    query_result = clickhouse_client.query(sql)
    raw_rows     = rows_to_dicts(
        columns=list(query_result.column_names or []),
        rows=list(query_result.result_rows or []),
    )
    records      = [life_history_record_from_row(row) for row in raw_rows]
    duration_ms  = int((time.monotonic() - started_at) * 1000)

    logger.info(
        "LIFE evaluation history loaded | rows=%d malformed=%d duration_ms=%d",
        len(records),
        sum(not record.payload_valid for record in records),
        duration_ms,
    )

    return LifeEvaluationHistoryResult(
        row_count=len(records),
        lookback_days=safe_lookback,
        eval_status_filter=normalized_status or "",
        scenario_id_filter=normalized_scenario or "",
        duration_ms=duration_ms,
        rows=records,
    )
