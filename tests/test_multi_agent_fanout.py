####
## Bounded Multi-Agent Fan-Out Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Validate immutable plans, Send concurrency, failure isolation, and checkpoint reuse."""

# --- Importing Libraries
from __future__ import annotations

import threading
import time
from typing import Any
from uuid import UUID, uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from agent.checkpointing import (
    CheckpointSettings,
    build_specialist_checkpoint_namespace,
)
from agent.llm.config import resolve_route
from agent.specialists.contracts import (
    AgentModelRoute,
    AgentResultEnvelope,
    AgentTaskStatus,
    EvidenceReference,
)
from agent.specialists.metadata_lineage import build_metadata_lineage_task
from agent.supervisor.budgets import SupervisorFanoutBudgetAllocator
from agent.supervisor.execution_plan import (
    AgentAggregationStrategy,
    AgentDependency,
    AgentExecutionPlan,
    AgentFanoutPolicy,
    AgentPlanSource,
    AgentTaskRequirement,
    PlannedAgentTask,
    aggregate_agent_results,
    assign_stable_task_identity,
    build_execution_waves,
    calculate_plan_hash,
    compile_execution_plan,
    validate_execution_plan,
)
from agent.supervisor.fanout import (
    FanoutRuntimeConfig,
    execute_agent_wave,
    run_control_plane_fanout,
)
from agent.supervisor.models import (
    SupervisorExecutionMode,
    SupervisorIntent,
    SupervisorRequest,
)
from agent.supervisor.runtime import (
    SupervisorRuntimeConfig,
    derive_supervisor_parent_run_id,
)


# --- Defining Test Doubles
class AuditRecorder:
    """Capture concurrent audit events safely without a live ClickHouse table."""

    def __init__(self) -> None:
        """Initialize an empty thread-safe event list."""
        self.events: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def __call__(self, **kwargs: Any) -> UUID:
        """
        Retain one append-only audit event.

        Args:
            kwargs: Audit writer arguments.

        Returns:
            New audit UUID.
        """
        with self._lock:
            self.events.append(kwargs)

        return uuid4()


class ContextRecorder:
    """Capture run-context writes required by the parent fan-out runtime."""

    def __init__(self) -> None:
        """Initialize empty context histories."""
        self.schema_count = 0
        self.events: list[Any] = []

    def ensure(self, _client: Any) -> None:
        """Record one idempotent schema-ensure call."""
        self.schema_count += 1

    def write_event(self, client: Any, event: Any) -> UUID:
        """Retain one validated run-context event using the production keywords."""
        del client
        self.events.append(event)

        return event.context_event_id

    def write_memory(self, client: Any, record: Any) -> UUID:
        """Return an injected memory identity using the production keywords."""
        del client
        return record.memory_id


class FakeClient:
    """Represent a non-query client so circuit policy returns a closed test state."""

    def close(self) -> None:
        """Provide a no-op close method used by the fan-out runtime."""
        return None


# --- Defining Test Helpers
def fanout_request(
    max_workers: int = 2,
    max_concurrency: int = 2,
) -> SupervisorRequest:
    """
    Build one deterministic metadata fan-out request.

    Args:
        max_workers: Worker capacity requested by the test.
        max_concurrency: Parallel worker cap.

    Returns:
        Validated no-LLM SupervisorRequest.
    """
    return SupervisorRequest(
        intent=SupervisorIntent.ASSET_CONTEXT,
        qualified_name="dq.raw_orders",
        execution_mode=SupervisorExecutionMode.FANOUT,
        max_workers=max_workers,
        max_concurrency=max_concurrency,
        max_handoffs=max_workers,
        max_model_calls=0,
        token_budget=0,
        estimated_cost_budget_usd=0.0,
        allow_external_llm=False,
    )


def successful_result(worker: PlannedAgentTask) -> AgentResultEnvelope:
    """
    Build one evidence-bearing deterministic result for an authorized worker.

    Args:
        worker: Source planned worker.

    Returns:
        Successful AgentResultEnvelope with no provider usage.
    """
    task = worker.task

    return AgentResultEnvelope(
        task_id=task.task_id,
        parent_run_id=task.parent_run_id,
        specialist_name=task.specialist_name,
        task_type=task.task_type,
        status=AgentTaskStatus.SUCCESS,
        evidence_references=[
            EvidenceReference(
                evidence_type="metadata_catalog_query",
                source_tool="metadata_catalog",
                reference=f"task:{task.task_id}",
                summary=f"Deterministic evidence for {task.task_type}.",
            )
        ],
        structured_output={"summary": f"Completed {task.task_type}."},
        confidence=0.90,
        model_route=AgentModelRoute.NO_LLM_FALLBACK,
        duration_ms=10,
        recommended_next_step="Review the retained evidence.",
    )


