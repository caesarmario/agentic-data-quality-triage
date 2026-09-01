####
## Agent Side-Effect Replay Smoke Runner for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Validate sequential replay safety for report artifacts and audit completion events."""

# --- Importing Libraries
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from uuid import NAMESPACE_URL, UUID, uuid5


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.checkpointing import validate_checkpoint_thread_id
from agent.display import build_report_id
from agent.state import Alert, TriageReport
from agent.tools.audit_log import (
    build_audit_event_id,
    build_audit_idempotency_key,
)
from agent.tools.s3 import (
    CONTENT_SHA256_METADATA,
    DEFAULT_ARTIFACTS_BUCKET,
    IMMUTABLE_KEY_POLICY,
    WRITE_POLICY_METADATA,
    build_report_artifact_keys,
    hash_artifact_body,
    inject_report_uri_placeholders,
    serialize_json_artifact,
    store_triage_report,
)
from pipelines.common.clickhouse import build_clickhouse_client
from pipelines.common.logging import logger
from pipelines.seeding.upload_to_s3 import build_s3_client


# --- Defining Constants
SIDE_EFFECT_PHASES         = ("write", "replay", "verify")
DEFAULT_SIDE_EFFECT_PREFIX = "agent-checkpoint-smoke"
FIXED_REPORT_DATE          = date(2026, 8, 27)
FIXED_REPORT_CREATED_AT    = datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc)


# --- Defining Replay Report Helpers
def build_replay_report(thread_id: str) -> TriageReport:
    """
    Build one deterministic synthetic report for a checkpoint smoke thread.

    Args:
        thread_id: Validated path-safe identifier shared by all Airflow tasks.

    Returns:
        TriageReport with stable alert, run, report, and timestamp values.
    """
    validated_thread = validate_checkpoint_thread_id(thread_id)
    thread_hash      = hashlib.sha256(validated_thread.encode("utf-8")).hexdigest()
    agent_run_id     = uuid5(NAMESPACE_URL, f"dq-checkpoint-agent-run:{validated_thread}")
    alert_id         = uuid5(NAMESPACE_URL, f"dq-checkpoint-alert:{validated_thread}")
    alert_key        = (
        "checkpoint_replay|side_effect|2026-08-27|dq.raw_orders|"
        f"artifact_replay|{thread_hash[:16]}"
    )

    alert = Alert(
        alert_id=alert_id,
        alert_key=alert_key,
        status="open",
        alert_type="checkpoint_smoke",
        severity="info",
        table_name="dq.raw_orders",
        metric="artifact_replay",
        dt=FIXED_REPORT_DATE,
        dimension="synthetic",
        details={
            "checkpoint_thread_id": validated_thread,
            "synthetic": True,
        },
    )

    report_id = build_report_id(agent_run_id=agent_run_id, alert_key=alert_key)

    logger.info(
        "Built deterministic replay report | thread_id=%s agent_run_id=%s report_id=%s",
        validated_thread,
        agent_run_id,
        report_id,
    )

    return TriageReport(
        agent_run_id=agent_run_id,
        alert=alert,
        summary="Synthetic checkpoint report used to validate replay-safe side effects.",
        impact="No production data is changed by this administrative smoke test.",
        hypotheses=[],
        confidence=1.0,
        recommended_actions=["Keep sequential replay checks in the Airflow acceptance path."],
        residual_risks=[
            "This smoke validates sequential replay safety, not concurrent distributed exactly-once writes."
        ],
        report_id=report_id,
        markdown_report=(
            "# Agent Side-Effect Replay Smoke\n\n"
            f"Checkpoint thread: `{validated_thread}`\n\n"
            "This synthetic report verifies immutable S3 keys and a deterministic "
            "ClickHouse audit completion event.\n\n"
            "Markdown artifact: {{MARKDOWN_REPORT_S3_URI}}\n\n"
            "JSON artifact: {{JSON_REPORT_S3_URI}}\n"
        ),
        created_at=FIXED_REPORT_CREATED_AT,
    )


def build_expected_artifacts(
    report: TriageReport,
    bucket: str,
    prefix: str,
) -> dict[str, dict[str, Any]]:
    """
    Build expected immutable object bodies and hashes for one replay report.

    Args:
        report: Deterministic synthetic report.
        bucket: S3 artifacts bucket.
        prefix: S3 key prefix used by the smoke run.

    Returns:
        Mapping from object kind to expected key, URI, body, digest, and content type.
    """
    markdown_key, json_key = build_report_artifact_keys(report=report, prefix=prefix)

    report.markdown_report_s3_uri = f"s3://{bucket}/{markdown_key}"
    report.json_report_s3_uri     = f"s3://{bucket}/{json_key}"
    inject_report_uri_placeholders(report)

    markdown_body = report.markdown_report.encode("utf-8")
    json_body     = serialize_json_artifact(report.model_dump(mode="json")).encode("utf-8")

    return {
        "markdown": {
            "key": markdown_key,
            "uri": report.markdown_report_s3_uri,
            "body": markdown_body,
            "sha256": hash_artifact_body(markdown_body),
            "content_type": "text/markdown; charset=utf-8",
        },
        "json": {
            "key": json_key,
            "uri": report.json_report_s3_uri,
            "body": json_body,
            "sha256": hash_artifact_body(json_body),
            "content_type": "application/json; charset=utf-8",
        },
    }


