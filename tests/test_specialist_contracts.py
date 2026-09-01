####
## Specialist Handoff Contract Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Validate typed handoff serialization, identity, evidence, permission, and budget policy."""

# --- Importing Libraries
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agent.specialists.contracts import (
    AgentModelRoute,
    AgentResultEnvelope,
    AgentRiskTier,
    AgentTaskEnvelope,
    AgentTaskStatus,
    ContextReference,
    ContextReferenceType,
    EvidenceReference,
    SupervisorState,
)
from agent.specialists.registry import (
    AGENT_CAPABILITY_REGISTRY,
    INCIDENT_TRIAGE_SPECIALIST_NAME,
    METADATA_LINEAGE_SPECIALIST_NAME,
    enforce_result_contract,
    enforce_task_capability,
    get_agent_capability,
    required_tools_for_task,
)
from pipelines.common.logging import logger


# --- Defining Test Data
REGISTERED_TASK_CASES = tuple(
    (specialist_name, task_type)
    for specialist_name, capability in sorted(AGENT_CAPABILITY_REGISTRY.items())
    for task_type in capability.accepted_task_types
)


# --- Defining Test Helpers
def build_registered_task(
    specialist_name: str = METADATA_LINEAGE_SPECIALIST_NAME,
    task_type: str = "asset_context",
    timeout_seconds: int = 30,
) -> AgentTaskEnvelope:
    """
    Build one valid task from the capability registry.

    Args:
        specialist_name: Registered specialist capability name.
        task_type: Task type accepted by the selected specialist.
        timeout_seconds: Bounded handoff timeout.

    Returns:
        Policy-compatible AgentTaskEnvelope with explicit context references.
    """
    capability = get_agent_capability(specialist_name)
    llm_routed = capability.default_model_route != AgentModelRoute.NO_LLM_FALLBACK

    task = AgentTaskEnvelope(
        parent_run_id=uuid4(),
        specialist_name=specialist_name,
        task_type=task_type,
        risk_tier=AgentRiskTier.LOW,
        allowed_tools=required_tools_for_task(specialist_name, task_type),
        context_references=[
            ContextReference(
                reference_type=ContextReferenceType.METADATA_ASSET,
                reference="dq.raw_orders",
                description="Explicit warehouse asset selected by the operator.",
            )
        ],
        model_route=capability.default_model_route,
        model_call_budget=2 if llm_routed else 0,
        token_budget=2_048 if llm_routed else 0,
        estimated_cost_budget_usd=0.02 if llm_routed else 0.0,
        timeout_seconds=timeout_seconds,
        requester="airflow_validation",
        input_payload={"qualified_name": "dq.raw_orders"},
        created_at=datetime.now(timezone.utc),
    )

    logger.info(
        "Built registered contract test task | specialist=%s task_type=%s route=%s",
        task.specialist_name,
        task.task_type,
        task.model_route.value,
    )

    return task


def build_success_result(
    task: AgentTaskEnvelope,
    *,
    source_tool: str | None = None,
    model_route: AgentModelRoute | None = None,
    duration_ms: int = 25,
    estimated_cost_usd: float | None = None,
) -> AgentResultEnvelope:
    """
    Build one successful result correlated to a source task.

    Args:
        task: Authorized source task.
        source_tool: Optional evidence tool override used by negative tests.
        model_route: Optional actual route override.
        duration_ms: Reported specialist duration.
        estimated_cost_usd: Optional actual model-cost override.

    Returns:
        Valid terminal AgentResultEnvelope.
    """
    actual_route = model_route or task.model_route
    llm_used     = actual_route != AgentModelRoute.NO_LLM_FALLBACK

    return AgentResultEnvelope(
        task_id=task.task_id,
        parent_run_id=task.parent_run_id,
        specialist_name=task.specialist_name,
        task_type=task.task_type,
        status=AgentTaskStatus.SUCCESS,
        evidence_references=[
            EvidenceReference(
                evidence_type="contract_test_evidence",
                source_tool=source_tool or task.allowed_tools[0],
                reference=f"task:{task.task_id}",
                summary="Deterministic evidence retained for contract validation.",
            )
        ],
        structured_output={"summary": "The bounded specialist task completed."},
        confidence=0.90,
        model_route=actual_route,
        model_call_count=1 if llm_used else 0,
        token_usage=256 if llm_used else 0,
        estimated_cost_usd=(
            estimated_cost_usd
            if estimated_cost_usd is not None
            else (0.002 if llm_used else 0.0)
        ),
        duration_ms=duration_ms,
        recommended_next_step="Review the retained evidence before any approved action.",
    )


