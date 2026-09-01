####
## Control Plane Execution Audit Verifier for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Dispatch strict ClickHouse audit verification for single or bounded fan-out runs."""

# --- Importing Libraries
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.supervisor.models import SupervisorExecutionMode, SupervisorIntent
from agent.supervisor.runtime import derive_supervisor_parent_run_id
from pipelines.common.clickhouse import build_clickhouse_client
from pipelines.common.logging import logger
from scripts.verify_control_plane_supervisor import (
    FORBIDDEN_PARENT_ACTIONS,
    build_parent_audit_sql,
    parse_json_value,
    query_audit_rows,
    verify_control_plane_supervisor,
)


# --- Defining Fan-Out Verification Policy
FANOUT_REQUIRED_ACTIONS = {
    "supervisor_run_started",
    "supervisor_execution_plan_created",
    "supervisor_execution_wave_started",
    "supervisor_execution_wave_completed",
    "supervisor_aggregation_completed",
    "supervisor_final_decision",
}

FANOUT_TERMINAL_HANDOFF_ACTIONS = {
    "supervisor_handoff_completed",
    "supervisor_handoff_failed",
}

FORBIDDEN_WORKER_TOOLS = {
    "airflow_backfill_executor",
    "execute_remediation",
    "mutation_executor",
    "run_clickhouse_mutation",
}

SAFE_PLAN_HASH = re.compile(r"^[a-f0-9]{64}$")


# --- Defining JSON Helpers
def row_payload(row: dict[str, Any], field_name: str) -> dict[str, Any]:
    """
    Parse one audit JSON object without accepting arrays or scalar values.

    Args:
        row: ClickHouse audit row.
        field_name: input_json or output_json.

    Returns:
        Parsed object, or an empty object for blank persisted JSON.

    Raises:
        RuntimeError: If the retained value is not a JSON object.
    """
    value = parse_json_value(row.get(field_name, "{}"), field_name)

    if not isinstance(value, dict):
        raise RuntimeError(f"Fan-out audit {field_name} must contain a JSON object.")

    return value


def single_action_row(
    rows_by_action: dict[str, list[dict[str, Any]]],
    action: str,
) -> dict[str, Any]:
    """
    Require exactly one audit row for a singleton parent lifecycle action.

    Args:
        rows_by_action: Audit rows grouped by action.
        action: Required singleton action.

    Returns:
        Exact retained audit row.

    Raises:
        RuntimeError: If the action count is not exactly one.
    """
    matching = rows_by_action.get(action, [])

    if len(matching) != 1:
        raise RuntimeError(
            f"Fan-out audit requires exactly one {action} row; found {len(matching)}."
        )

    return matching[0]


