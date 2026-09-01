####
## Bounded Multi-Agent Execution Plans for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Compile model proposals or deterministic policy into immutable worker plans."""

# --- Importing Libraries
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent.checkpointing import build_specialist_checkpoint_namespace
from agent.specialists.contracts import (
    AgentResultEnvelope,
    AgentTaskEnvelope,
    AgentTaskStatus,
    EvidenceReference,
)
from agent.specialists.incident_triage import build_incident_triage_task
from agent.specialists.metadata_lineage import build_metadata_lineage_task
from agent.specialists.registry import (
    INCIDENT_TRIAGE_SPECIALIST_NAME,
    METADATA_LINEAGE_SPECIALIST_NAME,
    SCHEMA_DRIFT_SPECIALIST_NAME,
    SQL_REVIEW_SPECIALIST_NAME,
    enforce_task_capability,
)
from agent.specialists.schema_drift import build_schema_drift_task
from agent.specialists.sql_review import build_sql_review_task
from agent.supervisor.models import (
    SupervisorExecutionMode,
    SupervisorIntent,
    SupervisorRequest,
)
from agent.supervisor.routing import resolve_supervisor_route
from pipelines.common.logging import logger


# --- Defining Constants
MAX_AGENT_WORKERS       = 10
DEFAULT_MAX_CONCURRENCY = 3
MAX_PLAN_DEPENDENCIES   = 45
PLAN_HASH_LENGTH        = 64


# --- Defining Enumerations
class AgentTaskRequirement(str, Enum):
    """Classify whether one worker is mandatory for parent synthesis."""

    REQUIRED = "required"
    OPTIONAL = "optional"


class AgentAggregationStrategy(str, Enum):
    """Define the bounded fan-in policy used after worker waves complete."""

    EVIDENCE_FIRST = "evidence_first"


class AgentPlanSource(str, Enum):
    """Record whether policy or a validated LLM proposal initiated planning."""

    DETERMINISTIC = "deterministic"
    LLM_PROPOSAL  = "llm_proposal"


# --- Defining Planning Contracts
class ProposedAgentTask(BaseModel):
    """
    Carry a narrow task suggestion that cannot assign tools, providers, or budgets.

    Attributes:
        specialist_name: Allowlisted registry specialist suggested by a model.
        task_type: Allowlisted capability suggested for the specialist.
        requirement: Whether the proposal considers this evidence mandatory.
        rationale: Short operator-readable reason for proposing the task.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    specialist_name: str                  = Field(min_length=3, max_length=80)
    task_type: str                        = Field(min_length=3, max_length=80)
    requirement: AgentTaskRequirement     = AgentTaskRequirement.OPTIONAL
    rationale: str                        = Field(default="", max_length=500)

    @field_validator("specialist_name", "task_type", "rationale")
    @classmethod
    def normalize_proposal_text(cls, value: str) -> str:
        """
        Normalize model-proposed text before deterministic validation.

        Args:
            value: Raw model proposal value.

        Returns:
            Lowercase identifiers or trimmed rationale text.
        """
        return value.strip()


class AgentPlanningProposal(BaseModel):
    """
    Hold one structured LLM planning suggestion before policy compilation.

    Attributes:
        tasks: Bounded task suggestions without executable permissions.
    """

    model_config = ConfigDict(extra="forbid")

    tasks: list[ProposedAgentTask] = Field(min_length=1, max_length=MAX_AGENT_WORKERS)


class AgentDependency(BaseModel):
    """
    Define one directed dependency between immutable worker task identifiers.

    Attributes:
        upstream_task_id: Task that must complete first.
        downstream_task_id: Task unblocked by the upstream result.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    upstream_task_id: UUID
    downstream_task_id: UUID

    @model_validator(mode="after")
    def reject_self_dependency(self) -> "AgentDependency":
        """
        Reject a task that depends on itself.

        Returns:
            Current dependency when its endpoints differ.
        """
        if self.upstream_task_id == self.downstream_task_id:
            raise ValueError("Agent task cannot depend on itself.")

        return self


