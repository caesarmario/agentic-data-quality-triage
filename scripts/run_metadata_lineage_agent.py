####
## Metadata And Lineage Agent Runner for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Run one bounded Metadata and Lineage Agent handoff from Airflow or an operator CLI."""

# --- Importing Libraries
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.specialists.contracts import AgentResultEnvelope, AgentTaskStatus
from agent.specialists.metadata_lineage import (
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_NODES,
    DEFAULT_RESULT_LIMIT,
    build_metadata_lineage_task,
    derive_metadata_lineage_parent_run_id,
    run_metadata_lineage_agent,
)
from agent.specialists.registry import METADATA_LINEAGE_TASK_TYPES
from pipelines.common.logging import logger


# --- Defining CLI Helpers
def build_parser() -> argparse.ArgumentParser:
    """
    Build the bounded Metadata and Lineage Agent CLI parser.

    Returns:
        Configured ArgumentParser that rejects arbitrary tools and model routes.
    """
    parser = argparse.ArgumentParser(
        description="Run one read-only Metadata and Lineage Agent handoff."
    )

    parser.add_argument("--run-id", required=True, help="Airflow or operator run correlation ID.")
    parser.add_argument(
        "--task-type",
        required=True,
        choices=list(METADATA_LINEAGE_TASK_TYPES),
        help="Allowlisted specialist task type.",
    )
    parser.add_argument("--qualified-name", default="", help="Exact database.table asset identity.")
    parser.add_argument("--query", default="", help="Bounded metadata discovery query.")
    parser.add_argument("--domain", default="", help="Optional metadata domain filter.")
    parser.add_argument("--data-layer", default="", help="Optional raw, staging, or mart filter.")
    parser.add_argument(
        "--certification-status",
        default="",
        help="Optional metadata certification filter.",
    )
    parser.add_argument("--lifecycle-status", default="", help="Optional lifecycle filter.")
    parser.add_argument("--limit", type=int, default=DEFAULT_RESULT_LIMIT)
    parser.add_argument("--max-depth", type=int, default=DEFAULT_MAX_DEPTH)
    parser.add_argument("--max-nodes", type=int, default=DEFAULT_MAX_NODES)
    parser.add_argument("--requester", default="airflow", help="Bounded operator-interface identity.")
    parser.add_argument("--alert-key", default="", help="Optional alert correlation key.")

    return parser


# --- Defining Runtime
def build_operator_summary(result: AgentResultEnvelope) -> dict[str, object]:
    """
    Build a compact Airflow-safe summary without dumping full lineage node lists.

    Args:
        result: Typed specialist result returned by the LangGraph subgraph.

    Returns:
        Bounded operator summary containing trust, impact, routing, and evidence references.
    """
    output          = result.structured_output
    metadata_assets = list(output.get("metadata_assets", []))
    blast_radius    = dict(output.get("blast_radius", {}))

    return {
        "task_id": str(result.task_id),
        "parent_run_id": str(result.parent_run_id),
        "specialist_name": result.specialist_name,
        "task_type": result.task_type,
        "status": result.status.value,
        "confidence": result.confidence,
        "summary": str(output.get("summary", "")),
        "trust_status": str(output.get("trust_status", "")),
        "qualified_names": [
            str(asset.get("qualified_name", ""))
            for asset in metadata_assets
            if asset.get("qualified_name")
        ],
        "impacted_asset_count": int(blast_radius.get("impacted_asset_count", 0)),
        "impacted_test_count": int(blast_radius.get("impacted_test_count", 0)),
        "truncated": bool(blast_radius.get("truncated", False)),
        "evidence_references": [
            {
                "evidence_type": reference.evidence_type,
                "source_tool": reference.source_tool,
                "reference": reference.reference,
            }
            for reference in result.evidence_references
        ],
        "model_route": result.model_route.value,
        "token_usage": result.token_usage,
        "estimated_cost_usd": result.estimated_cost_usd,
        "requires_human_approval": result.requires_human_approval,
        "duration_ms": result.duration_ms,
        "recommended_next_step": result.recommended_next_step,
        "errors": result.errors,
    }


def run_from_args(args: argparse.Namespace) -> dict[str, object]:
    """
    Build and execute one specialist task from parsed CLI arguments.

    Args:
        args: Parsed CLI namespace.

    Returns:
        JSON-safe AgentResultEnvelope dictionary.

    Raises:
        RuntimeError: If the bounded specialist returns blocked or failed status.
    """
    parent_run_id = derive_metadata_lineage_parent_run_id(args.run_id)
    task          = build_metadata_lineage_task(
        parent_run_id=parent_run_id,
        task_type=args.task_type,
        qualified_name=args.qualified_name,
        query=args.query,
        domain=args.domain,
        data_layer=args.data_layer,
        certification_status=args.certification_status,
        lifecycle_status=args.lifecycle_status,
        limit=args.limit,
        max_depth=args.max_depth,
        max_nodes=args.max_nodes,
        requester=args.requester,
        alert_key=args.alert_key,
    )

    logger.info(
        "Starting Metadata and Lineage Agent | run_id=%s parent_run_id=%s task_id=%s task_type=%s",
        args.run_id,
        parent_run_id,
        task.task_id,
        task.task_type,
    )

    result  = run_metadata_lineage_agent(task=task)
    payload = result.model_dump(mode="json")
    summary = build_operator_summary(result)

    # Airflow keeps the operational summary while full graph state remains in-process.
    print(json.dumps(summary, indent=2, sort_keys=True))

    if result.status != AgentTaskStatus.SUCCESS:
        raise RuntimeError(
            f"Metadata and Lineage Agent ended with status={result.status.value}: "
            + "; ".join(result.errors)
        )

    logger.info(
        "Metadata and Lineage Agent completed | run_id=%s task_id=%s confidence=%.2f evidence=%d",
        args.run_id,
        task.task_id,
        result.confidence,
        len(result.evidence_references),
    )

    return payload


def main() -> None:
    """
    Parse CLI arguments and execute one bounded specialist handoff.

    Returns:
        None.
    """
    parser = build_parser()
    args   = parser.parse_args()

    run_from_args(args)


if __name__ == "__main__":
    main()