def failed_result(worker: PlannedAgentTask, message: str = "Controlled worker failure.") -> AgentResultEnvelope:
    """
    Build one typed worker failure for aggregation tests.

    Args:
        worker: Source planned worker.
        message: Sanitized failure detail.

    Returns:
        Failed AgentResultEnvelope.
    """
    task = worker.task

    return AgentResultEnvelope(
        task_id=task.task_id,
        parent_run_id=task.parent_run_id,
        specialist_name=task.specialist_name,
        task_type=task.task_type,
        status=AgentTaskStatus.FAILED,
        model_route=AgentModelRoute.NO_LLM_FALLBACK,
        errors=[message],
        recommended_next_step="Inspect the failed worker audit.",
    )


def fake_runtime(
    worker_executor: Any,
    recorder: AuditRecorder | None = None,
    context: ContextRecorder | None = None,
) -> FanoutRuntimeConfig:
    """
    Build a no-network fan-out runtime with append-only test recorders.

    Args:
        worker_executor: Injected deterministic worker callable.
        recorder: Optional shared audit recorder.
        context: Optional shared context recorder.

    Returns:
        FanoutRuntimeConfig with checkpointing disabled.
    """
    audit   = recorder or AuditRecorder()
    context = context or ContextRecorder()
    supervisor = SupervisorRuntimeConfig(
        audit_client_factory=lambda **_: FakeClient(),
        audit_writer=audit,
        context_schema_ensurer=context.ensure,
        context_event_writer=context.write_event,
        incident_memory_writer=context.write_memory,
    )

    return FanoutRuntimeConfig(
        supervisor_config=supervisor,
        checkpoint_settings=CheckpointSettings(mode="off"),
        worker_executor=worker_executor,
    )


def build_ten_worker_plan(
    request: SupervisorRequest,
    parent_run_id: UUID,
) -> AgentExecutionPlan:
    """
    Build ten unique read-only metadata workers for capacity testing.

    Args:
        request: Parent fan-out limits.
        parent_run_id: Stable parent identity.

    Returns:
        Validated ten-worker single-wave execution plan.
    """
    workers = []

    for index in range(10):
        task = assign_stable_task_identity(
            build_metadata_lineage_task(
                parent_run_id=parent_run_id,
                task_type="asset_context",
                qualified_name=f"dq.synthetic_asset_{index}",
                requester="airflow",
            )
        )
        workers.append(
            PlannedAgentTask(
                task=task,
                intent=SupervisorIntent.ASSET_CONTEXT,
                requirement=AgentTaskRequirement.OPTIONAL,
                checkpoint_namespace=build_specialist_checkpoint_namespace(
                    parent_run_id=str(parent_run_id),
                    task_id=str(task.task_id),
                    specialist_name=task.specialist_name,
                ),
                rationale="Synthetic read-only capacity worker.",
            )
        )

    worker_tuple = tuple(workers)
    dependencies: tuple[AgentDependency, ...] = ()
    waves = build_execution_waves(
        tuple(worker.task.task_id for worker in worker_tuple),
        dependencies,
    )
    policy = AgentFanoutPolicy(
        max_workers=request.max_workers,
        max_concurrency=request.max_concurrency,
        max_model_calls=0,
        token_budget=0,
        estimated_cost_budget_usd=0.0,
        latency_budget_ms=request.latency_budget_ms,
        allow_external_llm=False,
    )
    plan_hash = calculate_plan_hash(
        parent_run_id=parent_run_id,
        workers=worker_tuple,
        dependencies=dependencies,
        fanout_policy=policy,
        waves=waves,
        aggregation_strategy=AgentAggregationStrategy.EVIDENCE_FIRST,
        plan_source=AgentPlanSource.DETERMINISTIC,
    )

    return AgentExecutionPlan(
        parent_run_id=parent_run_id,
        workers=worker_tuple,
        dependencies=dependencies,
        fanout_policy=policy,
        waves=waves,
        aggregation_strategy=AgentAggregationStrategy.EVIDENCE_FIRST,
        plan_source=AgentPlanSource.DETERMINISTIC,
        deterministic_plan_hash=plan_hash,
    )


# --- Testing Request And Plan Contracts
def test_single_mode_preserves_one_worker_defaults() -> None:
    """Existing callers must remain single-handoff and external-LLM denied by default."""
    request = SupervisorRequest(
        intent=SupervisorIntent.ASSET_CONTEXT,
        qualified_name="dq.raw_orders",
    )

    assert request.execution_mode == SupervisorExecutionMode.SINGLE
    assert request.max_workers == 1
    assert request.max_concurrency == 1
    assert request.allow_external_llm is False


