####
## dbt Lineage Tool for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Add repo root before importing project packages when this file is executed by path.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.state import Alert, EvidenceItem, EvidenceType
from agent.tools.alerts import load_alert
from agent.tools.audit_log import write_agent_audit_event
from pipelines.common.clickhouse import build_clickhouse_client
from pipelines.common.logging import logger
from pipelines.seeding.upload_to_s3 import build_s3_client


# --- Defining Constants
TOOL_NAME                    = "dbt_lineage"
BLAST_RADIUS_TOOL_NAME       = "dbt_blast_radius"
DEFAULT_LOCAL_MANIFEST_PATH  = PROJECT_ROOT / "warehouse" / "dbt" / "target" / "manifest.json"
DEFAULT_BLAST_RADIUS_DEPTH   = 5
DEFAULT_BLAST_RADIUS_NODES   = 100
MAX_BLAST_RADIUS_DEPTH       = 10
MAX_BLAST_RADIUS_NODES       = 250
LINEAGE_RESOURCE_COLLECTIONS = (
    "nodes",
    "sources",
    "exposures",
    "metrics",
    "semantic_models",
    "saved_queries",
)


# --- Defining Functions
def load_manifest_from_local(path: str | Path = DEFAULT_LOCAL_MANIFEST_PATH) -> dict[str, Any]:
    """
    Load a dbt manifest JSON file from the local workspace.

    Args:
        path: Local manifest.json path.

    Returns:
        Parsed dbt manifest dictionary.

    Raises:
        FileNotFoundError: If the manifest file does not exist.
        ValueError: If the manifest root is not a JSON object.
    """
    manifest_path = Path(path)
    logger.info("Loading dbt manifest from local file | path=%s", manifest_path)

    if not manifest_path.exists():
        raise FileNotFoundError(f"dbt manifest not found: {manifest_path}")

    with manifest_path.open("r", encoding="utf-8") as fh:
        manifest = json.load(fh)

    if not isinstance(manifest, dict):
        raise ValueError("dbt manifest root must be a JSON object")

    logger.info("Loaded local dbt manifest | nodes=%d sources=%d", len(manifest.get("nodes", {})), len(manifest.get("sources", {})))

    return manifest


def parse_s3_uri(s3_uri: str) -> tuple[str, str]:
    """
    Split an S3 URI into bucket and object key.

    Args:
        s3_uri: S3 URI such as s3://dq-artifacts/dbt/manifest.json.

    Returns:
        Tuple of bucket name and object key.

    Raises:
        ValueError: If the URI is not an S3 URI.
    """
    if not s3_uri.startswith("s3://"):
        raise ValueError(f"Expected S3 URI, got: {s3_uri}")

    without_scheme = s3_uri.removeprefix("s3://")
    bucket, _, key = without_scheme.partition("/")

    if not bucket or not key:
        raise ValueError(f"S3 URI must include bucket and key: {s3_uri}")

    return bucket, key


def load_manifest_from_s3(s3_uri: str, endpoint_url: str | None = None) -> dict[str, Any]:
    """
    Load a dbt manifest JSON file from SeaweedFS S3-compatible storage.

    Args:
        s3_uri: S3 URI for manifest.json.
        endpoint_url: Optional S3 endpoint URL override.

    Returns:
        Parsed dbt manifest dictionary.

    Raises:
        ValueError: If the S3 object is not a JSON object.
        botocore.exceptions.BotoCoreError: If S3 read fails.
    """
    bucket, key = parse_s3_uri(s3_uri)
    client      = build_s3_client(endpoint_url=endpoint_url)

    logger.info("Loading dbt manifest from S3 | uri=%s", s3_uri)

    response = client.get_object(Bucket=bucket, Key=key)
    payload  = response["Body"].read()
    manifest = json.loads(payload.decode("utf-8"))

    if not isinstance(manifest, dict):
        raise ValueError("dbt manifest root must be a JSON object")

    logger.info("Loaded S3 dbt manifest | uri=%s bytes=%d", s3_uri, len(payload))

    return manifest


