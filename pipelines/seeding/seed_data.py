####
## Legacy Postgres Seeding Runner for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
import argparse
import json
import random
from datetime import date, timedelta

from pipelines.common.logging import logger
from pipelines.seeding.db import (
    connect,
    get_conn_str,
    init_db,
    insert_alert,
    insert_dq_result,
    insert_pipeline_run,
    insert_raw_orders,
    query_dicts,
    query_scalar,
    rebuild_fact_orders_daily,
    reset_db,
)
from pipelines.seeding.helpers import (
    CHANNELS,
    COUNTRIES,
    build_order_count,
    build_order_event,
    utc_now,
)
from pipelines.seeding.incidents import apply_incidents, choose_incidents


# --- Defining Functions
def generate_raw_orders(
    days: int,
    seed_value: int | None,
) -> tuple[list[dict], date, date]:
    """
    Generate raw order events for a trailing date range.

    Args:
        days: Number of historical days to generate, ending yesterday.
        seed_value: Optional random seed for deterministic local runs.

    Returns:
        Tuple of generated rows, start_date, and end_date.
    """
    logger.info("Generating legacy raw orders | days=%d seed=%s", days, seed_value)

    rng = random.Random(seed_value)

    today      = date.today()
    start_date = today - timedelta(days=days)
    end_date   = today - timedelta(days=1)
    total_days = (end_date - start_date).days + 1

    rows: list[dict] = []
    seq              = 1

    for day_index in range(total_days):
        order_date = start_date + timedelta(days=day_index)

        for country in COUNTRIES:
            for channel in CHANNELS:
                order_count = build_order_count(
                    rng=rng,
                    order_date=order_date,
                    country=country,
                    channel=channel,
                    day_index=day_index,
                    total_days=total_days,
                )

                for _ in range(order_count):
                    rows.append(
                        build_order_event(
                            rng=rng,
                            order_date=order_date,
                            country=country,
                            channel=channel,
                            sequence=seq,
                            day_index=day_index,
                            total_days=total_days,
                        )
                    )
                    seq += 1

    logger.info("Generated legacy raw orders | rows=%d start_date=%s end_date=%s", len(rows), start_date, end_date)

    return rows, start_date, end_date


def derive_pipeline_status(incidents: list[str]) -> tuple[str, str | None]:
    """
    Derive a simulated pipeline status from injected incident types.

    Args:
        incidents: Incident names selected for the generated run.

    Returns:
        Tuple of status and optional error message.
    """
    logger.info("Deriving simulated pipeline status | incidents=%s", incidents)

    if "missing_latest_day" in incidents:
        return "failed", "Upstream load failed. Latest partition not landed."

    if any(name in incidents for name in ["missing_segment", "late_arriving_batch"]):
        return "partial_fail", "Partial ingestion or delayed batch detected for one or more segments."

    if any(name in incidents for name in ["duplicate_orders", "cancelled_revenue_leak", "fx_rate_spike"]):
        return "success", "Pipeline completed successfully, but downstream data quality issues were introduced."

    return "success", None


def severity_for_check(
    check_name: str,
    observed: float | int | None,
    threshold: float | int | None,
) -> str:
    """
    Assign alert severity for a deterministic DQ check.

    Args:
        check_name: DQ check identifier.
        observed: Observed check value.
        threshold: Expected or threshold value.

    Returns:
        Severity string: high, medium, or low.
    """
    high_severity_checks = {
        "freshness_max_order_date",
        "duplicate_order_id_latest_day",
        "cancelled_positive_revenue_latest_day",
        "recognized_revenue_anomaly_latest_day",
        "segment_completeness_latest_day",
    }
    medium_severity_checks = {
        "late_arriving_ratio_latest_day",
        "country_aov_spike_latest_day",
    }

    if check_name in high_severity_checks:
        return "high"

    if check_name in medium_severity_checks:
        return "medium"

    return "low"


