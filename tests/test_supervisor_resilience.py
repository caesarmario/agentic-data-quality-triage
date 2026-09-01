####
## Supervisor Resilience Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Validate hard deadlines, retry permissions, circuit state, and smoke contracts."""

# --- Importing Libraries
from __future__ import annotations

import signal
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from agent.specialists.contracts import (
    AgentModelRoute,
    AgentResultEnvelope,
    AgentTaskStatus,
    EvidenceReference,
    SupervisorState,
)
from agent.specialists.incident_triage import build_incident_triage_task
from agent.specialists.metadata_lineage import build_metadata_lineage_task
from agent.specialists.registry import (
    INCIDENT_TRIAGE_SPECIALIST_NAME,
    METADATA_LINEAGE_SPECIALIST_NAME,
    SCHEMA_DRIFT_SPECIALIST_NAME,
    SQL_REVIEW_SPECIALIST_NAME,
    get_agent_capability,
)
from agent.supervisor.models import SupervisorRequest
from agent.supervisor.resilience import (
    CircuitBreakerPolicy,
    SupervisorCircuitState,
    SupervisorFailureCategory,
    SupervisorHardTimeout,
    SupervisorHardTimeoutUnavailable,
    SupervisorInvocationFailure,
    SupervisorRetryableError,
    build_circuit_history_sql,
    enforce_hard_deadline,
    is_retryable_exception,
    load_circuit_breaker_snapshot,
    parse_circuit_timestamp,
)
from agent.supervisor.routing import resolve_supervisor_route
from agent.supervisor.runtime import (
    SupervisorRuntimeConfig,
    invoke_specialist_with_resilience,
    resolve_retry_schedule,
    run_control_plane_supervisor,
)
from agent.supervisor.smoke import (
    SupervisorResilienceScenario,
    build_resilience_smoke_request,
    build_resilience_smoke_runtime,
    expected_smoke_status,
    supported_resilience_scenarios,
)
from scripts.trigger_airflow_control_plane_resilience import (
    CONTROL_PLANE_RESILIENCE_DAG_ID,
    build_trigger_command,
    validate_trigger_inputs,
)


# --- Defining Test Doubles
class FakeQueryResult:
    """Provide clickhouse-connect-compatible circuit history rows."""

    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        """
        Initialize fixed timestamp and status rows.

        Args:
            rows: Query rows ordered newest first.

        Returns:
            None.
        """
        self.column_names = ["ts", "status"]
        self.result_rows  = rows


class CircuitClient:
    """Return fixed audit-derived circuit outcomes."""

    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        """
        Store deterministic circuit rows.

        Args:
            rows: Timestamp and terminal-status rows.

        Returns:
            None.
        """
        self.rows      = rows
        self.last_sql  = ""

    def query(self, sql: str) -> FakeQueryResult:
        """
        Capture bounded SQL and return fixed rows.

        Args:
            sql: Circuit history query.

        Returns:
            Fixed query result.
        """
        self.last_sql = sql

        return FakeQueryResult(self.rows)


class AuditRecorder:
    """Capture supervisor attempt audit events without ClickHouse."""

    def __init__(self) -> None:
        """Initialize an empty event list."""
        self.events: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> UUID:
        """
        Retain one audit event.

        Args:
            kwargs: Agent audit writer keyword arguments.

        Returns:
            Existing parent run UUID.
        """
        self.events.append(kwargs)

        return UUID(str(kwargs["agent_run_id"]))


def successful_metadata_result(task: Any) -> AgentResultEnvelope:
    """
    Build one deterministic metadata result for retry tests.

    Args:
        task: Source metadata handoff.

    Returns:
        Successful no-LLM result envelope.
    """
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
                summary="Bounded metadata catalog evidence was retained after retry.",
            )
        ],
        structured_output={"summary": "Metadata evidence loaded after one retry."},
        confidence=1.0,
        model_route=AgentModelRoute.NO_LLM_FALLBACK,
        duration_ms=10,
        recommended_next_step="Continue with the retained read-only evidence.",
    )


# --- Testing Capability Policy
def test_only_read_only_append_audit_specialists_are_retry_safe() -> None:
    """Incident report/lifecycle side effects must remain outside automatic retry."""
    assert get_agent_capability(INCIDENT_TRIAGE_SPECIALIST_NAME).retry_safe is False
    assert get_agent_capability(METADATA_LINEAGE_SPECIALIST_NAME).retry_safe is True
    assert get_agent_capability(SQL_REVIEW_SPECIALIST_NAME).retry_safe is True
    assert get_agent_capability(SCHEMA_DRIFT_SPECIALIST_NAME).retry_safe is True


