####
## Control Plane Budget Policy for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Evaluate deterministic supervisor budget reservations and actual usage."""

# --- Importing Libraries
from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
from threading import Lock
from typing import Iterator
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from agent.specialists.contracts import (
    AgentResultEnvelope,
    AgentTaskEnvelope,
    HandoffRecord,
    SupervisorState,
)
from pipelines.common.logging import logger


# --- Defining Enumerations
class SupervisorBudgetStage(str, Enum):
    """Represent the lifecycle point evaluated by supervisor budget policy."""

    PRE_HANDOFF  = "pre_handoff"
    POST_HANDOFF = "post_handoff"


# --- Defining Budget Models
class SupervisorBudgetVector(BaseModel):
    """
    Store one comparable set of control-plane budget dimensions.

    Attributes:
        handoffs: Number of specialist handoffs.
        retries: Aggregate retry attempts across all handoffs.
        model_calls: External model calls made by specialists.
        tokens: Aggregate external model tokens.
        estimated_cost_usd: Aggregate estimated model cost in USD.
        latency_ms: End-to-end supervisor wall-clock latency.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    handoffs: int                 = Field(default=0, ge=0)
    retries: int                  = Field(default=0, ge=0)
    model_calls: int              = Field(default=0, ge=0)
    tokens: int                   = Field(default=0, ge=0)
    estimated_cost_usd: float     = Field(default=0.0, ge=0.0)
    latency_ms: int               = Field(default=0, ge=0)


class SupervisorBudgetDecision(BaseModel):
    """
    Return one reproducible supervisor budget-policy decision.

    Attributes:
        stage: Pre-handoff reservation or post-handoff reconciliation stage.
        allowed: Whether every projected or actual dimension remains in budget.
        limits: Configured parent-run maximums.
        usage: Projected or actual usage evaluated by policy.
        remaining: Non-negative remaining budget after the evaluated usage.
        violations: Stable policy codes for every exceeded dimension.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: SupervisorBudgetStage
    allowed: bool
    limits: SupervisorBudgetVector
    usage: SupervisorBudgetVector
    remaining: SupervisorBudgetVector
    violations: tuple[str, ...] = ()


# --- Defining Budget Exceptions
class SupervisorBudgetExceeded(PermissionError):
    """Carry the exact failed budget decision across the supervisor boundary."""

    def __init__(self, decision: SupervisorBudgetDecision) -> None:
        """
        Initialize a budget-policy exception.

        Args:
            decision: Reproducible decision containing exceeded dimensions.

        Returns:
            None.
        """
        self.decision = decision
        dimensions    = ", ".join(decision.violations)

        super().__init__(
            f"Supervisor {decision.stage.value} budget exceeded: {dimensions}."
        )


class SupervisorLlmBudgetExceeded(PermissionError):
    """Represent one rejected external model attempt inside a supervised run."""

    def __init__(self, violation: str) -> None:
        """
        Initialize an external model budget exception.

        Args:
            violation: Stable exceeded budget code.

        Returns:
            None.
        """
        self.violation = violation

        super().__init__(f"Supervisor LLM budget rejected provider call: {violation}.")


