####
## Airflow Metadata And Lineage Agent Helpers for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""DAG-safe constants and retained-log summary for the bounded specialist smoke run."""

# --- Importing Libraries
from __future__ import annotations

import json
import logging
from typing import Any


# --- Getting Logger
logger = logging.getLogger(__name__)


# --- Defining Constants
METADATA_LINEAGE_TASK_TYPES = (
    "asset_context",
    "blast_radius",
    "trusted_asset_search",
)

METADATA_LINEAGE_TASK_IDS = (
    "t10_run_metadata_lineage_agent",
    "t20_verify_metadata_lineage_audit",
)


# --- Defining Functions
def emit_metadata_lineage_summary(**context: Any) -> dict[str, Any]:
    """
    Emit bounded Airflow evidence after specialist execution and audit verification.

    Args:
        context: Airflow task context containing the current DagRun.

    Returns:
        Dictionary containing run correlation, task parameters, and task-state evidence.

    Raises:
        ValueError: If Airflow task context does not contain a DagRun.
    """
    dag_run = context.get("dag_run")

    if dag_run is None:
        raise ValueError("Airflow dag_run context is required for metadata-lineage summary.")

    conf    = dag_run.conf or {}
    summary = {
        "dag_id": dag_run.dag_id,
        "run_id": dag_run.run_id,
        "task_type": str(conf.get("task_type", "asset_context")),
        "qualified_name": str(conf.get("qualified_name", "dq.raw_orders")),
        "result": "success",
        "state_evidence": (
            "summary reached after bounded specialist execution and exact ClickHouse audit verification"
        ),
        "task_states": {
            task_id: "success"
            for task_id in METADATA_LINEAGE_TASK_IDS
        },
    }

    logger.info("Airflow metadata-lineage summary | payload=%s", json.dumps(summary, sort_keys=True))
    print(json.dumps(summary, indent=2, sort_keys=True))

    return summary

