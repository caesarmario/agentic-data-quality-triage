####
## Bounded Fan-Out Resilience Scenarios for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Exercise fan-out failure semantics without calling an external model provider."""

# --- Importing Libraries
from __future__ import annotations

import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Callable
from uuid import NAMESPACE_URL, UUID, uuid5

from agent.checkpointing import CheckpointSettings, build_specialist_checkpoint_namespace
from agent.specialists.contracts import (
    AgentModelRoute,
    AgentResultEnvelope,
    AgentTaskEnvelope,
    AgentTaskStatus,
    EvidenceReference,
)
from agent.specialists.metadata_lineage import build_metadata_lineage_task
from agent.supervisor.budgets import (
    SupervisorFanoutBudgetAllocator,
    SupervisorLlmBudgetExceeded,
)
from agent.supervisor.execution_plan import (
    AgentAggregationStrategy,
    AgentDependency,
    AgentExecutionPlan,
    AgentFanoutPolicy,
    AgentPlanSource,
    AgentTaskRequirement,
    PlannedAgentTask,
    build_execution_waves,
    calculate_plan_hash,
    compile_execution_plan,
)
from agent.supervisor.fanout import FanoutRuntimeConfig, run_control_plane_fanout
from agent.supervisor.models import (
    SupervisorExecutionMode,
    SupervisorIntent,
    SupervisorRequest,
    SupervisorRunResult,
)
from agent.supervisor.resilience import (
    CircuitBreakerSnapshot,
    SupervisorCircuitState,
)
from agent.supervisor.runtime import SupervisorRuntimeConfig, derive_supervisor_parent_run_id
from agent.supervisor.scenario_registry import FANOUT_RESILIENCE_SCENARIOS
from agent.tools.audit_log import write_agent_audit_event
from pipelines.common.clickhouse import build_clickhouse_client
from pipelines.common.logging import logger


# --- Defining Constants
FANOUT_RESILIENCE_SUMMARY_ACTION = "supervisor_fanout_resilience_summary"
FANOUT_RESILIENCE_TOOL_NAME      = "control_plane_supervisor"
DEFAULT_FANOUT_CHECKPOINT_ROOT   = Path("/var/lib/agent-checkpoints/fanout-resilience")


# --- Defining Scenarios
class FanoutResilienceScenario(str, Enum):
    """Represent one allowlisted bounded fan-out failure or recovery scenario."""

    OPTIONAL_WORKER_FAILURE       = "optional_worker_failure"
    REQUIRED_WORKER_FAILURE       = "required_worker_failure"
    GEMINI_TIMEOUT_SIMULATED      = "gemini_timeout_simulated"
    GEMINI_RATE_LIMIT_SIMULATED   = "gemini_rate_limit_simulated"
    PRE_CALL_COST_REJECTION       = "pre_call_cost_rejection"
    INVALID_WORKER_CONTRACT       = "invalid_worker_contract"
    RESUME_COMPLETED_WAVE         = "resume_completed_parallel_wave"
    CIRCUIT_OPEN_REJECTION        = "circuit_open_specialist_rejection"
    AGGREGATION_PARTIAL_EVIDENCE  = "aggregation_partial_evidence"
    CONCURRENT_BUDGET_RESERVATION = "concurrent_budget_reservation"


def supported_fanout_resilience_scenarios() -> tuple[str, ...]:
    """
    Return every allowlisted fan-out resilience scenario.

    Returns:
        Stable scenario values accepted by Airflow and the CLI.
    """
    return FANOUT_RESILIENCE_SCENARIOS