# --- Defining Run-Scoped LLM Budget State
@dataclass
class SupervisorLlmBudgetLedger:
    """
    Reserve and reconcile every external provider attempt in one specialist run.

    Attributes:
        max_model_calls: Maximum external provider attempts.
        token_budget: Maximum aggregate estimated or actual tokens.
        estimated_cost_budget_usd: Maximum estimated aggregate provider cost.
        deadline_monotonic: Monotonic deadline shared with the parent supervisor.
    """

    max_model_calls: int
    token_budget: int
    estimated_cost_budget_usd: float
    deadline_monotonic: float
    model_calls: int                         = 0
    tokens: int                              = 0
    estimated_cost_usd: float                = 0.0
    _reservations: dict[UUID, tuple[int, float]] = field(default_factory=dict)
    _lock: Lock                              = field(default_factory=Lock, repr=False)

    def remaining_latency_ms(self) -> int:
        """
        Return remaining monotonic deadline capacity.

        Returns:
            Non-negative milliseconds remaining before the supervisor deadline.
        """
        return max(0, int((self.deadline_monotonic - time.monotonic()) * 1_000))

    def reserve_model_call(
        self,
        projected_tokens: int,
        projected_cost_usd: float,
    ) -> UUID:
        """
        Reserve one external provider attempt before any network call occurs.

        Args:
            projected_tokens: Conservative input plus maximum-output token estimate.
            projected_cost_usd: Conservative cost estimate for the projected tokens.

        Returns:
            Reservation UUID used for success or failure reconciliation.

        Raises:
            SupervisorLlmBudgetExceeded: If call, token, cost, or deadline is exhausted.
        """
        with self._lock:
            if self.remaining_latency_ms() <= 0:
                raise SupervisorLlmBudgetExceeded("latency_ms_budget_exceeded")

            if self.model_calls + 1 > self.max_model_calls:
                raise SupervisorLlmBudgetExceeded("model_calls_budget_exceeded")

            if self.tokens + projected_tokens > self.token_budget:
                raise SupervisorLlmBudgetExceeded("tokens_budget_exceeded")

            if (
                self.estimated_cost_usd + projected_cost_usd
                > self.estimated_cost_budget_usd
            ):
                raise SupervisorLlmBudgetExceeded("estimated_cost_usd_budget_exceeded")

            reservation_id                  = uuid4()
            self.model_calls               += 1
            self.tokens                   += projected_tokens
            self.estimated_cost_usd       += projected_cost_usd
            self._reservations[reservation_id] = (
                projected_tokens,
                projected_cost_usd,
            )

        logger.info(
            "Reserved supervised LLM call | reservation_id=%s calls=%d tokens=%d cost=%.8f",
            reservation_id,
            self.model_calls,
            self.tokens,
            self.estimated_cost_usd,
        )

        return reservation_id

    def reconcile_model_call(
        self,
        reservation_id: UUID,
        actual_tokens: int,
        actual_cost_usd: float,
    ) -> None:
        """
        Replace one conservative reservation with actual provider usage.

        Args:
            reservation_id: Existing provider-attempt reservation UUID.
            actual_tokens: Provider-reported or estimated actual tokens.
            actual_cost_usd: Estimated actual provider cost.

        Returns:
            None.

        Raises:
            LookupError: If the reservation is unknown or already reconciled.
            SupervisorLlmBudgetExceeded: If actual usage exceeds token or cost policy.
        """
        with self._lock:
            projected = self._reservations.pop(reservation_id, None)

            if projected is None:
                raise LookupError("Unknown supervised LLM budget reservation.")

            projected_tokens, projected_cost = projected
            self.tokens                = self.tokens - projected_tokens + actual_tokens
            self.estimated_cost_usd    = (
                self.estimated_cost_usd - projected_cost + actual_cost_usd
            )

            if self.tokens > self.token_budget:
                raise SupervisorLlmBudgetExceeded("tokens_budget_exceeded")

            if self.estimated_cost_usd > self.estimated_cost_budget_usd:
                raise SupervisorLlmBudgetExceeded("estimated_cost_usd_budget_exceeded")

        logger.info(
            "Reconciled supervised LLM call | reservation_id=%s calls=%d tokens=%d cost=%.8f",
            reservation_id,
            self.model_calls,
            self.tokens,
            self.estimated_cost_usd,
        )

    def snapshot(self, latency_ms: int) -> SupervisorBudgetVector:
        """
        Return actual or conservatively reserved model usage for parent reconciliation.

        Args:
            latency_ms: Current parent-run wall-clock duration.

        Returns:
            SupervisorBudgetVector containing LLM and latency usage.
        """
        with self._lock:
            return SupervisorBudgetVector(
                model_calls=self.model_calls,
                tokens=self.tokens,
                estimated_cost_usd=self.estimated_cost_usd,
                latency_ms=max(0, latency_ms),
            )


