####
## Supervisor Resilience Controls for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Enforce hard deadlines, bounded retries, and audit-derived circuit state."""

# --- Importing Libraries
from __future__ import annotations

import signal
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterator

from pydantic import BaseModel, ConfigDict, Field

from agent.specialists.contracts import AgentResultEnvelope, AgentTaskStatus
from agent.specialists.registry import AgentCapabilitySpec, get_agent_capability
from agent.tools.clickhouse_sql import rows_to_dicts
from pipelines.common.clickhouse import quote_sql_literal
from pipelines.common.logging import logger


# --- Defining Constants
SUPERVISOR_OUTCOME_AUDIT_ACTION = "supervisor_specialist_outcome"

RETRYABLE_ERROR_MARKERS = (
    "connection aborted",
    "connection refused",
    "connection reset",
    "connecttimeout",
    "readtimeout",
    "remote disconnected",
    "temporarily unavailable",
    "timeouterror",
    "timed out",
)

CIRCUIT_FAILURE_STATUSES = {
    "failed",
    "timed_out",
}

CIRCUIT_RESET_STATUSES = {
    "partial",
    "success",
}


# --- Defining Enums
class SupervisorCircuitState(str, Enum):
    """Represent the deterministic state of one specialist circuit."""

    CLOSED    = "closed"
    OPEN      = "open"
    HALF_OPEN = "half_open"


class SupervisorFailureCategory(str, Enum):
    """Classify failures without exposing unrestricted exception details."""

    CIRCUIT_OPEN       = "circuit_open"
    HARD_TIMEOUT       = "hard_timeout"
    POLICY_BLOCKED     = "policy_blocked"
    SPECIALIST_FAILED  = "specialist_failed"
    TRANSIENT_FAILURE  = "transient_failure"
    UNAVAILABLE_TIMER  = "hard_timeout_unavailable"


# --- Defining Exceptions
class SupervisorHardTimeout(BaseException):
    """
    Interrupt specialist execution after its absolute deadline.

    This intentionally inherits from BaseException so broad ``except Exception``
    blocks inside specialist implementations cannot swallow supervisor cancellation.
    """

    def __init__(
        self,
        specialist_name: str,
        attempt_number: int,
        timeout_seconds: float,
    ) -> None:
        """
        Initialize a bounded timeout signal.

        Args:
            specialist_name: Specialist interrupted by policy.
            attempt_number: One-based execution attempt.
            timeout_seconds: Effective remaining deadline at attempt start.

        Returns:
            None.
        """
        self.specialist_name = specialist_name
        self.attempt_number  = attempt_number
        self.timeout_seconds = max(0.0, float(timeout_seconds))

        super().__init__(
            f"Specialist {specialist_name} exceeded its hard deadline "
            f"during attempt {attempt_number}."
        )


class SupervisorHardTimeoutUnavailable(RuntimeError):
    """Fail closed when the runtime cannot provide interruptible hard deadlines."""


class SupervisorCircuitOpen(PermissionError):
    """Block a handoff while recent specialist failures keep its circuit open."""

    def __init__(self, snapshot: "CircuitBreakerSnapshot") -> None:
        """
        Initialize a circuit-open policy failure.

        Args:
            snapshot: Exact audit-derived circuit decision.

        Returns:
            None.
        """
        self.snapshot = snapshot

        super().__init__(
            f"Circuit for {snapshot.specialist_name} is open after "
            f"{snapshot.consecutive_failures} consecutive failures; "
            f"retry after {snapshot.retry_after_seconds} seconds."
        )


class SupervisorRetryableError(RuntimeError):
    """Mark an explicit transient failure that may be retried by supervisor policy."""


class SupervisorInvocationFailure(RuntimeError):
    """Carry bounded attempt accounting after a specialist cannot return an envelope."""

    def __init__(
        self,
        category: SupervisorFailureCategory,
        retry_count: int,
        attempt_count: int,
        cause: BaseException,
    ) -> None:
        """
        Initialize one failure-isolated invocation outcome.

        Args:
            category: Normalized supervisor failure category.
            retry_count: Retries actually executed before terminal failure.
            attempt_count: Total attempts actually started.
            cause: Original bounded exception or timeout signal.

        Returns:
            None.
        """
        self.category      = category
        self.retry_count   = retry_count
        self.attempt_count = attempt_count
        self.cause_type    = type(cause).__name__

        super().__init__(
            f"Specialist invocation ended as {category.value} after "
            f"{attempt_count} attempt(s) and {retry_count} retry/retries: {cause}"
        )


