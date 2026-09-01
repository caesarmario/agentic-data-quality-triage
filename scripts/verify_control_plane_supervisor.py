####
## Control Plane Supervisor Audit Verifier for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Verify parent decisions, specialist handoff, budgets, and child evidence in ClickHouse."""

# --- Importing Libraries
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.state import parse_json_object
from agent.context.models import find_forbidden_persisted_context_key
from agent.context.store import (
    INCIDENT_MEMORY_TABLE,
    RUN_CONTEXT_TABLE,
    clickhouse_text,
)
from agent.specialists.contracts import find_forbidden_context_key
from agent.supervisor.models import SupervisorIntent
from agent.supervisor.runtime import derive_supervisor_parent_run_id
from agent.tools.clickhouse_sql import rows_to_dicts
from pipelines.common.clickhouse import build_clickhouse_client, quote_sql_literal
from pipelines.common.logging import logger
from pipelines.schema_drift.detector import validate_schema_run_id


# --- Defining Constants
BASE_REQUIRED_ACTIONS = {
    "supervisor_run_started",
    "supervisor_intent_classified",
    "supervisor_budget_prechecked",
    "supervisor_circuit_checked",
    "supervisor_route_selected",
    "supervisor_handoff_started",
    "supervisor_specialist_attempt_started",
    "supervisor_specialist_attempt_completed",
    "supervisor_specialist_outcome",
    "supervisor_handoff_completed",
    "specialist_handoff_started",
    "specialist_handoff_completed",
    "supervisor_budget_reconciled",
    "supervisor_final_decision",
}

BUDGET_FIELDS = (
    "handoffs",
    "retries",
    "model_calls",
    "tokens",
    "estimated_cost_usd",
    "latency_ms",
)

TRIAGE_CHILD_CORE_SUCCESS_ACTIONS = {
    "fetch_incident_history",
    "store_triage_report",
    "mark_alert_triaged",
    "triage_completed",
}

TRIAGE_NONFATAL_FAILURE_ACTIONS = {
    "run_guarded_sql",
    "fetch_dq_history",
    "fetch_pipeline_runs",
    "fetch_dbt_lineage",
    "fetch_schema_drift_context",
}

TRIAGE_TERMINAL_STATUSES = {"success", "partial"}

EXPECTED_ROUTE_BY_INTENT = {
    SupervisorIntent.TRIAGE_ALERT.value: ("incident_triage_agent", "triage_alert"),
    SupervisorIntent.ASSET_CONTEXT.value: ("metadata_lineage_agent", "asset_context"),
    SupervisorIntent.BLAST_RADIUS.value: ("metadata_lineage_agent", "blast_radius"),
    SupervisorIntent.TRUSTED_ASSET_SEARCH.value: (
        "metadata_lineage_agent",
        "trusted_asset_search",
    ),
    SupervisorIntent.REVIEW_SQL.value: (
        "sql_safety_review_agent",
        "review_sql",
    ),
    SupervisorIntent.SCHEMA_DRIFT_ASSESSMENT.value: (
        "schema_drift_agent",
        "assess_schema_drift",
    ),
}

METADATA_TOOL_ACTIONS = {
    SupervisorIntent.ASSET_CONTEXT.value: {
        "get_metadata_asset",
        "fetch_dbt_lineage",
        "fetch_dbt_blast_radius",
    },
    SupervisorIntent.BLAST_RADIUS.value: {
        "get_metadata_asset",
        "fetch_dbt_blast_radius",
    },
    SupervisorIntent.TRUSTED_ASSET_SEARCH.value: {
        "search_metadata_assets",
    },
    SupervisorIntent.REVIEW_SQL.value: {
        "review_sql_policy",
    },
    SupervisorIntent.SCHEMA_DRIFT_ASSESSMENT.value: {
        "fetch_schema_drift_run_context",
        "get_metadata_asset",
        "fetch_dbt_blast_radius",
        "assess_schema_drift_policy",
    },
}

FORBIDDEN_PARENT_ACTIONS = {
    "execute_backfill",
    "execute_remediation",
    "run_clickhouse_mutation",
    "run_guarded_sql",
    "execute_sql_proposal",
    "alter_schema",
    "execute_schema_migration",
    "run_schema_mutation",
}

SUPPORTED_SCHEMA_ASSESSMENTS = {
    "",
    "compatible",
    "review_required",
    "breaking_change",
}

SUPPORTED_SCHEMA_IMPACT_LEVELS = {
    "none",
    "low",
    "medium",
    "high",
    "critical",
}

EXPECTED_RUN_CONTEXT_PHASES = ("started", "routed", "completed")

MODEL_ROUTE_ORDER = {
    "no_llm_fallback": 0,
    "quickthinkllm": 1,
    "deepthinkllm": 2,
}

ELEVATED_RISK_TIERS = {"high", "critical"}

# --- Defining Audit Query Helpers
def build_parent_audit_sql(parent_run_id: str) -> str:
    """
    Build an exact bounded parent-run audit query.

    Args:
        parent_run_id: Stable supervisor parent UUID.

    Returns:
        Read-only ClickHouse query with a hard LIMIT.
    """
    run_literal = quote_sql_literal(parent_run_id)

    return f"""
        SELECT
            audit_id,
            ts,
            agent_run_id,
            actor,
            alert_key,
            action,
            tool_name,
            status,
            input_json,
            output_json,
            error_message,
            sql_hash,
            row_count,
            duration_ms,
            report_s3_uri
        FROM dq.agent_audit_log
        WHERE agent_run_id = toUUID({run_literal})
        ORDER BY ts ASC
        LIMIT 250
    """


def build_child_triage_audit_sql(child_agent_run_id: str) -> str:
    """
    Build an exact bounded child triage audit query.

    Args:
        child_agent_run_id: Existing triage graph run UUID.

    Returns:
        Read-only ClickHouse query with a hard LIMIT.
    """
    run_literal = quote_sql_literal(child_agent_run_id)

    return f"""
        SELECT
            ts,
            action,
            tool_name,
            status,
            input_json,
            output_json,
            error_message,
            sql_hash,
            row_count,
            report_s3_uri
        FROM dq.agent_audit_log
        WHERE agent_run_id = toUUID({run_literal})
        ORDER BY ts ASC
        LIMIT 250
    """


def build_run_context_sql(parent_run_id: str) -> str:
    """
    Build an exact bounded query for persisted run-scoped context events.

    Args:
        parent_run_id: Stable supervisor parent UUID.

    Returns:
        Read-only ClickHouse query ordered by lifecycle sequence.
    """
    run_literal = quote_sql_literal(parent_run_id)

    return f"""
        SELECT
            context_event_id,
            parent_run_id,
            external_run_id,
            event_sequence,
            phase,
            occurred_at,
            expires_at,
            status,
            selected_specialist,
            task_type,
            task_id,
            alert_id,
            alert_key,
            alert_display_id,
            context_references_json,
            evidence_references_json,
            decision_json,
            report_s3_uri,
            approval_state,
            content_sha256
        FROM {RUN_CONTEXT_TABLE} FINAL
        WHERE parent_run_id = toUUID({run_literal})
        ORDER BY event_sequence ASC, occurred_at ASC
        LIMIT 20
    """


