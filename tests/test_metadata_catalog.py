####
## Metadata Catalog Tool Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Validate bounded metadata discovery, public fields, and audit behavior."""

# --- Importing Libraries
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from agent.tools import metadata_catalog


# --- Defining Test Doubles
class FakeQueryResult:
    """
    Represent one clickhouse-connect query result used by metadata tests.

    Attributes:
        column_names: Result column names.
        result_rows: Result row tuples.
    """

    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        """
        Initialize a fake query result.

        Args:
            rows: Result tuples aligned to PUBLIC_METADATA_COLUMNS.

        Returns:
            None.
        """
        self.column_names = list(metadata_catalog.PUBLIC_METADATA_COLUMNS)
        self.result_rows  = rows


class FakeClickHouseClient:
    """
    Capture metadata queries without connecting to ClickHouse.

    Attributes:
        result: Query result or exception returned by query().
        queries: SQL statements received by query().
    """

    def __init__(self, result: FakeQueryResult | Exception) -> None:
        """
        Initialize the fake client.

        Args:
            result: Query result or exception to raise.

        Returns:
            None.
        """
        self.result  = result
        self.queries: list[str] = []

    def query(self, sql: str) -> FakeQueryResult:
        """
        Capture SQL and return the configured query result.

        Args:
            sql: Metadata SELECT statement.

        Returns:
            Configured FakeQueryResult.

        Raises:
            Exception: Configured query error.
        """
        self.queries.append(sql)

        if isinstance(self.result, Exception):
            raise self.result

        return self.result


# --- Defining Fixtures
def metadata_row(qualified_name: str = "dq.fct_orders_daily") -> tuple[Any, ...]:
    """
    Build one representative public metadata registry row.

    Args:
        qualified_name: Fully qualified asset identity.

    Returns:
        Tuple aligned to PUBLIC_METADATA_COLUMNS.
    """
    database_name, table_name = qualified_name.split(".", maxsplit=1)

    values = {
        "qualified_name": qualified_name.encode("utf-8"),
        "database_name": database_name,
        "table_name": table_name,
        "display_name": "Daily Orders Fact",
        "description": "Curated daily order metrics grouped by date, country, and channel.",
        "dataset": "orders",
        "domain": "commerce",
        "data_layer": "mart",
        "technical_owner": "Analytics Engineering",
        "business_owner": "Commerce Analytics",
        "grain": "One row per business date, country, and channel.",
        "refresh_frequency": "daily",
        "sla_time": "01:15",
        "sla_timezone": "Asia/Bangkok",
        "criticality": "critical",
        "sensitivity": "internal",
        "contains_pii": 0,
        "certification_status": "certified",
        "lifecycle_status": "active",
        "tags": [b"analytics-ready", "orders"],
        "synced_at": datetime(2026, 8, 7, 8, 0, tzinfo=timezone.utc),
    }

    return tuple(values[column] for column in metadata_catalog.PUBLIC_METADATA_COLUMNS)


# --- Testing SQL Guardrails
def test_build_metadata_catalog_sql_is_static_bounded_and_filtered() -> None:
    """
    Ensure metadata search remains read-only, escaped, active-only, and bounded.

    Returns:
        None.
    """
    sql = metadata_catalog.build_metadata_catalog_sql(
        query="owner's orders",
        domain="commerce",
        data_layer="mart",
        certification_status="certified",
        lifecycle_status="active",
        limit=10,
    )

    assert "FROM dq.metadata_assets FINAL" in sql
    assert "is_active = 1" in sql
    assert "positionCaseInsensitiveUTF8" in sql
    assert "owner\\'s orders" in sql
    assert "domain = 'commerce'" in sql
    assert "data_layer = 'mart'" in sql
    assert "certification_status = 'certified'" in sql
    assert "lifecycle_status = 'active'" in sql
    assert "LIMIT 10" in sql
    assert "config_sha256" not in sql
    assert "source_config_path" not in sql


