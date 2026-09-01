####
## Airflow Schema Drift Trigger Helper for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Trigger the allowlisted schema drift DAG without shell interpolation."""

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

from dags.dq_platform.schema_drift import SCHEMA_CONTRACT_NAMES
from pipelines.common.logging import logger


# --- Defining Constants
SCHEMA_DRIFT_DAG_ID = "96_dag_dq_schema_drift_detection"


# --- Defining Functions
def validate_contract_name(contract_name: str) -> str:
    """
    Validate and normalize one requested schema contract alias.

    Args:
        contract_name: Raw contract alias from CLI input.

    Returns:
        Normalized allowlisted contract alias.

    Raises:
        ValueError: If the contract is not supported.
    """
    normalized = contract_name.strip().lower()

    if normalized not in SCHEMA_CONTRACT_NAMES:
        raise ValueError(f"Unknown schema contract: {contract_name}")

    return normalized


def build_schema_drift_run_id(contract_name: str, now: datetime | None = None) -> str:
    """
    Build a unique audit-friendly schema drift run identifier.

    Args:
        contract_name: Allowlisted schema contract alias.
        now: Optional UTC timestamp used by tests.

    Returns:
        Airflow run id without shell-sensitive characters.
    """
    normalized = validate_contract_name(contract_name)
    current    = now or datetime.now(timezone.utc)

    return f"manual__schema_drift_{normalized}_{current.strftime('%Y%m%dT%H%M%S%f')}"


def build_trigger_command(contract_name: str, run_id: str) -> list[str]:
    """
    Build the Airflow CLI trigger command with bounded JSON configuration.

    Args:
        contract_name: Allowlisted schema contract alias.
        run_id: Explicit Airflow run identifier.

    Returns:
        Subprocess argument list safe from shell parsing.
    """
    normalized = validate_contract_name(contract_name)
    conf       = json.dumps({"contract_name": normalized}, separators=(",", ":"))

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
        SCHEMA_DRIFT_DAG_ID,
    ]


def run_command(command: list[str]) -> None:
    """
    Run one Airflow CLI command and stream output.

    Args:
        command: Subprocess argument list.

    Returns:
        None.

    Raises:
        CalledProcessError: If the Airflow command fails.
    """
    logger.info("Running Airflow schema drift control command | command=%s", command)
    subprocess.run(command, check=True)


def trigger_schema_drift(contract_name: str, run_id: str = "") -> str:
    """
    Unpause and trigger the manual schema drift DAG.

    Args:
        contract_name: Allowlisted schema contract alias.
        run_id: Optional explicit run id for audit correlation.

    Returns:
        Run id created for the schema drift DagRun.
    """
    normalized      = validate_contract_name(contract_name)
    resolved_run_id = run_id.strip() or build_schema_drift_run_id(normalized)

    run_command(["airflow", "dags", "unpause", SCHEMA_DRIFT_DAG_ID])
    run_command(build_trigger_command(normalized, resolved_run_id))

    print(f"SCHEMA_DRIFT_DAG_ID={SCHEMA_DRIFT_DAG_ID}")
    print(f"SCHEMA_DRIFT_RUN_ID={resolved_run_id}")
    print(f"SCHEMA_CONTRACT={normalized}")

    return resolved_run_id


# --- Defining CLI Helpers
def build_parser() -> argparse.ArgumentParser:
    """
    Build the schema drift trigger parser.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(description="Trigger the manual schema drift detection DAG.")
    parser.add_argument("--contract", default="orders", choices=SCHEMA_CONTRACT_NAMES)
    parser.add_argument("--run-id", default="", help="Optional explicit run id for audit lookup.")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """
    Parse arguments and trigger the schema drift DAG.

    Args:
        argv: Optional argument sequence used by tests.

    Returns:
        Zero when the trigger command succeeds.
    """
    args = build_parser().parse_args(argv)
    trigger_schema_drift(contract_name=args.contract, run_id=args.run_id)

    return 0


# --- Running CLI Entrypoint
if __name__ == "__main__":
    raise SystemExit(main())