# --- Defining Fan-Out Verification Runtime
def verify_control_plane_fanout(
    run_id: str,
    expected_intent: str,
    expected_worker_count: int = 0,
    clickhouse_host: str | None = None,
    clickhouse_port: int | None = None,
) -> dict[str, Any]:
    """
    Verify one bounded fan-out lifecycle and aggregate budget from ClickHouse.

    Args:
        run_id: Airflow DagRun correlation ID.
        expected_intent: Requested supervisor intent retained by every parent event.
        expected_worker_count: Optional exact worker count; zero trusts the audited plan count.
        clickhouse_host: Optional ClickHouse host override.
        clickhouse_port: Optional ClickHouse HTTP port override.

    Returns:
        Bounded verification summary for Airflow logs.

    Raises:
        ValueError: If verifier expectations are outside policy bounds.
        RuntimeError: If audit lifecycle, permissions, identities, or budgets are inconsistent.
    """
    normalized_intent = expected_intent.strip().lower()

    if normalized_intent not in {item.value for item in SupervisorIntent}:
        raise ValueError(f"Unsupported fan-out verification intent: {expected_intent}")

    if not 0 <= expected_worker_count <= 10:
        raise ValueError("Expected fan-out worker count must be between 0 and 10.")

    parent_run_id = derive_supervisor_parent_run_id(run_id)
    client        = build_clickhouse_client(host=clickhouse_host, port=clickhouse_port)

    try:
        rows = query_audit_rows(
            client=client,
            sql=build_parent_audit_sql(str(parent_run_id)),
        )
    finally:
        close = getattr(client, "close", None)

        if callable(close):
            close()

    if not rows:
        raise RuntimeError("No parent fan-out audit rows were retained in ClickHouse.")

    actions        = [str(row.get("action", "")) for row in rows]
    action_counts  = Counter(actions)
    action_set     = set(actions)
    missing        = sorted(FANOUT_REQUIRED_ACTIONS - action_set)
    forbidden      = sorted(FORBIDDEN_PARENT_ACTIONS & action_set)
    rows_by_action = {
        action: [row for row in rows if str(row.get("action", "")) == action]
        for action in action_set
    }

    if missing:
        raise RuntimeError(f"Fan-out audit is missing required actions: {missing}")

    if forbidden:
        raise RuntimeError(f"Fan-out audit contains forbidden mutation actions: {forbidden}")

    started_row = single_action_row(rows_by_action, "supervisor_run_started")
    plan_row    = single_action_row(rows_by_action, "supervisor_execution_plan_created")
    aggregate   = single_action_row(rows_by_action, "supervisor_aggregation_completed")
    final_row   = single_action_row(rows_by_action, "supervisor_final_decision")
    started_in  = row_payload(started_row, "input_json")
    plan_out    = row_payload(plan_row, "output_json")
    aggregate_out = row_payload(aggregate, "output_json")
    final_out     = row_payload(final_row, "output_json")
    plan_details  = plan_out.get("resilience", {})
    aggregate_details = aggregate_out.get("resilience", {})
    final_details     = final_out.get("resilience", {})

    if started_in.get("execution_mode") != SupervisorExecutionMode.FANOUT.value:
        raise RuntimeError("Fan-out audit did not retain execution_mode=fanout.")

    if started_in.get("requested_intent") != normalized_intent:
        raise RuntimeError("Fan-out audit requested intent does not match the DagRun expectation.")

    worker_count   = int(plan_details.get("worker_count", 0) or 0)
    max_workers    = int(started_in.get("max_workers", 0) or 0)
    max_concurrency = int(started_in.get("max_concurrency", 0) or 0)
    plan_hash      = str(plan_details.get("plan_hash", ""))

    if not 2 <= worker_count <= 10:
        raise RuntimeError("Fan-out execution plan must contain between 2 and 10 workers.")

    if expected_worker_count and worker_count != expected_worker_count:
        raise RuntimeError(
            f"Fan-out worker count mismatch: expected {expected_worker_count}, got {worker_count}."
        )

    if worker_count > max_workers:
        raise RuntimeError("Fan-out plan exceeded the request worker capacity.")

    if not 1 <= max_concurrency <= 3 or max_concurrency > worker_count:
        raise RuntimeError("Fan-out concurrency exceeds the bounded runtime policy.")

    if not SAFE_PLAN_HASH.fullmatch(plan_hash):
        raise RuntimeError("Fan-out plan hash is missing or malformed.")

    queued_rows = rows_by_action.get("supervisor_worker_queued", [])
    terminal_rows = [
        row
        for row in rows
        if str(row.get("action", "")) in FANOUT_TERMINAL_HANDOFF_ACTIONS
    ]

    if len(queued_rows) != worker_count or len(terminal_rows) != worker_count:
        raise RuntimeError(
            "Fan-out queued and terminal handoff counts must match the execution plan."
        )

    queued_payloads   = [row_payload(row, "output_json") for row in queued_rows]
    terminal_payloads = [row_payload(row, "output_json") for row in terminal_rows]
    queued_task_ids   = {str(item.get("task_id", "")) for item in queued_payloads}
    terminal_task_ids = {str(item.get("task_id", "")) for item in terminal_payloads}

    if "" in queued_task_ids or len(queued_task_ids) != worker_count:
        raise RuntimeError("Fan-out queued worker task identities are blank or duplicated.")

    if terminal_task_ids != queued_task_ids:
        raise RuntimeError("Fan-out terminal worker identities do not match queued workers.")

    for payload in queued_payloads:
        allowed_tools = payload.get("allowed_tools", [])

        if not isinstance(allowed_tools, list) or not allowed_tools:
            raise RuntimeError("Every fan-out worker must retain an explicit tool allowlist.")

        if FORBIDDEN_WORKER_TOOLS & set(map(str, allowed_tools)):
            raise RuntimeError("A fan-out worker received a forbidden mutation tool.")

    model_calls = sum(int(item.get("model_call_count", 0) or 0) for item in terminal_payloads)
    tokens      = sum(int(item.get("token_usage", 0) or 0) for item in terminal_payloads)
    cost        = sum(float(item.get("estimated_cost_usd", 0.0) or 0.0) for item in terminal_payloads)

    if model_calls > int(started_in.get("max_model_calls", 0) or 0):
        raise RuntimeError("Fan-out model calls exceeded the parent request budget.")

    if tokens > int(started_in.get("token_budget", 0) or 0):
        raise RuntimeError("Fan-out token usage exceeded the parent request budget.")

    if cost > float(started_in.get("estimated_cost_budget_usd", 0.0) or 0.0) + 1e-12:
        raise RuntimeError("Fan-out estimated cost exceeded the parent request budget.")

    expected_totals = {
        "model_call_count": model_calls,
        "token_usage": tokens,
        "estimated_cost_usd": cost,
    }

    for field_name, expected_value in expected_totals.items():
        retained_value = final_details.get(field_name, 0)

        if field_name == "estimated_cost_usd":
            matches = abs(float(retained_value or 0.0) - float(expected_value)) <= 1e-12
        else:
            matches = int(retained_value or 0) == int(expected_value)

        if not matches:
            raise RuntimeError(f"Fan-out final {field_name} does not reconcile to worker totals.")

    if int(aggregate_details.get("worker_count", 0) or 0) != worker_count:
        raise RuntimeError("Fan-out aggregation worker count does not match the execution plan.")

    if str(final_row.get("status", "")) not in {"success", "partial"}:
        raise RuntimeError("Fan-out final decision is not an accepted terminal status.")

    summary = {
        "result": "success",
        "execution_mode": "fanout",
        "run_id": run_id,
        "parent_run_id": str(parent_run_id),
        "requested_intent": normalized_intent,
        "plan_hash": plan_hash,
        "worker_count": worker_count,
        "max_concurrency": max_concurrency,
        "terminal_status": str(final_row.get("status", "")),
        "model_call_count": model_calls,
        "token_usage": tokens,
        "estimated_cost_usd": cost,
        "audit_row_count": len(rows),
        "action_counts": dict(sorted(action_counts.items())),
    }

    logger.info(
        "Verified bounded fan-out audit | run_id=%s parent_run_id=%s workers=%d status=%s calls=%d tokens=%d cost=%.8f",
        run_id,
        parent_run_id,
        worker_count,
        summary["terminal_status"],
        model_calls,
        tokens,
        cost,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))

    return summary