def rebuild_result(
    result: AgentResultEnvelope,
    **updates: Any,
) -> AgentResultEnvelope:
    """
    Revalidate a result after applying explicit test-only field changes.

    Args:
        result: Valid baseline specialist result.
        **updates: Result fields to replace before validation.

    Returns:
        Revalidated AgentResultEnvelope.
    """
    payload = result.model_dump(mode="python")
    payload.update(updates)

    return AgentResultEnvelope.model_validate(payload)


# --- Testing Serialization Contracts
def test_task_envelope_round_trip_preserves_typed_contract() -> None:
    """JSON and Python round trips must retain task identity, enums, tools, and context."""
    task            = build_registered_task()
    python_reloaded = AgentTaskEnvelope.model_validate(task.model_dump(mode="python"))
    json_reloaded   = AgentTaskEnvelope.model_validate_json(task.model_dump_json())

    assert python_reloaded == task
    assert json_reloaded == task
    assert isinstance(json_reloaded.parent_run_id, type(task.parent_run_id))
    assert isinstance(json_reloaded.allowed_tools, tuple)
    assert json_reloaded.context_references[0].reference_type == ContextReferenceType.METADATA_ASSET


def test_result_envelope_round_trip_preserves_evidence_and_telemetry() -> None:
    """Result round trips must retain evidence, confidence, cost, latency, and route data."""
    task            = build_registered_task(INCIDENT_TRIAGE_SPECIALIST_NAME, "triage_alert")
    result          = build_success_result(task)
    python_reloaded = AgentResultEnvelope.model_validate(result.model_dump(mode="python"))
    json_reloaded   = AgentResultEnvelope.model_validate_json(result.model_dump_json())

    assert python_reloaded == result
    assert json_reloaded == result
    assert json_reloaded.evidence_references[0].source_tool == task.allowed_tools[0]
    assert json_reloaded.confidence == 0.90
    assert json_reloaded.model_call_count == 1
    assert json_reloaded.token_usage == 256
    assert json_reloaded.estimated_cost_usd == pytest.approx(0.002)
    assert json_reloaded.duration_ms == 25


# --- Testing Result Field Semantics
@pytest.mark.parametrize("status", [AgentTaskStatus.PENDING, AgentTaskStatus.RUNNING])
def test_result_envelope_rejects_non_terminal_status(status: AgentTaskStatus) -> None:
    """In-flight lifecycle states belong in HandoffRecord, not a result envelope."""
    task = build_registered_task()

    with pytest.raises(ValidationError, match="terminal specialist status"):
        AgentResultEnvelope(
            task_id=task.task_id,
            parent_run_id=task.parent_run_id,
            specialist_name=task.specialist_name,
            task_type=task.task_type,
            status=status,
        )


def test_partial_result_requires_a_retained_error() -> None:
    """A partial outcome must explain which part of the handoff did not complete."""
    task = build_registered_task()

    with pytest.raises(ValidationError, match="Partial specialist results require"):
        AgentResultEnvelope(
            task_id=task.task_id,
            parent_run_id=task.parent_run_id,
            specialist_name=task.specialist_name,
            task_type=task.task_type,
            status=AgentTaskStatus.PARTIAL,
        )