# --- Defining Controlled Worker Runtime
@dataclass
class ControlledFanoutExecutor:
    """
    Execute synthetic read-only workers and retain concurrency evidence.

    Attributes:
        scenario: Failure or recovery behavior injected by the administrative smoke.
        call_count: Number of worker invocations that crossed the execution boundary.
        peak_concurrency: Maximum simultaneous controlled workers observed.
    """

    scenario: FanoutResilienceScenario
    call_count: int       = 0
    peak_concurrency: int = 0
    _active: int          = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __call__(
        self,
        _request: SupervisorRequest,
        worker: PlannedAgentTask,
    ) -> AgentResultEnvelope:
        """
        Return deterministic evidence or one controlled worker failure.

        Args:
            _request: Parent request retained only for the production executor contract.
            worker: Fully authorized planned worker.

        Returns:
            Successful deterministic worker result when no failure is injected.

        Raises:
            TimeoutError: For the simulated Gemini timeout scenario.
            RuntimeError: For controlled worker and rate-limit failures.
            SupervisorLlmBudgetExceeded: Before a simulated provider call is admitted.
        """
        with self._lock:
            self.call_count += 1
            self._active += 1
            self.peak_concurrency = max(self.peak_concurrency, self._active)

        try:
            # Small sleep provides observable overlap without calling a provider or warehouse tool.
            if self.scenario == FanoutResilienceScenario.CONCURRENT_BUDGET_RESERVATION:
                time.sleep(0.05)

            is_optional = worker.requirement == AgentTaskRequirement.OPTIONAL
            is_required = worker.requirement == AgentTaskRequirement.REQUIRED

            if (
                self.scenario
                in {
                    FanoutResilienceScenario.OPTIONAL_WORKER_FAILURE,
                    FanoutResilienceScenario.AGGREGATION_PARTIAL_EVIDENCE,
                }
                and is_optional
            ):
                raise RuntimeError("Controlled optional worker failure for fan-out acceptance.")

            if (
                self.scenario == FanoutResilienceScenario.REQUIRED_WORKER_FAILURE
                and is_required
            ):
                raise RuntimeError("Controlled required worker failure for fan-out acceptance.")

            if (
                self.scenario == FanoutResilienceScenario.GEMINI_TIMEOUT_SIMULATED
                and is_optional
            ):
                raise TimeoutError(
                    "Simulated Gemini-compatible provider timeout; no external request was made."
                )

            if (
                self.scenario == FanoutResilienceScenario.GEMINI_RATE_LIMIT_SIMULATED
                and is_optional
            ):
                raise RuntimeError(
                    "Simulated Gemini-compatible provider rate limit; no external request was made."
                )

            if (
                self.scenario == FanoutResilienceScenario.PRE_CALL_COST_REJECTION
                and is_optional
            ):
                # The rejection occurs before any network call. The synthetic ledger is
                # deliberately exhausted to prove fail-closed admission behavior.
                raise SupervisorLlmBudgetExceeded("estimated_cost_usd_budget_exceeded")

            return build_successful_smoke_result(worker)

        finally:
            with self._lock:
                self._active -= 1


# --- Defining Scenario Helpers
def build_successful_smoke_result(worker: PlannedAgentTask) -> AgentResultEnvelope:
    """
    Build one evidence-bearing deterministic worker result.

    Args:
        worker: Source planned worker and its least-privilege tools.

    Returns:
        Successful no-LLM AgentResultEnvelope.
    """
    task = worker.task
    evidence_tool = next(
        (tool for tool in task.allowed_tools if tool != "agent_audit_log"),
        task.allowed_tools[0],
    )

    return AgentResultEnvelope(
        task_id=task.task_id,
        parent_run_id=task.parent_run_id,
        specialist_name=task.specialist_name,
        task_type=task.task_type,
        status=AgentTaskStatus.SUCCESS,
        evidence_references=[
            EvidenceReference(
                evidence_type="fanout_resilience_smoke",
                source_tool=evidence_tool,
                reference=f"fanout-smoke:{task.task_id}",
                summary=f"Synthetic read-only evidence for {task.task_type}.",
            )
        ],
        structured_output={
            "summary": f"Controlled worker completed {task.task_type} without mutation."
        },
        confidence=0.90,
        model_route=AgentModelRoute.NO_LLM_FALLBACK,
        duration_ms=10,
        recommended_next_step="Review the retained resilience audit evidence.",
    )


def closed_circuit_snapshot(
    client: Any,
    specialist_name: str,
    policy: Any,
) -> CircuitBreakerSnapshot:
    """
    Return a deterministic closed circuit for isolated smoke scenarios.

    Args:
        client: Unused ClickHouse client required by the loader contract.
        specialist_name: Registered worker specialist.
        policy: Circuit policy containing the configured failure threshold.

    Returns:
        Closed and request-allowed circuit snapshot.
    """
    del client

    return CircuitBreakerSnapshot(
        specialist_name=specialist_name,
        state=SupervisorCircuitState.CLOSED,
        request_allowed=True,
        failure_threshold=policy.failure_threshold,
        reason="Controlled resilience smoke circuit is closed.",
    )