# --- Defining Verification Helpers
def read_complete_object_body(client: Any, bucket: str, key: str) -> bytes:
    """
    Read one complete smoke artifact body from S3-compatible storage.

    Args:
        client: boto3-compatible S3 client.
        bucket: S3 bucket name.
        key: S3 object key.

    Returns:
        Complete object body as bytes.
    """
    response = client.get_object(Bucket=bucket, Key=key)

    return bytes(response["Body"].read())


def verify_replay_side_effects(
    thread_id: str,
    bucket: str = DEFAULT_ARTIFACTS_BUCKET,
    prefix: str = DEFAULT_SIDE_EFFECT_PREFIX,
    endpoint_url: str | None = None,
    clickhouse_host: str | None = None,
    clickhouse_port: int | None = None,
) -> dict[str, Any]:
    """
    Verify immutable artifacts and one deterministic audit completion event.

    Args:
        thread_id: Validated checkpoint thread identifier.
        bucket: S3 artifacts bucket.
        prefix: S3 key prefix used by the smoke run.
        endpoint_url: Optional S3 endpoint override.
        clickhouse_host: Optional ClickHouse host override.
        clickhouse_port: Optional ClickHouse HTTP port override.

    Returns:
        Structured verification evidence for Airflow task logs.

    Raises:
        ValueError: If an artifact or audit event violates the replay contract.
    """
    report             = build_replay_report(thread_id)
    expected_artifacts = build_expected_artifacts(report=report, bucket=bucket, prefix=prefix)
    s3_client          = build_s3_client(endpoint_url=endpoint_url)
    artifact_evidence: dict[str, dict[str, Any]] = {}

    for artifact_kind, expected in expected_artifacts.items():
        head          = s3_client.head_object(Bucket=bucket, Key=expected["key"])
        body          = read_complete_object_body(s3_client, bucket=bucket, key=expected["key"])
        metadata      = {str(name).lower(): str(value) for name, value in (head.get("Metadata") or {}).items()}
        actual_digest = hash_artifact_body(body)
        actual_length = int(head.get("ContentLength", 0) or 0)

        if body != expected["body"]:
            raise ValueError(f"{artifact_kind} artifact body changed across replay.")

        if actual_digest != expected["sha256"]:
            raise ValueError(f"{artifact_kind} artifact digest changed across replay.")

        if actual_length != len(expected["body"]):
            raise ValueError(f"{artifact_kind} artifact length changed across replay.")

        if metadata.get(CONTENT_SHA256_METADATA) != expected["sha256"]:
            raise ValueError(f"{artifact_kind} artifact SHA-256 metadata is missing or invalid.")

        if metadata.get(WRITE_POLICY_METADATA) != IMMUTABLE_KEY_POLICY:
            raise ValueError(f"{artifact_kind} artifact write policy metadata is invalid.")

        artifact_evidence[artifact_kind] = {
            "uri": expected["uri"],
            "sha256": actual_digest,
            "bytes": actual_length,
            "write_policy": metadata[WRITE_POLICY_METADATA],
        }

    markdown = expected_artifacts["markdown"]
    json_artifact = expected_artifacts["json"]
    audit_key = build_audit_idempotency_key(
        "store_triage_report",
        report.agent_run_id,
        markdown["uri"],
        json_artifact["uri"],
        markdown["sha256"],
        json_artifact["sha256"],
    )
    audit_id = build_audit_event_id(audit_key)

    clickhouse_client = build_clickhouse_client(
        host=clickhouse_host,
        port=clickhouse_port,
    )
    audit_result = clickhouse_client.query(
        """
            SELECT
                count() AS event_count,
                any(action) AS action,
                any(status) AS status,
                any(report_s3_uri) AS report_s3_uri
            FROM dq.agent_audit_log
            WHERE audit_id = {audit_id:UUID}
        """,
        parameters={"audit_id": str(audit_id)},
    )
    audit_row = audit_result.result_rows[0] if audit_result.result_rows else (0, "", "", "")
    audit_count, audit_action, audit_status, report_s3_uri = audit_row

    if int(audit_count) != 1:
        raise ValueError(f"Replay-safe audit event count must be one, got {audit_count}.")

    if audit_action != "store_triage_report" or audit_status != "success":
        raise ValueError("Replay-safe audit event does not represent a successful report write.")

    if report_s3_uri != markdown["uri"]:
        raise ValueError("Replay-safe audit event points to the wrong Markdown report URI.")

    result = {
        "status": "success",
        "thread_id": validate_checkpoint_thread_id(thread_id),
        "agent_run_id": str(report.agent_run_id),
        "report_id": report.report_id,
        "idempotency_contract": "sequential-replay-safe",
        "artifacts": artifact_evidence,
        "audit": {
            "audit_id": str(audit_id),
            "event_count": int(audit_count),
            "action": audit_action,
            "status": audit_status,
        },
    }

    logger.info("Agent side-effect replay verified | result=%s", result)

    return result


