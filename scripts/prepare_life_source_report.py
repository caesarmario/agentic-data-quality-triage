####
## LIFE Source Report Preparation for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Validate stored LIFE report sources or publish deterministic replay sources."""

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

from agent.evaluation.life import LIFE_SCENARIO_NAMES, validate_report_s3_uri
from agent.evaluation.replay import (
    DEFAULT_LIFE_REPLAY_PREFIX,
    LIFE_REPLAY_SCENARIO_NAMES,
    LIFE_SOURCE_MODES,
    build_replay_report_s3_uri,
    persist_life_replay_report,
)
from agent.evaluation.triage import (
    load_yaml_file,
    resolve_scenario_path,
    validate_scenario_config,
)
from pipelines.common.logging import logger


# --- Defining Source Preparation
def prepare_life_source_report(
    source_mode: str,
    scenario_id: str,
    report_s3_uri: str,
    evaluation_run_id: str,
    replay_prefix: str = DEFAULT_LIFE_REPLAY_PREFIX,
    endpoint_url: str | None = None,
) -> dict[str, str | bool]:
    """
    Validate a stored report reference or publish an evaluation-only replay report.

    Args:
        source_mode: Either stored_report or scenario_replay.
        scenario_id: Allowlisted LIFE scenario identifier.
        report_s3_uri: Expected source report URI consumed by downstream tasks.
        evaluation_run_id: Stable replay and evaluation correlation identifier.
        replay_prefix: Top-level S3 prefix for replay source artifacts.
        endpoint_url: Optional S3-compatible endpoint override.

    Returns:
        Source preparation summary for the Airflow task log.

    Raises:
        ValueError: If mode, scenario, or source URI violates the bounded contract.
    """
    normalized_mode     = source_mode.strip().lower()
    normalized_scenario = scenario_id.strip()

    if normalized_mode not in LIFE_SOURCE_MODES:
        raise ValueError(f"Unknown LIFE source mode: {source_mode}")

    if normalized_scenario not in LIFE_SCENARIO_NAMES:
        raise ValueError(f"Unknown LIFE scenario: {scenario_id}")

    normalized_report_uri = validate_report_s3_uri(report_s3_uri)

    if normalized_mode == "stored_report":
        logger.info(
            "Validated stored LIFE source report | scenario=%s uri=%s",
            normalized_scenario,
            normalized_report_uri,
        )

        return {
            "status": "success",
            "source_mode": normalized_mode,
            "scenario_id": normalized_scenario,
            "report_s3_uri": normalized_report_uri,
            "source_created": False,
        }

    if normalized_scenario not in LIFE_REPLAY_SCENARIO_NAMES:
        raise ValueError(f"Scenario does not support LIFE replay: {normalized_scenario}")

    scenario_path = resolve_scenario_path(normalized_scenario)
    scenario      = load_yaml_file(scenario_path)
    validate_scenario_config(path=scenario_path, scenario=scenario)
    expected_uri = build_replay_report_s3_uri(
        scenario_id=normalized_scenario,
        replay_run_id=evaluation_run_id,
        prefix=replay_prefix,
    )

    if normalized_report_uri != expected_uri:
        raise ValueError("LIFE replay source URI does not match its deterministic artifact key.")

    persist_life_replay_report(
        scenario=scenario,
        replay_run_id=evaluation_run_id,
        prefix=replay_prefix,
        endpoint_url=endpoint_url,
    )

    return {
        "status": "success",
        "source_mode": normalized_mode,
        "scenario_id": normalized_scenario,
        "report_s3_uri": normalized_report_uri,
        "source_created": True,
    }


# --- Defining CLI Helpers
def build_parser() -> argparse.ArgumentParser:
    """
    Build the bounded source-preparation CLI parser.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(description="Prepare one stored or replay LIFE source report.")

    parser.add_argument("--source-mode", required=True, choices=LIFE_SOURCE_MODES)
    parser.add_argument("--scenario", required=True, choices=LIFE_SCENARIO_NAMES)
    parser.add_argument("--report-s3-uri", required=True)
    parser.add_argument("--evaluation-run-id", required=True)
    parser.add_argument("--replay-prefix", default=DEFAULT_LIFE_REPLAY_PREFIX)
    parser.add_argument("--endpoint-url", default=None)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """
    Prepare the source report and print its correlation summary for Airflow.

    Args:
        argv: Optional argument sequence used by tests.

    Returns:
        Zero after successful source validation or publication.
    """
    args    = build_parser().parse_args(argv)
    summary = prepare_life_source_report(
        source_mode=args.source_mode,
        scenario_id=args.scenario,
        report_s3_uri=args.report_s3_uri,
        evaluation_run_id=args.evaluation_run_id,
        replay_prefix=args.replay_prefix,
        endpoint_url=args.endpoint_url,
    )

    print(json.dumps(summary, indent=2, ensure_ascii=True))

    return 0


# --- Running CLI Entrypoint
if __name__ == "__main__":
    raise SystemExit(main())