# --- Defining CLI
def build_parser() -> argparse.ArgumentParser:
    """
    Build one verifier parser shared by single and fan-out execution modes.

    Returns:
        Configured ArgumentParser.
    """
    parser = argparse.ArgumentParser(
        description="Verify single or bounded fan-out Control Plane audit evidence."
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--execution-mode",
        default="single",
        choices=[item.value for item in SupervisorExecutionMode],
    )
    parser.add_argument(
        "--expected-intent",
        required=True,
        choices=[item.value for item in SupervisorIntent],
    )
    parser.add_argument("--expected-worker-count", type=int, default=0)
    parser.add_argument("--expected-sql-decision", default="")
    parser.add_argument("--expected-schema-assessment", default="")
    parser.add_argument("--expected-schema-run-id", default="")
    parser.add_argument("--clickhouse-host", default=None)
    parser.add_argument("--clickhouse-port", type=int, default=None)

    return parser


def main() -> None:
    """
    Parse execution mode and run the corresponding strict verifier.

    Returns:
        None.
    """
    args = build_parser().parse_args()

    if args.execution_mode == SupervisorExecutionMode.FANOUT.value:
        verify_control_plane_fanout(
            run_id=args.run_id,
            expected_intent=args.expected_intent,
            expected_worker_count=args.expected_worker_count,
            clickhouse_host=args.clickhouse_host,
            clickhouse_port=args.clickhouse_port,
        )
        return

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
