####
## Supervisor Resilience Smoke Scenarios for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Provide allowlisted failure scenarios for manual Airflow resilience acceptance."""

# --- Importing Libraries
from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

from agent.specialists.contracts import AgentResultEnvelope, AgentTaskStatus
from agent.specialists.metadata_lineage import run_metadata_lineage_agent
from agent.specialists.registry import METADATA_LINEAGE_SPECIALIST_NAME
from agent.supervisor.models import SupervisorRequest
from agent.supervisor.resilience import (
    CircuitBreakerPolicy,
    CircuitBreakerSnapshot,
    SupervisorCircuitState,
    SupervisorRetryableError,
)
from agent.supervisor.runtime import SupervisorRuntimeConfig
from agent.supervisor.scenario_registry import SINGLE_HANDOFF_RESILIENCE_SCENARIOS
from pipelines.common.logging import logger


# --- Defining Enums
class SupervisorResilienceScenario(str, Enum):
    """Represent controlled smoke behavior accepted by the administrative DAG."""

    TRANSIENT_ONCE   = "transient_once"
    HARD_TIMEOUT     = "hard_timeout"
    CIRCUIT_OPEN     = "circuit_open"
    PARTIAL_RESULT   = "partial_result"
    TERMINAL_FAILURE = "terminal_failure"


# --- Defining Scenario Runners
@dataclass
class TransientOnceMetadataRunner:
    """Raise one explicit transient failure before executing the real read-only specialist."""

    attempt_count: int = 0

    def __call__(self, task: Any, config: Any) -> AgentResultEnvelope:
        """
        Execute one retryable failure followed by the real metadata specialist.

        Args:
            task: Typed metadata specialist handoff.
            config: Existing metadata specialist runtime configuration.

        Returns:
            Real metadata specialist result on the second attempt.

        Raises:
            SupervisorRetryableError: On the first controlled attempt only.
        """
        self.attempt_count += 1

        logger.info(
            "Running transient-once smoke specialist | task_id=%s attempt=%d",
            task.task_id,
            self.attempt_count,
        )

        if self.attempt_count == 1:
            raise SupervisorRetryableError(
                "Controlled transient connection failure before specialist side effects."
            )

        return run_metadata_lineage_agent(task=task, config=config)


def run_timeout_smoke_specialist(task: Any, config: Any) -> AgentResultEnvelope:
    """
    Sleep beyond the one-second smoke deadline without producing side effects.

    Args:
        task: Typed metadata specialist handoff.
        config: Unused metadata runtime configuration.

    Returns:
        No result; supervisor hard timeout interrupts this function first.
    """
    del config

    logger.info(
        "Starting controlled timeout smoke specialist | task_id=%s sleep_seconds=5",
        task.task_id,
    )
    time.sleep(5)

    raise AssertionError("Hard deadline failed to interrupt the timeout smoke specialist.")


def run_partial_smoke_specialist(task: Any, config: Any) -> AgentResultEnvelope:
    """
    Convert one real read-only metadata result into a bounded partial result.

    Args:
        task: Typed metadata specialist handoff.
        config: Existing metadata specialist runtime configuration.

    Returns:
        Partial envelope retaining safe evidence and an explicit optional-enrichment error.
    """
    result = run_metadata_lineage_agent(task=task, config=config)

    if result.status != AgentTaskStatus.SUCCESS:
        return result

    return AgentResultEnvelope.model_validate(
        {
            **result.model_dump(mode="python"),
            "status": AgentTaskStatus.PARTIAL,
            "errors": [
                "ControlledOptionalEnrichmentError: primary metadata evidence remains usable."
            ],
            "recommended_next_step": (
                "Use the retained metadata evidence and retry only the optional enrichment later."
            ),
        }
    )


def run_terminal_failure_smoke_specialist(
    task: Any,
    config: Any,
) -> AgentResultEnvelope:
    """
    Raise one non-retryable failure before any specialist side effect occurs.

    Args:
        task: Typed metadata specialist handoff.
        config: Unused metadata runtime configuration.

    Returns:
        No result because the controlled failure is terminal.

    Raises:
        RuntimeError: Always, to validate terminal failure audit completeness.
    """
    del config

    logger.info(
        "Starting controlled terminal-failure smoke specialist | task_id=%s",
        task.task_id,
    )

    raise RuntimeError("Controlled terminal specialist failure before side effects.")


# --- Defining Circuit Smoke Helpers
def force_open_circuit_snapshot(
    client: Any,
    specialist_name: str,
    policy: CircuitBreakerPolicy,
) -> CircuitBreakerSnapshot:
    """
    Return an explicit open circuit without invoking a specialist.

    Args:
        client: Unused audit client retained for loader compatibility.
        specialist_name: Specialist selected by deterministic routing.
        policy: Runtime circuit policy.

    Returns:
        Open and request-blocking circuit snapshot.
    """
    del client

    return CircuitBreakerSnapshot(
        specialist_name=specialist_name,
        state=SupervisorCircuitState.OPEN,
        request_allowed=False,
        consecutive_failures=policy.failure_threshold,
        failure_threshold=policy.failure_threshold,
        retry_after_seconds=policy.recovery_timeout_seconds,
        reason="Controlled Airflow smoke scenario forced the circuit open.",
    )


