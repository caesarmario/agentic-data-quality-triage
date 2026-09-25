####
## LIFE Supervisor Comparison Verifier for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Verify comparison artifacts, immutable source identities, and audit evidence."""

# --- Importing Libraries
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence
from uuid import UUID


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.evaluation.life import LIFE_SCENARIO_NAMES, normalize_evaluation_run_id
from agent.evaluation.supervisor_comparison import (
    COMPARISON_DECISIONS,
    DEFAULT_COMPARISON_ARTIFACT_PREFIX,
    SUPERVISOR_COMPARISON_ACTION,
    SupervisorModeComparisonReport,
    build_comparison_artifact_keys,
)
from agent.tools.s3 import resolve_artifacts_bucket
from pipelines.common.clickhouse import build_clickhouse_client
from pipelines.common.logging import logger
from pipelines.seeding.upload_to_s3 import build_s3_client


# --- Defining Load Helpers
def load_s3_text(client: Any, bucket: str, key: str) -> str:
    """
    Read one UTF-8 comparison artifact.

    Args:
        client: Boto3-compatible S3 client.
        bucket: Source bucket.
        key: Source object key.

    Returns:
        Decoded UTF-8 object body.
    """
    logger.info("Reading comparison artifact | bucket=%s key=%s", bucket, key)
    response = client.get_object(Bucket=bucket, Key=key)

    return response["Body"].read().decode("utf-8")


def query_comparison_audit(client: Any, comparison_run_id: str) -> list[tuple[Any, ...]]:
    """
    Load the replay-safe comparison audit event.

    Args:
        client: ClickHouse client.
        comparison_run_id: Stable comparison correlation identifier.

    Returns:
        Matching newest-first audit rows.
    """
    result = client.query(
        """
            SELECT
                action,
                status,
                input_json,
                output_json,
                report_s3_uri
            FROM dq.agent_audit_log
            WHERE ts >= now() - INTERVAL 7 DAY
              AND action = {action:String}
              AND JSONExtractString(input_json, 'comparison_run_id') = {run_id:String}
            ORDER BY ts DESC
            LIMIT 10
        """,
        parameters={
            "action": SUPERVISOR_COMPARISON_ACTION,
            "run_id": comparison_run_id,
        },
    )

    return list(result.result_rows)