# --- Testing Hard Deadlines
@pytest.mark.skipif(
    not hasattr(signal, "SIGALRM") or not hasattr(signal, "setitimer"),
    reason="Hard signal deadline requires the Linux Airflow runtime.",
)
def test_hard_deadline_interrupts_specialist_instead_of_leaving_background_work() -> None:
    """POSIX deadline must interrupt blocking work in the current process."""
    started = time.monotonic()

    with pytest.raises(SupervisorHardTimeout):
        with enforce_hard_deadline(
            deadline_monotonic=time.monotonic() + 0.05,
            specialist_name=METADATA_LINEAGE_SPECIALIST_NAME,
            attempt_number=1,
        ):
            time.sleep(1)

    assert time.monotonic() - started < 0.5


def test_unavailable_hard_timeout_control_is_not_retryable() -> None:
    """A runtime without cancellation safety must fail closed without another attempt."""
    failure = SupervisorHardTimeoutUnavailable(
        "POSIX main-thread timer is unavailable."
    )

    assert is_retryable_exception(failure) is False


def test_retry_schedule_rejects_backoff_that_cannot_fit_deadline() -> None:
    """Audit must never advertise a retry that cannot start before the deadline."""
    allowed, backoff_seconds, deadline_rejected = resolve_retry_schedule(
        capability=get_agent_capability(METADATA_LINEAGE_SPECIALIST_NAME),
        retries_consumed=0,
        max_retries=1,
        deadline_monotonic=time.monotonic() + 0.01,
    )

    assert allowed is False
    assert backoff_seconds == 0.25
    assert deadline_rejected is True


# --- Testing Bounded Retry
def test_retry_safe_specialist_retries_one_transient_failure_and_reuses_task_id() -> None:
    """One transient read-only failure must consume exactly one parent retry."""
    recorder  = AuditRecorder()
    calls: list[UUID] = []
    parent_run_id = uuid4()
    task = build_metadata_lineage_task(
        parent_run_id=parent_run_id,
        task_type="asset_context",
        qualified_name="dq.raw_orders",
    )
    request = SupervisorRequest(
        intent="asset_context",
        qualified_name="dq.raw_orders",
        max_retries=1,
        max_model_calls=0,
        token_budget=0,
        estimated_cost_budget_usd=0.0,
    )
    route = resolve_supervisor_route(request)
    state = SupervisorState(
        parent_run_id=parent_run_id,
        max_handoffs=1,
        max_retries=1,
        max_model_calls=0,
        token_budget=0,
        estimated_cost_budget_usd=0.0,
        latency_budget_ms=60_000,
    )

    def transient_once_runner(task: Any, **_: Any) -> AgentResultEnvelope:
        """Raise one transient error before returning deterministic evidence."""
        calls.append(task.task_id)

        if len(calls) == 1:
            raise SupervisorRetryableError("connection refused during controlled read")

        return successful_metadata_result(task)

    runtime = SupervisorRuntimeConfig(
        metadata_lineage_runner=transient_once_runner,
        audit_writer=recorder,
        sleep_callable=lambda _seconds: None,
    )
    outcome = invoke_specialist_with_resilience(
        task=task,
        request=request,
        route=route,
        state=state,
        config=runtime,
        client=object(),
        parent_run_id=parent_run_id,
        deadline_monotonic=time.monotonic() + 5,
    )
    actions = [event["action"] for event in recorder.events]

    assert outcome.result.status == AgentTaskStatus.SUCCESS
    assert outcome.retry_count == 1
    assert outcome.attempt_count == 2
    assert calls == [task.task_id, task.task_id]
    assert actions.count("supervisor_specialist_attempt_started") == 2
    assert actions.count("supervisor_specialist_attempt_failed") == 1
    assert actions.count("supervisor_specialist_retry_scheduled") == 1
    assert actions.count("supervisor_specialist_attempt_completed") == 1


