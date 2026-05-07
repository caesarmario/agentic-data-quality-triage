####
## Orders Data Quality Check Runner for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4


# --- Configuring Project Path
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


# --- Defining Constants
DQ_RESULTS_TABLE = "dq.dq_check_results"

DQ_RESULT_COLUMNS = [
    "check_run_id",
    "run_at",
    "dt",
    "table_name",
    "check_name",
    "check_type",
    "status",
    "severity",
    "observed_value",
    "expected_value",
    "threshold_value",
    "details_json",
    "evidence_s3_uri",
]


# --- Defining Classes
@dataclass(frozen=True)
class DqCheckResult:
    """
    One deterministic DQ check result ready to be written to ClickHouse.

    Attributes:
        check_run_id: Unique check result UUID.
        run_at: UTC timestamp when the DQ run started.
        dt: Business date being checked.
        table_name: Fully qualified table name.
        check_name: Stable check name.
        check_type: Category such as freshness, completeness, validity, anomaly, or threshold.
        status: Check result status: pass, warn, fail, or skip.
        severity: Business severity used when the check creates an alert.
        observed_value: Numeric observed value when available.
        expected_value: Numeric expected value when available.
        threshold_value: Numeric threshold value when available.
        details: Extra structured context stored as JSON.
        evidence_s3_uri: Optional S3 evidence artifact URI.
    """

    check_run_id: UUID
    run_at: datetime
    dt: date
    table_name: str
    check_name: str
    check_type: str
    status: str
    severity: str
    observed_value: float | None
    expected_value: float | None
    threshold_value: float | None
    details: dict[str, Any]
    evidence_s3_uri: str = ""

    def as_insert_row(self) -> list[Any]:
        """
        Convert the check result into a ClickHouse insert row.

        Returns:
            Ordered row matching DQ_RESULT_COLUMNS.
        """
        return [
            self.check_run_id,
            self.run_at,
            self.dt,
            self.table_name,
            self.check_name,
            self.check_type,
            self.status,
            self.severity,
            self.observed_value,
            self.expected_value,
            self.threshold_value,
            json.dumps(self.details, default=str),
            self.evidence_s3_uri,
        ]


# --- Defining Functions
def failure_status(severity: str) -> str:
    """
    Convert business severity into a DQ failure status.

    Args:
        severity: Business severity from the DQ contract.

    Returns:
        warn for warning severity, fail otherwise.
    """
    return "warn" if severity.lower() == "warning" else "fail"


def build_check_result(
    run_at: datetime,
    dt: date,
    table_name: str,
    check_name: str,
    check_type: str,
    status: str,
    severity: str,
    observed_value: float | None = None,
    expected_value: float | None = None,
    threshold_value: float | None = None,
    details: dict[str, Any] | None = None,
    evidence_s3_uri: str = "",
) -> DqCheckResult:
    """
    Build a typed DQ check result with validated table identifiers.

    Args:
        run_at: UTC timestamp for the DQ run.
        dt: Business date being checked.
        table_name: Fully qualified ClickHouse table name.
        check_name: Stable check name.
        check_type: Check category.
        status: Check status: pass, warn, fail, or skip.
        severity: Business severity.
        observed_value: Optional observed numeric value.
        expected_value: Optional expected numeric value.
        threshold_value: Optional threshold numeric value.
        details: Optional structured details stored as JSON.
        evidence_s3_uri: Optional S3 URI for exported evidence.

    Returns:
        DqCheckResult instance ready for insertion.
    """
    validate_qualified_table_name(table_name)

    result = DqCheckResult(
        check_run_id=uuid4(),
        run_at=run_at,
        dt=dt,
        table_name=table_name,
        check_name=check_name,
        check_type=check_type,
        status=status,
        severity=severity,
        observed_value=observed_value,
        expected_value=expected_value,
        threshold_value=threshold_value,
        details=details or {},
        evidence_s3_uri=evidence_s3_uri,
    )

    logger.info(
        "Built DQ check result | dt=%s table=%s check=%s status=%s severity=%s observed=%s",
        dt,
        table_name,
        check_name,
        status,
        severity,
        observed_value,
    )

    return result


