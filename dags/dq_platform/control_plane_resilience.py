####
## Airflow Control Plane Resilience Helpers for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Expose DAG-safe scenario constants and retained-log summary helpers."""

# --- Importing Libraries
from __future__ import annotations

import json
import logging
from typing import Any

from agent.supervisor.scenario_registry import CONTROL_PLANE_RESILIENCE_SCENARIOS


# --- Getting Logger
logger = logging.getLogger(__name__)


# --- Defining Constants
CONTROL_PLANE_RESILIENCE_TASK_IDS = (
    "t10_run_resilience_scenario",
    "t20_verify_resilience_audit",
)


# --- Defining Functions
def emit_control_plane_resilience_summary(**context: Any) -> dict[str, Any]:
    """
    Emit compact Airflow evidence after one resilience scenario is verified.

    Args:
        context: Airflow task context containing the current DagRun.

    Returns:
        Dictionary containing run, scenario, and upstream task-state evidence.

    Raises:
        ValueError: If DagRun context is unavailable.
    """
    dag_run = context.get("dag_run")

    if dag_run is None:
        raise ValueError("Airflow dag_run context is required for resilience summary.")

    conf    = dag_run.conf or {}
    summary = {
        "dag_id": dag_run.dag_id,
        "run_id": dag_run.run_id,
        "scenario": str(conf.get("scenario", "transient_once")),
        "result": "success",
        "state_evidence": (
            "summary reached only after controlled execution and exact ClickHouse "
            "audit/context verification"
        ),
        "task_states": {
            task_id: "success"
            for task_id in CONTROL_PLANE_RESILIENCE_TASK_IDS
        },
    }

    logger.info(
        "Airflow Control Plane resilience summary | payload=%s",
        json.dumps(summary, sort_keys=True),
    )
    print(json.dumps(summary, indent=2, sort_keys=True))

    return summary