class PlannedAgentTask(BaseModel):
    """
    Attach execution policy to one fully authorized specialist envelope.

    Attributes:
        task: Immutable specialist input with exact tools and model capability.
        intent: Supervisor intent represented by the worker.
        requirement: Required or optional aggregation behavior.
        retry_budget: Maximum retry attempts allocated to this worker.
        checkpoint_namespace: Per-invocation child checkpoint namespace.
        rationale: Bounded explanation for worker inclusion.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    task: AgentTaskEnvelope
    intent: SupervisorIntent
    requirement: AgentTaskRequirement = AgentTaskRequirement.OPTIONAL
    retry_budget: int                 = Field(default=0, ge=0, le=2)
    checkpoint_namespace: str         = Field(min_length=1, max_length=200)
    rationale: str                    = Field(default="", max_length=500)


class AgentFanoutPolicy(BaseModel):
    """
    Store parent-owned fan-out limits that workers cannot widen.

    Attributes:
        max_workers: Maximum worker capacity for one plan.
        max_concurrency: Maximum workers scheduled in parallel.
        max_model_calls: Aggregate external provider-call ceiling.
        token_budget: Aggregate provider token ceiling.
        estimated_cost_budget_usd: Aggregate estimated provider cost ceiling.
        latency_budget_ms: Parent wall-clock latency ceiling.
        allow_external_llm: Whether policy-selected external routes may execute.
        mutation_allowed: Always false for parallel worker waves.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_workers: int                    = Field(default=MAX_AGENT_WORKERS, ge=2, le=MAX_AGENT_WORKERS)
    max_concurrency: int                = Field(default=DEFAULT_MAX_CONCURRENCY, ge=1, le=3)
    max_model_calls: int                = Field(default=3, ge=0, le=10)
    token_budget: int                   = Field(default=32_000, ge=0, le=64_000)
    estimated_cost_budget_usd: float    = Field(default=0.05, ge=0.0, le=0.15)
    latency_budget_ms: int              = Field(default=300_000, ge=1_000, le=900_000)
    allow_external_llm: bool            = False
    mutation_allowed: bool              = False

    @model_validator(mode="after")
    def validate_concurrency_policy(self) -> "AgentFanoutPolicy":
        """
        Keep runtime concurrency inside worker capacity and deny mutation.

        Returns:
            Current immutable policy when safe.
        """
        if self.max_concurrency > self.max_workers:
            raise ValueError("Fan-out concurrency cannot exceed worker capacity.")

        if self.mutation_allowed:
            raise ValueError("Parallel fan-out workers cannot receive mutation permission.")

        return self


class AgentExecutionWave(BaseModel):
    """
    Group dependency-ready workers that may execute concurrently.

    Attributes:
        wave_index: Zero-based topological wave number.
        task_ids: Immutable task identifiers scheduled in this wave.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    wave_index: int           = Field(ge=0, le=MAX_AGENT_WORKERS - 1)
    task_ids: tuple[UUID, ...] = Field(min_length=1, max_length=MAX_AGENT_WORKERS)

    @field_validator("task_ids")
    @classmethod
    def reject_duplicate_wave_tasks(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        """
        Reject duplicate worker identities inside one execution wave.

        Args:
            value: Worker task identifiers in deterministic order.

        Returns:
            Original task tuple when unique.
        """
        if len(set(value)) != len(value):
            raise ValueError("Execution wave contains duplicate task identifiers.")

        return value


class AgentExecutionPlan(BaseModel):
    """
    Represent one immutable, deterministic, and auditable multi-agent plan.

    Attributes:
        parent_run_id: Stable parent supervisor correlation UUID.
        workers: Fully authorized worker tasks.
        dependencies: Directed acyclic dependency edges.
        fanout_policy: Parent-owned worker and resource limits.
        waves: Deterministic topological execution waves.
        aggregation_strategy: Fan-in policy used after the final wave.
        plan_source: Deterministic policy or validated LLM proposal.
        deterministic_plan_hash: Canonical SHA-256 plan identity.
        created_at: UTC plan creation timestamp.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    parent_run_id: UUID
    workers: tuple[PlannedAgentTask, ...]      = Field(min_length=2, max_length=MAX_AGENT_WORKERS)
    dependencies: tuple[AgentDependency, ...] = Field(default=(), max_length=MAX_PLAN_DEPENDENCIES)
    fanout_policy: AgentFanoutPolicy
    waves: tuple[AgentExecutionWave, ...]      = Field(min_length=1, max_length=MAX_AGENT_WORKERS)
    aggregation_strategy: AgentAggregationStrategy = AgentAggregationStrategy.EVIDENCE_FIRST
    plan_source: AgentPlanSource              = AgentPlanSource.DETERMINISTIC
    deterministic_plan_hash: str              = Field(min_length=PLAN_HASH_LENGTH, max_length=PLAN_HASH_LENGTH)
    created_at: datetime                      = Field(default_factory=lambda: datetime.now(timezone.utc))