def check_freshness(client: Any, contract: OrdersDqContract, dt: date, run_at: datetime) -> DqCheckResult:
    """
    Check whether the target table has data fresh enough for the requested dt.

    Args:
        client: clickhouse-connect client instance.
        contract: Validated orders DQ contract.
        dt: Target business date.
        run_at: UTC timestamp for the DQ run.

    Returns:
        DqCheckResult for freshness.
    """
    check       = contract.quality.freshness
    table_name  = check.table_name
    date_column = check.date_column

    validate_column_name(date_column)

    max_dt = scalar(
        client=client,
        query=f"""
            SELECT max({date_column})
            FROM {table_name}
            WHERE {date_column} <= {format_date_literal(dt)}
        """,
        default=None,
    )

    # No partition is treated as a large lag so the result is explainable in downstream alerts.
    lag_days = 999.0 if max_dt is None else float((dt - max_dt).days)
    status   = "pass" if lag_days <= check.max_lag_days else failure_status(check.severity)

    return build_check_result(
        run_at=run_at,
        dt=dt,
        table_name=table_name,
        check_name="freshness__max_dt_lag",
        check_type="freshness",
        status=status,
        severity=check.severity,
        observed_value=lag_days,
        expected_value=0.0,
        threshold_value=float(check.max_lag_days),
        details={
            "date_column": date_column,
            "max_dt": max_dt.isoformat() if max_dt else None,
        },
    )


def check_row_count_positive(client: Any, contract: OrdersDqContract, dt: date, run_at: datetime) -> list[DqCheckResult]:
    """
    Check that configured tables have at least the minimum expected rows for dt.

    Args:
        client: clickhouse-connect client instance.
        contract: Validated orders DQ contract.
        dt: Business date being checked.
        run_at: UTC timestamp for the DQ run.

    Returns:
        List of DqCheckResult objects, one per configured table.
    """
    results = []

    for check in contract.quality.row_count_positive:
        row_count = scalar(
            client=client,
            query=f"""
                SELECT count()
                FROM {check.table_name}
                WHERE dt = {format_date_literal(dt)}
            """,
            default=0,
        )
        status = "pass" if float(row_count or 0) >= check.min_rows else failure_status(check.severity)

        results.append(
            build_check_result(
                run_at=run_at,
                dt=dt,
                table_name=check.table_name,
                check_name="row_count_positive",
                check_type="volume",
                status=status,
                severity=check.severity,
                observed_value=float(row_count or 0),
                expected_value=float(check.min_rows),
                threshold_value=float(check.min_rows),
            )
        )

    return results


def check_not_null(client: Any, contract: OrdersDqContract, dt: date, run_at: datetime) -> list[DqCheckResult]:
    """
    Check required columns for null or blank-string values.

    Args:
        client: clickhouse-connect client instance.
        contract: Validated orders DQ contract.
        dt: Business date being checked.
        run_at: UTC timestamp for the DQ run.

    Returns:
        List of DqCheckResult objects, one per required column.
    """
    check      = contract.quality.not_null
    table_name = check.table_name
    results    = []

    for column_name in check.columns:
        validate_column_name(column_name)

        rows = client.query(
            f"""
            SELECT
                count()                                                        AS total_rows,
                countIf(isNull({column_name}) OR toString({column_name}) = '') AS bad_rows
            FROM {table_name}
            WHERE dt = {format_date_literal(dt)}
            """
        ).result_rows

        total_rows = int(rows[0][0] or 0) if rows else 0
        bad_rows   = int(rows[0][1] or 0) if rows else 0
        null_rate  = 0.0 if total_rows == 0 else bad_rows / total_rows
        status     = "pass" if bad_rows == 0 else failure_status(check.severity)

        results.append(
            build_check_result(
                run_at=run_at,
                dt=dt,
                table_name=table_name,
                check_name=f"not_null__{column_name}",
                check_type="completeness",
                status=status,
                severity=check.severity,
                observed_value=float(bad_rows),
                expected_value=0.0,
                threshold_value=0.0,
                details={
                    "column_name": column_name,
                    "total_rows": total_rows,
                    "null_or_blank_rows": bad_rows,
                    "null_rate": null_rate,
                },
            )
        )

    return results