def force_closed_circuit_snapshot(
    client: Any,
    specialist_name: str,
    policy: CircuitBreakerPolicy,
) -> CircuitBreakerSnapshot:
    """
    Return an isolated closed circuit for non-circuit smoke scenarios.

    Args:
        client: Unused audit client retained for loader compatibility.
        specialist_name: Specialist selected by deterministic routing.
        policy: Runtime circuit policy.

    Returns:
        Closed circuit snapshot that isolates one controlled acceptance scenario.
    """
    del client

    return CircuitBreakerSnapshot(
        specialist_name=specialist_name,
        state=SupervisorCircuitState.CLOSED,
        request_allowed=True,
        consecutive_failures=0,
        failure_threshold=policy.failure_threshold,
        retry_after_seconds=0,
        reason="Controlled Airflow smoke scenario uses an isolated closed circuit.",
    )


def fail_if_circuit_invokes_specialist(task: Any, config: Any) -> AgentResultEnvelope:
    """
    Fail the smoke scenario if an open circuit does not block execution.

    Args:
        task: Specialist task that must never be invoked.
        config: Unused specialist configuration.

    Returns:
        No result.

    Raises:
        AssertionError: Always, because reaching this function violates circuit policy.
    """
    del config

    raise AssertionError(
        f"Open circuit unexpectedly invoked specialist task {task.task_id}."
    )


# --- Building Smoke Configuration
def build_resilience_smoke_runtime(
    scenario: SupervisorResilienceScenario | str,
) -> SupervisorRuntimeConfig:
    """
    Build isolated runtime overrides for one allowlisted Airflow smoke scenario.

    Args:
        scenario: Controlled resilience scenario name or enum.

    Returns:
        Supervisor runtime with only the selected smoke dependency overridden.
    """
    resolved = SupervisorResilienceScenario(scenario)

    if resolved == SupervisorResilienceScenario.TRANSIENT_ONCE:
        return SupervisorRuntimeConfig(
            metadata_lineage_runner=TransientOnceMetadataRunner(),
            circuit_snapshot_loader=force_closed_circuit_snapshot,
        )

    if resolved == SupervisorResilienceScenario.HARD_TIMEOUT:
        return SupervisorRuntimeConfig(
            metadata_lineage_runner=run_timeout_smoke_specialist,
            circuit_snapshot_loader=force_closed_circuit_snapshot,
            specialist_timeout_cap_seconds=1,
        )

    if resolved == SupervisorResilienceScenario.CIRCUIT_OPEN:
        return SupervisorRuntimeConfig(
            metadata_lineage_runner=fail_if_circuit_invokes_specialist,
            circuit_snapshot_loader=force_open_circuit_snapshot,
        )

    if resolved == SupervisorResilienceScenario.PARTIAL_RESULT:
        return SupervisorRuntimeConfig(
            metadata_lineage_runner=run_partial_smoke_specialist,
            circuit_snapshot_loader=force_closed_circuit_snapshot,
        )

    return SupervisorRuntimeConfig(
        metadata_lineage_runner=run_terminal_failure_smoke_specialist,
        circuit_snapshot_loader=force_closed_circuit_snapshot,
    )


def build_resilience_smoke_request(
    scenario: SupervisorResilienceScenario | str,
) -> SupervisorRequest:
    """
    Build the deterministic read-only request used by resilience smoke scenarios.

    Args:
        scenario: Controlled resilience scenario name or enum.

    Returns:
        Bounded metadata request with scenario-appropriate retry budget.
    """
    resolved = SupervisorResilienceScenario(scenario)

    return SupervisorRequest(
        intent="asset_context",
        qualified_name="dq.raw_orders",
        requester="airflow_resilience_smoke",
        max_handoffs=1,
        max_retries=(1 if resolved == SupervisorResilienceScenario.TRANSIENT_ONCE else 0),
        max_model_calls=0,
        token_budget=0,
        estimated_cost_budget_usd=0.0,
        latency_budget_ms=120_000,
    )


def expected_smoke_status(
    scenario: SupervisorResilienceScenario | str,
) -> AgentTaskStatus:
    """
    Return the exact parent status required for a smoke scenario to pass.

    Args:
        scenario: Controlled resilience scenario name or enum.

    Returns:
        Expected terminal supervisor status.
    """
    resolved = SupervisorResilienceScenario(scenario)

    if resolved in {
        SupervisorResilienceScenario.HARD_TIMEOUT,
        SupervisorResilienceScenario.CIRCUIT_OPEN,
        SupervisorResilienceScenario.TERMINAL_FAILURE,
    }:
        return AgentTaskStatus.BLOCKED

    if resolved == SupervisorResilienceScenario.PARTIAL_RESULT:
        return AgentTaskStatus.PARTIAL

    return AgentTaskStatus.SUCCESS


def supported_resilience_scenarios() -> tuple[str, ...]:
    """
    Return the stable scenario allowlist used by CLI and Airflow validation.

    Returns:
        Tuple of supported smoke scenario values.
    """
    return SINGLE_HANDOFF_RESILIENCE_SCENARIOS
