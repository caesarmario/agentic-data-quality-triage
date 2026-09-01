####
## Fan-Out Resilience Audit Verifier for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Verify fan-out resilience summaries and parent lifecycle evidence in ClickHouse."""

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

from agent.specialists.contracts import AgentTaskStatus
from agent.supervisor.fanout_resilience import (
    FANOUT_RESILIENCE_SUMMARY_ACTION,
    FanoutResilienceScenario,
    expected_fanout_status,
    supported_fanout_resilience_scenarios,
)
from agent.supervisor.runtime import derive_supervisor_parent_run_id
from agent.tools.clickhouse_sql import rows_to_dicts
from pipelines.common.clickhouse import build_clickhouse_client, quote_sql_literal
from pipelines.common.logging import logger


# --- Defining Constants
MAX_AUDIT_ROWS = 500
FORBIDDEN_MUTATION_ACTIONS = {
    "approval_execution_dispatched",
    "approval_execution_succeeded",
    "backfill_triggered",
    "execute_remediation",
    "schema_mutation",
}


# --- Defining Query Helpers
def parse_json_object(value: Any) -> dict[str, Any]:
    """
    Parse a ClickHouse JSON string into one dictionary.

    Args:
        value: Raw JSON text or existing dictionary.

    Returns:
        Parsed dictionary.

    Raises:
        RuntimeError: If the value is malformed or not an object.
    """
    if isinstance(value, dict):
        return value

    try:
        parsed = json.loads(str(value or "{}"))

    except json.JSONDecodeError as exc:
        raise RuntimeError("Fan-out resilience audit contains malformed JSON.") from exc

    if not isinstance(parsed, dict):
        raise RuntimeError("Fan-out resilience audit JSON must be an object.")

    return parsed


def query_parent_audit_rows(
    parent_run_id: str,
    clickhouse_host: str | None = None,
    clickhouse_port: int | None = None,
) -> list[dict[str, Any]]:
    """
    Read bounded parent-correlated audit events from ClickHouse.

    Args:
        parent_run_id: Stable supervisor parent UUID.
        clickhouse_host: Optional ClickHouse host override.
        clickhouse_port: Optional ClickHouse HTTP port override.

    Returns:
        Oldest-first audit event dictionaries.
    """
    client = build_clickhouse_client(host=clickhouse_host, port=clickhouse_port)
    sql = f"""
        SELECT
            audit_id,
            ts,
            agent_run_id,
            actor,
            action,
            tool_name,
            status,
            input_json,
            output_json,
            error_message,
            sql_hash,
            report_s3_uri
        FROM dq.agent_audit_log
        WHERE agent_run_id = toUUID({quote_sql_literal(parent_run_id)})
        ORDER BY ts ASC, audit_id ASC
        LIMIT {MAX_AUDIT_ROWS}
    """

    try:
        result = client.query(sql)

        return rows_to_dicts(
            columns=list(result.column_names),
            rows=list(result.result_rows),
        )

    finally:
        close = getattr(client, "close", None)

        if callable(close):
            close()


