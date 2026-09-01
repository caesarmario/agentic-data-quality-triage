####
## Smoke Readiness CLI for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Add repo root before importing project packages when this file is executed by path.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipelines.common.clickhouse import build_clickhouse_client, quote_sql_literal, split_table_name
from pipelines.common.logging import logger
from pipelines.seeding.upload_to_s3 import build_s3_client


# --- Defining Constants
DEFAULT_CLICKHOUSE_TABLES = [
    "dq.raw_orders",
    "dq.stg_orders",
    "dq.fct_orders_daily",
    "dq.dq_check_results",
    "dq.alerts",
    "dq.pipeline_runs",
    "dq.agent_audit_log",
    "dq.approval_requests",
    "dq.metadata_assets",
    "dq.schema_snapshots",
    "dq.schema_drift_results",
    "dq.agent_run_context_events",
    "dq.incident_memory",
]

DEFAULT_S3_BUCKETS = [
    "dq-landing",
    "dq-artifacts",
    "dq-dqreports",
    "dq-dqfailures",
    "dq-audit",
]

DEFAULT_CH_UI_HEALTH_URL = os.getenv(
    "CH_UI_HEALTH_URL",
    "http://ch-ui:3488",
)
DEFAULT_STREAMLIT_HEALTH_URL = os.getenv(
    "STREAMLIT_HEALTH_URL",
    "http://streamlit:8501/_stcore/health",
)
DEFAULT_API_HEALTH_URL = os.getenv(
    "CONTROL_PLANE_API_HEALTH_URL",
    "http://api:8000/health",
)
DEFAULT_API_DAILY_SUMMARY_URL = os.getenv(
    "CONTROL_PLANE_API_DAILY_SUMMARY_URL",
    "http://api:8000/api/v1/summaries/daily?dt=2026-06-10",
)
DEFAULT_API_LIFE_HISTORY_URL = os.getenv(
    "CONTROL_PLANE_API_LIFE_HISTORY_URL",
    "http://api:8000/api/v1/evaluations/life?lookback_days=30&limit=1",
)
DEFAULT_API_INCIDENT_HISTORY_URL = os.getenv(
    "CONTROL_PLANE_API_INCIDENT_HISTORY_URL",
    "http://api:8000/api/v1/incidents/history?"
    "alert_reference=DQ-READINESS-000000&lookback_days=30&limit=1",
)
DEFAULT_API_METADATA_ASSET_URL = os.getenv(
    "CONTROL_PLANE_API_METADATA_ASSET_URL",
    "http://api:8000/api/v1/metadata/assets/dq.fct_orders_daily",
)
DEFAULT_API_BLAST_RADIUS_URL = os.getenv(
    "CONTROL_PLANE_API_BLAST_RADIUS_URL",
    "http://api:8000/api/v1/lineage/dbt/blast-radius?"
    "table_name=dq.raw_orders&"
    "manifest_s3_uri=s3%3A%2F%2Fdq-artifacts%2Fdbt-artifacts%2Forders%2Flatest%2Fmanifest.json&"
    "max_depth=5&max_nodes=100",
)
DEFAULT_HTTP_TIMEOUT_SECONDS = 5.0

DEFAULT_JSON_INDENT = 2


# --- Defining Data Models
@dataclass(frozen=True)
class ReadinessCheck:
    """
    Represent one read-only smoke readiness check result.

    Attributes:
        name: Human-readable check name.
        status: Check status, usually pass or fail.
        details: Additional check metadata for debugging.
    """

    name: str
    status: str
    details: dict[str, Any]


# --- Defining ClickHouse Functions
def build_table_exists_query(database: str, table: str) -> str:
    """
    Build a read-only query that checks whether one ClickHouse table exists.

    Args:
        database: ClickHouse database name.
        table: ClickHouse table name.

    Returns:
        SQL query returning a single count value.
    """
    return f"""
        SELECT count()
        FROM system.tables
        WHERE database = {quote_sql_literal(database)}
          AND name = {quote_sql_literal(table)}
    """


def build_table_count_query(table_name: str) -> str:
    """
    Build a bounded read-only row count query for one validated table.

    Args:
        table_name: Fully qualified ClickHouse table name.

    Returns:
        SQL query returning a single table row count.
    """
    database, table = split_table_name(table_name)

    return f"SELECT count() FROM {database}.{table}"


def query_single_value(client: Any, query: str, default: Any = None) -> Any:
    """
    Execute a ClickHouse query and return the first cell.

    Args:
        client: clickhouse-connect compatible client.
        query: Read-only SQL query.
        default: Value returned when no rows are returned.

    Returns:
        First result cell or default when the result set is empty.
    """
    result = client.query(query)

    if not getattr(result, "result_rows", None):
        return default

    first_row = result.result_rows[0]

    return first_row[0] if first_row else default