def test_transient_failure_does_not_schedule_retry_after_deadline_rejection() -> None:
    """Deadline-rejected backoff must terminate with hard-timeout audit accounting."""
    recorder      = AuditRecorder()
    parent_run_id = uuid4()
    task = build_metadata_lineage_task(
        parent_run_id=parent_run_id,
        task_type="asset_context",
        qualified_name="dq.raw_orders",
    )
    request = SupervisorRequest(
        intent="asset_context",
        qualified_name="dq.raw_orders",
        max_retries=1,
        max_model_calls=0,
        token_budget=0,
        estimated_cost_budget_usd=0.0,
    )
    route = resolve_supervisor_route(request)
    state = SupervisorState(
        parent_run_id=parent_run_id,
        max_handoffs=1,
        max_retries=1,
        max_model_calls=0,
        token_budget=0,
        estimated_cost_budget_usd=0.0,
        latency_budget_ms=60_000,
    )

    def always_transient_runner(task: Any, **_: Any) -> AgentResultEnvelope:
        """Raise one controlled transient error before any side effect."""
        raise SupervisorRetryableError(f"controlled transient failure for {task.task_id}")

    runtime = SupervisorRuntimeConfig(
        metadata_lineage_runner=always_transient_runner,
        audit_writer=recorder,
        sleep_callable=lambda _seconds: None,
    )

    with pytest.raises(SupervisorInvocationFailure) as raised:
        invoke_specialist_with_resilience(
            task=task,
            request=request,
            route=route,
            state=state,
            config=runtime,
            client=object(),
            parent_run_id=parent_run_id,
            deadline_monotonic=time.monotonic() + 0.01,
        )

    actions = [event["action"] for event in recorder.events]
    failure_payload = recorder.events[-1]["output_payload"]["resilience"]

    assert raised.value.category == SupervisorFailureCategory.HARD_TIMEOUT
    assert actions == [
        "supervisor_specialist_attempt_started",
        "supervisor_specialist_attempt_failed",
    ]
    assert failure_payload["retry_scheduled"] is False


def test_terminal_failure_retains_complete_parent_handoff_lifecycle() -> None:
    """One terminal specialist exception must remain isolated and fully auditable."""
    recorder = AuditRecorder()
    runtime = replace(
        build_resilience_smoke_runtime(
            SupervisorResilienceScenario.TERMINAL_FAILURE
        ),
        audit_client_factory=lambda **_: object(),
        audit_writer=recorder,
        context_schema_ensurer=lambda _client: None,
        context_event_writer=lambda client, event: event.context_event_id,
        incident_memory_writer=lambda client, record: record.memory_id,
    )
    result = run_control_plane_supervisor(
        request=build_resilience_smoke_request(
            SupervisorResilienceScenario.TERMINAL_FAILURE
        ),
        external_run_id="manual__terminal_failure_audit_contract",
        config=runtime,
    )
    actions = [event["action"] for event in recorder.events]

    assert result.status == AgentTaskStatus.BLOCKED
    assert result.failure_isolated is True
    assert result.supervisor_state.specialist_results == []
    assert len(result.supervisor_state.handoff_history) == 1
    assert actions == [
        "supervisor_run_started",
        "supervisor_intent_classified",
        "supervisor_budget_prechecked",
        "supervisor_circuit_checked",
        "supervisor_route_selected",
        "supervisor_handoff_started",
        "supervisor_specialist_attempt_started",
        "supervisor_specialist_attempt_failed",
        "supervisor_handoff_failed",
        "supervisor_specialist_outcome",
        "supervisor_final_decision",
    ]
    final_payload = recorder.events[-1]["output_payload"]

    assert final_payload["approval_state"] == "not_required"
    assert final_payload["resilience"]["failure_category"] == "specialist_failed"
    assert final_payload["resilience"]["attempt_count"] == 1


def test_incident_triage_cannot_enable_automatic_retry() -> None:
    """A report-writing triage handoff must be blocked before its first retryable attempt."""
    parent_run_id = uuid4()
    task = build_incident_triage_task(
        parent_run_id=parent_run_id,
        alert_key="DQ-20260822-A1B2C3",
    )
    request = SupervisorRequest(
        intent="triage_alert",
        alert_key="DQ-20260822-A1B2C3",
        max_retries=1,
    )
    route = resolve_supervisor_route(request)
    state = SupervisorState(
        parent_run_id=parent_run_id,
        max_handoffs=1,
        max_retries=1,
        max_model_calls=3,
        token_budget=16_384,
        estimated_cost_budget_usd=0.05,
        latency_budget_ms=300_000,
    )

    with pytest.raises(PermissionError, match="not eligible for automatic retries"):
        invoke_specialist_with_resilience(
            task=task,
            request=request,
            route=route,
            state=state,
            config=SupervisorRuntimeConfig(),
            client=object(),
            parent_run_id=parent_run_id,
            deadline_monotonic=time.monotonic() + 5,
        )


