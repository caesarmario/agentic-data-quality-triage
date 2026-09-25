####
## LIFE Supervisor Comparison Preparation for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Validate same-incident single and fan-out supervisor evidence before comparison."""

# --- Importing Libraries
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence
from uuid import UUID


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.evaluation.life import LIFE_SCENARIO_NAMES, normalize_evaluation_run_id
from agent.evaluation.supervisor_comparison import (
    build_supervisor_mode_snapshot,
    validate_comparison_identity,
)
from pipelines.common.clickhouse import build_clickhouse_client
from pipelines.common.logging import logger


# --- Defining Preparation Functions
def prepare_supervisor_comparison(
    scenario_id: str,
    single_parent_run_id: UUID | str,
    fanout_parent_run_id: UUID | str,
    comparison_run_id: str,
) -> dict[str, object]:
    """
    Validate scenario, identities, modes, and retained report availability.

    Args:
        scenario_id: Allowlisted incident ground-truth scenario.
        single_parent_run_id: Parent UUID for single-handoff execution.
        fanout_parent_run_id: Parent UUID for fan-out execution.
        comparison_run_id: Stable Airflow comparison correlation identifier.

    Returns:
        JSON-safe source preparation summary.

    Raises:
        ValueError: If the comparison does not represent one bounded incident.
    """
    if scenario_id not in LIFE_SCENARIO_NAMES:
        raise ValueError(f"Unknown LIFE scenario: {scenario_id}")

    run_id = normalize_evaluation_run_id(comparison_run_id)
    client = build_clickhouse_client()
    single = build_supervisor_mode_snapshot(single_parent_run_id, client)
    fanout = build_supervisor_mode_snapshot(fanout_parent_run_id, client)

    validate_comparison_identity(single, fanout)

    summary = {
        "status": "success",
        "source_mode": "supervisor_comparison",
        "comparison_run_id": run_id,
        "scenario_id": scenario_id,
        "alert_key": single.alert_key,
        "single_parent_run_id": str(single.parent_run_id),
        "fanout_parent_run_id": str(fanout.parent_run_id),
        "single_report_available": bool(single.report_s3_uri),
        "fanout_report_available": bool(fanout.report_s3_uri),
    }

    logger.info(
        "Prepared LIFE supervisor comparison | run_id=%s alert_key=%s single_report=%s fanout_report=%s",
        run_id,
        single.alert_key,
        summary["single_report_available"],
        summary["fanout_report_available"],
    )

    return summary


# --- Defining CLI Helpers
def build_parser() -> argparse.ArgumentParser:
    """
    Build the bounded comparison preparation parser.

    Returns:
        Configured ArgumentParser.
    """
    parser = argparse.ArgumentParser(description="Prepare one LIFE supervisor comparison.")

    parser.add_argument("--scenario", required=True, choices=LIFE_SCENARIO_NAMES)
    parser.add_argument("--single-parent-run-id", required=True)
    parser.add_argument("--fanout-parent-run-id", required=True)
    parser.add_argument("--comparison-run-id", required=True)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """
    Validate comparison sources and print their bounded summary.

    Args:
        argv: Optional command-line arguments used by tests.

    Returns:
        Zero after successful validation.
    """
    args = build_parser().parse_args(argv)
    summary = prepare_supervisor_comparison(
        scenario_id=args.scenario,
        single_parent_run_id=args.single_parent_run_id,
        fanout_parent_run_id=args.fanout_parent_run_id,
        comparison_run_id=args.comparison_run_id,
    )

    print(json.dumps(summary, indent=2, sort_keys=True))

    return 0


# --- Running CLI Entrypoint
if __name__ == "__main__":
    raise SystemExit(main())
