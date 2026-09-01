####
## DQ Alert Generator for Agentic Data Quality Triage
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
from uuid import UUID


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Add repo root before importing project packages when this file is executed by path.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipelines.common.clickhouse import build_clickhouse_client, format_date_literal, quote_sql_literal, scalar
from pipelines.common.alert_identity import build_alert_ref
from pipelines.common.alerts import ALERTS_TABLE, AlertCandidate, insert_alert_rows
from pipelines.common.logging import logger
from pipelines.dq.config import OrdersDqContract, load_orders_dq_contract
from pipelines.seeding.helpers import iter_dates, parse_date


# --- Defining Constants
DQ_RESULTS_TABLE = "dq.dq_check_results"


# --- Defining Functions
def parse_details(details_json: str) -> dict[str, Any]:
    """
    Parse a DQ result details_json string safely.

    Args:
        details_json: JSON string stored in dq_check_results.

    Returns:
        Parsed dictionary, or an empty dictionary when parsing fails.
    """
    if not details_json:
        return {}

    try:
        parsed = json.loads(details_json)

    except json.JSONDecodeError:
        logger.warning("Failed to parse DQ details JSON | value=%s", details_json[:200])
        return {}

    return parsed if isinstance(parsed, dict) else {}


def resolve_dimension(check_name: str, details: dict[str, Any]) -> str:
    """
    Resolve a compact dimension label for the alert row.

    Args:
        check_name: DQ check name.
        details: Parsed DQ result details.

    Returns:
        Dimension label, usually a column name, or an empty string for table-level alerts.
    """
    if "column_name" in details:
        return str(details["column_name"])

    if "country" in details and "channel" in details:
        return f"{details['country']}|{details['channel']}"

    if "__" in check_name:
        return check_name.split("__", 1)[1]

    return ""


def build_alert_key(dataset: str, alert_type: str, dt: date, table_name: str, metric: str, dimension: str) -> str:
    """
    Build a stable alert key used to avoid duplicate open alerts.

    Args:
        dataset: Dataset name from the DQ contract.
        alert_type: Alert type from the DQ contract.
        dt: Business date associated with the alert.
        table_name: Affected table name.
        metric: Check name or metric that triggered the alert.
        dimension: Optional dimension label.

    Returns:
        Stable alert key string.
    """
    key_parts = [dataset, alert_type, dt.isoformat(), table_name, metric, dimension or "table"]

    return "|".join(key_parts)


def fetch_latest_bad_results(client: Any, contract: OrdersDqContract, dt: date) -> list[dict[str, Any]]:
    """
    Fetch latest failing or warning DQ results for one business date.

    Args:
        client: clickhouse-connect client instance.
        contract: Validated orders DQ contract.
        dt: Business date to scan.

    Returns:
        List of dictionaries representing latest bad DQ results.
    """
    alert_statuses = sorted(set(contract.alerts.fail_statuses + contract.alerts.warn_statuses))
    status_sql     = ", ".join(quote_sql_literal(status) for status in alert_statuses)

    rows = client.query(
        f"""
        SELECT
            toString(argMax(check_run_id, run_at))      AS check_run_id,
            max(run_at)                                 AS latest_run_at,
            dt,
            table_name,
            check_name,
            argMax(check_type, run_at)                  AS check_type,
            argMax(status, run_at)                      AS result_status,
            argMax(severity, run_at)                    AS result_severity,
            argMax(observed_value, run_at)              AS observed_value,
            argMax(expected_value, run_at)              AS expected_value,
            argMax(threshold_value, run_at)             AS threshold_value,
            argMax(details_json, run_at)                AS details_json,
            argMax(evidence_s3_uri, run_at)             AS evidence_s3_uri
        FROM {DQ_RESULTS_TABLE}
        WHERE dt = {format_date_literal(dt)}
          AND status IN ({status_sql})
        GROUP BY
            dt,
            table_name,
            check_name
        ORDER BY
            result_severity DESC,
            table_name,
            check_name
        """
    ).result_rows

    results = []

    for row in rows:
        results.append(
            {
                "check_run_id": row[0],
                "run_at": row[1],
                "dt": row[2],
                "table_name": row[3],
                "check_name": row[4],
                "check_type": row[5],
                "status": row[6],
                "severity": row[7],
                "observed_value": row[8],
                "expected_value": row[9],
                "threshold_value": row[10],
                "details_json": row[11],
                "evidence_s3_uri": row[12],
            }
        )

    logger.info("Fetched latest bad DQ results | dt=%s rows=%d statuses=%s", dt, len(results), alert_statuses)

    return results


