####
## DQ History Tool for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Add repo root before importing project packages when this file is executed by path.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.state import Alert, EvidenceItem, EvidenceType
from agent.tools.alerts import load_alert
from agent.tools.audit_log import write_agent_audit_event
from agent.tools.clickhouse_sql import rows_to_dicts
from pipelines.common.clickhouse import build_clickhouse_client, format_date_literal, quote_sql_literal, validate_qualified_table_name
from pipelines.common.logging import logger
from pipelines.seeding.helpers import parse_date


# --- Defining Constants
TOOL_NAME            = "dq_history"
DQ_CHECK_RESULTS     = "dq.dq_check_results"
DEFAULT_LOOKBACK_DAYS = 14
DEFAULT_LIMIT         = 100


# --- Defining Functions
def build_dq_history_sql(
    table_name: str,
    dt: date,
    check_name: str | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    limit: int = DEFAULT_LIMIT,
) -> str:
    """
    Build a bounded DQ history query for one table and date window.

    Args:
        table_name: Fully qualified ClickHouse table name being investigated.
        dt: Target business date.
        check_name: Optional specific DQ check name.
        lookback_days: Number of days before dt to include.
        limit: Maximum rows returned.

    Returns:
        ClickHouse SQL query string.
    """
    validate_qualified_table_name(table_name)

    safe_lookback = max(0, min(lookback_days, 90))
    safe_limit    = max(1, min(limit, 500))
    filters       = [
        f"table_name = {quote_sql_literal(table_name)}",
        f"dt >= {format_date_literal(dt)} - INTERVAL {safe_lookback} DAY",
        f"dt <= {format_date_literal(dt)}",
    ]

    if check_name:
        filters.append(f"check_name = {quote_sql_literal(check_name)}")

    where_sql = " AND ".join(filters)

    return f"""
        SELECT
            check_run_id,
            run_at,
            dt,
            table_name,
            check_name,
            check_type,
            status,
            severity,
            observed_value,
            expected_value,
            threshold_value,
            details_json,
            evidence_s3_uri
        FROM {DQ_CHECK_RESULTS}
        WHERE {where_sql}
        ORDER BY dt DESC, run_at DESC, check_name
        LIMIT {safe_limit}
    """


def fetch_dq_history(
    table_name: str,
    dt: date,
    check_name: str | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    limit: int = DEFAULT_LIMIT,
    agent_run_id: UUID | str | None = None,
    alert_key: str = "",
    clickhouse_host: str | None = None,
    clickhouse_port: int | None = None,
) -> dict[str, Any]:
    """
    Fetch recent DQ check history for a table/check/date window.

    Args:
        table_name: Fully qualified ClickHouse table name being investigated.
        dt: Target business date.
        check_name: Optional specific DQ check name.
        lookback_days: Number of days before dt to include.
        limit: Maximum rows returned.
        agent_run_id: Optional agent run UUID for audit correlation.
        alert_key: Optional alert key for audit context.
        clickhouse_host: Optional ClickHouse host override.
        clickhouse_port: Optional ClickHouse HTTP port override.

    Returns:
        Dictionary with DQ history rows and metadata.
    """
    client                = build_clickhouse_client(host=clickhouse_host, port=clickhouse_port)
    resolved_agent_run_id = UUID(str(agent_run_id)) if agent_run_id else uuid4()
    started_monotonic     = time.monotonic()
    sql                   = build_dq_history_sql(
        table_name=table_name,
        dt=dt,
        check_name=check_name,
        lookback_days=lookback_days,
        limit=limit,
    )

    try:
        result      = client.query(sql)
        columns     = list(result.column_names or [])
        rows        = rows_to_dicts(columns=columns, rows=result.result_rows)
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)
        status_counts = summarize_statuses(rows)

        write_agent_audit_event(
            client=client,
            action="fetch_dq_history",
            status="success",
            agent_run_id=resolved_agent_run_id,
            alert_key=alert_key,
            tool_name=TOOL_NAME,
            duration_ms=duration_ms,
            input_payload={
                "table_name": table_name,
                "dt": dt.isoformat(),
                "check_name": check_name,
                "lookback_days": lookback_days,
                "limit": limit,
            },
            output_payload={"row_count": len(rows), "status_counts": status_counts},
            sql=sql,
            row_count=len(rows),
        )

        logger.info("Fetched DQ history | table=%s dt=%s rows=%d", table_name, dt, len(rows))

        return {
            "status": "success",
            "table_name": table_name,
            "dt": dt.isoformat(),
            "check_name": check_name,
            "lookback_days": lookback_days,
            "rows": rows,
            "row_count": len(rows),
            "status_counts": status_counts,
            "sql": sql,
        }

    except Exception as exc:
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)
        logger.exception("Failed to fetch DQ history | table=%s dt=%s", table_name, dt)

        write_agent_audit_event(
            client=client,
            action="fetch_dq_history",
            status="failed",
            agent_run_id=resolved_agent_run_id,
            alert_key=alert_key,
            tool_name=TOOL_NAME,
            duration_ms=duration_ms,
            input_payload={"table_name": table_name, "dt": dt.isoformat(), "check_name": check_name},
            output_payload={"error_type": type(exc).__name__},
            error_message=str(exc),
            sql=sql,
        )

        raise