def run_dq_checks(
    conn,
    incident_date: date,
    incidents_applied: list[dict],
) -> list[dict]:
    """
    Run deterministic DQ checks and persist check results.

    Args:
        conn: Open Postgres connection.
        incident_date: Latest business date targeted by checks.
        incidents_applied: Incident metadata from the data generation step.

    Returns:
        List of failed check dictionaries.
    """
    logger.info("Running deterministic DQ checks | dt=%s incidents=%s", incident_date, incidents_applied)

    results: list[dict] = []
    run_at              = utc_now()

    # Freshness catches missing latest-day partitions before deeper checks run.
    max_order_date   = query_scalar(conn, "SELECT MAX(order_date) FROM raw_orders")
    freshness_status = "fail" if max_order_date and max_order_date < incident_date else "pass"

    results.append(
        {
            "table_name": "public.raw_orders",
            "check_name": "freshness_max_order_date",
            "status": freshness_status,
            "observed": (max_order_date - date(1970, 1, 1)).days if max_order_date else None,
            "threshold": (incident_date - date(1970, 1, 1)).days,
            "details": {"max_order_date": str(max_order_date), "expected_min_date": str(incident_date)},
        }
    )

    # Segment completeness catches partial ingestion failures by country/channel grain.
    expected_segments = len(COUNTRIES) * len(CHANNELS)
    observed_segments = query_scalar(
        conn,
        """
        SELECT COUNT(*)
        FROM (
          SELECT DISTINCT country, channel
          FROM fact_orders_daily
          WHERE date = %s
        ) t
        """,
        (incident_date,),
    ) or 0

    results.append(
        {
            "table_name": "public.fact_orders_daily",
            "check_name": "segment_completeness_latest_day",
            "status": "fail" if observed_segments < expected_segments else "pass",
            "observed": observed_segments,
            "threshold": expected_segments,
            "details": {
                "expected_segments": expected_segments,
                "observed_segments": observed_segments,
                "date": str(incident_date),
            },
        }
    )

    # Duplicate business keys are checked at the event level, not the daily aggregate.
    duplicate_cnt = query_scalar(
        conn,
        """
        SELECT COALESCE(SUM(cnt - 1), 0)
        FROM (
          SELECT order_id, COUNT(*) AS cnt
          FROM raw_orders
          WHERE order_date = %s
          GROUP BY order_id
          HAVING COUNT(*) > 1
        ) t
        """,
        (incident_date,),
    ) or 0

    results.append(
        {
            "table_name": "public.raw_orders",
            "check_name": "duplicate_order_id_latest_day",
            "status": "fail" if duplicate_cnt > 0 else "pass",
            "observed": duplicate_cnt,
            "threshold": 0,
            "details": {"date": str(incident_date)},
        }
    )

    # Revenue leakage check protects the business metric definition for non-paid orders.
    cancelled_positive_revenue = query_scalar(
        conn,
        """
        SELECT COUNT(*)
        FROM raw_orders
        WHERE order_date = %s
          AND status IN ('cancelled', 'pending')
          AND recognized_revenue_usd > 0.01
        """,
        (incident_date,),
    ) or 0

    results.append(
        {
            "table_name": "public.raw_orders",
            "check_name": "cancelled_positive_revenue_latest_day",
            "status": "fail" if cancelled_positive_revenue > 0 else "pass",
            "observed": cancelled_positive_revenue,
            "threshold": 0,
            "details": {"date": str(incident_date)},
        }
    )

    # Late-arriving ratio helps distinguish true missing data from delayed ingestion.
    late_rows = query_scalar(
        conn,
        """
        SELECT COUNT(*)
        FROM raw_orders
        WHERE order_date = %s
          AND ingestion_ts::date > order_date
        """,
        (incident_date,),
    ) or 0
    total_rows = query_scalar(
        conn,
        "SELECT COUNT(*) FROM raw_orders WHERE order_date = %s",
        (incident_date,),
    ) or 0
    late_ratio = round((late_rows / total_rows), 4) if total_rows > 0 else 0.0

    results.append(
        {
            "table_name": "public.raw_orders",
            "check_name": "late_arriving_ratio_latest_day",
            "status": "fail" if late_ratio > 0.10 else "pass",
            "observed": late_ratio,
            "threshold": 0.10,
            "details": {"date": str(incident_date), "late_rows": late_rows, "total_rows": total_rows},
        }
    )

    # Country-level AOV spike is a compact signal for FX/conversion incidents.
    aov_spike = query_dicts(
        conn,
        """
        WITH latest AS (
          SELECT country, SUM(recognized_revenue_usd) / NULLIF(COUNT(DISTINCT order_id), 0) AS aov_latest
          FROM raw_orders
          WHERE order_date = %s
          GROUP BY country
        ),
        baseline AS (
          SELECT country, AVG(aov_day) AS aov_baseline
          FROM (
            SELECT order_date, country,
                   SUM(recognized_revenue_usd) / NULLIF(COUNT(DISTINCT order_id), 0) AS aov_day
            FROM raw_orders
            WHERE order_date BETWEEN %s AND %s
            GROUP BY order_date, country
          ) b
          GROUP BY country
        )
        SELECT
          l.country,
          COALESCE(l.aov_latest, 0)::float AS aov_latest,
          COALESCE(b.aov_baseline, 0)::float AS aov_baseline,
          CASE WHEN COALESCE(b.aov_baseline, 0) = 0 THEN 0
               ELSE (l.aov_latest / b.aov_baseline)::float
          END AS ratio
        FROM latest l
        LEFT JOIN baseline b USING(country)
        ORDER BY ratio DESC
        LIMIT 1
        """,
        (incident_date, incident_date - timedelta(days=7), incident_date - timedelta(days=1)),
    )

    if aov_spike:
        top   = aov_spike[0]
        ratio = round(float(top["ratio"] or 0), 4)

        results.append(
            {
                "table_name": "public.raw_orders",
                "check_name": "country_aov_spike_latest_day",
                "status": "fail" if ratio > 2.50 else "pass",
                "observed": ratio,
                "threshold": 2.50,
                "details": {
                    "date": str(incident_date),
                    "country": top["country"],
                    "aov_latest": float(top["aov_latest"] or 0),
                    "aov_baseline": float(top["aov_baseline"] or 0),
                },
            }
        )

    # Revenue anomaly is the business-facing check used to create a primary alert.
    revenue_latest = query_scalar(
        conn,
        """
        SELECT COALESCE(SUM(recognized_revenue_usd), 0)::float
        FROM fact_orders_daily
        WHERE date = %s
        """,
        (incident_date,),
    ) or 0.0
    revenue_baseline = query_scalar(
        conn,
        """
        SELECT COALESCE(AVG(daily_rev), 0)::float
        FROM (
          SELECT date, SUM(recognized_revenue_usd)::float AS daily_rev
          FROM fact_orders_daily
          WHERE date BETWEEN %s AND %s
          GROUP BY date
        ) t
        """,
        (incident_date - timedelta(days=7), incident_date - timedelta(days=1)),
    ) or 0.0
    revenue_ratio = round((revenue_latest / revenue_baseline), 4) if revenue_baseline > 0 else 0.0

    results.append(
        {
            "table_name": "public.fact_orders_daily",
            "check_name": "recognized_revenue_anomaly_latest_day",
            "status": "fail" if revenue_ratio < 0.70 or revenue_ratio > 1.35 else "pass",
            "observed": revenue_latest,
            "threshold": revenue_baseline,
            "details": {"date": str(incident_date), "ratio": revenue_ratio},
        }
    )

    for item in results:
        insert_dq_result(
            conn=conn,
            run_at=run_at,
            table_name=item["table_name"],
            check_name=item["check_name"],
            status=item["status"],
            observed_value=item["observed"],
            threshold=item["threshold"],
            details=item["details"],
        )

    failed = [item for item in results if item["status"] == "fail"]

    logger.info(
        "DQ checks completed | total=%d failed=%d failed_checks=%s",
        len(results),
        len(failed),
        [item["check_name"] for item in failed],
    )
    return failed