def check_clickhouse_tables(client: Any, table_names: list[str]) -> list[ReadinessCheck]:
    """
    Verify expected ClickHouse tables exist and collect row counts.

    Args:
        client: clickhouse-connect compatible client.
        table_names: Fully qualified table names to verify.

    Returns:
        List of readiness checks, one per table.
    """
    checks = []

    for table_name in table_names:
        database, table = split_table_name(table_name)

        logger.info("Checking ClickHouse table readiness | table=%s", table_name)

        try:
            exists = int(query_single_value(client, build_table_exists_query(database, table), default=0) or 0)

            if exists == 0:
                checks.append(
                    ReadinessCheck(
                        name=f"clickhouse_table:{table_name}",
                        status="fail",
                        details={"exists": False, "row_count": None},
                    )
                )
                continue

            # Count is useful for testing context, but this check only requires the table to exist.
            row_count = int(query_single_value(client, build_table_count_query(table_name), default=0) or 0)
            checks.append(
                ReadinessCheck(
                    name=f"clickhouse_table:{table_name}",
                    status="pass",
                    details={"exists": True, "row_count": row_count},
                )
            )
        except Exception as exc:  # pragma: no cover - exercised by runtime smoke checks.
            logger.exception("ClickHouse readiness check failed | table=%s", table_name)
            checks.append(
                ReadinessCheck(
                    name=f"clickhouse_table:{table_name}",
                    status="fail",
                    details={"error": str(exc)},
                )
            )

    return checks


# --- Defining S3 Functions
def list_available_buckets(client: Any) -> set[str]:
    """
    List available S3 bucket names from a boto3-compatible client.

    Args:
        client: boto3 S3 client.

    Returns:
        Set of bucket names visible to the configured credentials.
    """
    response = client.list_buckets()

    return {bucket["Name"] for bucket in response.get("Buckets", [])}


def check_s3_buckets(client: Any, bucket_names: list[str]) -> list[ReadinessCheck]:
    """
    Verify expected SeaweedFS S3 buckets are available.

    Args:
        client: boto3 S3 client.
        bucket_names: Bucket names to verify.

    Returns:
        List of readiness checks, one per bucket.
    """
    logger.info("Checking S3 bucket readiness | buckets=%s", bucket_names)

    try:
        available_buckets = list_available_buckets(client)
    except Exception as exc:  # pragma: no cover - exercised by runtime smoke checks.
        logger.exception("S3 readiness bucket listing failed")
        return [
            ReadinessCheck(
                name="s3_bucket_listing",
                status="fail",
                details={"error": str(exc)},
            )
        ]

    checks = []

    for bucket_name in bucket_names:
        checks.append(
            ReadinessCheck(
                name=f"s3_bucket:{bucket_name}",
                status="pass" if bucket_name in available_buckets else "fail",
                details={"exists": bucket_name in available_buckets},
            )
        )

    return checks


# --- Defining Service Readiness Functions
def check_http_service(
    service_name: str,
    url: str,
    timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
    opener: Any = urlopen,
) -> ReadinessCheck:
    """
    Verify one internal HTTP service accepts a bounded read-only request.

    Args:
        service_name: Stable service label used in readiness output.
        url: Internal HTTP URL to request.
        timeout_seconds: Bounded network timeout.
        opener: urllib-compatible opener injected by tests.

    Returns:
        ReadinessCheck with HTTP status or a safe error classification.
    """
    logger.info(
        "Checking HTTP service readiness | service=%s url=%s timeout=%.1f",
        service_name,
        url,
        timeout_seconds,
    )

    try:
        with opener(url, timeout=timeout_seconds) as response:
            status_code = int(getattr(response, "status", 0) or 0)

    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        logger.warning(
            "HTTP service readiness failed | service=%s error_type=%s",
            service_name,
            type(exc).__name__,
        )

        return ReadinessCheck(
            name=f"http_service:{service_name}",
            status="fail",
            details={
                "url": url,
                "error_type": type(exc).__name__,
            },
        )

    is_ready = 200 <= status_code < 400

    return ReadinessCheck(
        name=f"http_service:{service_name}",
        status="pass" if is_ready else "fail",
        details={
            "url": url,
            "status_code": status_code,
        },
    )


