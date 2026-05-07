####
## ClickHouse Raw Loader for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import argparse
import io
import json
import time
from datetime import date, datetime, timezone
from typing import Any

from pipelines.common.clickhouse import build_clickhouse_client, drop_date_partition_if_exists
from pipelines.common.logging import logger
from pipelines.common.pipeline_runs import write_pipeline_run
from pipelines.seeding.config import OrdersSeedConfig, load_orders_config
from pipelines.seeding.helpers import iter_dates, parse_date
from pipelines.seeding.upload_to_s3 import build_s3_client


# --- Defining Constants
RAW_ORDERS_TABLE = "dq.raw_orders"


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


# --- Defining Functions
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

    if normalized.empty:
        logger.info("Raw orders frame is empty; partition validation allowed | dt=%s", dt)

        return normalized

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
    drop_date_partition_if_exists(client=client, table_name=table_name, partition_dt=dt)


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

    if rows == 0:
        logger.info("Raw orders insert skipped for empty partition | table=%s", table_name)

        return 0

    logger.info("Inserting raw orders into ClickHouse | table=%s rows=%d", table_name, rows)

    client.insert_df(
        table=table_name,
        df=frame,
        column_names=RAW_ORDER_COLUMNS,
    )

    logger.info("Raw orders inserted into ClickHouse | table=%s rows=%d", table_name, rows)

    return rows


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
            target_table=RAW_ORDERS_TABLE,
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
                target_table=RAW_ORDERS_TABLE,
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


# --- Running CLI Entrypoint
if __name__ == "__main__":
    main()
