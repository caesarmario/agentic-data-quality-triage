####
## Airflow Checkpoint Smoke Trigger Helper for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Trigger the allowlisted Airflow checkpoint smoke with safe JSON configuration."""

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

from agent.checkpointing import validate_checkpoint_thread_id
from pipelines.common.logging import logger


# --- Defining Constants
CHECKPOINT_SMOKE_DAG_ID = "93_dag_dq_agent_checkpoint_smoke"


# --- Defining Functions
def build_checkpoint_smoke_identifiers(
    now: datetime | None = None,
) -> tuple[str, str]:
    """
    Build unique Airflow run and LangGraph thread identifiers.

    Args:
        now: Optional UTC timestamp used by tests.

    Returns:
        Tuple containing run id and thread id.
    """
    current = now or datetime.now(timezone.utc)
    token   = current.strftime("%Y%m%dT%H%M%S%f")

    return f"manual__checkpoint_smoke_{token}", f"checkpoint-smoke-{token}"


def build_trigger_command(run_id: str, thread_id: str) -> list[str]:
    """
    Build the Airflow CLI trigger command without shell interpolation.

    Args:
        run_id: Unique Airflow run identifier.
        thread_id: Unique validated checkpoint thread identifier.

    Returns:
        Subprocess arguments containing compact valid JSON configuration.
    """
    validated_thread = validate_checkpoint_thread_id(thread_id)
    conf             = json.dumps({"thread_id": validated_thread}, separators=(",", ":"))

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
        CHECKPOINT_SMOKE_DAG_ID,
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
    logger.info("Running Airflow checkpoint smoke control command | command=%s", command)
    subprocess.run(command, check=True)


def trigger_checkpoint_smoke(
    run_id: str = "",
    thread_id: str = "",
) -> tuple[str, str]:
    """
    Unpause and trigger one manual checkpoint smoke DagRun.

    Args:
        run_id: Optional explicit Airflow run identifier.
        thread_id: Optional explicit checkpoint thread identifier.

    Returns:
        Resolved run id and thread id.
    """
    generated_run_id, generated_thread_id = build_checkpoint_smoke_identifiers()
    resolved_run_id   = run_id.strip() or generated_run_id
    resolved_thread_id = validate_checkpoint_thread_id(thread_id.strip() or generated_thread_id)

    run_command(["airflow", "dags", "unpause", CHECKPOINT_SMOKE_DAG_ID])
    run_command(build_trigger_command(run_id=resolved_run_id, thread_id=resolved_thread_id))

    print(f"CHECKPOINT_SMOKE_DAG_ID={CHECKPOINT_SMOKE_DAG_ID}")
    print(f"CHECKPOINT_SMOKE_RUN_ID={resolved_run_id}")
    print(f"CHECKPOINT_SMOKE_THREAD_ID={resolved_thread_id}")

    return resolved_run_id, resolved_thread_id


# --- Defining CLI Helpers
def build_parser() -> argparse.ArgumentParser:
    """
    Build the Airflow checkpoint smoke trigger parser.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(description="Trigger the manual Airflow checkpoint smoke DAG.")

    parser.add_argument("--run-id", default="", help="Optional explicit Airflow run id.")
    parser.add_argument("--thread-id", default="", help="Optional explicit checkpoint thread id.")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """
    Parse arguments and trigger one checkpoint smoke DagRun.

    Args:
        argv: Optional argument sequence used by tests.

    Returns:
        Zero when the trigger succeeds.
    """
    args = build_parser().parse_args(argv)
    trigger_checkpoint_smoke(run_id=args.run_id, thread_id=args.thread_id)

    return 0


# --- Running CLI Entrypoint
if __name__ == "__main__":
    raise SystemExit(main())
