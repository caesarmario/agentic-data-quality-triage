####
## Airflow Validation Suite Runner for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dags.dq_platform.validation import VALIDATION_SUITE_NAMES
from pipelines.common.logging import logger


# --- Defining Constants
VALIDATION_SUITES: dict[str, tuple[str, ...]] = {
    "all": (),
    "airflow": (
        "tests/test_airflow_dag_design.py",
        "tests/test_backfill_approval_gate.py",
        "tests/test_validation_suite.py",
    ),
    "agent": (
        "tests/test_agent_architecture.py",
        "tests/test_approval_queue.py",
        "tests/test_alert_lifecycle.py",
        "tests/test_alert_lookup.py",
        "tests/test_clickhouse_sql_guardrails.py",
        "tests/test_copilot_narratives.py",
        "tests/test_dbt_lineage.py",
        "tests/test_evidence_planning.py",
        "tests/test_hypothesis_framing.py",
        "tests/test_human_display_helpers.py",
        "tests/test_llm_routing.py",
        "tests/test_triage_evaluation.py",
    ),
    "api": (
        "tests/test_api_app.py",
        "tests/test_control_plane_client.py",
        "tests/test_smoke_readiness.py",
    ),
    "checkpoint": (
        "tests/test_agent_checkpointing.py",
        "tests/test_airflow_dag_design.py",
        "tests/test_validation_suite.py",
    ),
    "discord": (
        "tests/test_control_plane_client.py",
        "tests/test_copilot_narratives.py",
        "tests/test_discord_bot.py",
        "tests/test_discord_formatters.py",
        "tests/test_discord_webhook.py",
    ),
    "dq": (
        "tests/test_dq_evidence.py",
        "tests/test_incident_policy.py",
    ),
    "llm": (
        "tests/test_airflow_dag_design.py",
        "tests/test_llm_routing.py",
        "tests/test_llm_provider_smoke.py",
        "tests/test_validation_suite.py",
    ),
    "life": (
        "tests/test_life_evaluation.py",
        "tests/test_life_history.py",
        "tests/test_airflow_dag_design.py",
        "tests/test_validation_suite.py",
    ),
    "mcp": (
        "tests/test_mcp_server.py",
    ),
    "metadata": (
        "tests/test_metadata_catalog.py",
        "tests/test_metadata_registry.py",
        "tests/test_api_app.py",
        "tests/test_control_plane_client.py",
        "tests/test_mcp_server.py",
        "tests/test_airflow_dag_design.py",
        "tests/test_smoke_readiness.py",
        "tests/test_validation_suite.py",
    ),
    "pipelines": (
        "tests/test_dq_evidence.py",
        "tests/test_incident_policy.py",
        "tests/test_smoke_readiness.py",
    ),
    "ui": (
        "tests/test_streamlit_helpers.py",
    ),
}


if set(VALIDATION_SUITES) != set(VALIDATION_SUITE_NAMES):
    raise RuntimeError("Validation suite registry does not match Airflow validation suite names.")


# --- Defining Validation Helpers
def list_validation_suites() -> tuple[str, ...]:
    """
    Return validation suite names accepted by the Airflow validation DAG.

    Returns:
        Sorted tuple of validation suite names.
    """
    return tuple(sorted(VALIDATION_SUITES))


def resolve_test_paths(suite: str) -> tuple[str, ...]:
    """
    Resolve an allowlisted suite into repository-relative pytest paths.

    Args:
        suite: Named validation suite selected by Airflow.

    Returns:
        Tuple of repository-relative test paths. An empty tuple means all tests.

    Raises:
        ValueError: If the suite is not in the allowlist.
        FileNotFoundError: If an allowlisted test path is missing.
    """
    normalized = suite.strip().lower()

    if normalized not in VALIDATION_SUITES:
        allowed = ", ".join(list_validation_suites())
        raise ValueError(f"Unknown validation suite: {suite}. Allowed suites: {allowed}")

    test_paths = VALIDATION_SUITES[normalized]

    for relative_path in test_paths:
        absolute_path = PROJECT_ROOT / relative_path

        if not absolute_path.is_file():
            raise FileNotFoundError(f"Validation test path does not exist: {relative_path}")

    logger.info("Resolved validation suite | suite=%s test_paths=%s", normalized, test_paths or ("tests",))

    return test_paths


def build_pytest_command(suite: str) -> list[str]:
    """
    Build the bounded pytest command for one named suite.

    Args:
        suite: Named validation suite selected by Airflow.

    Returns:
        Subprocess argument list safe from shell interpolation.
    """
    test_paths = resolve_test_paths(suite)
    command    = [sys.executable, "-m", "pytest", "-q"]

    command.extend(test_paths or ("tests",))

    return command


def run_validation_suite(suite: str) -> int:
    """
    Execute one named validation suite and stream output to the Airflow task log.

    Args:
        suite: Named validation suite selected by Airflow.

    Returns:
        Pytest process exit code.
    """
    normalized = suite.strip().lower()
    command    = build_pytest_command(normalized)
    started_at = datetime.now(timezone.utc)
    started    = time.monotonic()

    logger.info(
        "Starting validation suite | suite=%s started_at=%s command=%s",
        normalized,
        started_at.isoformat(),
        command,
    )

    # Do not capture output: pytest stdout/stderr must remain visible in Airflow logs.
    completed   = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
    duration_ms = int((time.monotonic() - started) * 1000)
    status      = "passed" if completed.returncode == 0 else "failed"

    logger.info(
        "Validation suite completed | suite=%s status=%s return_code=%d duration_ms=%d finished_at=%s",
        normalized,
        status,
        completed.returncode,
        duration_ms,
        datetime.now(timezone.utc).isoformat(),
    )

    return completed.returncode


# --- Defining CLI Helpers
def build_parser() -> argparse.ArgumentParser:
    """
    Build the command-line parser for Airflow validation tasks.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(description="Run an allowlisted project validation suite.")

    parser.add_argument(
        "--suite",
        default="all",
        choices=list_validation_suites(),
        help="Named validation suite. Arbitrary test paths are intentionally not accepted.",
    )
    parser.add_argument(
        "--list-suites",
        action="store_true",
        help="List available validation suites without running pytest.",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """
    Parse CLI arguments and run the selected validation suite.

    Args:
        argv: Optional CLI argument sequence for testing.

    Returns:
        Process exit code.
    """
    args = build_parser().parse_args(argv)

    if args.list_suites:
        print("\n".join(list_validation_suites()))
        return 0

    return run_validation_suite(args.suite)


# --- Running CLI Entrypoint
if __name__ == "__main__":
    raise SystemExit(main())