def check_accepted_values(client: Any, contract: OrdersDqContract, dt: date, run_at: datetime) -> list[DqCheckResult]:
    """
    Check categorical columns against configured accepted values.

    Args:
        client: clickhouse-connect client instance.
        contract: Validated orders DQ contract.
        dt: Business date being checked.
        run_at: UTC timestamp for the DQ run.

    Returns:
        List of DqCheckResult objects, one per categorical column.
    """
    check      = contract.quality.accepted_values
    table_name = check.table_name
    results    = []

    for column_name, accepted_values in check.columns.items():
        validate_column_name(column_name)
        accepted_sql = ", ".join(quote_sql_literal(str(value)) for value in accepted_values)

        rows = client.query(
            f"""
            SELECT
                count()                                                        AS total_rows,
                countIf(toString({column_name}) NOT IN ({accepted_sql}))        AS invalid_rows
            FROM {table_name}
            WHERE dt = {format_date_literal(dt)}
            """
        ).result_rows

        total_rows   = int(rows[0][0] or 0) if rows else 0
        invalid_rows = int(rows[0][1] or 0) if rows else 0
        invalid_rate = 0.0 if total_rows == 0 else invalid_rows / total_rows
        status       = "pass" if invalid_rows == 0 else failure_status(check.severity)

        results.append(
            build_check_result(
                run_at=run_at,
                dt=dt,
                table_name=table_name,
                check_name=f"accepted_values__{column_name}",
                check_type="validity",
                status=status,
                severity=check.severity,
                observed_value=float(invalid_rows),
                expected_value=0.0,
                threshold_value=0.0,
                details={
                    "column_name": column_name,
                    "accepted_values": accepted_values,
                    "total_rows": total_rows,
                    "invalid_rows": invalid_rows,
                    "invalid_rate": invalid_rate,
                },
            )
        )

    return results


def check_segment_coverage(client: Any, contract: OrdersDqContract, dt: date, run_at: datetime) -> DqCheckResult:
    """
    Check whether all expected country-channel segments exist in the daily mart.

    Args:
        client: clickhouse-connect client instance.
        contract: Validated orders DQ contract.
        dt: Business date being checked.
        run_at: UTC timestamp for the DQ run.

    Returns:
        DqCheckResult for segment coverage.
    """
    check          = contract.quality.segment_coverage
    expected_count = contract.expected_segments.expected_segment_count
    observed_count = scalar(
        client=client,
        query=f"""
            SELECT uniqExact(concat(toString(country), '|', toString(channel)))
            FROM {check.table_name}
            WHERE dt = {format_date_literal(dt)}
        """,
        default=0,
    )
    coverage_ratio = 0.0 if expected_count == 0 else float(observed_count or 0) / expected_count
    status         = "pass" if coverage_ratio >= check.min_coverage_ratio else failure_status(check.severity)

    return build_check_result(
        run_at=run_at,
        dt=dt,
        table_name=check.table_name,
        check_name="segment_coverage__country_channel",
        check_type="coverage",
        status=status,
        severity=check.severity,
        observed_value=coverage_ratio,
        expected_value=1.0,
        threshold_value=check.min_coverage_ratio,
        details={
            "observed_segment_count": int(observed_count or 0),
            "expected_segment_count": expected_count,
            "countries": contract.expected_segments.countries,
            "channels": contract.expected_segments.channels,
        },
    )


