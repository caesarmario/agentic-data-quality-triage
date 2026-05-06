####
## ClickHouse Raw Loader for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

from __future__ import annotations

import argparse
import io
import json
import os
import time
from datetime import date, datetime, timezone
from typing import Any

from pipelines.common.logging import logger
from pipelines.seeding.config import OrdersSeedConfig, load_orders_config
from pipelines.seeding.helpers import iter_dates, parse_date
from pipelines.seeding.upload_to_s3 import build_s3_client


RAW_ORDERS_TABLE = "dq.raw_orders"
PIPELINE_RUNS_TABLE = "dq.pipeline_runs"


RAW_ORDER_COLUMNS = [
    "dt",
    "order_id",
    "order_date",
    "order_ts",
    "ingestion_ts",
    "customer_id",
    "country",
    "channel",
    "status",
    "currency",
    "gross_amount_local",
    "fx_rate_to_usd",
    "gross_amount_usd",
    "discount_usd",
    "refund_amount_usd",
    "recognized_revenue_usd",
    "source_system",
    "is_test",
    "business_date_version",
    "incident_scenario",
]


PIPELINE_RUN_COLUMNS = [
    "job_name",
    "dag_id",
    "task_id",
    "logical_date",
    "partition_dt",
    "status",
    "started_at",
    "ended_at",
    "duration_ms",
    "rows_read",
    "rows_written",
    "source_uri",
    "target_table",
    "error_message",
    "metadata_json",
]


def build_clickhouse_client(
    host: str | None = None,
    port: int | None = None,
    database: str | None = None,
    username: str | None = None,
    password: str | None = None,
):
    """
    Build a ClickHouse HTTP client from explicit values or environment variables.

    Args:
        host: Optional ClickHouse host override.
        port: Optional ClickHouse HTTP port override.
        database: Optional database override.
        username: Optional ClickHouse username override.
        password: Optional ClickHouse password override.

    Returns:
        clickhouse-connect client instance.
    """
    resolved_host     = host or os.getenv("CLICKHOUSE_HOST", "localhost")
    resolved_port     = int(port or os.getenv("CLICKHOUSE_HTTP_PORT", "8123"))
    resolved_database = database or os.getenv("CLICKHOUSE_DB", "dq")
    resolved_username = username or os.getenv("CLICKHOUSE_USER", "default")
    resolved_password = password if password is not None else os.getenv("CLICKHOUSE_PASSWORD", "")

    logger.info(
        "Building ClickHouse client | host=%s port=%s database=%s user=%s",
        resolved_host,
        resolved_port,
        resolved_database,
        resolved_username,
    )

    # Import lazily so --help/static validation works before Docker dependencies are installed.
    import clickhouse_connect

    return clickhouse_connect.get_client(
        host=resolved_host,
        port=resolved_port,
        database=resolved_database,
        username=resolved_username,
        password=resolved_password,
    )


def source_uri_for_date(dt: date, config: OrdersSeedConfig) -> str:
    """
    Build the expected S3 URI for one orders landing partition.

    Args:
        dt: Business date represented by the landing file.
        config: Validated orders seeding config.

    Returns:
        S3 URI pointing to the partition Parquet file.
    """
    return f"s3://{config.output.bucket}/{config.output.object_key(dt)}"


def split_table_name(table_name: str) -> tuple[str, str]:
    """
    Split a fully qualified ClickHouse table name into database and table.

    Args:
        table_name: Fully qualified table name in database.table format.

    Returns:
        Tuple of database name and table name.

    Raises:
        ValueError: If the table name is not fully qualified or contains unsafe identifiers.
    """
    parts = table_name.split(".")

    if len(parts) != 2:
        raise ValueError(f"ClickHouse table name must use database.table format: {table_name}")

    database, table = parts

    _validate_clickhouse_identifier(database)
    _validate_clickhouse_identifier(table)

    return database, table


def _validate_clickhouse_identifier(identifier: str) -> None:
    """
    Validate a ClickHouse identifier used in bounded SQL string interpolation.

    Args:
        identifier: Database or table identifier.

    Returns:
        None.

    Raises:
        ValueError: If the identifier contains unsupported characters.
    """
    if not identifier.replace("_", "").isalnum():
        raise ValueError(f"Unsafe ClickHouse identifier: {identifier}")