@pytest.mark.parametrize("status", [AgentTaskStatus.FAILED, AgentTaskStatus.BLOCKED])
def test_failed_or_blocked_result_requires_zero_confidence(status: AgentTaskStatus) -> None:
    """A failed or policy-blocked handoff cannot advertise root-cause confidence."""
    task = build_registered_task()

    with pytest.raises(ValidationError, match="must report zero confidence"):
        AgentResultEnvelope(
            task_id=task.task_id,
            parent_run_id=task.parent_run_id,
            specialist_name=task.specialist_name,
            task_type=task.task_type,
            status=status,
            confidence=0.50,
            errors=["The deterministic specialist did not complete."],
        )


@pytest.mark.parametrize(
    ("errors", "expected_message"),
    [
        (["   "], "cannot be blank"),
        (["first line\nsecond line"], "single-line"),
        (["x" * 2_001], "cannot exceed 2000"),
        (["duplicate", "duplicate"], "cannot contain duplicates"),
    ],
)
def test_result_errors_are_bounded_and_operator_safe(
    errors: list[str],
    expected_message: str,
) -> None:
    """Result errors must remain concise enough for audit and operator surfaces."""
    task = build_registered_task()

    with pytest.raises(ValidationError, match=expected_message):
        AgentResultEnvelope(
            task_id=task.task_id,
            parent_run_id=task.parent_run_id,
            specialist_name=task.specialist_name,
            task_type=task.task_type,
            status=AgentTaskStatus.FAILED,
            errors=errors,
        )


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("confidence", 1.01),
        ("estimated_cost_usd", -0.001),
        ("duration_ms", -1),
    ],
)
def test_result_numeric_fields_reject_invalid_boundaries(
    field_name: str,
    invalid_value: float,
) -> None:
    """Confidence, cost, and latency telemetry must reject impossible values."""
    task    = build_registered_task()
    payload = build_success_result(task).model_dump(mode="python")
    payload[field_name] = invalid_value

    with pytest.raises(ValidationError):
        AgentResultEnvelope.model_validate(payload)


# --- Testing Cross-Envelope Contracts
@pytest.mark.parametrize(
    ("field_name", "invalid_value_factory"),
    [
        ("task_id", uuid4),
        ("parent_run_id", uuid4),
        ("specialist_name", lambda: "different_specialist"),
        ("task_type", lambda: "different_task"),
    ],
)
def test_result_contract_rejects_identity_mismatch(
    field_name: str,
    invalid_value_factory: Any,
) -> None:
    """Every specialist result must correlate exactly to its source handoff identity."""
    task   = build_registered_task()
    result = build_success_result(task)
    result = rebuild_result(result, **{field_name: invalid_value_factory()})

    with pytest.raises(PermissionError, match=field_name):
        enforce_result_contract(task=task, result=result)


def test_result_contract_requires_capability_evidence_for_success() -> None:
    """Enabled specialists cannot return a successful claim without evidence."""
    task   = build_registered_task()
    result = rebuild_result(build_success_result(task), evidence_references=[])

    with pytest.raises(PermissionError, match="must retain deterministic evidence"):
        enforce_result_contract(task=task, result=result)


def test_result_contract_rejects_evidence_from_ungranted_tool() -> None:
    """Evidence references cannot widen the task's least-privilege tool boundary."""
    task   = build_registered_task()
    result = build_success_result(task, source_tool="ungranted_tool")

    with pytest.raises(PermissionError, match="unauthorized tools"):
        enforce_result_contract(task=task, result=result)


