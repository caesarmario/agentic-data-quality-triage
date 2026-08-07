####
## Alerts Lifecycle Migration for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Add repo root before importing project packages when this file is executed by path.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipelines.common.clickhouse import build_clickhouse_client, quote_sql_literal, scalar
from pipelines.common.logging import logger


# --- Defining Constants
TARGET_DATABASE        = "dq"
TARGET_TABLE           = "alerts"
STAGING_TABLE          = "alerts_lifecycle_migrated"
EXPECTED_SORTING_KEY   = "alert_key"


# --- Defining SQL Templates
CREATE_ALERTS_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TARGET_DATABASE}.{STAGING_TABLE}
(
    alert_id              UUID DEFAULT generateUUIDv4(),
    alert_key             String,
    alert_display_id      String DEFAULT '',
    created_at            DateTime64(3, 'UTC') DEFAULT now64(3),
    updated_at            DateTime64(3, 'UTC') DEFAULT now64(3),
    status                LowCardinality(String) DEFAULT 'open',
    alert_type            LowCardinality(String),
    severity              LowCardinality(String),
    table_name            LowCardinality(String),
    metric                String,
    dt                    Nullable(Date),
    dimension             String DEFAULT '',
    observed_value        Nullable(Float64),
    expected_value        Nullable(Float64),
    threshold_value       Nullable(Float64),
    source_check_run_id   Nullable(UUID),
    details_json          String DEFAULT '{{}}',
    report_s3_uri         String DEFAULT '',
    acknowledged_by       String DEFAULT '',
    resolved_at           Nullable(DateTime64(3, 'UTC'))
)
ENGINE = ReplacingMergeTree(updated_at)
PARTITION BY toYYYYMM(created_at)
ORDER BY (alert_key)
"""


# --- Defining Functions
def alerts_table_exists(client: Any) -> bool:
    """
    Check whether dq.alerts exists before attempting a lifecycle migration.

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
    exists = int(table_count or 0) > 0

    logger.info("Checked alerts table existence | exists=%s", exists)

    return exists


def get_alerts_sorting_key(client: Any) -> str:
    """
    Read the current sorting key for dq.alerts.

    Args:
        client: clickhouse-connect client instance.

    Returns:
        Current sorting key expression, or an empty string when the table does not exist.
    """
    sorting_key = scalar(
        client=client,
        query=f"""
            SELECT sorting_key
            FROM system.tables
            WHERE database = {quote_sql_literal(TARGET_DATABASE)}
              AND name = {quote_sql_literal(TARGET_TABLE)}
        """,
        default="",
    )

    resolved_key = str(sorting_key or "")
    logger.info("Current alerts sorting key resolved | sorting_key=%s", resolved_key)

    return resolved_key


def needs_migration(sorting_key: str) -> bool:
    """
    Decide whether dq.alerts needs lifecycle migration.

    Args:
        sorting_key: Current sorting key from system.tables.

    Returns:
        True when migration is required.
    """
    normalized = sorting_key.strip().replace("`", "")

    return normalized != EXPECTED_SORTING_KEY


def build_backup_table_name(now: datetime | None = None) -> str:
    """
    Build a unique backup table name for the existing alerts table.

    Args:
        now: Optional UTC timestamp override for deterministic tests.

    Returns:
        Backup table name without database prefix.
    """
    resolved_now = now or datetime.now(timezone.utc)

    return f"alerts_legacy_{resolved_now.strftime('%Y%m%d_%H%M%S')}"


def migrate_alerts_lifecycle_table(
    clickhouse_host: str | None = None,
    clickhouse_port: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Migrate dq.alerts so alert status/report URI can be updated after triage.

    Args:
        clickhouse_host: Optional ClickHouse host override.
        clickhouse_port: Optional ClickHouse HTTP port override.
        dry_run: When true, only report whether migration is needed.

    Returns:
        Migration summary dictionary.
    """
    client             = build_clickhouse_client(host=clickhouse_host, port=clickhouse_port)
    started_monotonic  = time.monotonic()

    if not alerts_table_exists(client):
        summary = {
            "status": "skipped",
            "reason": "alerts_table_missing_run_clickhouse_bootstrap_first",
            "duration_ms": int((time.monotonic() - started_monotonic) * 1000),
        }
        logger.warning("Alerts lifecycle migration skipped because dq.alerts is missing | summary=%s", summary)

        return summary

    current_sorting_key = get_alerts_sorting_key(client)
    migration_needed    = needs_migration(current_sorting_key)

    if not migration_needed:
        summary = {
            "status": "skipped",
            "reason": "alerts_table_already_uses_lifecycle_sorting_key",
            "sorting_key": current_sorting_key,
            "duration_ms": int((time.monotonic() - started_monotonic) * 1000),
        }
        logger.info("Alerts lifecycle migration skipped | summary=%s", summary)

        return summary

    backup_table = build_backup_table_name()

    if dry_run:
        summary = {
            "status": "dry_run",
            "current_sorting_key": current_sorting_key,
            "expected_sorting_key": EXPECTED_SORTING_KEY,
            "backup_table": f"{TARGET_DATABASE}.{backup_table}",
            "staging_table": f"{TARGET_DATABASE}.{STAGING_TABLE}",
        }
        logger.info("Alerts lifecycle migration dry-run completed | summary=%s", summary)

        return summary

    logger.info(
        "Migrating alerts lifecycle table | current_sorting_key=%s backup_table=%s",
        current_sorting_key,
        backup_table,
    )

    # Drop only the staging table. The original alerts table is preserved under a timestamped backup name.
    client.command(f"DROP TABLE IF EXISTS {TARGET_DATABASE}.{STAGING_TABLE}")
    client.command(CREATE_ALERTS_TABLE_SQL)
    client.command(f"INSERT INTO {TARGET_DATABASE}.{STAGING_TABLE} SELECT * FROM {TARGET_DATABASE}.{TARGET_TABLE}")
    client.command(
        f"""
        RENAME TABLE
            {TARGET_DATABASE}.{TARGET_TABLE} TO {TARGET_DATABASE}.{backup_table},
            {TARGET_DATABASE}.{STAGING_TABLE} TO {TARGET_DATABASE}.{TARGET_TABLE}
        """
    )

    summary = {
        "status": "success",
        "previous_sorting_key": current_sorting_key,
        "new_sorting_key": EXPECTED_SORTING_KEY,
        "backup_table": f"{TARGET_DATABASE}.{backup_table}",
        "duration_ms": int((time.monotonic() - started_monotonic) * 1000),
    }

    logger.info("Alerts lifecycle migration completed | summary=%s", summary)

    return summary


def build_parser() -> argparse.ArgumentParser:
    """
    Build CLI parser for alerts lifecycle migration.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(description="Migrate dq.alerts so triage can update alert lifecycle fields.")

    parser.add_argument("--clickhouse-host", default=None, help="Optional ClickHouse host override.")
    parser.add_argument("--clickhouse-port", type=int, default=None, help="Optional ClickHouse HTTP port override.")
    parser.add_argument("--dry-run", action="store_true", help="Only report whether migration is required.")

    return parser


def main() -> None:
    """
    Parse CLI arguments and run alerts lifecycle migration.

    Returns:
        None.
    """
    parser = build_parser()
    args   = parser.parse_args()

    summary = migrate_alerts_lifecycle_table(
        clickhouse_host=args.clickhouse_host,
        clickhouse_port=args.clickhouse_port,
        dry_run=args.dry_run,
    )

    print(json.dumps(summary, indent=2, default=str))


# --- Running CLI Entrypoint
if __name__ == "__main__":
    main()
