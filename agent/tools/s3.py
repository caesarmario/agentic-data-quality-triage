####
## Agent S3 Artifact Tool for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Add repo root before importing project packages when this file is executed by path.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.state import TriageReport
from agent.tools.audit_log import write_agent_audit_event
from pipelines.common.clickhouse import build_clickhouse_client
from pipelines.common.logging import logger
from pipelines.seeding.upload_to_s3 import build_s3_client


# --- Defining Constants
TOOL_NAME                = "s3_artifacts"
DEFAULT_ARTIFACTS_BUCKET = "dq-artifacts"
DEFAULT_REPORT_PREFIX    = "agent-reports"


# --- Defining Functions
def resolve_artifacts_bucket(bucket: str | None = None) -> str:
    """
    Resolve the S3 bucket used for agent report artifacts.

    Args:
        bucket: Optional explicit bucket name.

    Returns:
        S3 bucket name for report artifacts.
    """
    resolved_bucket = (
        bucket
        or os.getenv("S3_ARTIFACTS_BUCKET")
        or os.getenv("ARTIFACTS_BUCKET")
        or DEFAULT_ARTIFACTS_BUCKET
    )

    logger.info("Resolved agent artifacts bucket | bucket=%s", resolved_bucket)

    return resolved_bucket


def parse_s3_uri(s3_uri: str) -> tuple[str, str]:
    """
    Split an S3 URI into bucket and object key.

    Args:
        s3_uri: S3 URI such as s3://dq-artifacts/agent-reports/report.json.

    Returns:
        Tuple of bucket and object key.

    Raises:
        ValueError: If the URI is not a valid S3 URI.
    """
    if not s3_uri.startswith("s3://"):
        raise ValueError(f"Expected S3 URI, got: {s3_uri}")

    without_scheme = s3_uri.removeprefix("s3://")
    bucket, _, key = without_scheme.partition("/")

    if not bucket or not key:
        raise ValueError(f"S3 URI must include bucket and key: {s3_uri}")

    return bucket, key


def hash_text(value: str, length: int = 12) -> str:
    """
    Build a compact stable hash for path-safe artifact grouping.

    Args:
        value: Raw text to hash.
        length: Number of hex characters to return.

    Returns:
        Short SHA-256 hex digest.
    """
    digest      = hashlib.sha256(value.encode("utf-8")).hexdigest()
    safe_length = max(8, min(length, len(digest)))

    return digest[:safe_length]


def put_text_artifact(
    bucket: str,
    key: str,
    text: str,
    content_type: str = "text/plain; charset=utf-8",
    endpoint_url: str | None = None,
) -> str:
    """
    Write a text artifact to S3-compatible storage.

    Args:
        bucket: Target S3 bucket.
        key: Target object key.
        text: Text body to store.
        content_type: S3 ContentType metadata.
        endpoint_url: Optional S3 endpoint override.

    Returns:
        S3 URI of the stored object.
    """
    client = build_s3_client(endpoint_url=endpoint_url)
    body   = text.encode("utf-8")

    logger.info("Writing text artifact to S3 | bucket=%s key=%s bytes=%d", bucket, key, len(body))

    # Explicit ContentType keeps Markdown/JSON artifacts readable in S3-compatible UIs.
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType=content_type,
    )

    s3_uri = f"s3://{bucket}/{key}"
    logger.info("Text artifact written | uri=%s", s3_uri)

    return s3_uri


def put_json_artifact(
    bucket: str,
    key: str,
    payload: dict[str, Any] | list[Any],
    endpoint_url: str | None = None,
) -> str:
    """
    Write a JSON artifact to S3-compatible storage.

    Args:
        bucket: Target S3 bucket.
        key: Target object key.
        payload: JSON-serializable payload.
        endpoint_url: Optional S3 endpoint override.

    Returns:
        S3 URI of the stored object.
    """
    text = json.dumps(payload, indent=2, ensure_ascii=True, default=str)

    return put_text_artifact(
        bucket=bucket,
        key=key,
        text=text,
        content_type="application/json; charset=utf-8",
        endpoint_url=endpoint_url,
    )


def build_report_artifact_keys(
    report: TriageReport,
    prefix: str = DEFAULT_REPORT_PREFIX,
) -> tuple[str, str]:
    """
    Build deterministic report artifact keys for one triage run.

    Args:
        report: Triage report that will be persisted.
        prefix: Top-level S3 key prefix for report artifacts.

    Returns:
        Tuple of Markdown key and JSON key.
    """
    dt_token          = report.alert.dt.isoformat() if report.alert.dt else "unknown"
    alert_key_hash   = hash_text(report.alert.alert_key)
    agent_run_id     = str(report.agent_run_id)
    normalized_prefix = prefix.strip("/")

    base_key = (
        f"{normalized_prefix}/"
        f"dt={dt_token}/"
        f"alert_key_hash={alert_key_hash}/"
        f"agent_run_id={agent_run_id}"
    )

    return f"{base_key}/report.md", f"{base_key}/report.json"


