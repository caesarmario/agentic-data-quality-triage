####
## dbt Lineage Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import pytest

from agent.tools import dbt_lineage as lineage_module
from agent.tools.dbt_lineage import (
    build_blast_radius_summary,
    build_lineage_summary,
    fetch_dbt_blast_radius,
    normalize_relation_name,
    parse_s3_uri,
    validate_blast_radius_bounds,
)


# --- Defining Functions
@pytest.fixture
def minimal_manifest() -> dict:
    """
    Build a minimal dbt manifest fixture for lineage parser tests.

    Returns:
        Dictionary shaped like the subset of dbt manifest needed by the lineage parser.
    """
    return {
        "nodes": {
            "model.project.stg_orders": {
                "resource_type": "model",
                "name": "stg_orders",
                "alias": "stg_orders",
                "schema": "dq",
                "relation_name": "`dq`.`stg_orders`",
                "description": "staging orders",
                "path": "staging/stg_orders.sql",
                "original_file_path": "models/staging/stg_orders.sql",
            },
            "model.project.fct_orders_daily": {
                "resource_type": "model",
                "name": "fct_orders_daily",
                "alias": "fct_orders_daily",
                "schema": "dq",
                "relation_name": "`dq`.`fct_orders_daily`",
                "description": "daily mart",
                "path": "marts/fct_orders_daily.sql",
                "original_file_path": "models/marts/fct_orders_daily.sql",
            },
            "model.project.mart_orders_weekly": {
                "resource_type": "model",
                "name": "mart_orders_weekly",
                "alias": "mart_orders_weekly",
                "schema": "dq",
                "relation_name": "`dq`.`mart_orders_weekly`",
                "description": "weekly reporting mart",
                "path": "marts/mart_orders_weekly.sql",
                "original_file_path": "models/marts/mart_orders_weekly.sql",
            },
            "test.project.not_null_stg_orders_order_id": {
                "resource_type": "test",
                "name": "not_null_stg_orders_order_id",
                "alias": "not_null_stg_orders_order_id",
                "schema": "dq_dbt_test__audit",
                "relation_name": None,
                "description": "",
                "path": "not_null_stg_orders_order_id.sql",
                "original_file_path": "models/staging/schema.yml",
            },
            "test.project.not_null_mart_orders_weekly_dt": {
                "resource_type": "test",
                "name": "not_null_mart_orders_weekly_dt",
                "alias": "not_null_mart_orders_weekly_dt",
                "schema": "dq_dbt_test__audit",
                "relation_name": None,
                "description": "",
                "path": "not_null_mart_orders_weekly_dt.sql",
                "original_file_path": "models/marts/schema.yml",
            },
        },
        "sources": {
            "source.project.raw.raw_orders": {
                "resource_type": "source",
                "name": "raw_orders",
                "schema": "dq",
                "relation_name": "`dq`.`raw_orders`",
                "description": "raw source",
                "path": "models/sources.yml",
                "original_file_path": "models/sources.yml",
            }
        },
        "parent_map": {
            "model.project.stg_orders": ["source.project.raw.raw_orders"],
            "model.project.fct_orders_daily": ["model.project.stg_orders"],
            "model.project.mart_orders_weekly": ["model.project.fct_orders_daily"],
            "test.project.not_null_stg_orders_order_id": ["model.project.stg_orders"],
            "test.project.not_null_mart_orders_weekly_dt": ["model.project.mart_orders_weekly"],
        },
        "child_map": {
            "source.project.raw.raw_orders": ["model.project.stg_orders"],
            "model.project.stg_orders": ["model.project.fct_orders_daily", "test.project.not_null_stg_orders_order_id"],
            "model.project.fct_orders_daily": ["model.project.mart_orders_weekly"],
            "model.project.mart_orders_weekly": ["test.project.not_null_mart_orders_weekly_dt"],
            "test.project.not_null_stg_orders_order_id": [],
            "test.project.not_null_mart_orders_weekly_dt": [],
        },
    }


