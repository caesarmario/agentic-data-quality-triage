####
## Airflow Metadata And Lineage Agent Trigger Helper
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Trigger the bounded specialist smoke DAG without shell interpolation."""

# --- Importing Libraries
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dags.dq_platform.metadata_lineage_agent import METADATA_LINEAGE_TASK_TYPES
from pipelines.common.clickhouse import validate_qualified_table_name
from pipelines.common.logging import logger


# --- Defining Constants
METADATA_LINEAGE_DAG_ID = "97_dag_dq_metadata_lineage_agent_smoke"
SAFE_SEARCH_PATTERN     = re.compile(r"^[A-Za-z0-9 _.-]{0,120}$")


# --- Defining Validation Helpers
def validate_trigger_inputs(
    task_type: str,
    qualified_name: str,
    query: str,
    max_depth: int,
    max_nodes: int,
) -> tuple[str, str, str, int, int]:
    """
    Normalize and validate externally supplied specialist smoke parameters.

    Args:
        task_type: Requested allowlisted specialist task.
        qualified_name: Exact database.table identity for asset tasks.
        query: Bounded metadata search text.
        max_depth: Maximum dbt traversal depth.
        max_nodes: Maximum dbt nodes returned.

    Returns:
        Tuple of normalized task, asset, query, depth, and node bound.

    Raises:
        ValueError: If any parameter violates trigger policy.
    """
    normalized_task  = task_type.strip().lower()
    normalized_asset = qualified_name.strip()
    normalized_query = query.strip()

    if normalized_task not in METADATA_LINEAGE_TASK_TYPES:
        raise ValueError(f"Unsupported metadata-lineage task type: {task_type}")

    if normalized_task in {"asset_context", "blast_radius"}:
        normalized_asset = validate_qualified_table_name(normalized_asset)
    else:
        # Search handoffs do not receive unnecessary exact-asset context.
        normalized_asset = ""

    if not SAFE_SEARCH_PATTERN.fullmatch(normalized_query):
        raise ValueError("Metadata search query contains unsupported characters.")

    if not 1 <= int(max_depth) <= 10:
        raise ValueError("max_depth must be between 1 and 10.")

    if not 1 <= int(max_nodes) <= 250:
        raise ValueError("max_nodes must be between 1 and 250.")

    return normalized_task, normalized_asset, normalized_query, int(max_depth), int(max_nodes)


def build_metadata_lineage_run_id(task_type: str, now: datetime | None = None) -> str:
    """
    Build a unique audit-friendly specialist DagRun identifier.

    Args:
        task_type: Allowlisted specialist task type.
        now: Optional UTC timestamp used by tests.

    Returns:
        Airflow run ID without shell-sensitive characters.
    """
    normalized_task, _, _, _, _ = validate_trigger_inputs(
        task_type=task_type,
        qualified_name="dq.raw_orders" if task_type != "trusted_asset_search" else "",
        query="orders",
        max_depth=5,
        max_nodes=100,
    )
    current = now or datetime.now(timezone.utc)

    return f"manual__metadata_lineage_{normalized_task}_{current.strftime('%Y%m%dT%H%M%S%f')}"


def build_trigger_command(
    task_type: str,
    qualified_name: str,
    query: str,
    max_depth: int,
    max_nodes: int,
    run_id: str,
) -> list[str]:
    """
    Build the Airflow CLI trigger command with bounded JSON configuration.

    Args:
        task_type: Allowlisted specialist task type.
        qualified_name: Validated database.table identity.
        query: Validated metadata search text.
        max_depth: Validated dbt traversal depth.
        max_nodes: Validated dbt node bound.
        run_id: Explicit Airflow run identifier.

    Returns:
        Subprocess argument list safe from shell parsing.
    """
    normalized_task, normalized_asset, normalized_query, resolved_depth, resolved_nodes = (
        validate_trigger_inputs(
            task_type=task_type,
            qualified_name=qualified_name,
            query=query,
            max_depth=max_depth,
            max_nodes=max_nodes,
        )
    )
    conf = json.dumps(
        {
            "task_type": normalized_task,
            "qualified_name": normalized_asset,
            "query": normalized_query,
            "max_depth": resolved_depth,
            "max_nodes": resolved_nodes,
        },
        separators=(",", ":"),
    )

    return [
        "airflow",
        "dags",
        "trigger",
        "-r",
        run_id,
        "-c",
        conf,
        "-o",
        "table",
        METADATA_LINEAGE_DAG_ID,
    ]


def run_command(command: list[str]) -> None:
    """
    Run one Airflow CLI command and stream output.

    Args:
        command: Subprocess argument list.

    Returns:
        None.

    Raises:
        CalledProcessError: If the Airflow command fails.
    """
    logger.info("Running Airflow metadata-lineage control command | command=%s", command)
    subprocess.run(command, check=True)


def trigger_metadata_lineage_agent(
    task_type: str,
    qualified_name: str,
    query: str,
    max_depth: int,
    max_nodes: int,
    run_id: str = "",
) -> str:
    """
    Unpause and trigger the manual Metadata and Lineage Agent smoke DAG.

    Args:
        task_type: Allowlisted specialist task type.
        qualified_name: Exact asset for non-search tasks.
        query: Bounded search query.
        max_depth: Maximum dbt traversal depth.
        max_nodes: Maximum dbt node count.
        run_id: Optional explicit Airflow run ID.

    Returns:
        Run ID created for the specialist DagRun.
    """
    normalized_task, normalized_asset, normalized_query, resolved_depth, resolved_nodes = (
        validate_trigger_inputs(
            task_type=task_type,
            qualified_name=qualified_name,
            query=query,
            max_depth=max_depth,
            max_nodes=max_nodes,
        )
    )
    resolved_run_id = run_id.strip() or build_metadata_lineage_run_id(normalized_task)

    run_command(["airflow", "dags", "unpause", METADATA_LINEAGE_DAG_ID])
    run_command(
        build_trigger_command(
            task_type=normalized_task,
            qualified_name=normalized_asset,
            query=normalized_query,
            max_depth=resolved_depth,
            max_nodes=resolved_nodes,
            run_id=resolved_run_id,
        )
    )

    print(f"METADATA_LINEAGE_DAG_ID={METADATA_LINEAGE_DAG_ID}")
    print(f"METADATA_LINEAGE_RUN_ID={resolved_run_id}")
    print(f"METADATA_LINEAGE_TASK={normalized_task}")
    print(f"METADATA_LINEAGE_ASSET={normalized_asset}")

    return resolved_run_id


# --- Defining CLI
def build_parser() -> argparse.ArgumentParser:
    """
    Build the Metadata and Lineage Agent trigger parser.

    Returns:
        Configured ArgumentParser.
    """
    parser = argparse.ArgumentParser(
        description="Trigger the manual Metadata and Lineage Agent smoke DAG."
    )

    parser.add_argument("--task-type", default="asset_context", choices=METADATA_LINEAGE_TASK_TYPES)
    parser.add_argument("--qualified-name", default="dq.raw_orders")
    parser.add_argument("--query", default="orders")
    parser.add_argument("--max-depth", type=int, default=5)
    parser.add_argument("--max-nodes", type=int, default=100)
    parser.add_argument("--run-id", default="", help="Optional explicit run ID for audit lookup.")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """
    Parse arguments and trigger the specialist smoke DAG.

    Args:
        argv: Optional argument sequence used by tests.

    Returns:
        Zero when the trigger command succeeds.
    """
    args = build_parser().parse_args(argv)
    trigger_metadata_lineage_agent(
        task_type=args.task_type,
        qualified_name=args.qualified_name,
        query=args.query,
        max_depth=args.max_depth,
        max_nodes=args.max_nodes,
        run_id=args.run_id,
    )

    return 0


# --- Running CLI Entrypoint
if __name__ == "__main__":
    raise SystemExit(main())
