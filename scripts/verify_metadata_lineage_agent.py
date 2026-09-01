####
## Metadata And Lineage Agent Audit Verifier for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Verify retained ClickHouse audit evidence for one specialist Airflow run."""

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

from agent.specialists.metadata_lineage import derive_metadata_lineage_parent_run_id
from agent.specialists.registry import METADATA_LINEAGE_TASK_TYPES
from agent.state import parse_json_object
from agent.tools.clickhouse_sql import rows_to_dicts
from pipelines.common.clickhouse import build_clickhouse_client, quote_sql_literal
from pipelines.common.logging import logger


# --- Defining Constants
AUDIT_ACTIONS_BY_TASK = {
    "asset_context": {
        "get_metadata_asset",
        "fetch_dbt_lineage",
        "fetch_dbt_blast_radius",
        "specialist_handoff_started",
        "specialist_handoff_completed",
    },
    "blast_radius": {
        "get_metadata_asset",
        "fetch_dbt_blast_radius",
        "specialist_handoff_started",
        "specialist_handoff_completed",
    },
    "trusted_asset_search": {
        "search_metadata_assets",
        "specialist_handoff_started",
        "specialist_handoff_completed",
    },
}


# --- Defining Audit Query Helpers
def build_specialist_audit_sql(parent_run_id: str) -> str:
    """
    Build an exact, bounded audit query for one specialist parent run.

    Args:
        parent_run_id: Deterministic UUID string derived from the Airflow run ID.

    Returns:
        Read-only ClickHouse query with an exact UUID predicate and hard LIMIT.
    """
    run_id_literal = quote_sql_literal(parent_run_id)

    return f"""
        SELECT
            ts,
            action,
            tool_name,
            status,
            input_json,
            output_json,
            error_message,
            duration_ms,
            row_count
        FROM dq.agent_audit_log
        WHERE agent_run_id = toUUID({run_id_literal})
        ORDER BY ts ASC
        LIMIT 100
    """


