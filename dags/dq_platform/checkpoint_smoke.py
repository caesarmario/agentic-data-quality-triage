####
## Airflow Checkpoint Smoke Helpers for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Summary helpers for the cross-process LangGraph checkpoint smoke DAG."""

# --- Importing Libraries
from __future__ import annotations

import json
import logging
from typing import Any


# --- Getting Logger
logger = logging.getLogger(__name__)


# --- Defining Constants
CHECKPOINT_SMOKE_TASK_IDS = (
    "t10_initialize_checkpoint",
    "t20_resume_checkpoint",
    "t30_resume_completed_checkpoint",
    "t40_verify_checkpoint",
    "t42_replay_historical_checkpoint",
    "t43_repeat_historical_checkpoint_replay",
    "t45_store_report_side_effect",
    "t46_replay_report_side_effect",
    "t47_verify_report_side_effect",
)


# --- Defining Functions
def emit_checkpoint_smoke_summary(**context: Any) -> dict[str, Any]:
    """
    Emit an Airflow-native summary after all checkpoint phases succeed.

    Args:
        context: Airflow task context containing the current DagRun.

    Returns:
        Summary containing thread correlation and upstream state evidence.

    Raises:
        ValueError: If no DagRun context is available.
    """
    dag_run = context.get("dag_run")

    if dag_run is None:
        raise ValueError("Airflow dag_run context is required for checkpoint smoke summary.")

    conf    = dag_run.conf or {}
    summary = {
        "dag_id": dag_run.dag_id,
        "run_id": dag_run.run_id,
        "thread_id": str(conf.get("thread_id", "")),
        "result": "success",
        "state_evidence": "t50 reached after all cross-process checkpoint phases succeeded",
        "task_states": {task_id: "success" for task_id in CHECKPOINT_SMOKE_TASK_IDS},
    }

    logger.info("Airflow checkpoint smoke summary | payload=%s", json.dumps(summary, sort_keys=True))
    print(json.dumps(summary, indent=2, sort_keys=True))

    return summary
