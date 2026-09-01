####
## Bounded Multi-Agent Fan-Out Runtime for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Execute immutable specialist plans with LangGraph Send, isolation, and fan-in."""

# --- Importing Libraries
from __future__ import annotations

import json
import operator
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Callable, TypedDict
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from agent.checkpointing import (
    CheckpointSettings,
    build_checkpoint_config,
    build_checkpoint_thread_id,
    build_supervisor_checkpoint_namespace,
    checkpoint_exists,
    load_checkpoint_settings,
    open_checkpoint_saver,
    resume_checkpointed_graph,
)
from agent.context.models import RunContextPhase
from agent.llm.config import external_llm_permission_scope
from agent.specialists.contracts import (
    AgentApprovalState,
    AgentModelRoute,
    AgentResultEnvelope,
    AgentTaskStatus,
    HandoffRecord,
    SupervisorState,
)
from agent.specialists.registry import enforce_result_contract
from agent.supervisor.budgets import SupervisorFanoutBudgetAllocator
from agent.supervisor.execution_plan import (
    AgentAggregationResult,
    AgentExecutionPlan,
    AgentExecutionWave,
    PlannedAgentTask,
    aggregate_agent_results,
    compile_execution_plan,
    validate_execution_plan,
)
from agent.supervisor.models import (
    SupervisorExecutionMode,
    SupervisorRequest,
    SupervisorRoute,
    SupervisorRunResult,
)
from agent.supervisor.resilience import (
    circuit_snapshot_payload,
    require_circuit_allows,
)
from agent.supervisor.runtime import (
    SupervisorRuntimeConfig,
    derive_supervisor_parent_run_id,
    persist_supervisor_context_event,
    sanitize_supervisor_error,
    write_supervisor_audit,
)
from pipelines.common.logging import logger


# --- Defining Constants
WORKER_SCRIPT_PATH       = Path(__file__).resolve().parents[2] / "scripts" / "run_agent_worker.py"
WORKER_TIMEOUT_GRACE_SEC = 5


# --- Defining LangGraph State
class FanoutChildState(TypedDict, total=False):
    """Carry one planned worker and its terminal serialized result."""

    worker: dict[str, Any]
    result: dict[str, Any]


class FanoutWaveState(TypedDict, total=False):
    """Carry one execution wave and reducer-merged worker results."""

    workers: list[dict[str, Any]]
    worker: dict[str, Any]
    results: Annotated[list[dict[str, Any]], operator.add]


# --- Defining Runtime Configuration
@dataclass(frozen=True)
class FanoutRuntimeConfig:
    """
    Inject fan-out worker, audit, checkpoint, and subprocess dependencies.

    Attributes:
        supervisor_config: Existing single-handoff dependency configuration.
        checkpoint_settings: Optional persistent LangGraph checkpoint backend.
        worker_executor: Optional in-process test executor. Production defaults
            to the isolated subprocess boundary.
        subprocess_runner: Injectable subprocess function used by focused tests.
        worker_timeout_grace_seconds: Parent kill grace after task deadline.
    """

    supervisor_config: SupervisorRuntimeConfig = field(default_factory=SupervisorRuntimeConfig)
    checkpoint_settings: CheckpointSettings    = field(default_factory=load_checkpoint_settings)
    worker_executor: Callable[[SupervisorRequest, PlannedAgentTask], AgentResultEnvelope] | None = None
    subprocess_runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run
    worker_timeout_grace_seconds: int = WORKER_TIMEOUT_GRACE_SEC

    def __post_init__(self) -> None:
        """
        Validate bounded subprocess grace configuration.

        Raises:
            ValueError: If the grace window is outside the safe local range.
        """
        if not 1 <= self.worker_timeout_grace_seconds <= 30:
            raise ValueError("worker_timeout_grace_seconds must be between 1 and 30.")


