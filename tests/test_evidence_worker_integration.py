####
## Evidence Worker Supervisor Integration Tests
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Verify policy-owned evidence fan-out without provider calls or service access."""

# --- Importing Libraries
from uuid import uuid4
import json

import pytest
from pydantic import ValidationError

from agent.specialists.contracts import AgentModelRoute
from agent.specialists.registry import (
    EVIDENCE_INTERPRETATION_TASKS,
    enforce_task_capability,
)
from agent.supervisor.execution_plan import (
    AgentPlanningProposal,
    AgentTaskRequirement,
    ProposedAgentTask,
    build_task_for_proposal,
    compile_execution_plan,
)
from agent.supervisor.models import SupervisorIntent, SupervisorRequest
from agent.supervisor.routing import classify_supervisor_intent
from agent.supervisor.runtime import SupervisorRuntimeConfig, invoke_selected_specialist
from dags.dq_platform.control_plane_supervisor import CONTROL_PLANE_SUPERVISOR_INTENTS
from scripts.trigger_airflow_control_plane_supervisor import SUPPORTED_INTENTS
from scripts.verify_control_plane_execution import verify_evidence_interpretation_workers


# --- Defining Isolated Request Fixtures
def evidence_request(**overrides: object) -> SupervisorRequest:
    """Return a synthetic three-worker request with optional invalid-field overrides."""
    values = {
        "intent": "interpret_incident_evidence",
        "alert_key": "orders|dq_failure|2026-06-10|dq.raw_orders|row_count_positive|table",
        "execution_mode": "fanout",
        "max_workers": 3,
        "max_handoffs": 3,
        "max_concurrency": 3,
        "token_budget": 32_000,
        "max_model_calls": 3,
        "estimated_cost_budget_usd": 0.05,
    }
    return SupervisorRequest.model_validate({**values, **overrides})


# --- Testing Exact Permissions And Reproducible Planning
def test_evidence_plan_has_three_required_isolated_workers() -> None:
    """All evidence categories must survive compilation with fixed identities and budgets."""
    request = evidence_request()
    parent  = uuid4()
    plan    = compile_execution_plan(request, parent)
    replay  = compile_execution_plan(request, parent)

    assert plan.deterministic_plan_hash == replay.deterministic_plan_hash
    assert len(plan.workers) == 3
    assert {worker.task.task_type for worker in plan.workers} == set(EVIDENCE_INTERPRETATION_TASKS)
    assert all(worker.requirement == AgentTaskRequirement.REQUIRED for worker in plan.workers)
    assert len({worker.checkpoint_namespace for worker in plan.workers}) == 3
    assert sum(worker.task.model_call_budget for worker in plan.workers) == 3
    assert sum(worker.task.token_budget for worker in plan.workers) == 24_576
    assert sum(worker.task.estimated_cost_budget_usd for worker in plan.workers) == pytest.approx(0.048)
    assert len(plan.waves) == 1
    assert plan.fanout_policy.max_concurrency == 3
    assert plan.fanout_policy.allow_external_llm is False

    for worker in plan.workers:
        assert worker.task.model_route == AgentModelRoute.QUICKTHINK_LLM
        assert not {"s3_artifacts", "alert_lifecycle", "clickhouse_sql"}.intersection(worker.task.allowed_tools)
        enforce_task_capability(worker.task)


@pytest.mark.parametrize("override", [
    {"alert_key": ""},
    {"execution_mode": "single"},
    {"max_workers": 2},
    {"max_model_calls": 2},
    {"token_budget": 24_575},
    {"estimated_cost_budget_usd": 0.047},
    {"model_route": "deepthinkllm"},
    {"provider": "gemini"},
])
def test_invalid_evidence_request_fails_before_execution(override: dict) -> None:
    """Reject missing context, insufficient capacity, and caller-selected provider policy."""
    with pytest.raises(ValidationError):
        evidence_request(**override)


def test_model_proposal_cannot_remove_required_evidence_categories() -> None:
    """The explicit three-task investigation does not accept model-owned plan substitution."""
    proposal = AgentPlanningProposal(tasks=[ProposedAgentTask(
        specialist_name="incident_triage_agent",
        task_type="interpret_dq_history",
        rationale="Collect DQ evidence only.",
    )])
    with pytest.raises(ValueError, match="deterministic three-task"):
        compile_execution_plan(evidence_request(), uuid4(), proposal)


def test_existing_intent_cannot_propose_new_interpretation_task() -> None:
    """Adding a registry capability must not silently expand older operator intents."""
    request = SupervisorRequest(intent="triage_alert", alert_key="sample-alert")
    proposal = ProposedAgentTask(
        specialist_name="incident_triage_agent",
        task_type="interpret_dq_history",
        rationale="Try to add an interpretation without the explicit intent.",
    )
    with pytest.raises(ValueError, match="explicit interpretation intent"):
        build_task_for_proposal(request, uuid4(), proposal)


def test_evidence_worker_dispatch_uses_adapter_not_legacy_triage() -> None:
    """All three exact task types reach the injectable adapter without tool or provider IO."""
    observed = []

    def adapter(task, config):
        """Record the selected task and return a dispatch sentinel."""
        observed.append(task.task_type)
        return "adapter-result"

    def forbidden(**kwargs):
        """Fail when interpretation is incorrectly sent to a legacy specialist."""
        raise AssertionError("Legacy specialist must not execute this task.")

    config = SupervisorRuntimeConfig(
        evidence_worker_runner=adapter,
        incident_runner=forbidden,
        metadata_lineage_runner=forbidden,
    )
    for worker in compile_execution_plan(evidence_request(), uuid4()).workers:
        assert invoke_selected_specialist(worker.task, config) == "adapter-result"
        enlarged = worker.task.model_copy(update={"allowed_tools": (*worker.task.allowed_tools, "dbt_lineage")})
        with pytest.raises(PermissionError, match="exact read-only"):
            enforce_task_capability(enlarged)

    assert set(observed) == set(EVIDENCE_INTERPRETATION_TASKS)


def test_auto_routing_and_airflow_boundary_remain_explicit() -> None:
    """Automatic triage stays unchanged while both Airflow allowlists accept the new intent."""
    request = SupervisorRequest(alert_key="sample-alert")
    assert classify_supervisor_intent(request) == SupervisorIntent.TRIAGE_ALERT
    assert request.execution_mode.value == "single"
    assert "interpret_incident_evidence" in SUPPORTED_INTENTS
    assert "interpret_incident_evidence" in CONTROL_PLANE_SUPERVISOR_INTENTS


# --- Testing Strict Paid Acceptance Evidence
def audit_fixture(external: bool = True) -> tuple[list[dict], list[dict]]:
    """Return synthetic audit payloads for exactly three distinct evidence workers."""
    rows, terminals = [], []
    for task_type, specialist in EVIDENCE_INTERPRETATION_TASKS.items():
        task_id = str(uuid4())
        terminals.append({
            "task_id": task_id, "task_type": task_type, "selected_specialist": specialist,
            "result_status": "success", "evidence_reference_count": 1,
            "model_call_count": 1 if external else 0,
            "token_usage": 120 if external else 0,
            "estimated_cost_usd": 0.001 if external else 0,
        })
        if external:
            rows.append({
                "action": "llm_route_completed", "status": "success",
                "input_json": json.dumps({"task_id": task_id}),
                "output_json": json.dumps({
                    "provider": "gemini", "model": "configured-test-model",
                    "used_heuristic": False, "fallback_reason": "",
                    "structured_output_status": "validated",
                    "attempted_routes": ["cheap_summary"],
                    "input_tokens": 100, "output_tokens": 20, "estimated_cost_usd": 0.001,
                }),
            })
    return rows, terminals


@pytest.mark.parametrize("external", [False, True])
def test_acceptance_reconciles_zero_cost_or_three_actual_calls(external: bool) -> None:
    """No-LLM acceptance remains distinct from three-worker paid-provider acceptance."""
    rows, terminals = audit_fixture(external)
    result = verify_evidence_interpretation_workers(rows, terminals, external)
    assert result["strict_external_acceptance"] is external
    assert result["provider_calls"] == (3 if external else 0)


@pytest.mark.parametrize("corruption", ["fallback", "missing_call", "cost", "failed", "tokens", "duplicate"])
def test_acceptance_rejects_incomplete_or_inconsistent_proof(corruption: str) -> None:
    """Green parent status alone cannot substitute for exact worker provider evidence."""
    rows, terminals = audit_fixture()
    if corruption == "fallback":
        event = json.loads(rows[0]["output_json"])
        event["used_heuristic"] = True
        rows[0]["output_json"] = json.dumps(event)
    elif corruption == "missing_call":
        rows.pop()
    elif corruption == "cost":
        terminals[0]["estimated_cost_usd"] = 0.003
    elif corruption == "failed":
        terminals[0]["result_status"] = "failed"
    elif corruption == "tokens":
        terminals[0]["token_usage"] = 0
    else:
        rows[1]["input_json"] = rows[0]["input_json"]
    with pytest.raises(RuntimeError):
        verify_evidence_interpretation_workers(rows, terminals, True)