def create_alerts(
    conn,
    incident_date: date,
    failed_checks: list[dict],
    incidents_applied: list[dict],
) -> list[int]:
    """
    Create alerts from failed deterministic DQ checks.

    Args:
        conn: Open Postgres connection.
        incident_date: Affected business date.
        failed_checks: Failed DQ check dictionaries.
        incidents_applied: Incident metadata to attach as alert evidence.

    Returns:
        List of inserted alert ids.
    """
    logger.info("Creating alerts from failed checks | dt=%s failed_checks=%d", incident_date, len(failed_checks))

    alert_ids: list[int] = []
    created_at           = utc_now()

    for check in failed_checks:
        severity = severity_for_check(check["check_name"], check["observed"], check["threshold"])
        alert_id = insert_alert(
            conn=conn,
            created_at=created_at,
            alert_type="dq_check_failure",
            metric=check["check_name"],
            table_name=check["table_name"],
            d=incident_date,
            dimension=None,
            severity=severity,
            observed=check["observed"],
            expected=check["threshold"],
            details={**check["details"], "incidents_applied": incidents_applied},
        )

        alert_ids.append(alert_id)

    # Add a primary business-facing metric alert when the revenue anomaly check fails.
    revenue_check = next(
        (item for item in failed_checks if item["check_name"] == "recognized_revenue_anomaly_latest_day"),
        None,
    )

    if revenue_check:
        alert_ids.append(
            insert_alert(
                conn=conn,
                created_at=created_at,
                alert_type="metric_anomaly",
                metric="recognized_revenue_usd",
                table_name="public.fact_orders_daily",
                d=incident_date,
                dimension="date",
                severity="high",
                observed=revenue_check["observed"],
                expected=revenue_check["threshold"],
                details={**revenue_check["details"], "incidents_applied": incidents_applied},
            )
        )

    logger.info("Alerts created | count=%d alert_ids=%s", len(alert_ids), alert_ids)

    return alert_ids


