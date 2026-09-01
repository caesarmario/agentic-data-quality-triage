####
## Airflow Control Plane Resilience Trigger for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Validate and trigger one manual supervisor resilience Airflow scenario."""

# --- Importing Libraries
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Sequence
from zoneinfo import ZoneInfo


# --- Resolving Project Imports
PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.supervisor.scenario_registry import (
    supported_control_plane_resilience_scenarios,
)
from pipelines.common.logging import logger


# --- Defining Constants
CONTROL_PLANE_RESILIENCE_DAG_ID = "99_dag_dq_control_plane_resilience_smoke"
LOCAL_TIMEZONE                  = ZoneInfo("Asia/Bangkok")
SAFE_RUN_ID                     = re.compile(r"^[A-Za-z0-9_.:+-]{1,250}$")


# --- Defining Trigger Helpers
def build_resilience_run_id(scenario: str) -> str:
    """
    Build a sortable Asia/Bangkok manual DagRun identifier.

    Args:
        scenario: Validated resilience scenario.

    Returns:
        Unique Airflow run ID.
    """
    timestamp = datetime.now(LOCAL_TIMEZONE).strftime("%Y%m%dT%H%M%S%f")

    return f"manual__control_plane_resilience_{scenario}_{timestamp}"


def validate_trigger_inputs(scenario: str, run_id: str = "") -> tuple[str, str]:
    """
    Validate the scenario allowlist and optional run identifier.

    Args:
        scenario: Requested controlled failure scenario.
        run_id: Optional explicit Airflow run ID.

    Returns:
        Normalized scenario and resolved run ID.

    Raises:
        ValueError: If scenario or run ID is not allowlisted and safe.
    """
    normalized_scenario = scenario.strip().lower()

    if normalized_scenario not in supported_control_plane_resilience_scenarios():
        raise ValueError(f"Unsupported resilience scenario: {scenario}")

    resolved_run_id = run_id.strip() or build_resilience_run_id(normalized_scenario)

    if not SAFE_RUN_ID.fullmatch(resolved_run_id):
        raise ValueError("Resilience Airflow run ID contains unsupported characters.")

    return normalized_scenario, resolved_run_id


def build_trigger_command(scenario: str, run_id: str) -> list[str]:
    """
    Build an argv-only Airflow trigger command without shell interpolation.

    Args:
        scenario: Validated scenario.
        run_id: Validated explicit DagRun identifier.

    Returns:
        Airflow CLI argument list.
    """
    conf = json.dumps({"scenario": scenario}, separators=(",", ":"))

    return [
        "airflow",
        "dags",
        "trigger",
        "-r",
        run_id,
        "-c",
        conf,
        "-o",
        "table",
        CONTROL_PLANE_RESILIENCE_DAG_ID,
    ]


def run_command(command: list[str]) -> None:
    """
    Run one Airflow CLI command and preserve its output.

    Args:
        command: Argument list passed directly to subprocess.

    Returns:
        None.

    Raises:
        CalledProcessError: If Airflow returns non-zero.
    """
    logger.info("Running Airflow resilience control command | command=%s", command)
    subprocess.run(command, check=True)


def trigger_control_plane_resilience(
    scenario: str,
    run_id: str = "",
) -> str:
    """
    Unpause and trigger one controlled resilience smoke DagRun.

    Args:
        scenario: Allowlisted resilience scenario.
        run_id: Optional explicit DagRun identifier.

    Returns:
        Resolved Airflow run ID.
    """
    normalized_scenario, resolved_run_id = validate_trigger_inputs(
        scenario=scenario,
        run_id=run_id,
    )

    run_command(["airflow", "dags", "unpause", CONTROL_PLANE_RESILIENCE_DAG_ID])
    run_command(build_trigger_command(normalized_scenario, resolved_run_id))

    print(f"CONTROL_PLANE_RESILIENCE_DAG_ID={CONTROL_PLANE_RESILIENCE_DAG_ID}")
    print(f"CONTROL_PLANE_RESILIENCE_RUN_ID={resolved_run_id}")
    print(f"CONTROL_PLANE_RESILIENCE_SCENARIO={normalized_scenario}")

    return resolved_run_id


# --- Defining CLI
def build_parser() -> argparse.ArgumentParser:
    """
    Build the resilience Airflow trigger CLI parser.

    Returns:
        Configured argparse parser.
    """
    parser = argparse.ArgumentParser(
        description="Trigger one manual Control Plane resilience smoke scenario."
    )
    parser.add_argument(
        "--scenario",
        default="transient_once",
        choices=supported_control_plane_resilience_scenarios(),
    )
    parser.add_argument("--run-id", default="")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """
    Parse arguments and trigger the resilience DAG.

    Args:
        argv: Optional explicit argument sequence.

    Returns:
        Zero when Airflow accepts the trigger.
    """
    args = build_parser().parse_args(argv)
    trigger_control_plane_resilience(
        scenario=args.scenario,
        run_id=args.run_id,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