def metadata_circuit_open_snapshot(
    client: Any,
    specialist_name: str,
    policy: Any,
) -> CircuitBreakerSnapshot:
    """
    Block only the metadata specialist while allowing its SQL-review sibling.

    Args:
        client: Unused ClickHouse client required by the loader contract.
        specialist_name: Registered worker specialist.
        policy: Circuit policy containing the configured failure threshold.

    Returns:
        Open metadata circuit or closed snapshot for other specialists.
    """
    if specialist_name == "metadata_lineage_agent":
        return CircuitBreakerSnapshot(
            specialist_name=specialist_name,
            state=SupervisorCircuitState.OPEN,
            request_allowed=False,
            consecutive_failures=policy.failure_threshold,
            failure_threshold=policy.failure_threshold,
            retry_after_seconds=300,
            reason="Controlled open-circuit rejection for one optional specialist.",
        )

    return closed_circuit_snapshot(client, specialist_name, policy)


def build_fanout_smoke_request(
    scenario: FanoutResilienceScenario,
) -> SupervisorRequest:
    """
    Build a no-provider fan-out request suitable for one resilience scenario.

    Args:
        scenario: Allowlisted controlled scenario.

    Returns:
        Validated SupervisorRequest with bounded worker and cost policy.
    """
    if scenario == FanoutResilienceScenario.CIRCUIT_OPEN_REJECTION:
        return SupervisorRequest(
            intent=SupervisorIntent.REVIEW_SQL,
            qualified_name="dq.raw_orders",
            sql_proposal=(
                "SELECT order_id FROM dq.raw_orders "
                "WHERE dt = toDate('2026-08-31') LIMIT 10"
            ),
            sql_purpose="Controlled read-only SQL review resilience smoke.",
            execution_mode=SupervisorExecutionMode.FANOUT,
            max_workers=2,
            max_concurrency=2,
            max_handoffs=2,
            max_model_calls=0,
            token_budget=0,
            estimated_cost_budget_usd=0.0,
            allow_external_llm=False,
        )

    worker_capacity = (
        10
        if scenario == FanoutResilienceScenario.CONCURRENT_BUDGET_RESERVATION
        else 2
    )

    return SupervisorRequest(
        intent=SupervisorIntent.ASSET_CONTEXT,
        qualified_name="dq.raw_orders",
        execution_mode=SupervisorExecutionMode.FANOUT,
        max_workers=worker_capacity,
        max_concurrency=min(3, worker_capacity),
        max_handoffs=worker_capacity,
        max_model_calls=0,
        token_budget=0,
        estimated_cost_budget_usd=0.0,
        allow_external_llm=False,
    )


def rebuild_plan(
    plan: AgentExecutionPlan,
    workers: tuple[PlannedAgentTask, ...],
) -> AgentExecutionPlan:
    """
    Recalculate canonical plan identity after controlled contract changes.

    Args:
        plan: Source execution plan.
        workers: Replacement worker tuple.

    Returns:
        Rehashed AgentExecutionPlan preserving topology and policy.
    """
    plan_hash = calculate_plan_hash(
        parent_run_id=plan.parent_run_id,
        workers=workers,
        dependencies=plan.dependencies,
        fanout_policy=plan.fanout_policy,
        waves=plan.waves,
        aggregation_strategy=plan.aggregation_strategy,
        plan_source=plan.plan_source,
    )

    return plan.model_copy(
        update={
            "workers": workers,
            "deterministic_plan_hash": plan_hash,
        }
    )