@dataclass
class SupervisorFanoutBudgetAllocator:
    """
    Reserve immutable per-worker ceilings against one shared parent budget.

    Attributes:
        max_model_calls: Aggregate provider-call capacity for the parent run.
        token_budget: Aggregate provider token capacity for the parent run.
        estimated_cost_budget_usd: Aggregate estimated provider cost capacity.
        worker_capacity: Maximum unique worker reservations.
    """

    max_model_calls: int
    token_budget: int
    estimated_cost_budget_usd: float
    worker_capacity: int
    _allocations: dict[UUID, SupervisorBudgetVector] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock, repr=False)

    def reserve_worker(self, task: AgentTaskEnvelope) -> SupervisorBudgetVector:
        """
        Atomically reserve one worker's maximum model usage before execution.

        Repeated reservation for the same immutable task is idempotent, which
        allows checkpoint resume without double-counting completed workers.

        Args:
            task: Fully authorized worker task with explicit model ceilings.

        Returns:
            Reserved worker budget vector.

        Raises:
            SupervisorLlmBudgetExceeded: If worker capacity or parent budget is exceeded.
        """
        requested = SupervisorBudgetVector(
            model_calls=task.model_call_budget,
            tokens=task.token_budget,
            estimated_cost_usd=task.estimated_cost_budget_usd,
        )

        with self._lock:
            existing = self._allocations.get(task.task_id)

            if existing is not None:
                return existing

            if len(self._allocations) + 1 > self.worker_capacity:
                raise SupervisorLlmBudgetExceeded("worker_capacity_exceeded")

            used_calls = sum(item.model_calls for item in self._allocations.values())
            used_tokens = sum(item.tokens for item in self._allocations.values())
            used_cost = sum(item.estimated_cost_usd for item in self._allocations.values())

            if used_calls + requested.model_calls > self.max_model_calls:
                raise SupervisorLlmBudgetExceeded("model_calls_budget_exceeded")

            if used_tokens + requested.tokens > self.token_budget:
                raise SupervisorLlmBudgetExceeded("tokens_budget_exceeded")

            if used_cost + requested.estimated_cost_usd > self.estimated_cost_budget_usd:
                raise SupervisorLlmBudgetExceeded("estimated_cost_usd_budget_exceeded")

            self._allocations[task.task_id] = requested
            allocation_count               = len(self._allocations)

        logger.info(
            "Reserved fan-out worker budget | task_id=%s workers=%d calls=%d tokens=%d cost=%.8f",
            task.task_id,
            allocation_count,
            requested.model_calls,
            requested.tokens,
            requested.estimated_cost_usd,
        )

        return requested

    def snapshot(self) -> SupervisorBudgetVector:
        """
        Return the current conservative parent allocations.

        Returns:
            Aggregate reserved worker ceilings.
        """
        with self._lock:
            return SupervisorBudgetVector(
                handoffs=len(self._allocations),
                model_calls=sum(item.model_calls for item in self._allocations.values()),
                tokens=sum(item.tokens for item in self._allocations.values()),
                estimated_cost_usd=sum(
                    item.estimated_cost_usd
                    for item in self._allocations.values()
                ),
            )


ACTIVE_SUPERVISOR_LLM_BUDGET: ContextVar[SupervisorLlmBudgetLedger | None] = ContextVar(
    "active_supervisor_llm_budget",
    default=None,
)


