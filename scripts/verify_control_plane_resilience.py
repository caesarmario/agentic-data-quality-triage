####
## Control Plane Resilience Audit Verifier for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Verify timeout, retry, circuit, and partial-result evidence from one Airflow run."""

# --- Importing Libraries
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence


# --- Resolving Project Imports
PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.context.models import RunContextPhase
from agent.supervisor.fanout_resilience import supported_fanout_resilience_scenarios
from agent.supervisor.runtime import SUPERVISOR_TOOL_NAME, derive_supervisor_parent_run_id
from agent.supervisor.smoke import SupervisorResilienceScenario, supported_resilience_scenarios
from pipelines.common.clickhouse import build_clickhouse_client
from pipelines.common.logging import logger
from scripts.verify_control_plane_supervisor import (
    FORBIDDEN_PARENT_ACTIONS,
    build_incident_memory_sql,
    build_parent_audit_sql,
    build_run_context_sql,
    parse_json_object,
    query_audit_rows,
)


# --- Defining Constants
COMMON_REQUIRED_ACTIONS = {
    "supervisor_run_started",
    "supervisor_intent_classified",
    "supervisor_budget_prechecked",
    "supervisor_circuit_checked",
    "supervisor_final_decision",
}

HANDOFF_ACTIONS = {
    "supervisor_route_selected",
    "supervisor_handoff_started",
    "supervisor_handoff_completed",
    "supervisor_handoff_failed",
    "supervisor_handoff_rejected",
    "supervisor_specialist_attempt_started",
    "supervisor_specialist_attempt_completed",
    "supervisor_specialist_attempt_failed",
    "supervisor_specialist_attempt_timed_out",
    "supervisor_specialist_retry_scheduled",
    "supervisor_specialist_outcome",
}

EXPECTED_PHASES = {
    SupervisorResilienceScenario.TRANSIENT_ONCE: (
        RunContextPhase.STARTED.value,
        RunContextPhase.ROUTED.value,
        RunContextPhase.COMPLETED.value,
    ),
    SupervisorResilienceScenario.HARD_TIMEOUT: (
        RunContextPhase.STARTED.value,
        RunContextPhase.ROUTED.value,
        RunContextPhase.BLOCKED.value,
    ),
    SupervisorResilienceScenario.CIRCUIT_OPEN: (
        RunContextPhase.STARTED.value,
        RunContextPhase.BLOCKED.value,
    ),
    SupervisorResilienceScenario.PARTIAL_RESULT: (
        RunContextPhase.STARTED.value,
        RunContextPhase.ROUTED.value,
        RunContextPhase.COMPLETED.value,
    ),
    SupervisorResilienceScenario.TERMINAL_FAILURE: (
        RunContextPhase.STARTED.value,
        RunContextPhase.ROUTED.value,
        RunContextPhase.BLOCKED.value,
    ),
}

ALLOWED_PARENT_STATUSES = {
    "blocked",
    "failed",
    "partial",
    "running",
    "success",
    "timed_out",
}


# --- Defining CLI Helpers
def supported_control_plane_resilience_scenarios() -> tuple[str, ...]:
    """
    Return all single-handoff and fan-out scenarios accepted by DAG 99.

    Returns:
        Stable de-duplicated resilience scenario values.
    """
    return tuple(
        dict.fromkeys(
            (
                *supported_resilience_scenarios(),
                *supported_fanout_resilience_scenarios(),
            )
        )
    )


def build_parser() -> argparse.ArgumentParser:
    """
    Build the resilience verifier CLI parser.

    Returns:
        Configured argparse parser.
    """
    parser = argparse.ArgumentParser(
        description="Verify one Control Plane resilience smoke audit trail."
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--scenario",
        required=True,
        choices=supported_control_plane_resilience_scenarios(),
    )
    parser.add_argument("--clickhouse-host", default=None)
    parser.add_argument("--clickhouse-port", type=int, default=None)

    return parser


