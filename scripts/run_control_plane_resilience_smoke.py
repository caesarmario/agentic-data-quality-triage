####
## Control Plane Resilience Smoke Runner for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Run one controlled supervisor resilience scenario from an Airflow task."""

# --- Importing Libraries
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence


# --- Resolving Project Imports
PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.supervisor.runtime import run_control_plane_supervisor
from agent.supervisor.scenario_registry import (
    supported_control_plane_resilience_scenarios,
)
from agent.supervisor.models import SupervisorRunResult
from agent.supervisor.fanout_resilience import (
    run_fanout_resilience_scenario,
    supported_fanout_resilience_scenarios,
)
from agent.supervisor.smoke import (
    SupervisorResilienceScenario,
    build_resilience_smoke_request,
    build_resilience_smoke_runtime,
    expected_smoke_status,
    supported_resilience_scenarios,
)
from pipelines.common.logging import logger


# --- Defining CLI Helpers
def build_parser() -> argparse.ArgumentParser:
    """
    Build the allowlisted resilience smoke CLI parser.

    Returns:
        Configured argparse parser.
    """
    parser = argparse.ArgumentParser(
        description="Run one controlled Control Plane Supervisor resilience scenario."
    )
    parser.add_argument(
        "--scenario",
        required=True,
        choices=supported_control_plane_resilience_scenarios(),
        help="Allowlisted failure mode executed by the administrative Airflow DAG.",
    )
    parser.add_argument(
        "--run-id",
        required=True,
        help="Airflow DagRun identifier retained as the parent audit correlation key.",
    )

    return parser


# --- Defining Validation Helpers
def validate_smoke_result(
    scenario: SupervisorResilienceScenario,
    result: SupervisorRunResult,
) -> None:
    """
    Enforce exact expected status and isolation invariants for one scenario.

    Args:
        scenario: Controlled scenario that was executed.
        result: Typed SupervisorRunResult returned by the runtime.

    Returns:
        None when every scenario invariant passes.

    Raises:
        RuntimeError: If status, retries, handoffs, or accepted results are unsafe.
    """
    expected_status = expected_smoke_status(scenario)

    if result.status != expected_status:
        raise RuntimeError(
            f"Scenario {scenario.value} expected {expected_status.value}, "
            f"received {result.status.value}."
        )

    state      = result.supervisor_state
    resilience = result.audit_summary.get("resilience", {})

    if scenario == SupervisorResilienceScenario.TRANSIENT_ONCE:
        if resilience.get("attempt_count") != 2 or resilience.get("retry_count") != 1:
            raise RuntimeError("Transient scenario did not execute exactly one bounded retry.")

        if len(state.specialist_results) != 1 or len(state.handoff_history) != 1:
            raise RuntimeError("Transient scenario lost its one terminal specialist result.")

    elif scenario == SupervisorResilienceScenario.HARD_TIMEOUT:
        if resilience.get("failure_category") != "hard_timeout":
            raise RuntimeError("Timeout scenario did not retain hard_timeout evidence.")

        if state.specialist_results or state.incident_memory_ids:
            raise RuntimeError("Timed-out output entered accepted state or durable memory.")

        if not result.failure_isolated or len(state.handoff_history) != 1:
            raise RuntimeError("Timeout scenario did not retain one isolated handoff failure.")

    elif scenario == SupervisorResilienceScenario.CIRCUIT_OPEN:
        circuit = resilience.get("circuit", {})

        if circuit.get("state") != "open" or circuit.get("request_allowed") is not False:
            raise RuntimeError("Circuit scenario did not retain an open blocking decision.")

        if state.handoff_history or state.specialist_results:
            raise RuntimeError("Open circuit allowed a specialist handoff or result.")

    elif scenario == SupervisorResilienceScenario.PARTIAL_RESULT:
        if len(state.specialist_results) != 1 or len(state.handoff_history) != 1:
            raise RuntimeError("Partial scenario did not retain one bounded partial result.")

        if not result.failure_isolated or state.specialist_results[0].status.value != "partial":
            raise RuntimeError("Partial scenario was not isolated as an explicit partial result.")

    elif scenario == SupervisorResilienceScenario.TERMINAL_FAILURE:
        if resilience.get("failure_category") != "specialist_failed":
            raise RuntimeError("Terminal failure did not retain specialist_failed evidence.")

        if state.specialist_results or state.incident_memory_ids:
            raise RuntimeError("Failed specialist output entered accepted state or durable memory.")

        if not result.failure_isolated or len(state.handoff_history) != 1:
            raise RuntimeError("Terminal failure did not retain one isolated handoff failure.")


def build_smoke_summary(
    scenario: SupervisorResilienceScenario,
    run_id: str,
    result: SupervisorRunResult,
) -> dict[str, object]:
    """
    Build compact retained-log evidence for one resilience scenario.

    Args:
        scenario: Controlled scenario executed by Airflow.
        run_id: Source Airflow DagRun identifier.
        result: Validated supervisor result.

    Returns:
        JSON-safe operational summary.
    """
    state = result.supervisor_state

    return {
        "run_id": run_id,
        "scenario": scenario.value,
        "status": result.status.value,
        "parent_run_id": str(result.parent_run_id),
        "selected_specialist": result.selected_specialist,
        "handoff_count": len(state.handoff_history),
        "accepted_result_count": len(state.specialist_results),
        "incident_memory_count": len(state.incident_memory_ids),
        "failure_isolated": result.failure_isolated,
        "resilience": result.audit_summary.get("resilience", {}),
        "budget": result.audit_summary.get("budget", {}),
        "final_response": result.final_response,
        "errors": state.errors,
    }


# --- Running The Smoke Scenario
def run_smoke_scenario(
    scenario: SupervisorResilienceScenario | str,
    run_id: str,
) -> dict[str, object]:
    """
    Execute and validate one controlled supervisor resilience scenario.

    Args:
        scenario: Allowlisted scenario name or enum.
        run_id: Airflow DagRun identifier.

    Returns:
        Compact scenario evidence printed into the Airflow task log.
    """
    resolved = SupervisorResilienceScenario(scenario)
    request  = build_resilience_smoke_request(resolved)
    runtime  = build_resilience_smoke_runtime(resolved)

    logger.info(
        "Starting Control Plane resilience smoke | run_id=%s scenario=%s",
        run_id,
        resolved.value,
    )

    result = run_control_plane_supervisor(
        request=request,
        external_run_id=run_id,
        config=runtime,
    )
    validate_smoke_result(scenario=resolved, result=result)
    summary = build_smoke_summary(
        scenario=resolved,
        run_id=run_id,
        result=result,
    )

    print(json.dumps(summary, indent=2, sort_keys=True))
    logger.info(
        "Control Plane resilience smoke passed | run_id=%s scenario=%s status=%s",
        run_id,
        resolved.value,
        result.status.value,
    )

    return summary


def main(argv: Sequence[str] | None = None) -> int:
    """
    Parse CLI arguments and run one allowlisted resilience scenario.

    Args:
        argv: Optional explicit CLI argument sequence.

    Returns:
        Zero when exact scenario invariants pass.
    """
    args = build_parser().parse_args(argv)
    if args.scenario in supported_fanout_resilience_scenarios():
        summary = run_fanout_resilience_scenario(
            scenario=args.scenario,
            run_id=args.run_id,
        )
        print(json.dumps(summary, indent=2, sort_keys=True))

    else:
        run_smoke_scenario(
            scenario=args.scenario,
            run_id=args.run_id,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
