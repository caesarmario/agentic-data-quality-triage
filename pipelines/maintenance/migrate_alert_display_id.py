####
## Alert Display ID Migration for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Add repo root before importing project packages when this file is executed by path.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipelines.common.alert_identity import build_alert_ref
from pipelines.common.clickhouse import build_clickhouse_client, quote_sql_literal, scalar
from pipelines.common.logging import logger


# --- Defining Constants
TARGET_DATABASE = "dq"
TARGET_TABLE    = "alerts"
TARGET_COLUMN   = "alert_display_id"


# --- Defining SQL Helpers
def alerts_table_exists(client: Any) -> bool:
    """
    Check whether the dq.alerts table exists.

    Args:
        client: clickhouse-connect client instance.

    Returns:
        True when dq.alerts exists, otherwise False.
    """
    table_count = scalar(
        client=client,
        query=f"""
            SELECT count()
            FROM system.tables
            WHERE database = {quote_sql_literal(TARGET_DATABASE)}
              AND name = {quote_sql_literal(TARGET_TABLE)}
        """,
        default=0,
    )

    return int(table_count or 0) > 0


def alert_display_id_column_exists(client: Any) -> bool:
    """
    Check whether dq.alerts already has alert_display_id.

    Args:
        client: clickhouse-connect client instance.

    Returns:
        True when the column exists, otherwise False.
    """
    column_count = scalar(
        client=client,
        query=f"""
            SELECT count()
            FROM system.columns
            WHERE database = {quote_sql_literal(TARGET_DATABASE)}
              AND table = {quote_sql_literal(TARGET_TABLE)}
              AND name = {quote_sql_literal(TARGET_COLUMN)}
        """,
        default=0,
    )

    exists = int(column_count or 0) > 0
    logger.info("Checked alert display id column existence | exists=%s", exists)

    return exists


def add_alert_display_id_column(client: Any, dry_run: bool = False) -> bool:
    """
    Add alert_display_id to dq.alerts when missing.

    Args:
        client: clickhouse-connect client instance.
        dry_run: When true, do not mutate ClickHouse.

    Returns:
        True when the column was added, otherwise False.
    """
    if alert_display_id_column_exists(client):
        return False

    sql = f"""
        ALTER TABLE {TARGET_DATABASE}.{TARGET_TABLE}
        ADD COLUMN IF NOT EXISTS {TARGET_COLUMN} String DEFAULT '' AFTER alert_key
    """

    if dry_run:
        logger.info("Dry-run would add alert display id column | sql=%s", sql.strip())
        return True

    logger.info("Adding alert display id column | table=%s.%s", TARGET_DATABASE, TARGET_TABLE)
    client.command(sql)

    return True


def fetch_rows_missing_display_id(client: Any, limit: int) -> list[dict[str, Any]]:
    """
    Fetch alert rows that need a human-facing display id.

    Args:
        client: clickhouse-connect client instance.
        limit: Maximum rows to inspect.

    Returns:
        List of alert rows with alert_key and dt.
    """
    safe_limit = max(1, min(limit, 10_000))
    result     = client.query(
        f"""
        SELECT
            alert_key,
            dt
        FROM {TARGET_DATABASE}.{TARGET_TABLE}
        WHERE {TARGET_COLUMN} = ''
        LIMIT {safe_limit}
        """
    )
    columns    = list(result.column_names or [])
    rows       = [dict(zip(columns, row)) for row in result.result_rows]

    logger.info("Fetched alerts missing display ids | rows=%d", len(rows))

    return rows


def update_alert_display_id(client: Any, alert_key: str, display_id: str, dry_run: bool = False) -> None:
    """
    Update one alert row with a deterministic human-facing display id.

    Args:
        client: clickhouse-connect client instance.
        alert_key: Internal stable alert key.
        display_id: Human-facing display id.
        dry_run: When true, do not mutate ClickHouse.

    Returns:
        None.
    """
    sql = f"""
        ALTER TABLE {TARGET_DATABASE}.{TARGET_TABLE}
        UPDATE {TARGET_COLUMN} = {quote_sql_literal(display_id)}
        WHERE alert_key = {quote_sql_literal(alert_key)}
          AND {TARGET_COLUMN} = ''
    """

    if dry_run:
        logger.info("Dry-run would update alert display id | display_id=%s alert_key=%s", display_id, alert_key)
        return

    client.command(sql)
    logger.info("Updated alert display id | display_id=%s alert_key=%s", display_id, alert_key)


# --- Defining Migration Runner
def migrate_alert_display_id(
    clickhouse_host: str | None = None,
    clickhouse_port: int | None = None,
    dry_run: bool = False,
    limit: int = 10_000,
) -> dict[str, Any]:
    """
    Add and backfill alert_display_id for dq.alerts.

    Args:
        clickhouse_host: Optional ClickHouse host override.
        clickhouse_port: Optional ClickHouse HTTP port override.
        dry_run: When true, only report intended changes.
        limit: Maximum existing rows to backfill.

    Returns:
        Migration summary dictionary.
    """
    client            = build_clickhouse_client(host=clickhouse_host, port=clickhouse_port)
    started_monotonic = time.monotonic()

    if not alerts_table_exists(client):
        summary = {
            "status": "skipped",
            "reason": "alerts_table_missing_run_clickhouse_bootstrap_first",
        }
        logger.warning("Alert display id migration skipped | summary=%s", summary)

        return summary

    column_added = add_alert_display_id_column(client=client, dry_run=dry_run)
    rows         = [] if dry_run and column_added else fetch_rows_missing_display_id(client=client, limit=limit)
    updates      = []

    for row in rows:
        alert_key  = str(row.get("alert_key") or "")
        display_id = build_alert_ref(alert_key=alert_key, dt=row.get("dt"))

        update_alert_display_id(client=client, alert_key=alert_key, display_id=display_id, dry_run=dry_run)
        updates.append({"alert_key": alert_key, "alert_display_id": display_id})

    summary = {
        "status": "dry_run" if dry_run else "success",
        "column_added": column_added,
        "updated_rows": len(updates),
        "sample_updates": updates[:10],
        "duration_ms": int((time.monotonic() - started_monotonic) * 1000),
    }

    logger.info("Alert display id migration completed | summary=%s", summary)

    return summary


# --- Defining CLI
def build_parser() -> argparse.ArgumentParser:
    """
    Build CLI parser for the alert display id migration.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(description="Add and backfill dq.alerts.alert_display_id.")

    parser.add_argument("--clickhouse-host", default=None, help="Optional ClickHouse host override.")
    parser.add_argument("--clickhouse-port", type=int, default=None, help="Optional ClickHouse HTTP port override.")
    parser.add_argument("--dry-run", action="store_true", help="Only report intended changes.")
    parser.add_argument("--limit", type=int, default=10_000, help="Maximum existing alerts to backfill.")

    return parser


def main() -> None:
    """
    Parse CLI arguments and run the alert display id migration.

    Returns:
        None.
    """
    parser  = build_parser()
    args    = parser.parse_args()
    summary = migrate_alert_display_id(
        clickhouse_host=args.clickhouse_host,
        clickhouse_port=args.clickhouse_port,
        dry_run=args.dry_run,
        limit=args.limit,
    )

    print(json.dumps(summary, indent=2, default=str))


# --- Running CLI Entrypoint
if __name__ == "__main__":
    main()