# --- Defining Audit Helpers
def action_rows(rows: list[dict[str, Any]], action: str) -> list[dict[str, Any]]:
    """
    Select audit rows for one stable action.

    Args:
        rows: Parent-correlated audit rows.
        action: Exact action name.

    Returns:
        Matching rows in audit order.
    """
    return [row for row in rows if str(row.get("action", "")) == action]


def require_action_count(
    rows: list[dict[str, Any]],
    action: str,
    expected_count: int,
) -> list[dict[str, Any]]:
    """
    Enforce an exact audit action count.

    Args:
        rows: Parent-correlated audit rows.
        action: Exact action name.
        expected_count: Required number of rows.

    Returns:
        Matching rows when the count is exact.

    Raises:
        RuntimeError: If the action count differs.
    """
    matched = action_rows(rows, action)

    if len(matched) != expected_count:
        raise RuntimeError(
            f"Expected {expected_count} {action} event(s), found {len(matched)}."
        )

    return matched


def parse_required_json_object(value: Any, field_name: str) -> dict[str, Any]:
    """
    Parse one required audit JSON object without silently accepting corruption.

    Args:
        value: ClickHouse-normalized JSON field value.
        field_name: Audit field name used in validation errors.

    Returns:
        Parsed JSON object.

    Raises:
        RuntimeError: If the field is malformed or does not contain an object.
    """
    if isinstance(value, dict):
        return value

    try:
        parsed = json.loads(str(value))

    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Audit {field_name} contains malformed JSON.") from exc

    if not isinstance(parsed, dict):
        raise RuntimeError(f"Audit {field_name} must contain a JSON object.")

    return parsed


