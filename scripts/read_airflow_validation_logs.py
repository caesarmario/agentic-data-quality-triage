####
## Airflow Administrative Log Reader for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Sequence


# --- Defining Constants
VALIDATION_DAG_ID = "91_dag_dq_platform_validation"
TRIAGE_DAG_ID     = "40_dag_dq_orders_triage_agent"
LLM_SMOKE_DAG_ID  = "92_dag_dq_llm_provider_smoke"
CHECKPOINT_SMOKE_DAG_ID = "93_dag_dq_agent_checkpoint_smoke"
LIFE_EVALUATION_DAG_ID  = "94_dag_dq_agent_life_evaluation"
METADATA_SYNC_DAG_ID    = "95_dag_dq_metadata_registry_sync"
SCHEMA_DRIFT_DAG_ID     = "96_dag_dq_schema_drift_detection"
METADATA_LINEAGE_DAG_ID = "97_dag_dq_metadata_lineage_agent_smoke"
CONTROL_PLANE_SUPERVISOR_DAG_ID = "98_dag_dq_control_plane_supervisor_smoke"
CONTROL_PLANE_RESILIENCE_DAG_ID = "99_dag_dq_control_plane_resilience_smoke"
AIRFLOW_LOG_ROOT  = Path("/opt/airflow/logs")
SAFE_IDENTIFIER   = re.compile(r"^[A-Za-z0-9_.-]+$")
SAFE_RUN_ID       = SAFE_IDENTIFIER

ADMINISTRATIVE_DAG_IDS = (
    TRIAGE_DAG_ID,
    VALIDATION_DAG_ID,
    LLM_SMOKE_DAG_ID,
    CHECKPOINT_SMOKE_DAG_ID,
    LIFE_EVALUATION_DAG_ID,
    METADATA_SYNC_DAG_ID,
    SCHEMA_DRIFT_DAG_ID,
    METADATA_LINEAGE_DAG_ID,
    CONTROL_PLANE_SUPERVISOR_DAG_ID,
    CONTROL_PLANE_RESILIENCE_DAG_ID,
)


# --- Defining Functions
def validation_log_directory(run_id: str, log_root: Path = AIRFLOW_LOG_ROOT) -> Path:
    """
    Resolve the bounded Airflow log directory for one validation run.

    Args:
        run_id: Airflow validation run id.
        log_root: Airflow log root, overridable by tests.

    Returns:
        Expected validation run log directory.

    Raises:
        ValueError: If run_id contains path or shell control characters.
    """
    return airflow_log_directory(
        dag_id=VALIDATION_DAG_ID,
        run_id=run_id,
        log_root=log_root,
    )


def airflow_log_directory(
    dag_id: str,
    run_id: str,
    log_root: Path = AIRFLOW_LOG_ROOT,
) -> Path:
    """
    Resolve a bounded log directory for an allowlisted operator DAG.

    Args:
        dag_id: Allowlisted Airflow DAG identifier.
        run_id: Airflow run identifier.
        log_root: Airflow log root, overridable by tests.

    Returns:
        Expected Airflow task log directory.

    Raises:
        ValueError: If the DAG is not allowlisted or identifiers are unsafe.
    """
    if dag_id not in ADMINISTRATIVE_DAG_IDS:
        raise ValueError(f"Unsupported operator DAG id: {dag_id}")

    if not SAFE_IDENTIFIER.fullmatch(dag_id):
        raise ValueError("Airflow DAG id contains unsupported characters.")

    if not SAFE_RUN_ID.fullmatch(run_id):
        raise ValueError("Airflow run id contains unsupported characters.")

    return log_root / f"dag_id={dag_id}" / f"run_id={run_id}"


def print_validation_logs(run_id: str, log_root: Path = AIRFLOW_LOG_ROOT) -> int:
    """
    Print retained Airflow task logs for one validation run.

    Args:
        run_id: Airflow validation run id.
        log_root: Airflow log root, overridable by tests.

    Returns:
        Zero when at least one log file is found, otherwise one.
    """
    return print_airflow_logs(
        dag_id=VALIDATION_DAG_ID,
        run_id=run_id,
        log_root=log_root,
    )


def print_airflow_logs(
    dag_id: str,
    run_id: str,
    log_root: Path = AIRFLOW_LOG_ROOT,
) -> int:
    """
    Print retained task logs for one allowlisted operator DagRun.

    Args:
        dag_id: Allowlisted Airflow DAG identifier.
        run_id: Airflow run identifier.
        log_root: Airflow log root, overridable by tests.

    Returns:
        Zero when at least one log file is found, otherwise one.
    """
    directory = airflow_log_directory(
        dag_id=dag_id,
        run_id=run_id,
        log_root=log_root,
    )
    log_files = sorted(directory.rglob("*.log")) if directory.is_dir() else []

    if not log_files:
        print(f"No Airflow task logs found under {directory}")
        return 1

    for log_file in log_files:
        print(f"\n===== {log_file} =====")
        print(log_file.read_text(encoding="utf-8", errors="replace"))

    return 0


# --- Defining CLI Helpers
def build_parser() -> argparse.ArgumentParser:
    """
    Build the Airflow validation log parser.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(description="Read retained task logs for one allowlisted Airflow run.")
    parser.add_argument("--dag-id", default=VALIDATION_DAG_ID, choices=ADMINISTRATIVE_DAG_IDS)
    parser.add_argument("--run-id", required=True)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """
    Parse arguments and print validation logs.

    Args:
        argv: Optional argument sequence for tests.

    Returns:
        Log reader exit code.
    """
    args = build_parser().parse_args(argv)

    return print_airflow_logs(
        dag_id=args.dag_id,
        run_id=args.run_id,
    )


# --- Running CLI Entrypoint
if __name__ == "__main__":
    raise SystemExit(main())