def load_manifest(
    manifest_path: str | Path | None = None,
    manifest_s3_uri: str | None = None,
    endpoint_url: str | None = None,
) -> tuple[dict[str, Any], str]:
    """
    Load a dbt manifest from S3 when provided, otherwise from local file.

    Args:
        manifest_path: Optional local manifest path.
        manifest_s3_uri: Optional S3 URI for manifest.json.
        endpoint_url: Optional S3 endpoint URL override.

    Returns:
        Tuple of manifest dictionary and source label.
    """
    if manifest_s3_uri:
        return load_manifest_from_s3(s3_uri=manifest_s3_uri, endpoint_url=endpoint_url), manifest_s3_uri

    local_path = Path(manifest_path) if manifest_path else DEFAULT_LOCAL_MANIFEST_PATH

    return load_manifest_from_local(local_path), str(local_path)


def normalize_relation_name(value: str) -> str:
    """
    Normalize dbt relation names and table names for matching.

    Args:
        value: Relation name from dbt manifest or user input.

    Returns:
        Lowercase relation name without ClickHouse quotes.
    """
    cleaned = value.replace("`", "").replace('"', "").strip().lower()

    if cleaned.startswith("."):
        cleaned = cleaned[1:]

    return cleaned


def node_to_summary(unique_id: str, node: dict[str, Any]) -> dict[str, Any]:
    """
    Convert a dbt manifest node into compact lineage metadata.

    Args:
        unique_id: dbt unique_id for the node.
        node: dbt manifest node dictionary.

    Returns:
        Compact node summary dictionary.
    """
    return {
        "unique_id": unique_id,
        "resource_type": node.get("resource_type"),
        "name": node.get("name"),
        "alias": node.get("alias"),
        "schema": node.get("schema"),
        "relation_name": node.get("relation_name"),
        "description": node.get("description", ""),
        "path": node.get("path"),
        "original_file_path": node.get("original_file_path"),
    }