def test_normalize_relation_name_removes_clickhouse_quotes() -> None:
    """
    Validate ClickHouse relation-name normalization.

    Returns:
        None.
    """
    assert normalize_relation_name("`dq`.`stg_orders`") == "dq.stg_orders"


def test_parse_s3_uri_returns_bucket_and_key() -> None:
    """
    Validate S3 URI parsing for manifest artifact paths.

    Returns:
        None.
    """
    bucket, key = parse_s3_uri("s3://dq-artifacts/dbt/manifest.json")

    assert bucket == "dq-artifacts"
    assert key == "dbt/manifest.json"


def test_parse_s3_uri_rejects_invalid_uri() -> None:
    """
    Validate that non-S3 manifest URIs are rejected.

    Returns:
        None.
    """
    with pytest.raises(ValueError, match="Expected S3 URI"):
        parse_s3_uri("warehouse/dbt/target/manifest.json")


def test_build_lineage_summary_matches_model(minimal_manifest: dict) -> None:
    """
    Validate parent, child, and test extraction for a matched dbt model.

    Args:
        minimal_manifest: Minimal manifest fixture.

    Returns:
        None.
    """
    lineage = build_lineage_summary(manifest=minimal_manifest, table_name="dq.stg_orders")

    assert lineage["matched"] is True
    assert lineage["node"]["unique_id"] == "model.project.stg_orders"
    assert [item["unique_id"] for item in lineage["parents"]] == ["source.project.raw.raw_orders"]
    assert [item["unique_id"] for item in lineage["children"]] == ["model.project.fct_orders_daily"]
    assert [item["unique_id"] for item in lineage["tests"]] == ["test.project.not_null_stg_orders_order_id"]


def test_build_lineage_summary_returns_unmatched(minimal_manifest: dict) -> None:
    """
    Validate unmatched table behavior.

    Args:
        minimal_manifest: Minimal manifest fixture.

    Returns:
        None.
    """
    lineage = build_lineage_summary(manifest=minimal_manifest, table_name="dq.unknown_table")

    assert lineage["matched"] is False
    assert lineage["parents"] == []
    assert lineage["children"] == []
    assert lineage["tests"] == []


# --- Defining Blast Radius Tests
def test_build_blast_radius_summary_returns_transitive_assets_and_tests(
    minimal_manifest: dict,
) -> None:
    """
    Validate breadth-first downstream impact across multiple dbt levels.

    Args:
        minimal_manifest: Minimal manifest fixture.

    Returns:
        None.
    """
    result = build_blast_radius_summary(
        manifest=minimal_manifest,
        table_name="dq.raw_orders",
        max_depth=5,
        max_nodes=20,
    )

    assert result["matched"] is True
    assert result["truncated"] is False
    assert result["impacted_asset_count"] == 3
    assert result["impacted_test_count"] == 2
    assert result["unresolved_node_count"] == 0
    assert result["max_depth_reached"] == 4
    assert result["resource_type_counts"] == {"model": 3, "test": 2}
    assert [item["name"] for item in result["impacted_assets"]] == [
        "stg_orders",
        "fct_orders_daily",
        "mart_orders_weekly",
    ]
    assert [item["depth"] for item in result["impacted_assets"]] == [1, 2, 3]
    assert result["impacted_assets"][2]["lineage_path"] == [
        "source.project.raw.raw_orders",
        "model.project.stg_orders",
        "model.project.fct_orders_daily",
        "model.project.mart_orders_weekly",
    ]
    assert "raw_code" not in result["impacted_assets"][0]
    assert "compiled_code" not in result["impacted_assets"][0]


def test_build_blast_radius_summary_reports_depth_truncation(
    minimal_manifest: dict,
) -> None:
    """
    Validate explicit truncation when deeper descendants exist.

    Args:
        minimal_manifest: Minimal manifest fixture.

    Returns:
        None.
    """
    result = build_blast_radius_summary(
        manifest=minimal_manifest,
        table_name="dq.raw_orders",
        max_depth=2,
        max_nodes=20,
    )

    assert result["truncated"] is True
    assert result["max_depth_reached"] == 2
    assert [item["name"] for item in result["impacted_assets"]] == [
        "stg_orders",
        "fct_orders_daily",
    ]
    assert "Result truncated" in result["summary"]


