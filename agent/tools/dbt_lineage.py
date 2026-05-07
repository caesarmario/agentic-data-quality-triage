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
TOOL_NAME                   = "dbt_lineage"
DEFAULT_LOCAL_MANIFEST_PATH = PROJECT_ROOT / "warehouse" / "dbt" / "target" / "manifest.json"


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


def combined_nodes(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """
    Combine dbt model/test nodes and sources into one lookup mapping.

    Args:
        manifest: Parsed dbt manifest dictionary.

    Returns:
        Mapping of dbt unique_id to node/source dictionary.
    """
    nodes = {}
    nodes.update(manifest.get("nodes", {}))
    nodes.update(manifest.get("sources", {}))

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

    for unique_id, node in combined_nodes(manifest).items():
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