class AgentAggregationResult(BaseModel):
    """
    Return typed fan-in evidence without hiding missing or failed workers.

    Attributes:
        status: Success, partial, or blocked parent outcome.
        completed_task_ids: Workers that returned successful evidence.
        optional_failed_task_ids: Optional failures retained without blocking synthesis.
        required_failed_task_ids: Required failures that block high-confidence conclusions.
        evidence_references: De-duplicated deterministic evidence references.
        confidence: Conservative aggregate confidence.
        summary: Operator-facing synthesis with explicit evidence gaps.
        missing_evidence: Bounded worker failure descriptions.
        model_call_count: Aggregate external provider attempts.
        token_usage: Aggregate model tokens.
        estimated_cost_usd: Aggregate estimated provider cost.
        duration_ms: Parent fan-out wall-clock duration.
    """

    model_config = ConfigDict(extra="forbid")

    status: AgentTaskStatus
    completed_task_ids: list[UUID]        = Field(default_factory=list, max_length=MAX_AGENT_WORKERS)
    optional_failed_task_ids: list[UUID]  = Field(default_factory=list, max_length=MAX_AGENT_WORKERS)
    required_failed_task_ids: list[UUID]  = Field(default_factory=list, max_length=MAX_AGENT_WORKERS)
    evidence_references: list[EvidenceReference] = Field(default_factory=list, max_length=250)
    confidence: float                     = Field(default=0.0, ge=0.0, le=1.0)
    summary: str                          = Field(default="", max_length=20_000)
    missing_evidence: list[str]           = Field(default_factory=list, max_length=20)
    model_call_count: int                 = Field(default=0, ge=0, le=10)
    token_usage: int                      = Field(default=0, ge=0, le=64_000)
    estimated_cost_usd: float             = Field(default=0.0, ge=0.0, le=0.15)
    duration_ms: int                      = Field(default=0, ge=0, le=900_000)