def check_control_plane_api(
    timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
    opener: Any = urlopen,
) -> list[ReadinessCheck]:
    """
    Verify the control-plane health and representative read-only routes.

    Args:
        timeout_seconds: Bounded timeout applied to each HTTP request.
        opener: urllib-compatible opener injected by tests.

    Returns:
        Health, daily summary, LIFE history, incident history, metadata, and
        blast-radius checks.
    """
    logger.info(
        "Checking control-plane API health, daily summary, LIFE history, incident history, metadata, and dbt blast-radius routes"
    )

    return [
        check_http_service(
            service_name="control-plane-api",
            url=DEFAULT_API_HEALTH_URL,
            timeout_seconds=timeout_seconds,
            opener=opener,
        ),
        check_http_service(
            service_name="control-plane-daily-summary",
            url=DEFAULT_API_DAILY_SUMMARY_URL,
            timeout_seconds=timeout_seconds,
            opener=opener,
        ),
        check_http_service(
            service_name="control-plane-life-history",
            url=DEFAULT_API_LIFE_HISTORY_URL,
            timeout_seconds=timeout_seconds,
            opener=opener,
        ),
        check_http_service(
            service_name="control-plane-incident-history",
            url=DEFAULT_API_INCIDENT_HISTORY_URL,
            timeout_seconds=timeout_seconds,
            opener=opener,
        ),
        check_http_service(
            service_name="control-plane-metadata-asset",
            url=DEFAULT_API_METADATA_ASSET_URL,
            timeout_seconds=timeout_seconds,
            opener=opener,
        ),
        check_http_service(
            service_name="control-plane-dbt-blast-radius",
            url=DEFAULT_API_BLAST_RADIUS_URL,
            timeout_seconds=timeout_seconds,
            opener=opener,
        ),
    ]


# --- Defining Summary Functions
def summarize_checks(checks: list[ReadinessCheck]) -> dict[str, Any]:
    """
    Summarize readiness checks into a compact operational payload.

    Args:
        checks: Readiness checks to summarize.

    Returns:
        Dictionary with status, counts, and detailed checks.
    """
    failed_checks = [check for check in checks if check.status != "pass"]

    summary = {
        "status": "pass" if not failed_checks else "fail",
        "passed": len(checks) - len(failed_checks),
        "failed": len(failed_checks),
        "checks": [asdict(check) for check in checks],
    }

    logger.info(
        "Smoke readiness summary built | status=%s passed=%d failed=%d",
        summary["status"],
        summary["passed"],
        summary["failed"],
    )

    return summary


def render_text_summary(summary: dict[str, Any]) -> str:
    """
    Render readiness summary as readable terminal text.

    Args:
        summary: Summary dictionary from summarize_checks.

    Returns:
        Multi-line text summary.
    """
    lines = [
        "Agentic DQ smoke readiness",
        f"status={summary['status']} passed={summary['passed']} failed={summary['failed']}",
        "",
    ]

    for check in summary["checks"]:
        lines.append(f"[{check['status'].upper()}] {check['name']} {json.dumps(check['details'], default=str)}")

    return "\n".join(lines)


# --- Defining CLI Functions
def build_parser() -> argparse.ArgumentParser:
    """
    Build the command-line parser for smoke readiness checks.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(description="Run read-only smoke readiness checks for the local DQ platform.")

    parser.add_argument("--format", choices=["text", "json"], default="text", help="Output format.")
    parser.add_argument("--skip-clickhouse", action="store_true", help="Skip ClickHouse table checks.")
    parser.add_argument("--skip-s3", action="store_true", help="Skip SeaweedFS S3 bucket checks.")
    parser.add_argument("--skip-ch-ui", action="store_true", help="Skip CH-UI HTTP readiness check.")
    parser.add_argument("--skip-streamlit", action="store_true", help="Skip Streamlit HTTP readiness check.")
    parser.add_argument(
        "--require-api",
        action="store_true",
        help="Require the optional control-plane API profile to pass its health check.",
    )
    parser.add_argument("--table", action="append", default=None, help="Optional ClickHouse table override. Repeatable.")
    parser.add_argument("--bucket", action="append", default=None, help="Optional S3 bucket override. Repeatable.")

    return parser


def main() -> None:
    """
    Run smoke readiness checks and exit non-zero when required checks fail.

    Returns:
        None.

    Raises:
        SystemExit: With code 1 when any readiness check fails.
    """
    parser = build_parser()
    args   = parser.parse_args()
    checks = []

    table_names  = args.table or DEFAULT_CLICKHOUSE_TABLES
    bucket_names = args.bucket or DEFAULT_S3_BUCKETS

    if not args.skip_clickhouse:
        clickhouse_client = build_clickhouse_client()
        checks.extend(check_clickhouse_tables(client=clickhouse_client, table_names=table_names))

    if not args.skip_s3:
        s3_client = build_s3_client()
        checks.extend(check_s3_buckets(client=s3_client, bucket_names=bucket_names))

    if not args.skip_ch_ui:
        checks.append(
            check_http_service(
                service_name="ch-ui",
                url=DEFAULT_CH_UI_HEALTH_URL,
            )
        )

    if not args.skip_streamlit:
        checks.append(
            check_http_service(
                service_name="streamlit",
                url=DEFAULT_STREAMLIT_HEALTH_URL,
            )
        )

    if args.require_api:
        checks.extend(check_control_plane_api())

    summary = summarize_checks(checks)

    if args.format == "json":
        print(json.dumps(summary, indent=DEFAULT_JSON_INDENT, default=str))
    else:
        print(render_text_summary(summary))

    if summary["status"] != "pass":
        raise SystemExit(1)


# --- Running CLI Entrypoint
if __name__ == "__main__":
    main()
