####
## MCP Server for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Add repo root before importing project packages when this file is executed by path.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.graph import DEFAULT_CONFIDENCE_TARGET, DEFAULT_MAX_EVIDENCE_LOOP, TriageRuntimeConfig, run_triage
from agent.tools.alerts import list_alerts, load_alert
from agent.tools.audit_log import write_agent_audit_event
from agent.tools.clickhouse_sql import run_guarded_sql
from agent.tools.dbt_lineage import (
    DEFAULT_BLAST_RADIUS_DEPTH,
    DEFAULT_BLAST_RADIUS_NODES,
    fetch_dbt_blast_radius,
    fetch_dbt_lineage,
)
from agent.tools.dq_history import fetch_dq_history
from agent.tools.metadata_catalog import get_metadata_asset, search_metadata_assets
from agent.tools.pipeline_runs import fetch_pipeline_runs
from agent.tools.s3 import parse_s3_uri
from pipelines.common.clickhouse import build_clickhouse_client
from pipelines.common.logging import logger
from pipelines.seeding.helpers import parse_date
from pipelines.seeding.upload_to_s3 import build_s3_client


# --- Defining Constants
SERVER_NAME = "agentic-data-quality-triage"
TOOL_NAME   = "mcp_server"

DEFAULT_ARTIFACT_READ_LIMIT_BYTES = 200_000
DEFAULT_SKILLS_MAX_CHARS          = 12_000
SKILLS_PATH                       = PROJECT_ROOT / "agent" / "SKILLS.md"

REPORT_BUCKET_ALLOWLIST = {
    "dq-artifacts",
    "dq-dqreports",
    "dq-dqfailures",
    "dq-audit",
}


# --- Defining Classes
@dataclass(frozen=True)
class McpToolSpec:
    """
    Describe one MCP-facing tool exposed by the local data reliability platform.

    Attributes:
        name: Public MCP tool name.
        purpose: Short explanation of what the tool does.
        audit_behavior: How tool calls are audited.
        risk_level: Operational risk level for the tool.
    """

    name: str
    purpose: str
    audit_behavior: str
    risk_level: str = "low"


# --- Defining MCP Tool Registry
MCP_TOOL_REGISTRY: tuple[McpToolSpec, ...] = (
    McpToolSpec(
        name="list_alerts",
        purpose="List recent DQ alerts from ClickHouse with optional status/date filters.",
        audit_behavior="Audited by agent.tools.alerts.list_alerts.",
    ),
    McpToolSpec(
        name="get_alert",
        purpose="Load one alert by alert_id or alert_key.",
        audit_behavior="Audited by agent.tools.alerts.load_alert.",
    ),
    McpToolSpec(
        name="search_metadata_assets",
        purpose="Search trusted warehouse assets by text, domain, layer, certification, or lifecycle.",
        audit_behavior="Audited by agent.tools.metadata_catalog.search_metadata_assets.",
    ),
    McpToolSpec(
        name="get_metadata_asset",
        purpose="Read ownership, grain, SLA, sensitivity, and certification for one warehouse asset.",
        audit_behavior="Audited by agent.tools.metadata_catalog.get_metadata_asset.",
    ),
    McpToolSpec(
        name="run_guarded_sql",
        purpose="Run read-only ClickHouse SQL with SQL guardrails, date filters, denylist, and hard LIMIT.",
        audit_behavior="Audited by agent.tools.clickhouse_sql.run_guarded_sql.",
        risk_level="medium",
    ),
    McpToolSpec(
        name="get_dbt_lineage",
        purpose="Read dbt manifest lineage context for one warehouse table.",
        audit_behavior="Audited by agent.tools.dbt_lineage.fetch_dbt_lineage.",
    ),
    McpToolSpec(
        name="get_dbt_blast_radius",
        purpose="Trace bounded transitive downstream dbt impact for one warehouse table.",
        audit_behavior="Audited by agent.tools.dbt_lineage.fetch_dbt_blast_radius.",
    ),
    McpToolSpec(
        name="get_dq_history",
        purpose="Fetch recent DQ check history for one table/check/date window.",
        audit_behavior="Audited by agent.tools.dq_history.fetch_dq_history.",
    ),
    McpToolSpec(
        name="get_pipeline_runs",
        purpose="Fetch pipeline run status history around a business date.",
        audit_behavior="Audited by agent.tools.pipeline_runs.fetch_pipeline_runs.",
    ),
    McpToolSpec(
        name="run_triage",
        purpose="Run the LangGraph triage workflow for one alert and store Markdown/JSON reports in S3.",
        audit_behavior="Audited by the underlying triage tools and final agent audit log.",
        risk_level="medium",
    ),
    McpToolSpec(
        name="get_triage_skills",
        purpose="Read the agent triage operating playbook from agent/SKILLS.md for MCP clients.",
        audit_behavior="Audited directly by agent.mcp.server.",
    ),
    McpToolSpec(
        name="read_report_artifact",
        purpose="Read a bounded Markdown/JSON report artifact from approved local S3 buckets.",
        audit_behavior="Audited directly by agent.mcp.server.",
    ),
)


