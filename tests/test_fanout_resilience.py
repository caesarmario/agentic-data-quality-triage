####
## Bounded Fan-Out Resilience Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Validate DAG 99 fan-out scenario contracts without external provider calls."""

# --- Importing Libraries
from __future__ import annotations

import pytest

from agent.specialists.contracts import AgentTaskStatus
from agent.specialists.registry import enforce_task_capability
from agent.supervisor.execution_plan import validate_execution_plan
from agent.supervisor.fanout_resilience import (
    ControlledFanoutExecutor,
    FanoutResilienceScenario,
    build_fanout_smoke_request,
    build_scenario_plan,
    build_ten_worker_smoke_plan,
    exercise_concurrent_budget_reservations,
    expected_fanout_status,
    supported_fanout_resilience_scenarios,
)
from agent.supervisor.runtime import derive_supervisor_parent_run_id
from dags.dq_platform.control_plane_resilience import CONTROL_PLANE_RESILIENCE_SCENARIOS
from scripts.run_control_plane_resilience_smoke import build_parser as build_runner_parser
from scripts.trigger_airflow_control_plane_resilience import (
    build_parser as build_trigger_parser,
    supported_control_plane_resilience_scenarios,
)
from scripts.verify_control_plane_resilience import build_parser as build_verifier_parser


# --- Testing Scenario Registry
def test_fanout_resilience_scenarios_are_unique_and_airflow_allowlisted() -> None:
    """Every runtime scenario must be represented once in DAG 99 parameters."""
    scenarios = supported_fanout_resilience_scenarios()

    assert len(scenarios) == len(set(scenarios)) == 10
    assert set(scenarios).issubset(set(CONTROL_PLANE_RESILIENCE_SCENARIOS))


@pytest.mark.parametrize("scenario", supported_fanout_resilience_scenarios())
def test_resilience_runner_and_verifier_accept_every_fanout_scenario(
    scenario: str,
) -> None:
    """CLI dispatch must accept allowlisted fan-out scenarios without arbitrary commands."""
    runner_args = build_runner_parser().parse_args(
        ["--scenario", scenario, "--run-id", "manual__fanout_parser"]
    )
    trigger_args = build_trigger_parser().parse_args(
        ["--scenario", scenario, "--run-id", "manual__fanout_parser"]
    )
    verifier_args = build_verifier_parser().parse_args(
        ["--scenario", scenario, "--run-id", "manual__fanout_parser"]
    )

    assert runner_args.scenario == scenario
    assert trigger_args.scenario == scenario
    assert verifier_args.scenario == scenario


def test_trigger_registry_matches_airflow_scenario_allowlist() -> None:
    """Operator trigger and DAG parameter must expose the same bounded scenarios."""
    assert supported_control_plane_resilience_scenarios() == (
        CONTROL_PLANE_RESILIENCE_SCENARIOS
    )


def test_unknown_resilience_scenario_is_rejected_by_both_clis() -> None:
    """DAG 99 runners must not accept arbitrary failure-injection strings."""
    with pytest.raises(SystemExit):
        build_runner_parser().parse_args(
            ["--scenario", "run_arbitrary_shell", "--run-id", "manual__invalid"]
        )

    with pytest.raises(SystemExit):
        build_trigger_parser().parse_args(
            ["--scenario", "run_arbitrary_shell", "--run-id", "manual__invalid"]
        )

    with pytest.raises(SystemExit):
        build_verifier_parser().parse_args(
            ["--scenario", "run_arbitrary_shell", "--run-id", "manual__invalid"]
        )


# --- Testing Expected Failure Semantics
@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        (FanoutResilienceScenario.OPTIONAL_WORKER_FAILURE, AgentTaskStatus.PARTIAL),
        (FanoutResilienceScenario.REQUIRED_WORKER_FAILURE, AgentTaskStatus.BLOCKED),
        (FanoutResilienceScenario.GEMINI_TIMEOUT_SIMULATED, AgentTaskStatus.PARTIAL),
        (FanoutResilienceScenario.GEMINI_RATE_LIMIT_SIMULATED, AgentTaskStatus.PARTIAL),
        (FanoutResilienceScenario.PRE_CALL_COST_REJECTION, AgentTaskStatus.PARTIAL),
        (FanoutResilienceScenario.INVALID_WORKER_CONTRACT, AgentTaskStatus.BLOCKED),
        (FanoutResilienceScenario.RESUME_COMPLETED_WAVE, AgentTaskStatus.SUCCESS),
        (FanoutResilienceScenario.CIRCUIT_OPEN_REJECTION, AgentTaskStatus.PARTIAL),
        (FanoutResilienceScenario.AGGREGATION_PARTIAL_EVIDENCE, AgentTaskStatus.PARTIAL),
        (FanoutResilienceScenario.CONCURRENT_BUDGET_RESERVATION, AgentTaskStatus.SUCCESS),
    ],
)
def test_fanout_resilience_status_policy_is_explicit(
    scenario: FanoutResilienceScenario,
    expected: AgentTaskStatus,
) -> None:
    """Each controlled scenario must have one deterministic parent status."""
    assert expected_fanout_status(scenario) == expected


def test_invalid_worker_contract_is_rejected_before_execution() -> None:
    """An unauthorized tool must invalidate the immutable plan before spawning a worker."""
    scenario      = FanoutResilienceScenario.INVALID_WORKER_CONTRACT
    request       = build_fanout_smoke_request(scenario)
    parent_run_id = derive_supervisor_parent_run_id("manual__invalid_worker_contract")
    plan          = build_scenario_plan(scenario, request, parent_run_id)
    executor      = ControlledFanoutExecutor(scenario)

    with pytest.raises(PermissionError, match="unauthorized"):
        validate_execution_plan(plan)

    assert executor.call_count == 0


# --- Testing Capacity And Shared Budget Admission
def test_ten_worker_smoke_plan_is_deterministic_and_least_privilege() -> None:
    """Capacity proof must contain ten valid read-only workers and concurrency three."""
    scenario      = FanoutResilienceScenario.CONCURRENT_BUDGET_RESERVATION
    request       = build_fanout_smoke_request(scenario)
    parent_run_id = derive_supervisor_parent_run_id("manual__ten_worker_plan")
    first         = build_ten_worker_smoke_plan(request, parent_run_id)
    second        = build_ten_worker_smoke_plan(request, parent_run_id)

    assert first.deterministic_plan_hash == second.deterministic_plan_hash
    assert len(first.workers) == 10
    assert first.fanout_policy.max_concurrency == 3
    assert first.fanout_policy.mutation_allowed is False

    for worker in first.workers:
        capability = enforce_task_capability(worker.task)
        assert capability.mutation_allowed is False


def test_concurrent_parent_budget_reservation_retains_ten_unique_workers() -> None:
    """Thread-safe parent admission must not lose or duplicate concurrent reservations."""
    parent_run_id = derive_supervisor_parent_run_id("manual__concurrent_budget")

    assert exercise_concurrent_budget_reservations(parent_run_id) == 10