@contextmanager
def supervisor_llm_budget_scope(
    max_model_calls: int,
    token_budget: int,
    estimated_cost_budget_usd: float,
    deadline_monotonic: float,
) -> Iterator[SupervisorLlmBudgetLedger]:
    """
    Install one run-scoped LLM ledger for all nested provider attempts.

    Args:
        max_model_calls: Maximum external provider attempts.
        token_budget: Maximum estimated or actual provider tokens.
        estimated_cost_budget_usd: Maximum estimated model cost.
        deadline_monotonic: Shared parent/specialist monotonic deadline.

    Yields:
        Active SupervisorLlmBudgetLedger.
    """
    ledger = SupervisorLlmBudgetLedger(
        max_model_calls=max_model_calls,
        token_budget=token_budget,
        estimated_cost_budget_usd=estimated_cost_budget_usd,
        deadline_monotonic=deadline_monotonic,
    )
    token  = ACTIVE_SUPERVISOR_LLM_BUDGET.set(ledger)

    try:
        yield ledger

    finally:
        ACTIVE_SUPERVISOR_LLM_BUDGET.reset(token)


def active_supervisor_llm_budget() -> SupervisorLlmBudgetLedger | None:
    """
    Return the current supervised LLM ledger when one is active.

    Returns:
        Active ledger or None for ordinary non-supervisor LLM calls.
    """
    return ACTIVE_SUPERVISOR_LLM_BUDGET.get()


# --- Defining Budget Helpers
def supervisor_budget_limits(state: SupervisorState) -> SupervisorBudgetVector:
    """
    Build the configured budget vector from supervisor state.

    Args:
        state: Parent state containing caller-approved budget limits.

    Returns:
        SupervisorBudgetVector containing all configured maximums.
    """
    return SupervisorBudgetVector(
        handoffs=state.max_handoffs,
        retries=state.max_retries,
        model_calls=state.max_model_calls,
        tokens=state.token_budget,
        estimated_cost_usd=state.estimated_cost_budget_usd,
        latency_ms=state.latency_budget_ms,
    )


def consumed_budget_usage(
    state: SupervisorState,
    elapsed_ms: int = 0,
) -> SupervisorBudgetVector:
    """
    Aggregate budget already consumed by accepted specialist results.

    Args:
        state: Current supervisor state.
        elapsed_ms: Current parent-run wall-clock duration.

    Returns:
        SupervisorBudgetVector containing current actual usage.
    """
    result_latency = sum(result.duration_ms for result in state.specialist_results)

    return SupervisorBudgetVector(
        handoffs=len(state.handoff_history),
        retries=sum(record.retry_count for record in state.handoff_history),
        model_calls=sum(result.model_call_count for result in state.specialist_results),
        tokens=sum(result.token_usage for result in state.specialist_results),
        estimated_cost_usd=sum(
            result.estimated_cost_usd
            for result in state.specialist_results
        ),
        latency_ms=max(elapsed_ms, result_latency),
    )


def build_budget_decision(
    stage: SupervisorBudgetStage,
    limits: SupervisorBudgetVector,
    usage: SupervisorBudgetVector,
) -> SupervisorBudgetDecision:
    """
    Compare one projected or actual usage vector with policy limits.

    Args:
        stage: Budget lifecycle stage.
        limits: Parent-run maximums.
        usage: Projected or actual budget usage.

    Returns:
        SupervisorBudgetDecision with stable violation codes and remaining values.
    """
    violations: list[str] = []

    for field_name in (
        "handoffs",
        "retries",
        "model_calls",
        "tokens",
        "estimated_cost_usd",
        "latency_ms",
    ):
        if getattr(usage, field_name) > getattr(limits, field_name):
            violations.append(f"{field_name}_budget_exceeded")

    remaining = SupervisorBudgetVector(
        handoffs=max(0, limits.handoffs - usage.handoffs),
        retries=max(0, limits.retries - usage.retries),
        model_calls=max(0, limits.model_calls - usage.model_calls),
        tokens=max(0, limits.tokens - usage.tokens),
        estimated_cost_usd=max(
            0.0,
            round(limits.estimated_cost_usd - usage.estimated_cost_usd, 8),
        ),
        latency_ms=max(0, limits.latency_ms - usage.latency_ms),
    )

    return SupervisorBudgetDecision(
        stage=stage,
        allowed=not violations,
        limits=limits,
        usage=usage,
        remaining=remaining,
        violations=tuple(violations),
    )


