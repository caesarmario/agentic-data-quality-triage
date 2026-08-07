####
## LIFE Evaluation Artifact Verifier for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Verify persisted LIFE artifacts and their ClickHouse audit evidence."""

# --- Importing Libraries
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.evaluation.life import (
    DEFAULT_LIFE_ARTIFACT_PREFIX,
    LIFE_SCENARIO_NAMES,
    LifeEvaluationReport,
    build_life_artifact_keys,
    normalize_evaluation_run_id,
    validate_report_s3_uri,
)
from agent.tools.s3 import resolve_artifacts_bucket
from pipelines.common.clickhouse import build_clickhouse_client
from pipelines.common.logging import logger
from pipelines.seeding.upload_to_s3 import build_s3_client


# --- Defining S3 Verification Helpers
def load_s3_text(client: Any, bucket: str, key: str) -> str:
    """
    Read one UTF-8 artifact from S3-compatible storage.

    Args:
        client: Boto3-compatible S3 client.
        bucket: Source bucket name.
        key: Source object key.

    Returns:
        Decoded UTF-8 object body.
    """
    logger.info("Reading LIFE artifact | bucket=%s key=%s", bucket, key)
    response = client.get_object(Bucket=bucket, Key=key)

    return response["Body"].read().decode("utf-8")


# --- Defining Audit Verification Helpers
def query_life_audit_events(client: Any, evaluation_run_id: str) -> list[tuple[Any, ...]]:
    """
    Fetch recent audit events for one normalized LIFE evaluation run.

    Args:
        client: clickhouse-connect client instance.
        evaluation_run_id: Stable evaluation run identifier stored in output_json.

    Returns:
        Matching audit rows ordered from newest to oldest.
    """
    query = """
        SELECT
            action,
            status,
            output_json,
            report_s3_uri
        FROM dq.agent_audit_log
        WHERE ts >= now() - INTERVAL 7 DAY
          AND action = 'life_evaluation_completed'
          AND JSONExtractString(output_json, 'run_id') = {run_id:String}
        ORDER BY ts DESC
        LIMIT 10
    """
    result = client.query(query, parameters={"run_id": evaluation_run_id})

    return list(result.result_rows)


