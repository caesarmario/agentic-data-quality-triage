####
## LIFE Supervisor Comparison Runner for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Evaluate and persist one same-ground-truth single-versus-fan-out comparison."""

# --- Importing Libraries
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.evaluation.life import LIFE_SCENARIO_NAMES
from agent.evaluation.supervisor_comparison import (
    DEFAULT_COMPARISON_ARTIFACT_PREFIX,
    build_supervisor_mode_comparison,
    persist_supervisor_mode_comparison,
)
from pipelines.common.logging import logger


# --- Defining Runtime Functions
def run_supervisor_comparison(
    scenario_id: str,
    single_parent_run_id: str,
    fanout_parent_run_id: str,
    comparison_run_id: str,
    bucket: str | None = None,
    prefix: str = DEFAULT_COMPARISON_ARTIFACT_PREFIX,
    endpoint_url: str | None = None,
) -> dict[str, object]:
    """
    Build and persist one conservative supervisor execution-mode comparison.

    Args:
        scenario_id: Allowlisted incident ground-truth scenario.
        single_parent_run_id: Parent UUID for single-handoff execution.
        fanout_parent_run_id: Parent UUID for fan-out execution.
        comparison_run_id: Stable Airflow comparison correlation identifier.
        bucket: Optional artifact bucket override.
        prefix: Path-safe artifact prefix.
        endpoint_url: Optional S3-compatible endpoint override.

    Returns:
        Compact JSON-safe comparison summary.
    """
    report, _, _ = build_supervisor_mode_comparison(
        scenario_id=scenario_id,
        single_parent_run_id=single_parent_run_id,
        fanout_parent_run_id=fanout_parent_run_id,
        comparison_run_id=comparison_run_id,
        endpoint_url=endpoint_url,
    )
    persisted = persist_supervisor_mode_comparison(
        report=report,
        bucket=bucket,
        prefix=prefix,
        endpoint_url=endpoint_url,
    )
    summary = {
        "status": "success",
        "comparison_run_id": persisted.comparison_run_id,
        "scenario_id": persisted.scenario_id,
        "alert_key": persisted.alert_key,
        "decision": persisted.decision,
        "measurable_benefit": persisted.measurable_benefit,
        "quality_delta": persisted.quality_delta,
        "confidence_delta": persisted.confidence_delta,
        "evidence_reference_delta": persisted.evidence_reference_delta,
        "latency_delta_ms": persisted.latency_delta_ms,
        "estimated_cost_delta_usd": persisted.estimated_cost_delta_usd,
        "json_report_s3_uri": persisted.json_report_s3_uri,
        "markdown_report_s3_uri": persisted.markdown_report_s3_uri,
    }

    logger.info(
        "LIFE supervisor comparison completed | run_id=%s decision=%s benefit=%s",
        persisted.comparison_run_id,
        persisted.decision,
        persisted.measurable_benefit,
    )

    return summary


# --- Defining CLI Helpers
def build_parser() -> argparse.ArgumentParser:
    """
    Build the comparison runner parser.

    Returns:
        Configured ArgumentParser.
    """
    parser = argparse.ArgumentParser(description="Run one LIFE supervisor mode comparison.")

    parser.add_argument("--scenario", required=True, choices=LIFE_SCENARIO_NAMES)
    parser.add_argument("--single-parent-run-id", required=True)
    parser.add_argument("--fanout-parent-run-id", required=True)
    parser.add_argument("--comparison-run-id", required=True)
    parser.add_argument("--bucket", default=None)
    parser.add_argument("--artifact-prefix", default=DEFAULT_COMPARISON_ARTIFACT_PREFIX)
    parser.add_argument("--endpoint-url", default=None)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """
    Parse arguments, run the comparison, and print retained evidence.

    Args:
        argv: Optional command-line arguments used by tests.

    Returns:
        Zero after successful comparison persistence.
    """
    args = build_parser().parse_args(argv)
    summary = run_supervisor_comparison(
        scenario_id=args.scenario,
        single_parent_run_id=args.single_parent_run_id,
        fanout_parent_run_id=args.fanout_parent_run_id,
        comparison_run_id=args.comparison_run_id,
        bucket=args.bucket,
        prefix=args.artifact_prefix,
        endpoint_url=args.endpoint_url,
    )

    print(json.dumps(summary, indent=2, sort_keys=True))

    return 0


# --- Running CLI Entrypoint
if __name__ == "__main__":
    raise SystemExit(main())