@pytest.mark.parametrize(
    ("kwargs", "error_pattern"),
    [
        ({"limit": 0}, "limit must be between"),
        ({"limit": 101}, "limit must be between"),
        ({"data_layer": "gold"}, "data_layer must be one of"),
        ({"certification_status": "trusted"}, "certification_status must be one of"),
        ({"domain": "commerce; DROP TABLE dq.alerts"}, "Unsafe ClickHouse identifier"),
        ({"qualified_name": "dq.raw_orders; DROP TABLE dq.alerts"}, "database.table format"),
        ({"query": "x" * 121}, "cannot exceed 120"),
    ],
)
def test_build_metadata_catalog_sql_rejects_unbounded_or_unsafe_inputs(
    kwargs: dict[str, Any],
    error_pattern: str,
) -> None:
    """
    Ensure public metadata filters cannot become arbitrary SQL input.

    Args:
        kwargs: Invalid SQL builder arguments.
        error_pattern: Expected validation error fragment.

    Returns:
        None.
    """
    with pytest.raises(ValueError, match=error_pattern):
        metadata_catalog.build_metadata_catalog_sql(**kwargs)


# --- Testing Tool Results And Audit
def test_search_metadata_assets_returns_public_contract_and_audits(monkeypatch) -> None:
    """
    Verify metadata search normalizes ClickHouse values and records a safe audit event.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    client       = FakeClickHouseClient(FakeQueryResult([metadata_row()]))
    audit_events: list[dict[str, Any]] = []

    monkeypatch.setattr(metadata_catalog, "build_clickhouse_client", lambda **_: client)
    monkeypatch.setattr(
        metadata_catalog,
        "write_agent_audit_event",
        lambda **kwargs: audit_events.append(kwargs),
    )

    result = metadata_catalog.search_metadata_assets(
        query="orders",
        data_layer="mart",
        certification_status="certified",
        limit=5,
    )

    assert result["status"] == "success"
    assert result["row_count"] == 1
    assert result["assets"][0]["qualified_name"] == "dq.fct_orders_daily"
    assert result["assets"][0]["tags"] == ["analytics-ready", "orders"]
    assert result["assets"][0]["contains_pii"] is False
    assert "config_sha256" not in result["assets"][0]
    assert "source_config_path" not in result["assets"][0]
    assert audit_events[0]["action"] == "search_metadata_assets"
    assert audit_events[0]["status"] == "success"
    assert audit_events[0]["row_count"] == 1
    assert audit_events[0]["output_payload"]["qualified_names"] == ["dq.fct_orders_daily"]


def test_get_metadata_asset_returns_exact_asset_and_raises_for_missing(monkeypatch) -> None:
    """
    Verify exact lookup is bounded to one asset and missing assets fail explicitly.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    audit_events: list[dict[str, Any]] = []
    client = FakeClickHouseClient(FakeQueryResult([metadata_row("dq.raw_orders")]))

    monkeypatch.setattr(metadata_catalog, "build_clickhouse_client", lambda **_: client)
    monkeypatch.setattr(
        metadata_catalog,
        "write_agent_audit_event",
        lambda **kwargs: audit_events.append(kwargs),
    )

    asset = metadata_catalog.get_metadata_asset("dq.raw_orders")

    assert asset["qualified_name"] == "dq.raw_orders"
    assert "qualified_name = 'dq.raw_orders'" in client.queries[0]
    assert "LIMIT 1" in client.queries[0]
    assert audit_events[0]["action"] == "get_metadata_asset"

    client.result = FakeQueryResult([])

    with pytest.raises(LookupError, match="Metadata asset not found"):
        metadata_catalog.get_metadata_asset("dq.missing_table")

    assert audit_events[-1]["action"] == "get_metadata_asset"
    assert audit_events[-1]["status"] == "not_found"
    assert audit_events[-1]["row_count"] == 0


def test_search_metadata_assets_audits_query_failure(monkeypatch) -> None:
    """
    Ensure failed metadata reads remain visible in the agent audit trail.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    client       = FakeClickHouseClient(RuntimeError("query failed"))
    audit_events: list[dict[str, Any]] = []

    monkeypatch.setattr(metadata_catalog, "build_clickhouse_client", lambda **_: client)
    monkeypatch.setattr(
        metadata_catalog,
        "write_agent_audit_event",
        lambda **kwargs: audit_events.append(kwargs),
    )

    with pytest.raises(RuntimeError, match="query failed"):
        metadata_catalog.search_metadata_assets(query="orders")

    assert audit_events[0]["action"] == "search_metadata_assets"
    assert audit_events[0]["status"] == "failed"
    assert audit_events[0]["output_payload"] == {"error_type": "RuntimeError"}