# --- Defining Phase Execution
def run_side_effect_phase(
    phase: str,
    thread_id: str,
    bucket: str = DEFAULT_ARTIFACTS_BUCKET,
    prefix: str = DEFAULT_SIDE_EFFECT_PREFIX,
    endpoint_url: str | None = None,
    clickhouse_host: str | None = None,
    clickhouse_port: int | None = None,
) -> dict[str, Any]:
    """
    Execute one allowlisted report side-effect replay phase.

    Args:
        phase: One of write, replay, or verify.
        thread_id: Stable checkpoint thread shared by Airflow tasks.
        bucket: S3 artifacts bucket.
        prefix: S3 key prefix used by the smoke run.
        endpoint_url: Optional S3 endpoint override.
        clickhouse_host: Optional ClickHouse host override.
        clickhouse_port: Optional ClickHouse HTTP port override.

    Returns:
        JSON-serializable phase result.

    Raises:
        ValueError: If the phase is not allowlisted.
    """
    normalized_phase = phase.strip().lower()

    if normalized_phase not in SIDE_EFFECT_PHASES:
        raise ValueError(f"Unknown agent side-effect phase: {phase}.")

    validated_thread = validate_checkpoint_thread_id(thread_id)

    logger.info(
        "Starting agent side-effect replay phase | phase=%s thread_id=%s bucket=%s prefix=%s",
        normalized_phase,
        validated_thread,
        bucket,
        prefix,
    )

    if normalized_phase == "verify":
        return verify_replay_side_effects(
            thread_id=validated_thread,
            bucket=bucket,
            prefix=prefix,
            endpoint_url=endpoint_url,
            clickhouse_host=clickhouse_host,
            clickhouse_port=clickhouse_port,
        )

    report = build_replay_report(validated_thread)
    result = store_triage_report(
        report=report,
        bucket=bucket,
        prefix=prefix,
        endpoint_url=endpoint_url,
        clickhouse_host=clickhouse_host,
        clickhouse_port=clickhouse_port,
    )
    phase_result = {
        "status": "success",
        "phase": normalized_phase,
        "thread_id": validated_thread,
        "agent_run_id": str(report.agent_run_id),
        "report_id": report.report_id,
        "markdown_report_s3_uri": result["markdown_report_s3_uri"],
        "json_report_s3_uri": result["json_report_s3_uri"],
        "markdown_sha256": result["markdown_sha256"],
        "json_sha256": result["json_sha256"],
        "idempotency_contract": "sequential-replay-safe",
    }

    logger.info("Agent side-effect replay phase completed | result=%s", phase_result)

    return phase_result


# --- Defining CLI Helpers
def build_parser() -> argparse.ArgumentParser:
    """
    Build the report side-effect replay command-line parser.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(
        description="Run one Airflow phase for sequential report side-effect replay validation."
    )

    parser.add_argument("--phase", required=True, choices=SIDE_EFFECT_PHASES)
    parser.add_argument("--thread-id", required=True)
    parser.add_argument("--bucket", default=DEFAULT_ARTIFACTS_BUCKET)
    parser.add_argument("--prefix", default=DEFAULT_SIDE_EFFECT_PREFIX)
    parser.add_argument("--endpoint-url", default=None)
    parser.add_argument("--clickhouse-host", default=None)
    parser.add_argument("--clickhouse-port", type=int, default=None)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """
    Parse arguments, execute one phase, and print structured evidence.

    Args:
        argv: Optional argument sequence used by validation tests.

    Returns:
        Zero when the selected phase succeeds.
    """
    args   = build_parser().parse_args(argv)
    result = run_side_effect_phase(
        phase=args.phase,
        thread_id=args.thread_id,
        bucket=args.bucket,
        prefix=args.prefix,
        endpoint_url=args.endpoint_url,
        clickhouse_host=args.clickhouse_host,
        clickhouse_port=args.clickhouse_port,
    )

    print(json.dumps(result, indent=2, sort_keys=True))

    return 0


# --- Running CLI Entrypoint
if __name__ == "__main__":
    raise SystemExit(main())