def combined_table_nodes(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """
    Combine dbt model/test nodes and sources used for table matching.

    Args:
        manifest: Parsed dbt manifest dictionary.

    Returns:
        Mapping of dbt unique_id to node/source dictionary.
    """
    nodes = {}
    nodes.update(manifest.get("nodes", {}))
    nodes.update(manifest.get("sources", {}))

    return nodes


def combined_nodes(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """
    Combine dbt lineage resource collections into one lookup mapping.

    Args:
        manifest: Parsed dbt manifest dictionary.

    Returns:
        Mapping of dbt unique_id to model, source, test, exposure, or semantic metadata.
    """
    nodes: dict[str, dict[str, Any]] = {}

    for collection_name in LINEAGE_RESOURCE_COLLECTIONS:
        collection = manifest.get(collection_name, {})

        if isinstance(collection, dict):
            nodes.update(collection)

    return nodes


def find_node_for_table(manifest: dict[str, Any], table_name: str) -> tuple[str, dict[str, Any]] | None:
    """
    Find the dbt node or source that matches a ClickHouse table name.

    Args:
        manifest: Parsed dbt manifest dictionary.
        table_name: Fully qualified ClickHouse table name such as dq.fct_orders_daily.

    Returns:
        Tuple of unique_id and node dictionary when found, otherwise None.
    """
    normalized_table = normalize_relation_name(table_name)
    table_tail       = normalized_table.split(".")[-1]

    for unique_id, node in combined_table_nodes(manifest).items():
        candidates = [
            node.get("relation_name", ""),
            f"{node.get('schema', '')}.{node.get('alias') or node.get('name', '')}",
            node.get("name", ""),
            node.get("alias", ""),
        ]

        normalized_candidates = {normalize_relation_name(str(item)) for item in candidates if item}

        if normalized_table in normalized_candidates or table_tail in normalized_candidates:
            logger.info("Matched dbt node for table | table=%s unique_id=%s", table_name, unique_id)
            return unique_id, node

    logger.warning("No dbt node matched table | table=%s", table_name)

    return None


def build_lineage_summary(manifest: dict[str, Any], table_name: str) -> dict[str, Any]:
    """
    Build parent/child/test lineage summary for a ClickHouse table.

    Args:
        manifest: Parsed dbt manifest dictionary.
        table_name: Fully qualified ClickHouse table name.

    Returns:
        Dictionary describing matched node, upstream parents, downstream children, and tests.
    """
    match = find_node_for_table(manifest=manifest, table_name=table_name)

    if not match:
        return {
            "table_name": table_name,
            "matched": False,
            "node": None,
            "parents": [],
            "children": [],
            "tests": [],
        }

    unique_id, node = match
    nodes           = combined_nodes(manifest)
    parent_map      = manifest.get("parent_map", {})
    child_map       = manifest.get("child_map", {})
    parent_ids      = parent_map.get(unique_id, [])
    child_ids       = child_map.get(unique_id, [])
    parents         = [node_to_summary(parent_id, nodes[parent_id]) for parent_id in parent_ids if parent_id in nodes]
    child_nodes     = [node_to_summary(child_id, nodes[child_id]) for child_id in child_ids if child_id in nodes]
    tests           = [item for item in child_nodes if item.get("resource_type") == "test"]
    children        = [item for item in child_nodes if item.get("resource_type") != "test"]

    return {
        "table_name": table_name,
        "matched": True,
        "node": node_to_summary(unique_id, node),
        "parents": parents,
        "children": children,
        "tests": tests,
    }


def validate_blast_radius_bounds(max_depth: int, max_nodes: int) -> tuple[int, int]:
    """
    Validate bounded downstream traversal settings.

    Args:
        max_depth: Maximum child-map depth below the selected dbt node.
        max_nodes: Maximum downstream nodes returned, excluding the root node.

    Returns:
        Validated max_depth and max_nodes values.

    Raises:
        ValueError: If either traversal setting is outside the supported range.
    """
    if not 1 <= max_depth <= MAX_BLAST_RADIUS_DEPTH:
        raise ValueError(
            f"max_depth must be between 1 and {MAX_BLAST_RADIUS_DEPTH}."
        )

    if not 1 <= max_nodes <= MAX_BLAST_RADIUS_NODES:
        raise ValueError(
            f"max_nodes must be between 1 and {MAX_BLAST_RADIUS_NODES}."
        )

    return max_depth, max_nodes


def impact_node_to_summary(
    unique_id: str,
    node: dict[str, Any] | None,
    depth: int,
    parent_unique_id: str,
    path: tuple[str, ...],
) -> dict[str, Any]:
    """
    Convert one traversed child into bounded blast-radius metadata.

    Args:
        unique_id: dbt unique identifier for the downstream node.
        node: Optional manifest node metadata when the identifier is resolvable.
        depth: Shortest downstream distance from the selected root node.
        parent_unique_id: Parent used by the breadth-first traversal.
        path: Shortest unique-id path from the root to this node.

    Returns:
        Compact downstream node summary without raw or compiled SQL.
    """
    summary = node_to_summary(unique_id, node or {})
    summary.update(
        {
            "resource_type": summary.get("resource_type") or "unknown",
            "depth": depth,
            "parent_unique_id": parent_unique_id,
            "lineage_path": list(path),
        }
    )

    return summary


def build_blast_radius_summary(
    manifest: dict[str, Any],
    table_name: str,
    max_depth: int = DEFAULT_BLAST_RADIUS_DEPTH,
    max_nodes: int = DEFAULT_BLAST_RADIUS_NODES,
) -> dict[str, Any]:
    """
    Build a bounded transitive downstream blast radius from a dbt manifest.

    The traversal is deterministic, breadth-first, cycle-safe, and excludes raw
    or compiled SQL from its output. dbt tests are separated from downstream
    data assets so operators can distinguish business impact from validation
    coverage.

    Args:
        manifest: Parsed dbt manifest dictionary.
        table_name: Fully qualified ClickHouse table name.
        max_depth: Maximum downstream child-map depth.
        max_nodes: Maximum downstream nodes returned, excluding the root node.

    Returns:
        Bounded blast-radius summary with assets, tests, counts, and truncation state.
    """
    resolved_depth, resolved_nodes = validate_blast_radius_bounds(
        max_depth=max_depth,
        max_nodes=max_nodes,
    )
    match = find_node_for_table(manifest=manifest, table_name=table_name)

    if not match:
        return {
            "table_name": table_name,
            "matched": False,
            "node": None,
            "max_depth": resolved_depth,
            "max_nodes": resolved_nodes,
            "max_depth_reached": 0,
            "truncated": False,
            "total_impacted_nodes": 0,
            "impacted_asset_count": 0,
            "impacted_test_count": 0,
            "unresolved_node_count": 0,
            "resource_type_counts": {},
            "impacted_assets": [],
            "impacted_tests": [],
            "unresolved_nodes": [],
            "summary": f"No dbt manifest node matched {table_name}.",
        }

    root_unique_id, root_node = match
    nodes                     = combined_nodes(manifest)
    child_map                 = manifest.get("child_map", {})
    queue                     = deque()
    visited                   = {root_unique_id}
    impacted_assets: list[dict[str, Any]] = []
    impacted_tests: list[dict[str, Any]]  = []
    unresolved_nodes: list[dict[str, Any]] = []
    resource_type_counts: dict[str, int]   = {}
    max_depth_reached = 0
    truncated         = False

    for child_unique_id in sorted(child_map.get(root_unique_id, [])):
        queue.append(
            (
                child_unique_id,
                1,
                root_unique_id,
                (root_unique_id, child_unique_id),
            )
        )

    while queue:
        unique_id, depth, parent_unique_id, path = queue.popleft()

        if unique_id in visited:
            continue

        if len(visited) - 1 >= resolved_nodes:
            truncated = True
            break

        visited.add(unique_id)
        max_depth_reached = max(max_depth_reached, depth)

        node         = nodes.get(unique_id)
        node_summary = impact_node_to_summary(
            unique_id=unique_id,
            node=node,
            depth=depth,
            parent_unique_id=parent_unique_id,
            path=path,
        )
        resource_type = str(node_summary["resource_type"])
        resource_type_counts[resource_type] = resource_type_counts.get(resource_type, 0) + 1

        if node is None:
            unresolved_nodes.append(node_summary)

        elif resource_type == "test":
            impacted_tests.append(node_summary)

        else:
            impacted_assets.append(node_summary)

        child_ids = [
            child_id
            for child_id in sorted(child_map.get(unique_id, []))
            if child_id not in visited
        ]

        if depth >= resolved_depth:
            truncated = truncated or bool(child_ids)
            continue

        for child_unique_id in child_ids:
            queue.append(
                (
                    child_unique_id,
                    depth + 1,
                    unique_id,
                    (*path, child_unique_id),
                )
            )

    total_impacted_nodes = len(impacted_assets) + len(impacted_tests) + len(unresolved_nodes)
    summary = (
        f"{table_name} impacts {len(impacted_assets)} downstream data assets and "
        f"{len(impacted_tests)} dbt tests across {max_depth_reached} levels."
    )

    if unresolved_nodes:
        summary += f" {len(unresolved_nodes)} manifest references could not be resolved."

    if truncated:
        summary += " Result truncated by traversal bounds."

    logger.info(
        "Built dbt blast radius | table=%s assets=%d tests=%d unresolved=%d depth=%d truncated=%s",
        table_name,
        len(impacted_assets),
        len(impacted_tests),
        len(unresolved_nodes),
        max_depth_reached,
        truncated,
    )

    return {
        "table_name": table_name,
        "matched": True,
        "node": node_to_summary(root_unique_id, root_node),
        "max_depth": resolved_depth,
        "max_nodes": resolved_nodes,
        "max_depth_reached": max_depth_reached,
        "truncated": truncated,
        "total_impacted_nodes": total_impacted_nodes,
        "impacted_asset_count": len(impacted_assets),
        "impacted_test_count": len(impacted_tests),
        "unresolved_node_count": len(unresolved_nodes),
        "resource_type_counts": dict(sorted(resource_type_counts.items())),
        "impacted_assets": impacted_assets,
        "impacted_tests": impacted_tests,
        "unresolved_nodes": unresolved_nodes,
        "summary": summary,
    }


def fetch_dbt_lineage(
    table_name: str,
    agent_run_id: UUID | str | None = None,
    alert_key: str = "",
    manifest_path: str | Path | None = None,
    manifest_s3_uri: str | None = None,
    endpoint_url: str | None = None,
    clickhouse_host: str | None = None,
    clickhouse_port: int | None = None,
) -> dict[str, Any]:
    """
    Fetch dbt lineage context for a table and audit the tool call.

    Args:
        table_name: Fully qualified ClickHouse table name.
        agent_run_id: Optional agent run UUID for audit correlation.
        alert_key: Optional alert key for audit context.
        manifest_path: Optional local manifest path.
        manifest_s3_uri: Optional S3 URI for manifest.json.
        endpoint_url: Optional S3 endpoint URL override.
        clickhouse_host: Optional ClickHouse host override for audit logging.
        clickhouse_port: Optional ClickHouse HTTP port override for audit logging.

    Returns:
        Lineage summary dictionary.
    """
    client                = build_clickhouse_client(host=clickhouse_host, port=clickhouse_port)
    resolved_agent_run_id = UUID(str(agent_run_id)) if agent_run_id else uuid4()
    started_monotonic     = time.monotonic()

    try:
        manifest, source = load_manifest(
            manifest_path=manifest_path,
            manifest_s3_uri=manifest_s3_uri,
            endpoint_url=endpoint_url,
        )
        lineage     = build_lineage_summary(manifest=manifest, table_name=table_name)
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)

        write_agent_audit_event(
            client=client,
            action="fetch_dbt_lineage",
            status="success",
            agent_run_id=resolved_agent_run_id,
            alert_key=alert_key,
            tool_name=TOOL_NAME,
            duration_ms=duration_ms,
            input_payload={
                "table_name": table_name,
                "manifest_source": source,
                "manifest_s3_uri": manifest_s3_uri,
            },
            output_payload={
                "matched": lineage["matched"],
                "parent_count": len(lineage["parents"]),
                "child_count": len(lineage["children"]),
                "test_count": len(lineage["tests"]),
            },
            row_count=1 if lineage["matched"] else 0,
        )

        lineage["manifest_source"] = source

        logger.info("Fetched dbt lineage | table=%s matched=%s", table_name, lineage["matched"])

        return lineage

    except Exception as exc:
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)
        logger.exception("Failed to fetch dbt lineage | table=%s", table_name)

        write_agent_audit_event(
            client=client,
            action="fetch_dbt_lineage",
            status="failed",
            agent_run_id=resolved_agent_run_id,
            alert_key=alert_key,
            tool_name=TOOL_NAME,
            duration_ms=duration_ms,
            input_payload={"table_name": table_name, "manifest_s3_uri": manifest_s3_uri},
            output_payload={"error_type": type(exc).__name__},
            error_message=str(exc),
        )

        raise