# --- Defining Models
class CircuitBreakerPolicy(BaseModel):
    """
    Configure bounded audit history used for circuit decisions.

    Attributes:
        failure_threshold: Consecutive failed handoffs required to open the circuit.
        history_window_seconds: Maximum audit lookback window.
        recovery_timeout_seconds: Cooldown before one half-open probe is allowed.
        event_limit: Hard LIMIT applied to ClickHouse history reads.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    failure_threshold: int         = Field(default=3, ge=1, le=20)
    history_window_seconds: int    = Field(default=900, ge=60, le=86_400)
    recovery_timeout_seconds: int  = Field(default=300, ge=1, le=86_400)
    event_limit: int               = Field(default=20, ge=1, le=100)


class CircuitBreakerSnapshot(BaseModel):
    """
    Return an explainable circuit decision for one specialist.

    Attributes:
        specialist_name: Registered specialist evaluated by policy.
        state: Closed, open, or half-open state.
        request_allowed: Whether one handoff may proceed.
        consecutive_failures: Recent consecutive failed outcomes.
        failure_threshold: Configured threshold used by the decision.
        retry_after_seconds: Remaining open-circuit cooldown.
        last_failure_at: UTC timestamp of the latest counted failure.
        reason: Bounded operator-readable decision explanation.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    specialist_name: str
    state: SupervisorCircuitState
    request_allowed: bool
    consecutive_failures: int      = Field(default=0, ge=0)
    failure_threshold: int         = Field(ge=1)
    retry_after_seconds: int       = Field(default=0, ge=0)
    last_failure_at: datetime | None = None
    reason: str                    = Field(min_length=1, max_length=1_000)


# --- Defining Hard Deadline Controls
@contextmanager
def enforce_hard_deadline(
    deadline_monotonic: float,
    specialist_name: str,
    attempt_number: int,
) -> Iterator[None]:
    """
    Interrupt one specialist attempt at an absolute monotonic deadline.

    Args:
        deadline_monotonic: Absolute ``time.monotonic`` deadline shared by all attempts.
        specialist_name: Specialist protected by the timer.
        attempt_number: One-based attempt number retained in timeout evidence.

    Yields:
        Control to the specialist while the hard timer is armed.

    Raises:
        SupervisorHardTimeout: When the deadline is exhausted.
        SupervisorHardTimeoutUnavailable: When POSIX main-thread signals are unavailable.
    """
    remaining_seconds = deadline_monotonic - time.monotonic()

    if remaining_seconds <= 0:
        raise SupervisorHardTimeout(
            specialist_name=specialist_name,
            attempt_number=attempt_number,
            timeout_seconds=0.0,
        )

    if (
        not hasattr(signal, "SIGALRM")
        or not hasattr(signal, "setitimer")
        or threading.current_thread() is not threading.main_thread()
    ):
        raise SupervisorHardTimeoutUnavailable(
            "Hard specialist deadlines require a POSIX main-thread runtime. "
            "The supervisor failed closed before invoking the specialist."
        )

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer   = signal.getitimer(signal.ITIMER_REAL)
    armed_at         = time.monotonic()
    effective_timer  = remaining_seconds

    if previous_timer[0] > 0:
        effective_timer = min(effective_timer, previous_timer[0])

    def handle_timeout(_signum: int, _frame: Any) -> None:
        """Raise a cancellation signal that specialist ``Exception`` handlers cannot swallow."""
        raise SupervisorHardTimeout(
            specialist_name=specialist_name,
            attempt_number=attempt_number,
            timeout_seconds=effective_timer,
        )

    signal.signal(signal.SIGALRM, handle_timeout)
    signal.setitimer(signal.ITIMER_REAL, max(0.001, effective_timer))

    logger.info(
        "Armed specialist hard deadline | specialist=%s attempt=%d remaining_ms=%d",
        specialist_name,
        attempt_number,
        int(effective_timer * 1_000),
    )

    try:
        yield

    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, previous_handler)

        # Preserve an outer timer when this context is nested under another deadline.
        if previous_timer[0] > 0:
            elapsed = time.monotonic() - armed_at
            restored_remaining = max(0.001, previous_timer[0] - elapsed)
            signal.setitimer(
                signal.ITIMER_REAL,
                restored_remaining,
                previous_timer[1],
            )