def build_incident_memory_sql(parent_run_id: str) -> str:
    """
    Build an exact bounded query for durable memory created by one supervisor run.

    Args:
        parent_run_id: Stable supervisor parent UUID.

    Returns:
        Read-only ClickHouse query for idempotently persisted incident outcomes.
    """
    run_literal = quote_sql_literal(parent_run_id)

    return f"""
        SELECT
            memory_id,
            memory_key,
            parent_run_id,
            recorded_at,
            memory_type,
            alert_id,
            alert_key,
            alert_display_id,
            outcome_status,
            specialist_name,
            task_type,
            summary,
            evidence_references_json,
            decision_json,
            report_s3_uri,
            approval_state,
            resolution_reference,
            content_sha256
        FROM {INCIDENT_MEMORY_TABLE} FINAL
        WHERE parent_run_id = toUUID({run_literal})
        ORDER BY recorded_at ASC
        LIMIT 20
    """


def parse_json_value(value: Any, field_name: str) -> Any:
    """
    Parse one persisted JSON field with explicit malformed-data reporting.

    Args:
        value: Raw JSON text from ClickHouse.
        field_name: Field name used in verification errors.

    Returns:
        Parsed JSON value.

    Raises:
        RuntimeError: If the persisted value is malformed JSON.
    """
    try:
        return json.loads(clickhouse_text(value))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Persisted context contains malformed {field_name} JSON.") from exc


def query_audit_rows(client: Any, sql: str) -> list[dict[str, Any]]:
    """
    Execute one fixed audit query and normalize result rows.

    Args:
        client: clickhouse-connect compatible client.
        sql: Fixed audit SQL from this verifier.

    Returns:
        JSON-like audit row dictionaries.
    """
    result = client.query(sql)

    return rows_to_dicts(
        columns=list(result.column_names or []),
        rows=result.result_rows,
    )


