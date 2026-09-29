####
## Bounded Quality Summary Tool for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Build deterministic weekly, table, and warehouse quality summaries."""

# --- Importing Libraries
from __future__ import annotations

import re
import time
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from agent.tools.audit_log import write_agent_audit_event
from agent.tools.clickhouse_sql import rows_to_dicts
from pipelines.common.clickhouse import build_clickhouse_client, format_date_literal, quote_sql_literal
from pipelines.common.logging import logger
from pipelines.seeding.helpers import parse_date


# --- Defining Constants
TOOL_NAME                 = "quality_summary"
DQ_CHECK_RESULTS_TABLE    = "dq.dq_check_results"
ALERTS_TABLE              = "dq.alerts"
METADATA_ASSETS_TABLE     = "dq.metadata_assets"
OPEN_ALERT_STATUS         = "open"
WEEKLY_LOOKBACK_DAYS      = 7
MAX_SUMMARY_DAYS          = 31
MAX_SUMMARY_GROUPS        = 1_000
QUALITY_SUMMARY_SCOPES    = {"weekly", "table", "database"}
QUALIFIED_TABLE_PATTERN   = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*$")

RERUN_DAG_BY_TABLE = {
    "dq.raw_orders": "10_dag_dq_orders_landing_orchestrator",
    "dq.stg_orders": "20_dag_dq_orders_dbt_transform",
    "dq.fct_orders_daily": "20_dag_dq_orders_dbt_transform",
}


# --- Defining Validation Helpers
def normalize_quality_summary_request(
    scope: str,
    start_date: str,
    end_date: str,
    table_name: str | None = None,
) -> tuple[str, str, str, str]:
    """Validate and normalize one bounded summary request."""
    normalized_scope = str(scope or "").strip().lower()

    if normalized_scope not in QUALITY_SUMMARY_SCOPES:
        allowed = ", ".join(sorted(QUALITY_SUMMARY_SCOPES))
        raise ValueError(f"Quality summary scope must be one of: {allowed}.")

    normalized_start = parse_date(start_date)
    normalized_end   = parse_date(end_date)

    if normalized_start > normalized_end:
        raise ValueError("Quality summary start_date cannot be after end_date.")

    inclusive_days = (normalized_end - normalized_start).days + 1

    if inclusive_days > MAX_SUMMARY_DAYS:
        raise ValueError(f"Quality summary date range cannot exceed {MAX_SUMMARY_DAYS} days.")

    normalized_table = str(table_name or "").strip()

    if normalized_scope == "table":
        if not QUALIFIED_TABLE_PATTERN.fullmatch(normalized_table):
            raise ValueError("Table summary requires a qualified schema.table name.")

    elif normalized_table:
        raise ValueError("table_name is allowed only for table summaries.")

    return (
        normalized_scope,
        normalized_start.isoformat(),
        normalized_end.isoformat(),
        normalized_table,
    )