def verify_parent_audit_envelopes(
    rows: list[dict[str, Any]],
    parent_run_id: str,
    scenario: SupervisorResilienceScenario,
) -> dict[str, Any]:
    """
    Verify correlation, actor, task, route, SQL-hash, and approval audit fields.

    Args:
        rows: Parent-correlated audit rows including child specialist events.
        parent_run_id: Expected stable supervisor UUID.
        scenario: Controlled resilience scenario.

    Returns:
        Compact completeness evidence for parent-owned audit events.

    Raises:
        RuntimeError: If any parent event is malformed, mis-correlated, or incomplete.
    """
    parent_rows = [
        row
        for row in rows
        if str(row.get("tool_name", "")) == SUPERVISOR_TOOL_NAME
    ]

    if not parent_rows:
        raise RuntimeError("No parent-owned supervisor audit events were retained.")

    audit_ids = [str(row.get("audit_id", "")) for row in parent_rows]

    if any(not audit_id for audit_id in audit_ids) or len(set(audit_ids)) != len(audit_ids):
        raise RuntimeError("Parent audit identifiers must be present and unique.")

    expected_retry_budget = (
        1
        if scenario == SupervisorResilienceScenario.TRANSIENT_ONCE
        else 0
    )
    task_ids: set[str]        = set()
    selected_agents: set[str] = set()
    task_types: set[str]       = set()
    model_routes: set[str]     = set()

    for row in parent_rows:
        if str(row.get("agent_run_id", "")) != parent_run_id:
            raise RuntimeError("Parent audit event has an incorrect agent_run_id.")

        if str(row.get("actor", "")) != "airflow_resilience_smoke":
            raise RuntimeError("Parent audit event has an unexpected actor.")

        if str(row.get("status", "")) not in ALLOWED_PARENT_STATUSES:
            raise RuntimeError("Parent audit event has an unsupported status.")

        input_payload  = parse_required_json_object(row.get("input_json"), "input_json")
        output_payload = parse_required_json_object(row.get("output_json"), "output_json")

        if input_payload.get("requested_intent") != "asset_context":
            raise RuntimeError("Parent audit request intent is inconsistent.")

        if input_payload.get("qualified_name") != "dq.raw_orders":
            raise RuntimeError("Parent audit asset scope is inconsistent.")

        if int(input_payload.get("max_handoffs", -1)) != 1:
            raise RuntimeError("Parent audit did not retain the one-handoff limit.")

        if int(input_payload.get("max_retries", -1)) != expected_retry_budget:
            raise RuntimeError("Parent audit retry budget is inconsistent.")

        if any(
            (
                bool(input_payload.get("has_sql_proposal")),
                bool(input_payload.get("proposal_sql_hash")),
                bool(row.get("sql_hash")),
            )
        ):
            raise RuntimeError("Metadata resilience audit unexpectedly retained a SQL proposal.")

        if str(row.get("alert_key", "")):
            raise RuntimeError("Metadata resilience audit unexpectedly retained an alert key.")

        task_id = str(output_payload.get("task_id", ""))

        if task_id:
            task_ids.add(task_id)
            selected_agents.add(str(output_payload.get("selected_specialist", "")))
            task_types.add(str(output_payload.get("task_type", "")))
            model_routes.add(str(output_payload.get("model_route", "")))

            allowed_tools = output_payload.get("allowed_tools")

            if not isinstance(allowed_tools, list) or not allowed_tools:
                raise RuntimeError("Parent task audit is missing its bounded tool allowlist.")

    if len(task_ids) != 1:
        raise RuntimeError("One resilience run must retain exactly one correlated task_id.")

    if selected_agents != {"metadata_lineage_agent"}:
        raise RuntimeError("Parent task audit retained an unexpected specialist.")

    if task_types != {"asset_context"}:
        raise RuntimeError("Parent task audit retained an unexpected task type.")

    if model_routes != {"no_llm_fallback"}:
        raise RuntimeError("Metadata resilience task did not retain no_llm_fallback routing.")

    final_row     = require_action_count(rows, "supervisor_final_decision", 1)[0]
    final_payload = parse_required_json_object(final_row.get("output_json"), "output_json")

    if final_payload.get("approval_state") != "not_required":
        raise RuntimeError("Final decision is missing its explicit not_required approval state.")

    if not isinstance(final_payload.get("resilience"), dict):
        raise RuntimeError("Final decision is missing its terminal resilience summary.")

    return {
        "parent_event_count": len(parent_rows),
        "unique_audit_id_count": len(audit_ids),
        "task_id": next(iter(task_ids)),
        "model_route": next(iter(model_routes)),
        "approval_state": final_payload["approval_state"],
    }


def verify_circuit_decision(
    rows: list[dict[str, Any]],
    scenario: SupervisorResilienceScenario,
) -> dict[str, Any]:
    """
    Verify one explicit and scenario-appropriate circuit check.

    Args:
        rows: Parent-correlated audit rows.
        scenario: Controlled resilience scenario.

    Returns:
        Serialized circuit snapshot.
    """
    row        = require_action_count(rows, "supervisor_circuit_checked", 1)[0]
    payload    = parse_json_object(row.get("output_json"))
    resilience = payload.get("resilience", {})
    circuit    = resilience.get("circuit", {}) if isinstance(resilience, dict) else {}
    expected_state = (
        "open"
        if scenario == SupervisorResilienceScenario.CIRCUIT_OPEN
        else "closed"
    )

    if circuit.get("state") != expected_state:
        raise RuntimeError("Circuit audit retained an unexpected state.")

    expected_allowed = scenario != SupervisorResilienceScenario.CIRCUIT_OPEN

    if circuit.get("request_allowed") is not expected_allowed:
        raise RuntimeError("Circuit audit retained an incorrect request_allowed decision.")

    return circuit


