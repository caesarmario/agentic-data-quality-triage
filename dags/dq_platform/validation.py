####
## Airflow Validation Helpers for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import json
import logging
from typing import Any


# --- Getting Logger
logger = logging.getLogger(__name__)


# --- Defining Constants
VALIDATION_SUITE_NAMES = (
    "all",
    "agent",
    "airflow",
    "api",
    "checkpoint",
    "discord",
    "dq",
    "llm",
    "life",
    "mcp",
    "metadata",
    "pipelines",
    "ui",
)

VALIDATION_TASK_IDS = (
    "t10_run_named_pytest_suite",
    "t20_run_platform_readiness",
)


# --- Defining Functions
def emit_validation_summary(**context: Any) -> dict[str, Any]:
    """
    Emit an audit-friendly summary after validation tasks succeed.

    Args:
        context: Airflow task context containing dag_run and task metadata.

    Returns:
        Dictionary containing DAG, run, suite, and upstream task states.

    Raises:
        ValueError: If Airflow task context does not contain a DagRun.
    """
    dag_run = context.get("dag_run")

    if dag_run is None:
        raise ValueError("Airflow dag_run context is required for validation summary.")

    # This task uses the default all_success trigger rule, so reaching it proves both
    # upstream validation tasks completed successfully without requiring ORM access.
    task_states = {task_id: "success" for task_id in VALIDATION_TASK_IDS}
    summary = {
        "dag_id": dag_run.dag_id,
        "run_id": dag_run.run_id,
        "validation_suite": str((dag_run.conf or {}).get("validation_suite", "all")),
        "result": "success",
        "state_evidence": "t30 reached after all_success upstream dependencies",
        "task_states": task_states,
    }

    logger.info("Airflow validation summary | payload=%s", json.dumps(summary, sort_keys=True))
    print(json.dumps(summary, indent=2, sort_keys=True))

    return summary