def test_single_mode_rejects_hidden_fanout_capacity() -> None:
    """A caller cannot request multiple workers while labelling the run single."""
    with pytest.raises(ValueError, match="single execution mode"):
        SupervisorRequest(
            intent=SupervisorIntent.ASSET_CONTEXT,
            qualified_name="dq.raw_orders",
            max_workers=2,
        )


def test_plan_hash_and_worker_ids_are_stable_across_recompilation() -> None:
    """Same parent and request must reproduce immutable worker and plan identities."""
    request       = fanout_request()
    parent_run_id = derive_supervisor_parent_run_id("manual__fanout_stable")
    first         = compile_execution_plan(request, parent_run_id)
    second        = compile_execution_plan(request, parent_run_id)

    assert first.deterministic_plan_hash == second.deterministic_plan_hash
    assert [item.task.task_id for item in first.workers] == [
        item.task.task_id for item in second.workers
    ]
    assert len({item.checkpoint_namespace for item in first.workers}) == 2


def test_plan_validation_rejects_tampered_hash() -> None:
    """Persisted plans cannot be modified without invalidating canonical identity."""
    request       = fanout_request()
    parent_run_id = derive_supervisor_parent_run_id("manual__fanout_tamper")
    plan          = compile_execution_plan(request, parent_run_id)
    tampered      = plan.model_copy(update={"deterministic_plan_hash": "0" * 64})

    with pytest.raises(ValueError, match="hash"):
        validate_execution_plan(tampered)


def test_dependency_cycle_is_rejected_before_worker_execution() -> None:
    """Cyclic task dependencies must fail closed during deterministic planning."""
    plan  = compile_execution_plan(
        fanout_request(),
        derive_supervisor_parent_run_id("manual__fanout_cycle"),
    )
    first = plan.workers[0].task.task_id
    second = plan.workers[1].task.task_id

    with pytest.raises(ValueError, match="cycle"):
        build_execution_waves(
            (first, second),
            (
                AgentDependency(upstream_task_id=first, downstream_task_id=second),
                AgentDependency(upstream_task_id=second, downstream_task_id=first),
            ),
        )


# --- Testing Aggregation Semantics
def test_optional_worker_failure_retains_partial_sibling_evidence() -> None:
    """One optional failure must not erase valid required evidence."""
    plan    = compile_execution_plan(
        fanout_request(),
        derive_supervisor_parent_run_id("manual__fanout_optional_failure"),
    )
    results = [
        successful_result(plan.workers[0]),
        failed_result(plan.workers[1]),
    ]
    aggregation = aggregate_agent_results(plan=plan, results=results, duration_ms=50)

    assert aggregation.status == AgentTaskStatus.PARTIAL
    assert aggregation.completed_task_ids == [plan.workers[0].task.task_id]
    assert aggregation.optional_failed_task_ids == [plan.workers[1].task.task_id]
    assert aggregation.required_failed_task_ids == []
    assert len(aggregation.evidence_references) == 1


def test_required_worker_failure_blocks_high_confidence_conclusion() -> None:
    """A required failure must block synthesis even when an optional sibling succeeds."""
    plan    = compile_execution_plan(
        fanout_request(),
        derive_supervisor_parent_run_id("manual__fanout_required_failure"),
    )
    results = [
        failed_result(plan.workers[0]),
        successful_result(plan.workers[1]),
    ]
    aggregation = aggregate_agent_results(plan=plan, results=results, duration_ms=50)

    assert aggregation.status == AgentTaskStatus.BLOCKED
    assert aggregation.confidence == 0.0
    assert aggregation.required_failed_task_ids == [plan.workers[0].task.task_id]


# --- Testing Runtime Fan-Out
def test_fanout_runtime_executes_two_workers_and_audits_aggregation() -> None:
    """The opt-in parent runtime must execute and aggregate two independent tasks."""
    recorder = AuditRecorder()
    context  = ContextRecorder()
    calls: list[str] = []

    def executor(_request: SupervisorRequest, worker: PlannedAgentTask) -> AgentResultEnvelope:
        """Capture worker order and return deterministic evidence."""
        calls.append(worker.task.task_type)

        return successful_result(worker)

    result = run_control_plane_fanout(
        request=fanout_request(),
        external_run_id="manual__fanout_runtime",
        config=fake_runtime(executor, recorder=recorder, context=context),
    )

    assert result.status == AgentTaskStatus.SUCCESS
    assert result.execution_mode == SupervisorExecutionMode.FANOUT
    assert result.worker_count == 2
    assert set(calls) == {"asset_context", "blast_radius"}
    assert len(result.supervisor_state.specialist_results) == 2
    assert result.aggregation["status"] == "success"
    assert context.schema_count == 1
    assert [event.phase.value for event in context.events] == [
        "started",
        "routed",
        "completed",
    ]
    actions = [event["action"] for event in recorder.events]
    assert "supervisor_execution_plan_created" in actions
    assert actions.count("supervisor_worker_queued") == 2
    assert actions.count("supervisor_handoff_completed") == 2
    assert "supervisor_aggregation_completed" in actions
    assert actions[-1] == "supervisor_final_decision"


