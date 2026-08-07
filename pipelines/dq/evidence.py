####
## DQ Failure Evidence Exporter for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import replace
from datetime import date
from typing import Any, Iterable

from pipelines.common.clickhouse import (
    format_date_literal,
    quote_sql_literal,
    split_table_name,
    validate_column_name,
    validate_qualified_table_name,
)
from pipelines.common.logging import logger
from pipelines.seeding.upload_to_s3 import build_s3_client


# --- Defining Constants
DEFAULT_DQ_FAILURES_BUCKET = "dq-dqfailures"
DEFAULT_EVIDENCE_PREFIX    = "dq-failures/orders"
DEFAULT_SAMPLE_LIMIT       = 50

BAD_RESULT_STATUSES        = {"fail", "warn"}
SAFE_KEY_PATTERN           = re.compile(r"[^A-Za-z0-9_.=-]+")


# --- Defining Functions
def resolve_dq_failures_bucket(bucket: str | None = None) -> str:
    """
    Resolve the S3 bucket used for DQ failure evidence artifacts.

    Args:
        bucket: Optional explicit bucket name from CLI or caller.

    Returns:
        Bucket name used for failed DQ evidence exports.
    """
    resolved_bucket = (
        bucket
        or os.getenv("DQ_FAILURES_BUCKET")
        or os.getenv("DQFAILURES_BUCKET")
        or DEFAULT_DQ_FAILURES_BUCKET
    )

    logger.info("Resolved DQ failures bucket | bucket=%s", resolved_bucket)

    return resolved_bucket


def safe_key_token(value: Any) -> str:
    """
    Convert arbitrary values into compact S3 key-safe path tokens.

    Args:
        value: Raw value to include in an object key.

    Returns:
        Path-safe token for S3 object keys.
    """
    normalized = str(value or "unknown").strip().replace(".", "_")
    safe_value = SAFE_KEY_PATTERN.sub("_", normalized).strip("_")

    return safe_value or "unknown"


def build_evidence_key(
    result: Any,
    prefix: str = DEFAULT_EVIDENCE_PREFIX,
) -> str:
    """
    Build a deterministic S3 object key for one DQ evidence artifact.

    Args:
        result: DQ result object with dt, table_name, check_name, and check_run_id attributes.
        prefix: Top-level S3 object prefix.

    Returns:
        S3 object key for the evidence JSON artifact.
    """
    dt_token          = result.dt.isoformat() if result.dt else "unknown"
    table_token       = safe_key_token(result.table_name)
    check_token       = safe_key_token(result.check_name)
    check_run_id      = safe_key_token(result.check_run_id)
    normalized_prefix = prefix.strip("/")

    return (
        f"{normalized_prefix}/"
        f"dt={dt_token}/"
        f"table={table_token}/"
        f"check={check_token}/"
        f"check_run_id={check_run_id}/"
        "evidence.json"
    )


def rows_to_dicts(columns: Iterable[str], rows: Iterable[Iterable[Any]]) -> list[dict[str, Any]]:
    """
    Convert ClickHouse rows into JSON-friendly dictionaries.

    Args:
        columns: Ordered column names from the query.
        rows: Iterable of row values.

    Returns:
        List of dictionaries keyed by column name.
    """
    column_list = list(columns)

    return [dict(zip(column_list, row)) for row in rows]


def query_dicts(client: Any, query: str) -> list[dict[str, Any]]:
    """
    Execute a ClickHouse query and return rows as dictionaries.

    Args:
        client: clickhouse-connect client instance.
        query: SQL query expected to return rows.

    Returns:
        List of row dictionaries.
    """
    logger.debug("Fetching DQ evidence rows | sql=%s", query)

    response = client.query(query)
    columns  = getattr(response, "column_names", []) or []

    return rows_to_dicts(columns=columns, rows=response.result_rows)


def fetch_recent_partitions(
    client: Any,
    table_name: str,
    dt: date,
) -> list[dict[str, Any]]:
    """
    Fetch nearby partition row counts for a date-partitioned table.

    Args:
        client: clickhouse-connect client instance.
        table_name: Fully qualified table name.
        dt: Business date being investigated.

    Returns:
        Recent partition row counts around the target date.
    """
    validate_qualified_table_name(table_name)

    return query_dicts(
        client=client,
        query=f"""
            SELECT
                dt,
                count() AS row_count
            FROM {table_name}
            WHERE dt >= {format_date_literal(dt)} - INTERVAL 7 DAY
              AND dt <= {format_date_literal(dt)} + INTERVAL 1 DAY
            GROUP BY dt
            ORDER BY dt
        """,
    )


