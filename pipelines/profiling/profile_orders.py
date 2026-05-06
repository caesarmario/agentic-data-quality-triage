####
## Orders Data Profiler for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Add repo root before importing project packages when this file is executed by path.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipelines.common.clickhouse import (
    build_clickhouse_client,
    format_date_literal,
    quote_sql_literal,
    scalar,
    validate_column_name,
    validate_qualified_table_name,
)
from pipelines.common.logging import logger
from pipelines.dq.config import OrdersDqContract, load_orders_dq_contract
from pipelines.seeding.helpers import iter_dates, parse_date


PROFILE_RESULTS_TABLE = "dq.data_profile_results"

PROFILE_RESULT_COLUMNS = [
    "profile_run_id",
    "run_at",
    "dt",
    "table_name",
    "column_name",
    "metric_name",
    "metric_value",
    "metric_unit",
    "details_json",
]


@dataclass(frozen=True)
class ProfileMetric:
    """
    One data profiling metric ready to be written to ClickHouse.

    Attributes:
        profile_run_id: Shared profile run UUID for all metrics in one execution.
        run_at: UTC timestamp when profiling started.
        dt: Business date being profiled.
        table_name: Fully qualified source table name.
        column_name: Column name related to the metric, or an empty string for table-level metrics.
        metric_name: Stable metric name.
        metric_value: Numeric metric value.
        metric_unit: Metric unit such as rows, rate, days, segments, or usd.
        details: Additional context stored as JSON.
    """

    profile_run_id: str
    run_at: datetime
    dt: date
    table_name: str
    column_name: str
    metric_name: str
    metric_value: float
    metric_unit: str
    details: dict[str, Any]

    def as_insert_row(self) -> list[Any]:
        """
        Convert the profile metric into a ClickHouse insert row.

        Returns:
            Ordered row matching PROFILE_RESULT_COLUMNS.
        """
        return [
            self.profile_run_id,
            self.run_at,
            self.dt,
            self.table_name,
            self.column_name,
            self.metric_name,
            self.metric_value,
            self.metric_unit,
            json.dumps(self.details, default=str),
        ]


def build_metric(
    profile_run_id: str,
    run_at: datetime,
    dt: date,
    table_name: str,
    metric_name: str,
    metric_value: float,
    metric_unit: str,
    column_name: str = "",
    details: dict[str, Any] | None = None,
) -> ProfileMetric:
    """
    Build a typed profile metric with validated table and column identifiers.

    Args:
        profile_run_id: Shared profile run UUID.
        run_at: UTC timestamp for the profile run.
        dt: Business date being profiled.
        table_name: Fully qualified ClickHouse table name.
        metric_name: Stable metric name.
        metric_value: Numeric metric value.
        metric_unit: Unit label for the metric.
        column_name: Optional column name for column-level metrics.
        details: Optional structured details stored as JSON.

    Returns:
        ProfileMetric instance ready for insertion.
    """
    validate_qualified_table_name(table_name)

    if column_name:
        validate_column_name(column_name)

    return ProfileMetric(
        profile_run_id=profile_run_id,
        run_at=run_at,
        dt=dt,
        table_name=table_name,
        column_name=column_name,
        metric_name=metric_name,
        metric_value=float(metric_value),
        metric_unit=metric_unit,
        details=details or {},
    )


def profile_row_count(
    client: Any,
    table_name: str,
    dt: date,
    profile_run_id: str,
    run_at: datetime,
) -> ProfileMetric:
    """
    Profile daily row count for one ClickHouse table.

    Args:
        client: clickhouse-connect client instance.
        table_name: Fully qualified table name.
        dt: Business date being profiled.
        profile_run_id: Shared profile run UUID.
        run_at: UTC timestamp for the profile run.

    Returns:
        ProfileMetric containing row count for the target date.
    """
    validate_qualified_table_name(table_name)

    row_count = scalar(
        client=client,
        query=f"""
            SELECT count()
            FROM {table_name}
            WHERE dt = {format_date_literal(dt)}
        """,
        default=0,
    )

    logger.info("Profiled row count | table=%s dt=%s rows=%s", table_name, dt, row_count)

    return build_metric(
        profile_run_id=profile_run_id,
        run_at=run_at,
        dt=dt,
        table_name=table_name,
        metric_name="row_count",
        metric_value=float(row_count or 0),
        metric_unit="rows",
    )