def check_rowcount_anomaly(client: Any, contract: OrdersDqContract, dt: date, run_at: datetime) -> DqCheckResult:
    """
    Compare current daily row count with a historical lookback average.

    Args:
        client: clickhouse-connect client instance.
        contract: Validated orders DQ contract.
        dt: Business date being checked.
        run_at: UTC timestamp for the DQ run.

    Returns:
        DqCheckResult for row-count anomaly detection.
    """
    check = contract.quality.rowcount_anomaly

    current_rows = scalar(
        client=client,
        query=f"""
            SELECT sum(row_count)
            FROM {check.table_name}
            WHERE dt = {format_date_literal(dt)}
        """,
        default=0,
    )

    history_rows = client.query(
        f"""
        SELECT
            count()         AS history_days,
            avg(daily_rows) AS avg_daily_rows
        FROM
        (
            SELECT
                dt,
                sum(row_count) AS daily_rows
            FROM {check.table_name}
            WHERE dt >= {format_date_literal(dt)} - INTERVAL {check.lookback_days} DAY
              AND dt < {format_date_literal(dt)}
            GROUP BY dt
        )
        """
    ).result_rows

    history_days   = int(history_rows[0][0] or 0) if history_rows else 0
    avg_daily_rows = float(history_rows[0][1] or 0) if history_rows else 0.0

    if history_days < check.min_history_days or avg_daily_rows <= 0:
        status      = "skip"
        ratio       = None
        lower_bound = None
        upper_bound = None

    else:
        ratio       = float(current_rows or 0) / avg_daily_rows
        lower_bound = avg_daily_rows * check.lower_ratio
        upper_bound = avg_daily_rows * check.upper_ratio
        status      = "pass" if check.lower_ratio <= ratio <= check.upper_ratio else failure_status(check.severity)

    return build_check_result(
        run_at=run_at,
        dt=dt,
        table_name=check.table_name,
        check_name="rowcount_anomaly__daily_total_vs_lookback_avg",
        check_type="anomaly",
        status=status,
        severity=check.severity,
        observed_value=float(current_rows or 0),
        expected_value=avg_daily_rows if history_days >= check.min_history_days else None,
        threshold_value=check.lower_ratio,
        details={
            "history_days": history_days,
            "min_history_days": check.min_history_days,
            "lookback_days": check.lookback_days,
            "lower_ratio": check.lower_ratio,
            "upper_ratio": check.upper_ratio,
            "current_to_avg_ratio": ratio,
            "lower_bound": lower_bound,
            "upper_bound": upper_bound,
        },
    )


def check_rate_threshold(
    client: Any,
    table_name: str,
    dt: date,
    run_at: datetime,
    metric_column: str,
    denominator_column: str,
    check_name: str,
    severity: str,
    max_rate: float,
) -> DqCheckResult:
    """
    Check a numerator/denominator rate against a maximum threshold.

    Args:
        client: clickhouse-connect client instance.
        table_name: Fully qualified mart table name.
        dt: Business date being checked.
        run_at: UTC timestamp for the DQ run.
        metric_column: Numerator metric column.
        denominator_column: Denominator metric column.
        check_name: Stable check name.
        severity: Business severity when the check fails.
        max_rate: Maximum accepted rate.

    Returns:
        DqCheckResult for the rate threshold.
    """
    validate_qualified_table_name(table_name)
    validate_column_name(metric_column)
    validate_column_name(denominator_column)

    rows = client.query(
        f"""
        SELECT
            sum({metric_column})      AS numerator,
            sum({denominator_column}) AS denominator
        FROM {table_name}
        WHERE dt = {format_date_literal(dt)}
        """
    ).result_rows

    numerator   = float(rows[0][0] or 0) if rows else 0.0
    denominator = float(rows[0][1] or 0) if rows else 0.0
    rate        = 0.0 if denominator == 0 else numerator / denominator
    status      = "pass" if rate <= max_rate else failure_status(severity)

    return build_check_result(
        run_at=run_at,
        dt=dt,
        table_name=table_name,
        check_name=check_name,
        check_type="threshold",
        status=status,
        severity=severity,
        observed_value=rate,
        expected_value=0.0,
        threshold_value=max_rate,
        details={
            "numerator_column": metric_column,
            "denominator_column": denominator_column,
            "numerator": numerator,
            "denominator": denominator,
        },
    )