def fetch_dbt_blast_radius(
    table_name: str,
    agent_run_id: UUID | str | None = None,
    alert_key: str = "",
    manifest_path: str | Path | None = None,
    manifest_s3_uri: str | None = None,
    endpoint_url: str | None = None,
    max_depth: int = DEFAULT_BLAST_RADIUS_DEPTH,
    max_nodes: int = DEFAULT_BLAST_RADIUS_NODES,
    clickhouse_host: str | None = None,
    clickhouse_port: int | None = None,
) -> dict[str, Any]:
    """
    Fetch and audit a bounded transitive dbt blast-radius analysis.

    Args:
        table_name: Fully qualified ClickHouse table name.
        agent_run_id: Optional agent run UUID for audit correlation.
        alert_key: Optional alert key for audit context.
        manifest_path: Optional local manifest path.
        manifest_s3_uri: Optional S3 URI for manifest.json.
        endpoint_url: Optional S3 endpoint URL override.
        max_depth: Maximum downstream child-map depth.
        max_nodes: Maximum downstream nodes returned, excluding the root node.
        clickhouse_host: Optional ClickHouse host override for audit logging.
        clickhouse_port: Optional ClickHouse HTTP port override for audit logging.

    Returns:
        Bounded blast-radius summary with manifest source metadata.
    """
    resolved_depth, resolved_nodes = validate_blast_radius_bounds(
        max_depth=max_depth,
        max_nodes=max_nodes,
    )
    client                = build_clickhouse_client(host=clickhouse_host, port=clickhouse_port)
    resolved_agent_run_id = UUID(str(agent_run_id)) if agent_run_id else uuid4()
    started_monotonic     = time.monotonic()

    try:
        manifest, source = load_manifest(
            manifest_path=manifest_path,
            manifest_s3_uri=manifest_s3_uri,
            endpoint_url=endpoint_url,
        )
        blast_radius = build_blast_radius_summary(
            manifest=manifest,
            table_name=table_name,
            max_depth=resolved_depth,
            max_nodes=resolved_nodes,
        )
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)

        write_agent_audit_event(
            client=client,
            action="fetch_dbt_blast_radius",
            status="success",
            agent_run_id=resolved_agent_run_id,
            alert_key=alert_key,
            tool_name=BLAST_RADIUS_TOOL_NAME,
            duration_ms=duration_ms,
            input_payload={
                "table_name": table_name,
                "manifest_source": source,
                "manifest_s3_uri": manifest_s3_uri,
                "max_depth": resolved_depth,
                "max_nodes": resolved_nodes,
            },
            output_payload={
                "matched": blast_radius["matched"],
                "impacted_asset_count": blast_radius["impacted_asset_count"],
                "impacted_test_count": blast_radius["impacted_test_count"],
                "unresolved_node_count": blast_radius["unresolved_node_count"],
                "max_depth_reached": blast_radius["max_depth_reached"],
                "truncated": blast_radius["truncated"],
            },
            row_count=blast_radius["total_impacted_nodes"],
        )

        blast_radius["manifest_source"] = source

        logger.info(
            "Fetched dbt blast radius | table=%s matched=%s impacted_nodes=%d truncated=%s",
            table_name,
            blast_radius["matched"],
            blast_radius["total_impacted_nodes"],
            blast_radius["truncated"],
        )

        return blast_radius

    except Exception as exc:
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)
        logger.exception("Failed to fetch dbt blast radius | table=%s", table_name)

        write_agent_audit_event(
            client=client,
            action="fetch_dbt_blast_radius",
            status="failed",
            agent_run_id=resolved_agent_run_id,
            alert_key=alert_key,
            tool_name=BLAST_RADIUS_TOOL_NAME,
            duration_ms=duration_ms,
            input_payload={
                "table_name": table_name,
                "manifest_s3_uri": manifest_s3_uri,
                "max_depth": resolved_depth,
                "max_nodes": resolved_nodes,
            },
            output_payload={"error_type": type(exc).__name__},
            error_message=str(exc),
        )

        raise