def test_result_contract_allows_only_routes_within_authorized_capability() -> None:
    """An LLM-routed task may use a weaker route but cannot escalate beyond its ceiling."""
    llm_task       = build_registered_task(INCIDENT_TRIAGE_SPECIALIST_NAME, "triage_alert")
    fallback_result = build_success_result(
        llm_task,
        source_tool="s3_artifacts",
        model_route=AgentModelRoute.NO_LLM_FALLBACK,
        estimated_cost_usd=0.0,
    )

    validated = enforce_result_contract(task=llm_task, result=fallback_result)

    assert validated.model_route == AgentModelRoute.NO_LLM_FALLBACK

    quickthink_result = build_success_result(
        llm_task,
        source_tool="s3_artifacts",
        model_route=AgentModelRoute.QUICKTHINK_LLM,
    )

    assert enforce_result_contract(
        task=llm_task,
        result=quickthink_result,
    ).model_route == AgentModelRoute.QUICKTHINK_LLM

    deterministic_task = build_registered_task()
    escalated_result    = build_success_result(
        deterministic_task,
        model_route=AgentModelRoute.QUICKTHINK_LLM,
    )

    with pytest.raises(PermissionError, match="exceeds its authorized task capability"):
        enforce_result_contract(task=deterministic_task, result=escalated_result)


def test_result_contract_rejects_duration_beyond_source_timeout() -> None:
    """Reported specialist latency must fit inside the source handoff deadline."""
    task   = build_registered_task(timeout_seconds=1)
    result = build_success_result(task, duration_ms=1_001)

    with pytest.raises(PermissionError, match="duration exceeds"):
        enforce_result_contract(task=task, result=result)


# --- Testing Registry Permission Matrix
@pytest.mark.parametrize(("specialist_name", "task_type"), REGISTERED_TASK_CASES)
def test_registry_enforces_exact_task_permission_matrix(
    specialist_name: str,
    task_type: str,
) -> None:
    """Every registered task must have an exact least-privilege tool policy."""
    capability    = get_agent_capability(specialist_name)
    required_tools = required_tools_for_task(specialist_name, task_type)
    task          = build_registered_task(specialist_name, task_type)

    assert task_type in capability.accepted_task_types
    assert set(required_tools).issubset(capability.allowed_tools)
    assert enforce_task_capability(task) == capability

    missing_tool_task = task.model_copy(update={"allowed_tools": required_tools[1:]})

    with pytest.raises(PermissionError, match="omitted required"):
        enforce_task_capability(missing_tool_task)

    unauthorized_task = task.model_copy(
        update={"allowed_tools": (*required_tools, "ungranted_tool")}
    )

    with pytest.raises(PermissionError, match="unauthorized"):
        enforce_task_capability(unauthorized_task)

    alternate_route = (
        AgentModelRoute.QUICKTHINK_LLM
        if capability.default_model_route == AgentModelRoute.NO_LLM_FALLBACK
        else AgentModelRoute.NO_LLM_FALLBACK
    )
    wrong_route_task = task.model_copy(update={"model_route": alternate_route})

    with pytest.raises(PermissionError, match="Model route"):
        enforce_task_capability(wrong_route_task)

    excessive_risk_task = task.model_copy(update={"risk_tier": AgentRiskTier.CRITICAL})

    with pytest.raises(PermissionError, match="Risk tier"):
        enforce_task_capability(excessive_risk_task)


# --- Testing Aggregate Cost And Latency Budgets
@pytest.mark.parametrize(
    "state_updates",
    [
        {"estimated_cost_budget_usd": 0.001},
        {"latency_budget_ms": 1_000},
    ],
)
def test_supervisor_state_rejects_result_cost_or_latency_overrun(
    state_updates: dict[str, Any],
) -> None:
    """Serialized child telemetry cannot exceed parent cost or latency budgets."""
    task   = build_registered_task(INCIDENT_TRIAGE_SPECIALIST_NAME, "triage_alert")
    result = build_success_result(
        task,
        duration_ms=1_001,
        estimated_cost_usd=0.002,
    )
    state_payload = {
        "parent_run_id": task.parent_run_id,
        "specialist_results": [result],
        "max_model_calls": 2,
        "token_budget": 2_048,
        "estimated_cost_budget_usd": 0.05,
        "latency_budget_ms": 120_000,
    }
    state_payload.update(state_updates)

    with pytest.raises(ValidationError, match="budget exceeded"):
        SupervisorState.model_validate(state_payload)