# --- Defining Query Helpers
def build_quality_summary_sql(
    scope: str,
    start_date: str,
    end_date: str,
    table_name: str | None = None,
) -> str:
    """Build one bounded aggregation query for a quality summary."""
    normalized_scope, normalized_start, normalized_end, normalized_table = normalize_quality_summary_request(
        scope=scope,
        start_date=start_date,
        end_date=end_date,
        table_name=table_name,
    )
    start_literal = format_date_literal(parse_date(normalized_start))
    end_literal   = format_date_literal(parse_date(normalized_end))
    table_filter  = (
        f" AND table_name = {quote_sql_literal(normalized_table)}"
        if normalized_scope == "table"
        else ""
    )
    metadata_filter = (
        f" AND qualified_name = {quote_sql_literal(normalized_table)}"
        if normalized_scope == "table"
        else ""
    )

    return f"""
        SELECT
            category,
            label,
            entity,
            bucket_date,
            count
        FROM
        (
            SELECT
                'check' AS category,
                toString(status) AS label,
                '' AS entity,
                CAST(NULL AS Nullable(Date)) AS bucket_date,
                count() AS count
            FROM {DQ_CHECK_RESULTS_TABLE}
            WHERE dt BETWEEN {start_literal} AND {end_literal}{table_filter}
            GROUP BY status

            UNION ALL

            SELECT
                'alert' AS category,
                toString(severity) AS label,
                '' AS entity,
                CAST(NULL AS Nullable(Date)) AS bucket_date,
                count() AS count
            FROM {ALERTS_TABLE} FINAL
            WHERE dt BETWEEN {start_literal} AND {end_literal}
              AND status = {quote_sql_literal(OPEN_ALERT_STATUS)}{table_filter}
            GROUP BY severity

            UNION ALL

            SELECT
                'daily_check' AS category,
                toString(status) AS label,
                '' AS entity,
                dt AS bucket_date,
                count() AS count
            FROM {DQ_CHECK_RESULTS_TABLE}
            WHERE dt BETWEEN {start_literal} AND {end_literal}{table_filter}
            GROUP BY dt, status

            UNION ALL

            SELECT
                'daily_alert' AS category,
                toString(severity) AS label,
                '' AS entity,
                dt AS bucket_date,
                count() AS count
            FROM {ALERTS_TABLE} FINAL
            WHERE dt BETWEEN {start_literal} AND {end_literal}
              AND status = {quote_sql_literal(OPEN_ALERT_STATUS)}{table_filter}
            GROUP BY dt, severity

            UNION ALL

            SELECT
                'table_check' AS category,
                toString(status) AS label,
                toString(table_name) AS entity,
                CAST(NULL AS Nullable(Date)) AS bucket_date,
                count() AS count
            FROM {DQ_CHECK_RESULTS_TABLE}
            WHERE dt BETWEEN {start_literal} AND {end_literal}{table_filter}
            GROUP BY table_name, status

            UNION ALL

            SELECT
                'table_alert' AS category,
                toString(severity) AS label,
                toString(table_name) AS entity,
                CAST(NULL AS Nullable(Date)) AS bucket_date,
                count() AS count
            FROM {ALERTS_TABLE} FINAL
            WHERE dt BETWEEN {start_literal} AND {end_literal}
              AND status = {quote_sql_literal(OPEN_ALERT_STATUS)}{table_filter}
            GROUP BY table_name, severity

            UNION ALL

            SELECT
                'metadata' AS category,
                toString(certification_status) AS label,
                '' AS entity,
                CAST(NULL AS Nullable(Date)) AS bucket_date,
                count() AS count
            FROM {METADATA_ASSETS_TABLE} FINAL
            WHERE is_active = 1{metadata_filter}
            GROUP BY certification_status
        )
        ORDER BY category, bucket_date, entity, label
        LIMIT {MAX_SUMMARY_GROUPS}
    """