def verify_context_phases(
    rows: list[dict[str, Any]],
    scenario: SupervisorResilienceScenario,
) -> list[str]:
    """
    Verify exact run-context lifecycle phases for one failure scenario.

    Args:
        rows: Context rows ordered by event time.
        scenario: Controlled resilience scenario.

    Returns:
        Ordered verified phase values.

    Raises:
        RuntimeError: If phases or terminal status differ from policy.
    """
    phases   = [str(row.get("phase", "")) for row in rows]
    expected = list(EXPECTED_PHASES[scenario])

    if phases != expected:
        raise RuntimeError(f"Unexpected context phases: expected={expected} actual={phases}")

    expected_terminal_status = {
        SupervisorResilienceScenario.TRANSIENT_ONCE: "success",
        SupervisorResilienceScenario.HARD_TIMEOUT: "blocked",
        SupervisorResilienceScenario.CIRCUIT_OPEN: "blocked",
        SupervisorResilienceScenario.PARTIAL_RESULT: "partial",
        SupervisorResilienceScenario.TERMINAL_FAILURE: "blocked",
    }[scenario]

    if str(rows[-1].get("status", "")) != expected_terminal_status:
        raise RuntimeError("Run-context terminal status does not match the scenario.")

    return phases


def verify_scenario_actions(
    rows: list[dict[str, Any]],
    scenario: SupervisorResilienceScenario,
) -> dict[str, Any]:
    """
    Enforce exact retry, timeout, circuit, and partial-result action evidence.

    Args:
        rows: Parent-correlated audit rows.
        scenario: Controlled resilience scenario.

    Returns:
        Compact attempt and outcome evidence.
    """
    actions = {str(row.get("action", "")) for row in rows}
    missing = sorted(COMMON_REQUIRED_ACTIONS - actions)

    if missing:
        raise RuntimeError("Resilience audit is missing actions: " + ", ".join(missing))

    if actions.intersection(FORBIDDEN_PARENT_ACTIONS):
        raise RuntimeError("Resilience audit contains a forbidden mutation action.")

    expected_counts = {
        SupervisorResilienceScenario.TRANSIENT_ONCE: {
            "supervisor_route_selected": 1,
            "supervisor_handoff_started": 1,
            "supervisor_handoff_completed": 1,
            "supervisor_handoff_failed": 0,
            "supervisor_specialist_attempt_started": 2,
            "supervisor_specialist_attempt_failed": 1,
            "supervisor_specialist_retry_scheduled": 1,
            "supervisor_specialist_attempt_completed": 1,
            "supervisor_specialist_attempt_timed_out": 0,
            "supervisor_specialist_outcome": 1,
            "supervisor_budget_reconciled": 1,
            "supervisor_circuit_opened": 0,
            "supervisor_handoff_rejected": 0,
        },
        SupervisorResilienceScenario.HARD_TIMEOUT: {
            "supervisor_route_selected": 1,
            "supervisor_handoff_started": 1,
            "supervisor_handoff_completed": 0,
            "supervisor_handoff_failed": 1,
            "supervisor_specialist_attempt_started": 1,
            "supervisor_specialist_attempt_failed": 0,
            "supervisor_specialist_retry_scheduled": 0,
            "supervisor_specialist_attempt_completed": 0,
            "supervisor_specialist_attempt_timed_out": 1,
            "supervisor_specialist_outcome": 1,
            "supervisor_budget_reconciled": 0,
            "supervisor_circuit_opened": 0,
            "supervisor_handoff_rejected": 0,
        },
        SupervisorResilienceScenario.CIRCUIT_OPEN: {
            "supervisor_route_selected": 0,
            "supervisor_handoff_started": 0,
            "supervisor_handoff_completed": 0,
            "supervisor_handoff_failed": 0,
            "supervisor_specialist_attempt_started": 0,
            "supervisor_specialist_attempt_failed": 0,
            "supervisor_specialist_retry_scheduled": 0,
            "supervisor_specialist_attempt_completed": 0,
            "supervisor_specialist_attempt_timed_out": 0,
            "supervisor_specialist_outcome": 0,
            "supervisor_budget_reconciled": 0,
            "supervisor_circuit_opened": 1,
            "supervisor_handoff_rejected": 1,
        },
        SupervisorResilienceScenario.PARTIAL_RESULT: {
            "supervisor_route_selected": 1,
            "supervisor_handoff_started": 1,
            "supervisor_handoff_completed": 1,
            "supervisor_handoff_failed": 0,
            "supervisor_specialist_attempt_started": 1,
            "supervisor_specialist_attempt_failed": 0,
            "supervisor_specialist_retry_scheduled": 0,
            "supervisor_specialist_attempt_completed": 1,
            "supervisor_specialist_attempt_timed_out": 0,
            "supervisor_specialist_outcome": 1,
            "supervisor_budget_reconciled": 1,
            "supervisor_circuit_opened": 0,
            "supervisor_handoff_rejected": 0,
        },
        SupervisorResilienceScenario.TERMINAL_FAILURE: {
            "supervisor_route_selected": 1,
            "supervisor_handoff_started": 1,
            "supervisor_handoff_completed": 0,
            "supervisor_handoff_failed": 1,
            "supervisor_specialist_attempt_started": 1,
            "supervisor_specialist_attempt_failed": 1,
            "supervisor_specialist_retry_scheduled": 0,
            "supervisor_specialist_attempt_completed": 0,
            "supervisor_specialist_attempt_timed_out": 0,
            "supervisor_specialist_outcome": 1,
            "supervisor_budget_reconciled": 0,
            "supervisor_circuit_opened": 0,
            "supervisor_handoff_rejected": 0,
        },
    }[scenario]

    for action, count in expected_counts.items():
        require_action_count(rows, action, count)

    final_row    = require_action_count(rows, "supervisor_final_decision", 1)[0]
    final_status = str(final_row.get("status", ""))
    expected_final = {
        SupervisorResilienceScenario.TRANSIENT_ONCE: "success",
        SupervisorResilienceScenario.HARD_TIMEOUT: "blocked",
        SupervisorResilienceScenario.CIRCUIT_OPEN: "blocked",
        SupervisorResilienceScenario.PARTIAL_RESULT: "partial",
        SupervisorResilienceScenario.TERMINAL_FAILURE: "blocked",
    }[scenario]

    if final_status != expected_final:
        raise RuntimeError("Supervisor final decision does not match the scenario.")

    outcome_rows   = action_rows(rows, "supervisor_specialist_outcome")
    outcome_status = str(outcome_rows[0].get("status", "")) if outcome_rows else ""
    expected_outcome_status = {
        SupervisorResilienceScenario.TRANSIENT_ONCE: "success",
        SupervisorResilienceScenario.HARD_TIMEOUT: "timed_out",
        SupervisorResilienceScenario.CIRCUIT_OPEN: "",
        SupervisorResilienceScenario.PARTIAL_RESULT: "partial",
        SupervisorResilienceScenario.TERMINAL_FAILURE: "failed",
    }[scenario]

    if outcome_status != expected_outcome_status:
        raise RuntimeError("Specialist outcome status does not match the scenario.")

    outcome_resilience: dict[str, Any] = {}

    if outcome_rows:
        outcome_payload = parse_json_object(outcome_rows[0].get("output_json"))
        raw_resilience  = outcome_payload.get("resilience")

        if not isinstance(raw_resilience, dict):
            raise RuntimeError("Specialist outcome is missing resilience evidence.")

        outcome_resilience = raw_resilience

        if int(raw_resilience.get("attempt_count", -1)) != expected_counts[
            "supervisor_specialist_attempt_started"
        ]:
            raise RuntimeError("Specialist outcome retained an incorrect attempt count.")

        if int(raw_resilience.get("retry_count", -1)) != expected_counts[
            "supervisor_specialist_retry_scheduled"
        ]:
            raise RuntimeError("Specialist outcome retained an incorrect retry count.")

    timeout_rows = action_rows(rows, "supervisor_specialist_attempt_timed_out")

    if timeout_rows:
        timeout_payload    = parse_json_object(timeout_rows[0].get("output_json"))
        timeout_resilience = timeout_payload.get("resilience")

        if not isinstance(timeout_resilience, dict):
            raise RuntimeError("Timeout event is missing resilience evidence.")

        if timeout_resilience.get("retry_scheduled") is not False:
            raise RuntimeError("An exhausted hard deadline cannot schedule a retry.")

    expected_attempt_numbers = list(
        range(1, expected_counts["supervisor_specialist_attempt_started"] + 1)
    )
    started_attempt_numbers = [
        int(
            parse_required_json_object(row.get("output_json"), "output_json")
            .get("resilience", {})
            .get("attempt_number", -1)
        )
        for row in action_rows(rows, "supervisor_specialist_attempt_started")
    ]

    if sorted(started_attempt_numbers) != expected_attempt_numbers:
        raise RuntimeError("Specialist attempt-start audit numbering is incomplete.")

    expected_failed_attempts = {
        SupervisorResilienceScenario.TRANSIENT_ONCE: [1],
        SupervisorResilienceScenario.HARD_TIMEOUT: [],
        SupervisorResilienceScenario.CIRCUIT_OPEN: [],
        SupervisorResilienceScenario.PARTIAL_RESULT: [],
        SupervisorResilienceScenario.TERMINAL_FAILURE: [1],
    }[scenario]
    failed_attempt_numbers: list[int] = []

    for row in action_rows(rows, "supervisor_specialist_attempt_failed"):
        payload    = parse_required_json_object(row.get("output_json"), "output_json")
        resilience = payload.get("resilience")

        if not isinstance(resilience, dict):
            raise RuntimeError("Failed attempt is missing resilience evidence.")

        failed_attempt_numbers.append(int(resilience.get("attempt_number", -1)))

        expected_failure_category = (
            "transient_failure"
            if scenario == SupervisorResilienceScenario.TRANSIENT_ONCE
            else "specialist_failed"
        )
        expected_retry_scheduled = (
            scenario == SupervisorResilienceScenario.TRANSIENT_ONCE
        )

        if resilience.get("failure_category") != expected_failure_category:
            raise RuntimeError("Failed attempt retained an incorrect failure category.")

        if resilience.get("retry_scheduled") is not expected_retry_scheduled:
            raise RuntimeError("Failed attempt retained an incorrect retry decision.")

        if not str(row.get("error_message", "")):
            raise RuntimeError("Failed attempt is missing its bounded error message.")

    if sorted(failed_attempt_numbers) != expected_failed_attempts:
        raise RuntimeError("Specialist failed-attempt audit numbering is incomplete.")

    retry_rows = action_rows(rows, "supervisor_specialist_retry_scheduled")

    for row in retry_rows:
        payload    = parse_required_json_object(row.get("output_json"), "output_json")
        resilience = payload.get("resilience")

        if not isinstance(resilience, dict):
            raise RuntimeError("Retry event is missing resilience evidence.")

        if resilience.get("retry_scheduled") is not True:
            raise RuntimeError("Retry event does not retain an approved retry decision.")

        if resilience.get("failure_category") != "transient_failure":
            raise RuntimeError("Retry event retained an incorrect failure category.")

    completed_rows = action_rows(rows, "supervisor_specialist_attempt_completed")
    expected_completed_status = {
        SupervisorResilienceScenario.TRANSIENT_ONCE: "success",
        SupervisorResilienceScenario.PARTIAL_RESULT: "partial",
    }.get(scenario)

    for row in completed_rows:
        if str(row.get("status", "")) != expected_completed_status:
            raise RuntimeError("Completed attempt retained an incorrect terminal status.")

    rejected_rows = action_rows(rows, "supervisor_handoff_rejected")

    for row in rejected_rows:
        payload    = parse_required_json_object(row.get("output_json"), "output_json")
        resilience = payload.get("resilience")

        if not isinstance(resilience, dict):
            raise RuntimeError("Rejected handoff is missing resilience evidence.")

        if resilience.get("failure_category") != "circuit_open":
            raise RuntimeError("Rejected handoff retained an incorrect policy category.")

        if int(resilience.get("attempt_count", -1)) != 0:
            raise RuntimeError("Rejected handoff must retain zero specialist attempts.")

        if not str(row.get("error_message", "")):
            raise RuntimeError("Rejected handoff is missing its bounded rejection reason.")

    handoff_started_rows = action_rows(rows, "supervisor_handoff_started")

    for row in handoff_started_rows:
        if str(row.get("status", "")) != "running":
            raise RuntimeError("Started handoff must retain running status.")

    handoff_completed_rows = action_rows(rows, "supervisor_handoff_completed")
    expected_handoff_completed_status = {
        SupervisorResilienceScenario.TRANSIENT_ONCE: "success",
        SupervisorResilienceScenario.PARTIAL_RESULT: "partial",
    }.get(scenario)

    for row in handoff_completed_rows:
        if str(row.get("status", "")) != expected_handoff_completed_status:
            raise RuntimeError("Completed handoff retained an incorrect status.")

    handoff_failed_rows = action_rows(rows, "supervisor_handoff_failed")
    expected_handoff_failed_status = (
        "timed_out"
        if scenario == SupervisorResilienceScenario.HARD_TIMEOUT
        else "failed"
    )

    for row in handoff_failed_rows:
        payload    = parse_required_json_object(row.get("output_json"), "output_json")
        resilience = payload.get("resilience")

        if str(row.get("status", "")) != expected_handoff_failed_status:
            raise RuntimeError("Failed handoff retained an incorrect status.")

        if not isinstance(resilience, dict):
            raise RuntimeError("Failed handoff is missing resilience evidence.")

        expected_category = (
            "hard_timeout"
            if scenario == SupervisorResilienceScenario.HARD_TIMEOUT
            else "specialist_failed"
        )

        if resilience.get("failure_category") != expected_category:
            raise RuntimeError("Failed handoff retained an incorrect failure category.")

        if not str(row.get("error_message", "")):
            raise RuntimeError("Failed handoff is missing its bounded error message.")

    final_payload    = parse_required_json_object(final_row.get("output_json"), "output_json")
    final_resilience = final_payload.get("resilience")

    if not isinstance(final_resilience, dict):
        raise RuntimeError("Final decision is missing resilience evidence.")

    expected_failure_category = {
        SupervisorResilienceScenario.TRANSIENT_ONCE: "",
        SupervisorResilienceScenario.HARD_TIMEOUT: "hard_timeout",
        SupervisorResilienceScenario.CIRCUIT_OPEN: "circuit_open",
        SupervisorResilienceScenario.PARTIAL_RESULT: "",
        SupervisorResilienceScenario.TERMINAL_FAILURE: "specialist_failed",
    }[scenario]

    if final_resilience.get("failure_category") != expected_failure_category:
        raise RuntimeError("Final decision retained an incorrect failure category.")

    if int(final_resilience.get("attempt_count", -1)) != expected_counts[
        "supervisor_specialist_attempt_started"
    ]:
        raise RuntimeError("Final decision retained an incorrect attempt count.")

    if int(final_resilience.get("retry_count", -1)) != expected_counts[
        "supervisor_specialist_retry_scheduled"
    ]:
        raise RuntimeError("Final decision retained an incorrect retry count.")

    if scenario in {
        SupervisorResilienceScenario.HARD_TIMEOUT,
        SupervisorResilienceScenario.CIRCUIT_OPEN,
        SupervisorResilienceScenario.TERMINAL_FAILURE,
    } and not str(final_row.get("error_message", "")):
        raise RuntimeError("Blocked final decision is missing its bounded failure reason.")

    return {
        "final_status": final_status,
        "outcome_status": outcome_status,
        "attempt_count": expected_counts["supervisor_specialist_attempt_started"],
        "retry_count": expected_counts["supervisor_specialist_retry_scheduled"],
        "outcome_resilience": outcome_resilience,
        "final_resilience": final_resilience,
        "rejected_handoff_count": len(rejected_rows),
        "completed_handoff_count": len(handoff_completed_rows),
        "failed_handoff_count": len(handoff_failed_rows),
    }


