####
## Airflow Schema Drift Helpers for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""DAG-safe constants and summary helpers for schema drift operations."""

# --- Importing Libraries
from __future__ import annotations

import json
import logging
from typing import Any


# --- Getting Logger
logger = logging.getLogger(__name__)


# --- Defining Constants
SCHEMA_CONTRACT_NAMES = ("orders",)

SCHEMA_DRIFT_TASK_IDS = (
    "t10_detect_schema_drift",
    "t20_verify_schema_drift_evidence",
)


# --- Defining Functions
def emit_schema_drift_summary(**context: Any) -> dict[str, Any]:
    """
    Emit bounded Airflow evidence after detection and verification succeed.

    Args:
        context: Airflow task context containing the current DagRun.

    Returns:
        Dictionary containing run correlation, contract, and task-state evidence.

    Raises:
        ValueError: If Airflow task context does not contain a DagRun.
    """
    dag_run = context.get("dag_run")

    if dag_run is None:
        raise ValueError("Airflow dag_run context is required for schema drift summary.")

    summary = {
        "dag_id": dag_run.dag_id,
        "run_id": dag_run.run_id,
        "contract_name": str((dag_run.conf or {}).get("contract_name", "orders")),
        "result": "success",
        "state_evidence": "summary reached after schema capture, comparison, persistence, and verification",
        "task_states": {task_id: "success" for task_id in SCHEMA_DRIFT_TASK_IDS},
    }

    logger.info("Airflow schema drift summary | payload=%s", json.dumps(summary, sort_keys=True))
    print(json.dumps(summary, indent=2, sort_keys=True))

    return summary