def profile_distinct_count(
    client: Any,
    table_name: str,
    column_name: str,
    dt: date,
    profile_run_id: str,
    run_at: datetime,
) -> ProfileMetric:
    """
    Profile daily distinct count for one column.

    Args:
        client: clickhouse-connect client instance.
        table_name: Fully qualified table name.
        column_name: Column to profile.
        dt: Business date being profiled.
        profile_run_id: Shared profile run UUID.
        run_at: UTC timestamp for the profile run.

    Returns:
        ProfileMetric containing the column distinct count.
    """
    validate_qualified_table_name(table_name)
    validate_column_name(column_name)

    distinct_count = scalar(
        client=client,
        query=f"""
            SELECT uniqExact({column_name})
            FROM {table_name}
            WHERE dt = {format_date_literal(dt)}
        """,
        default=0,
    )

    logger.info(
        "Profiled distinct count | table=%s column=%s dt=%s distinct=%s",
        table_name,
        column_name,
        dt,
        distinct_count,
    )

    return build_metric(
        profile_run_id=profile_run_id,
        run_at=run_at,
        dt=dt,
        table_name=table_name,
        column_name=column_name,
        metric_name="distinct_count",
        metric_value=float(distinct_count or 0),
        metric_unit="values",
    )


def profile_null_rate(
    client: Any,
    table_name: str,
    column_name: str,
    dt: date,
    profile_run_id: str,
    run_at: datetime,
) -> ProfileMetric:
    """
    Profile daily null or blank-string rate for one column.

    Args:
        client: clickhouse-connect client instance.
        table_name: Fully qualified table name.
        column_name: Column to profile.
        dt: Business date being profiled.
        profile_run_id: Shared profile run UUID.
        run_at: UTC timestamp for the profile run.

    Returns:
        ProfileMetric containing the null/blank rate.
    """
    validate_qualified_table_name(table_name)
    validate_column_name(column_name)

    rows = client.query(
        f"""
        SELECT
            count()                                                    AS total_rows,
            countIf(isNull({column_name}) OR toString({column_name}) = '') AS null_or_blank_rows
        FROM {table_name}
        WHERE dt = {format_date_literal(dt)}
        """
    ).result_rows

    total_rows         = int(rows[0][0] or 0) if rows else 0
    null_or_blank_rows = int(rows[0][1] or 0) if rows else 0
    null_rate          = 0.0 if total_rows == 0 else null_or_blank_rows / total_rows

    logger.info(
        "Profiled null rate | table=%s column=%s dt=%s null_or_blank=%d total=%d rate=%.6f",
        table_name,
        column_name,
        dt,
        null_or_blank_rows,
        total_rows,
        null_rate,
    )

    return build_metric(
        profile_run_id=profile_run_id,
        run_at=run_at,
        dt=dt,
        table_name=table_name,
        column_name=column_name,
        metric_name="null_rate",
        metric_value=null_rate,
        metric_unit="rate",
        details={
            "null_or_blank_rows": null_or_blank_rows,
            "total_rows": total_rows,
        },
    )


def profile_freshness_lag(
    client: Any,
    table_name: str,
    dt: date,
    profile_run_id: str,
    run_at: datetime,
) -> ProfileMetric:
    """
    Profile freshness lag in days against a target business date.

    Args:
        client: clickhouse-connect client instance.
        table_name: Fully qualified table name.
        dt: Target business date.
        profile_run_id: Shared profile run UUID.
        run_at: UTC timestamp for the profile run.

    Returns:
        ProfileMetric containing lag days between target dt and max available dt.
    """
    validate_qualified_table_name(table_name)

    max_dt = scalar(
        client=client,
        query=f"""
            SELECT max(dt)
            FROM {table_name}
            WHERE dt <= {format_date_literal(dt)}
        """,
        default=None,
    )

    # Use a large sentinel lag when no partition exists so downstream checks can fail clearly.
    lag_days = 999.0 if max_dt is None else float((dt - max_dt).days)

    logger.info("Profiled freshness lag | table=%s target_dt=%s max_dt=%s lag_days=%s", table_name, dt, max_dt, lag_days)

    return build_metric(
        profile_run_id=profile_run_id,
        run_at=run_at,
        dt=dt,
        table_name=table_name,
        column_name="dt",
        metric_name="freshness_lag_days",
        metric_value=lag_days,
        metric_unit="days",
        details={"max_dt": max_dt.isoformat() if max_dt else None},
    )


