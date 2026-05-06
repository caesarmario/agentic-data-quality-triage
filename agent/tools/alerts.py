####
## Alert Lookup Tool for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Add repo root before importing project packages when this file is executed by path.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.state import Alert
from agent.tools.audit_log import write_agent_audit_event
from agent.tools.clickhouse_sql import rows_to_dicts
from pipelines.common.clickhouse import build_clickhouse_client, format_date_literal, quote_sql_literal
from pipelines.common.logging import logger
from pipelines.seeding.helpers import parse_date


TOOL_NAME    = "alerts"
ALERTS_TABLE = "dq.alerts"


def build_alert_filters(
    alert_id: str | None = None,
    alert_key: str | None = None,
    status: str | None = None,
    dt: str | None = None,
) -> list[str]:
    """
    Build bounded WHERE predicates for alert lookup queries.

    Args:
        alert_id: Optional alert UUID string.
        alert_key: Optional stable alert key.
        status: Optional alert status filter.
        dt: Optional business date in YYYY-MM-DD format.

    Returns:
        List of SQL predicate strings.
    """
    filters = []

    if alert_id:
        filters.append(f"alert_id = toUUID({quote_sql_literal(str(alert_id))})")

    if alert_key:
        filters.append(f"alert_key = {quote_sql_literal(alert_key)}")

    if status:
        filters.append(f"status = {quote_sql_literal(status)}")

    if dt:
        filters.append(f"dt = {format_date_literal(parse_date(dt))}")

    return filters


def build_alert_lookup_sql(
    alert_id: str | None = None,
    alert_key: str | None = None,
    status: str | None = None,
    dt: str | None = None,
    limit: int = 20,
) -> str:
    """
    Build a ClickHouse SQL query for alert lookup.

    Args:
        alert_id: Optional alert UUID string.
        alert_key: Optional stable alert key.
        status: Optional alert status filter.
        dt: Optional business date in YYYY-MM-DD format.
        limit: Maximum alerts to return.

    Returns:
        SQL query string.
    """
    filters    = build_alert_filters(alert_id=alert_id, alert_key=alert_key, status=status, dt=dt)
    where_sql  = "WHERE " + " AND ".join(filters) if filters else ""
    safe_limit = max(1, min(limit, 100))

    return f"""
        SELECT
            alert_id,
            alert_key,
            created_at,
            updated_at,
            status,
            alert_type,
            severity,
            table_name,
            metric,
            dt,
            dimension,
            observed_value,
            expected_value,
            threshold_value,
            source_check_run_id,
            details_json,
            report_s3_uri
        FROM {ALERTS_TABLE}
        {where_sql}
        ORDER BY created_at DESC
        LIMIT {safe_limit}
    """


def query_alert_rows(
    client: Any,
    alert_id: str | None = None,
    alert_key: str | None = None,
    status: str | None = None,
    dt: str | None = None,
    limit: int = 20,
) -> tuple[str, list[dict[str, Any]]]:
    """
    Query ClickHouse alert rows and return JSON-serializable dictionaries.

    Args:
        client: clickhouse-connect client instance.
        alert_id: Optional alert UUID string.
        alert_key: Optional stable alert key.
        status: Optional alert status filter.
        dt: Optional business date in YYYY-MM-DD format.
        limit: Maximum alerts to return.

    Returns:
        Tuple of SQL query and alert row dictionaries.
    """
    sql       = build_alert_lookup_sql(alert_id=alert_id, alert_key=alert_key, status=status, dt=dt, limit=limit)
    result    = client.query(sql)
    columns   = list(result.column_names or [])
    rows      = rows_to_dicts(columns=columns, rows=result.result_rows)

    logger.info("Queried alerts | rows=%d status=%s dt=%s", len(rows), status, dt)

    return sql, rows


def list_alerts(
    status: str = "open",
    dt: str | None = None,
    limit: int = 20,
    agent_run_id: UUID | str | None = None,
    clickhouse_host: str | None = None,
    clickhouse_port: int | None = None,
) -> dict[str, Any]:
    """
    List alerts for UI, Discord, or agent context loading.

    Args:
        status: Alert status filter. Defaults to open.
        dt: Optional business date in YYYY-MM-DD format.
        limit: Maximum alerts to return.
        agent_run_id: Optional agent run UUID for audit correlation.
        clickhouse_host: Optional ClickHouse host override.
        clickhouse_port: Optional ClickHouse HTTP port override.

    Returns:
        Dictionary containing alert rows and query metadata.
    """
    client                = build_clickhouse_client(host=clickhouse_host, port=clickhouse_port)
    resolved_agent_run_id = UUID(str(agent_run_id)) if agent_run_id else uuid4()
    started_monotonic     = time.monotonic()

    try:
        sql, rows   = query_alert_rows(client=client, status=status, dt=dt, limit=limit)
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)
        payload     = {
            "status": "success",
            "alerts": rows,
            "row_count": len(rows),
            "sql": sql,
        }

        write_agent_audit_event(
            client=client,
            action="list_alerts",
            status="success",
            agent_run_id=resolved_agent_run_id,
            tool_name=TOOL_NAME,
            duration_ms=duration_ms,
            input_payload={"status": status, "dt": dt, "limit": limit},
            output_payload={"row_count": len(rows)},
            sql=sql,
            row_count=len(rows),
        )

        return payload

    except Exception as exc:
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)
        logger.exception("Failed to list alerts")

        write_agent_audit_event(
            client=client,
            action="list_alerts",
            status="failed",
            agent_run_id=resolved_agent_run_id,
            tool_name=TOOL_NAME,
            duration_ms=duration_ms,
            input_payload={"status": status, "dt": dt, "limit": limit},
            output_payload={"error_type": type(exc).__name__},
            error_message=str(exc),
        )

        raise