def read_orders_parquet_from_s3(
    dt: date,
    config: OrdersSeedConfig,
    endpoint_url: str | None = None,
) -> Any:
    """
    Read one orders Parquet object from SeaweedFS S3 into a DataFrame.

    Args:
        dt: Business date represented by the landing file.
        config: Validated orders seeding config.
        endpoint_url: Optional S3 endpoint URL override.

    Returns:
        pandas DataFrame containing the raw orders partition.

    Raises:
        botocore.exceptions.BotoCoreError: If S3 download fails.
        pyarrow.ArrowInvalid: If the object is not readable as Parquet.
    """
    import pandas as pd

    bucket = config.output.bucket
    key    = config.output.object_key(dt)
    uri    = source_uri_for_date(dt, config)
    client = build_s3_client(endpoint_url)

    logger.info("Reading orders Parquet from S3 | uri=%s", uri)

    response = client.get_object(Bucket=bucket, Key=key)
    payload  = response["Body"].read()

    # Read from memory to avoid Windows path/permission surprises in Docker bind mounts.
    frame = pd.read_parquet(io.BytesIO(payload), engine="pyarrow")

    logger.info("Orders Parquet read from S3 | uri=%s rows=%d bytes=%d", uri, len(frame), len(payload))

    return frame


def normalize_raw_orders_frame(frame: Any, dt: date) -> Any:
    """
    Validate and normalize a raw orders DataFrame before ClickHouse insert.

    Args:
        frame: pandas DataFrame loaded from landing Parquet.
        dt: Expected business date for the partition.

    Returns:
        Normalized pandas DataFrame with columns ordered for dq.raw_orders.

    Raises:
        ValueError: If required columns are missing or partition dates do not match.
    """
    import pandas as pd

    missing_columns = [column for column in RAW_ORDER_COLUMNS if column not in frame.columns]

    if missing_columns:
        logger.error("Raw orders frame missing columns | columns=%s", missing_columns)
        raise ValueError(f"Raw orders frame missing required columns: {missing_columns}")

    normalized = frame.loc[:, RAW_ORDER_COLUMNS].copy()

    normalized["dt"]           = pd.to_datetime(normalized["dt"]).dt.date
    normalized["order_date"]   = pd.to_datetime(normalized["order_date"]).dt.date
    normalized["order_ts"]     = pd.to_datetime(normalized["order_ts"], utc=True)
    normalized["ingestion_ts"] = pd.to_datetime(normalized["ingestion_ts"], utc=True)

    observed_dates = set(normalized["dt"].dropna().unique())

    if observed_dates != {dt}:
        logger.error("Partition date mismatch | expected=%s observed=%s", dt, sorted(observed_dates))
        raise ValueError(f"Partition date mismatch: expected={dt}, observed={sorted(observed_dates)}")

    # ClickHouse UInt8 expects 0/1 values; Parquet may round-trip booleans as bool.
    normalized["is_test"]               = normalized["is_test"].astype("uint8")
    normalized["business_date_version"] = normalized["business_date_version"].astype("uint32")

    logger.info("Raw orders frame normalized | dt=%s rows=%d columns=%d", dt, len(normalized), len(normalized.columns))

    return normalized


def drop_raw_orders_partition(client: Any, dt: date, table_name: str = RAW_ORDERS_TABLE) -> None:
    """
    Drop one ClickHouse raw_orders partition before a replacement insert.

    Args:
        client: clickhouse-connect client instance.
        dt: Business date partition to replace.
        table_name: Fully qualified ClickHouse table name.

    Returns:
        None.
    """
    partition_id = dt.isoformat()
    database, table = split_table_name(table_name)

    partition_count = client.query(
        f"""
        SELECT count()
        FROM system.parts
        WHERE database = '{database}'
          AND table = '{table}'
          AND partition = '{partition_id}'
          AND active
        """
    ).result_rows[0][0]

    if partition_count == 0:
        logger.info("ClickHouse partition does not exist; drop skipped | table=%s partition=%s", table_name, partition_id)

        return

    logger.info(
        "Dropping ClickHouse partition | table=%s partition=%s active_parts=%s",
        table_name,
        partition_id,
        partition_count,
    )

    # Date partitions are addressed by their ISO date value in ClickHouse.
    # partition_id is derived from a datetime.date object, so this string interpolation is bounded.
    client.command(f"ALTER TABLE {table_name} DROP PARTITION '{partition_id}'")

    logger.info("ClickHouse partition dropped | table=%s partition=%s", table_name, partition_id)