# --- Running Verification
def verify_control_plane_resilience(
    run_id: str,
    scenario: SupervisorResilienceScenario | str,
    clickhouse_host: str | None = None,
    clickhouse_port: int | None = None,
) -> dict[str, Any]:
    """
    Verify one scenario against ClickHouse audit and context evidence.

    Args:
        run_id: Source Airflow DagRun identifier.
        scenario: Controlled resilience scenario name or enum.
        clickhouse_host: Optional ClickHouse host override.
        clickhouse_port: Optional ClickHouse HTTP port override.

    Returns:
        Compact verified evidence printed to the Airflow task log.
    """
    resolved      = SupervisorResilienceScenario(scenario)
    parent_run_id = derive_supervisor_parent_run_id(run_id)
    client        = build_clickhouse_client(
        host=clickhouse_host,
        port=clickhouse_port,
    )
    audit_rows    = query_audit_rows(
        client=client,
        sql=build_parent_audit_sql(str(parent_run_id)),
    )

    if not audit_rows:
        raise RuntimeError("No parent audit rows found for resilience smoke run.")

    action_evidence = verify_scenario_actions(audit_rows, resolved)
    audit_contract  = verify_parent_audit_envelopes(
        rows=audit_rows,
        parent_run_id=str(parent_run_id),
        scenario=resolved,
    )
    circuit         = verify_circuit_decision(audit_rows, resolved)
    context_rows    = query_audit_rows(
        client=client,
        sql=build_run_context_sql(str(parent_run_id)),
    )
    phases          = verify_context_phases(context_rows, resolved)
    memory_rows     = query_audit_rows(
        client=client,
        sql=build_incident_memory_sql(str(parent_run_id)),
    )

    if memory_rows:
        raise RuntimeError("Metadata resilience smoke must not create durable incident memory.")

    summary = {
        "run_id": run_id,
        "parent_run_id": str(parent_run_id),
        "scenario": resolved.value,
        "status": "success",
        "audit_event_count": len(audit_rows),
        "context_phases": phases,
        "incident_memory_count": 0,
        "circuit": circuit,
        "audit_contract": audit_contract,
        **action_evidence,
    }

    print(json.dumps(summary, indent=2, sort_keys=True))
    logger.info(
        "Control Plane resilience audit verified | run_id=%s scenario=%s events=%d",
        run_id,
        resolved.value,
        len(audit_rows),
    )

    return summary


def main(argv: Sequence[str] | None = None) -> int:
    """
    Parse CLI arguments and verify one Airflow resilience smoke run.

    Args:
        argv: Optional explicit CLI argument sequence.

    Returns:
        Zero when all retained evidence passes.
    """
    args = build_parser().parse_args(argv)
    if args.scenario in supported_fanout_resilience_scenarios():
        from scripts.verify_control_plane_fanout_resilience import (
            verify_fanout_resilience_audit,
        )

        verify_fanout_resilience_audit(
            run_id=args.run_id,
            scenario=args.scenario,
            clickhouse_host=args.clickhouse_host,
            clickhouse_port=args.clickhouse_port,
        )

    else:
        verify_control_plane_resilience(
            run_id=args.run_id,
            scenario=args.scenario,
            clickhouse_host=args.clickhouse_host,
            clickhouse_port=args.clickhouse_port,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