def fetch_sample_rows(
    client: Any,
    table_name: str,
    dt: date,
    where_clause: str = "",
    limit: int = DEFAULT_SAMPLE_LIMIT,
) -> list[dict[str, Any]]:
    """
    Fetch bounded sample rows for one DQ evidence artifact.

    Args:
        client: clickhouse-connect client instance.
        table_name: Fully qualified ClickHouse table name.
        dt: Business date being investigated.
        where_clause: Optional extra SQL predicate beginning with AND.
        limit: Maximum sample rows to fetch.

    Returns:
        Sample rows matching the evidence predicate.
    """
    validate_qualified_table_name(table_name)
    bounded_limit = max(1, min(int(limit), DEFAULT_SAMPLE_LIMIT))

    # Samples are intentionally small so evidence exports stay readable in S3.
    return query_dicts(
        client=client,
        query=f"""
            SELECT *
            FROM {table_name}
            WHERE dt = {format_date_literal(dt)}
            {where_clause}
            LIMIT {bounded_limit}
        """,
    )


def fetch_segment_coverage_evidence(
    client: Any,
    result: Any,
) -> dict[str, Any]:
    """
    Build evidence for missing country-channel coverage in the daily mart.

    Args:
        client: clickhouse-connect client instance.
        result: DQ result object with table, date, and details attributes.

    Returns:
        Segment coverage evidence dictionary.
    """
    observed_segments = query_dicts(
        client=client,
        query=f"""
            SELECT
                country,
                channel,
                row_count
            FROM {result.table_name}
            WHERE dt = {format_date_literal(result.dt)}
            ORDER BY country, channel
        """,
    )
    observed_pairs = {(row["country"], row["channel"]) for row in observed_segments}
    expected_pairs = {
        (country, channel)
        for country in result.details.get("countries", [])
        for channel in result.details.get("channels", [])
    }
    missing_segments = sorted(expected_pairs - observed_pairs)

    return {
        "observed_segments": observed_segments,
        "missing_segments": [
            {"country": country, "channel": channel}
            for country, channel in missing_segments
        ],
    }


def fetch_rowcount_anomaly_evidence(
    client: Any,
    result: Any,
) -> dict[str, Any]:
    """
    Build historical row-count evidence for anomaly checks.

    Args:
        client: clickhouse-connect client instance.
        result: DQ result object with table, date, and details attributes.

    Returns:
        Historical daily row-count evidence dictionary.
    """
    lookback_days = int(result.details.get("lookback_days") or 7)

    history_rows = query_dicts(
        client=client,
        query=f"""
            SELECT
                dt,
                sum(row_count) AS daily_rows
            FROM {result.table_name}
            WHERE dt >= {format_date_literal(result.dt)} - INTERVAL {lookback_days} DAY
              AND dt <= {format_date_literal(result.dt)}
            GROUP BY dt
            ORDER BY dt
        """,
    )

    return {"daily_history": history_rows}


def build_evidence_payload(
    client: Any,
    result: Any,
    sample_limit: int = DEFAULT_SAMPLE_LIMIT,
) -> dict[str, Any]:
    """
    Build a JSON-serializable evidence payload for one failed or warning DQ result.

    Args:
        client: clickhouse-connect client instance.
        result: DQ result object from run_checks.py.
        sample_limit: Maximum sample rows to include when row-level samples are useful.

    Returns:
        Evidence payload ready to upload to S3.
    """
    validate_qualified_table_name(result.table_name)
    table_database, table_name = split_table_name(result.table_name)

    payload: dict[str, Any] = {
        "check_run_id": str(result.check_run_id),
        "run_at": result.run_at.isoformat(),
        "dt": result.dt.isoformat() if result.dt else None,
        "table_name": result.table_name,
        "table_database": table_database,
        "table": table_name,
        "check_name": result.check_name,
        "check_type": result.check_type,
        "status": result.status,
        "severity": result.severity,
        "observed_value": result.observed_value,
        "expected_value": result.expected_value,
        "threshold_value": result.threshold_value,
        "details": result.details,
        "recent_partitions": fetch_recent_partitions(client=client, table_name=result.table_name, dt=result.dt),
        "sample_rows": [],
        "derived_evidence": {},
    }

    if result.check_name.startswith("not_null__"):
        column_name = validate_column_name(result.check_name.split("__", 1)[1])
        payload["sample_rows"] = fetch_sample_rows(
            client=client,
            table_name=result.table_name,
            dt=result.dt,
            where_clause=f"AND (isNull({column_name}) OR toString({column_name}) = '')",
            limit=sample_limit,
        )

    elif result.check_name.startswith("accepted_values__"):
        column_name      = validate_column_name(result.check_name.split("__", 1)[1])
        accepted_values  = result.details.get("accepted_values", [])
        accepted_sql     = ", ".join(quote_sql_literal(str(value)) for value in accepted_values) or "''"
        payload["sample_rows"] = fetch_sample_rows(
            client=client,
            table_name=result.table_name,
            dt=result.dt,
            where_clause=f"AND toString({column_name}) NOT IN ({accepted_sql})",
            limit=sample_limit,
        )

    elif result.check_name == "segment_coverage__country_channel":
        payload["derived_evidence"] = fetch_segment_coverage_evidence(client=client, result=result)

    elif result.check_name == "rowcount_anomaly__daily_total_vs_lookback_avg":
        payload["derived_evidence"] = fetch_rowcount_anomaly_evidence(client=client, result=result)

    elif result.check_name == "duplicate_rate__daily_total":
        payload["sample_rows"] = fetch_sample_rows(
            client=client,
            table_name=result.table_name,
            dt=result.dt,
            where_clause="AND duplicate_order_count > 0",
            limit=sample_limit,
        )

    elif result.check_name == "late_arriving_rate__daily_total":
        payload["sample_rows"] = fetch_sample_rows(
            client=client,
            table_name=result.table_name,
            dt=result.dt,
            where_clause="AND late_arriving_count > 0",
            limit=sample_limit,
        )

    elif result.check_name.startswith("non_negative__"):
        metric_column = validate_column_name(result.check_name.split("__", 1)[1])
        payload["sample_rows"] = fetch_sample_rows(
            client=client,
            table_name=result.table_name,
            dt=result.dt,
            where_clause=f"AND {metric_column} < 0",
            limit=sample_limit,
        )

    else:
        payload["sample_rows"] = fetch_sample_rows(
            client=client,
            table_name=result.table_name,
            dt=result.dt,
            limit=min(sample_limit, 10),
        )

    logger.info(
        "Built DQ evidence payload | dt=%s table=%s check=%s samples=%d",
        result.dt,
        result.table_name,
        result.check_name,
        len(payload["sample_rows"]),
    )

    return payload


