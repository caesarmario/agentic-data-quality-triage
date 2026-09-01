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

from botocore.exceptions import ClientError


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Add repo root before importing project packages when this file is executed by path.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.display import build_report_id
from agent.state import TriageReport
from agent.tools.audit_log import build_audit_idempotency_key, write_agent_audit_event
from pipelines.common.clickhouse import build_clickhouse_client
from pipelines.common.logging import logger
from pipelines.seeding.upload_to_s3 import build_s3_client


# --- Defining Constants
TOOL_NAME                 = "s3_artifacts"
DEFAULT_ARTIFACTS_BUCKET  = "dq-artifacts"
DEFAULT_REPORT_PREFIX     = "agent-reports"
CONTENT_SHA256_METADATA   = "content-sha256"
WRITE_POLICY_METADATA     = "write-policy"
IMMUTABLE_KEY_POLICY      = "immutable-key-sha256"
MAX_ARTIFACT_VERIFY_BYTES = 10 * 1024 * 1024


# --- Defining Exceptions
class ArtifactConflictError(RuntimeError):
    """Raised when an immutable S3 key already contains different content."""


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


def hash_artifact_body(body: bytes) -> str:
    """
    Build the full SHA-256 digest for one artifact body.

    Args:
        body: Raw artifact bytes.

    Returns:
        Lowercase SHA-256 digest.
    """
    return hashlib.sha256(body).hexdigest()


def is_s3_not_found_error(exc: ClientError) -> bool:
    """
    Return whether a boto3 ClientError represents a missing S3 object.

    Args:
        exc: ClientError raised by head_object or get_object.

    Returns:
        True for common S3-compatible missing-object codes.
    """
    error       = exc.response.get("Error", {})
    error_code  = str(error.get("Code", ""))
    status_code = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) or 0)

    return error_code in {"404", "NoSuchKey", "NotFound"} or status_code == 404


def read_existing_artifact_state(
    client: Any,
    bucket: str,
    key: str,
) -> tuple[str, int] | None:
    """
    Read the digest and byte length for an existing immutable artifact.

    Args:
        client: boto3-compatible S3 client.
        bucket: S3 bucket name.
        key: S3 object key.

    Returns:
        Tuple of SHA-256 digest and byte length, or None when the key is absent.

    Raises:
        ArtifactConflictError: If an existing artifact is too large or has invalid metadata.
        ClientError: If S3 returns an error other than object-not-found.
    """
    try:
        head = client.head_object(Bucket=bucket, Key=key)

    except ClientError as exc:
        if is_s3_not_found_error(exc):
            return None

        raise

    content_length = int(head.get("ContentLength", 0) or 0)
    metadata       = {str(name).lower(): str(value) for name, value in (head.get("Metadata") or {}).items()}
    stored_digest  = metadata.get(CONTENT_SHA256_METADATA, "").lower()

    if stored_digest:
        if len(stored_digest) != 64 or any(char not in "0123456789abcdef" for char in stored_digest):
            raise ArtifactConflictError(f"Existing S3 artifact has invalid SHA-256 metadata: s3://{bucket}/{key}")

        return stored_digest, content_length

    if content_length > MAX_ARTIFACT_VERIFY_BYTES:
        raise ArtifactConflictError(
            f"Existing S3 artifact exceeds the bounded digest verification size: s3://{bucket}/{key}"
        )

    response = client.get_object(Bucket=bucket, Key=key)
    body     = response["Body"].read(MAX_ARTIFACT_VERIFY_BYTES + 1)

    if len(body) > MAX_ARTIFACT_VERIFY_BYTES:
        raise ArtifactConflictError(
            f"Existing S3 artifact exceeds the bounded digest verification size: s3://{bucket}/{key}"
        )

    return hash_artifact_body(body), len(body)


def serialize_json_artifact(payload: dict[str, Any] | list[Any]) -> str:
    """
    Serialize a JSON artifact deterministically for stable content hashing.

    Args:
        payload: JSON-serializable dictionary or list.

    Returns:
        Pretty-printed JSON with stable key order.
    """
    return json.dumps(
        payload,
        indent=2,
        ensure_ascii=True,
        default=str,
        sort_keys=True,
    )


