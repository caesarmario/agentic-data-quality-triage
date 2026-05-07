####
## Legacy Postgres Utility Functions for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
import json
import os
from typing import Dict, List

import psycopg2
from psycopg2.extras import execute_values

from pipelines.common.logging import logger


# --- Defining Constants
SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS public;

CREATE TABLE IF NOT EXISTS raw_orders (
  id bigserial PRIMARY KEY,
  order_id text NOT NULL,
  order_date date NOT NULL,
  order_ts timestamptz NOT NULL,
  ingestion_ts timestamptz NOT NULL,
  customer_id text NOT NULL,
  country text NOT NULL,
  channel text NOT NULL,
  status text NOT NULL,
  currency text NOT NULL,
  gross_amount_local numeric(18,2) NOT NULL,
  fx_rate_to_usd numeric(18,6) NOT NULL,
  gross_amount_usd numeric(18,2) NOT NULL,
  discount_usd numeric(18,2) NOT NULL,
  refund_amount_usd numeric(18,2) NOT NULL,
  recognized_revenue_usd numeric(18,2) NOT NULL,
  source_system text NOT NULL,
  is_test boolean NOT NULL DEFAULT false,
  business_date_version integer NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS fact_orders_daily (
  date date NOT NULL,
  country text NOT NULL,
  channel text NOT NULL,
  row_count integer NOT NULL,
  distinct_order_count integer NOT NULL,
  paid_order_count integer NOT NULL,
  cancelled_order_count integer NOT NULL,
  refunded_order_count integer NOT NULL,
  pending_order_count integer NOT NULL,
  gross_amount_usd numeric(18,2) NOT NULL,
  refund_amount_usd numeric(18,2) NOT NULL,
  recognized_revenue_usd numeric(18,2) NOT NULL,
  aov_usd numeric(18,2) NOT NULL,
  PRIMARY KEY (date, country, channel)
);

CREATE TABLE IF NOT EXISTS pipeline_runs (
  id bigserial PRIMARY KEY,
  run_at timestamptz NOT NULL,
  job_name text NOT NULL,
  status text NOT NULL,
  started_at timestamptz NOT NULL,
  ended_at timestamptz NOT NULL,
  commit_sha text,
  error_msg text
);

CREATE TABLE IF NOT EXISTS dq_check_results (
  id bigserial PRIMARY KEY,
  run_at timestamptz NOT NULL,
  table_name text NOT NULL,
  check_name text NOT NULL,
  status text NOT NULL,
  observed_value numeric(18,4),
  threshold numeric(18,4),
  details_json jsonb
);

CREATE TABLE IF NOT EXISTS alerts (
  id bigserial PRIMARY KEY,
  created_at timestamptz NOT NULL,
  alert_type text NOT NULL,
  metric text NOT NULL,
  table_name text NOT NULL,
  date date NOT NULL,
  dimension text,
  severity text NOT NULL,
  observed numeric(18,4),
  expected numeric(18,4),
  details_json jsonb
);

CREATE TABLE IF NOT EXISTS agent_audit_log (
  id bigserial PRIMARY KEY,
  ts timestamptz NOT NULL,
  alert_id bigint,
  action text NOT NULL,
  payload_json jsonb,
  status text NOT NULL,
  duration_ms integer
);

CREATE INDEX IF NOT EXISTS idx_raw_orders_order_date ON raw_orders(order_date);
CREATE INDEX IF NOT EXISTS idx_raw_orders_order_id ON raw_orders(order_id);
CREATE INDEX IF NOT EXISTS idx_raw_orders_ingestion_ts ON raw_orders(ingestion_ts);
CREATE INDEX IF NOT EXISTS idx_fact_orders_date ON fact_orders_daily(date);
CREATE INDEX IF NOT EXISTS idx_dq_table_run ON dq_check_results(table_name, run_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_date ON alerts(date DESC);
"""


TRUNCATE_SQL = """
TRUNCATE TABLE
  raw_orders,
  fact_orders_daily,
  pipeline_runs,
  dq_check_results,
  alerts,
  agent_audit_log
RESTART IDENTITY;
"""


# --- Defining Functions
def get_conn_str() -> str:
    """
    Build the Postgres connection string for the legacy seeding flow.

    This module is kept temporarily while the project migrates toward the
    final SeaweedFS -> ClickHouse pipeline.

    Returns:
        PostgreSQL connection URI from DATABASE_URL or DB_* environment variables.
    """
    db_url = os.getenv("DATABASE_URL")

    if db_url:
        logger.info("Using Postgres connection string from DATABASE_URL")

        return db_url

    host     = os.getenv("DB_HOST", "localhost")
    port     = os.getenv("DB_PORT", "5432")
    name     = os.getenv("DB_NAME", "dq")
    user     = os.getenv("DB_USER", "dq")
    password = os.getenv("DB_PASSWORD", "dq")

    logger.info(
        "Building Postgres connection string from DB_* env vars | host=%s port=%s db=%s user=%s",
        host,
        port,
        name,
        user,
    )

    return f"postgresql://{user}:{password}@{host}:{port}/{name}"


def connect(conn_str: str):
    """
    Open a Postgres connection.

    Args:
        conn_str: PostgreSQL connection URI.

    Returns:
        psycopg2 connection object.

    Raises:
        psycopg2.Error: If the connection cannot be established.
    """
    logger.info("Opening Postgres connection")

    return psycopg2.connect(conn_str)


def init_db(conn) -> None:
    """
    Create legacy Postgres schema and tables if they do not exist.

    Args:
        conn: Open psycopg2 connection.

    Returns:
        None.
    """
    logger.info("Initializing legacy Postgres schema")

    with conn, conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)

    logger.info("Legacy Postgres schema initialized")


def reset_db(conn) -> None:
    """
    Truncate legacy demo tables and restart identity sequences.

    Args:
        conn: Open psycopg2 connection.

    Returns:
        None.
    """
    logger.info("Resetting legacy Postgres demo tables")

    with conn, conn.cursor() as cur:
        cur.execute(TRUNCATE_SQL)

    logger.info("Legacy Postgres demo tables reset")


def insert_raw_orders(conn, rows: List[Dict]) -> None:
    """
    Bulk insert generated raw order events into Postgres.

    Args:
        conn: Open psycopg2 connection.
        rows: Raw order dictionaries produced by the seeding helpers.

    Returns:
        None.
    """
    logger.info("Inserting raw orders into Postgres | rows=%d", len(rows))

    sql = """
      INSERT INTO raw_orders (
        order_id, order_date, order_ts, ingestion_ts, customer_id,
        country, channel, status, currency,
        gross_amount_local, fx_rate_to_usd, gross_amount_usd,
        discount_usd, refund_amount_usd, recognized_revenue_usd,
        source_system, is_test, business_date_version
      )
      VALUES %s
    """
    tuples = [
        (
            row["order_id"],
            row["order_date"],
            row["order_ts"],
            row["ingestion_ts"],
            row["customer_id"],
            row["country"],
            row["channel"],
            row["status"],
            row["currency"],
            row["gross_amount_local"],
            row["fx_rate_to_usd"],
            row["gross_amount_usd"],
            row["discount_usd"],
            row["refund_amount_usd"],
            row["recognized_revenue_usd"],
            row["source_system"],
            row["is_test"],
            row["business_date_version"],
        )
        for row in rows
    ]

    with conn, conn.cursor() as cur:
        # execute_values keeps local bulk inserts fast without hand-building SQL strings.
        execute_values(cur, sql, tuples, page_size=5000)

    logger.info("Raw orders inserted into Postgres | rows=%d", len(rows))


def rebuild_fact_orders_daily(conn, start_date, end_date) -> None:
    """
    Rebuild the daily fact aggregate for a date range.

    Args:
        conn: Open psycopg2 connection.
        start_date: Inclusive start business date.
        end_date: Inclusive end business date.

    Returns:
        None.
    """
    logger.info("Rebuilding fact_orders_daily | start_date=%s end_date=%s", start_date, end_date)

    delete_sql = """
      DELETE FROM fact_orders_daily
      WHERE date BETWEEN %s AND %s
    """
    insert_sql = """
      INSERT INTO fact_orders_daily (
        date, country, channel,
        row_count, distinct_order_count,
        paid_order_count, cancelled_order_count, refunded_order_count, pending_order_count,
        gross_amount_usd, refund_amount_usd, recognized_revenue_usd, aov_usd
      )
      SELECT
        order_date AS date,
        country,
        channel,
        COUNT(*)::int AS row_count,
        COUNT(DISTINCT order_id)::int AS distinct_order_count,
        SUM(CASE WHEN status = 'paid' THEN 1 ELSE 0 END)::int AS paid_order_count,
        SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END)::int AS cancelled_order_count,
        SUM(CASE WHEN status = 'refunded' THEN 1 ELSE 0 END)::int AS refunded_order_count,
        SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END)::int AS pending_order_count,
        COALESCE(SUM(gross_amount_usd), 0)::numeric(18,2) AS gross_amount_usd,
        COALESCE(SUM(refund_amount_usd), 0)::numeric(18,2) AS refund_amount_usd,
        COALESCE(SUM(recognized_revenue_usd), 0)::numeric(18,2) AS recognized_revenue_usd,
        CASE
          WHEN COUNT(DISTINCT order_id) = 0 THEN 0
          ELSE ROUND(COALESCE(SUM(recognized_revenue_usd), 0) / COUNT(DISTINCT order_id), 2)
        END::numeric(18,2) AS aov_usd
      FROM raw_orders
      WHERE order_date BETWEEN %s AND %s
      GROUP BY 1, 2, 3
      ON CONFLICT (date, country, channel) DO UPDATE
      SET
        row_count = EXCLUDED.row_count,
        distinct_order_count = EXCLUDED.distinct_order_count,
        paid_order_count = EXCLUDED.paid_order_count,
        cancelled_order_count = EXCLUDED.cancelled_order_count,
        refunded_order_count = EXCLUDED.refunded_order_count,
        pending_order_count = EXCLUDED.pending_order_count,
        gross_amount_usd = EXCLUDED.gross_amount_usd,
        refund_amount_usd = EXCLUDED.refund_amount_usd,
        recognized_revenue_usd = EXCLUDED.recognized_revenue_usd,
        aov_usd = EXCLUDED.aov_usd
    """

    with conn, conn.cursor() as cur:
        # Delete before insert keeps reruns idempotent for the selected date range.
        cur.execute(delete_sql, (start_date, end_date))
        cur.execute(insert_sql, (start_date, end_date))

    logger.info("fact_orders_daily rebuilt | start_date=%s end_date=%s", start_date, end_date)


def query_scalar(conn, sql: str, params=None):
    """
    Execute a query expected to return a single scalar value.

    Args:
        conn: Open psycopg2 connection.
        sql: SQL statement with optional placeholders.
        params: Optional query parameters.

    Returns:
        First column of the first row, or None when no rows are returned.
    """
    logger.debug("Executing scalar query | sql_head=%s params=%s", sql.strip()[:120], params)

    with conn.cursor() as cur:
        cur.execute(sql, params or ())
        row = cur.fetchone()

        return row[0] if row else None


def query_dicts(conn, sql: str, params=None) -> List[Dict]:
    """
    Execute a query and return rows as dictionaries.

    Args:
        conn: Open psycopg2 connection.
        sql: SQL statement with optional placeholders.
        params: Optional query parameters.

    Returns:
        List of dictionaries keyed by result column name.
    """
    logger.debug("Executing dict query | sql_head=%s params=%s", sql.strip()[:120], params)

    with conn.cursor() as cur:
        cur.execute(sql, params or ())
        cols = [description[0] for description in cur.description]

        return [dict(zip(cols, row)) for row in cur.fetchall()]


def insert_pipeline_run(
    conn,
    run_at,
    job_name: str,
    status: str,
    started_at,
    ended_at,
    commit_sha: str | None = None,
    error_msg: str | None = None,
) -> None:
    """
    Insert one pipeline run record.

    Args:
        conn: Open psycopg2 connection.
        run_at: Logical run timestamp.
        job_name: Pipeline job name.
        status: Run status, such as success, failed, or partial_fail.
        started_at: Job start timestamp.
        ended_at: Job end timestamp.
        commit_sha: Optional code version marker.
        error_msg: Optional failure message.

    Returns:
        None.
    """
    logger.info("Inserting pipeline run | job_name=%s status=%s", job_name, status)

    with conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pipeline_runs (run_at, job_name, status, started_at, ended_at, commit_sha, error_msg)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (run_at, job_name, status, started_at, ended_at, commit_sha, error_msg),
        )


def insert_dq_result(
    conn,
    run_at,
    table_name: str,
    check_name: str,
    status: str,
    observed_value=None,
    threshold=None,
    details: Dict | None = None,
) -> None:
    """
    Insert one deterministic DQ check result.

    Args:
        conn: Open psycopg2 connection.
        run_at: Check execution timestamp.
        table_name: Target table name.
        check_name: DQ check identifier.
        status: Check status, usually pass or fail.
        observed_value: Optional measured value.
        threshold: Optional expected/threshold value.
        details: Optional structured details stored as JSON.

    Returns:
        None.
    """
    logger.info("Inserting DQ result | table=%s check=%s status=%s", table_name, check_name, status)

    with conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO dq_check_results (run_at, table_name, check_name, status, observed_value, threshold, details_json)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
            """,
            (run_at, table_name, check_name, status, observed_value, threshold, json.dumps(details or {})),
        )