def insert_raw_orders_frame(client: Any, frame: Any, table_name: str = RAW_ORDERS_TABLE) -> int:
    """
    Insert a normalized raw orders DataFrame into ClickHouse.

    Args:
        client: clickhouse-connect client instance.
        frame: Normalized pandas DataFrame.
        table_name: Fully qualified ClickHouse table name.

    Returns:
        Number of inserted rows.
    """
    rows = int(len(frame))

    logger.info("Inserting raw orders into ClickHouse | table=%s rows=%d", table_name, rows)

    client.insert_df(
        table=table_name,
        df=frame,
        column_names=RAW_ORDER_COLUMNS,
    )

    logger.info("Raw orders inserted into ClickHouse | table=%s rows=%d", table_name, rows)

    return rows


def write_pipeline_run(
    client: Any,
    job_name: str,
    partition_dt: date,
    status: str,
    started_at: datetime,
    ended_at: datetime,
    rows_read: int | None = None,
    rows_written: int | None = None,
    source_uri: str = "",
    target_table: str = RAW_ORDERS_TABLE,
    error_message: str = "",
    metadata: dict[str, Any] | None = None,
    dag_id: str = "",
    task_id: str = "",
) -> None:
    """
    Persist one pipeline run record to ClickHouse observability storage.

    Args:
        client: clickhouse-connect client instance.
        job_name: Logical job name.
        partition_dt: Business date processed by the job.
        status: Run status such as success or failed.
        started_at: UTC timestamp when the job started.
        ended_at: UTC timestamp when the job ended.
        rows_read: Optional number of rows read from source.
        rows_written: Optional number of rows written to target.
        source_uri: Source URI used by the job.
        target_table: Target ClickHouse table name.
        error_message: Optional failure message.
        metadata: Optional structured metadata stored as JSON.
        dag_id: Optional Airflow DAG id.
        task_id: Optional Airflow task id.

    Returns:
        None.
    """
    duration_ms = int((ended_at - started_at).total_seconds() * 1000)

    row = [
        job_name,
        dag_id,
        task_id,
        partition_dt,
        partition_dt,
        status,
        started_at,
        ended_at,
        duration_ms,
        rows_read,
        rows_written,
        source_uri,
        target_table,
        error_message,
        json.dumps(metadata or {}, default=str),
    ]

    logger.info(
        "Writing pipeline run | job=%s dt=%s status=%s rows_read=%s rows_written=%s",
        job_name,
        partition_dt,
        status,
        rows_read,
        rows_written,
    )

    client.insert(
        table=PIPELINE_RUNS_TABLE,
        data=[row],
        column_names=PIPELINE_RUN_COLUMNS,
    )


