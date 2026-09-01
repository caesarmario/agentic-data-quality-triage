####
## Airflow LIFE Evaluation Trigger Helper for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Trigger the bounded LIFE evaluation DAG with safely encoded JSON configuration."""

# --- Importing Libraries
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.evaluation.life import (
    DEFAULT_LIFE_ARTIFACT_PREFIX,
    DEFAULT_MIN_CONFIDENCE,
    LIFE_SCENARIO_NAMES,
    build_life_artifact_keys,
    normalize_evaluation_run_id,
    validate_report_s3_uri,
)
from agent.evaluation.replay import (
    DEFAULT_LIFE_REPLAY_PREFIX,
    LIFE_REPLAY_SCENARIO_NAMES,
    LIFE_SOURCE_MODES,
    build_replay_report_s3_uri,
)
from pipelines.common.logging import logger


# --- Defining Constants
LIFE_EVALUATION_DAG_ID = "94_dag_dq_agent_life_evaluation"


# --- Defining Functions
def build_life_evaluation_identifiers(now: datetime | None = None) -> tuple[str, str]:
    """
    Build unique Airflow and LIFE correlation identifiers.

    Args:
        now: Optional UTC timestamp used by tests.

    Returns:
        Tuple containing Airflow run id and LIFE evaluation run id.
    """
    current = now or datetime.now(timezone.utc)
    token   = current.strftime("%Y%m%dT%H%M%S%f")

    return f"manual__life_eval_{token}", f"life-eval-{token}"


def build_trigger_command(
    run_id: str,
    evaluation_run_id: str,
    scenario_id: str,
    report_s3_uri: str = "",
    source_mode: str = "stored_report",
    minimum_confidence: float = DEFAULT_MIN_CONFIDENCE,
    artifact_prefix: str = DEFAULT_LIFE_ARTIFACT_PREFIX,
    fail_on_eval_failure: bool = False,
    enable_critic: bool = False,
) -> list[str]:
    """
    Build an Airflow trigger command without shell interpolation.

    Args:
        run_id: Unique Airflow run identifier.
        evaluation_run_id: Unique LIFE artifact correlation identifier.
        scenario_id: Allowlisted incident scenario.
        report_s3_uri: Source triage report JSON URI. Optional only for replay mode.
        source_mode: Stored report or deterministic scenario replay.
        minimum_confidence: Review threshold from zero to one.
        artifact_prefix: Path-safe target artifact prefix.
        fail_on_eval_failure: Whether hard reliability failure should fail the DAG.
        enable_critic: Whether the DAG should add a bounded critic review.

    Returns:
        Subprocess argument list containing compact valid JSON configuration.

    Raises:
        ValueError: If scenario, URI, threshold, run id, or prefix is invalid.
    """
    if scenario_id not in LIFE_SCENARIO_NAMES:
        raise ValueError(f"Unknown LIFE scenario: {scenario_id}")

    normalized_source_mode = source_mode.strip().lower()

    if normalized_source_mode not in LIFE_SOURCE_MODES:
        raise ValueError(f"Unknown LIFE source mode: {source_mode}")

    if not 0.0 <= minimum_confidence <= 1.0:
        raise ValueError("LIFE minimum confidence must be between 0.0 and 1.0.")

    normalized_airflow_run = normalize_evaluation_run_id(run_id)
    normalized_evaluation  = normalize_evaluation_run_id(evaluation_run_id)

    if normalized_source_mode == "scenario_replay":
        if scenario_id not in LIFE_REPLAY_SCENARIO_NAMES:
            raise ValueError(f"Scenario does not support LIFE replay: {scenario_id}")

        derived_report_uri = build_replay_report_s3_uri(
            scenario_id=scenario_id,
            replay_run_id=normalized_evaluation,
            prefix=DEFAULT_LIFE_REPLAY_PREFIX,
        )

        if report_s3_uri and validate_report_s3_uri(report_s3_uri) != derived_report_uri:
            raise ValueError("LIFE replay report URI must match its deterministic artifact key.")

        normalized_report_uri = derived_report_uri

    else:
        normalized_report_uri = validate_report_s3_uri(report_s3_uri)

    # Reuse artifact-key validation before handing any prefix to Airflow templating.
    build_life_artifact_keys(normalized_evaluation, prefix=artifact_prefix)

    conf = json.dumps(
        {
            "scenario": scenario_id,
            "source_mode": normalized_source_mode,
            "report_s3_uri": normalized_report_uri,
            "evaluation_run_id": normalized_evaluation,
            "minimum_confidence": minimum_confidence,
            "artifact_prefix": artifact_prefix,
            "fail_on_eval_failure": fail_on_eval_failure,
            "enable_critic": enable_critic,
        },
        separators=(",", ":"),
    )

    return [
        "airflow",
        "dags",
        "trigger",
        "-r",
        normalized_airflow_run,
        "-c",
        conf,
        "-o",
        "table",
        LIFE_EVALUATION_DAG_ID,
    ]