# --- Defining Isolated Worker Helpers
def invoke_isolated_agent_worker(
    request: SupervisorRequest,
    worker: PlannedAgentTask,
    config: FanoutRuntimeConfig,
) -> AgentResultEnvelope:
    """
    Execute one worker through an isolated, parent-cancellable Python process.

    Args:
        request: Parent supervisor request and external-provider permission.
        worker: Fully authorized worker contract.
        config: Fan-out subprocess runtime dependencies.

    Returns:
        Validated terminal AgentResultEnvelope from the child process.

    Raises:
        TimeoutError: If the child exceeds its task deadline plus grace.
        RuntimeError: If the child fails or does not write a valid result contract.
    """
    task = worker.task

    with tempfile.TemporaryDirectory(prefix="dq-agent-worker-") as temporary_directory:
        contract_path = Path(temporary_directory) / "input.json"
        result_path   = Path(temporary_directory) / "result.json"
        contract = {
            "request": request.model_dump(mode="json"),
            "worker": worker.model_dump(mode="json"),
        }
        contract_path.write_text(
            json.dumps(contract, sort_keys=True),
            encoding="utf-8",
        )
        command = [
            sys.executable,
            str(WORKER_SCRIPT_PATH),
            "--input",
            str(contract_path),
            "--output",
            str(result_path),
        ]

        logger.info(
            "Starting isolated fan-out worker | task_id=%s specialist=%s task_type=%s timeout_seconds=%d",
            task.task_id,
            task.specialist_name,
            task.task_type,
            task.timeout_seconds,
        )

        try:
            completed = config.subprocess_runner(
                command,
                check=False,
                timeout=task.timeout_seconds + config.worker_timeout_grace_seconds,
            )

        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(
                f"Isolated worker exceeded timeout for {task.specialist_name}.{task.task_type}."
            ) from exc

        if completed.returncode != 0:
            raise RuntimeError(
                f"Isolated worker exited with code {completed.returncode} for "
                f"{task.specialist_name}.{task.task_type}."
            )

        if not result_path.is_file():
            raise RuntimeError("Isolated worker completed without a result contract.")

        result = AgentResultEnvelope.model_validate_json(
            result_path.read_text(encoding="utf-8")
        )

    validated = enforce_result_contract(task=task, result=result)
    validate_worker_usage(worker=worker, result=validated)

    return validated


def validate_worker_usage(
    worker: PlannedAgentTask,
    result: AgentResultEnvelope,
) -> None:
    """
    Reject a worker result that exceeds its immutable resource allocation.

    Args:
        worker: Source planned task.
        result: Terminal specialist result.

    Returns:
        None when actual usage remains within worker limits.

    Raises:
        PermissionError: If calls, tokens, cost, or duration exceeds policy.
    """
    task = worker.task

    if result.model_call_count > task.model_call_budget:
        raise PermissionError("Worker result exceeded its model-call budget.")

    if result.token_usage > task.token_budget:
        raise PermissionError("Worker result exceeded its token budget.")

    if result.estimated_cost_usd > task.estimated_cost_budget_usd:
        raise PermissionError("Worker result exceeded its estimated-cost budget.")

    if result.duration_ms > task.timeout_seconds * 1_000:
        raise PermissionError("Worker result exceeded its timeout budget.")


def build_failed_worker_result(
    worker: PlannedAgentTask,
    exc: BaseException,
    duration_ms: int,
) -> AgentResultEnvelope:
    """
    Convert an isolated worker exception into a typed failed result.

    Args:
        worker: Source planned worker.
        exc: Bounded invocation, circuit, budget, or checkpoint exception.
        duration_ms: Worker wall-clock duration before isolation.

    Returns:
        Failed AgentResultEnvelope safe for sibling aggregation.
    """
    return AgentResultEnvelope(
        task_id=worker.task.task_id,
        parent_run_id=worker.task.parent_run_id,
        specialist_name=worker.task.specialist_name,
        task_type=worker.task.task_type,
        status=AgentTaskStatus.FAILED,
        model_route=AgentModelRoute.NO_LLM_FALLBACK,
        duration_ms=max(0, duration_ms),
        errors=[sanitize_supervisor_error(exc)],
        recommended_next_step=(
            "Review the worker audit and dependency state before a bounded retry."
        ),
    )