# --- Defining End-To-End Verification
def verify_supervisor_comparison(
    comparison_run_id: str,
    scenario_id: str,
    single_parent_run_id: UUID | str,
    fanout_parent_run_id: UUID | str,
    bucket: str | None = None,
    prefix: str = DEFAULT_COMPARISON_ARTIFACT_PREFIX,
    endpoint_url: str | None = None,
    s3_client: Any | None = None,
    clickhouse_client: Any | None = None,
) -> dict[str, object]:
    """
    Verify persisted comparison content and ClickHouse audit correlation.

    Args:
        comparison_run_id: Stable comparison artifact identifier.
        scenario_id: Expected ground-truth scenario.
        single_parent_run_id: Expected single-handoff parent UUID.
        fanout_parent_run_id: Expected fan-out parent UUID.
        bucket: Optional artifacts bucket override.
        prefix: Path-safe artifact prefix.
        endpoint_url: Optional S3-compatible endpoint override.
        s3_client: Optional injected S3 client.
        clickhouse_client: Optional injected ClickHouse client.

    Returns:
        JSON-safe verification summary.

    Raises:
        ValueError: If any artifact, identity, safety, or audit invariant fails.
    """
    run_id         = normalize_evaluation_run_id(comparison_run_id)
    expected_single = UUID(str(single_parent_run_id))
    expected_fanout = UUID(str(fanout_parent_run_id))

    if scenario_id not in LIFE_SCENARIO_NAMES:
        raise ValueError(f"Unknown LIFE scenario: {scenario_id}")

    resolved_bucket        = resolve_artifacts_bucket(bucket)
    json_key, markdown_key = build_comparison_artifact_keys(run_id, prefix=prefix)
    json_uri               = f"s3://{resolved_bucket}/{json_key}"
    markdown_uri           = f"s3://{resolved_bucket}/{markdown_key}"
    resolved_s3_client     = s3_client or build_s3_client(endpoint_url=endpoint_url)
    payload = json.loads(load_s3_text(resolved_s3_client, resolved_bucket, json_key))
    markdown_text = load_s3_text(resolved_s3_client, resolved_bucket, markdown_key)
    report        = SupervisorModeComparisonReport.model_validate(payload)
    errors: list[str] = []

    if report.comparison_run_id != run_id:
        errors.append("Comparison run id differs from the Airflow request")

    if report.scenario_id != scenario_id:
        errors.append("Comparison scenario differs from the Airflow request")

    if report.single.parent_run_id != expected_single:
        errors.append("Single parent run identity differs from the Airflow request")

    if report.fanout.parent_run_id != expected_fanout:
        errors.append("Fan-out parent run identity differs from the Airflow request")

    if report.single.execution_mode != "single" or report.fanout.execution_mode != "fanout":
        errors.append("Comparison artifacts contain incorrect execution modes")

    if report.decision not in COMPARISON_DECISIONS:
        errors.append("Comparison decision is outside the bounded policy")

    if report.measurable_benefit and report.decision != "fanout_candidate":
        errors.append("Measurable benefit may only accompany fanout_candidate")

    if not report.requires_human_review:
        errors.append("Comparison is missing its mandatory human review boundary")

    if report.json_report_s3_uri != json_uri:
        errors.append("Comparison JSON URI differs from its deterministic key")

    if report.markdown_report_s3_uri != markdown_uri:
        errors.append("Comparison Markdown URI differs from its deterministic key")

    if report.markdown_report != markdown_text:
        errors.append("Comparison Markdown object differs from embedded Markdown")

    if "cannot enable fan-out" not in markdown_text:
        errors.append("Comparison Markdown omits the non-automatic promotion boundary")

    resolved_clickhouse = clickhouse_client or build_clickhouse_client()
    audit_rows          = query_comparison_audit(resolved_clickhouse, run_id)

    if len(audit_rows) != 1:
        errors.append("Expected exactly one replay-safe comparison audit event")
    else:
        action, status, input_json, output_json, audit_report_uri = audit_rows[0]
        audit_input  = json.loads(input_json)
        audit_output = json.loads(output_json)

        if action != SUPERVISOR_COMPARISON_ACTION or status != "success":
            errors.append("Comparison audit event is not successful")

        if audit_report_uri != json_uri:
            errors.append("Comparison audit references the wrong artifact")

        if audit_input.get("single_parent_run_id") != str(expected_single):
            errors.append("Comparison audit references the wrong single parent")

        if audit_input.get("fanout_parent_run_id") != str(expected_fanout):
            errors.append("Comparison audit references the wrong fan-out parent")

        if audit_output.get("decision") != report.decision:
            errors.append("Comparison audit decision differs from the artifact")

        if audit_output.get("requires_human_review") is not True:
            errors.append("Comparison audit omits mandatory human review")

    if errors:
        raise ValueError("Supervisor comparison verification failed: " + "; ".join(errors))

    summary = {
        "status": "success",
        "comparison_run_id": run_id,
        "scenario_id": report.scenario_id,
        "alert_key": report.alert_key,
        "decision": report.decision,
        "measurable_benefit": report.measurable_benefit,
        "quality_delta": report.quality_delta,
        "confidence_delta": report.confidence_delta,
        "evidence_reference_delta": report.evidence_reference_delta,
        "json_report_s3_uri": json_uri,
        "markdown_report_s3_uri": markdown_uri,
        "audit_event_count": len(audit_rows),
    }

    logger.info(
        "Verified LIFE supervisor comparison | run_id=%s decision=%s audit_events=%d",
        run_id,
        report.decision,
        len(audit_rows),
    )

    return summary


# --- Defining CLI Helpers
def build_parser() -> argparse.ArgumentParser:
    """
    Build the comparison verifier parser.

    Returns:
        Configured ArgumentParser.
    """
    parser = argparse.ArgumentParser(description="Verify one LIFE supervisor comparison.")

    parser.add_argument("--comparison-run-id", required=True)
    parser.add_argument("--scenario", required=True, choices=LIFE_SCENARIO_NAMES)
    parser.add_argument("--single-parent-run-id", required=True)
    parser.add_argument("--fanout-parent-run-id", required=True)
    parser.add_argument("--bucket", default=None)
    parser.add_argument("--artifact-prefix", default=DEFAULT_COMPARISON_ARTIFACT_PREFIX)
    parser.add_argument("--endpoint-url", default=None)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """
    Parse arguments, verify comparison evidence, and print the summary.

    Args:
        argv: Optional command-line arguments used by tests.

    Returns:
        Zero after successful verification.
    """
    args = build_parser().parse_args(argv)
    summary = verify_supervisor_comparison(
        comparison_run_id=args.comparison_run_id,
        scenario_id=args.scenario,
        single_parent_run_id=args.single_parent_run_id,
        fanout_parent_run_id=args.fanout_parent_run_id,
        bucket=args.bucket,
        prefix=args.artifact_prefix,
        endpoint_url=args.endpoint_url,
    )

    print(json.dumps(summary, indent=2, sort_keys=True))

    return 0


# --- Running CLI Entrypoint
if __name__ == "__main__":
    raise SystemExit(main())
