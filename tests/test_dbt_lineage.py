####
## dbt Lineage Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import pytest

from agent.tools.dbt_lineage import build_lineage_summary, normalize_relation_name, parse_s3_uri


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
            "test.project.not_null_stg_orders_order_id": ["model.project.stg_orders"],
        },
        "child_map": {
            "source.project.raw.raw_orders": ["model.project.stg_orders"],
            "model.project.stg_orders": ["model.project.fct_orders_daily", "test.project.not_null_stg_orders_order_id"],
            "model.project.fct_orders_daily": [],
            "test.project.not_null_stg_orders_order_id": [],
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