# --- Defining Helper Functions
def tool_registry_as_dicts() -> list[dict[str, str]]:
    """
    Return the MCP tool registry as JSON-serializable dictionaries.

    Returns:
        List of MCP tool metadata dictionaries.
    """
    return [asdict(tool) for tool in MCP_TOOL_REGISTRY]


def hash_text(value: str) -> str:
    """
    Build a stable SHA-256 hash for MCP-visible text artifacts.

    Args:
        value: Text value to hash.

    Returns:
        SHA-256 hex digest.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def ensure_report_s3_uri_allowed(s3_uri: str) -> tuple[str, str]:
    """
    Validate that a report artifact URI points to an approved project bucket.

    Args:
        s3_uri: S3 URI for a report or audit artifact.

    Returns:
        Tuple of bucket name and object key.

    Raises:
        ValueError: If the URI bucket is outside the project report allowlist.
    """
    bucket, key = parse_s3_uri(s3_uri)

    if bucket not in REPORT_BUCKET_ALLOWLIST:
        allowed = ", ".join(sorted(REPORT_BUCKET_ALLOWLIST))
        raise ValueError(f"Report artifact bucket is not allowed: {bucket}. Allowed buckets: {allowed}")

    return bucket, key


def bounded_text(payload: bytes, max_bytes: int) -> tuple[str, bool]:
    """
    Decode an S3 artifact payload and cap the text returned to MCP clients.

    Args:
        payload: Raw S3 object bytes.
        max_bytes: Maximum bytes to return in the response.

    Returns:
        Tuple of decoded text and whether the response was truncated.
    """
    safe_limit = max(1, min(max_bytes, DEFAULT_ARTIFACT_READ_LIMIT_BYTES))
    truncated  = len(payload) > safe_limit
    text       = payload[:safe_limit].decode("utf-8", errors="replace")

    return text, truncated


def load_triage_skills(max_chars: int = DEFAULT_SKILLS_MAX_CHARS) -> dict[str, Any]:
    """
    Load the triage agent operating playbook from agent/SKILLS.md.

    Args:
        max_chars: Maximum number of characters to return.

    Returns:
        Dictionary containing bounded SKILLS.md text and metadata.

    Raises:
        FileNotFoundError: If agent/SKILLS.md is missing.
    """
    text       = SKILLS_PATH.read_text(encoding="utf-8")
    safe_limit = max(1, min(max_chars, DEFAULT_SKILLS_MAX_CHARS))
    truncated  = len(text) > safe_limit

    logger.info("Loaded triage skills playbook | path=%s chars=%d truncated=%s", SKILLS_PATH, len(text), truncated)

    return {
        "path": str(SKILLS_PATH),
        "sha256": hash_text(text),
        "chars_total": len(text),
        "chars_returned": min(len(text), safe_limit),
        "truncated": truncated,
        "text": text[:safe_limit],
    }


def compact_triage_report(report: Any) -> dict[str, Any]:
    """
    Convert a TriageReport into a compact MCP response payload.

    Args:
        report: TriageReport returned by agent.graph.run_triage.

    Returns:
        Dictionary containing the report summary and artifact URIs.
    """
    return {
        "status": "success",
        "agent_run_id": str(report.agent_run_id),
        "alert_key": report.alert.alert_key,
        "severity": report.alert.severity,
        "confidence": report.confidence,
        "top_hypothesis": report.top_hypothesis.title if report.top_hypothesis else None,
        "markdown_report_s3_uri": report.markdown_report_s3_uri,
        "json_report_s3_uri": report.json_report_s3_uri,
        "skills_playbook_path": str(SKILLS_PATH),
        "skills_playbook_sha256": hash_text(SKILLS_PATH.read_text(encoding="utf-8")),
        "approval_gated_actions": [action.model_dump(mode="json") for action in report.approval_gated_actions],
    }


# --- Defining MCP-Facing Tools
def mcp_list_alerts(status: str = "open", dt: str | None = None, limit: int = 20) -> dict[str, Any]:
    """
    List DQ alerts for MCP clients.

    Args:
        status: Alert status filter.
        dt: Optional business date in YYYY-MM-DD format.
        limit: Maximum alerts to return.

    Returns:
        Dictionary containing alert rows and query metadata.
    """
    logger.info("MCP list_alerts called | status=%s dt=%s limit=%s", status, dt, limit)

    return list_alerts(status=status, dt=dt, limit=limit)


def mcp_get_alert(alert_id: str | None = None, alert_key: str | None = None) -> dict[str, Any]:
    """
    Load one DQ alert for MCP clients.

    Args:
        alert_id: Optional alert UUID.
        alert_key: Optional stable alert key.

    Returns:
        Alert model serialized as a dictionary.
    """
    logger.info("MCP get_alert called | alert_id=%s alert_key=%s", alert_id, alert_key)
    alert = load_alert(alert_id=alert_id, alert_key=alert_key)

    return alert.model_dump(mode="json")


def mcp_run_guarded_sql(
    sql: str,
    alert_key: str = "",
    hard_limit: int = 100,
    require_date_filter: bool = True,
) -> dict[str, Any]:
    """
    Run guarded read-only ClickHouse SQL for MCP clients.

    Args:
        sql: Read-only SQL statement.
        alert_key: Optional alert key for audit correlation.
        hard_limit: Maximum rows returned.
        require_date_filter: Whether guarded large tables require date filters.

    Returns:
        SQL execution result serialized as a dictionary.
    """
    logger.info("MCP run_guarded_sql called | alert_key=%s hard_limit=%s", alert_key, hard_limit)
    result = run_guarded_sql(
        sql=sql,
        alert_key=alert_key,
        hard_limit=hard_limit,
        require_date_filter=require_date_filter,
    )

    return result.model_dump(mode="json")


def mcp_search_metadata_assets(
    query: str | None = None,
    domain: str | None = None,
    data_layer: str | None = None,
    certification_status: str | None = None,
    lifecycle_status: str | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    """
    Search the audited metadata trust registry with bounded filters.

    Args:
        query: Optional free-text search.
        domain: Optional data domain filter.
        data_layer: Optional raw, staging, or mart filter.
        certification_status: Optional certification filter.
        lifecycle_status: Optional active or deprecated filter.
        limit: Maximum metadata assets returned.

    Returns:
        Bounded metadata discovery response without internal registry fields.
    """
    logger.info(
        "MCP search_metadata_assets called | query=%s domain=%s layer=%s certification=%s limit=%d",
        str(query or "").strip(),
        str(domain or "").strip(),
        str(data_layer or "").strip(),
        str(certification_status or "").strip(),
        limit,
    )

    return search_metadata_assets(
        query=query,
        domain=domain,
        data_layer=data_layer,
        certification_status=certification_status,
        lifecycle_status=lifecycle_status,
        limit=limit,
    )


def mcp_get_metadata_asset(qualified_name: str) -> dict[str, Any]:
    """
    Fetch one exact trusted warehouse asset from the audited registry.

    Args:
        qualified_name: Fully qualified database.table asset identity.

    Returns:
        Public ownership, grain, SLA, sensitivity, certification, and lifecycle context.
    """
    logger.info("MCP get_metadata_asset called | qualified_name=%s", qualified_name)

    return get_metadata_asset(qualified_name=qualified_name)


def mcp_get_dbt_lineage(
    table_name: str,
    manifest_path: str | None = None,
    manifest_s3_uri: str | None = None,
    endpoint_url: str | None = None,
) -> dict[str, Any]:
    """
    Fetch dbt lineage metadata for one table.

    Args:
        table_name: Fully qualified ClickHouse table name.
        manifest_path: Optional local manifest path.
        manifest_s3_uri: Optional S3 URI for manifest.json.
        endpoint_url: Optional S3 endpoint override.

    Returns:
        dbt lineage summary dictionary.
    """
    logger.info("MCP get_dbt_lineage called | table=%s", table_name)

    return fetch_dbt_lineage(
        table_name=table_name,
        manifest_path=manifest_path,
        manifest_s3_uri=manifest_s3_uri,
        endpoint_url=endpoint_url,
    )


def mcp_get_dbt_blast_radius(
    table_name: str,
    manifest_path: str | None = None,
    manifest_s3_uri: str | None = None,
    endpoint_url: str | None = None,
    max_depth: int = DEFAULT_BLAST_RADIUS_DEPTH,
    max_nodes: int = DEFAULT_BLAST_RADIUS_NODES,
) -> dict[str, Any]:
    """
    Fetch bounded transitive downstream dbt impact for one table.

    Args:
        table_name: Fully qualified ClickHouse table name.
        manifest_path: Optional local manifest path.
        manifest_s3_uri: Optional S3 URI for manifest.json.
        endpoint_url: Optional S3 endpoint override.
        max_depth: Maximum downstream lineage depth.
        max_nodes: Maximum downstream nodes returned, excluding the root.

    Returns:
        Audited blast-radius summary without raw or compiled SQL.

    Raises:
        ValueError: If traversal bounds or manifest inputs are invalid.
        RuntimeError: If the manifest cannot be loaded.
    """
    logger.info(
        "MCP get_dbt_blast_radius called | table=%s max_depth=%d max_nodes=%d",
        table_name,
        max_depth,
        max_nodes,
    )

    return fetch_dbt_blast_radius(
        table_name=table_name,
        manifest_path=manifest_path,
        manifest_s3_uri=manifest_s3_uri,
        endpoint_url=endpoint_url,
        max_depth=max_depth,
        max_nodes=max_nodes,
    )


def mcp_get_dq_history(
    table_name: str,
    dt: str,
    check_name: str | None = None,
    lookback_days: int = 14,
    limit: int = 100,
) -> dict[str, Any]:
    """
    Fetch recent DQ history for one table/check/date window.

    Args:
        table_name: Fully qualified ClickHouse table name.
        dt: Target business date in YYYY-MM-DD format.
        check_name: Optional DQ check name.
        lookback_days: Days before dt to include.
        limit: Maximum rows returned.

    Returns:
        DQ history result dictionary.
    """
    logger.info("MCP get_dq_history called | table=%s dt=%s check=%s", table_name, dt, check_name)

    return fetch_dq_history(
        table_name=table_name,
        dt=parse_date(dt),
        check_name=check_name,
        lookback_days=lookback_days,
        limit=limit,
    )


def mcp_get_pipeline_runs(
    dt: str,
    lookback_days: int = 7,
    job_name: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """
    Fetch pipeline run history around one business date.

    Args:
        dt: Target business date in YYYY-MM-DD format.
        lookback_days: Days before dt to include.
        job_name: Optional job name filter.
        limit: Maximum rows returned.

    Returns:
        Pipeline run result dictionary.
    """
    logger.info("MCP get_pipeline_runs called | dt=%s job=%s", dt, job_name)

    return fetch_pipeline_runs(
        dt=parse_date(dt),
        lookback_days=lookback_days,
        job_name=job_name,
        limit=limit,
    )


def mcp_run_triage(
    alert_id: str | None = None,
    alert_key: str | None = None,
    confidence_threshold: float = DEFAULT_CONFIDENCE_TARGET,
    max_evidence_iterations: int = DEFAULT_MAX_EVIDENCE_LOOP,
    manifest_path: str | None = None,
    manifest_s3_uri: str | None = None,
    endpoint_url: str | None = None,
    artifacts_bucket: str | None = None,
    artifacts_prefix: str = "agent-reports",
) -> dict[str, Any]:
    """
    Run one agentic triage workflow for MCP clients.

    Args:
        alert_id: Optional alert UUID.
        alert_key: Optional stable alert key.
        confidence_threshold: Confidence target before stopping evidence collection.
        max_evidence_iterations: Maximum bounded evidence loops.
        manifest_path: Optional local dbt manifest path.
        manifest_s3_uri: Optional S3 URI for dbt manifest.json.
        endpoint_url: Optional S3 endpoint override.
        artifacts_bucket: Optional report artifact bucket override.
        artifacts_prefix: S3 prefix for report artifacts.

    Returns:
        Compact triage report payload with S3 artifact URIs.

    Raises:
        ValueError: If neither alert_id nor alert_key is provided.
    """
    if not alert_id and not alert_key:
        raise ValueError("Provide alert_id or alert_key.")

    logger.info("MCP run_triage called | alert_id=%s alert_key=%s", alert_id, alert_key)

    config = TriageRuntimeConfig(
        manifest_path=manifest_path,
        manifest_s3_uri=manifest_s3_uri,
        s3_endpoint_url=endpoint_url,
        artifacts_bucket=artifacts_bucket,
        artifacts_prefix=artifacts_prefix,
    )
    report = run_triage(
        alert_id=alert_id,
        alert_key=alert_key,
        confidence_threshold=confidence_threshold,
        max_evidence_iterations=max_evidence_iterations,
        config=config,
    )

    return compact_triage_report(report)


def mcp_get_triage_skills(
    max_chars: int = DEFAULT_SKILLS_MAX_CHARS,
    agent_run_id: str | None = None,
) -> dict[str, Any]:
    """
    Return the MCP-facing triage operating playbook.

    Args:
        max_chars: Maximum characters to return from agent/SKILLS.md.
        agent_run_id: Optional agent run UUID for audit correlation.

    Returns:
        Dictionary containing bounded playbook text and metadata.
    """
    resolved_agent_run_id = UUID(str(agent_run_id)) if agent_run_id else uuid4()
    started_monotonic     = time.monotonic()
    clickhouse_client     = build_clickhouse_client()

    logger.info("MCP get_triage_skills called | max_chars=%s", max_chars)

    try:
        result      = load_triage_skills(max_chars=max_chars)
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)

        write_agent_audit_event(
            client=clickhouse_client,
            action="get_triage_skills",
            status="success",
            agent_run_id=resolved_agent_run_id,
            tool_name=TOOL_NAME,
            duration_ms=duration_ms,
            input_payload={"max_chars": max_chars},
            output_payload={
                "path": result["path"],
                "sha256": result["sha256"],
                "chars_returned": result["chars_returned"],
                "truncated": result["truncated"],
            },
            row_count=1,
        )

        return result

    except Exception as exc:
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)
        logger.exception("MCP get_triage_skills failed")

        write_agent_audit_event(
            client=clickhouse_client,
            action="get_triage_skills",
            status="failed",
            agent_run_id=resolved_agent_run_id,
            tool_name=TOOL_NAME,
            duration_ms=duration_ms,
            input_payload={"max_chars": max_chars},
            output_payload={"error_type": type(exc).__name__},
            error_message=str(exc),
        )

        raise


def mcp_read_report_artifact(
    s3_uri: str,
    max_bytes: int = DEFAULT_ARTIFACT_READ_LIMIT_BYTES,
    endpoint_url: str | None = None,
    agent_run_id: str | None = None,
) -> dict[str, Any]:
    """
    Read a bounded report artifact from local S3-compatible storage.

    Args:
        s3_uri: Report artifact S3 URI.
        max_bytes: Maximum response bytes.
        endpoint_url: Optional S3 endpoint override.
        agent_run_id: Optional agent run UUID for audit correlation.

    Returns:
        Dictionary containing bounded artifact text and metadata.
    """
    bucket, key           = ensure_report_s3_uri_allowed(s3_uri)
    resolved_agent_run_id = UUID(str(agent_run_id)) if agent_run_id else uuid4()
    started_monotonic     = time.monotonic()
    client                = build_s3_client(endpoint_url=endpoint_url)
    clickhouse_client     = build_clickhouse_client()

    logger.info("MCP read_report_artifact called | uri=%s max_bytes=%s", s3_uri, max_bytes)

    try:
        response    = client.get_object(Bucket=bucket, Key=key)
        payload     = response["Body"].read()
        text, is_truncated = bounded_text(payload=payload, max_bytes=max_bytes)
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)
        result      = {
            "status": "success",
            "s3_uri": s3_uri,
            "bucket": bucket,
            "key": key,
            "bytes_read": len(payload),
            "returned_bytes": len(text.encode("utf-8")),
            "truncated": is_truncated,
            "text": text,
        }

        write_agent_audit_event(
            client=clickhouse_client,
            action="read_report_artifact",
            status="success",
            agent_run_id=resolved_agent_run_id,
            tool_name=TOOL_NAME,
            duration_ms=duration_ms,
            input_payload={"s3_uri": s3_uri, "max_bytes": max_bytes},
            output_payload={
                "bytes_read": len(payload),
                "returned_bytes": result["returned_bytes"],
                "truncated": is_truncated,
            },
            row_count=1,
            report_s3_uri=s3_uri,
        )

        return result

    except Exception as exc:
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)
        logger.exception("MCP report artifact read failed | uri=%s", s3_uri)

        write_agent_audit_event(
            client=clickhouse_client,
            action="read_report_artifact",
            status="failed",
            agent_run_id=resolved_agent_run_id,
            tool_name=TOOL_NAME,
            duration_ms=duration_ms,
            input_payload={"s3_uri": s3_uri, "max_bytes": max_bytes},
            output_payload={"error_type": type(exc).__name__},
            error_message=str(exc),
            report_s3_uri=s3_uri,
        )

        raise


# --- Defining Server Registration
def register_mcp_tools(server: Any) -> Any:
    """
    Register project tools on a FastMCP server instance.

    Args:
        server: FastMCP-compatible server object.

    Returns:
        The same server instance after tool registration.
    """
    server.tool(name="list_alerts")(mcp_list_alerts)
    server.tool(name="get_alert")(mcp_get_alert)
    server.tool(name="search_metadata_assets")(mcp_search_metadata_assets)
    server.tool(name="get_metadata_asset")(mcp_get_metadata_asset)
    server.tool(name="run_guarded_sql")(mcp_run_guarded_sql)
    server.tool(name="get_dbt_lineage")(mcp_get_dbt_lineage)
    server.tool(name="get_dbt_blast_radius")(mcp_get_dbt_blast_radius)
    server.tool(name="get_dq_history")(mcp_get_dq_history)
    server.tool(name="get_pipeline_runs")(mcp_get_pipeline_runs)
    server.tool(name="run_triage")(mcp_run_triage)
    server.tool(name="get_triage_skills")(mcp_get_triage_skills)
    server.tool(name="read_report_artifact")(mcp_read_report_artifact)

    logger.info("Registered MCP tools | count=%d", len(MCP_TOOL_REGISTRY))

    return server


def build_mcp_server() -> Any:
    """
    Build the FastMCP server for this project.

    Returns:
        FastMCP server with project tools registered.

    Raises:
        RuntimeError: If the optional mcp package is not installed.
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError("Install the optional 'mcp' package to run the MCP server.") from exc

    server = FastMCP(SERVER_NAME)

    return register_mcp_tools(server)


# --- Defining CLI Entrypoint
def build_parser() -> argparse.ArgumentParser:
    """
    Build the command-line parser for MCP server operations.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(description="Run or inspect the Agentic DQ MCP server.")

    parser.add_argument("--list-tools", action="store_true", help="Print registered MCP tool metadata without starting the server.")
    parser.add_argument("--transport", default="stdio", choices=["stdio"], help="MCP transport for FastMCP runtime.")

    return parser


def main() -> None:
    """
    Parse CLI arguments and run the MCP server or registry inspection.

    Returns:
        None.
    """
    parser = build_parser()
    args   = parser.parse_args()

    if args.list_tools:
        print(json.dumps({"server": SERVER_NAME, "tools": tool_registry_as_dicts()}, indent=2))
        return

    server = build_mcp_server()
    logger.info("Starting MCP server | name=%s transport=%s", SERVER_NAME, args.transport)

    # stdio keeps the server local and connector-friendly for portfolio demos.
    server.run(transport=args.transport)


# --- Running CLI Entrypoint
if __name__ == "__main__":
    main()