# --- Testing Persistent Circuit State
def test_circuit_opens_from_three_recent_consecutive_failed_handoffs() -> None:
    """Audit-derived failures at threshold must block another specialist request."""
    now    = datetime.now(timezone.utc)
    policy = CircuitBreakerPolicy(
        failure_threshold=3,
        history_window_seconds=900,
        recovery_timeout_seconds=300,
        event_limit=20,
    )
    client = CircuitClient(
        [
            (now - timedelta(seconds=10), "timed_out"),
            (now - timedelta(seconds=20), "failed"),
            (now - timedelta(seconds=30), "failed"),
        ]
    )
    snapshot = load_circuit_breaker_snapshot(
        client=client,
        specialist_name=METADATA_LINEAGE_SPECIALIST_NAME,
        policy=policy,
        now=now,
    )

    assert snapshot.state == SupervisorCircuitState.OPEN
    assert snapshot.request_allowed is False
    assert snapshot.consecutive_failures == 3
    assert snapshot.retry_after_seconds == 290
    assert "ORDER BY ts DESC" in client.last_sql
    assert "LIMIT 20" in client.last_sql


def test_circuit_allows_half_open_probe_after_cooldown() -> None:
    """Expired cooldown must allow one policy-visible half-open probe."""
    now    = datetime.now(timezone.utc)
    policy = CircuitBreakerPolicy(
        failure_threshold=3,
        recovery_timeout_seconds=5,
    )
    rows = [
        (now - timedelta(seconds=10), "failed"),
        (now - timedelta(seconds=11), "failed"),
        (now - timedelta(seconds=12), "failed"),
    ]
    snapshot = load_circuit_breaker_snapshot(
        client=CircuitClient(rows),
        specialist_name=METADATA_LINEAGE_SPECIALIST_NAME,
        policy=policy,
        now=now,
    )

    assert snapshot.state == SupervisorCircuitState.HALF_OPEN
    assert snapshot.request_allowed is True
    assert snapshot.retry_after_seconds == 0


def test_circuit_query_rejects_unknown_specialist_and_remains_bounded() -> None:
    """Circuit SQL must accept only registry names and always retain a hard LIMIT."""
    policy = CircuitBreakerPolicy(event_limit=7)
    sql = build_circuit_history_sql(
        specialist_name=METADATA_LINEAGE_SPECIALIST_NAME,
        policy=policy,
    )

    assert "SELECT" in sql
    assert "INTERVAL 900 SECOND" in sql
    assert "LIMIT 7" in sql

    with pytest.raises(LookupError):
        build_circuit_history_sql(
            specialist_name="unknown_agent",
            policy=policy,
        )


def test_circuit_timestamp_parser_accepts_normalized_iso_and_rejects_corruption() -> None:
    """ClickHouse-normalized timestamps must remain usable without unsafe defaults."""
    parsed = parse_circuit_timestamp("2026-08-22T03:00:00+00:00")

    assert parsed == datetime(2026, 8, 22, 3, 0, tzinfo=timezone.utc)

    with pytest.raises(RuntimeError, match="malformed failure timestamp"):
        parse_circuit_timestamp("not-a-timestamp")


# --- Testing Airflow Smoke Contracts
def test_resilience_scenarios_have_explicit_expected_statuses() -> None:
    """Every allowlisted Airflow failure scenario must have a deterministic outcome."""
    assert supported_resilience_scenarios() == (
        "transient_once",
        "hard_timeout",
        "circuit_open",
        "partial_result",
        "terminal_failure",
    )
    assert expected_smoke_status("transient_once") == AgentTaskStatus.SUCCESS
    assert expected_smoke_status("hard_timeout") == AgentTaskStatus.BLOCKED
    assert expected_smoke_status("circuit_open") == AgentTaskStatus.BLOCKED
    assert expected_smoke_status("partial_result") == AgentTaskStatus.PARTIAL
    assert expected_smoke_status("terminal_failure") == AgentTaskStatus.BLOCKED


def test_resilience_trigger_uses_only_allowlisted_structured_configuration() -> None:
    """Trigger helper must reject arbitrary scenarios and shell-like run identifiers."""
    scenario, run_id = validate_trigger_inputs(
        scenario="transient_once",
        run_id="manual__resilience_test",
    )
    command = build_trigger_command(scenario=scenario, run_id=run_id)
    conf    = command[command.index("-c") + 1]

    assert command[0:3] == ["airflow", "dags", "trigger"]
    assert command[-1] == CONTROL_PLANE_RESILIENCE_DAG_ID
    assert conf == '{"scenario":"transient_once"}'

    with pytest.raises(ValueError, match="Unsupported resilience scenario"):
        validate_trigger_inputs("arbitrary_shell", "manual__safe")

    with pytest.raises(ValueError, match="unsupported characters"):
        validate_trigger_inputs("hard_timeout", "manual__bad;rm")
