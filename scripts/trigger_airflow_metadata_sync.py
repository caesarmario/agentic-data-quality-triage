####
## Airflow Metadata Registry Trigger Helper for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Trigger the allowlisted metadata registry sync DAG without shell interpolation."""

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

from dags.dq_platform.metadata_registry import METADATA_REGISTRY_NAMES
from pipelines.common.logging import logger


# --- Defining Constants
METADATA_SYNC_DAG_ID = "95_dag_dq_metadata_registry_sync"


# --- Defining Functions
def validate_registry_name(registry_name: str) -> str:
    """
    Validate and normalize one requested metadata registry.

    Args:
        registry_name: Raw registry name from CLI input.

    Returns:
        Normalized allowlisted registry name.

    Raises:
        ValueError: If the registry is not supported.
    """
    normalized = registry_name.strip().lower()

    if normalized not in METADATA_REGISTRY_NAMES:
        raise ValueError(f"Unknown metadata registry: {registry_name}")

    return normalized


def build_metadata_sync_run_id(registry_name: str, now: datetime | None = None) -> str:
    """
    Build a unique audit-friendly metadata sync run identifier.

    Args:
        registry_name: Allowlisted metadata registry name.
        now: Optional UTC timestamp used by tests.

    Returns:
        Airflow run id without shell-sensitive characters.
    """
    normalized = validate_registry_name(registry_name)
    current    = now or datetime.now(timezone.utc)

    return f"manual__metadata_sync_{normalized}_{current.strftime('%Y%m%dT%H%M%S%f')}"


def build_trigger_command(registry_name: str, run_id: str) -> list[str]:
    """
    Build the Airflow CLI trigger command with bounded JSON configuration.

    Args:
        registry_name: Allowlisted metadata registry name.
        run_id: Explicit Airflow run id.

    Returns:
        Subprocess argument list safe from shell parsing.
    """
    normalized = validate_registry_name(registry_name)
    conf       = json.dumps({"registry_name": normalized}, separators=(",", ":"))

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
        METADATA_SYNC_DAG_ID,
    ]


def run_command(command: list[str]) -> None:
    """
    Run one Airflow CLI command and stream its output.

    Args:
        command: Subprocess argument list.

    Returns:
        None.

    Raises:
        CalledProcessError: If the Airflow command fails.
    """
    logger.info("Running Airflow metadata control command | command=%s", command)
    subprocess.run(command, check=True)


def trigger_metadata_sync(registry_name: str, run_id: str = "") -> str:
    """
    Unpause and trigger the manual metadata registry sync DAG.

    Args:
        registry_name: Allowlisted metadata registry name.
        run_id: Optional explicit run id for audit correlation.

    Returns:
        Run id created for the metadata sync DagRun.
    """
    normalized      = validate_registry_name(registry_name)
    resolved_run_id = run_id.strip() or build_metadata_sync_run_id(normalized)

    run_command(["airflow", "dags", "unpause", METADATA_SYNC_DAG_ID])
    run_command(build_trigger_command(normalized, resolved_run_id))

    print(f"METADATA_SYNC_DAG_ID={METADATA_SYNC_DAG_ID}")
    print(f"METADATA_SYNC_RUN_ID={resolved_run_id}")
    print(f"METADATA_REGISTRY={normalized}")

    return resolved_run_id


# --- Defining CLI Helpers
def build_parser() -> argparse.ArgumentParser:
    """
    Build the metadata sync trigger parser.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(description="Trigger the manual metadata registry sync DAG.")
    parser.add_argument("--registry", default="orders", choices=METADATA_REGISTRY_NAMES)
    parser.add_argument("--run-id", default="", help="Optional explicit run id for audit lookup.")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """
    Parse arguments and trigger the metadata sync DAG.

    Args:
        argv: Optional argument sequence used by tests.

    Returns:
        Zero when the trigger command succeeds.
    """
    args = build_parser().parse_args(argv)
    trigger_metadata_sync(registry_name=args.registry, run_id=args.run_id)

    return 0


# --- Running CLI Entrypoint
if __name__ == "__main__":
    raise SystemExit(main())
