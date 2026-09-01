####
## Daily Data Quality Summary Tool for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Build one deterministic, audited daily data-quality summary from ClickHouse."""

# --- Importing Libraries
from __future__ import annotations

import time
from typing import Any
from uuid import UUID, uuid4

from agent.tools.audit_log import write_agent_audit_event
from agent.tools.clickhouse_sql import rows_to_dicts
from pipelines.common.clickhouse import build_clickhouse_client, format_date_literal, quote_sql_literal
from pipelines.common.logging import logger
from pipelines.seeding.helpers import parse_date


# --- Defining Constants
TOOL_NAME                = "daily_summary"
DQ_CHECK_RESULTS_TABLE   = "dq.dq_check_results"
ALERTS_TABLE             = "dq.alerts"
OPEN_ALERT_STATUS        = "open"
MAX_SUMMARY_GROUPS       = 100


# --- Defining Query Helpers
def build_daily_summary_sql(dt: str) -> str:
    """
    Build one date-filtered aggregation query for checks and open alerts.

    Args:
        dt: Business date in YYYY-MM-DD format.

    Returns:
        Bounded ClickHouse SQL that emits category, label, and count rows.
    """
    run_dt = parse_date(dt)

    return f"""
        SELECT
            category,
            label,
            count
        FROM
        (
            SELECT
                'check' AS category,
                toString(status) AS label,
                count() AS count
            FROM {DQ_CHECK_RESULTS_TABLE}
            WHERE dt = {format_date_literal(run_dt)}
            GROUP BY status

            UNION ALL

            SELECT
                'alert' AS category,
                toString(severity) AS label,
                count() AS count
            FROM {ALERTS_TABLE}
            WHERE dt = {format_date_literal(run_dt)}
              AND status = {quote_sql_literal(OPEN_ALERT_STATUS)}
            GROUP BY severity
        )
        ORDER BY category, label
        LIMIT {MAX_SUMMARY_GROUPS}
    """


def query_daily_summary_rows(
    client: Any,
    dt: str,
) -> tuple[str, list[dict[str, Any]]]:
    """
    Execute the bounded daily aggregation query.

    Args:
        client: clickhouse-connect client instance.
        dt: Business date in YYYY-MM-DD format.

    Returns:
        Tuple containing the executed SQL and normalized result dictionaries.
    """
    sql     = build_daily_summary_sql(dt=dt)
    result  = client.query(sql)
    columns = list(result.column_names or [])
    rows    = rows_to_dicts(columns=columns, rows=result.result_rows)

    logger.info("Queried daily quality summary | dt=%s groups=%d", dt, len(rows))

    return sql, rows


def build_daily_summary_payload(
    dt: str,
    rows: list[dict[str, Any]],
    duration_ms: int = 0,
) -> dict[str, Any]:
    """
    Convert generic aggregation rows into the public summary shape.

    Args:
        dt: Business date in YYYY-MM-DD format.
        rows: Aggregation rows with category, label, and count fields.
        duration_ms: Query duration measured by the caller.

    Returns:
        JSON-serializable daily summary with check and alert totals.

    Raises:
        ValueError: If a row contains an unknown category, blank label, or invalid count.
    """
    normalized_dt = parse_date(dt).isoformat()
    check_counts  = []
    alert_counts  = []

    for row in rows:
        category = str(row.get("category") or "").strip().lower()
        label    = str(row.get("label") or "").strip().lower()
        count    = row.get("count")

        if not label:
            raise ValueError("Daily summary aggregation returned a blank label.")

        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("Daily summary aggregation returned an invalid count.")

        if category == "check":
            check_counts.append({"status": label, "count": count})

        elif category == "alert":
            alert_counts.append({"severity": label, "count": count})

        else:
            raise ValueError(f"Daily summary aggregation returned an unknown category: {category}")

    total_checks      = sum(item["count"] for item in check_counts)
    total_open_alerts = sum(item["count"] for item in alert_counts)

    return {
        "status": "success",
        "dt": normalized_dt,
        "check_counts": check_counts,
        "alert_counts": alert_counts,
        "total_checks": total_checks,
        "total_open_alerts": total_open_alerts,
        "duration_ms": max(0, int(duration_ms)),
        "summary": (
            f"Daily quality summary for {normalized_dt}: "
            f"{total_checks} check result(s) and {total_open_alerts} open alert(s)."
        ),
    }


# --- Defining Public Tool Function
def fetch_daily_quality_summary(
    dt: str,
    agent_run_id: UUID | str | None = None,
    clickhouse_host: str | None = None,
    clickhouse_port: int | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    """
    Fetch one date's deterministic DQ check and open-alert summary.

    Args:
        dt: Business date in YYYY-MM-DD format.
        agent_run_id: Optional audit correlation UUID.
        clickhouse_host: Optional ClickHouse host override.
        clickhouse_port: Optional ClickHouse HTTP port override.
        client: Optional injected ClickHouse client for focused tests.

    Returns:
        Daily summary payload plus internal SQL metadata for audit consumers.

    Raises:
        ValueError: If the date or aggregation result is invalid.
        Exception: If ClickHouse querying or audit persistence fails.
    """
    normalized_dt         = parse_date(dt).isoformat()
    resolved_client       = client or build_clickhouse_client(host=clickhouse_host, port=clickhouse_port)
    resolved_agent_run_id = UUID(str(agent_run_id)) if agent_run_id else uuid4()
    started_monotonic     = time.monotonic()

    try:
        sql, rows  = query_daily_summary_rows(client=resolved_client, dt=normalized_dt)
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)
        payload     = build_daily_summary_payload(
            dt=normalized_dt,
            rows=rows,
            duration_ms=duration_ms,
        )
        payload["sql"] = sql

        write_agent_audit_event(
            client=resolved_client,
            action="fetch_daily_quality_summary",
            status="success",
            agent_run_id=resolved_agent_run_id,
            tool_name=TOOL_NAME,
            duration_ms=duration_ms,
            input_payload={"dt": normalized_dt, "alert_status": OPEN_ALERT_STATUS},
            output_payload={
                "total_checks": payload["total_checks"],
                "total_open_alerts": payload["total_open_alerts"],
            },
            sql=sql,
            row_count=len(rows),
        )

        logger.info(
            "Fetched daily quality summary | dt=%s checks=%d open_alerts=%d duration_ms=%d",
            normalized_dt,
            payload["total_checks"],
            payload["total_open_alerts"],
            duration_ms,
        )

        return payload

    except Exception as exc:
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)
        logger.exception("Failed to fetch daily quality summary | dt=%s", normalized_dt)

        write_agent_audit_event(
            client=resolved_client,
            action="fetch_daily_quality_summary",
            status="failed",
            agent_run_id=resolved_agent_run_id,
            tool_name=TOOL_NAME,
            duration_ms=duration_ms,
            input_payload={"dt": normalized_dt, "alert_status": OPEN_ALERT_STATUS},
            output_payload={"error_type": type(exc).__name__},
            error_message=str(exc),
        )

        raise