def verify_specialist_audit(
    run_id: str,
    task_type: str,
    qualified_name: str = "",
    clickhouse_host: str | None = None,
    clickhouse_port: int | None = None,
) -> dict[str, Any]:
    """
    Verify required tool and handoff events for one specialist execution.

    Args:
        run_id: Airflow or operator run correlation ID.
        task_type: Allowlisted specialist task type.
        qualified_name: Expected exact metadata asset for non-search tasks.
        clickhouse_host: Optional ClickHouse host override.
        clickhouse_port: Optional ClickHouse HTTP port override.

    Returns:
        Bounded audit verification summary.

    Raises:
        ValueError: If task type or expected asset input is invalid.
        RuntimeError: If required audit evidence is missing, failed, or inconsistent.
    """
    normalized_task = task_type.strip().lower()

    if normalized_task not in AUDIT_ACTIONS_BY_TASK:
        raise ValueError(f"Unsupported metadata-lineage task type: {task_type}")

    if normalized_task != "trusted_asset_search" and not qualified_name.strip():
        raise ValueError(f"{normalized_task} verification requires qualified_name.")

    parent_run_id = derive_metadata_lineage_parent_run_id(run_id)
    client        = build_clickhouse_client(host=clickhouse_host, port=clickhouse_port)
    sql           = build_specialist_audit_sql(str(parent_run_id))
    result        = client.query(sql)
    rows          = rows_to_dicts(
        columns=list(result.column_names),
        rows=result.result_rows,
    )
    actions       = [str(row.get("action", "")) for row in rows]
    action_set    = set(actions)
    required      = AUDIT_ACTIONS_BY_TASK[normalized_task]
    missing       = sorted(required - action_set)
    failures      = [
        row
        for row in rows
        if str(row.get("status", "")).lower() in {"failed", "blocked"}
        or str(row.get("action", "")) in {
            "specialist_handoff_failed",
            "specialist_handoff_rejected",
        }
    ]

    if missing:
        raise RuntimeError("Missing specialist audit actions: " + ", ".join(missing))

    if failures:
        failed_actions = sorted({str(row.get("action", "")) for row in failures})
        raise RuntimeError("Specialist audit contains failed actions: " + ", ".join(failed_actions))

    completion_rows = [
        row
        for row in rows
        if row.get("action") == "specialist_handoff_completed"
        and row.get("status") == "success"
    ]

    if not completion_rows:
        raise RuntimeError("Successful specialist_handoff_completed audit event was not found.")

    completion_output = parse_json_object(completion_rows[-1].get("output_json"))
    qualified_names   = [str(value) for value in completion_output.get("qualified_names", [])]

    if completion_output.get("result_status") != "success":
        raise RuntimeError("Specialist completion audit did not report result_status=success.")

    if completion_output.get("model_route") != "no_llm_fallback":
        raise RuntimeError("Metadata and Lineage Agent must use no_llm_fallback in this pilot.")

    if int(completion_output.get("token_usage", -1)) != 0:
        raise RuntimeError("Deterministic specialist unexpectedly reported model token usage.")

    if float(completion_output.get("estimated_cost_usd", -1.0)) != 0.0:
        raise RuntimeError("Deterministic specialist unexpectedly reported model cost.")

    if bool(completion_output.get("requires_human_approval", True)):
        raise RuntimeError("Read-only metadata specialist unexpectedly requested mutation approval.")

    if normalized_task != "trusted_asset_search":
        expected_name = qualified_name.strip().lower()
        normalized_names = {value.lower() for value in qualified_names}

        if expected_name not in normalized_names:
            raise RuntimeError(
                f"Specialist completion audit did not include expected asset: {qualified_name}"
            )

    summary = {
        "run_id": run_id,
        "parent_run_id": str(parent_run_id),
        "task_type": normalized_task,
        "qualified_name": qualified_name.strip(),
        "audit_event_count": len(rows),
        "required_actions": sorted(required),
        "observed_actions": sorted(action_set),
        "result_status": completion_output.get("result_status"),
        "trust_status": completion_output.get("trust_status"),
        "confidence": completion_output.get("confidence"),
        "evidence_reference_count": completion_output.get("evidence_reference_count"),
        "impacted_asset_count": completion_output.get("impacted_asset_count"),
        "impacted_test_count": completion_output.get("impacted_test_count"),
        "model_route": completion_output.get("model_route"),
        "token_usage": completion_output.get("token_usage"),
        "estimated_cost_usd": completion_output.get("estimated_cost_usd"),
        "requires_human_approval": completion_output.get("requires_human_approval"),
    }

    logger.info(
        "Verified Metadata and Lineage Agent audit | run_id=%s parent_run_id=%s events=%d",
        run_id,
        parent_run_id,
        len(rows),
    )
    print(json.dumps(summary, indent=2, sort_keys=True))

    return summary


# --- Defining CLI
def build_parser() -> argparse.ArgumentParser:
    """
    Build the specialist audit verification CLI parser.

    Returns:
        Configured ArgumentParser.
    """
    parser = argparse.ArgumentParser(
        description="Verify ClickHouse audit evidence for one Metadata and Lineage Agent run."
    )

    parser.add_argument("--run-id", required=True)
    parser.add_argument("--task-type", required=True, choices=list(METADATA_LINEAGE_TASK_TYPES))
    parser.add_argument("--qualified-name", default="")
    parser.add_argument("--clickhouse-host", default=None)
    parser.add_argument("--clickhouse-port", type=int, default=None)

    return parser


def main() -> None:
    """
    Parse CLI arguments and verify one specialist audit trail.

    Returns:
        None.
    """
    parser = build_parser()
    args   = parser.parse_args()

    verify_specialist_audit(
        run_id=args.run_id,
        task_type=args.task_type,
        qualified_name=args.qualified_name,
        clickhouse_host=args.clickhouse_host,
        clickhouse_port=args.clickhouse_port,
    )


if __name__ == "__main__":
    main()