# --- Defining Worker Audit Runtime
def execute_planned_worker(
    request: SupervisorRequest,
    worker: PlannedAgentTask,
    allocator: SupervisorFanoutBudgetAllocator,
    config: FanoutRuntimeConfig,
) -> AgentResultEnvelope:
    """
    Reserve budget, enforce circuit policy, and execute one isolated worker.

    Args:
        request: Parent supervisor request.
        worker: Fully authorized planned task.
        allocator: Shared thread-safe parent budget allocator.
        config: Fan-out runtime dependencies.

    Returns:
        Success, partial, failed, or blocked terminal worker result.
    """
    started           = time.monotonic()
    supervisor_config = config.supervisor_config
    task              = worker.task
    route             = SupervisorRoute(
        intent=worker.intent,
        specialist_name=task.specialist_name,
        task_type=task.task_type,
        rationale=worker.rationale or "Authorized fan-out worker task.",
    )
    client = supervisor_config.audit_client_factory(
        host=supervisor_config.clickhouse_host,
        port=supervisor_config.clickhouse_port,
    )

    try:
        allocation = allocator.reserve_worker(task)
        write_supervisor_audit(
            config=supervisor_config,
            client=client,
            parent_run_id=task.parent_run_id,
            request=request,
            action="supervisor_worker_budget_reserved",
            status="success",
            route=route,
            task=task,
            resilience_payload={
                "checkpoint_namespace": worker.checkpoint_namespace,
                "worker_budget": allocation.model_dump(mode="json"),
            },
        )
        circuit_snapshot = supervisor_config.circuit_snapshot_loader(
            client=client,
            specialist_name=task.specialist_name,
            policy=supervisor_config.circuit_policy,
        )
        write_supervisor_audit(
            config=supervisor_config,
            client=client,
            parent_run_id=task.parent_run_id,
            request=request,
            action="supervisor_circuit_checked",
            status="success" if circuit_snapshot.request_allowed else "blocked",
            route=route,
            task=task,
            resilience_payload={
                "checkpoint_namespace": worker.checkpoint_namespace,
                "circuit": circuit_snapshot_payload(circuit_snapshot),
            },
        )
        require_circuit_allows(circuit_snapshot)
        write_supervisor_audit(
            config=supervisor_config,
            client=client,
            parent_run_id=task.parent_run_id,
            request=request,
            action="supervisor_handoff_started",
            status="running",
            route=route,
            task=task,
            resilience_payload={
                "checkpoint_namespace": worker.checkpoint_namespace,
                "retry_budget": worker.retry_budget,
            },
        )

        executor = config.worker_executor

        if executor is None:
            result = invoke_isolated_agent_worker(
                request=request,
                worker=worker,
                config=config,
            )
        else:
            with external_llm_permission_scope(request.allow_external_llm):
                result = executor(request, worker)

            result = enforce_result_contract(task=task, result=result)
            validate_worker_usage(worker=worker, result=result)

        duration_ms = int((time.monotonic() - started) * 1_000)
        write_supervisor_audit(
            config=supervisor_config,
            client=client,
            parent_run_id=task.parent_run_id,
            request=request,
            action="supervisor_handoff_completed",
            status=result.status.value,
            route=route,
            task=task,
            result=result,
            resilience_payload={
                "checkpoint_namespace": worker.checkpoint_namespace,
                "retry_budget": worker.retry_budget,
            },
            duration_ms=duration_ms,
            error_message="; ".join(result.errors)[:2_000],
        )

        return result

    except Exception as exc:
        duration_ms = int((time.monotonic() - started) * 1_000)
        result      = build_failed_worker_result(
            worker=worker,
            exc=exc,
            duration_ms=duration_ms,
        )
        write_supervisor_audit(
            config=supervisor_config,
            client=client,
            parent_run_id=task.parent_run_id,
            request=request,
            action="supervisor_handoff_failed",
            status="failed",
            route=route,
            task=task,
            result=result,
            resilience_payload={
                "checkpoint_namespace": worker.checkpoint_namespace,
                "retry_budget": worker.retry_budget,
            },
            duration_ms=duration_ms,
            error_message=result.errors[0],
        )

        return result

    finally:
        close = getattr(client, "close", None)

        if callable(close):
            close()