def run_command(command: list[str]) -> None:
    """
    Run one Airflow control command and stream output.

    Args:
        command: Bounded subprocess argument list.

    Returns:
        None.

    Raises:
        CalledProcessError: If Airflow rejects the command.
    """
    logger.info("Running Airflow LIFE evaluation control command | command=%s", command)
    subprocess.run(command, check=True)


def trigger_life_evaluation(
    scenario_id: str,
    report_s3_uri: str = "",
    source_mode: str = "stored_report",
    minimum_confidence: float = DEFAULT_MIN_CONFIDENCE,
    artifact_prefix: str = DEFAULT_LIFE_ARTIFACT_PREFIX,
    fail_on_eval_failure: bool = False,
    enable_critic: bool = False,
    run_id: str = "",
    evaluation_run_id: str = "",
) -> tuple[str, str]:
    """
    Unpause and trigger one manual LIFE evaluation DagRun.

    Args:
        scenario_id: Allowlisted incident ground-truth scenario.
        report_s3_uri: Source report JSON URI. Optional only for replay mode.
        source_mode: Stored report or deterministic scenario replay.
        minimum_confidence: Review threshold from zero to one.
        artifact_prefix: Path-safe target artifact prefix.
        fail_on_eval_failure: Whether failed report evaluation fails the DagRun.
        enable_critic: Whether to run a deterministic critic before the proposal.
        run_id: Optional explicit Airflow run id.
        evaluation_run_id: Optional explicit LIFE artifact correlation id.

    Returns:
        Resolved Airflow run id and evaluation run id.
    """
    generated_run_id, generated_evaluation_id = build_life_evaluation_identifiers()
    resolved_run_id        = run_id.strip() or generated_run_id
    resolved_evaluation_id = evaluation_run_id.strip() or generated_evaluation_id
    command = build_trigger_command(
        run_id=resolved_run_id,
        evaluation_run_id=resolved_evaluation_id,
        scenario_id=scenario_id,
        report_s3_uri=report_s3_uri,
        source_mode=source_mode,
        minimum_confidence=minimum_confidence,
        artifact_prefix=artifact_prefix,
        fail_on_eval_failure=fail_on_eval_failure,
        enable_critic=enable_critic,
    )

    run_command(["airflow", "dags", "unpause", LIFE_EVALUATION_DAG_ID])
    run_command(command)

    print(f"LIFE_EVALUATION_DAG_ID={LIFE_EVALUATION_DAG_ID}")
    print(f"LIFE_EVALUATION_RUN_ID={resolved_run_id}")
    print(f"LIFE_ARTIFACT_RUN_ID={resolved_evaluation_id}")
    print(f"LIFE_SOURCE_MODE={source_mode}")

    return resolved_run_id, resolved_evaluation_id


# --- Defining CLI Helpers
def build_parser() -> argparse.ArgumentParser:
    """
    Build the Airflow LIFE evaluation trigger parser.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(description="Trigger the manual Airflow LIFE evaluation DAG.")

    parser.add_argument("--scenario", required=True, choices=LIFE_SCENARIO_NAMES)
    parser.add_argument("--source-mode", choices=LIFE_SOURCE_MODES, default="stored_report")
    parser.add_argument("--report-s3-uri", default="")
    parser.add_argument("--minimum-confidence", type=float, default=DEFAULT_MIN_CONFIDENCE)
    parser.add_argument("--artifact-prefix", default=DEFAULT_LIFE_ARTIFACT_PREFIX)
    parser.add_argument("--fail-on-eval-failure", action="store_true")
    parser.add_argument("--enable-critic", action="store_true")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--evaluation-run-id", default="")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """
    Parse arguments and trigger one LIFE evaluation DagRun.

    Args:
        argv: Optional argument sequence used by tests.

    Returns:
        Zero when the trigger succeeds.
    """
    args = build_parser().parse_args(argv)
    trigger_life_evaluation(
        scenario_id=args.scenario,
        report_s3_uri=args.report_s3_uri,
        source_mode=args.source_mode,
        minimum_confidence=args.minimum_confidence,
        artifact_prefix=args.artifact_prefix,
        fail_on_eval_failure=args.fail_on_eval_failure,
        enable_critic=args.enable_critic,
        run_id=args.run_id,
        evaluation_run_id=args.evaluation_run_id,
    )

    return 0


# --- Running CLI Entrypoint
if __name__ == "__main__":
    raise SystemExit(main())