# --- Defining End-To-End Verification
def verify_life_evaluation(
    evaluation_run_id: str,
    scenario_id: str,
    expected_source_report_s3_uri: str,
    bucket: str | None = None,
    prefix: str = DEFAULT_LIFE_ARTIFACT_PREFIX,
    endpoint_url: str | None = None,
    s3_client: Any | None = None,
    clickhouse_client: Any | None = None,
) -> dict[str, Any]:
    """
    Verify JSON, Markdown, source correlation, and ClickHouse audit evidence.

    Args:
        evaluation_run_id: Stable LIFE evaluation identifier.
        scenario_id: Expected incident scenario identifier.
        expected_source_report_s3_uri: Source report URI supplied to the evaluator.
        bucket: Optional artifacts bucket override.
        prefix: LIFE artifact prefix.
        endpoint_url: Optional S3 endpoint override.
        s3_client: Optional injected S3 client for tests.
        clickhouse_client: Optional injected ClickHouse client for tests.

    Returns:
        JSON-ready verification summary.

    Raises:
        ValueError: If any persisted artifact or audit invariant is violated.
    """
    run_id = normalize_evaluation_run_id(evaluation_run_id)
    source_report_s3_uri = validate_report_s3_uri(expected_source_report_s3_uri)

    if scenario_id not in LIFE_SCENARIO_NAMES:
        raise ValueError(f"Unknown LIFE scenario: {scenario_id}")

    resolved_bucket        = resolve_artifacts_bucket(bucket)
    json_key, markdown_key = build_life_artifact_keys(run_id, prefix=prefix)
    json_uri               = f"s3://{resolved_bucket}/{json_key}"
    markdown_uri           = f"s3://{resolved_bucket}/{markdown_key}"
    resolved_s3_client     = s3_client or build_s3_client(endpoint_url=endpoint_url)
    json_text              = load_s3_text(resolved_s3_client, resolved_bucket, json_key)
    markdown_text          = load_s3_text(resolved_s3_client, resolved_bucket, markdown_key)
    payload                = json.loads(json_text)
    evaluation             = LifeEvaluationReport.model_validate(payload)
    errors                 = []

    if evaluation.run_id != run_id:
        errors.append("JSON run_id does not match requested evaluation run")

    if evaluation.scenario_id != scenario_id:
        errors.append("JSON scenario_id does not match requested scenario")

    if evaluation.report_s3_uri != source_report_s3_uri:
        errors.append("JSON source report URI does not match the Airflow request")

    if evaluation.json_report_s3_uri != json_uri:
        errors.append("JSON artifact URI does not match its deterministic key")

    if evaluation.markdown_report_s3_uri != markdown_uri:
        errors.append("Markdown artifact URI does not match its deterministic key")

    if evaluation.markdown_report != markdown_text:
        errors.append("Markdown object differs from the Markdown embedded in JSON")

    if "does not modify prompts" not in markdown_text:
        errors.append("Markdown artifact does not state the non-mutating safety boundary")

    resolved_clickhouse_client = clickhouse_client or build_clickhouse_client()
    audit_rows                 = query_life_audit_events(resolved_clickhouse_client, run_id)

    if not audit_rows:
        errors.append("No matching life_evaluation_completed audit event was found")
    else:
        _, audit_status, audit_output_json, audit_report_uri = audit_rows[0]
        audit_payload = json.loads(audit_output_json)

        if audit_status != "success":
            errors.append("Latest LIFE audit event is not successful")

        if audit_report_uri != json_uri:
            errors.append("Latest LIFE audit event references the wrong JSON artifact")

        if audit_payload.get("source_report_sha256") != evaluation.source_report_sha256:
            errors.append("Latest LIFE audit event has a different source report digest")

    if errors:
        raise ValueError("LIFE verification failed: " + "; ".join(errors))

    summary = {
        "status": "success",
        "evaluation_run_id": run_id,
        "scenario_id": scenario_id,
        "eval_status": evaluation.eval_status,
        "failure_categories": evaluation.failure_categories,
        "requires_human_approval": evaluation.requires_human_approval,
        "source_report_sha256": evaluation.source_report_sha256,
        "json_report_s3_uri": json_uri,
        "markdown_report_s3_uri": markdown_uri,
        "audit_event_count": len(audit_rows),
    }

    logger.info(
        "LIFE artifacts and audit verified | run_id=%s scenario=%s eval_status=%s audit_events=%d",
        run_id,
        scenario_id,
        evaluation.eval_status,
        len(audit_rows),
    )

    return summary


# --- Defining CLI Helpers
def build_parser() -> argparse.ArgumentParser:
    """
    Build the LIFE verification command-line parser.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(description="Verify LIFE S3 artifacts and ClickHouse audit evidence.")

    parser.add_argument("--evaluation-run-id", required=True)
    parser.add_argument("--scenario", required=True, choices=LIFE_SCENARIO_NAMES)
    parser.add_argument("--source-report-s3-uri", required=True)
    parser.add_argument("--bucket", default=None)
    parser.add_argument("--artifact-prefix", default=DEFAULT_LIFE_ARTIFACT_PREFIX)
    parser.add_argument("--endpoint-url", default=None)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """
    Parse arguments and print one verification summary for Airflow logs.

    Args:
        argv: Optional argument sequence used by tests.

    Returns:
        Zero after successful verification.
    """
    args    = build_parser().parse_args(argv)
    summary = verify_life_evaluation(
        evaluation_run_id=args.evaluation_run_id,
        scenario_id=args.scenario,
        expected_source_report_s3_uri=args.source_report_s3_uri,
        bucket=args.bucket,
        prefix=args.artifact_prefix,
        endpoint_url=args.endpoint_url,
    )

    print(json.dumps(summary, indent=2, ensure_ascii=True))

    return 0


# --- Running CLI Entrypoint
if __name__ == "__main__":
    raise SystemExit(main())
