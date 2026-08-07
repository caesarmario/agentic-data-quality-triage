####
## Airflow Metadata Registry Helpers for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""DAG-safe constants and summary helpers for metadata registry operations."""

# --- Importing Libraries
from __future__ import annotations

import json
import logging
from typing import Any


# --- Getting Logger
logger = logging.getLogger(__name__)


# --- Defining Constants
METADATA_REGISTRY_NAMES = ("orders",)

METADATA_SYNC_TASK_IDS = (
    "t10_sync_metadata_registry",
    "t20_verify_metadata_registry",
)


# --- Defining Functions
def emit_metadata_sync_summary(**context: Any) -> dict[str, Any]:
    """
    Emit a bounded Airflow summary after sync and verification succeed.

    Args:
        context: Airflow task context containing the current DagRun.

    Returns:
        Dictionary containing run correlation, registry, and task state evidence.

    Raises:
        ValueError: If Airflow task context does not contain a DagRun.
    """
    dag_run = context.get("dag_run")

    if dag_run is None:
        raise ValueError("Airflow dag_run context is required for metadata sync summary.")

    summary = {
        "dag_id": dag_run.dag_id,
        "run_id": dag_run.run_id,
        "registry_name": str((dag_run.conf or {}).get("registry_name", "orders")),
        "result": "success",
        "state_evidence": "summary reached after sync and exact registry verification",
        "task_states": {task_id: "success" for task_id in METADATA_SYNC_TASK_IDS},
    }

    logger.info("Airflow metadata sync summary | payload=%s", json.dumps(summary, sort_keys=True))
    print(json.dumps(summary, indent=2, sort_keys=True))

    return summary