def test_ten_worker_wave_caps_observed_concurrency_at_three() -> None:
    """Ten worker capacity must never exceed the configured concurrency of three."""
    request       = fanout_request(max_workers=10, max_concurrency=3)
    parent_run_id = derive_supervisor_parent_run_id("manual__fanout_ten_workers")
    plan          = build_ten_worker_plan(request, parent_run_id)
    active        = 0
    peak_active   = 0
    lock          = threading.Lock()

    def executor(_request: SupervisorRequest, worker: PlannedAgentTask) -> AgentResultEnvelope:
        """Measure concurrent LangGraph Send worker execution."""
        nonlocal active, peak_active

        with lock:
            active += 1
            peak_active = max(peak_active, active)

        time.sleep(0.03)

        with lock:
            active -= 1

        return successful_result(worker)

    runtime   = fake_runtime(executor)
    allocator = SupervisorFanoutBudgetAllocator(
        max_model_calls=0,
        token_budget=0,
        estimated_cost_budget_usd=0.0,
        worker_capacity=10,
    )
    results = execute_agent_wave(
        request=request,
        plan=plan,
        wave=plan.waves[0],
        prior_results={},
        allocator=allocator,
        config=runtime,
        checkpointer=None,
    )

    assert len(results) == 10
    assert 2 <= peak_active <= 3
    assert allocator.snapshot().handoffs == 10


def test_completed_wave_checkpoint_is_reused_without_repeating_workers() -> None:
    """A repeated wave invocation must reuse completed child writes and side effects."""
    request       = fanout_request()
    parent_run_id = derive_supervisor_parent_run_id("manual__fanout_checkpoint_reuse")
    plan          = compile_execution_plan(request, parent_run_id)
    call_count    = 0

    def executor(_request: SupervisorRequest, worker: PlannedAgentTask) -> AgentResultEnvelope:
        """Count actual executions so checkpoint reuse is observable."""
        nonlocal call_count
        call_count += 1

        return successful_result(worker)

    runtime      = fake_runtime(executor)
    checkpointer = InMemorySaver()

    def invoke_wave() -> list[AgentResultEnvelope]:
        """Invoke the same immutable wave with a fresh admission allocator."""
        allocator = SupervisorFanoutBudgetAllocator(
            max_model_calls=0,
            token_budget=0,
            estimated_cost_budget_usd=0.0,
            worker_capacity=2,
        )

        return execute_agent_wave(
            request=request,
            plan=plan,
            wave=plan.waves[0],
            prior_results={},
            allocator=allocator,
            config=runtime,
            checkpointer=checkpointer,
        )

    first  = invoke_wave()
    second = invoke_wave()

    assert len(first) == len(second) == 2
    assert call_count == 2
    assert [item.task_id for item in first] == [item.task_id for item in second]


def test_request_scope_blocks_external_route_even_when_global_switch_is_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-request permission must remain narrower than the global environment switch."""
    request       = fanout_request()
    parent_run_id = derive_supervisor_parent_run_id("manual__fanout_external_permission")
    plan          = compile_execution_plan(request, parent_run_id)
    observed: list[bool] = []

    monkeypatch.setenv("EXTERNAL_LLM_ENABLED", "true")
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-test-key")
    monkeypatch.setenv(
        "GEMINI_BASE_URL",
        "https://generativelanguage.googleapis.com/v1beta/openai/",
    )
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.5-flash-lite")

    def executor(_request: SupervisorRequest, worker: PlannedAgentTask) -> AgentResultEnvelope:
        """Resolve a provider route inside the fan-out request permission scope."""
        observed.append(resolve_route("cheap_summary").use_heuristic)

        return successful_result(worker)

    result = run_control_plane_fanout(
        request=request,
        external_run_id="manual__fanout_external_permission",
        config=fake_runtime(executor),
        execution_plan=plan,
    )

    assert result.status == AgentTaskStatus.SUCCESS
    assert observed == [True, True]
