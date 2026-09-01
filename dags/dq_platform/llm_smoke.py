####
## Airflow LLM Provider Smoke Helpers for Agentic Data Quality Triage
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
LLM_SMOKE_ROUTE_NAMES = (
    "evidence_summary",
    "cheap_summary",
    "openai_summary",
    "evidence_planning",
    "hypothesis_framing",
    "triage_reasoning",
    "low_confidence_rca",
    "catalog_qa",
)

LLM_SMOKE_TASK_IDS = (
    "t10_smoke_heuristic_baseline",
    "t20_smoke_selected_route",
)


# --- Defining Functions
def emit_llm_smoke_summary(**context: Any) -> dict[str, Any]:
    """
    Emit an audit-friendly summary after both provider smoke tasks succeed.

    Args:
        context: Airflow task context containing the current DagRun.

    Returns:
        Summary containing route, external execution mode, and upstream task state evidence.

    Raises:
        ValueError: If the callable is executed without Airflow DagRun context.
    """
    dag_run = context.get("dag_run")

    if dag_run is None:
        raise ValueError("Airflow dag_run context is required for LLM smoke summary.")

    conf    = dag_run.conf or {}
    summary = {
        "dag_id": dag_run.dag_id,
        "run_id": dag_run.run_id,
        "route_name": str(conf.get("route_name", "cheap_summary")),
        "run_external_provider": bool(conf.get("run_external_provider", False)),
        "result": "success",
        "state_evidence": "t30 reached after all_success provider smoke dependencies",
        "task_states": {task_id: "success" for task_id in LLM_SMOKE_TASK_IDS},
    }

    logger.info("Airflow LLM provider smoke summary | payload=%s", json.dumps(summary, sort_keys=True))
    print(json.dumps(summary, indent=2, sort_keys=True))

    return summary