def check_revenue_non_negative(client: Any, contract: OrdersDqContract, dt: date, run_at: datetime) -> DqCheckResult:
    """
    Check that revenue metrics are not negative in the daily mart.

    Args:
        client: clickhouse-connect client instance.
        contract: Validated orders DQ contract.
        dt: Business date being checked.
        run_at: UTC timestamp for the DQ run.

    Returns:
        DqCheckResult for revenue non-negativity.
    """
    check         = contract.quality.revenue_non_negative
    metric_column = check.metric_column

    validate_column_name(metric_column)

    rows = client.query(
        f"""
        SELECT
            countIf({metric_column} < 0) AS negative_rows,
            min({metric_column})         AS min_value
        FROM {check.table_name}
        WHERE dt = {format_date_literal(dt)}
        """
    ).result_rows

    negative_rows = int(rows[0][0] or 0) if rows else 0
    min_value     = float(rows[0][1] or 0) if rows else 0.0
    status        = "pass" if negative_rows == 0 else failure_status(check.severity)

    return build_check_result(
        run_at=run_at,
        dt=dt,
        table_name=check.table_name,
        check_name=f"non_negative__{metric_column}",
        check_type="validity",
        status=status,
        severity=check.severity,
        observed_value=float(negative_rows),
        expected_value=0.0,
        threshold_value=0.0,
        details={
            "metric_column": metric_column,
            "negative_rows": negative_rows,
            "min_value": min_value,
        },
    )


def build_check_results_for_date(client: Any, contract: OrdersDqContract, dt: date) -> list[DqCheckResult]:
    """
    Build all deterministic DQ check results for one business date.

    Args:
        client: clickhouse-connect client instance.
        contract: Validated orders DQ contract.
        dt: Business date being checked.

    Returns:
        List of DqCheckResult objects ready for insertion.
    """
    run_at  = datetime.now(timezone.utc)
    results = []

    logger.info("Building DQ check results | dt=%s", dt)

    results.append(check_freshness(client=client, contract=contract, dt=dt, run_at=run_at))
    results.extend(check_row_count_positive(client=client, contract=contract, dt=dt, run_at=run_at))
    results.extend(check_not_null(client=client, contract=contract, dt=dt, run_at=run_at))
    results.extend(check_accepted_values(client=client, contract=contract, dt=dt, run_at=run_at))
    results.append(check_segment_coverage(client=client, contract=contract, dt=dt, run_at=run_at))
    results.append(check_rowcount_anomaly(client=client, contract=contract, dt=dt, run_at=run_at))
    results.append(
        check_rate_threshold(
            client=client,
            table_name=contract.quality.duplicate_rate.table_name,
            dt=dt,
            run_at=run_at,
            metric_column="duplicate_order_count",
            denominator_column="row_count",
            check_name="duplicate_rate__daily_total",
            severity=contract.quality.duplicate_rate.severity,
            max_rate=contract.quality.duplicate_rate.max_rate,
        )
    )
    results.append(
        check_rate_threshold(
            client=client,
            table_name=contract.quality.late_arriving_rate.table_name,
            dt=dt,
            run_at=run_at,
            metric_column="late_arriving_count",
            denominator_column="row_count",
            check_name="late_arriving_rate__daily_total",
            severity=contract.quality.late_arriving_rate.severity,
            max_rate=contract.quality.late_arriving_rate.max_rate,
        )
    )
    results.append(check_revenue_non_negative(client=client, contract=contract, dt=dt, run_at=run_at))

    logger.info("DQ check results built | dt=%s results=%d", dt, len(results))

    return results


def insert_check_results(client: Any, results: list[DqCheckResult]) -> int:
    """
    Insert DQ check results into ClickHouse.

    Args:
        client: clickhouse-connect client instance.
        results: DQ check results to insert.

    Returns:
        Number of inserted rows.
    """
    if not results:
        logger.info("No DQ check results to insert")
        return 0

    logger.info("Inserting DQ check results | table=%s rows=%d", DQ_RESULTS_TABLE, len(results))

    client.insert(
        table=DQ_RESULTS_TABLE,
        data=[result.as_insert_row() for result in results],
        column_names=DQ_RESULT_COLUMNS,
    )

    logger.info("DQ check results inserted | table=%s rows=%d", DQ_RESULTS_TABLE, len(results))

    return len(results)