def evaluate_pre_handoff_budgets(
    task: AgentTaskEnvelope,
    state: SupervisorState,
    elapsed_ms: int = 0,
) -> SupervisorBudgetDecision:
    """
    Reserve the maximum declared resources before invoking one specialist.

    Args:
        task: Candidate specialist handoff.
        state: Current parent state and approved limits.
        elapsed_ms: Parent-run wall-clock duration before specialist execution.

    Returns:
        SupervisorBudgetDecision based on projected worst-case usage.
    """
    current              = consumed_budget_usage(state=state, elapsed_ms=elapsed_ms)
    remaining_latency_ms = max(0, state.latency_budget_ms - current.latency_ms)

    # The runtime caps specialist execution at the parent deadline. Reserving the
    # full child timeout in addition to elapsed time would reject a healthy run
    # whenever both limits are equal, even though the child cannot exceed the
    # remaining parent budget.
    projected_latency_ms = current.latency_ms + min(
        task.timeout_seconds * 1_000,
        remaining_latency_ms,
    )

    if remaining_latency_ms == 0:
        projected_latency_ms = current.latency_ms + 1

    projected = SupervisorBudgetVector(
        handoffs=current.handoffs + 1,
        retries=current.retries,
        model_calls=current.model_calls + task.model_call_budget,
        tokens=current.tokens + task.token_budget,
        estimated_cost_usd=(
            current.estimated_cost_usd + task.estimated_cost_budget_usd
        ),
        latency_ms=projected_latency_ms,
    )
    decision = build_budget_decision(
        stage=SupervisorBudgetStage.PRE_HANDOFF,
        limits=supervisor_budget_limits(state),
        usage=projected,
    )

    logger.info(
        "Evaluated pre-handoff budgets | task_id=%s allowed=%s violations=%s",
        task.task_id,
        decision.allowed,
        decision.violations,
    )

    return decision


def evaluate_post_handoff_budgets(
    state: SupervisorState,
    record: HandoffRecord,
    result: AgentResultEnvelope,
    elapsed_ms: int,
) -> SupervisorBudgetDecision:
    """
    Reconcile actual specialist usage against all parent-run budgets.

    Args:
        state: Parent state before the latest handoff is accepted.
        record: Terminal handoff lifecycle record.
        result: Terminal structured specialist result.
        elapsed_ms: End-to-end parent-run wall-clock duration.

    Returns:
        SupervisorBudgetDecision based on actual usage.
    """
    current = consumed_budget_usage(state=state)
    actual  = SupervisorBudgetVector(
        handoffs=current.handoffs + 1,
        retries=current.retries + record.retry_count,
        model_calls=current.model_calls + result.model_call_count,
        tokens=current.tokens + result.token_usage,
        estimated_cost_usd=current.estimated_cost_usd + result.estimated_cost_usd,
        latency_ms=max(
            elapsed_ms,
            current.latency_ms + result.duration_ms,
        ),
    )
    decision = build_budget_decision(
        stage=SupervisorBudgetStage.POST_HANDOFF,
        limits=supervisor_budget_limits(state),
        usage=actual,
    )

    logger.info(
        "Reconciled post-handoff budgets | task_id=%s allowed=%s violations=%s",
        result.task_id,
        decision.allowed,
        decision.violations,
    )

    return decision


def require_budget_decision(decision: SupervisorBudgetDecision) -> None:
    """
    Fail closed when a budget decision contains any exceeded dimension.

    Args:
        decision: Pre-handoff or post-handoff policy decision.

    Returns:
        None.

    Raises:
        SupervisorBudgetExceeded: If one or more dimensions exceed policy.
    """
    if not decision.allowed:
        raise SupervisorBudgetExceeded(decision)