def put_text_artifact(
    bucket: str,
    key: str,
    text: str,
    content_type: str = "text/plain; charset=utf-8",
    endpoint_url: str | None = None,
) -> str:
    """
    Write or reuse one immutable text artifact in S3-compatible storage.

    This contract protects sequential retries and replays. SeaweedFS does not expose
    a portable conditional create through this helper, so concurrent writers are not
    represented as distributed exactly-once behavior.

    Args:
        bucket: Target S3 bucket.
        key: Target object key.
        text: Text body to store.
        content_type: S3 ContentType metadata.
        endpoint_url: Optional S3 endpoint override.

    Returns:
        S3 URI of the stored object.
    """
    client      = build_s3_client(endpoint_url=endpoint_url)
    body        = text.encode("utf-8")
    body_digest = hash_artifact_body(body)
    s3_uri      = f"s3://{bucket}/{key}"

    existing_state = read_existing_artifact_state(
        client=client,
        bucket=bucket,
        key=key,
    )

    if existing_state is not None:
        existing_digest, existing_length = existing_state

        if existing_digest != body_digest or existing_length != len(body):
            raise ArtifactConflictError(
                f"Immutable S3 artifact key already contains different content: {s3_uri}"
            )

        logger.info(
            "Reusing immutable S3 artifact | uri=%s sha256=%s bytes=%d",
            s3_uri,
            body_digest,
            len(body),
        )

        return s3_uri

    logger.info(
        "Writing immutable text artifact to S3 | bucket=%s key=%s sha256=%s bytes=%d",
        bucket,
        key,
        body_digest,
        len(body),
    )

    # Explicit ContentType keeps Markdown/JSON artifacts readable in S3-compatible UIs.
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType=content_type,
        Metadata={
            CONTENT_SHA256_METADATA: body_digest,
            WRITE_POLICY_METADATA: IMMUTABLE_KEY_POLICY,
        },
    )

    persisted_state = read_existing_artifact_state(
        client=client,
        bucket=bucket,
        key=key,
    )

    if persisted_state != (body_digest, len(body)):
        raise ArtifactConflictError(f"S3 artifact readback did not match the written content: {s3_uri}")

    logger.info("Immutable text artifact written and verified | uri=%s sha256=%s", s3_uri, body_digest)

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
    text = serialize_json_artifact(payload)

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
    alert_key_hash    = hash_text(report.alert.alert_key)
    agent_run_id      = str(report.agent_run_id)
    report_id         = report.report_id or f"RPT-{hash_text(agent_run_id, length=8).upper()}"
    normalized_prefix = prefix.strip("/")

    base_key = (
        f"{normalized_prefix}/"
        f"dt={dt_token}/"
        f"report_id={report_id}/"
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

    # Ensure manually constructed report objects still get a searchable operator id.
    if not report.report_id:
        report.report_id = build_report_id(report.agent_run_id, report.alert.alert_key)

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
        report_payload = report.model_dump(mode="json")
        json_text      = serialize_json_artifact(report_payload)
        markdown_hash  = hash_artifact_body(report.markdown_report.encode("utf-8"))
        json_hash      = hash_artifact_body(json_text.encode("utf-8"))

        markdown_uri = put_text_artifact(
            bucket=resolved_bucket,
            key=markdown_key,
            text=report.markdown_report,
            content_type="text/markdown; charset=utf-8",
            endpoint_url=endpoint_url,
        )
        json_uri = put_text_artifact(
            bucket=resolved_bucket,
            key=json_key,
            text=json_text,
            content_type="application/json; charset=utf-8",
            endpoint_url=endpoint_url,
        )
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)

        result = {
            "status": "success",
            "bucket": resolved_bucket,
            "report_id": report.report_id,
            "markdown_key": markdown_key,
            "json_key": json_key,
            "markdown_report_s3_uri": markdown_uri,
            "json_report_s3_uri": json_uri,
            "markdown_sha256": markdown_hash,
            "json_sha256": json_hash,
            "idempotency_contract": IMMUTABLE_KEY_POLICY,
        }

        audit_idempotency_key = build_audit_idempotency_key(
            "store_triage_report",
            report.agent_run_id,
            markdown_uri,
            json_uri,
            markdown_hash,
            json_hash,
        )

        write_agent_audit_event(
            client=client,
            action="store_triage_report",
            status="success",
            agent_run_id=report.agent_run_id,
            alert_id=report.alert.alert_id,
            alert_key=report.alert.alert_key,
            tool_name=TOOL_NAME,
            duration_ms=duration_ms,
            input_payload={
                "bucket": resolved_bucket,
                "prefix": prefix,
                "idempotency_contract": IMMUTABLE_KEY_POLICY,
                "markdown_sha256": markdown_hash,
                "json_sha256": json_hash,
            },
            output_payload=result,
            row_count=2,
            report_s3_uri=markdown_uri,
            idempotency_key=audit_idempotency_key,
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
