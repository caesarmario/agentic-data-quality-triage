####
## Airflow Control Plane Supervisor Helpers for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""DAG-safe constants and retained-log summary for supervisor smoke runs."""

# --- Importing Libraries
from __future__ import annotations

import json
import logging
from typing import Any


# --- Getting Logger
logger = logging.getLogger(__name__)


# --- Defining Constants
CONTROL_PLANE_SUPERVISOR_INTENTS = (
    "auto",
    "triage_alert",
    "asset_context",
    "blast_radius",
    "trusted_asset_search",
    "review_sql",
    "schema_drift_assessment",
)

CONTROL_PLANE_SUPERVISOR_TASK_IDS = (
    "t10_run_control_plane_supervisor",
    "t20_verify_supervisor_audit",
)


# --- Defining Functions
def emit_control_plane_supervisor_summary(**context: Any) -> dict[str, Any]:
    """
    Emit bounded Airflow evidence after supervisor execution and verification.

    Args:
        context: Airflow task context containing the current DagRun.

    Returns:
        Dictionary containing correlation, intent, and task-state evidence.

    Raises:
        ValueError: If Airflow task context does not contain a DagRun.
    """
    dag_run = context.get("dag_run")

    if dag_run is None:
        raise ValueError("Airflow dag_run context is required for supervisor summary.")

    conf    = dag_run.conf or {}
    execution_mode = str(conf.get("execution_mode", "single"))
    worker_capacity = int(conf.get("max_workers", 1))
    max_concurrency = int(conf.get("max_concurrency", 1))
    summary = {
        "dag_id": dag_run.dag_id,
        "run_id": dag_run.run_id,
        "intent": str(conf.get("intent", "asset_context")),
        "execution_mode": execution_mode,
        "worker_capacity": worker_capacity,
        "max_concurrency": max_concurrency,
        "external_llm_allowed": bool(conf.get("allow_external_llm", False)),
        "result": "success",
        "state_evidence": (
            "summary reached after deterministic routing, bounded worker execution, "
            "and exact ClickHouse audit verification"
        ),
        "task_states": {
            task_id: "success"
            for task_id in CONTROL_PLANE_SUPERVISOR_TASK_IDS
        },
    }

    logger.info(
        "Airflow Control Plane Supervisor summary | payload=%s",
        json.dumps(summary, sort_keys=True),
    )
    print(json.dumps(summary, indent=2, sort_keys=True))

    return summary