def verify_budget_evidence(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Verify admission and reconciliation decisions retained by the parent run.

    Args:
        rows: Exact parent-run audit rows ordered by timestamp.

    Returns:
        Compact pre-handoff and post-handoff budget evidence.

    Raises:
        RuntimeError: If a decision is missing, malformed, disallowed, or over budget.
    """
    expected_stages = {
        "supervisor_budget_prechecked": "pre_handoff",
        "supervisor_budget_reconciled": "post_handoff",
    }
    summary: dict[str, Any] = {}

    for action, expected_stage in expected_stages.items():
        decision_rows = [
            row
            for row in rows
            if str(row.get("action", "")) == action
        ]

        if len(decision_rows) != 1:
            raise RuntimeError(f"Supervisor must retain exactly one {action} event.")

        if str(decision_rows[0].get("status", "")).lower() != "success":
            raise RuntimeError(f"Supervisor budget action did not pass: {action}.")

        output_payload = parse_json_object(decision_rows[0].get("output_json"))
        decision       = output_payload.get("budget_decision")

        if not isinstance(decision, dict):
            raise RuntimeError(f"Supervisor {action} is missing budget_decision evidence.")

        if decision.get("stage") != expected_stage:
            raise RuntimeError(f"Supervisor {action} retained an unexpected budget stage.")

        if decision.get("allowed") is not True or decision.get("violations"):
            raise RuntimeError(f"Supervisor {action} did not retain an allowed decision.")

        limits    = decision.get("limits")
        usage     = decision.get("usage")
        remaining = decision.get("remaining")

        if not all(isinstance(item, dict) for item in (limits, usage, remaining)):
            raise RuntimeError(f"Supervisor {action} has malformed budget vectors.")

        for field_name in BUDGET_FIELDS:
            limit_value     = float(limits.get(field_name, -1))
            usage_value     = float(usage.get(field_name, -1))
            remaining_value = float(remaining.get(field_name, -1))

            if min(limit_value, usage_value, remaining_value) < 0:
                raise RuntimeError(f"Supervisor {action} has a negative budget value.")

            if usage_value > limit_value + 1e-8:
                raise RuntimeError(f"Supervisor {action} exceeded {field_name}.")

            expected_remaining = max(0.0, limit_value - usage_value)

            if abs(remaining_value - expected_remaining) > 1e-6:
                raise RuntimeError(
                    f"Supervisor {action} retained inconsistent remaining {field_name}."
                )

        summary[expected_stage] = {
            "limits": limits,
            "usage": usage,
            "remaining": remaining,
        }

    return summary


def verify_routing_policy_evidence(
    rows: list[dict[str, Any]],
    resolved_intent: str,
) -> dict[str, Any]:
    """
    Verify pre-handoff capability and post-evidence risk decisions from audit logs.

    Args:
        rows: Exact parent-run audit rows ordered by timestamp.
        resolved_intent: Deterministic supervisor intent retained by route audit.

    Returns:
        Compact pre-handoff and post-evidence routing-policy evidence.

    Raises:
        RuntimeError: If route, risk, strong-review, or approval invariants are missing.
    """
    expected_actions = {
        "supervisor_route_selected": "pre_handoff",
        "supervisor_final_decision": "post_evidence",
    }
    decisions: dict[str, dict[str, Any]] = {}

    for action, expected_stage in expected_actions.items():
        decision_rows = [
            row
            for row in rows
            if str(row.get("action", "")) == action
        ]

        if len(decision_rows) != 1:
            raise RuntimeError(f"Supervisor must retain exactly one {action} event.")

        output_payload = parse_json_object(decision_rows[0].get("output_json"))
        decision       = output_payload.get("routing_policy")

        if not isinstance(decision, dict):
            raise RuntimeError(f"Supervisor {action} is missing routing_policy evidence.")

        if str(decision.get("stage", "")) != expected_stage:
            raise RuntimeError(f"Supervisor {action} retained an unexpected policy stage.")

        authorized_route = str(decision.get("authorized_model_route", ""))
        actual_route     = str(decision.get("actual_model_route", ""))

        if authorized_route not in MODEL_ROUTE_ORDER or actual_route not in MODEL_ROUTE_ORDER:
            raise RuntimeError(f"Supervisor {action} retained an unknown model route.")

        if MODEL_ROUTE_ORDER[actual_route] > MODEL_ROUTE_ORDER[authorized_route]:
            raise RuntimeError(f"Supervisor {action} exceeded its authorized model capability.")

        reason_codes = decision.get("reason_codes")

        if not isinstance(reason_codes, list) or not reason_codes:
            raise RuntimeError(f"Supervisor {action} is missing policy reason codes.")

        decisions[expected_stage] = decision

    pre_decision  = decisions["pre_handoff"]
    post_decision = decisions["post_evidence"]
    expected_authorized_route = (
        "deepthinkllm"
        if resolved_intent == SupervisorIntent.TRIAGE_ALERT.value
        else "no_llm_fallback"
    )

    if pre_decision.get("authorized_model_route") != expected_authorized_route:
        raise RuntimeError("Supervisor pre-handoff policy retained the wrong capability ceiling.")

    if pre_decision.get("actual_model_route") != "no_llm_fallback":
        raise RuntimeError("Pre-handoff policy cannot claim model execution before the handoff.")

    if post_decision.get("authorized_model_route") != expected_authorized_route:
        raise RuntimeError("Supervisor post-evidence policy changed the capability ceiling.")

    final_rows    = [row for row in rows if str(row.get("action", "")) == "supervisor_final_decision"]
    final_payload = parse_json_object(final_rows[0].get("output_json"))

    if str(final_payload.get("actual_model_route", "")) != str(
        post_decision.get("actual_model_route", "")
    ):
        raise RuntimeError("Supervisor final audit lost the proven actual model route.")

    strong_satisfied = bool(post_decision.get("strong_review_satisfied", False))
    human_approval   = bool(post_decision.get("human_approval_required", False))
    disposition      = str(post_decision.get("disposition", ""))
    effective_risk   = str(post_decision.get("effective_risk_tier", ""))

    if strong_satisfied and post_decision.get("actual_model_route") != "deepthinkllm":
        raise RuntimeError("Strong review was claimed without proven deepthink execution.")

    if effective_risk in ELEVATED_RISK_TIERS:
        if not strong_satisfied and not human_approval and disposition != "block":
            raise RuntimeError(
                "High-risk supervisor output bypassed strong review and human approval."
            )

    if human_approval:
        if disposition != "human_review_required":
            raise RuntimeError("Human approval policy retained an unsafe disposition.")

        if str(final_payload.get("approval_state", "")) != "pending":
            raise RuntimeError("Human-review policy did not persist pending approval state.")

    if resolved_intent != SupervisorIntent.TRIAGE_ALERT.value:
        if post_decision.get("actual_model_route") != "no_llm_fallback":
            raise RuntimeError("Deterministic specialist unexpectedly reported an LLM route.")

        if post_decision.get("requested_provider_routes"):
            raise RuntimeError("Deterministic specialist unexpectedly requested a provider route.")

    return decisions


def verify_resilience_evidence(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Verify one allowed supervisor attempt and its terminal specialist outcome.

    Args:
        rows: Exact parent-run audit rows ordered by timestamp.

    Returns:
        Compact circuit, attempt, retry, and terminal-status evidence.

    Raises:
        RuntimeError: If execution contains missing, duplicate, or invalid evidence.
    """
    expected_counts = {
        "supervisor_circuit_checked": 1,
        "supervisor_specialist_attempt_started": 1,
        "supervisor_specialist_attempt_completed": 1,
        "supervisor_specialist_outcome": 1,
        "supervisor_specialist_attempt_failed": 0,
        "supervisor_specialist_attempt_timed_out": 0,
        "supervisor_specialist_retry_scheduled": 0,
        "supervisor_circuit_opened": 0,
    }
    rows_by_action = {
        action: [row for row in rows if str(row.get("action", "")) == action]
        for action in expected_counts
    }

    for action, expected_count in expected_counts.items():
        actual_count = len(rows_by_action[action])

        if actual_count != expected_count:
            raise RuntimeError(
                f"Supervisor expected {expected_count} {action} event(s), "
                f"found {actual_count}."
            )

    circuit_payload = parse_json_object(
        rows_by_action["supervisor_circuit_checked"][0].get("output_json")
    )
    circuit_resilience = circuit_payload.get("resilience")

    if not isinstance(circuit_resilience, dict):
        raise RuntimeError("Supervisor circuit check is missing resilience evidence.")

    circuit = circuit_resilience.get("circuit")

    if not isinstance(circuit, dict):
        raise RuntimeError("Supervisor circuit check is missing its circuit snapshot.")

    if circuit.get("state") not in {"closed", "half_open"}:
        raise RuntimeError("Normal supervisor execution retained a blocking circuit state.")

    if circuit.get("request_allowed") is not True:
        raise RuntimeError("Normal supervisor execution was not explicitly allowed by circuit policy.")

    outcome_row = rows_by_action["supervisor_specialist_outcome"][0]
    terminal_status = str(outcome_row.get("status", "")).lower()

    if terminal_status not in TRIAGE_TERMINAL_STATUSES:
        raise RuntimeError(
            "Supervisor specialist outcome must retain success or an explicit partial status."
        )

    outcome_payload    = parse_json_object(outcome_row.get("output_json"))
    outcome_resilience = outcome_payload.get("resilience")

    if not isinstance(outcome_resilience, dict):
        raise RuntimeError("Supervisor specialist outcome is missing resilience evidence.")

    if int(outcome_resilience.get("attempt_count", -1)) != 1:
        raise RuntimeError("Normal supervisor execution did not retain exactly one attempt.")

    if int(outcome_resilience.get("retry_count", -1)) != 0:
        raise RuntimeError("Normal supervisor execution retained an unexpected retry.")

    if str(outcome_resilience.get("failure_category", "")):
        raise RuntimeError("Normal supervisor execution retained an unexpected failure category.")

    return {
        "circuit": circuit,
        "attempt_count": 1,
        "retry_count": 0,
        "terminal_status": terminal_status,
    }


def verify_incident_history_read(rows: list[dict[str, Any]]) -> int:
    """
    Verify one bounded and sanitized incident-history read in child audit evidence.

    Args:
        rows: Exact child-agent audit rows ordered by timestamp.

    Returns:
        Number of prior investigation rows returned by the tool.

    Raises:
        RuntimeError: If the read is missing, duplicated, unbounded, or leaks raw context.
    """
    history_rows = [
        row
        for row in rows
        if str(row.get("action", "")) == "fetch_incident_history"
    ]

    if len(history_rows) != 1:
        raise RuntimeError(
            "Child incident audit must retain exactly one fetch_incident_history event."
        )

    row = history_rows[0]

    if str(row.get("status", "")).lower() != "success":
        raise RuntimeError("Incident history evidence read did not complete successfully.")

    if str(row.get("tool_name", "")) != "incident_history":
        raise RuntimeError("Incident history audit used an unexpected tool identity.")

    sql_hash = clickhouse_text(row.get("sql_hash"))

    if len(sql_hash) != 64 or any(character not in "0123456789abcdef" for character in sql_hash):
        raise RuntimeError("Incident history audit must retain a SHA-256 SQL hash.")

    input_payload  = parse_json_value(row.get("input_json"), "input_json")
    output_payload = parse_json_value(row.get("output_json"), "output_json")

    if not isinstance(input_payload, dict) or not isinstance(output_payload, dict):
        raise RuntimeError("Incident history audit payloads must be JSON objects.")

    lookback_days = int(input_payload.get("lookback_days", 0) or 0)
    limit         = int(input_payload.get("limit", 0) or 0)
    row_count     = int(row.get("row_count", 0) or 0)
    output_count  = int(output_payload.get("row_count", -1) or 0)

    if input_payload.get("identity_type") != "exact_alert_reference":
        raise RuntimeError("Incident history audit must use exact alert identity matching.")

    if not 1 <= lookback_days <= 365:
        raise RuntimeError("Incident history audit has an invalid lookback boundary.")

    if not 1 <= limit <= 100:
        raise RuntimeError("Incident history audit has an invalid row-limit boundary.")

    if row_count < 0 or row_count > limit or output_count != row_count:
        raise RuntimeError("Incident history audit row counts violate the hard result limit.")

    if "rows" in output_payload:
        raise RuntimeError("Incident history audit must not persist raw memory rows.")

    for field_name, payload in (
        ("input_json", input_payload),
        ("output_json", output_payload),
    ):
        forbidden_secret = find_forbidden_context_key(payload, path=field_name)
        forbidden_raw    = find_forbidden_persisted_context_key(
            payload,
            path=field_name,
        )

        if forbidden_secret or forbidden_raw:
            raise RuntimeError("Incident history audit contains forbidden raw or secret context.")

    return row_count


def verify_triage_terminal_evidence(
    completion_row: dict[str, Any],
    completion_payload: dict[str, Any],
    child_rows: list[dict[str, Any]],
    terminal_status: str,
    complexity_reason_codes: list[str],
    routing_policy_evidence: dict[str, dict[str, Any]],
) -> list[str]:
    """
    Verify success or a bounded partial result for one incident triage handoff.

    Args:
        completion_row: Parent-correlated specialist completion audit row.
        completion_payload: Parsed specialist completion output.
        child_rows: Exact child triage audit rows.
        terminal_status: Supervisor-verified success or partial status.
        complexity_reason_codes: Deterministic complexity facts from the report.
        routing_policy_evidence: Pre- and post-evidence routing decisions.

    Returns:
        Sanitized investigation errors retained by a valid partial result.

    Raises:
        RuntimeError: If status, core actions, optional failures, or approval facts conflict.
    """
    if terminal_status not in TRIAGE_TERMINAL_STATUSES:
        raise RuntimeError("Incident triage retained an unsupported terminal status.")

    if str(completion_row.get("status", "")).lower() != terminal_status:
        raise RuntimeError("Incident specialist completion status does not match the parent outcome.")

    if str(completion_payload.get("result_status", "")).lower() != terminal_status:
        raise RuntimeError("Incident specialist payload does not match the parent outcome.")

    raw_errors = completion_payload.get("investigation_errors")

    if not isinstance(raw_errors, list):
        raise RuntimeError("Incident specialist completion is missing investigation_errors.")

    investigation_errors = [
        " ".join(str(error).split())
        for error in raw_errors
        if str(error).strip()
    ]

    if len(investigation_errors) != len(set(investigation_errors)):
        raise RuntimeError("Incident specialist completion contains duplicate investigation errors.")

    if int(completion_payload.get("investigation_error_count", -1)) != len(
        investigation_errors
    ):
        raise RuntimeError("Incident specialist investigation error count is inconsistent.")

    completion_error = clickhouse_text(completion_row.get("error_message"))
    child_rows_by_action = {
        action: [row for row in child_rows if str(row.get("action", "")) == action]
        for action in TRIAGE_CHILD_CORE_SUCCESS_ACTIONS
    }

    for action, rows in child_rows_by_action.items():
        if len(rows) != 1:
            raise RuntimeError(
                f"Child incident audit must retain exactly one successful {action} event."
            )

        if str(rows[0].get("status", "")).lower() != "success":
            raise RuntimeError(f"Child incident core action did not succeed: {action}.")

    child_failures = [
        row
        for row in child_rows
        if str(row.get("status", "")).lower() in {"failed", "blocked"}
    ]

    if terminal_status == "success":
        if investigation_errors or completion_error or child_failures:
            raise RuntimeError(
                "Successful incident triage cannot retain investigation gaps or failed child events."
            )

        return []

    if not investigation_errors:
        raise RuntimeError("Partial incident triage requires at least one investigation error.")

    if not completion_error:
        raise RuntimeError("Partial incident triage must retain a parent-correlated error summary.")

    if not child_failures:
        raise RuntimeError("Partial incident triage must retain its failed optional dependency evidence.")

    unsupported_failures = [
        row
        for row in child_failures
        if str(row.get("status", "")).lower() != "failed"
        or str(row.get("action", "")) not in TRIAGE_NONFATAL_FAILURE_ACTIONS
    ]

    if unsupported_failures:
        actions = sorted(
            {
                f"{row.get('action')}:{row.get('status')}"
                for row in unsupported_failures
            }
        )
        raise RuntimeError(
            "Partial incident triage contains a non-allowlisted child failure: "
            + ", ".join(actions)
        )

    if len(child_failures) > len(investigation_errors):
        raise RuntimeError("Partial incident triage did not retain every failed dependency as an error.")

    if "unresolved_tool_errors" not in complexity_reason_codes:
        raise RuntimeError("Partial incident triage lost the unresolved-tool complexity reason.")

    post_policy = routing_policy_evidence.get("post_evidence", {})

    if post_policy.get("human_approval_required") is not True:
        raise RuntimeError("Partial incident triage must remain human approval-gated.")

    return investigation_errors


def verify_run_context_evidence(
    rows: list[dict[str, Any]],
    external_run_id: str,
    parent_run_id: str,
    expected_specialist: str,
    expected_task_type: str,
    expected_terminal_status: str,
) -> dict[str, dict[str, Any]]:
    """
    Verify exact, bounded, secret-free lifecycle context for one supervisor run.

    Args:
        rows: Persisted context rows returned with FINAL semantics.
        external_run_id: Exact Airflow or operator run identifier.
        parent_run_id: Stable supervisor correlation UUID.
        expected_specialist: Policy-selected specialist name.
        expected_task_type: Policy-selected specialist task.
        expected_terminal_status: Exact success or partial terminal result.

    Returns:
        Context rows keyed by phase.

    Raises:
        RuntimeError: If lifecycle, correlation, TTL, hash, or payload policy fails.
    """
    phases = [clickhouse_text(row.get("phase")) for row in rows]

    if tuple(phases) != EXPECTED_RUN_CONTEXT_PHASES:
        raise RuntimeError(
            "Supervisor run context must retain exactly started, routed, and completed phases."
        )

    phase_rows = dict(zip(phases, rows, strict=True))

    for phase, row in phase_rows.items():
        if clickhouse_text(row.get("external_run_id")) != external_run_id:
            raise RuntimeError("Run context external_run_id correlation is inconsistent.")

        if clickhouse_text(row.get("parent_run_id")) != parent_run_id:
            raise RuntimeError("Run context parent_run_id correlation is inconsistent.")

        if len(clickhouse_text(row.get("content_sha256"))) != 64:
            raise RuntimeError("Run context event is missing a SHA-256 content digest.")

        occurred_at = row.get("occurred_at")
        expires_at  = row.get("expires_at")

        if occurred_at is None or expires_at is None or expires_at <= occurred_at:
            raise RuntimeError("Run context event has an invalid TTL boundary.")

        for field_name in (
            "context_references_json",
            "evidence_references_json",
            "decision_json",
        ):
            payload = parse_json_value(row.get(field_name), field_name)
            forbidden_secret = find_forbidden_context_key(payload, path=field_name)
            forbidden_raw    = find_forbidden_persisted_context_key(
                payload,
                path=field_name,
            )

            if forbidden_secret:
                raise RuntimeError(
                    f"Run context contains a forbidden credential-like key: {forbidden_secret}"
                )

            if forbidden_raw:
                raise RuntimeError(
                    f"Run context contains a forbidden raw-context key: {forbidden_raw}"
                )

    routed_row    = phase_rows["routed"]
    completed_row = phase_rows["completed"]

    for row in (routed_row, completed_row):
        if clickhouse_text(row.get("selected_specialist")) != expected_specialist:
            raise RuntimeError("Run context retained a different specialist route.")

        if clickhouse_text(row.get("task_type")) != expected_task_type:
            raise RuntimeError("Run context retained a different specialist task.")

    routed_references = parse_json_value(
        routed_row.get("context_references_json"),
        "context_references_json",
    )

    if not isinstance(routed_references, list):
        raise RuntimeError("Routed context references must be a JSON list.")

    if not any(
        isinstance(item, dict) and item.get("reference_type") == "run_context"
        for item in routed_references
    ):
        raise RuntimeError("Specialist handoff is missing its explicit run-context reference.")

    if clickhouse_text(phase_rows["started"].get("status")) != "running":
        raise RuntimeError("Started context must retain running status.")

    if clickhouse_text(routed_row.get("status")) != "running":
        raise RuntimeError("Routed context must retain running status.")

    if clickhouse_text(completed_row.get("status")) != expected_terminal_status:
        raise RuntimeError("Completed context does not match the verified terminal status.")

    return phase_rows


def verify_incident_memory_evidence(
    rows: list[dict[str, Any]],
    parent_run_id: str,
    expected_specialist: str,
    expected_task_type: str,
    report_s3_uri: str,
    expected_terminal_status: str,
    required: bool,
) -> str:
    """
    Verify one idempotent durable incident outcome when alert context exists.

    Args:
        rows: Persisted memory rows returned with FINAL semantics.
        parent_run_id: Stable supervisor correlation UUID.
        expected_specialist: Policy-selected specialist name.
        expected_task_type: Policy-selected specialist task.
        report_s3_uri: Report URI retained by the specialist audit.
        expected_terminal_status: Exact success or partial terminal result.
        required: Whether the routed intent must create durable incident memory.

    Returns:
        Durable memory UUID, or an empty string when no memory is expected.

    Raises:
        RuntimeError: If required memory is missing, duplicated, malformed, or inconsistent.
    """
    if required and len(rows) != 1:
        raise RuntimeError("Alert triage must retain exactly one durable incident-memory record.")

    if len(rows) > 1:
        raise RuntimeError("One supervisor run cannot retain multiple investigation outcomes.")

    if not rows:
        return ""

    row = rows[0]

    if clickhouse_text(row.get("parent_run_id")) != parent_run_id:
        raise RuntimeError("Incident memory parent_run_id correlation is inconsistent.")

    if clickhouse_text(row.get("memory_type")) != "investigation_outcome":
        raise RuntimeError("Supervisor memory must retain an investigation_outcome record.")

    if not any(
        (
            row.get("alert_id"),
            clickhouse_text(row.get("alert_key")),
            clickhouse_text(row.get("alert_display_id")),
        )
    ):
        raise RuntimeError("Incident memory is missing its alert identity.")

    if clickhouse_text(row.get("outcome_status")) != expected_terminal_status:
        raise RuntimeError("Incident memory does not match the verified terminal outcome.")

    if clickhouse_text(row.get("specialist_name")) != expected_specialist:
        raise RuntimeError("Incident memory retained a different specialist.")

    if clickhouse_text(row.get("task_type")) != expected_task_type:
        raise RuntimeError("Incident memory retained a different task type.")

    if not clickhouse_text(row.get("summary")):
        raise RuntimeError("Incident memory is missing its operator-facing summary.")

    if len(clickhouse_text(row.get("memory_key"))) != 64:
        raise RuntimeError("Incident memory is missing its stable idempotency key.")

    if len(clickhouse_text(row.get("content_sha256"))) != 64:
        raise RuntimeError("Incident memory is missing its content digest.")

    evidence = parse_json_value(
        row.get("evidence_references_json"),
        "evidence_references_json",
    )
    decision = parse_json_value(row.get("decision_json"), "decision_json")

    if not isinstance(evidence, list) or not evidence:
        raise RuntimeError("Incident memory must retain bounded evidence references.")

    for field_name, payload in (
        ("evidence_references_json", evidence),
        ("decision_json", decision),
    ):
        forbidden_secret = find_forbidden_context_key(payload, path=field_name)
        forbidden_raw    = find_forbidden_persisted_context_key(
            payload,
            path=field_name,
        )

        if forbidden_secret or forbidden_raw:
            raise RuntimeError("Incident memory contains forbidden hidden or credential context.")

    memory_report_uri = clickhouse_text(row.get("report_s3_uri"))

    if required and memory_report_uri != report_s3_uri:
        raise RuntimeError("Incident memory report URI does not match specialist evidence.")

    return clickhouse_text(row.get("memory_id"))


# --- Defining Verification Runtime
def verify_control_plane_supervisor(
    run_id: str,
    expected_intent: str,
    expected_sql_decision: str = "",
    expected_schema_assessment: str = "",
    expected_schema_run_id: str = "",
    clickhouse_host: str | None = None,
    clickhouse_port: int | None = None,
) -> dict[str, Any]:
    """
    Verify a complete parent route and its specialist evidence.

    Args:
        run_id: Airflow or operator run correlation ID.
        expected_intent: Explicit supervisor intent expected in audit output.
        expected_sql_decision: Optional approved or rejected SQL review expectation.
        expected_schema_assessment: Optional compatibility assessment expectation.
        expected_schema_run_id: Optional exact detector run expected in specialist output.
        clickhouse_host: Optional ClickHouse host override.
        clickhouse_port: Optional ClickHouse HTTP port override.

    Returns:
        Bounded verification summary.

    Raises:
        ValueError: If expected intent is not allowlisted.
        RuntimeError: If audit evidence is missing, failed, unsafe, or inconsistent.
    """
    normalized_intent = expected_intent.strip().lower()
    normalized_sql_decision = expected_sql_decision.strip().lower()
    normalized_schema_assessment = expected_schema_assessment.strip().lower()
    normalized_schema_run = (
        validate_schema_run_id(expected_schema_run_id)
        if expected_schema_run_id.strip()
        else ""
    )

    if normalized_intent not in {
        SupervisorIntent.AUTO.value,
        *EXPECTED_ROUTE_BY_INTENT,
    }:
        raise ValueError(f"Unsupported supervisor verification intent: {expected_intent}")

    if normalized_sql_decision not in {"", "approved", "rejected"}:
        raise ValueError("Expected SQL decision must be approved, rejected, or blank.")

    if normalized_sql_decision and normalized_intent not in {
        SupervisorIntent.AUTO.value,
        SupervisorIntent.REVIEW_SQL.value,
    }:
        raise ValueError("Expected SQL decision is valid only for auto or review_sql intent.")

    if normalized_schema_assessment not in SUPPORTED_SCHEMA_ASSESSMENTS:
        raise ValueError("Unsupported expected schema assessment.")

    if normalized_schema_assessment and normalized_intent not in {
        SupervisorIntent.AUTO.value,
        SupervisorIntent.SCHEMA_DRIFT_ASSESSMENT.value,
    }:
        raise ValueError(
            "Expected schema assessment is valid only for auto or "
            "schema_drift_assessment intent."
        )

    if normalized_schema_run and normalized_intent not in {
        SupervisorIntent.AUTO.value,
        SupervisorIntent.SCHEMA_DRIFT_ASSESSMENT.value,
    }:
        raise ValueError(
            "Expected schema run ID is valid only for auto or schema_drift_assessment intent."
        )

    if (
        normalized_intent == SupervisorIntent.SCHEMA_DRIFT_ASSESSMENT.value
        and not normalized_schema_run
    ):
        raise ValueError("schema_drift_assessment verification requires expected_schema_run_id.")

    parent_run_id = derive_supervisor_parent_run_id(run_id)
    client        = build_clickhouse_client(
        host=clickhouse_host,
        port=clickhouse_port,
    )
    parent_rows = query_audit_rows(
        client=client,
        sql=build_parent_audit_sql(str(parent_run_id)),
    )
    actions     = [str(row.get("action", "")) for row in parent_rows]
    action_set  = set(actions)
    required    = BASE_REQUIRED_ACTIONS | METADATA_TOOL_ACTIONS.get(
        normalized_intent,
        set(),
    )

    if (
        normalized_intent == SupervisorIntent.REVIEW_SQL.value
        and normalized_sql_decision == "approved"
    ):
        required |= {"get_metadata_asset", "fetch_table_statistics"}
    missing     = sorted(required - action_set)
    failures    = [
        row
        for row in parent_rows
        if str(row.get("status", "")).lower() in {"failed", "blocked"}
        or str(row.get("action", "")) in {
            "specialist_handoff_failed",
            "specialist_handoff_rejected",
        }
    ]

    if missing:
        raise RuntimeError("Supervisor audit is missing required actions: " + ", ".join(missing))

    if failures:
        raise RuntimeError("Supervisor audit contains failed or blocked events.")

    if action_set.intersection(FORBIDDEN_PARENT_ACTIONS):
        raise RuntimeError("Supervisor audit contains a forbidden remediation action.")

    budget_evidence     = verify_budget_evidence(parent_rows)
    resilience_evidence = verify_resilience_evidence(parent_rows)

    route_rows = [
        row
        for row in parent_rows
        if str(row.get("action", "")) == "supervisor_route_selected"
    ]

    if len(route_rows) != 1:
        raise RuntimeError("Supervisor must retain exactly one route-selected event.")

    route_payload   = parse_json_object(route_rows[0].get("output_json"))
    resolved_intent = str(route_payload.get("resolved_intent", ""))

    if normalized_intent != SupervisorIntent.AUTO.value and resolved_intent != normalized_intent:
        raise RuntimeError("Supervisor route audit resolved a different intent.")

    if resolved_intent not in EXPECTED_ROUTE_BY_INTENT:
        raise RuntimeError("Supervisor route audit contains an unsupported resolved intent.")

    expected_specialist, expected_task_type = EXPECTED_ROUTE_BY_INTENT[resolved_intent]

    if str(route_payload.get("selected_specialist", "")) != expected_specialist:
        raise RuntimeError("Supervisor route audit selected a different specialist.")

    if str(route_payload.get("task_type", "")) != expected_task_type:
        raise RuntimeError("Supervisor route audit selected a different task type.")

    routing_policy_evidence = verify_routing_policy_evidence(
        rows=parent_rows,
        resolved_intent=resolved_intent,
    )

    missing_resolved_tools = sorted(
        METADATA_TOOL_ACTIONS.get(resolved_intent, set()) - action_set
    )

    if missing_resolved_tools:
        raise RuntimeError(
            "Supervisor audit is missing resolved specialist tool actions: "
            + ", ".join(missing_resolved_tools)
        )

    completion_rows = [
        row
        for row in parent_rows
        if str(row.get("action", "")) == "specialist_handoff_completed"
    ]

    if len(completion_rows) != 1:
        raise RuntimeError("Supervisor must retain exactly one completed specialist handoff.")

    completion_payload = parse_json_object(completion_rows[0].get("output_json"))
    terminal_status    = str(completion_rows[0].get("status", "")).lower()
    child_agent_run_id  = str(completion_payload.get("child_agent_run_id", ""))
    report_s3_uri       = str(completion_rows[0].get("report_s3_uri", ""))
    child_event_count   = 0
    incident_history_row_count = 0
    complexity_tier         = ""
    complexity_score        = 0
    complexity_reason_codes: list[str] = []
    investigation_errors: list[str] = []

    if terminal_status not in TRIAGE_TERMINAL_STATUSES:
        raise RuntimeError("Supervisor specialist completion has an invalid terminal status.")

    if terminal_status != str(resilience_evidence.get("terminal_status", "")):
        raise RuntimeError("Supervisor resilience and specialist completion statuses differ.")

    if str(completion_payload.get("result_status", "")).lower() != terminal_status:
        raise RuntimeError("Supervisor specialist completion payload has an inconsistent status.")

    if (
        terminal_status == "partial"
        and resolved_intent != SupervisorIntent.TRIAGE_ALERT.value
    ):
        raise RuntimeError("Only bounded incident triage gaps may pass this verifier as partial.")

    if resolved_intent == SupervisorIntent.REVIEW_SQL.value:
        actual_decision = str(completion_payload.get("decision", ""))

        if actual_decision not in {"approved", "rejected"}:
            raise RuntimeError("SQL review completion is missing a valid decision.")

        if normalized_sql_decision and actual_decision != normalized_sql_decision:
            raise RuntimeError("SQL review completion returned an unexpected decision.")

        if bool(completion_payload.get("execution_performed", True)):
            raise RuntimeError("SQL Review Agent must retain execution_performed=false.")

        if not str(completion_payload.get("proposal_sql_hash", "")):
            raise RuntimeError("SQL review completion is missing proposal_sql_hash.")

    if resolved_intent == SupervisorIntent.SCHEMA_DRIFT_ASSESSMENT.value:
        actual_assessment = str(completion_payload.get("assessment", ""))
        actual_impact     = str(completion_payload.get("impact_level", ""))

        if actual_assessment not in SUPPORTED_SCHEMA_ASSESSMENTS - {""}:
            raise RuntimeError("Schema specialist completion is missing a valid assessment.")

        if normalized_schema_assessment and actual_assessment != normalized_schema_assessment:
            raise RuntimeError("Schema specialist completion returned an unexpected assessment.")

        if actual_impact not in SUPPORTED_SCHEMA_IMPACT_LEVELS:
            raise RuntimeError("Schema specialist completion is missing a valid impact level.")

        actual_schema_run = str(completion_payload.get("source_schema_run_id", ""))

        if not actual_schema_run:
            raise RuntimeError("Schema specialist completion is missing source_schema_run_id.")

        if normalized_schema_run and actual_schema_run != normalized_schema_run:
            raise RuntimeError("Schema specialist completion used a different source run ID.")

        if bool(completion_payload.get("execution_performed", True)):
            raise RuntimeError("Schema Drift Agent must retain execution_performed=false.")

        if int(completion_payload.get("token_usage", 0) or 0) != 0:
            raise RuntimeError("Schema Drift Agent deterministic route must use zero model tokens.")

        if float(completion_payload.get("estimated_cost_usd", 0.0) or 0.0) != 0.0:
            raise RuntimeError("Schema Drift Agent deterministic route must retain zero model cost.")

        requires_approval = bool(completion_payload.get("requires_human_approval", False))

        if actual_assessment == "compatible" and requires_approval:
            raise RuntimeError("Compatible schema assessment must not request remediation approval.")

        if actual_assessment != "compatible" and not requires_approval:
            raise RuntimeError("Drifted schema assessment must remain human approval-gated.")

    if resolved_intent == SupervisorIntent.TRIAGE_ALERT.value:
        if not child_agent_run_id:
            raise RuntimeError("Incident specialist completion is missing child_agent_run_id.")

        complexity_tier = str(completion_payload.get("complexity_tier", ""))
        complexity_score = int(completion_payload.get("complexity_score", -1))
        raw_complexity_reasons = completion_payload.get("complexity_reason_codes")

        if complexity_tier not in {"low", "moderate", "high"}:
            raise RuntimeError("Incident specialist completion has an invalid complexity tier.")

        if not 0 <= complexity_score <= 100:
            raise RuntimeError("Incident specialist completion has an invalid complexity score.")

        if not isinstance(raw_complexity_reasons, list):
            raise RuntimeError("Incident specialist completion is missing complexity reasons.")

        complexity_reason_codes = [
            str(reason)
            for reason in raw_complexity_reasons
            if str(reason).strip()
        ]
        post_policy = routing_policy_evidence["post_evidence"]
        post_policy_reasons = {
            str(reason)
            for reason in post_policy.get("reason_codes", [])
        }

        if complexity_tier == "high":
            if "high_complexity" not in post_policy_reasons:
                raise RuntimeError(
                    "High-complexity incident did not retain its routing-policy reason."
                )

            if post_policy.get("strong_review_required") is not True:
                raise RuntimeError(
                    "High-complexity incident did not require a strong reasoning attempt."
                )

        elif "high_complexity" in post_policy_reasons:
            raise RuntimeError(
                "Non-high-complexity incident retained an unexpected high-complexity reason."
            )

        child_rows       = query_audit_rows(
            client=client,
            sql=build_child_triage_audit_sql(child_agent_run_id),
        )
        child_event_count = len(child_rows)

        if not report_s3_uri:
            raise RuntimeError("Incident specialist completion is missing report_s3_uri.")

        investigation_errors = verify_triage_terminal_evidence(
            completion_row=completion_rows[0],
            completion_payload=completion_payload,
            child_rows=child_rows,
            terminal_status=terminal_status,
            complexity_reason_codes=complexity_reason_codes,
            routing_policy_evidence=routing_policy_evidence,
        )
        incident_history_row_count = verify_incident_history_read(child_rows)

    final_rows = [
        row
        for row in parent_rows
        if str(row.get("action", "")) == "supervisor_final_decision"
    ]

    if len(final_rows) != 1:
        raise RuntimeError("Supervisor must retain exactly one final decision event.")

    if str(final_rows[0].get("status", "")).lower() != terminal_status:
        raise RuntimeError("Supervisor final decision does not match the terminal specialist status.")

    final_payload = parse_json_object(final_rows[0].get("output_json"))
    sql_review_decision = str(final_payload.get("decision", ""))
    schema_assessment   = str(final_payload.get("assessment", ""))
    schema_impact_level = str(final_payload.get("impact_level", ""))

    if resolved_intent == SupervisorIntent.TRIAGE_ALERT.value:
        if str(final_payload.get("complexity_tier", "")) != complexity_tier:
            raise RuntimeError("Supervisor final decision lost the incident complexity tier.")

        if int(final_payload.get("complexity_score", -1)) != complexity_score:
            raise RuntimeError("Supervisor final decision lost the incident complexity score.")

        if final_payload.get("complexity_reason_codes") != complexity_reason_codes:
            raise RuntimeError("Supervisor final decision lost incident complexity reasons.")

        if final_payload.get("investigation_errors") != investigation_errors:
            raise RuntimeError("Supervisor final decision lost retained investigation errors.")

        if int(final_payload.get("investigation_error_count", -1)) != len(
            investigation_errors
        ):
            raise RuntimeError("Supervisor final decision lost the investigation error count.")

    if resolved_intent == SupervisorIntent.REVIEW_SQL.value:
        if sql_review_decision != str(completion_payload.get("decision", "")):
            raise RuntimeError("Supervisor final decision lost the SQL specialist disposition.")

        if bool(final_payload.get("execution_performed", True)):
            raise RuntimeError("Supervisor final audit must retain execution_performed=false.")

    final_model_calls = int(final_payload.get("model_call_count", 0) or 0)
    reconciled_model_calls = int(
        budget_evidence["post_handoff"]["usage"].get("model_calls", 0) or 0
    )

    if final_model_calls != reconciled_model_calls:
        raise RuntimeError(
            "Supervisor final audit lost reconciled external model-call usage."
        )

    if resolved_intent == SupervisorIntent.SCHEMA_DRIFT_ASSESSMENT.value:
        if schema_assessment != str(completion_payload.get("assessment", "")):
            raise RuntimeError("Supervisor final decision lost the schema assessment.")

        if schema_impact_level != str(completion_payload.get("impact_level", "")):
            raise RuntimeError("Supervisor final decision lost the schema impact level.")

        if str(final_payload.get("source_schema_run_id", "")) != str(
            completion_payload.get("source_schema_run_id", "")
        ):
            raise RuntimeError("Supervisor final decision lost the schema source run ID.")

        if bool(final_payload.get("execution_performed", True)):
            raise RuntimeError("Supervisor final audit must retain execution_performed=false.")

    # Run context is short-lived operational state. Verify its exact lifecycle and
    # audit correlation before accepting the supervisor run as complete.
    context_rows = query_audit_rows(
        client=client,
        sql=build_run_context_sql(str(parent_run_id)),
    )
    context_by_phase = verify_run_context_evidence(
        rows=context_rows,
        external_run_id=run_id,
        parent_run_id=str(parent_run_id),
        expected_specialist=expected_specialist,
        expected_task_type=expected_task_type,
        expected_terminal_status=terminal_status,
    )
    started_rows = [
        row
        for row in parent_rows
        if str(row.get("action", "")) == "supervisor_run_started"
    ]

    if len(started_rows) != 1:
        raise RuntimeError("Supervisor must retain exactly one run-started event.")

    started_payload = parse_json_object(started_rows[0].get("output_json"))
    expected_context_ids = {
        "started": str(started_payload.get("context_event_id", "")),
        "routed": str(route_payload.get("context_event_id", "")),
        "completed": str(final_payload.get("context_event_id", "")),
    }

    for phase, expected_context_id in expected_context_ids.items():
        actual_context_id = clickhouse_text(
            context_by_phase[phase].get("context_event_id")
        )

        if not expected_context_id or actual_context_id != expected_context_id:
            raise RuntimeError(
                f"Supervisor audit lost its persisted {phase} context-event correlation."
            )

    # Durable memory is required for alert triage and optional for other routes
    # only when an explicit alert identity was supplied.
    incident_memory_rows = query_audit_rows(
        client=client,
        sql=build_incident_memory_sql(str(parent_run_id)),
    )
    incident_memory_id = verify_incident_memory_evidence(
        rows=incident_memory_rows,
        parent_run_id=str(parent_run_id),
        expected_specialist=expected_specialist,
        expected_task_type=expected_task_type,
        report_s3_uri=report_s3_uri,
        expected_terminal_status=terminal_status,
        required=(resolved_intent == SupervisorIntent.TRIAGE_ALERT.value),
    )
    final_memory_id = str(final_payload.get("incident_memory_id", ""))

    if final_memory_id != incident_memory_id:
        raise RuntimeError(
            "Supervisor final audit lost its durable incident-memory correlation."
        )

    summary = {
        "status": terminal_status,
        "run_id": run_id,
        "parent_run_id": str(parent_run_id),
        "requested_intent": normalized_intent,
        "resolved_intent": resolved_intent,
        "selected_specialist": expected_specialist,
        "task_type": expected_task_type,
        "parent_audit_event_count": len(parent_rows),
        "child_audit_event_count": child_event_count,
        "child_agent_run_id": child_agent_run_id,
        "incident_history_row_count": incident_history_row_count,
        "complexity_tier": complexity_tier,
        "complexity_score": complexity_score,
        "complexity_reason_codes": complexity_reason_codes,
        "investigation_errors": investigation_errors,
        "investigation_error_count": len(investigation_errors),
        "report_s3_uri": report_s3_uri,
        "model_call_count": final_model_calls,
        "token_usage": int(final_payload.get("token_usage", 0) or 0),
        "estimated_cost_usd": float(
            final_payload.get("estimated_cost_usd", 0.0) or 0.0
        ),
        "requires_human_approval": bool(
            final_payload.get("requires_human_approval", False)
        ),
        "sql_review_decision": sql_review_decision,
        "proposal_sql_hash": str(final_payload.get("proposal_sql_hash", "")),
        "query_risk_level": str(final_payload.get("query_risk_level", "")),
        "schema_assessment": schema_assessment,
        "schema_impact_level": schema_impact_level,
        "source_schema_run_id": str(final_payload.get("source_schema_run_id", "")),
        "schema_finding_count": int(final_payload.get("finding_count", 0) or 0),
        "impacted_asset_count": int(final_payload.get("impacted_asset_count", 0) or 0),
        "impacted_test_count": int(final_payload.get("impacted_test_count", 0) or 0),
        "run_context_event_count": len(context_rows),
        "run_context_event_ids": {
            phase: clickhouse_text(row.get("context_event_id"))
            for phase, row in context_by_phase.items()
        },
        "incident_memory_count": len(incident_memory_rows),
        "incident_memory_id": incident_memory_id,
        "execution_performed": bool(final_payload.get("execution_performed", False)),
        "budget_evidence": budget_evidence,
        "routing_policy_evidence": routing_policy_evidence,
        "resilience_evidence": resilience_evidence,
        "forbidden_action_count": len(action_set.intersection(FORBIDDEN_PARENT_ACTIONS)),
    }

    logger.info(
        "Control Plane Supervisor audit verified | run_id=%s parent_run_id=%s intent=%s specialist=%s parent_events=%d child_events=%d context_events=%d incident_memory=%d",
        run_id,
        parent_run_id,
        resolved_intent,
        expected_specialist,
        len(parent_rows),
        child_event_count,
        len(context_rows),
        len(incident_memory_rows),
    )
    print(json.dumps(summary, indent=2, sort_keys=True))

    return summary


# --- Defining CLI
def build_parser() -> argparse.ArgumentParser:
    """
    Build the supervisor audit verifier CLI parser.

    Returns:
        Configured ArgumentParser.
    """
    parser = argparse.ArgumentParser(
        description="Verify retained Control Plane Supervisor audit evidence."
    )

    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--expected-intent",
        required=True,
        choices=sorted({SupervisorIntent.AUTO.value, *EXPECTED_ROUTE_BY_INTENT}),
    )
    parser.add_argument(
        "--expected-sql-decision",
        default="",
        choices=("", "approved", "rejected"),
    )
    parser.add_argument(
        "--expected-schema-assessment",
        default="",
        choices=sorted(SUPPORTED_SCHEMA_ASSESSMENTS),
    )
    parser.add_argument("--expected-schema-run-id", default="")
    parser.add_argument("--clickhouse-host", default=None)
    parser.add_argument("--clickhouse-port", type=int, default=None)

    return parser


def main() -> None:
    """
    Parse CLI arguments and verify one supervisor audit trail.

    Returns:
        None.
    """
    args = build_parser().parse_args()

    verify_control_plane_supervisor(
        run_id=args.run_id,
        expected_intent=args.expected_intent,
        expected_sql_decision=args.expected_sql_decision,
        expected_schema_assessment=args.expected_schema_assessment,
        expected_schema_run_id=args.expected_schema_run_id,
        clickhouse_host=args.clickhouse_host,
        clickhouse_port=args.clickhouse_port,
    )


# --- Running CLI Entrypoint
if __name__ == "__main__":
    main()