def query_quality_summary_rows(
    client: Any,
    scope: str,
    start_date: str,
    end_date: str,
    table_name: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Execute one bounded quality-summary aggregation."""
    sql     = build_quality_summary_sql(scope, start_date, end_date, table_name)
    result  = client.query(sql)
    columns = list(result.column_names or [])
    rows    = rows_to_dicts(columns=columns, rows=result.result_rows)

    logger.info(
        "Queried bounded quality summary | scope=%s start=%s end=%s table=%s groups=%d",
        scope,
        start_date,
        end_date,
        table_name or "all",
        len(rows),
    )

    return sql, rows


# --- Defining Payload Helpers
def empty_health_bucket(identity_field: str, identity: str) -> dict[str, Any]:
    """Create one zeroed daily or table health bucket."""
    return {
        identity_field: identity,
        "total_checks": 0,
        "failed_checks": 0,
        "warning_checks": 0,
        "open_alerts": 0,
        "critical_alerts": 0,
    }


def apply_health_count(bucket: dict[str, Any], category: str, label: str, count: int) -> None:
    """Apply one validated aggregate to a daily or table health bucket."""
    if category.endswith("check"):
        bucket["total_checks"] += count

        if label == "fail":
            bucket["failed_checks"] += count

        elif label == "warn":
            bucket["warning_checks"] += count

    else:
        bucket["open_alerts"] += count

        if label == "critical":
            bucket["critical_alerts"] += count


def build_manual_rerun_suggestions(
    table_counts: list[dict[str, Any]],
    start_date: str,
    end_date: str,
) -> list[dict[str, Any]]:
    """Build allowlisted, advisory-only rerun suggestions from observed failures."""
    suggestions_by_dag: dict[str, dict[str, Any]] = {}

    for table in table_counts:
        if int(table.get("failed_checks") or 0) <= 0 and int(table.get("critical_alerts") or 0) <= 0:
            continue

        table_name    = str(table.get("table_name") or "")
        target_dag_id = RERUN_DAG_BY_TABLE.get(table_name)

        if not target_dag_id:
            continue

        suggestion = suggestions_by_dag.setdefault(
            target_dag_id,
            {
                "target_dag_id": target_dag_id,
                "affected_tables": [],
                "start_date": start_date,
                "end_date": end_date,
                "mode": "advisory_only",
                "requires_approval": True,
                "reason": "Observed failed checks or critical alerts require evidence review before a bounded rerun.",
            },
        )
        suggestion["affected_tables"].append(table_name)

    return [
        {**suggestion, "affected_tables": sorted(set(suggestion["affected_tables"]))}
        for _, suggestion in sorted(suggestions_by_dag.items())
    ]


def build_quality_summary_payload(
    scope: str,
    start_date: str,
    end_date: str,
    rows: list[dict[str, Any]],
    table_name: str | None = None,
    duration_ms: int = 0,
) -> dict[str, Any]:
    """Convert generic aggregation rows into the public summary contract."""
    normalized_scope, normalized_start, normalized_end, normalized_table = normalize_quality_summary_request(
        scope=scope,
        start_date=start_date,
        end_date=end_date,
        table_name=table_name,
    )
    check_counts: dict[str, int]    = {}
    alert_counts: dict[str, int]    = {}
    metadata_counts: dict[str, int] = {}
    daily_buckets: dict[str, dict[str, Any]] = {}
    table_buckets: dict[str, dict[str, Any]] = {}
    valid_categories = {
        "check",
        "alert",
        "daily_check",
        "daily_alert",
        "table_check",
        "table_alert",
        "metadata",
    }

    for row in rows:
        category = str(row.get("category") or "").strip().lower()
        label    = str(row.get("label") or "").strip().lower()
        entity   = str(row.get("entity") or "").strip()
        count    = row.get("count")

        if category not in valid_categories:
            raise ValueError(f"Quality summary returned an unknown category: {category}")

        if not label:
            raise ValueError("Quality summary returned a blank label.")

        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("Quality summary returned an invalid count.")

        if category == "check":
            check_counts[label] = check_counts.get(label, 0) + count

        elif category == "alert":
            alert_counts[label] = alert_counts.get(label, 0) + count

        elif category == "metadata":
            metadata_counts[label] = metadata_counts.get(label, 0) + count

        elif category.startswith("daily_"):
            bucket_date = row.get("bucket_date")

            if bucket_date in (None, ""):
                raise ValueError("Daily quality summary row is missing bucket_date.")

            normalized_bucket_date = parse_date(str(bucket_date)).isoformat()
            bucket = daily_buckets.setdefault(
                normalized_bucket_date,
                empty_health_bucket("dt", normalized_bucket_date),
            )
            apply_health_count(bucket, category, label, count)

        else:
            if not QUALIFIED_TABLE_PATTERN.fullmatch(entity):
                raise ValueError("Table quality summary row contains an invalid table name.")

            bucket = table_buckets.setdefault(entity, empty_health_bucket("table_name", entity))
            apply_health_count(bucket, category, label, count)

    normalized_check_counts = [
        {"status": label, "count": count}
        for label, count in sorted(check_counts.items())
    ]
    normalized_alert_counts = [
        {"severity": label, "count": count}
        for label, count in sorted(alert_counts.items())
    ]
    normalized_metadata_counts = [
        {"certification_status": label, "count": count}
        for label, count in sorted(metadata_counts.items())
    ]
    daily_counts = [daily_buckets[key] for key in sorted(daily_buckets)]
    table_counts = [table_buckets[key] for key in sorted(table_buckets)]
    suggestions  = build_manual_rerun_suggestions(
        table_counts=table_counts,
        start_date=normalized_start,
        end_date=normalized_end,
    )
    total_checks      = sum(item["count"] for item in normalized_check_counts)
    total_open_alerts = sum(item["count"] for item in normalized_alert_counts)
    registered_assets = sum(item["count"] for item in normalized_metadata_counts)
    scope_label       = normalized_table if normalized_scope == "table" else normalized_scope

    return {
        "status": "success",
        "scope": normalized_scope,
        "start_date": normalized_start,
        "end_date": normalized_end,
        "table_name": normalized_table,
        "check_counts": normalized_check_counts,
        "alert_counts": normalized_alert_counts,
        "daily_counts": daily_counts,
        "table_counts": table_counts,
        "metadata_counts": normalized_metadata_counts,
        "total_checks": total_checks,
        "total_open_alerts": total_open_alerts,
        "registered_asset_count": registered_assets,
        "rerun_suggestions": suggestions,
        "duration_ms": max(0, int(duration_ms)),
        "summary": (
            f"Quality summary for {scope_label} from {normalized_start} through {normalized_end}: "
            f"{total_checks} check result(s), {total_open_alerts} open alert(s), "
            f"and {len(suggestions)} advisory rerun suggestion(s)."
        ),
    }


# --- Defining Public Tool Functions
def fetch_quality_summary(
    scope: str,
    start_date: str,
    end_date: str,
    table_name: str | None = None,
    agent_run_id: UUID | str | None = None,
    clickhouse_host: str | None = None,
    clickhouse_port: int | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    """Fetch one deterministic, audited quality summary."""
    normalized_scope, normalized_start, normalized_end, normalized_table = normalize_quality_summary_request(
        scope=scope,
        start_date=start_date,
        end_date=end_date,
        table_name=table_name,
    )
    resolved_client       = client or build_clickhouse_client(host=clickhouse_host, port=clickhouse_port)
    resolved_agent_run_id = UUID(str(agent_run_id)) if agent_run_id else uuid4()
    started_monotonic     = time.monotonic()

    try:
        sql, rows   = query_quality_summary_rows(
            client=resolved_client,
            scope=normalized_scope,
            start_date=normalized_start,
            end_date=normalized_end,
            table_name=normalized_table or None,
        )
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)
        payload     = build_quality_summary_payload(
            scope=normalized_scope,
            start_date=normalized_start,
            end_date=normalized_end,
            table_name=normalized_table or None,
            rows=rows,
            duration_ms=duration_ms,
        )
        payload["sql"] = sql

        write_agent_audit_event(
            client=resolved_client,
            action="fetch_quality_summary",
            status="success",
            agent_run_id=resolved_agent_run_id,
            tool_name=TOOL_NAME,
            duration_ms=duration_ms,
            input_payload={
                "scope": normalized_scope,
                "start_date": normalized_start,
                "end_date": normalized_end,
                "table_name": normalized_table,
            },
            output_payload={
                "total_checks": payload["total_checks"],
                "total_open_alerts": payload["total_open_alerts"],
                "registered_asset_count": payload["registered_asset_count"],
                "rerun_suggestion_count": len(payload["rerun_suggestions"]),
            },
            sql=sql,
            row_count=len(rows),
        )

        return payload

    except Exception as exc:
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)
        logger.exception(
            "Failed to fetch bounded quality summary | scope=%s start=%s end=%s table=%s",
            normalized_scope,
            normalized_start,
            normalized_end,
            normalized_table or "all",
        )
        write_agent_audit_event(
            client=resolved_client,
            action="fetch_quality_summary",
            status="failed",
            agent_run_id=resolved_agent_run_id,
            tool_name=TOOL_NAME,
            duration_ms=duration_ms,
            input_payload={
                "scope": normalized_scope,
                "start_date": normalized_start,
                "end_date": normalized_end,
                "table_name": normalized_table,
            },
            output_payload={"error_type": type(exc).__name__},
            error_message=str(exc),
        )
        raise


def fetch_weekly_quality_summary(
    end_date: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Fetch the seven-day quality window ending on end_date."""
    normalized_end   = parse_date(end_date)
    normalized_start = normalized_end - timedelta(days=WEEKLY_LOOKBACK_DAYS - 1)

    return fetch_quality_summary(
        scope="weekly",
        start_date=normalized_start.isoformat(),
        end_date=normalized_end.isoformat(),
        **kwargs,
    )


def fetch_table_quality_summary(
    table_name: str,
    start_date: str,
    end_date: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Fetch one bounded per-table quality summary."""
    return fetch_quality_summary(
        scope="table",
        start_date=start_date,
        end_date=end_date,
        table_name=table_name,
        **kwargs,
    )


def fetch_database_quality_summary(
    start_date: str,
    end_date: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Fetch a bounded warehouse-wide quality summary."""
    return fetch_quality_summary(
        scope="database",
        start_date=start_date,
        end_date=end_date,
        **kwargs,
    )