def load_orders_partition(
    dt: date,
    config: OrdersSeedConfig,
    endpoint_url: str | None = None,
    clickhouse_host: str | None = None,
    clickhouse_port: int | None = None,
) -> dict[str, Any]:
    """
    Load one orders partition from S3 landing into ClickHouse raw_orders.

    Args:
        dt: Business date partition to load.
        config: Validated orders seeding config.
        endpoint_url: Optional S3 endpoint URL override.
        clickhouse_host: Optional ClickHouse host override.
        clickhouse_port: Optional ClickHouse HTTP port override.

    Returns:
        Summary dictionary for the partition load.

    Raises:
        Exception: Propagates source, validation, or ClickHouse load failures after logging pipeline_runs.
    """
    started_at = datetime.now(timezone.utc)
    source_uri = source_uri_for_date(dt, config)
    client     = build_clickhouse_client(host=clickhouse_host, port=clickhouse_port)

    logger.info("Starting orders partition load | dt=%s source=%s target=%s", dt, source_uri, RAW_ORDERS_TABLE)

    rows_read    = None
    rows_written = None

    try:
        raw_frame        = read_orders_parquet_from_s3(dt=dt, config=config, endpoint_url=endpoint_url)
        normalized_frame = normalize_raw_orders_frame(frame=raw_frame, dt=dt)

        rows_read = int(len(normalized_frame))

        drop_raw_orders_partition(client=client, dt=dt)
        rows_written = insert_raw_orders_frame(client=client, frame=normalized_frame)

        ended_at = datetime.now(timezone.utc)

        write_pipeline_run(
            client=client,
            job_name="load_orders_s3_to_clickhouse",
            partition_dt=dt,
            status="success",
            started_at=started_at,
            ended_at=ended_at,
            rows_read=rows_read,
            rows_written=rows_written,
            source_uri=source_uri,
            metadata={"loader": "pipelines.loading.load_clickhouse"},
        )

        summary = {
            "status": "success",
            "dt": dt.isoformat(),
            "source_uri": source_uri,
            "target_table": RAW_ORDERS_TABLE,
            "rows_read": rows_read,
            "rows_written": rows_written,
        }

        logger.info("Orders partition load completed | summary=%s", summary)

        return summary

    except Exception as exc:
        ended_at = datetime.now(timezone.utc)
        logger.exception("Orders partition load failed | dt=%s source=%s", dt, source_uri)

        try:
            write_pipeline_run(
                client=client,
                job_name="load_orders_s3_to_clickhouse",
                partition_dt=dt,
                status="failed",
                started_at=started_at,
                ended_at=ended_at,
                rows_read=rows_read,
                rows_written=rows_written,
                source_uri=source_uri,
                error_message=str(exc)[:1000],
                metadata={"loader": "pipelines.loading.load_clickhouse"},
            )

        except Exception:
            logger.exception("Failed to write failure pipeline run | dt=%s", dt)

        raise


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
        List of business dates to load.

    Raises:
        ValueError: If date arguments are missing or mutually invalid.
    """
    if dt and (start or end):
        raise ValueError("Use either --dt or --start/--end, not both.")

    if dt:
        run_dt = parse_date(dt)
        logger.info("Resolved single-date load | dt=%s", run_dt)

        return [run_dt]

    if not start or not end:
        raise ValueError("Provide --dt or both --start and --end.")

    return iter_dates(start_date=parse_date(start), end_date=parse_date(end))


def run_load(
    dates: list[date],
    config: OrdersSeedConfig,
    endpoint_url: str | None = None,
    clickhouse_host: str | None = None,
    clickhouse_port: int | None = None,
) -> dict[str, Any]:
    """
    Load one or more S3 landing partitions into ClickHouse.

    Args:
        dates: Business dates to load.
        config: Validated orders seeding config.
        endpoint_url: Optional S3 endpoint URL override.
        clickhouse_host: Optional ClickHouse host override.
        clickhouse_port: Optional ClickHouse HTTP port override.

    Returns:
        Summary dictionary for the load run.
    """
    logger.info("Starting ClickHouse load run | dates=%s", [item.isoformat() for item in dates])

    started_monotonic = time.monotonic()
    partitions        = []

    for run_dt in dates:
        partitions.append(
            load_orders_partition(
                dt=run_dt,
                config=config,
                endpoint_url=endpoint_url,
                clickhouse_host=clickhouse_host,
                clickhouse_port=clickhouse_port,
            )
        )

    duration_ms = int((time.monotonic() - started_monotonic) * 1000)
    total_rows  = sum(item["rows_written"] for item in partitions)

    summary = {
        "status": "success",
        "partition_count": len(partitions),
        "total_rows_written": total_rows,
        "duration_ms": duration_ms,
        "partitions": partitions,
    }

    logger.info("ClickHouse load run completed | summary=%s", summary)

    return summary


def build_parser() -> argparse.ArgumentParser:
    """
    Build the command-line parser for ClickHouse load execution.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(description="Load orders Parquet partitions from SeaweedFS S3 into ClickHouse.")

    parser.add_argument("--dt", default=None, help="Single business date to load, in YYYY-MM-DD format.")
    parser.add_argument("--start", default=None, help="Inclusive start date for backfill, in YYYY-MM-DD format.")
    parser.add_argument("--end", default=None, help="Inclusive end date for backfill, in YYYY-MM-DD format.")
    parser.add_argument("--config", default=None, help="Optional path to orders.yml.")
    parser.add_argument("--endpoint-url", default=None, help="Optional S3 endpoint URL override.")
    parser.add_argument("--clickhouse-host", default=None, help="Optional ClickHouse host override.")
    parser.add_argument("--clickhouse-port", type=int, default=None, help="Optional ClickHouse HTTP port override.")

    return parser


def main() -> None:
    """
    Parse CLI arguments and run the ClickHouse load.

    Returns:
        None.
    """
    parser = build_parser()
    args   = parser.parse_args()

    try:
        dates = resolve_run_dates(dt=args.dt, start=args.start, end=args.end)

    except ValueError as exc:
        parser.error(str(exc))

    config  = load_orders_config(args.config)
    summary = run_load(
        dates=dates,
        config=config,
        endpoint_url=args.endpoint_url,
        clickhouse_host=args.clickhouse_host,
        clickhouse_port=args.clickhouse_port,
    )

    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