def collect_dbt_lineage_evidence(
    alert: Alert,
    agent_run_id: UUID | str | None = None,
    manifest_path: str | Path | None = None,
    manifest_s3_uri: str | None = None,
    endpoint_url: str | None = None,
    clickhouse_host: str | None = None,
    clickhouse_port: int | None = None,
) -> EvidenceItem:
    """
    Build a dbt lineage evidence item for an alert table.

    Args:
        alert: Alert being investigated.
        agent_run_id: Optional agent run UUID for audit correlation.
        manifest_path: Optional local manifest path.
        manifest_s3_uri: Optional S3 URI for manifest.json.
        endpoint_url: Optional S3 endpoint URL override.
        clickhouse_host: Optional ClickHouse host override.
        clickhouse_port: Optional ClickHouse HTTP port override.

    Returns:
        EvidenceItem containing dbt lineage context.
    """
    lineage = fetch_dbt_lineage(
        table_name=alert.table_name,
        agent_run_id=agent_run_id,
        alert_key=alert.alert_key,
        manifest_path=manifest_path,
        manifest_s3_uri=manifest_s3_uri,
        endpoint_url=endpoint_url,
        clickhouse_host=clickhouse_host,
        clickhouse_port=clickhouse_port,
    )
    summary = (
        f"dbt lineage matched={lineage['matched']} for {alert.table_name}; "
        f"parents={len(lineage['parents'])}, children={len(lineage['children'])}, tests={len(lineage['tests'])}."
    )

    return EvidenceItem(
        evidence_type=EvidenceType.LINEAGE,
        tool_name=TOOL_NAME,
        description="dbt lineage context for the alert table.",
        rows=[lineage],
        row_count=1,
        summary=summary,
    )