def build_alert_candidates(client: Any, contract: OrdersDqContract, dt: date) -> list[AlertCandidate]:
    """
    Convert latest bad DQ results into alert candidates.

    Args:
        client: clickhouse-connect client instance.
        contract: Validated orders DQ contract.
        dt: Business date to scan.

    Returns:
        List of AlertCandidate objects.
    """
    candidates = []

    for result in fetch_latest_bad_results(client=client, contract=contract, dt=dt):
        details    = parse_details(result["details_json"])
        dimension  = resolve_dimension(check_name=result["check_name"], details=details)
        alert_key  = build_alert_key(
            dataset=contract.dataset,
            alert_type=contract.alerts.alert_type,
            dt=result["dt"],
            table_name=result["table_name"],
            metric=result["check_name"],
            dimension=dimension,
        )
        alert_info = {
            "source_status": result["status"],
            "source_check_type": result["check_type"],
            "source_run_at": result["run_at"],
            "source_details": details,
            "evidence_s3_uri": result["evidence_s3_uri"],
        }

        candidates.append(
            AlertCandidate(
                alert_key=alert_key,
                alert_display_id=build_alert_ref(alert_key=alert_key, dt=result["dt"]),
                status=contract.alerts.open_status,
                alert_type=contract.alerts.alert_type,
                severity=result["severity"],
                table_name=result["table_name"],
                metric=result["check_name"],
                dt=result["dt"],
                dimension=dimension,
                observed_value=result["observed_value"],
                expected_value=result["expected_value"],
                threshold_value=result["threshold_value"],
                source_check_run_id=UUID(result["check_run_id"]),
                details=alert_info,
            )
        )

    logger.info("Built alert candidates | dt=%s candidates=%d", dt, len(candidates))

    return candidates


def open_alert_exists(client: Any, alert_key: str, open_status: str) -> bool:
    """
    Check whether an open alert already exists for a stable alert key.

    Args:
        client: clickhouse-connect client instance.
        alert_key: Stable alert key.
        open_status: Open alert lifecycle status.

    Returns:
        True when an open alert already exists, otherwise False.
    """
    existing_count = scalar(
        client=client,
        query=f"""
            SELECT count()
            FROM {ALERTS_TABLE}
            WHERE alert_key = {quote_sql_literal(alert_key)}
              AND status = {quote_sql_literal(open_status)}
        """,
        default=0,
    )

    return int(existing_count or 0) > 0


def insert_new_alerts(client: Any, candidates: list[AlertCandidate], open_status: str) -> dict[str, Any]:
    """
    Insert alert candidates that do not already have an open alert.

    Args:
        client: clickhouse-connect client instance.
        candidates: Alert candidates to consider.
        open_status: Open alert lifecycle status.

    Returns:
        Summary dictionary with inserted and skipped counts.
    """
    rows_to_insert = []
    skipped_keys   = []

    for candidate in candidates:
        if open_alert_exists(client=client, alert_key=candidate.alert_key, open_status=open_status):
            # Idempotency guard: rerunning alert generation should not create duplicate open alerts.
            logger.info("Open alert already exists; skipping insert | alert_key=%s", candidate.alert_key)
            skipped_keys.append(candidate.alert_key)

            continue

        rows_to_insert.append(candidate)

    inserted = insert_alert_rows(client=client, candidates=rows_to_insert)

    return {
        "inserted": inserted,
        "skipped_existing": len(skipped_keys),
        "skipped_alert_keys": skipped_keys,
    }