def load_alert(
    alert_id: str | None = None,
    alert_key: str | None = None,
    agent_run_id: UUID | str | None = None,
    clickhouse_host: str | None = None,
    clickhouse_port: int | None = None,
) -> Alert:
    """
    Load one alert by alert_id or alert_key.

    Args:
        alert_id: Optional ClickHouse alert UUID string.
        alert_key: Optional stable alert key.
        agent_run_id: Optional agent run UUID for audit correlation.
        clickhouse_host: Optional ClickHouse host override.
        clickhouse_port: Optional ClickHouse HTTP port override.

    Returns:
        Alert Pydantic model.

    Raises:
        ValueError: If neither identifier is provided or no alert is found.
    """
    if not alert_id and not alert_key:
        raise ValueError("Provide alert_id or alert_key.")

    client                = build_clickhouse_client(host=clickhouse_host, port=clickhouse_port)
    resolved_agent_run_id = UUID(str(agent_run_id)) if agent_run_id else uuid4()
    started_monotonic     = time.monotonic()

    try:
        sql, rows = query_alert_rows(client=client, alert_id=alert_id, alert_key=alert_key, limit=1)

        if not rows:
            raise ValueError(f"Alert not found: alert_id={alert_id}, alert_key={alert_key}")

        alert       = Alert.from_clickhouse_dict(rows[0])
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)

        write_agent_audit_event(
            client=client,
            action="load_alert",
            status="success",
            agent_run_id=resolved_agent_run_id,
            alert_id=alert.alert_id,
            alert_key=alert.alert_key,
            tool_name=TOOL_NAME,
            duration_ms=duration_ms,
            input_payload={"alert_id": alert_id, "alert_key": alert_key},
            output_payload={"loaded_alert_key": alert.alert_key, "severity": alert.severity},
            sql=sql,
            row_count=1,
        )

        logger.info("Loaded alert | alert_key=%s severity=%s", alert.alert_key, alert.severity)

        return alert

    except Exception as exc:
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)
        logger.exception("Failed to load alert | alert_id=%s alert_key=%s", alert_id, alert_key)

        write_agent_audit_event(
            client=client,
            action="load_alert",
            status="failed",
            agent_run_id=resolved_agent_run_id,
            alert_key=alert_key or "",
            tool_name=TOOL_NAME,
            duration_ms=duration_ms,
            input_payload={"alert_id": alert_id, "alert_key": alert_key},
            output_payload={"error_type": type(exc).__name__},
            error_message=str(exc),
        )

        raise


def build_parser() -> argparse.ArgumentParser:
    """
    Build the command-line parser for alert lookup.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(description="Load or list DQ alerts from ClickHouse.")

    parser.add_argument("--alert-id", default=None, help="Optional alert UUID to load.")
    parser.add_argument("--alert-key", default=None, help="Optional alert key to load.")
    parser.add_argument("--status", default="open", help="Alert status for list mode.")
    parser.add_argument("--dt", default=None, help="Optional business date for list mode, in YYYY-MM-DD format.")
    parser.add_argument("--limit", type=int, default=20, help="Maximum alerts to list.")
    parser.add_argument("--mode", choices=["load", "list"], default="list", help="Lookup mode.")
    parser.add_argument("--agent-run-id", default=None, help="Optional agent run UUID.")
    parser.add_argument("--clickhouse-host", default=None, help="Optional ClickHouse host override.")
    parser.add_argument("--clickhouse-port", type=int, default=None, help="Optional ClickHouse HTTP port override.")

    return parser


def main() -> None:
    """
    Parse CLI arguments and run alert lookup.

    Returns:
        None.
    """
    parser = build_parser()
    args   = parser.parse_args()

    if args.mode == "load":
        alert = load_alert(
            alert_id=args.alert_id,
            alert_key=args.alert_key,
            agent_run_id=args.agent_run_id,
            clickhouse_host=args.clickhouse_host,
            clickhouse_port=args.clickhouse_port,
        )
        print(alert.model_dump_json(indent=2))

        return

    result = list_alerts(
        status=args.status,
        dt=args.dt,
        limit=args.limit,
        agent_run_id=args.agent_run_id,
        clickhouse_host=args.clickhouse_host,
        clickhouse_port=args.clickhouse_port,
    )
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