# --- Defining Checkpointed Worker Subgraphs
def invoke_checkpointed_worker(
    request: SupervisorRequest,
    worker: PlannedAgentTask,
    allocator: SupervisorFanoutBudgetAllocator,
    config: FanoutRuntimeConfig,
    checkpointer: Any | None,
    execution_plan_hash: str,
) -> AgentResultEnvelope:
    """
    Invoke or resume one per-task child graph without repeating completed work.

    Args:
        request: Parent supervisor request.
        worker: Planned worker with immutable checkpoint namespace.
        allocator: Shared parent budget allocator.
        config: Fan-out runtime dependencies.
        checkpointer: Optional LangGraph saver.
        execution_plan_hash: Stable plan identity used for thread correlation.

    Returns:
        Terminal worker result from execution or completed checkpoint state.
    """
    def run_worker_node(_state: FanoutChildState) -> FanoutChildState:
        """Execute one worker and serialize its typed terminal result."""
        result = execute_planned_worker(
            request=request,
            worker=worker,
            allocator=allocator,
            config=config,
        )

        return {"result": result.model_dump(mode="json")}

    builder: StateGraph[FanoutChildState] = StateGraph(FanoutChildState)
    builder.add_node("execute_worker", run_worker_node)
    builder.add_edge(START, "execute_worker")
    builder.add_edge("execute_worker", END)
    graph = builder.compile(checkpointer=checkpointer)

    if checkpointer is None:
        values = graph.invoke({"worker": worker.model_dump(mode="json")})
        return AgentResultEnvelope.model_validate(values["result"])

    # Each worker graph is compiled as an independent root graph. LangGraph's
    # checkpoint_ns field is reserved for a graph embedded as a subgraph, so a
    # per-invocation root graph must isolate persistence through its thread ID.
    # The logical child namespace remains immutable in the plan and audit log.
    thread_id = build_checkpoint_thread_id(
        namespace=f"fanout-worker-{worker.checkpoint_namespace}",
        correlation_value=(
            f"{execution_plan_hash}|{worker.task.task_id}"
        ),
    )
    graph_config = build_checkpoint_config(thread_id=thread_id)

    if checkpoint_exists(checkpointer=checkpointer, config=graph_config):
        snapshot = graph.get_state(graph_config)

        if snapshot.values and not snapshot.next and snapshot.values.get("result"):
            logger.info(
                "Reused completed worker checkpoint | task_id=%s namespace=%s",
                worker.task.task_id,
                worker.checkpoint_namespace,
            )

            return AgentResultEnvelope.model_validate(snapshot.values["result"])

        values, _ = resume_checkpointed_graph(
            graph=graph,
            checkpointer=checkpointer,
            config=graph_config,
        )

    else:
        values = graph.invoke(
            {"worker": worker.model_dump(mode="json")},
            config=graph_config,
        )

    return AgentResultEnvelope.model_validate(values["result"])


# --- Defining LangGraph Send Wave Execution
def dependency_blocked_result(
    worker: PlannedAgentTask,
    dependency_results: dict[UUID, AgentResultEnvelope],
    plan: AgentExecutionPlan,
) -> AgentResultEnvelope | None:
    """
    Block a worker when an upstream dependency lacks usable terminal evidence.

    Args:
        worker: Candidate worker in the current wave.
        dependency_results: Results completed before the current wave.
        plan: Immutable dependency graph.

    Returns:
        Blocked result when an upstream dependency failed, otherwise None.
    """
    upstream_ids = {
        edge.upstream_task_id
        for edge in plan.dependencies
        if edge.downstream_task_id == worker.task.task_id
    }
    failed_upstream = [
        task_id
        for task_id in upstream_ids
        if task_id not in dependency_results
        or dependency_results[task_id].status
        not in {AgentTaskStatus.SUCCESS, AgentTaskStatus.PARTIAL}
    ]

    if not failed_upstream:
        return None

    return AgentResultEnvelope(
        task_id=worker.task.task_id,
        parent_run_id=worker.task.parent_run_id,
        specialist_name=worker.task.specialist_name,
        task_type=worker.task.task_type,
        status=AgentTaskStatus.BLOCKED,
        model_route=AgentModelRoute.NO_LLM_FALLBACK,
        errors=[
            "Worker dependency did not return usable evidence: "
            + ", ".join(str(item) for item in failed_upstream)
        ],
        recommended_next_step="Resolve the failed upstream worker before retrying this task.",
    )