def put_json_evidence(
    bucket: str,
    key: str,
    payload: dict[str, Any],
    endpoint_url: str | None = None,
) -> str:
    """
    Store one DQ evidence payload as JSON in S3-compatible storage.

    Args:
        bucket: Target S3 bucket.
        key: Target S3 object key.
        payload: JSON-serializable evidence payload.
        endpoint_url: Optional S3 endpoint URL override.

    Returns:
        S3 URI of the written evidence artifact.
    """
    client = build_s3_client(endpoint_url=endpoint_url)
    body   = json.dumps(payload, indent=2, ensure_ascii=True, default=str).encode("utf-8")

    logger.info("Writing DQ evidence artifact | bucket=%s key=%s bytes=%d", bucket, key, len(body))

    # ContentType makes the artifact readable in SeaweedFS and future UIs.
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType="application/json; charset=utf-8",
    )

    s3_uri = f"s3://{bucket}/{key}"
    logger.info("DQ evidence artifact written | uri=%s", s3_uri)

    return s3_uri


def export_failure_evidence_for_results(
    client: Any,
    results: list[Any],
    bucket: str | None = None,
    prefix: str = DEFAULT_EVIDENCE_PREFIX,
    endpoint_url: str | None = None,
    sample_limit: int = DEFAULT_SAMPLE_LIMIT,
) -> tuple[list[Any], dict[str, Any]]:
    """
    Export S3 evidence for failed or warning DQ results and attach the S3 URI.

    Args:
        client: clickhouse-connect client instance.
        results: DQ results to inspect.
        bucket: Optional target evidence bucket.
        prefix: S3 object key prefix for evidence artifacts.
        endpoint_url: Optional S3 endpoint URL override.
        sample_limit: Maximum sample rows for row-level evidence.

    Returns:
        Tuple of updated DQ results and export summary.
    """
    resolved_bucket = resolve_dq_failures_bucket(bucket)
    updated_results = []
    exported        = 0
    skipped         = 0
    started_at      = time.monotonic()

    for result in results:
        if result.status not in BAD_RESULT_STATUSES:
            skipped += 1
            updated_results.append(result)

            continue

        payload = build_evidence_payload(client=client, result=result, sample_limit=sample_limit)
        key     = build_evidence_key(result=result, prefix=prefix)
        s3_uri  = put_json_evidence(bucket=resolved_bucket, key=key, payload=payload, endpoint_url=endpoint_url)

        updated_results.append(replace(result, evidence_s3_uri=s3_uri))
        exported += 1

    duration_ms = int((time.monotonic() - started_at) * 1000)
    summary     = {
        "status": "success",
        "bucket": resolved_bucket,
        "prefix": prefix,
        "exported": exported,
        "skipped": skipped,
        "duration_ms": duration_ms,
    }

    logger.info("DQ evidence export completed | summary=%s", summary)

    return updated_results, summary