def profile_segment_coverage(
    client: Any,
    contract: OrdersDqContract,
    dt: date,
    profile_run_id: str,
    run_at: datetime,
) -> list[ProfileMetric]:
    """
    Profile country-channel coverage for the daily orders mart.

    Args:
        client: clickhouse-connect client instance.
        contract: Validated orders DQ contract.
        dt: Business date being profiled.
        profile_run_id: Shared profile run UUID.
        run_at: UTC timestamp for the profile run.

    Returns:
        List of ProfileMetric objects for observed, expected, and ratio metrics.
    """
    table_name       = contract.profiling.segment_metric_table
    expected_count   = contract.expected_segments.expected_segment_count
    observed_count   = scalar(
        client=client,
        query=f"""
            SELECT uniqExact(concat(toString(country), '|', toString(channel)))
            FROM {table_name}
            WHERE dt = {format_date_literal(dt)}
        """,
        default=0,
    )
    coverage_ratio   = 0.0 if expected_count == 0 else float(observed_count or 0) / expected_count
    expected_details = {
        "countries": contract.expected_segments.countries,
        "channels": contract.expected_segments.channels,
    }

    logger.info(
        "Profiled segment coverage | table=%s dt=%s observed=%s expected=%s ratio=%.6f",
        table_name,
        dt,
        observed_count,
        expected_count,
        coverage_ratio,
    )

    return [
        build_metric(
            profile_run_id=profile_run_id,
            run_at=run_at,
            dt=dt,
            table_name=table_name,
            metric_name="observed_segment_count",
            metric_value=float(observed_count or 0),
            metric_unit="segments",
            details=expected_details,
        ),
        build_metric(
            profile_run_id=profile_run_id,
            run_at=run_at,
            dt=dt,
            table_name=table_name,
            metric_name="expected_segment_count",
            metric_value=float(expected_count),
            metric_unit="segments",
            details=expected_details,
        ),
        build_metric(
            profile_run_id=profile_run_id,
            run_at=run_at,
            dt=dt,
            table_name=table_name,
            metric_name="segment_coverage_ratio",
            metric_value=coverage_ratio,
            metric_unit="rate",
            details=expected_details,
        ),
    ]


def profile_revenue_total(
    client: Any,
    contract: OrdersDqContract,
    dt: date,
    profile_run_id: str,
    run_at: datetime,
) -> ProfileMetric:
    """
    Profile recognized revenue total from the daily orders mart.

    Args:
        client: clickhouse-connect client instance.
        contract: Validated orders DQ contract.
        dt: Business date being profiled.
        profile_run_id: Shared profile run UUID.
        run_at: UTC timestamp for the profile run.

    Returns:
        ProfileMetric containing daily recognized revenue in USD.
    """
    table_name = contract.profiling.revenue_metric_table
    revenue    = scalar(
        client=client,
        query=f"""
            SELECT sum(recognized_revenue_usd)
            FROM {table_name}
            WHERE dt = {format_date_literal(dt)}
        """,
        default=0,
    )

    logger.info("Profiled revenue total | table=%s dt=%s revenue=%s", table_name, dt, revenue)

    return build_metric(
        profile_run_id=profile_run_id,
        run_at=run_at,
        dt=dt,
        table_name=table_name,
        metric_name="recognized_revenue_usd",
        metric_value=float(revenue or 0),
        metric_unit="usd",
    )


def build_profile_metrics_for_date(client: Any, contract: OrdersDqContract, dt: date) -> list[ProfileMetric]:
    """
    Build all configured profile metrics for one business date.

    Args:
        client: clickhouse-connect client instance.
        contract: Validated orders DQ contract.
        dt: Business date being profiled.

    Returns:
        List of ProfileMetric objects ready for ClickHouse insertion.
    """
    profile_run_id = str(uuid4())
    run_at         = datetime.now(timezone.utc)
    metrics        = []

    logger.info("Building profile metrics | dt=%s profile_run_id=%s", dt, profile_run_id)

    for table_name in contract.profiling.row_count_tables:
        metrics.append(profile_row_count(client, table_name, dt, profile_run_id, run_at))

    for table_name, columns in contract.profiling.null_rate_columns.items():
        for column_name in columns:
            metrics.append(profile_null_rate(client, table_name, column_name, dt, profile_run_id, run_at))

    for table_name, columns in contract.profiling.distinct_count_columns.items():
        for column_name in columns:
            metrics.append(profile_distinct_count(client, table_name, column_name, dt, profile_run_id, run_at))

    metrics.append(profile_freshness_lag(client, contract.quality.freshness.table_name, dt, profile_run_id, run_at))
    metrics.extend(profile_segment_coverage(client, contract, dt, profile_run_id, run_at))
    metrics.append(profile_revenue_total(client, contract, dt, profile_run_id, run_at))

    logger.info("Profile metrics built | dt=%s metrics=%d", dt, len(metrics))

    return metrics