def collect_dq_history_evidence(
    alert: Alert,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    agent_run_id: UUID | str | None = None,
    clickhouse_host: str | None = None,
    clickhouse_port: int | None = None,
) -> EvidenceItem:
    """
    Build a DQ history evidence item for an alert.

    Args:
        alert: Alert being investigated.
        lookback_days: Number of days before alert.dt to include.
        agent_run_id: Optional agent run UUID for audit correlation.
        clickhouse_host: Optional ClickHouse host override.
        clickhouse_port: Optional ClickHouse HTTP port override.

    Returns:
        EvidenceItem containing recent DQ check rows.

    Raises:
        ValueError: If alert.dt is missing.
    """
    if alert.dt is None:
        raise ValueError("Alert dt is required to collect DQ history evidence.")

    result = fetch_dq_history(
        table_name=alert.table_name,
        dt=alert.dt,
        check_name=alert.metric,
        lookback_days=lookback_days,
        agent_run_id=agent_run_id,
        alert_key=alert.alert_key,
        clickhouse_host=clickhouse_host,
        clickhouse_port=clickhouse_port,
    )
    summary = (
        f"Found {result['row_count']} DQ history rows for {alert.table_name}.{alert.metric} "
        f"over {lookback_days} day(s). Status counts: {result['status_counts']}"
    )

    return EvidenceItem(
        evidence_type=EvidenceType.DQ_HISTORY,
        tool_name=TOOL_NAME,
        description="Recent DQ check history for the alert metric and table.",
        query=result["sql"],
        rows=result["rows"],
        row_count=result["row_count"],
        summary=summary,
    )


def summarize_statuses(rows: list[dict[str, Any]]) -> dict[str, int]:
    """
    Count DQ result statuses in a result set.

    Args:
        rows: DQ check result row dictionaries.

    Returns:
        Mapping of status name to count.
    """
    counts: dict[str, int] = {}

    for row in rows:
        status = str(row.get("status", "unknown"))
        counts[status] = counts.get(status, 0) + 1

    return counts


def build_parser() -> argparse.ArgumentParser:
    """
    Build the command-line parser for DQ history lookup.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(description="Fetch recent DQ check history for an alert or table.")

    parser.add_argument("--alert-key", default=None, help="Optional alert key to derive table, metric, and dt.")
    parser.add_argument("--table-name", default=None, help="Fully qualified table name when not using --alert-key.")
    parser.add_argument("--check-name", default=None, help="Optional DQ check name when not using --alert-key.")
    parser.add_argument("--dt", default=None, help="Business date in YYYY-MM-DD format when not using --alert-key.")
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS, help="Historical lookback window.")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Maximum rows returned.")
    parser.add_argument("--agent-run-id", default=None, help="Optional agent run UUID.")
    parser.add_argument("--clickhouse-host", default=None, help="Optional ClickHouse host override.")
    parser.add_argument("--clickhouse-port", type=int, default=None, help="Optional ClickHouse HTTP port override.")

    return parser


def main() -> None:
    """
    Parse CLI arguments and fetch DQ history.

    Returns:
        None.
    """
    parser = build_parser()
    args   = parser.parse_args()

    if args.alert_key:
        resolved_agent_run_id = args.agent_run_id or str(uuid4())
        alert                 = load_alert(alert_key=args.alert_key, agent_run_id=resolved_agent_run_id)
        evidence = collect_dq_history_evidence(
            alert=alert,
            lookback_days=args.lookback_days,
            agent_run_id=resolved_agent_run_id,
            clickhouse_host=args.clickhouse_host,
            clickhouse_port=args.clickhouse_port,
        )
        print(evidence.model_dump_json(indent=2))

        return

    if not args.table_name or not args.dt:
        parser.error("Provide --alert-key or both --table-name and --dt.")

    result = fetch_dq_history(
        table_name=args.table_name,
        dt=parse_date(args.dt),
        check_name=args.check_name,
        lookback_days=args.lookback_days,
        limit=args.limit,
        agent_run_id=args.agent_run_id,
        clickhouse_host=args.clickhouse_host,
        clickhouse_port=args.clickhouse_port,
    )
    print(json.dumps(result, indent=2, default=str))


# --- Running CLI Entrypoint
if __name__ == "__main__":
    main()