def execute_agent_wave(
    request: SupervisorRequest,
    plan: AgentExecutionPlan,
    wave: AgentExecutionWave,
    prior_results: dict[UUID, AgentResultEnvelope],
    allocator: SupervisorFanoutBudgetAllocator,
    config: FanoutRuntimeConfig,
    checkpointer: Any | None,
) -> list[AgentResultEnvelope]:
    """
    Execute one dependency-ready wave through LangGraph dynamic Send workers.

    Args:
        request: Parent supervisor request.
        plan: Immutable execution plan.
        wave: Topological wave being executed.
        prior_results: Terminal results from earlier waves.
        allocator: Shared parent budget allocator.
        config: Fan-out runtime dependencies.
        checkpointer: Optional persistent LangGraph saver.

    Returns:
        Terminal results sorted into deterministic plan order.
    """
    workers_by_id = {worker.task.task_id: worker for worker in plan.workers}
    wave_workers  = [workers_by_id[task_id] for task_id in wave.task_ids]

    # Reserve every wave allocation before LangGraph starts any worker. This
    # makes admission deterministic and also reconstructs conservative parent
    # reservations when a completed checkpoint is reused after process restart.
    for worker in wave_workers:
        allocator.reserve_worker(worker.task)

    def prepare_wave(_state: FanoutWaveState) -> FanoutWaveState:
        """Anchor the dynamic Send routing step for checkpoint visibility."""
        return {}

    def dispatch_workers(state: FanoutWaveState) -> list[Send]:
        """Create one dynamic Send for each dependency-ready worker."""
        return [
            Send("execute_worker", {"worker": worker_payload})
            for worker_payload in state.get("workers", [])
        ]

    def run_worker_node(state: FanoutWaveState) -> FanoutWaveState:
        """Execute one Send-scoped child subgraph and return reducer-safe JSON."""
        worker = PlannedAgentTask.model_validate(state["worker"])
        blocked = dependency_blocked_result(
            worker=worker,
            dependency_results=prior_results,
            plan=plan,
        )
        result = blocked or invoke_checkpointed_worker(
            request=request,
            worker=worker,
            allocator=allocator,
            config=config,
            checkpointer=checkpointer,
            execution_plan_hash=plan.deterministic_plan_hash,
        )

        return {"results": [result.model_dump(mode="json")]}

    builder: StateGraph[FanoutWaveState] = StateGraph(FanoutWaveState)
    builder.add_node("prepare_wave", prepare_wave)
    builder.add_node("execute_worker", run_worker_node)
    builder.add_edge(START, "prepare_wave")
    builder.add_conditional_edges(
        "prepare_wave",
        dispatch_workers,
        ["execute_worker"],
    )
    builder.add_edge("execute_worker", END)
    graph = builder.compile(checkpointer=checkpointer)
    initial_state: FanoutWaveState = {
        "workers": [worker.model_dump(mode="json") for worker in wave_workers],
        "results": [],
    }
    graph_config: dict[str, Any] = {
        "max_concurrency": plan.fanout_policy.max_concurrency,
    }

    if checkpointer is not None:
        parent_namespace = build_supervisor_checkpoint_namespace(str(plan.parent_run_id))
        thread_id = build_checkpoint_thread_id(
            namespace=f"fanout-wave-{parent_namespace}-wave-{wave.wave_index}",
            correlation_value=(
                f"{plan.deterministic_plan_hash}|{wave.wave_index}"
            ),
        )
        graph_config.update(
            build_checkpoint_config(thread_id=thread_id)
        )

        if checkpoint_exists(checkpointer=checkpointer, config=graph_config):
            snapshot = graph.get_state(graph_config)

            if snapshot.values and not snapshot.next:
                values = dict(snapshot.values)
                logger.info(
                    "Reused completed fan-out wave checkpoint | plan_hash=%s wave=%d results=%d",
                    plan.deterministic_plan_hash,
                    wave.wave_index,
                    len(values.get("results", [])),
                )
            else:
                values, _ = resume_checkpointed_graph(
                    graph=graph,
                    checkpointer=checkpointer,
                    config=graph_config,
                )

        else:
            values = graph.invoke(initial_state, config=graph_config)

    else:
        values = graph.invoke(initial_state, config=graph_config)

    result_by_id = {
        result.task_id: result
        for result in (
            AgentResultEnvelope.model_validate(item)
            for item in values.get("results", [])
        )
    }

    return [
        result_by_id[worker.task.task_id]
        for worker in wave_workers
        if worker.task.task_id in result_by_id
    ]


# --- Defining Parent Fan-Out Runtime
def build_fanout_response(
    aggregation: AgentAggregationResult,
    plan: AgentExecutionPlan,
) -> str:
    """
    Build an operator-facing response with explicit worker and evidence counts.

    Args:
        aggregation: Typed fan-in result.
        plan: Immutable execution plan.

    Returns:
        Bounded human-readable parent response.
    """
    return (
        f"Fan-out investigation {aggregation.status.value}. {aggregation.summary} "
        f"Workers completed: {len(aggregation.completed_task_ids)}/{len(plan.workers)}. "
        f"Evidence references retained: {len(aggregation.evidence_references)}. "
        f"Plan reference: {plan.deterministic_plan_hash[:12]}."
    )[:20_000]


