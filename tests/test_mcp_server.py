####
## MCP Server Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import pytest

from agent.mcp import server as mcp_server
from agent.mcp.server import (
    DEFAULT_ARTIFACT_READ_LIMIT_BYTES,
    MCP_TOOL_REGISTRY,
    bounded_text,
    ensure_report_s3_uri_allowed,
    load_triage_skills,
    register_mcp_tools,
    tool_registry_as_dicts,
)


# --- Defining Test Fakes
class FakeMcpServer:
    """
    Minimal FastMCP-like object used to verify local tool registration.

    Attributes:
        tools: Mapping of registered tool names to Python callables.
    """

    def __init__(self) -> None:
        """
        Initialize an empty fake MCP server.

        Returns:
            None.
        """
        self.tools: dict[str, object] = {}

    def tool(self, name: str):
        """
        Return a decorator that records a tool function under the provided name.

        Args:
            name: Public MCP tool name.

        Returns:
            Decorator function that records the callable and returns it unchanged.
        """
        def decorator(func):
            """
            Register a function on the fake server.

            Args:
                func: Tool function being registered.

            Returns:
                The original function.
            """
            self.tools[name] = func

            return func

        return decorator


# --- Defining Tests
def test_mcp_tool_registry_exposes_expected_tools() -> None:
    """
    Validate the MCP tool surface expected by portfolio and external client demos.

    Returns:
        None.
    """
    tool_names = {tool.name for tool in MCP_TOOL_REGISTRY}

    assert tool_names == {
        "list_alerts",
        "get_alert",
        "search_metadata_assets",
        "get_metadata_asset",
        "run_guarded_sql",
        "get_dbt_lineage",
        "get_dbt_blast_radius",
        "get_dq_history",
        "get_pipeline_runs",
        "run_triage",
        "get_triage_skills",
        "read_report_artifact",
    }


def test_tool_registry_as_dicts_is_serializable() -> None:
    """
    Validate registry inspection output for the Makefile smoke target.

    Returns:
        None.
    """
    payload = tool_registry_as_dicts()

    assert len(payload) == len(MCP_TOOL_REGISTRY)
    assert payload[0]["name"] == "list_alerts"
    assert "purpose" in payload[0]
    assert "audit_behavior" in payload[0]


def test_register_mcp_tools_uses_registry_names() -> None:
    """
    Validate MCP registration without requiring the external mcp package.

    Returns:
        None.
    """
    fake_server = FakeMcpServer()

    register_mcp_tools(fake_server)

    assert set(fake_server.tools) == {tool.name for tool in MCP_TOOL_REGISTRY}
    assert callable(fake_server.tools["run_guarded_sql"])
    assert callable(fake_server.tools["search_metadata_assets"])
    assert callable(fake_server.tools["get_metadata_asset"])
    assert callable(fake_server.tools["get_dbt_blast_radius"])
    assert callable(fake_server.tools["get_triage_skills"])
    assert callable(fake_server.tools["read_report_artifact"])