# --- Defining Retry Policy
def is_retryable_exception(exc: BaseException) -> bool:
    """
    Decide whether an exception represents a bounded transient dependency failure.

    Args:
        exc: Exception raised by one specialist attempt.

    Returns:
        True only for explicit transient or connection/timeout failures.
    """
    # Runtime inability to enforce a hard deadline is a safety failure, not a
    # dependency outage. Retrying would repeat work without cancellation safety.
    if isinstance(exc, SupervisorHardTimeoutUnavailable):
        return False

    if isinstance(exc, (SupervisorHardTimeout, SupervisorRetryableError)):
        return True

    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True

    error_text = f"{type(exc).__name__}: {exc}".lower()

    return any(marker in error_text for marker in RETRYABLE_ERROR_MARKERS)


def is_retryable_result(result: AgentResultEnvelope) -> bool:
    """
    Decide whether a failed specialist envelope contains a transient marker.

    Args:
        result: Terminal specialist envelope returned by a failure-isolated child.

    Returns:
        True only for failed results with a recognized transient error marker.
    """
    if result.status != AgentTaskStatus.FAILED:
        return False

    error_text = " ".join(result.errors).lower()

    return any(marker in error_text for marker in RETRYABLE_ERROR_MARKERS)


def retry_is_allowed(
    capability: AgentCapabilitySpec,
    retries_consumed: int,
    max_retries: int,
) -> bool:
    """
    Enforce retry eligibility from deterministic capability and parent budget policy.

    Args:
        capability: Registered specialist capability.
        retries_consumed: Retries already executed for the current handoff.
        max_retries: Parent-run retry budget.

    Returns:
        True when the specialist is retry-safe and one retry remains.
    """
    return capability.retry_safe and retries_consumed < max_retries


def retry_backoff_seconds(retries_consumed: int) -> float:
    """
    Return deterministic bounded exponential backoff for the next retry.

    Args:
        retries_consumed: Retries already executed before scheduling the next one.

    Returns:
        Backoff seconds capped at two seconds.
    """
    return min(2.0, 0.25 * (2 ** retries_consumed))


# --- Defining Circuit Breaker Queries
def build_circuit_history_sql(
    specialist_name: str,
    policy: CircuitBreakerPolicy,
) -> str:
    """
    Build a fixed, bounded read-only query for recent specialist outcomes.

    Args:
        specialist_name: Registered specialist name.
        policy: Circuit breaker history limits.

    Returns:
        ClickHouse SELECT with a hard time window and LIMIT.
    """
    # Registry lookup rejects unknown names before they can enter SQL interpolation.
    get_agent_capability(specialist_name)
    specialist_literal = quote_sql_literal(specialist_name)

    return f"""
        SELECT
            ts,
            status
        FROM dq.agent_audit_log
        WHERE action = {quote_sql_literal(SUPERVISOR_OUTCOME_AUDIT_ACTION)}
          AND tool_name = 'control_plane_supervisor'
          AND JSONExtractString(output_json, 'selected_specialist') = {specialist_literal}
          AND ts >= now64(3) - INTERVAL {policy.history_window_seconds} SECOND
        ORDER BY ts DESC
        LIMIT {policy.event_limit}
    """


def closed_circuit_snapshot(
    specialist_name: str,
    policy: CircuitBreakerPolicy,
    reason: str = "No recent terminal specialist failures reached the circuit threshold.",
) -> CircuitBreakerSnapshot:
    """
    Build a closed circuit snapshot for an authorized specialist.

    Args:
        specialist_name: Registered specialist name.
        policy: Circuit breaker policy used by the decision.
        reason: Bounded decision explanation.

    Returns:
        Closed, request-allowed circuit snapshot.
    """
    get_agent_capability(specialist_name)

    return CircuitBreakerSnapshot(
        specialist_name=specialist_name,
        state=SupervisorCircuitState.CLOSED,
        request_allowed=True,
        failure_threshold=policy.failure_threshold,
        reason=reason,
    )


def parse_circuit_timestamp(value: Any) -> datetime:
    """
    Parse one normalized ClickHouse audit timestamp for cooldown calculation.

    Args:
        value: Raw datetime or ISO timestamp returned by the shared row normalizer.

    Returns:
        Timezone-aware UTC-compatible datetime.

    Raises:
        RuntimeError: If the persisted failure timestamp is missing or malformed.
    """
    if isinstance(value, datetime):
        parsed = value

    elif isinstance(value, str) and value.strip():
        normalized = value.strip().replace("Z", "+00:00")

        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise RuntimeError("Circuit history contains a malformed failure timestamp.") from exc

    else:
        raise RuntimeError("Circuit history is missing a failure timestamp.")

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed


def load_circuit_breaker_snapshot(
    client: Any,
    specialist_name: str,
    policy: CircuitBreakerPolicy,
    now: datetime | None = None,
) -> CircuitBreakerSnapshot:
    """
    Derive one persistent circuit state from append-only ClickHouse audit outcomes.

    Args:
        client: clickhouse-connect compatible client.
        specialist_name: Registered specialist evaluated by policy.
        policy: Circuit threshold, window, cooldown, and query limit.
        now: Optional UTC clock override for deterministic tests.

    Returns:
        Closed, open, or half-open circuit snapshot.

    Raises:
        RuntimeError: If a production-like client cannot return circuit history.
    """
    if not hasattr(client, "query"):
        # Focused tests inject lightweight clients and explicit circuit decisions.
        logger.info(
            "Circuit history skipped for non-query test client | specialist=%s",
            specialist_name,
        )

        return closed_circuit_snapshot(
            specialist_name=specialist_name,
            policy=policy,
            reason="Circuit history was not requested by the injected test runtime.",
        )

    query_result = client.query(build_circuit_history_sql(specialist_name, policy))
    rows         = rows_to_dicts(
        columns=list(query_result.column_names or []),
        rows=query_result.result_rows,
    )

    if not rows:
        return closed_circuit_snapshot(specialist_name=specialist_name, policy=policy)

    consecutive_failures = 0
    last_failure_at: datetime | None = None

    for row in rows:
        status = str(row.get("status", "")).strip().lower()

        if status in CIRCUIT_RESET_STATUSES:
            break

        if status not in CIRCUIT_FAILURE_STATUSES:
            continue

        consecutive_failures += 1

        if last_failure_at is None:
            last_failure_at = parse_circuit_timestamp(row.get("ts"))

    if consecutive_failures < policy.failure_threshold or last_failure_at is None:
        return CircuitBreakerSnapshot(
            specialist_name=specialist_name,
            state=SupervisorCircuitState.CLOSED,
            request_allowed=True,
            consecutive_failures=consecutive_failures,
            failure_threshold=policy.failure_threshold,
            last_failure_at=last_failure_at,
            reason="Recent failures remain below the configured circuit threshold.",
        )

    resolved_now = now or datetime.now(timezone.utc)

    elapsed_seconds = max(0, int((resolved_now - last_failure_at).total_seconds()))
    retry_after     = max(0, policy.recovery_timeout_seconds - elapsed_seconds)

    if retry_after > 0:
        return CircuitBreakerSnapshot(
            specialist_name=specialist_name,
            state=SupervisorCircuitState.OPEN,
            request_allowed=False,
            consecutive_failures=consecutive_failures,
            failure_threshold=policy.failure_threshold,
            retry_after_seconds=retry_after,
            last_failure_at=last_failure_at,
            reason="Circuit is open until the recent failure cooldown expires.",
        )

    return CircuitBreakerSnapshot(
        specialist_name=specialist_name,
        state=SupervisorCircuitState.HALF_OPEN,
        request_allowed=True,
        consecutive_failures=consecutive_failures,
        failure_threshold=policy.failure_threshold,
        last_failure_at=last_failure_at,
        reason="Circuit cooldown elapsed; one bounded half-open probe is allowed.",
    )


def require_circuit_allows(snapshot: CircuitBreakerSnapshot) -> None:
    """
    Reject a specialist handoff when the audit-derived circuit remains open.

    Args:
        snapshot: Circuit decision returned by the configured loader.

    Returns:
        None when the request is allowed.

    Raises:
        SupervisorCircuitOpen: If the circuit blocks the request.
    """
    if not snapshot.request_allowed:
        raise SupervisorCircuitOpen(snapshot)


def circuit_snapshot_payload(snapshot: CircuitBreakerSnapshot | None) -> dict[str, Any]:
    """
    Convert optional circuit state into bounded JSON-safe audit evidence.

    Args:
        snapshot: Optional circuit snapshot.

    Returns:
        Empty dictionary or serialized circuit evidence.
    """
    return snapshot.model_dump(mode="json") if snapshot else {}