def run_control_plane_fanout(
    request: SupervisorRequest,
    external_run_id: str,
    config: FanoutRuntimeConfig | None = None,
    execution_plan: AgentExecutionPlan | None = None,
) -> SupervisorRunResult:
    """
    Compile and execute one opt-in bounded multi-agent fan-out request.

    Args:
        request: Validated fan-out supervisor request.
        external_run_id: Stable Airflow or operator correlation identifier.
        config: Optional fan-out dependency overrides.
        execution_plan: Optional precompiled plan used by tests or replay.

    Returns:
        SupervisorRunResult containing every worker and aggregation outcome.
    """
    if request.execution_mode != SupervisorExecutionMode.FANOUT:
        raise ValueError("run_control_plane_fanout requires execution_mode=fanout.")

    runtime           = config or FanoutRuntimeConfig()
    supervisor_config = runtime.supervisor_config
    parent_run_id     = derive_supervisor_parent_run_id(external_run_id)
    started           = time.monotonic()
    client = supervisor_config.audit_client_factory(
        host=supervisor_config.clickhouse_host,
        port=supervisor_config.clickhouse_port,
    )
    context_event_ids: list[UUID] = []

    try:
        supervisor_config.context_schema_ensurer(client)
        started_context = persist_supervisor_context_event(
            config=supervisor_config,
            client=client,
            parent_run_id=parent_run_id,
            external_run_id=external_run_id,
            request=request,
            phase=RunContextPhase.STARTED,
            status=AgentTaskStatus.RUNNING,
            decision_facts={
                "execution_mode": request.execution_mode.value,
                "max_workers": request.max_workers,
                "max_concurrency": request.max_concurrency,
                "allow_external_llm": request.allow_external_llm,
            },
        )
        context_event_ids.append(started_context.context_event_id)
        write_supervisor_audit(
            config=supervisor_config,
            client=client,
            parent_run_id=parent_run_id,
            request=request,
            action="supervisor_run_started",
            status="running",
            context_event_id=started_context.context_event_id,
            resilience_payload={"execution_mode": "fanout"},
        )

        plan = execution_plan or compile_execution_plan(
            request=request,
            parent_run_id=parent_run_id,
        )
        validate_execution_plan(plan)

        if plan.parent_run_id != parent_run_id:
            raise ValueError("Execution plan parent identity does not match external run id.")

        write_supervisor_audit(
            config=supervisor_config,
            client=client,
            parent_run_id=parent_run_id,
            request=request,
            action="supervisor_execution_plan_created",
            status="success",
            context_event_id=started_context.context_event_id,
            resilience_payload={
                "execution_mode": "fanout",
                "plan_hash": plan.deterministic_plan_hash,
                "plan_source": plan.plan_source.value,
                "worker_count": len(plan.workers),
                "wave_count": len(plan.waves),
                "max_concurrency": plan.fanout_policy.max_concurrency,
                "checkpoint_namespaces": [
                    worker.checkpoint_namespace
                    for worker in plan.workers
                ],
            },
        )
        routed_context = persist_supervisor_context_event(
            config=supervisor_config,
            client=client,
            parent_run_id=parent_run_id,
            external_run_id=external_run_id,
            request=request,
            phase=RunContextPhase.ROUTED,
            status=AgentTaskStatus.RUNNING,
            decision_facts={
                "execution_mode": "fanout",
                "plan_hash": plan.deterministic_plan_hash,
                "worker_count": len(plan.workers),
                "wave_count": len(plan.waves),
            },
        )
        context_event_ids.append(routed_context.context_event_id)

        for worker in plan.workers:
            route = SupervisorRoute(
                intent=worker.intent,
                specialist_name=worker.task.specialist_name,
                task_type=worker.task.task_type,
                rationale=worker.rationale or "Authorized fan-out worker task.",
            )
            write_supervisor_audit(
                config=supervisor_config,
                client=client,
                parent_run_id=parent_run_id,
                request=request,
                action="supervisor_worker_queued",
                status="pending",
                route=route,
                task=worker.task,
                resilience_payload={
                    "plan_hash": plan.deterministic_plan_hash,
                    "requirement": worker.requirement.value,
                    "checkpoint_namespace": worker.checkpoint_namespace,
                },
            )

        allocator = SupervisorFanoutBudgetAllocator(
            max_model_calls=plan.fanout_policy.max_model_calls,
            token_budget=plan.fanout_policy.token_budget,
            estimated_cost_budget_usd=plan.fanout_policy.estimated_cost_budget_usd,
            worker_capacity=plan.fanout_policy.max_workers,
        )
        results: list[AgentResultEnvelope] = []
        results_by_id: dict[UUID, AgentResultEnvelope] = {}

        with open_checkpoint_saver(runtime.checkpoint_settings) as checkpointer:
            for wave in plan.waves:
                write_supervisor_audit(
                    config=supervisor_config,
                    client=client,
                    parent_run_id=parent_run_id,
                    request=request,
                    action="supervisor_execution_wave_started",
                    status="running",
                    resilience_payload={
                        "plan_hash": plan.deterministic_plan_hash,
                        "wave_index": wave.wave_index,
                        "task_ids": [str(item) for item in wave.task_ids],
                    },
                )
                wave_results = execute_agent_wave(
                    request=request,
                    plan=plan,
                    wave=wave,
                    prior_results=results_by_id,
                    allocator=allocator,
                    config=runtime,
                    checkpointer=checkpointer,
                )
                results.extend(wave_results)
                results_by_id.update({item.task_id: item for item in wave_results})
                write_supervisor_audit(
                    config=supervisor_config,
                    client=client,
                    parent_run_id=parent_run_id,
                    request=request,
                    action="supervisor_execution_wave_completed",
                    status="success",
                    resilience_payload={
                        "plan_hash": plan.deterministic_plan_hash,
                        "wave_index": wave.wave_index,
                        "result_count": len(wave_results),
                        "result_statuses": [item.status.value for item in wave_results],
                    },
                )

        duration_ms = int((time.monotonic() - started) * 1_000)
        aggregation = aggregate_agent_results(
            plan=plan,
            results=results,
            duration_ms=duration_ms,
        )

        if aggregation.model_call_count > plan.fanout_policy.max_model_calls:
            raise PermissionError("Fan-out actual model calls exceeded the parent budget.")

        if aggregation.token_usage > plan.fanout_policy.token_budget:
            raise PermissionError("Fan-out actual token usage exceeded the parent budget.")

        if aggregation.estimated_cost_usd > plan.fanout_policy.estimated_cost_budget_usd:
            raise PermissionError("Fan-out actual estimated cost exceeded the parent budget.")

        approval_state = (
            AgentApprovalState.PENDING
            if any(result.requires_human_approval for result in results)
            else AgentApprovalState.NOT_REQUIRED
        )
        final_response = build_fanout_response(aggregation=aggregation, plan=plan)
        completed_context = persist_supervisor_context_event(
            config=supervisor_config,
            client=client,
            parent_run_id=parent_run_id,
            external_run_id=external_run_id,
            request=request,
            phase=(
                RunContextPhase.COMPLETED
                if aggregation.status != AgentTaskStatus.BLOCKED
                else RunContextPhase.BLOCKED
            ),
            status=aggregation.status,
            approval_state=approval_state,
            decision_facts={
                "execution_mode": "fanout",
                "plan_hash": plan.deterministic_plan_hash,
                "aggregation": aggregation.model_dump(mode="json"),
            },
        )
        context_event_ids.append(completed_context.context_event_id)

        worker_by_id = {worker.task.task_id: worker for worker in plan.workers}
        handoff_history = [
            HandoffRecord(
                task_id=result.task_id,
                specialist_name=result.specialist_name,
                task_type=result.task_type,
                status=result.status,
                completed_at=datetime.now(timezone.utc),
                duration_ms=result.duration_ms,
                retry_count=worker_by_id[result.task_id].retry_budget,
                error_message="; ".join(result.errors)[:2_000],
            )
            for result in results
        ]
        state = SupervisorState(
            parent_run_id=parent_run_id,
            active_task=None,
            handoff_history=handoff_history,
            specialist_results=results,
            run_context_event_ids=context_event_ids,
            approval_state=approval_state,
            max_handoffs=request.max_handoffs,
            max_retries=request.max_retries,
            max_model_calls=request.max_model_calls,
            token_budget=request.token_budget,
            estimated_cost_budget_usd=request.estimated_cost_budget_usd,
            latency_budget_ms=request.latency_budget_ms,
            errors=aggregation.missing_evidence,
            final_response=final_response,
        )
        allocation = allocator.snapshot()
        write_supervisor_audit(
            config=supervisor_config,
            client=client,
            parent_run_id=parent_run_id,
            request=request,
            action="supervisor_aggregation_completed",
            status=aggregation.status.value,
            context_event_id=completed_context.context_event_id,
            approval_state=approval_state,
            resilience_payload={
                "plan_hash": plan.deterministic_plan_hash,
                "worker_count": len(plan.workers),
                "completed_count": len(aggregation.completed_task_ids),
                "optional_failure_count": len(aggregation.optional_failed_task_ids),
                "required_failure_count": len(aggregation.required_failed_task_ids),
                "allocation": allocation.model_dump(mode="json"),
            },
            duration_ms=duration_ms,
        )
        write_supervisor_audit(
            config=supervisor_config,
            client=client,
            parent_run_id=parent_run_id,
            request=request,
            action="supervisor_final_decision",
            status=aggregation.status.value,
            context_event_id=completed_context.context_event_id,
            approval_state=approval_state,
            resilience_payload={
                "execution_mode": "fanout",
                "plan_hash": plan.deterministic_plan_hash,
                "worker_count": len(plan.workers),
                "wave_count": len(plan.waves),
                "model_call_count": aggregation.model_call_count,
                "token_usage": aggregation.token_usage,
                "estimated_cost_usd": aggregation.estimated_cost_usd,
            },
            duration_ms=duration_ms,
            error_message="; ".join(aggregation.missing_evidence)[:2_000],
        )

        logger.info(
            "Bounded fan-out completed | parent_run_id=%s plan_hash=%s status=%s workers=%d duration_ms=%d",
            parent_run_id,
            plan.deterministic_plan_hash,
            aggregation.status.value,
            len(plan.workers),
            duration_ms,
        )

        return SupervisorRunResult(
            status=aggregation.status,
            parent_run_id=parent_run_id,
            requested_intent=request.intent,
            resolved_intent=plan.workers[0].intent,
            selected_specialist=",".join(
                dict.fromkeys(worker.task.specialist_name for worker in plan.workers)
            ),
            task_type=",".join(worker.task.task_type for worker in plan.workers),
            task_id=plan.workers[0].task.task_id,
            final_response=final_response,
            supervisor_state=state,
            failure_isolated=bool(
                aggregation.optional_failed_task_ids
                or aggregation.required_failed_task_ids
                or aggregation.missing_evidence
            ),
            audit_summary={
                "supervisor_decisions": 5 + (2 * len(plan.waves)),
                "specialist_handoffs": len(plan.workers),
                "parent_run_id": str(parent_run_id),
                "run_context_event_count": len(context_event_ids),
                "incident_memory_count": 0,
                "execution_mode": "fanout",
                "plan_hash": plan.deterministic_plan_hash,
                "plan_source": plan.plan_source.value,
                "worker_count": len(plan.workers),
                "wave_count": len(plan.waves),
                "max_concurrency": plan.fanout_policy.max_concurrency,
                "allocation": allocation.model_dump(mode="json"),
            },
            execution_mode=SupervisorExecutionMode.FANOUT,
            execution_plan_hash=plan.deterministic_plan_hash,
            worker_count=len(plan.workers),
            aggregation=aggregation.model_dump(mode="json"),
        )

    except Exception as exc:
        duration_ms   = int((time.monotonic() - started) * 1_000)
        error_message = sanitize_supervisor_error(exc)
        state = SupervisorState(
            parent_run_id=parent_run_id,
            run_context_event_ids=context_event_ids,
            approval_state=AgentApprovalState.NOT_REQUIRED,
            max_handoffs=request.max_handoffs,
            max_retries=request.max_retries,
            max_model_calls=request.max_model_calls,
            token_budget=request.token_budget,
            estimated_cost_budget_usd=request.estimated_cost_budget_usd,
            latency_budget_ms=request.latency_budget_ms,
            errors=[error_message],
            final_response=(
                "The bounded fan-out request was blocked before a safe aggregate result was available. "
                "No remediation was executed. Review the retained supervisor audit before retrying."
            ),
        )

        try:
            write_supervisor_audit(
                config=supervisor_config,
                client=client,
                parent_run_id=parent_run_id,
                request=request,
                action="supervisor_final_decision",
                status="blocked",
                resilience_payload={"execution_mode": "fanout"},
                duration_ms=duration_ms,
                error_message=error_message,
            )
        except Exception:
            logger.exception(
                "Failed to persist blocked fan-out audit | parent_run_id=%s",
                parent_run_id,
            )

        return SupervisorRunResult(
            status=AgentTaskStatus.BLOCKED,
            parent_run_id=parent_run_id,
            requested_intent=request.intent,
            final_response=state.final_response,
            supervisor_state=state,
            failure_isolated=True,
            audit_summary={
                "execution_mode": "fanout",
                "parent_run_id": str(parent_run_id),
                "failure_stage": "fanout_runtime",
            },
            execution_mode=SupervisorExecutionMode.FANOUT,
            worker_count=0,
        )

    finally:
        close = getattr(client, "close", None)

        if callable(close):
            close()