# --- Defining Verification
def verify_fanout_resilience_audit(
    run_id: str,
    scenario: FanoutResilienceScenario | str,
    clickhouse_host: str | None = None,
    clickhouse_port: int | None = None,
) -> dict[str, Any]:
    """
    Verify one fan-out resilience scenario from retained ClickHouse evidence.

    Args:
        run_id: Source Airflow DagRun identifier.
        scenario: Allowlisted fan-out resilience scenario.
        clickhouse_host: Optional ClickHouse host override.
        clickhouse_port: Optional ClickHouse HTTP port override.

    Returns:
        Compact verified evidence for the Airflow task log.

    Raises:
        RuntimeError: If summary, lifecycle, budget, or safety evidence is incomplete.
    """
    resolved      = FanoutResilienceScenario(scenario)
    parent_run_id = str(derive_supervisor_parent_run_id(run_id))
    rows = query_parent_audit_rows(
        parent_run_id=parent_run_id,
        clickhouse_host=clickhouse_host,
        clickhouse_port=clickhouse_port,
    )

    if not rows:
        raise RuntimeError("No fan-out resilience audit rows were retained.")

    actions = [str(row.get("action", "")) for row in rows]

    if FORBIDDEN_MUTATION_ACTIONS.intersection(actions):
        raise RuntimeError("Fan-out resilience smoke retained a forbidden mutation action.")

    summary_rows = [
        row
        for row in rows
        if str(row.get("action", "")) == FANOUT_RESILIENCE_SUMMARY_ACTION
    ]

    if len(summary_rows) != 1:
        raise RuntimeError("Fan-out resilience smoke requires one replay-safe summary event.")

    summary = parse_json_object(summary_rows[0].get("output_json"))

    if summary.get("scenario") != resolved.value:
        raise RuntimeError("Fan-out resilience summary scenario does not match Airflow conf.")

    expected_status = expected_fanout_status(resolved).value

    if summary.get("status") != expected_status:
        raise RuntimeError("Fan-out resilience summary retained an unexpected status.")

    if summary.get("parent_run_id") != parent_run_id:
        raise RuntimeError("Fan-out resilience summary parent identity is inconsistent.")

    if int(summary.get("external_request_count", -1)) != 0:
        raise RuntimeError("Administrative resilience smoke must not call an external provider.")

    if any(
        (
            int(summary.get("model_call_count", -1)) != 0,
            int(summary.get("token_usage", -1)) != 0,
            float(summary.get("estimated_cost_usd", -1.0)) != 0.0,
        )
    ):
        raise RuntimeError("Fan-out resilience smoke retained non-zero provider usage.")

    final_rows = [row for row in rows if str(row.get("action", "")) == "supervisor_final_decision"]
    expected_final_count = (
        2
        if resolved == FanoutResilienceScenario.RESUME_COMPLETED_WAVE
        else 1
    )

    if len(final_rows) != expected_final_count:
        raise RuntimeError("Supervisor final-decision audit count does not match scenario policy.")

    if any(str(row.get("status", "")) != expected_status for row in final_rows):
        raise RuntimeError("Supervisor final-decision status does not match scenario policy.")

    if resolved == FanoutResilienceScenario.CONCURRENT_BUDGET_RESERVATION:
        if int(summary.get("worker_count", 0)) != 10:
            raise RuntimeError("Capacity scenario did not retain ten workers.")

        if int(summary.get("peak_concurrency", 0)) > 3:
            raise RuntimeError("Capacity scenario exceeded concurrency three.")

        if int(summary.get("concurrent_reservation_count", 0)) != 10:
            raise RuntimeError("Concurrent budget allocation evidence is incomplete.")

    if resolved == FanoutResilienceScenario.RESUME_COMPLETED_WAVE:
        if summary.get("replay_executor_calls") != summary.get("executor_call_count"):
            raise RuntimeError("Checkpoint resume repeated a completed worker execution.")

    if resolved in {
        FanoutResilienceScenario.GEMINI_TIMEOUT_SIMULATED,
        FanoutResilienceScenario.GEMINI_RATE_LIMIT_SIMULATED,
    } and summary.get("provider_failure_simulated") is not True:
        raise RuntimeError("Simulated provider failure is not explicitly labelled as synthetic.")

    if resolved == FanoutResilienceScenario.INVALID_WORKER_CONTRACT:
        if int(summary.get("executor_call_count", -1)) != 0:
            raise RuntimeError("Invalid worker contract reached the worker executor.")

    verified = {
        "run_id": run_id,
        "parent_run_id": parent_run_id,
        "scenario": resolved.value,
        "result": "success",
        "parent_status": expected_status,
        "audit_event_count": len(rows),
        "final_decision_count": len(final_rows),
        "worker_count": int(summary.get("worker_count", 0)),
        "executor_call_count": int(summary.get("executor_call_count", 0)),
        "peak_concurrency": int(summary.get("peak_concurrency", 0)),
        "external_request_count": 0,
        "model_call_count": 0,
        "token_usage": 0,
        "estimated_cost_usd": 0.0,
    }

    print(json.dumps(verified, indent=2, sort_keys=True))
    logger.info(
        "Verified fan-out resilience audit | run_id=%s scenario=%s status=%s events=%d workers=%d",
        run_id,
        resolved.value,
        expected_status,
        len(rows),
        verified["worker_count"],
    )

    return verified


# --- Defining CLI
def build_parser() -> argparse.ArgumentParser:
    """
    Build the allowlisted fan-out resilience verifier parser.

    Returns:
        Configured argparse parser.
    """
    parser = argparse.ArgumentParser(
        description="Verify one bounded fan-out resilience audit trail."
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--scenario",
        required=True,
        choices=supported_fanout_resilience_scenarios(),
    )
    parser.add_argument("--clickhouse-host", default=None)
    parser.add_argument("--clickhouse-port", type=int, default=None)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """
    Parse CLI arguments and verify one fan-out resilience run.

    Args:
        argv: Optional explicit CLI argument sequence.

    Returns:
        Zero when all retained evidence passes.
    """
    args = build_parser().parse_args(argv)
    verify_fanout_resilience_audit(
        run_id=args.run_id,
        scenario=args.scenario,
        clickhouse_host=args.clickhouse_host,
        clickhouse_port=args.clickhouse_port,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