def summarize_statuses(results: list[DqCheckResult]) -> dict[str, int]:
    """
    Count DQ result statuses for reporting.

    Args:
        results: DQ check results from one or more dates.

    Returns:
        Mapping of status name to count.
    """
    summary: dict[str, int] = {}

    for result in results:
        summary[result.status] = summary.get(result.status, 0) + 1

    return summary


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
        List of business dates to check.

    Raises:
        ValueError: If date arguments are missing or mutually invalid.
    """
    if dt and (start or end):
        raise ValueError("Use either --dt or --start/--end, not both.")

    if dt:
        run_dt = parse_date(dt)
        logger.info("Resolved single-date DQ run | dt=%s", run_dt)

        return [run_dt]

    if not start or not end:
        raise ValueError("Provide --dt or both --start and --end.")

    return iter_dates(start_date=parse_date(start), end_date=parse_date(end))


def run_dq_checks(
    dates: list[date],
    contract: OrdersDqContract,
    clickhouse_host: str | None = None,
    clickhouse_port: int | None = None,
) -> dict[str, Any]:
    """
    Run deterministic DQ checks for one or more business dates.

    Args:
        dates: Business dates to check.
        contract: Validated orders DQ contract.
        clickhouse_host: Optional ClickHouse host override.
        clickhouse_port: Optional ClickHouse HTTP port override.

    Returns:
        Summary dictionary for the DQ run.
    """
    client            = build_clickhouse_client(host=clickhouse_host, port=clickhouse_port)
    started_monotonic = time.monotonic()
    all_results       = []
    partition_results = []

    logger.info("Starting DQ check run | dates=%s", [item.isoformat() for item in dates])

    for run_dt in dates:
        results       = build_check_results_for_date(client=client, contract=contract, dt=run_dt)
        rows_inserted = insert_check_results(client=client, results=results)
        all_results.extend(results)

        partition_results.append(
            {
                "dt": run_dt.isoformat(),
                "checks": len(results),
                "rows_inserted": rows_inserted,
                "statuses": summarize_statuses(results),
            }
        )

    duration_ms = int((time.monotonic() - started_monotonic) * 1000)

    summary = {
        "status": "success",
        "partition_count": len(dates),
        "rows_inserted": len(all_results),
        "duration_ms": duration_ms,
        "statuses": summarize_statuses(all_results),
        "partitions": partition_results,
    }

    logger.info("DQ check run completed | summary=%s", summary)

    return summary


def build_parser() -> argparse.ArgumentParser:
    """
    Build the command-line parser for DQ check execution.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(description="Run deterministic orders DQ checks and write results to ClickHouse.")

    parser.add_argument("--dt", default=None, help="Single business date to check, in YYYY-MM-DD format.")
    parser.add_argument("--start", default=None, help="Inclusive start date for backfill, in YYYY-MM-DD format.")
    parser.add_argument("--end", default=None, help="Inclusive end date for backfill, in YYYY-MM-DD format.")
    parser.add_argument("--contract", default=None, help="Optional path to orders DQ contract YAML.")
    parser.add_argument("--clickhouse-host", default=None, help="Optional ClickHouse host override.")
    parser.add_argument("--clickhouse-port", type=int, default=None, help="Optional ClickHouse HTTP port override.")

    return parser


def main() -> None:
    """
    Parse CLI arguments and run deterministic DQ checks.

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
    summary  = run_dq_checks(
        dates=dates,
        contract=contract,
        clickhouse_host=args.clickhouse_host,
        clickhouse_port=args.clickhouse_port,
    )

    print(json.dumps(summary, indent=2, default=str))


# --- Running CLI Entrypoint
if __name__ == "__main__":
    main()