def build_ten_worker_smoke_plan(
    request: SupervisorRequest,
    parent_run_id: UUID,
) -> AgentExecutionPlan:
    """
    Build ten unique metadata workers for scheduler-capacity acceptance.

    Args:
        request: Parent request containing ten-worker and concurrency limits.
        parent_run_id: Stable Airflow-derived parent identity.

    Returns:
        Validated-shape deterministic single-wave execution plan.
    """
    workers: list[PlannedAgentTask] = []

    for index in range(10):
        task = build_metadata_lineage_task(
            parent_run_id=parent_run_id,
            task_type="asset_context",
            qualified_name=f"dq.synthetic_asset_{index}",
            requester="airflow_resilience_smoke",
        )
        # The source builder creates random task IDs. A deterministic parent-scoped
        # identity keeps checkpoint and audit correlation stable across replay.
        stable_task_id = uuid5(
            NAMESPACE_URL,
            f"agentic-dq|{parent_run_id}|capacity-worker|{index}",
        )
        task = task.model_copy(update={"task_id": stable_task_id})
        workers.append(
            PlannedAgentTask(
                task=task,
                intent=SupervisorIntent.ASSET_CONTEXT,
                requirement=(
                    AgentTaskRequirement.REQUIRED
                    if index == 0
                    else AgentTaskRequirement.OPTIONAL
                ),
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
        max_workers=10,
        max_concurrency=3,
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


def build_scenario_plan(
    scenario: FanoutResilienceScenario,
    request: SupervisorRequest,
    parent_run_id: UUID,
) -> AgentExecutionPlan:
    """
    Build one deterministic scenario-specific execution plan.

    Args:
        scenario: Controlled failure or recovery scenario.
        request: Parent fan-out request.
        parent_run_id: Stable Airflow-derived parent UUID.

    Returns:
        Plan used by the fan-out runtime. Invalid-contract scenarios intentionally
        return a plan that runtime validation must reject.
    """
    if scenario == FanoutResilienceScenario.CONCURRENT_BUDGET_RESERVATION:
        return build_ten_worker_smoke_plan(request=request, parent_run_id=parent_run_id)

    plan = compile_execution_plan(request=request, parent_run_id=parent_run_id)

    if scenario == FanoutResilienceScenario.INVALID_WORKER_CONTRACT:
        first        = plan.workers[0]
        invalid_task = first.task.model_copy(
            update={"allowed_tools": tuple(first.task.allowed_tools) + ("filesystem_write",)}
        )
        invalid_worker = first.model_copy(update={"task": invalid_task})

        return rebuild_plan(plan, (invalid_worker, *plan.workers[1:]))

    return plan


def expected_fanout_status(scenario: FanoutResilienceScenario) -> AgentTaskStatus:
    """
    Return the exact parent status expected from one scenario.

    Args:
        scenario: Controlled fan-out scenario.

    Returns:
        Required parent AgentTaskStatus.
    """
    if scenario in {
        FanoutResilienceScenario.REQUIRED_WORKER_FAILURE,
        FanoutResilienceScenario.INVALID_WORKER_CONTRACT,
    }:
        return AgentTaskStatus.BLOCKED

    if scenario in {
        FanoutResilienceScenario.OPTIONAL_WORKER_FAILURE,
        FanoutResilienceScenario.GEMINI_TIMEOUT_SIMULATED,
        FanoutResilienceScenario.GEMINI_RATE_LIMIT_SIMULATED,
        FanoutResilienceScenario.PRE_CALL_COST_REJECTION,
        FanoutResilienceScenario.CIRCUIT_OPEN_REJECTION,
        FanoutResilienceScenario.AGGREGATION_PARTIAL_EVIDENCE,
    }:
        return AgentTaskStatus.PARTIAL

    return AgentTaskStatus.SUCCESS


def validate_fanout_smoke_result(
    scenario: FanoutResilienceScenario,
    result: SupervisorRunResult,
    executor: ControlledFanoutExecutor,
    replay_executor_calls: int | None = None,
    concurrent_reservation_count: int = 0,
) -> dict[str, Any]:
    """
    Enforce scenario status, worker count, no-provider usage, and replay invariants.

    Args:
        scenario: Controlled scenario that ran.
        result: Parent supervisor result.
        executor: Controlled worker execution evidence.
        replay_executor_calls: Optional call count after a repeated checkpoint run.
        concurrent_reservation_count: Number of successful shared budget reservations.

    Returns:
        Compact JSON-safe acceptance summary.

    Raises:
        RuntimeError: If any scenario invariant is violated.
    """
    expected = expected_fanout_status(scenario)

    if result.status != expected:
        raise RuntimeError(
            f"Fan-out scenario {scenario.value} expected {expected.value}, "
            f"received {result.status.value}."
        )

    aggregation = result.aggregation or {}
    model_calls = int(aggregation.get("model_call_count", 0))
    token_usage = int(aggregation.get("token_usage", 0))
    cost_usd    = float(aggregation.get("estimated_cost_usd", 0.0))

    if model_calls or token_usage or cost_usd:
        raise RuntimeError("Fan-out resilience smoke unexpectedly consumed provider budget.")

    if scenario == FanoutResilienceScenario.CONCURRENT_BUDGET_RESERVATION:
        if result.worker_count != 10 or executor.call_count != 10:
            raise RuntimeError("Ten-worker capacity scenario did not execute exactly ten workers.")

        if not 1 <= executor.peak_concurrency <= 3:
            raise RuntimeError("Observed worker concurrency exceeded the default cap of three.")

        if concurrent_reservation_count != 10:
            raise RuntimeError("Shared parent budget did not retain ten unique reservations.")

    if scenario == FanoutResilienceScenario.RESUME_COMPLETED_WAVE:
        if replay_executor_calls != executor.call_count:
            raise RuntimeError("Completed checkpoint replay repeated a worker side effect.")

        if executor.call_count != result.worker_count:
            raise RuntimeError("Initial checkpoint run did not execute every planned worker once.")

    if scenario == FanoutResilienceScenario.INVALID_WORKER_CONTRACT:
        if result.worker_count != 0 or executor.call_count != 0:
            raise RuntimeError("Invalid worker contract crossed the execution boundary.")

    if scenario in {
        FanoutResilienceScenario.OPTIONAL_WORKER_FAILURE,
        FanoutResilienceScenario.GEMINI_TIMEOUT_SIMULATED,
        FanoutResilienceScenario.GEMINI_RATE_LIMIT_SIMULATED,
        FanoutResilienceScenario.PRE_CALL_COST_REJECTION,
        FanoutResilienceScenario.CIRCUIT_OPEN_REJECTION,
        FanoutResilienceScenario.AGGREGATION_PARTIAL_EVIDENCE,
    }:
        if not aggregation.get("optional_failed_task_ids"):
            raise RuntimeError("Partial scenario did not retain an optional failed worker.")

        if not aggregation.get("completed_task_ids"):
            raise RuntimeError("Partial scenario erased valid sibling evidence.")

    if scenario == FanoutResilienceScenario.REQUIRED_WORKER_FAILURE:
        if not aggregation.get("required_failed_task_ids"):
            raise RuntimeError("Required worker failure did not block high-confidence synthesis.")

    return {
        "scenario": scenario.value,
        "status": result.status.value,
        "parent_run_id": str(result.parent_run_id),
        "plan_hash": result.execution_plan_hash,
        "worker_count": result.worker_count,
        "executor_call_count": executor.call_count,
        "replay_executor_calls": replay_executor_calls,
        "peak_concurrency": executor.peak_concurrency,
        "concurrent_reservation_count": concurrent_reservation_count,
        "completed_count": len(aggregation.get("completed_task_ids", [])),
        "optional_failure_count": len(aggregation.get("optional_failed_task_ids", [])),
        "required_failure_count": len(aggregation.get("required_failed_task_ids", [])),
        "model_call_count": model_calls,
        "token_usage": token_usage,
        "estimated_cost_usd": cost_usd,
        "external_request_count": 0,
        "provider_failure_simulated": scenario in {
            FanoutResilienceScenario.GEMINI_TIMEOUT_SIMULATED,
            FanoutResilienceScenario.GEMINI_RATE_LIMIT_SIMULATED,
        },
    }


def exercise_concurrent_budget_reservations(parent_run_id: UUID) -> int:
    """
    Atomically reserve ten synthetic model budgets without making provider calls.

    Args:
        parent_run_id: Parent identity used to create unique reservation tasks.

    Returns:
        Number of unique shared-parent allocations retained.
    """
    allocator = SupervisorFanoutBudgetAllocator(
        max_model_calls=10,
        token_budget=10_000,
        estimated_cost_budget_usd=0.10,
        worker_capacity=10,
    )
    tasks = [
        AgentTaskEnvelope(
            parent_run_id=parent_run_id,
            specialist_name="metadata_lineage_agent",
            task_type="asset_context",
            allowed_tools=(
                "metadata_catalog",
                "dbt_lineage",
                "dbt_blast_radius",
                "agent_audit_log",
            ),
            model_route=AgentModelRoute.QUICKTHINK_LLM,
            model_call_budget=1,
            token_budget=1_000,
            estimated_cost_budget_usd=0.01,
            input_payload={"qualified_name": f"dq.synthetic_asset_{index}"},
        )
        for index in range(10)
    ]

    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(allocator.reserve_worker, tasks))

    return allocator.snapshot().handoffs


def persist_fanout_resilience_summary(
    summary: dict[str, Any],
    run_id: str,
) -> None:
    """
    Persist one replay-safe scenario summary for Airflow verification.

    Args:
        summary: Validated scenario acceptance evidence.
        run_id: Source Airflow DagRun identifier.

    Returns:
        None.
    """
    client = build_clickhouse_client()
    idempotency_key = hashlib.sha256(
        f"fanout-resilience|{run_id}|{summary['scenario']}".encode("utf-8")
    ).hexdigest()

    try:
        write_agent_audit_event(
            client=client,
            action=FANOUT_RESILIENCE_SUMMARY_ACTION,
            status="success",
            agent_run_id=summary["parent_run_id"],
            actor="airflow",
            tool_name=FANOUT_RESILIENCE_TOOL_NAME,
            input_payload={
                "run_id": run_id,
                "scenario": summary["scenario"],
            },
            output_payload=summary,
            idempotency_key=idempotency_key,
        )

    finally:
        close = getattr(client, "close", None)

        if callable(close):
            close()


def run_fanout_resilience_scenario(
    scenario: FanoutResilienceScenario | str,
    run_id: str,
    summary_writer: Callable[[dict[str, Any], str], None] = persist_fanout_resilience_summary,
) -> dict[str, Any]:
    """
    Execute one bounded fan-out scenario and persist its exact acceptance evidence.

    Args:
        scenario: Allowlisted scenario name or enum.
        run_id: Stable Airflow DagRun correlation identifier.
        summary_writer: Injectable ClickHouse summary writer for focused tests.

    Returns:
        Validated scenario summary.
    """
    resolved      = FanoutResilienceScenario(scenario)
    request       = build_fanout_smoke_request(resolved)
    parent_run_id = derive_supervisor_parent_run_id(run_id)
    plan          = build_scenario_plan(resolved, request, parent_run_id)
    executor      = ControlledFanoutExecutor(resolved)
    circuit_loader = (
        metadata_circuit_open_snapshot
        if resolved == FanoutResilienceScenario.CIRCUIT_OPEN_REJECTION
        else closed_circuit_snapshot
    )
    supervisor_config = replace(
        SupervisorRuntimeConfig(),
        circuit_snapshot_loader=circuit_loader,
    )
    checkpoint_settings = CheckpointSettings(mode="off")

    if resolved == FanoutResilienceScenario.RESUME_COMPLETED_WAVE:
        checkpoint_digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:24]
        checkpoint_path   = DEFAULT_FANOUT_CHECKPOINT_ROOT / f"{checkpoint_digest}.sqlite3"
        checkpoint_settings = CheckpointSettings(
            mode="sqlite",
            sqlite_path=str(checkpoint_path),
            busy_timeout_ms=10_000,
        )

    runtime = FanoutRuntimeConfig(
        supervisor_config=supervisor_config,
        checkpoint_settings=checkpoint_settings,
        worker_executor=executor,
    )

    logger.info(
        "Starting bounded fan-out resilience smoke | run_id=%s scenario=%s workers=%d",
        run_id,
        resolved.value,
        len(plan.workers),
    )
    result = run_control_plane_fanout(
        request=request,
        external_run_id=run_id,
        config=runtime,
        execution_plan=plan,
    )
    replay_executor_calls: int | None = None

    if resolved == FanoutResilienceScenario.RESUME_COMPLETED_WAVE:
        first_executor_calls = executor.call_count
        replay_result = run_control_plane_fanout(
            request=request,
            external_run_id=run_id,
            config=runtime,
            execution_plan=plan,
        )
        replay_executor_calls = executor.call_count

        if replay_result.status != result.status:
            raise RuntimeError("Checkpoint replay changed the parent terminal status.")

        if first_executor_calls != replay_executor_calls:
            raise RuntimeError("Checkpoint replay repeated a completed worker execution.")

    reservation_count = (
        exercise_concurrent_budget_reservations(parent_run_id)
        if resolved == FanoutResilienceScenario.CONCURRENT_BUDGET_RESERVATION
        else 0
    )
    summary = validate_fanout_smoke_result(
        scenario=resolved,
        result=result,
        executor=executor,
        replay_executor_calls=replay_executor_calls,
        concurrent_reservation_count=reservation_count,
    )
    summary_writer(summary, run_id)

    logger.info(
        "Bounded fan-out resilience smoke passed | run_id=%s scenario=%s status=%s workers=%d peak_concurrency=%d",
        run_id,
        resolved.value,
        result.status.value,
        result.worker_count,
        executor.peak_concurrency,
    )

    return summary