def insert_alert(
    conn,
    created_at,
    alert_type: str,
    metric: str,
    table_name: str,
    d,
    dimension: str | None,
    severity: str,
    observed=None,
    expected=None,
    details: Dict | None = None,
) -> int:
    """
    Insert one alert generated from failed DQ or metric checks.

    Args:
        conn: Open psycopg2 connection.
        created_at: Alert creation timestamp.
        alert_type: Alert category, such as dq_check_failure or metric_anomaly.
        metric: Metric/check name that triggered the alert.
        table_name: Affected table.
        d: Affected business date.
        dimension: Optional affected dimension name.
        severity: Alert severity.
        observed: Optional observed value.
        expected: Optional expected value.
        details: Optional structured details stored as JSON.

    Returns:
        Newly inserted integer alert id.
    """
    logger.info("Inserting alert | type=%s metric=%s table=%s severity=%s", alert_type, metric, table_name, severity)

    with conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO alerts (created_at, alert_type, metric, table_name, date, dimension, severity, observed, expected, details_json)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            RETURNING id
            """,
            (
                created_at,
                alert_type,
                metric,
                table_name,
                d,
                dimension,
                severity,
                observed,
                expected,
                json.dumps(details or {}),
            ),
        )
        alert_id = int(cur.fetchone()[0])

    logger.info("Alert inserted | alert_id=%d type=%s metric=%s", alert_id, alert_type, metric)

    return alert_id