def test_build_blast_radius_summary_reports_node_limit_truncation(
    minimal_manifest: dict,
) -> None:
    """
    Validate the hard downstream node limit excludes the root node.

    Args:
        minimal_manifest: Minimal manifest fixture.

    Returns:
        None.
    """
    result = build_blast_radius_summary(
        manifest=minimal_manifest,
        table_name="dq.raw_orders",
        max_depth=5,
        max_nodes=1,
    )

    assert result["truncated"] is True
    assert result["total_impacted_nodes"] == 1
    assert result["impacted_assets"][0]["name"] == "stg_orders"


def test_build_blast_radius_summary_is_cycle_safe(minimal_manifest: dict) -> None:
    """
    Validate malformed cyclic child maps cannot create an infinite traversal.

    Args:
        minimal_manifest: Minimal manifest fixture.

    Returns:
        None.
    """
    minimal_manifest["child_map"]["model.project.mart_orders_weekly"].append(
        "model.project.stg_orders"
    )

    result = build_blast_radius_summary(
        manifest=minimal_manifest,
        table_name="dq.stg_orders",
        max_depth=10,
        max_nodes=20,
    )

    returned_ids = [
        item["unique_id"]
        for collection in (
            result["impacted_assets"],
            result["impacted_tests"],
            result["unresolved_nodes"],
        )
        for item in collection
    ]

    assert result["truncated"] is False
    assert len(returned_ids) == len(set(returned_ids))
    assert "model.project.stg_orders" not in returned_ids


@pytest.mark.parametrize(
    ("max_depth", "max_nodes", "expected_message"),
    [
        (0, 10, "max_depth"),
        (11, 10, "max_depth"),
        (5, 0, "max_nodes"),
        (5, 251, "max_nodes"),
    ],
)
def test_validate_blast_radius_bounds_rejects_unsafe_values(
    max_depth: int,
    max_nodes: int,
    expected_message: str,
) -> None:
    """
    Validate direct callers cannot bypass traversal limits.

    Args:
        max_depth: Candidate downstream depth.
        max_nodes: Candidate downstream node limit.
        expected_message: Error field expected in the exception.

    Returns:
        None.
    """
    with pytest.raises(ValueError, match=expected_message):
        validate_blast_radius_bounds(max_depth=max_depth, max_nodes=max_nodes)


def test_fetch_dbt_blast_radius_writes_bounded_audit_metadata(
    monkeypatch,
    minimal_manifest: dict,
) -> None:
    """
    Validate blast-radius tool execution records counts without raw manifest data.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        minimal_manifest: Minimal manifest fixture.

    Returns:
        None.
    """
    captured: dict[str, object] = {}

    monkeypatch.setattr(lineage_module, "build_clickhouse_client", lambda **kwargs: object())
    monkeypatch.setattr(
        lineage_module,
        "load_manifest",
        lambda **kwargs: (minimal_manifest, "s3://dq-artifacts/dbt/manifest.json"),
    )
    monkeypatch.setattr(
        lineage_module,
        "write_agent_audit_event",
        lambda **kwargs: captured.update(kwargs),
    )

    result = fetch_dbt_blast_radius(
        table_name="dq.raw_orders",
        max_depth=5,
        max_nodes=20,
    )

    assert result["manifest_source"] == "s3://dq-artifacts/dbt/manifest.json"
    assert captured["action"] == "fetch_dbt_blast_radius"
    assert captured["status"] == "success"
    assert captured["tool_name"] == "dbt_blast_radius"
    assert captured["row_count"] == result["total_impacted_nodes"]
    assert captured["input_payload"]["max_depth"] == 5
    assert captured["output_payload"]["impacted_asset_count"] == 3
    assert "impacted_assets" not in captured["output_payload"]
