####
## Table Health Tool for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Build one bounded, audited warehouse table-health view from deterministic evidence."""

# --- Importing Libraries
from __future__ import annotations

import json
import time
from datetime import date, timedelta
from typing import Any
from uuid import UUID, uuid4

from agent.tools.audit_log import write_agent_audit_event
from agent.tools.clickhouse_sql import rows_to_dicts
from agent.tools.metadata_catalog import build_metadata_catalog_sql, normalize_metadata_asset
from pipelines.common.clickhouse import (
    build_clickhouse_client,
    format_date_literal,
    quote_sql_literal,
    validate_qualified_table_name,
)
from pipelines.common.logging import logger
from pipelines.seeding.helpers import parse_date


# --- Defining Constants
TOOL_NAME             = "table_health"
PROFILE_RESULTS_TABLE = "dq.data_profile_results"
DQ_RESULTS_TABLE      = "dq.dq_check_results"
DEFAULT_LOOKBACK_DAYS = 14
MAX_LOOKBACK_DAYS     = 30
MAX_PROFILE_ROWS      = 500
MAX_DQ_ROWS           = 500

TRUST_STATES          = {"healthy", "warning", "critical", "unverified"}
KNOWN_DQ_STATUSES     = {"pass", "warn", "fail", "skip"}


# --- Defining Validation Helpers
def normalize_lookback_days(lookback_days: int) -> int:
    """
    Validate the historical window used by table-health queries.

    Args:
        lookback_days: Number of calendar days before the target date.

    Returns:
        Validated lookback value between zero and thirty.

    Raises:
        ValueError: If the lookback is outside the public safety bound.
    """
    normalized = int(lookback_days)

    if not 0 <= normalized <= MAX_LOOKBACK_DAYS:
        raise ValueError(
            f"Table health lookback_days must be between 0 and {MAX_LOOKBACK_DAYS}."
        )

    return normalized


def parse_details_object(value: Any, field_name: str) -> dict[str, Any]:
    """
    Parse one persisted JSON object without silently hiding malformed evidence.

    Args:
        value: Raw JSON text or an already parsed dictionary.
        field_name: Source field name used in validation errors.

    Returns:
        Parsed JSON object.

    Raises:
        ValueError: If the value is malformed or is not a JSON object.
    """
    if isinstance(value, dict):
        return value

    raw_text = str(value or "{}").strip() or "{}"

    try:
        parsed = json.loads(raw_text)

    except json.JSONDecodeError as exc:
        raise ValueError(f"{field_name} contains malformed JSON.") from exc

    if not isinstance(parsed, dict):
        raise ValueError(f"{field_name} must contain a JSON object.")

    return parsed