def build_parser() -> argparse.ArgumentParser:
    """
    Build the command-line parser for dbt lineage lookup.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(description="Fetch dbt lineage context for an alert or table.")

    parser.add_argument("--alert-key", default=None, help="Optional alert key to derive table name.")
    parser.add_argument("--table-name", default=None, help="Fully qualified table name when not using --alert-key.")
    parser.add_argument("--manifest-path", default=None, help="Optional local manifest.json path.")
    parser.add_argument("--manifest-s3-uri", default=None, help="Optional S3 URI for manifest.json.")
    parser.add_argument("--endpoint-url", default=None, help="Optional S3 endpoint URL override.")
    parser.add_argument("--agent-run-id", default=None, help="Optional agent run UUID.")
    parser.add_argument(
        "--blast-radius",
        action="store_true",
        help="Return bounded transitive downstream impact instead of direct lineage only.",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=DEFAULT_BLAST_RADIUS_DEPTH,
        help=f"Maximum downstream depth for blast radius, up to {MAX_BLAST_RADIUS_DEPTH}.",
    )
    parser.add_argument(
        "--max-nodes",
        type=int,
        default=DEFAULT_BLAST_RADIUS_NODES,
        help=f"Maximum downstream nodes for blast radius, up to {MAX_BLAST_RADIUS_NODES}.",
    )
    parser.add_argument("--clickhouse-host", default=None, help="Optional ClickHouse host override.")
    parser.add_argument("--clickhouse-port", type=int, default=None, help="Optional ClickHouse HTTP port override.")

    return parser


def main() -> None:
    """
    Parse CLI arguments and fetch dbt lineage.

    Returns:
        None.
    """
    parser = build_parser()
    args   = parser.parse_args()

    if args.alert_key:
        resolved_agent_run_id = args.agent_run_id or str(uuid4())
        alert                 = load_alert(alert_key=args.alert_key, agent_run_id=resolved_agent_run_id)

        if args.blast_radius:
            blast_radius = fetch_dbt_blast_radius(
                table_name=alert.table_name,
                agent_run_id=resolved_agent_run_id,
                alert_key=alert.alert_key,
                manifest_path=args.manifest_path,
                manifest_s3_uri=args.manifest_s3_uri,
                endpoint_url=args.endpoint_url,
                max_depth=args.max_depth,
                max_nodes=args.max_nodes,
                clickhouse_host=args.clickhouse_host,
                clickhouse_port=args.clickhouse_port,
            )
            print(json.dumps(blast_radius, indent=2, default=str))

            return

        evidence = collect_dbt_lineage_evidence(
            alert=alert,
            agent_run_id=resolved_agent_run_id,
            manifest_path=args.manifest_path,
            manifest_s3_uri=args.manifest_s3_uri,
            endpoint_url=args.endpoint_url,
            clickhouse_host=args.clickhouse_host,
            clickhouse_port=args.clickhouse_port,
        )
        print(evidence.model_dump_json(indent=2))

        return

    if not args.table_name:
        parser.error("Provide --alert-key or --table-name.")

    if args.blast_radius:
        lineage = fetch_dbt_blast_radius(
            table_name=args.table_name,
            agent_run_id=args.agent_run_id,
            manifest_path=args.manifest_path,
            manifest_s3_uri=args.manifest_s3_uri,
            endpoint_url=args.endpoint_url,
            max_depth=args.max_depth,
            max_nodes=args.max_nodes,
            clickhouse_host=args.clickhouse_host,
            clickhouse_port=args.clickhouse_port,
        )

    else:
        lineage = fetch_dbt_lineage(
            table_name=args.table_name,
            agent_run_id=args.agent_run_id,
            manifest_path=args.manifest_path,
            manifest_s3_uri=args.manifest_s3_uri,
            endpoint_url=args.endpoint_url,
            clickhouse_host=args.clickhouse_host,
            clickhouse_port=args.clickhouse_port,
        )

    print(json.dumps(lineage, indent=2, default=str))


# --- Running CLI Entrypoint
if __name__ == "__main__":
    main()