def seed(
    conn_str: str,
    days: int,
    reset: bool,
    seed_value: int | None,
    forced_incidents: list[str] | None,
    max_incidents: int,
) -> None:
    """
    Run the legacy end-to-end Postgres seeding workflow.

    Args:
        conn_str: PostgreSQL connection URI.
        days: Number of trailing days to generate.
        reset: Whether to truncate demo tables before inserting.
        seed_value: Optional deterministic random seed.
        forced_incidents: Optional explicit incident names.
        max_incidents: Maximum randomly selected incidents.

    Returns:
        None.
    """
    logger.info(
        "Starting legacy seed workflow | days=%d reset=%s seed=%s forced_incidents=%s max_incidents=%d",
        days,
        reset,
        seed_value,
        forced_incidents,
        max_incidents,
    )

    conn = connect(conn_str)

    try:
        init_db(conn)

        if reset:
            reset_db(conn)

        raw_rows, start_date, end_date = generate_raw_orders(days=days, seed_value=seed_value)

        incident_rng = random.Random(seed_value if seed_value is not None else None)
        incidents     = choose_incidents(
            rng=incident_rng,
            forced_incidents=forced_incidents,
            max_incidents=max_incidents,
        )
        incident_results = apply_incidents(
            rows=raw_rows,
            incident_date=end_date,
            incidents=incidents,
            rng=incident_rng,
        )

        insert_raw_orders(conn, raw_rows)
        rebuild_fact_orders_daily(conn, start_date, end_date)

        run_at     = utc_now()
        started_at = run_at - timedelta(minutes=6)
        ended_at   = run_at - timedelta(minutes=1)

        pipeline_status, error_msg = derive_pipeline_status(incidents)

        insert_pipeline_run(
            conn=conn,
            run_at=run_at,
            job_name="ingest_orders_api",
            status=pipeline_status,
            started_at=started_at,
            ended_at=ended_at,
            commit_sha="seed-local-run",
            error_msg=error_msg,
        )

        failed_checks = run_dq_checks(conn, end_date, incident_results)
        alert_ids     = create_alerts(conn, end_date, failed_checks, incident_results)

        summary = {
            "status": "ok",
            "seed_days": days,
            "start_date": str(start_date),
            "end_date": str(end_date),
            "incidents_selected": incidents,
            "incidents_applied": incident_results,
            "failed_checks": [item["check_name"] for item in failed_checks],
            "alert_ids": alert_ids,
            "raw_rows_inserted": len(raw_rows),
        }
        logger.info("Legacy seed workflow completed | summary=%s", summary)

        print(json.dumps(summary, indent=2, default=str))

    finally:
        logger.info("Closing Postgres connection")
        conn.close()


def main() -> None:
    """
    Parse CLI arguments and run the legacy Postgres seeding workflow.

    Returns:
        None.
    """
    parser = argparse.ArgumentParser(description="Seed complex event-level demo data for Agentic DQ Triage Bot (Postgres).")

    parser.add_argument("--days", type=int, default=60, help="Number of days to generate.")
    parser.add_argument("--no-reset", action="store_true", help="Do not truncate tables before seeding.")
    parser.add_argument("--seed", type=int, default=None, help="Random seed. Leave empty for non-deterministic generation.")
    parser.add_argument(
        "--incidents",
        type=str,
        default=None,
        help="Comma-separated incidents. Example: missing_segment,duplicate_orders",
    )
    parser.add_argument("--max-incidents", type=int, default=2, help="Max random incidents when incidents are auto-picked.")

    args = parser.parse_args()

    forced_incidents = [item.strip() for item in args.incidents.split(",")] if args.incidents else None

    logger.info("Parsed seed CLI args | args=%s forced_incidents=%s", vars(args), forced_incidents)

    seed(
        conn_str=get_conn_str(),
        days=args.days,
        reset=not args.no_reset,
        seed_value=args.seed,
        forced_incidents=forced_incidents,
        max_incidents=args.max_incidents,
    )


# --- Running CLI Entrypoint
if __name__ == "__main__":
    main()
