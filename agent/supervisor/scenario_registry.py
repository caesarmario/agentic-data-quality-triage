####
## Control Plane Resilience Scenario Registry for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Expose dependency-free scenario allowlists for Airflow control commands."""

# --- Importing Libraries
from __future__ import annotations

import logging


# --- Getting Logger
logger = logging.getLogger(__name__)


# --- Defining Scenario Allowlists
SINGLE_HANDOFF_RESILIENCE_SCENARIOS = (
    "transient_once",
    "hard_timeout",
    "circuit_open",
    "partial_result",
    "terminal_failure",
)

FANOUT_RESILIENCE_SCENARIOS = (
    "optional_worker_failure",
    "required_worker_failure",
    "gemini_timeout_simulated",
    "gemini_rate_limit_simulated",
    "pre_call_cost_rejection",
    "invalid_worker_contract",
    "resume_completed_parallel_wave",
    "circuit_open_specialist_rejection",
    "aggregation_partial_evidence",
    "concurrent_budget_reservation",
)

CONTROL_PLANE_RESILIENCE_SCENARIOS = (
    *SINGLE_HANDOFF_RESILIENCE_SCENARIOS,
    *FANOUT_RESILIENCE_SCENARIOS,
)


# --- Defining Registry Helpers
def supported_control_plane_resilience_scenarios() -> tuple[str, ...]:
    """
    Return every scenario accepted by the DAG 99 operator boundary.

    Returns:
        Stable single-handoff and fan-out scenario tuple.
    """
    return CONTROL_PLANE_RESILIENCE_SCENARIOS


logger.info(
    "Loaded Control Plane resilience scenario registry | scenarios=%d",
    len(CONTROL_PLANE_RESILIENCE_SCENARIOS),
)
