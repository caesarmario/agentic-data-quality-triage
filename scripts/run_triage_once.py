####
## Triage Runner CLI for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Add repo root before importing project packages when this file is executed by path.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.graph import (
    DEFAULT_CONFIDENCE_TARGET,
    DEFAULT_MAX_EVIDENCE_LOOP,
    DEFAULT_REPORT_PREFIX,
    TriageRuntimeConfig,
    run_triage,
)
from agent.checkpointing import CHECKPOINT_MODE_OFF
from pipelines.common.logging import logger


# --- Defining Functions
def build_parser() -> argparse.ArgumentParser:
    """
    Build the command-line parser for the one-alert triage runner.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(description="Run one agentic DQ triage workflow and store report artifacts.")

    parser.add_argument("--alert-id", default=None, help="Optional ClickHouse alert UUID.")
    parser.add_argument("--alert-key", default=None, help="Optional stable alert key.")
    parser.add_argument("--confidence-threshold", type=float, default=DEFAULT_CONFIDENCE_TARGET, help="Confidence threshold.")
    parser.add_argument("--max-evidence-iterations", type=int, default=DEFAULT_MAX_EVIDENCE_LOOP, help="Maximum evidence loops.")
    parser.add_argument("--manifest-path", default=None, help="Optional local dbt manifest.json path.")
    parser.add_argument("--manifest-s3-uri", default=None, help="Optional S3 URI for dbt manifest.json.")
    parser.add_argument("--endpoint-url", default=None, help="Optional S3 endpoint URL override.")
    parser.add_argument("--artifacts-bucket", default=None, help="Optional artifacts bucket override.")
    parser.add_argument("--artifacts-prefix", default=DEFAULT_REPORT_PREFIX, help="S3 prefix for report artifacts.")
    parser.add_argument("--clickhouse-host", default=None, help="Optional ClickHouse host override.")
    parser.add_argument("--clickhouse-port", type=int, default=None, help="Optional ClickHouse HTTP port override.")
    parser.add_argument("--checkpoint-mode", default=CHECKPOINT_MODE_OFF, help="Checkpoint mode: off or sqlite.")
    parser.add_argument("--checkpoint-sqlite-path", default=None, help="Absolute SQLite checkpoint path override.")
    parser.add_argument("--checkpoint-thread-id", default=None, help="Stable checkpoint thread identifier.")
    parser.add_argument("--checkpoint-resume", action="store_true", help="Resume an existing checkpoint thread.")

    return parser


def main() -> None:
    """
    Parse CLI arguments, run triage, and print a compact operational summary.

    Returns:
        None.

    Raises:
        ValueError: If neither alert_id nor alert_key is provided.
    """
    parser = build_parser()
    args   = parser.parse_args()

    if not args.alert_id and not args.alert_key:
        raise ValueError("Provide --alert-id or --alert-key.")

    config = TriageRuntimeConfig(
        manifest_path=args.manifest_path,
        manifest_s3_uri=args.manifest_s3_uri,
        s3_endpoint_url=args.endpoint_url,
        artifacts_bucket=args.artifacts_bucket,
        artifacts_prefix=args.artifacts_prefix,
        clickhouse_host=args.clickhouse_host,
        clickhouse_port=args.clickhouse_port,
    )

    logger.info("Running one triage workflow from CLI | alert_id=%s alert_key=%s", args.alert_id, args.alert_key)

    report = run_triage(
        alert_id=args.alert_id,
        alert_key=args.alert_key,
        confidence_threshold=args.confidence_threshold,
        max_evidence_iterations=args.max_evidence_iterations,
        config=config,
        checkpoint_mode=args.checkpoint_mode,
        checkpoint_sqlite_path=args.checkpoint_sqlite_path,
        checkpoint_thread_id=args.checkpoint_thread_id,
        checkpoint_resume=args.checkpoint_resume,
    )
    output = {
        "status": "success",
        "agent_run_id": str(report.agent_run_id),
        "alert_key": report.alert.alert_key,
        "severity": report.alert.severity,
        "confidence": report.confidence,
        "top_hypothesis": report.top_hypothesis.title if report.top_hypothesis else None,
        "markdown_report_s3_uri": report.markdown_report_s3_uri,
        "json_report_s3_uri": report.json_report_s3_uri,
        "approval_gated_actions": [action.model_dump(mode="json") for action in report.approval_gated_actions],
        "checkpoint_mode": args.checkpoint_mode,
        "checkpoint_thread_id": args.checkpoint_thread_id or "",
        "checkpoint_resume_requested": args.checkpoint_resume,
    }

    print(json.dumps(output, indent=2, default=str))


# --- Running CLI Entrypoint
if __name__ == "__main__":
    main()