# --- Building Bounded SQL
def build_table_health_profile_sql(
    table_name: str,
    dt: date,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> str:
    """
    Build the latest-per-metric profile query for one table and date window.

    Args:
        table_name: Fully qualified ClickHouse table identity.
        dt: Target business date.
        lookback_days: Number of preceding calendar days to include.

    Returns:
        Static read-only ClickHouse SQL with date filters and a hard row limit.
    """
    normalized_table    = validate_qualified_table_name(table_name.strip())
    normalized_lookback = normalize_lookback_days(lookback_days)

    return f"""
        SELECT
            argMax(profile_run_id, run_at) AS profile_run_id,
            max(run_at)                    AS run_at,
            dt,
            table_name,
            column_name,
            metric_name,
            argMax(metric_value, run_at)   AS metric_value,
            argMax(metric_unit, run_at)    AS metric_unit,
            argMax(details_json, run_at)   AS details_json
        FROM {PROFILE_RESULTS_TABLE}
        WHERE table_name = {quote_sql_literal(normalized_table)}
          AND dt >= {format_date_literal(dt)} - INTERVAL {normalized_lookback} DAY
          AND dt <= {format_date_literal(dt)}
        GROUP BY
            dt,
            table_name,
            column_name,
            metric_name
        ORDER BY dt DESC, column_name, metric_name
        LIMIT {MAX_PROFILE_ROWS}
    """


def build_table_health_dq_sql(
    table_name: str,
    dt: date,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> str:
    """
    Build the latest-per-check DQ query for one table and date window.

    Args:
        table_name: Fully qualified ClickHouse table identity.
        dt: Target business date.
        lookback_days: Number of preceding calendar days to include.

    Returns:
        Static read-only ClickHouse SQL with date filters and a hard row limit.
    """
    normalized_table    = validate_qualified_table_name(table_name.strip())
    normalized_lookback = normalize_lookback_days(lookback_days)

    return f"""
        SELECT
            argMax(check_run_id, run_at)       AS check_run_id,
            max(run_at)                        AS run_at,
            dt,
            table_name,
            check_name,
            argMax(check_type, run_at)         AS check_type,
            argMax(status, run_at)             AS status,
            argMax(severity, run_at)           AS severity,
            argMax(observed_value, run_at)     AS observed_value,
            argMax(expected_value, run_at)     AS expected_value,
            argMax(threshold_value, run_at)    AS threshold_value,
            argMax(details_json, run_at)       AS details_json,
            argMax(evidence_s3_uri, run_at)    AS evidence_s3_uri
        FROM {DQ_RESULTS_TABLE}
        WHERE table_name = {quote_sql_literal(normalized_table)}
          AND dt >= {format_date_literal(dt)} - INTERVAL {normalized_lookback} DAY
          AND dt <= {format_date_literal(dt)}
        GROUP BY
            dt,
            table_name,
            check_name
        ORDER BY dt DESC, check_name
        LIMIT {MAX_DQ_ROWS}
    """


# --- Normalizing Query Results
def normalize_table_health_rows(
    rows: list[dict[str, Any]],
    table_name: str,
    dt: date,
    lookback_days: int,
    details_field: str = "details_json",
) -> list[dict[str, Any]]:
    """
    Validate table/date identity and expose parsed public details.

    Args:
        rows: Raw ClickHouse result dictionaries.
        table_name: Exact requested fully qualified table.
        dt: Target business date.
        lookback_days: Applied historical date window.
        details_field: Persisted JSON field to parse and remove.

    Returns:
        Public rows containing parsed details and no serialized JSON field.

    Raises:
        ValueError: If a row escapes the requested table/date window or JSON contract.
    """
    earliest_dt = dt - timedelta(days=lookback_days)
    normalized  = []

    for row in rows:
        row_table = str(row.get("table_name") or "").strip()
        row_dt    = parse_date(str(row.get("dt") or ""))

        if row_table != table_name:
            raise ValueError("Table health query returned evidence for another table.")

        if not earliest_dt <= row_dt <= dt:
            raise ValueError("Table health query returned evidence outside the requested date window.")

        public_row            = dict(row)
        public_row["details"] = parse_details_object(
            public_row.pop(details_field, "{}"),
            field_name=details_field,
        )
        normalized.append(public_row)

    return normalized


def classify_table_trust_state(
    current_dq_rows: list[dict[str, Any]],
    current_profile_rows: list[dict[str, Any]],
) -> tuple[str, str]:
    """
    Derive a conservative trust state from current deterministic evidence.

    Args:
        current_dq_rows: Latest DQ results for the exact target date.
        current_profile_rows: Latest profile metrics for the exact target date.

    Returns:
        Tuple containing trust state and an operator-facing reason.
    """
    if not current_dq_rows:
        return (
            "unverified",
            "No DQ check result exists for the selected table and date.",
        )

    statuses = {
        str(row.get("status") or "unknown").strip().lower()
        for row in current_dq_rows
    }

    if "fail" in statuses:
        return (
            "critical",
            "At least one current DQ check failed. Investigate before trusting downstream use.",
        )

    if statuses.difference(KNOWN_DQ_STATUSES):
        return (
            "unverified",
            "Current DQ evidence contains an unsupported status and requires review.",
        )

    if statuses.intersection({"warn", "skip"}):
        return (
            "warning",
            "Current DQ checks include warning or skipped outcomes that require review.",
        )

    if not current_profile_rows:
        return (
            "warning",
            "DQ checks passed, but no current profiling evidence is available for this table.",
        )

    return (
        "healthy",
        "Current DQ checks passed and profiling evidence is available for the selected date.",
    )


def build_table_health_payload(
    table_name: str,
    dt: date,
    lookback_days: int,
    metadata_asset: dict[str, Any] | None,
    profile_rows: list[dict[str, Any]],
    dq_rows: list[dict[str, Any]],
    duration_ms: int = 0,
) -> dict[str, Any]:
    """
    Assemble one public table-health response from normalized evidence rows.

    Args:
        table_name: Exact fully qualified table identity.
        dt: Target business date.
        lookback_days: Applied historical date window.
        metadata_asset: Optional registered asset ownership and trust context.
        profile_rows: Normalized profile evidence across the window.
        dq_rows: Normalized latest-per-check DQ evidence across the window.
        duration_ms: End-to-end tool duration measured by the caller.

    Returns:
        JSON-serializable table-health payload without SQL or raw JSON fields.
    """
    normalized_lookback = normalize_lookback_days(lookback_days)

    if metadata_asset and metadata_asset.get("qualified_name") != table_name:
        raise ValueError("Table health metadata identity does not match the requested table.")

    current_profiles = [row for row in profile_rows if parse_date(str(row["dt"])) == dt]
    current_dq       = [row for row in dq_rows if parse_date(str(row["dt"])) == dt]
    status_counts: dict[str, int] = {}

    for row in current_dq:
        status = str(row.get("status") or "unknown").strip().lower()
        status_counts[status] = status_counts.get(status, 0) + 1

    trust_state, trust_reason = classify_table_trust_state(
        current_dq_rows=current_dq,
        current_profile_rows=current_profiles,
    )

    return {
        "status": "success",
        "table_name": table_name,
        "dt": dt.isoformat(),
        "lookback_days": normalized_lookback,
        "trust_state": trust_state,
        "trust_reason": trust_reason,
        "metadata_registered": metadata_asset is not None,
        "metadata_asset": metadata_asset,
        "current_check_count": len(current_dq),
        "current_status_counts": status_counts,
        "profile_metric_count": len(profile_rows),
        "dq_result_count": len(dq_rows),
        "profile_metrics": profile_rows,
        "dq_results": dq_rows,
        "duration_ms": max(0, int(duration_ms)),
        "summary": (
            f"Table health for {table_name} on {dt.isoformat()} is {trust_state}: "
            f"{trust_reason}"
        ),
    }


# --- Defining Public Tool Function
def fetch_table_health(
    table_name: str,
    dt: date | str,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    agent_run_id: UUID | str | None = None,
    alert_key: str = "",
    clickhouse_host: str | None = None,
    clickhouse_port: int | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    """
    Fetch bounded metadata, profile, and DQ evidence for one table/date.

    Args:
        table_name: Fully qualified ClickHouse table identity.
        dt: Target business date or YYYY-MM-DD string.
        lookback_days: Number of preceding calendar days to include.
        agent_run_id: Optional audit correlation UUID.
        alert_key: Optional related alert system key.
        clickhouse_host: Optional ClickHouse host override.
        clickhouse_port: Optional ClickHouse HTTP port override.
        client: Optional injected ClickHouse client for focused tests.

    Returns:
        Public deterministic table-health payload plus no internal query text.

    Raises:
        ValueError: If input or returned evidence violates the bounded contract.
        Exception: If ClickHouse querying or audit persistence fails.
    """
    normalized_table        = validate_qualified_table_name(table_name.strip())
    target_dt               = dt if isinstance(dt, date) else parse_date(str(dt))
    normalized_lookback     = normalize_lookback_days(lookback_days)
    resolved_client = client or build_clickhouse_client(
        host=clickhouse_host,
        port=clickhouse_port,
    )
    resolved_agent_run_id   = UUID(str(agent_run_id)) if agent_run_id else uuid4()
    started_monotonic       = time.monotonic()
    metadata_sql            = build_metadata_catalog_sql(qualified_name=normalized_table, limit=1)
    profile_sql             = build_table_health_profile_sql(normalized_table, target_dt, normalized_lookback)
    dq_sql                  = build_table_health_dq_sql(normalized_table, target_dt, normalized_lookback)
    combined_sql_for_audit = (
        "\n-- metadata\n"
        + metadata_sql
        + "\n-- profiles\n"
        + profile_sql
        + "\n-- dq\n"
        + dq_sql
    )

    try:
        metadata_result = resolved_client.query(metadata_sql)
        profile_result  = resolved_client.query(profile_sql)
        dq_result       = resolved_client.query(dq_sql)

        metadata_rows = rows_to_dicts(
            columns=list(metadata_result.column_names or []),
            rows=metadata_result.result_rows,
        )
        raw_profiles = rows_to_dicts(
            columns=list(profile_result.column_names or []),
            rows=profile_result.result_rows,
        )
        raw_dq = rows_to_dicts(
            columns=list(dq_result.column_names or []),
            rows=dq_result.result_rows,
        )

        metadata_asset = normalize_metadata_asset(metadata_rows[0]) if metadata_rows else None
        profile_rows   = normalize_table_health_rows(
            rows=raw_profiles,
            table_name=normalized_table,
            dt=target_dt,
            lookback_days=normalized_lookback,
        )
        dq_rows = normalize_table_health_rows(
            rows=raw_dq,
            table_name=normalized_table,
            dt=target_dt,
            lookback_days=normalized_lookback,
        )
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)
        payload     = build_table_health_payload(
            table_name=normalized_table,
            dt=target_dt,
            lookback_days=normalized_lookback,
            metadata_asset=metadata_asset,
            profile_rows=profile_rows,
            dq_rows=dq_rows,
            duration_ms=duration_ms,
        )

        write_agent_audit_event(
            client=resolved_client,
            action="fetch_table_health",
            status="success",
            agent_run_id=resolved_agent_run_id,
            alert_key=alert_key,
            tool_name=TOOL_NAME,
            duration_ms=duration_ms,
            input_payload={
                "table_name": normalized_table,
                "dt": target_dt.isoformat(),
                "lookback_days": normalized_lookback,
            },
            output_payload={
                "trust_state": payload["trust_state"],
                "metadata_registered": payload["metadata_registered"],
                "profile_metric_count": payload["profile_metric_count"],
                "dq_result_count": payload["dq_result_count"],
                "current_status_counts": payload["current_status_counts"],
            },
            sql=combined_sql_for_audit,
            row_count=len(metadata_rows) + len(profile_rows) + len(dq_rows),
        )

        logger.info(
            "Fetched table health | table=%s dt=%s trust_state=%s profiles=%d dq_results=%d duration_ms=%d",
            normalized_table,
            target_dt,
            payload["trust_state"],
            payload["profile_metric_count"],
            payload["dq_result_count"],
            duration_ms,
        )

        return payload

    except Exception as exc:
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)
        logger.exception("Failed to fetch table health | table=%s dt=%s", normalized_table, target_dt)

        write_agent_audit_event(
            client=resolved_client,
            action="fetch_table_health",
            status="failed",
            agent_run_id=resolved_agent_run_id,
            alert_key=alert_key,
            tool_name=TOOL_NAME,
            duration_ms=duration_ms,
            input_payload={
                "table_name": normalized_table,
                "dt": target_dt.isoformat(),
                "lookback_days": normalized_lookback,
            },
            output_payload={"error_type": type(exc).__name__},
            error_message=str(exc),
            sql=combined_sql_for_audit,
        )

        raise