def inject_report_uri_placeholders(report: TriageReport) -> None:
    """
    Replace report URI placeholders after S3 keys are known.

    Args:
        report: Triage report with markdown/json S3 URI fields already populated.

    Returns:
        None.
    """
    if not report.markdown_report:
        return

    report.markdown_report = (
        report.markdown_report
        .replace("{{MARKDOWN_REPORT_S3_URI}}", report.markdown_report_s3_uri)
        .replace("{{JSON_REPORT_S3_URI}}", report.json_report_s3_uri)
    )


def store_triage_report(
    report: TriageReport,
    bucket: str | None = None,
    prefix: str = DEFAULT_REPORT_PREFIX,
    endpoint_url: str | None = None,
    clickhouse_host: str | None = None,
    clickhouse_port: int | None = None,
) -> dict[str, Any]:
    """
    Store a triage report as both Markdown and JSON artifacts, then audit the tool call.

    Args:
        report: Triage report to store.
        bucket: Optional S3 bucket override.
        prefix: S3 key prefix for report artifacts.
        endpoint_url: Optional S3 endpoint override.
        clickhouse_host: Optional ClickHouse host override for audit logging.
        clickhouse_port: Optional ClickHouse HTTP port override for audit logging.

    Returns:
        Dictionary with stored artifact URIs and metadata.

    Raises:
        botocore.exceptions.BotoCoreError: If S3 write fails.
        clickhouse_connect.driver.exceptions.ClickHouseError: If audit logging fails.
    """
    resolved_bucket       = resolve_artifacts_bucket(bucket)
    markdown_key, json_key = build_report_artifact_keys(report=report, prefix=prefix)
    client                = build_clickhouse_client(host=clickhouse_host, port=clickhouse_port)
    started_monotonic     = time.monotonic()

    report.markdown_report_s3_uri = f"s3://{resolved_bucket}/{markdown_key}"
    report.json_report_s3_uri     = f"s3://{resolved_bucket}/{json_key}"
    inject_report_uri_placeholders(report)

    logger.info(
        "Storing triage report artifacts | agent_run_id=%s alert_key=%s",
        report.agent_run_id,
        report.alert.alert_key,
    )

    try:
        markdown_uri = put_text_artifact(
            bucket=resolved_bucket,
            key=markdown_key,
            text=report.markdown_report,
            content_type="text/markdown; charset=utf-8",
            endpoint_url=endpoint_url,
        )
        json_uri = put_json_artifact(
            bucket=resolved_bucket,
            key=json_key,
            payload=report.model_dump(mode="json"),
            endpoint_url=endpoint_url,
        )
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)

        result = {
            "status": "success",
            "bucket": resolved_bucket,
            "markdown_key": markdown_key,
            "json_key": json_key,
            "markdown_report_s3_uri": markdown_uri,
            "json_report_s3_uri": json_uri,
        }

        write_agent_audit_event(
            client=client,
            action="store_triage_report",
            status="success",
            agent_run_id=report.agent_run_id,
            alert_id=report.alert.alert_id,
            alert_key=report.alert.alert_key,
            tool_name=TOOL_NAME,
            duration_ms=duration_ms,
            input_payload={"bucket": resolved_bucket, "prefix": prefix},
            output_payload=result,
            row_count=2,
            report_s3_uri=markdown_uri,
        )

        logger.info("Triage report artifacts stored | markdown_uri=%s json_uri=%s", markdown_uri, json_uri)

        return result

    except Exception as exc:
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)
        logger.exception("Failed to store triage report artifacts | alert_key=%s", report.alert.alert_key)

        write_agent_audit_event(
            client=client,
            action="store_triage_report",
            status="failed",
            agent_run_id=report.agent_run_id,
            alert_id=report.alert.alert_id,
            alert_key=report.alert.alert_key,
            tool_name=TOOL_NAME,
            duration_ms=duration_ms,
            input_payload={"bucket": resolved_bucket, "prefix": prefix},
            output_payload={"error_type": type(exc).__name__},
            error_message=str(exc),
            report_s3_uri=report.markdown_report_s3_uri,
        )

        raise


def build_parser() -> argparse.ArgumentParser:
    """
    Build the command-line parser for S3 artifact smoke testing.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(description="Write a small S3 artifact to local SeaweedFS S3.")

    parser.add_argument("--bucket", default=None, help="Optional target bucket. Defaults to dq-artifacts.")
    parser.add_argument("--key", default=None, help="Optional object key for the text artifact.")
    parser.add_argument("--text", default="agent s3 smoke test", help="Text body to write.")
    parser.add_argument("--endpoint-url", default=None, help="Optional S3 endpoint URL override.")

    return parser


def main() -> None:
    """
    Parse CLI arguments and write a smoke-test text artifact.

    Returns:
        None.
    """
    parser = build_parser()
    args   = parser.parse_args()

    bucket = resolve_artifacts_bucket(args.bucket)
    key    = args.key or f"smoke/agent-s3-smoke-{uuid4()}.txt"
    uri    = put_text_artifact(
        bucket=bucket,
        key=key,
        text=args.text,
        endpoint_url=args.endpoint_url,
    )

    print(json.dumps({"status": "success", "s3_uri": uri}, indent=2))


# --- Running CLI Entrypoint
if __name__ == "__main__":
    main()