def test_mcp_metadata_tools_delegate_to_audited_catalog(monkeypatch) -> None:
    """
    Ensure MCP metadata discovery reuses audited tools without custom SQL logic.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    captured_search: dict[str, object] = {}
    captured_get: dict[str, object]    = {}

    def fake_search_metadata_assets(**kwargs) -> dict[str, object]:
        """
        Capture delegated metadata search arguments.

        Args:
            **kwargs: Metadata search arguments.

        Returns:
            Minimal metadata discovery result.
        """
        captured_search.update(kwargs)

        return {"status": "success", "assets": [], "row_count": 0}

    def fake_get_metadata_asset(**kwargs) -> dict[str, object]:
        """
        Capture delegated exact metadata arguments.

        Args:
            **kwargs: Exact metadata lookup arguments.

        Returns:
            Minimal exact metadata asset.
        """
        captured_get.update(kwargs)

        return {"qualified_name": kwargs["qualified_name"]}

    monkeypatch.setattr(mcp_server, "search_metadata_assets", fake_search_metadata_assets)
    monkeypatch.setattr(mcp_server, "get_metadata_asset", fake_get_metadata_asset)

    search_result = mcp_server.mcp_search_metadata_assets(
        query="orders",
        domain="commerce",
        data_layer="mart",
        certification_status="certified",
        lifecycle_status="active",
        limit=10,
    )
    asset_result = mcp_server.mcp_get_metadata_asset("dq.fct_orders_daily")

    assert search_result["row_count"] == 0
    assert captured_search == {
        "query": "orders",
        "domain": "commerce",
        "data_layer": "mart",
        "certification_status": "certified",
        "lifecycle_status": "active",
        "limit": 10,
    }
    assert asset_result["qualified_name"] == "dq.fct_orders_daily"
    assert captured_get == {"qualified_name": "dq.fct_orders_daily"}


def test_mcp_get_dbt_blast_radius_delegates_to_audited_tool(monkeypatch) -> None:
    """
    Ensure MCP reuses the bounded audited blast-radius tool without custom traversal logic.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    captured: dict[str, object] = {}

    def fake_fetch_dbt_blast_radius(**kwargs) -> dict[str, object]:
        """
        Capture delegated tool arguments and return a sanitized response.

        Args:
            **kwargs: Blast-radius tool arguments.

        Returns:
            Minimal sanitized impact result.
        """
        captured.update(kwargs)

        return {
            "table_name": kwargs["table_name"],
            "matched": True,
            "impacted_assets": [],
            "impacted_tests": [],
            "unresolved_nodes": [],
            "truncated": False,
        }

    monkeypatch.setattr(
        mcp_server,
        "fetch_dbt_blast_radius",
        fake_fetch_dbt_blast_radius,
    )

    result = mcp_server.mcp_get_dbt_blast_radius(
        table_name="dq.raw_orders",
        manifest_s3_uri="s3://dq-artifacts/dbt-artifacts/orders/latest/manifest.json",
        max_depth=4,
        max_nodes=80,
    )

    assert result["table_name"] == "dq.raw_orders"
    assert captured == {
        "table_name": "dq.raw_orders",
        "manifest_path": None,
        "manifest_s3_uri": "s3://dq-artifacts/dbt-artifacts/orders/latest/manifest.json",
        "endpoint_url": None,
        "max_depth": 4,
        "max_nodes": 80,
    }


def test_ensure_report_s3_uri_allowed_accepts_project_artifact_bucket() -> None:
    """
    Validate that report artifact reads are limited to approved local project buckets.

    Returns:
        None.
    """
    bucket, key = ensure_report_s3_uri_allowed("s3://dq-artifacts/agent-reports/report.json")

    assert bucket == "dq-artifacts"
    assert key == "agent-reports/report.json"


def test_ensure_report_s3_uri_allowed_rejects_non_project_bucket() -> None:
    """
    Validate that MCP report reads cannot browse arbitrary buckets.

    Returns:
        None.
    """
    with pytest.raises(ValueError, match="bucket is not allowed"):
        ensure_report_s3_uri_allowed("s3://personal-files/secrets.json")


def test_bounded_text_caps_large_artifact_payloads() -> None:
    """
    Validate that report artifact reads cannot return unbounded text.

    Returns:
        None.
    """
    text, truncated = bounded_text(payload=b"abcdef", max_bytes=3)

    assert text == "abc"
    assert truncated is True


def test_bounded_text_uses_default_ceiling() -> None:
    """
    Validate that caller-provided max_bytes cannot exceed the project ceiling.

    Returns:
        None.
    """
    payload = b"a" * (DEFAULT_ARTIFACT_READ_LIMIT_BYTES + 10)

    text, truncated = bounded_text(payload=payload, max_bytes=DEFAULT_ARTIFACT_READ_LIMIT_BYTES + 999)

    assert len(text) == DEFAULT_ARTIFACT_READ_LIMIT_BYTES
    assert truncated is True


def test_load_triage_skills_returns_bounded_playbook_metadata() -> None:
    """
    Validate that MCP clients can inspect the same triage playbook used by the project.

    Returns:
        None.
    """
    payload = load_triage_skills(max_chars=200)

    assert payload["path"].endswith("agent\\SKILLS.md") or payload["path"].endswith("agent/SKILLS.md")
    assert len(payload["sha256"]) == 64
    assert payload["chars_returned"] <= 200
    assert "Agentic Data Quality Triage Skills" in payload["text"]