# --- Defining Canonical Identity Helpers
def canonical_json(value: Any) -> str:
    """
    Serialize JSON-compatible values deterministically for identity hashing.

    Args:
        value: JSON-compatible contract payload.

    Returns:
        Stable compact JSON text.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def task_signature(task: AgentTaskEnvelope) -> str:
    """
    Build a semantic task signature that excludes random creation metadata.

    Args:
        task: Fully authorized specialist task.

    Returns:
        SHA-256 signature for duplicate detection and stable task IDs.
    """
    payload = {
        "specialist_name": task.specialist_name,
        "task_type": task.task_type,
        "input_payload": task.input_payload,
        "context_references": [
            item.model_dump(mode="json")
            for item in task.context_references
        ],
    }

    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def assign_stable_task_identity(task: AgentTaskEnvelope) -> AgentTaskEnvelope:
    """
    Replace a builder-generated UUID with a deterministic parent-scoped task UUID.

    Args:
        task: Authorized task returned by an existing specialist builder.

    Returns:
        Revalidated task with stable identity.
    """
    stable_task_id = uuid5(task.parent_run_id, task_signature(task))

    return AgentTaskEnvelope.model_validate(
        {
            **task.model_dump(mode="python"),
            "task_id": stable_task_id,
        }
    )


def plan_hash_payload(
    parent_run_id: UUID,
    workers: tuple[PlannedAgentTask, ...],
    dependencies: tuple[AgentDependency, ...],
    fanout_policy: AgentFanoutPolicy,
    waves: tuple[AgentExecutionWave, ...],
    aggregation_strategy: AgentAggregationStrategy,
    plan_source: AgentPlanSource,
) -> dict[str, Any]:
    """
    Build the immutable plan subset included in its deterministic hash.

    Args:
        parent_run_id: Parent supervisor UUID.
        workers: Authorized worker contracts.
        dependencies: Directed task dependencies.
        fanout_policy: Parent resource policy.
        waves: Topological execution waves.
        aggregation_strategy: Selected fan-in strategy.
        plan_source: Deterministic or LLM-proposed source.

    Returns:
        JSON-safe canonical plan payload without timestamps.
    """
    worker_payloads = []

    for worker in workers:
        worker_payload = worker.model_dump(mode="json")
        worker_payload["task"].pop("created_at", None)
        worker_payloads.append(worker_payload)

    return {
        "parent_run_id": str(parent_run_id),
        "workers": worker_payloads,
        "dependencies": [item.model_dump(mode="json") for item in dependencies],
        "fanout_policy": fanout_policy.model_dump(mode="json"),
        "waves": [item.model_dump(mode="json") for item in waves],
        "aggregation_strategy": aggregation_strategy.value,
        "plan_source": plan_source.value,
    }


def calculate_plan_hash(
    parent_run_id: UUID,
    workers: tuple[PlannedAgentTask, ...],
    dependencies: tuple[AgentDependency, ...],
    fanout_policy: AgentFanoutPolicy,
    waves: tuple[AgentExecutionWave, ...],
    aggregation_strategy: AgentAggregationStrategy,
    plan_source: AgentPlanSource,
) -> str:
    """
    Calculate the canonical SHA-256 identity for one execution plan.

    Returns:
        Lowercase hexadecimal SHA-256 plan hash.
    """
    payload = plan_hash_payload(
        parent_run_id=parent_run_id,
        workers=workers,
        dependencies=dependencies,
        fanout_policy=fanout_policy,
        waves=waves,
        aggregation_strategy=aggregation_strategy,
        plan_source=plan_source,
    )

    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


# --- Defining Dependency Helpers
def build_execution_waves(
    task_ids: tuple[UUID, ...],
    dependencies: tuple[AgentDependency, ...],
) -> tuple[AgentExecutionWave, ...]:
    """
    Build stable topological waves and reject cyclic dependency graphs.

    Args:
        task_ids: Every worker task identifier in plan order.
        dependencies: Directed dependency edges.

    Returns:
        Ordered execution waves containing each task exactly once.

    Raises:
        ValueError: If an edge references an unknown task or forms a cycle.
    """
    task_set = set(task_ids)
    incoming = {task_id: set() for task_id in task_ids}

    for dependency in dependencies:
        if dependency.upstream_task_id not in task_set:
            raise ValueError("Agent dependency references an unknown upstream task.")

        if dependency.downstream_task_id not in task_set:
            raise ValueError("Agent dependency references an unknown downstream task.")

        incoming[dependency.downstream_task_id].add(dependency.upstream_task_id)

    remaining = set(task_ids)
    completed: set[UUID] = set()
    waves: list[AgentExecutionWave] = []

    while remaining:
        ready = tuple(
            task_id
            for task_id in task_ids
            if task_id in remaining and incoming[task_id].issubset(completed)
        )

        if not ready:
            raise ValueError("Agent execution plan contains a dependency cycle.")

        waves.append(AgentExecutionWave(wave_index=len(waves), task_ids=ready))
        completed.update(ready)
        remaining.difference_update(ready)

    return tuple(waves)


# --- Defining Task Compilation Helpers
def build_task_for_proposal(
    request: SupervisorRequest,
    parent_run_id: UUID,
    proposal: ProposedAgentTask,
) -> AgentTaskEnvelope:
    """
    Convert a narrow proposal into a policy-owned specialist task.

    The proposal cannot provide context, tools, model route, or budgets. All of
    those fields are reconstructed from the validated parent request and registry.

    Args:
        request: Validated parent operator request.
        parent_run_id: Stable parent supervisor UUID.
        proposal: Allowlisted specialist and task suggestion.

    Returns:
        Fully authorized specialist envelope with stable identity.

    Raises:
        ValueError: If the proposal is unavailable for the parent request.
    """
    specialist = proposal.specialist_name.strip().lower()
    task_type   = proposal.task_type.strip().lower()

    if specialist == INCIDENT_TRIAGE_SPECIALIST_NAME and task_type == "triage_alert":
        if not request.alert_id and not request.alert_key:
            raise ValueError("Incident triage proposal requires parent alert context.")

        task = build_incident_triage_task(
            parent_run_id=parent_run_id,
            alert_id=request.alert_id,
            alert_key=request.alert_key,
            confidence_threshold=request.confidence_threshold,
            max_evidence_iterations=request.max_evidence_iterations,
            manifest_s3_uri=request.manifest_s3_uri,
            artifacts_bucket=request.artifacts_bucket,
            artifacts_prefix=request.artifacts_prefix,
            requester=request.requester,
        )

    elif specialist == METADATA_LINEAGE_SPECIALIST_NAME and task_type in {
        "asset_context",
        "blast_radius",
        "trusted_asset_search",
    }:
        if task_type != "trusted_asset_search" and not request.qualified_name:
            raise ValueError(f"{task_type} proposal requires parent qualified_name context.")

        if task_type == "trusted_asset_search" and not (request.query or request.question):
            raise ValueError("trusted_asset_search proposal requires parent search context.")

        task = build_metadata_lineage_task(
            parent_run_id=parent_run_id,
            task_type=task_type,
            qualified_name=request.qualified_name,
            query=request.query or request.question,
            domain=request.domain,
            data_layer=request.data_layer,
            certification_status=request.certification_status,
            lifecycle_status=request.lifecycle_status,
            limit=request.result_limit,
            max_depth=request.max_depth,
            max_nodes=request.max_nodes,
            requester=request.requester,
            alert_key=request.alert_key,
        )

    elif specialist == SQL_REVIEW_SPECIALIST_NAME and task_type == "review_sql":
        if not request.sql_proposal:
            raise ValueError("SQL review proposal requires parent sql_proposal context.")

        task = build_sql_review_task(
            parent_run_id=parent_run_id,
            sql_proposal=request.sql_proposal,
            purpose=request.sql_purpose,
            hard_limit=request.sql_hard_limit,
            require_date_filter=request.sql_require_date_filter,
            max_scan_bytes=request.sql_max_scan_bytes,
            requester=request.requester,
            alert_key=request.alert_key,
        )

    elif specialist == SCHEMA_DRIFT_SPECIALIST_NAME and task_type == "assess_schema_drift":
        if not request.schema_run_id or not request.qualified_name:
            raise ValueError("Schema drift proposal requires parent run and asset context.")

        task = build_schema_drift_task(
            parent_run_id=parent_run_id,
            source_schema_run_id=request.schema_run_id,
            qualified_name=request.qualified_name,
            finding_limit=request.schema_finding_limit,
            max_depth=request.max_depth,
            max_nodes=request.max_nodes,
            manifest_s3_uri=request.manifest_s3_uri,
            requester=request.requester,
            alert_key=request.alert_key,
        )

    else:
        raise ValueError(
            f"Unavailable proposed specialist task: {specialist}.{task_type}"
        )

    stable_task = assign_stable_task_identity(task)
    enforce_task_capability(stable_task)

    return stable_task


def intent_for_task(task: AgentTaskEnvelope) -> SupervisorIntent:
    """
    Resolve one task contract back to its bounded supervisor intent.

    Args:
        task: Authorized specialist task.

    Returns:
        SupervisorIntent represented by the task.
    """
    mapping = {
        (INCIDENT_TRIAGE_SPECIALIST_NAME, "triage_alert"): SupervisorIntent.TRIAGE_ALERT,
        (METADATA_LINEAGE_SPECIALIST_NAME, "asset_context"): SupervisorIntent.ASSET_CONTEXT,
        (METADATA_LINEAGE_SPECIALIST_NAME, "blast_radius"): SupervisorIntent.BLAST_RADIUS,
        (METADATA_LINEAGE_SPECIALIST_NAME, "trusted_asset_search"): SupervisorIntent.TRUSTED_ASSET_SEARCH,
        (SQL_REVIEW_SPECIALIST_NAME, "review_sql"): SupervisorIntent.REVIEW_SQL,
        (SCHEMA_DRIFT_SPECIALIST_NAME, "assess_schema_drift"): SupervisorIntent.SCHEMA_DRIFT_ASSESSMENT,
    }

    try:
        return mapping[(task.specialist_name, task.task_type)]

    except KeyError as exc:
        raise ValueError(
            f"No supervisor intent mapping for {task.specialist_name}.{task.task_type}."
        ) from exc


def build_deterministic_proposal(request: SupervisorRequest) -> AgentPlanningProposal:
    """
    Build the default two-specialist or two-task fan-out proposal.

    Args:
        request: Validated fan-out supervisor request.

    Returns:
        Narrow proposal containing a mandatory primary task and useful read-only evidence task.

    Raises:
        ValueError: If the request lacks a second independent bounded use case.
    """
    route = resolve_supervisor_route(request)
    tasks = [
        ProposedAgentTask(
            specialist_name=route.specialist_name,
            task_type=route.task_type,
            requirement=AgentTaskRequirement.REQUIRED,
            rationale=route.rationale,
        )
    ]

    secondary: ProposedAgentTask | None = None

    if route.intent == SupervisorIntent.ASSET_CONTEXT:
        secondary = ProposedAgentTask(
            specialist_name=METADATA_LINEAGE_SPECIALIST_NAME,
            task_type="blast_radius",
            rationale="Collect independent downstream-impact evidence for the requested asset.",
        )

    elif route.intent == SupervisorIntent.BLAST_RADIUS:
        secondary = ProposedAgentTask(
            specialist_name=METADATA_LINEAGE_SPECIALIST_NAME,
            task_type="asset_context",
            rationale="Collect owner, grain, lifecycle, and trust context beside blast-radius evidence.",
        )

    elif route.intent == SupervisorIntent.SCHEMA_DRIFT_ASSESSMENT:
        secondary = ProposedAgentTask(
            specialist_name=METADATA_LINEAGE_SPECIALIST_NAME,
            task_type="blast_radius",
            rationale="Collect independent lineage impact for the schema finding.",
        )

    elif route.intent == SupervisorIntent.TRIAGE_ALERT and request.qualified_name:
        secondary = ProposedAgentTask(
            specialist_name=METADATA_LINEAGE_SPECIALIST_NAME,
            task_type="blast_radius",
            rationale="Collect downstream impact while incident evidence is investigated.",
        )

    elif route.intent == SupervisorIntent.REVIEW_SQL and request.qualified_name:
        secondary = ProposedAgentTask(
            specialist_name=METADATA_LINEAGE_SPECIALIST_NAME,
            task_type="asset_context",
            rationale="Collect independent table trust context for the SQL review.",
        )

    if secondary is None:
        raise ValueError(
            "Fan-out requires a second independent bounded task; add exact asset context or use single mode."
        )

    tasks.append(secondary)

    return AgentPlanningProposal(tasks=tasks)


def compile_execution_plan(
    request: SupervisorRequest,
    parent_run_id: UUID,
    proposal: AgentPlanningProposal | None = None,
    dependencies: tuple[AgentDependency, ...] = (),
) -> AgentExecutionPlan:
    """
    Compile deterministic or model-proposed tasks into an immutable execution plan.

    Args:
        request: Validated fan-out parent request.
        parent_run_id: Stable parent supervisor UUID.
        proposal: Optional narrow model proposal. Policy supplies the default.
        dependencies: Optional explicit task dependencies for advanced callers.

    Returns:
        Fully validated AgentExecutionPlan.

    Raises:
        ValueError: If task count, capabilities, budgets, dependencies, or hashes are unsafe.
    """
    if request.execution_mode != SupervisorExecutionMode.FANOUT:
        raise ValueError("Execution plans can be compiled only for fanout mode.")

    selected_proposal = proposal or build_deterministic_proposal(request)
    plan_source       = (
        AgentPlanSource.LLM_PROPOSAL
        if proposal is not None
        else AgentPlanSource.DETERMINISTIC
    )
    worker_candidates: list[PlannedAgentTask] = []
    seen_signatures: set[str]                 = set()

    for item in selected_proposal.tasks:
        task      = build_task_for_proposal(request, parent_run_id, item)
        signature = task_signature(task)

        # Overlapping model suggestions are removed before capacity and budget
        # checks so duplicate evidence cannot consume parallel worker slots.
        if signature in seen_signatures:
            logger.info(
                "Removed duplicate proposed worker | specialist=%s task_type=%s",
                task.specialist_name,
                task.task_type,
            )
            continue

        seen_signatures.add(signature)
        worker_candidates.append(
            PlannedAgentTask(
                task=task,
                intent=intent_for_task(task),
                requirement=item.requirement,
                retry_budget=0,
                checkpoint_namespace=build_specialist_checkpoint_namespace(
                    parent_run_id=str(parent_run_id),
                    task_id=str(task.task_id),
                    specialist_name=task.specialist_name,
                ),
                rationale=item.rationale,
            )
        )

    workers = tuple(worker_candidates[: request.max_workers])

    if len(workers) < 2:
        raise ValueError("Fan-out plan requires at least two distinct authorized workers.")

    policy = AgentFanoutPolicy(
        max_workers=request.max_workers,
        max_concurrency=min(request.max_concurrency, len(workers)),
        max_model_calls=request.max_model_calls,
        token_budget=request.token_budget,
        estimated_cost_budget_usd=request.estimated_cost_budget_usd,
        latency_budget_ms=request.latency_budget_ms,
        allow_external_llm=request.allow_external_llm,
        mutation_allowed=False,
    )
    task_ids = tuple(worker.task.task_id for worker in workers)
    waves    = build_execution_waves(task_ids, dependencies)
    strategy = AgentAggregationStrategy.EVIDENCE_FIRST
    plan_hash = calculate_plan_hash(
        parent_run_id=parent_run_id,
        workers=workers,
        dependencies=dependencies,
        fanout_policy=policy,
        waves=waves,
        aggregation_strategy=strategy,
        plan_source=plan_source,
    )
    plan = AgentExecutionPlan(
        parent_run_id=parent_run_id,
        workers=workers,
        dependencies=dependencies,
        fanout_policy=policy,
        waves=waves,
        aggregation_strategy=strategy,
        plan_source=plan_source,
        deterministic_plan_hash=plan_hash,
    )

    validate_execution_plan(plan)

    logger.info(
        "Compiled bounded agent execution plan | parent_run_id=%s plan_hash=%s workers=%d waves=%d concurrency=%d source=%s",
        parent_run_id,
        plan.deterministic_plan_hash,
        len(plan.workers),
        len(plan.waves),
        plan.fanout_policy.max_concurrency,
        plan.plan_source.value,
    )

    return plan


# --- Defining Plan Validation
def validate_execution_plan(plan: AgentExecutionPlan) -> AgentExecutionPlan:
    """
    Revalidate capabilities, budgets, waves, identities, and deterministic hash.

    Args:
        plan: Candidate execution plan from policy compilation or persistence.

    Returns:
        Original plan when every invariant passes.

    Raises:
        ValueError: If plan topology, budgets, identities, or hash are inconsistent.
        PermissionError: If a worker exceeds registry permissions.
    """
    if len(plan.workers) > plan.fanout_policy.max_workers:
        raise ValueError("Execution plan exceeds its worker capacity.")

    task_ids = tuple(worker.task.task_id for worker in plan.workers)

    if len(set(task_ids)) != len(task_ids):
        raise ValueError("Execution plan contains duplicate worker task identifiers.")

    signatures = [task_signature(worker.task) for worker in plan.workers]

    if len(set(signatures)) != len(signatures):
        raise ValueError("Execution plan contains duplicate or overlapping worker tasks.")

    for worker in plan.workers:
        if worker.task.parent_run_id != plan.parent_run_id:
            raise ValueError("Worker parent_run_id does not match execution plan.")

        capability = enforce_task_capability(worker.task)

        if capability.mutation_allowed:
            raise PermissionError("Mutation-capable specialists cannot run in a parallel worker plan.")

    expected_waves = build_execution_waves(task_ids, plan.dependencies)

    if plan.waves != expected_waves:
        raise ValueError("Execution plan waves do not match deterministic dependency topology.")

    total_model_calls = sum(worker.task.model_call_budget for worker in plan.workers)
    total_tokens      = sum(worker.task.token_budget for worker in plan.workers)
    total_cost        = sum(worker.task.estimated_cost_budget_usd for worker in plan.workers)
    total_retries     = sum(worker.retry_budget for worker in plan.workers)

    if total_model_calls > plan.fanout_policy.max_model_calls:
        raise ValueError("Execution plan exceeds aggregate model-call budget.")

    if total_tokens > plan.fanout_policy.token_budget:
        raise ValueError("Execution plan exceeds aggregate token budget.")

    if total_cost > plan.fanout_policy.estimated_cost_budget_usd:
        raise ValueError("Execution plan exceeds aggregate estimated-cost budget.")

    if total_retries > MAX_AGENT_WORKERS * 2:
        raise ValueError("Execution plan exceeds bounded retry allocation.")

    expected_hash = calculate_plan_hash(
        parent_run_id=plan.parent_run_id,
        workers=plan.workers,
        dependencies=plan.dependencies,
        fanout_policy=plan.fanout_policy,
        waves=plan.waves,
        aggregation_strategy=plan.aggregation_strategy,
        plan_source=plan.plan_source,
    )

    if plan.deterministic_plan_hash != expected_hash:
        raise ValueError("Execution plan hash does not match its canonical policy payload.")

    return plan


# --- Defining Aggregation
def aggregate_agent_results(
    plan: AgentExecutionPlan,
    results: list[AgentResultEnvelope],
    duration_ms: int,
) -> AgentAggregationResult:
    """
    Aggregate worker evidence while making every failure visible to the parent.

    Args:
        plan: Validated immutable execution plan.
        results: Terminal worker results in any completion order.
        duration_ms: Parent fan-out wall-clock duration.

    Returns:
        Typed aggregation with explicit required and optional failures.

    Raises:
        ValueError: If a result does not belong to the plan or is duplicated.
    """
    validate_execution_plan(plan)
    workers_by_id = {worker.task.task_id: worker for worker in plan.workers}
    results_by_id: dict[UUID, AgentResultEnvelope] = {}

    for result in results:
        if result.task_id not in workers_by_id:
            raise ValueError("Aggregation received a result outside the execution plan.")

        if result.task_id in results_by_id:
            raise ValueError("Aggregation received duplicate results for one worker.")

        results_by_id[result.task_id] = result

    completed: list[UUID]       = []
    optional_failed: list[UUID] = []
    required_failed: list[UUID] = []
    missing_evidence: list[str] = []
    evidence_by_key: dict[tuple[str, str, str], EvidenceReference] = {}
    confidence_values: list[float] = []

    for worker in plan.workers:
        result = results_by_id.get(worker.task.task_id)

        if result is None:
            failure_message = (
                f"{worker.task.specialist_name}.{worker.task.task_type} returned no terminal result."
            )

            if worker.requirement == AgentTaskRequirement.REQUIRED:
                required_failed.append(worker.task.task_id)
            else:
                optional_failed.append(worker.task.task_id)

            missing_evidence.append(failure_message)
            continue

        if result.status in {AgentTaskStatus.SUCCESS, AgentTaskStatus.PARTIAL}:
            completed.append(result.task_id)
            confidence_values.append(result.confidence)

            for evidence in result.evidence_references:
                evidence_key = (
                    evidence.evidence_type,
                    evidence.source_tool,
                    evidence.reference,
                )
                evidence_by_key.setdefault(evidence_key, evidence)

            if result.status == AgentTaskStatus.PARTIAL:
                missing_evidence.extend(result.errors)

        else:
            failure_message = result.errors[0] if result.errors else (
                f"{result.specialist_name}.{result.task_type} failed without usable evidence."
            )

            if worker.requirement == AgentTaskRequirement.REQUIRED:
                required_failed.append(result.task_id)
            else:
                optional_failed.append(result.task_id)

            missing_evidence.append(failure_message)

    if required_failed:
        status     = AgentTaskStatus.BLOCKED
        confidence = 0.0
        summary    = (
            "The investigation is blocked because required evidence failed. "
            "No remediation may be proposed from this partial worker set."
        )

    elif optional_failed or missing_evidence:
        status     = AgentTaskStatus.PARTIAL
        confidence = min(confidence_values, default=0.0)
        summary    = (
            "The investigation retained usable evidence, but optional or partial evidence is missing. "
            "Review the disclosed worker gaps before acting."
        )

    else:
        status     = AgentTaskStatus.SUCCESS
        confidence = min(confidence_values, default=0.0)
        summary    = "All required and optional worker evidence completed successfully."

    return AgentAggregationResult(
        status=status,
        completed_task_ids=completed,
        optional_failed_task_ids=optional_failed,
        required_failed_task_ids=required_failed,
        evidence_references=list(evidence_by_key.values()),
        confidence=confidence,
        summary=summary,
        missing_evidence=missing_evidence[:20],
        model_call_count=sum(result.model_call_count for result in results),
        token_usage=sum(result.token_usage for result in results),
        estimated_cost_usd=sum(result.estimated_cost_usd for result in results),
        duration_ms=duration_ms,
    )