def insert_profile_metrics(client: Any, metrics: list[ProfileMetric]) -> int:
    """
    Insert profile metrics into ClickHouse.

    Args:
        client: clickhouse-connect client instance.
        metrics: Profile metrics to insert.

    Returns:
        Number of inserted rows.
    """
    if not metrics:
        logger.info("No profile metrics to insert")
        return 0

    logger.info("Inserting profile metrics | table=%s rows=%d", PROFILE_RESULTS_TABLE, len(metrics))

    client.insert(
        table=PROFILE_RESULTS_TABLE,
        data=[metric.as_insert_row() for metric in metrics],
        column_names=PROFILE_RESULT_COLUMNS,
    )

    logger.info("Profile metrics inserted | table=%s rows=%d", PROFILE_RESULTS_TABLE, len(metrics))

    return len(metrics)


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
        List of business dates to profile.

    Raises:
        ValueError: If date arguments are missing or mutually invalid.
    """
    if dt and (start or end):
        raise ValueError("Use either --dt or --start/--end, not both.")

    if dt:
        run_dt = parse_date(dt)
        logger.info("Resolved single-date profiling run | dt=%s", run_dt)

        return [run_dt]

    if not start or not end:
        raise ValueError("Provide --dt or both --start and --end.")

    return iter_dates(start_date=parse_date(start), end_date=parse_date(end))


def run_profiling(
    dates: list[date],
    contract: OrdersDqContract,
    clickhouse_host: str | None = None,
    clickhouse_port: int | None = None,
) -> dict[str, Any]:
    """
    Run data profiling for one or more business dates.

    Args:
        dates: Business dates to profile.
        contract: Validated orders DQ contract.
        clickhouse_host: Optional ClickHouse host override.
        clickhouse_port: Optional ClickHouse HTTP port override.

    Returns:
        Summary dictionary for the profiling run.
    """
    if not contract.profiling.enabled:
        logger.info("Profiling disabled by contract | dataset=%s", contract.dataset)
        return {"status": "skipped", "reason": "profiling_disabled", "dates": [item.isoformat() for item in dates]}

    client            = build_clickhouse_client(host=clickhouse_host, port=clickhouse_port)
    started_monotonic = time.monotonic()
    inserted_rows     = 0
    partition_results = []

    logger.info("Starting orders profiling run | dates=%s", [item.isoformat() for item in dates])

    for run_dt in dates:
        metrics       = build_profile_metrics_for_date(client=client, contract=contract, dt=run_dt)
        rows_inserted = insert_profile_metrics(client=client, metrics=metrics)
        inserted_rows += rows_inserted

        partition_results.append(
            {
                "dt": run_dt.isoformat(),
                "metrics": len(metrics),
                "rows_inserted": rows_inserted,
            }
        )

    duration_ms = int((time.monotonic() - started_monotonic) * 1000)

    summary = {
        "status": "success",
        "partition_count": len(dates),
        "rows_inserted": inserted_rows,
        "duration_ms": duration_ms,
        "partitions": partition_results,
    }

    logger.info("Orders profiling run completed | summary=%s", summary)

    return summary


def build_parser() -> argparse.ArgumentParser:
    """
    Build the command-line parser for orders profiling execution.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(description="Profile orders tables and write metrics to ClickHouse.")

    parser.add_argument("--dt", default=None, help="Single business date to profile, in YYYY-MM-DD format.")
    parser.add_argument("--start", default=None, help="Inclusive start date for backfill, in YYYY-MM-DD format.")
    parser.add_argument("--end", default=None, help="Inclusive end date for backfill, in YYYY-MM-DD format.")
    parser.add_argument("--contract", default=None, help="Optional path to orders DQ contract YAML.")
    parser.add_argument("--clickhouse-host", default=None, help="Optional ClickHouse host override.")
    parser.add_argument("--clickhouse-port", type=int, default=None, help="Optional ClickHouse HTTP port override.")

    return parser


def main() -> None:
    """
    Parse CLI arguments and run orders profiling.

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
    summary  = run_profiling(
        dates=dates,
        contract=contract,
        clickhouse_host=args.clickhouse_host,
        clickhouse_port=args.clickhouse_port,
    )

    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