def resolve_run_dates(
    dt: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> list[date]:
    """
    Resolve CLI date arguments into an inclusive execution date list.

    Args:
        dt: Optional single business date in YYYY-MM-DD format.
        start: Optional inclusive start date for backfill.
        end: Optional inclusive end date for backfill.

    Returns:
        List of business dates to scan for alerts.

    Raises:
        ValueError: If date arguments are missing or mutually invalid.
    """
    if dt and (start or end):
        raise ValueError("Use either --dt or --start/--end, not both.")

    if dt:
        run_dt = parse_date(dt)
        logger.info("Resolved single-date alert generation run | dt=%s", run_dt)

        return [run_dt]

    if not start or not end:
        raise ValueError("Provide --dt or both --start and --end.")

    return iter_dates(start_date=parse_date(start), end_date=parse_date(end))


def run_alert_generation(
    dates: list[date],
    contract: OrdersDqContract,
    clickhouse_host: str | None = None,
    clickhouse_port: int | None = None,
) -> dict[str, Any]:
    """
    Generate alerts from latest DQ failures and warnings.

    Args:
        dates: Business dates to scan for alert-worthy DQ results.
        contract: Validated orders DQ contract.
        clickhouse_host: Optional ClickHouse host override.
        clickhouse_port: Optional ClickHouse HTTP port override.

    Returns:
        Summary dictionary for alert generation.
    """
    client            = build_clickhouse_client(host=clickhouse_host, port=clickhouse_port)
    started_monotonic = time.monotonic()
    partition_results = []
    total_candidates  = 0
    total_inserted    = 0
    total_skipped     = 0

    logger.info("Starting alert generation | dates=%s", [item.isoformat() for item in dates])

    for run_dt in dates:
        candidates = build_alert_candidates(client=client, contract=contract, dt=run_dt)
        insert_summary = insert_new_alerts(
            client=client,
            candidates=candidates,
            open_status=contract.alerts.open_status,
        )

        total_candidates += len(candidates)
        total_inserted   += insert_summary["inserted"]
        total_skipped    += insert_summary["skipped_existing"]

        partition_results.append(
            {
                "dt": run_dt.isoformat(),
                "candidates": len(candidates),
                "inserted": insert_summary["inserted"],
                "skipped_existing": insert_summary["skipped_existing"],
            }
        )

    duration_ms = int((time.monotonic() - started_monotonic) * 1000)

    summary = {
        "status": "success",
        "partition_count": len(dates),
        "candidates": total_candidates,
        "inserted": total_inserted,
        "skipped_existing": total_skipped,
        "duration_ms": duration_ms,
        "partitions": partition_results,
    }

    logger.info("Alert generation completed | summary=%s", summary)

    return summary


def build_parser() -> argparse.ArgumentParser:
    """
    Build the command-line parser for alert generation.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(description="Generate ClickHouse alerts from latest DQ failures and warnings.")

    parser.add_argument("--dt", default=None, help="Single business date to scan, in YYYY-MM-DD format.")
    parser.add_argument("--start", default=None, help="Inclusive start date for backfill, in YYYY-MM-DD format.")
    parser.add_argument("--end", default=None, help="Inclusive end date for backfill, in YYYY-MM-DD format.")
    parser.add_argument("--contract", default=None, help="Optional path to orders DQ contract YAML.")
    parser.add_argument("--clickhouse-host", default=None, help="Optional ClickHouse host override.")
    parser.add_argument("--clickhouse-port", type=int, default=None, help="Optional ClickHouse HTTP port override.")

    return parser


def main() -> None:
    """
    Parse CLI arguments and generate alerts from DQ results.

    Returns:
        None.
    """
    parser = build_parser()
    args   = parser.parse_args()

    try:
        dates = resolve_run_dates(dt=args.dt, start=args.start, end=args.end)

    except ValueError as exc:
        parser.error(str(exc))

    contract = load_orders_dq_contract(args.contract)
    summary  = run_alert_generation(
        dates=dates,
        contract=contract,
        clickhouse_host=args.clickhouse_host,
        clickhouse_port=args.clickhouse_port,
    )

    print(json.dumps(summary, indent=2, default=str))


# --- Running CLI Entrypoint
if __name__ == "__main__":
    main()

