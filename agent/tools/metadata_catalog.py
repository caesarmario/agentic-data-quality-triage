####
## Metadata Catalog Tool for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Read bounded, audited warehouse asset context from the metadata registry."""

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

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.tools.audit_log import write_agent_audit_event
from agent.tools.clickhouse_sql import rows_to_dicts
from pipelines.common.clickhouse import (
    build_clickhouse_client,
    quote_sql_literal,
    validate_clickhouse_identifier,
    validate_qualified_table_name,
)
from pipelines.common.logging import logger
from pipelines.metadata.registry import METADATA_ASSETS_TABLE, clickhouse_text


# --- Defining Constants
TOOL_NAME                  = "metadata_catalog"
DEFAULT_LIMIT              = 25
MAX_LIMIT                  = 100
MAX_SEARCH_QUERY_CHARS     = 120
ALLOWED_DATA_LAYERS        = {"raw", "staging", "mart"}
ALLOWED_CERTIFICATION      = {"experimental", "candidate", "certified", "deprecated"}
ALLOWED_LIFECYCLE_STATUSES = {"active", "deprecated"}

PUBLIC_METADATA_COLUMNS = (
    "qualified_name",
    "database_name",
    "table_name",
    "display_name",
    "description",
    "dataset",
    "domain",
    "data_layer",
    "technical_owner",
    "business_owner",
    "grain",
    "refresh_frequency",
    "sla_time",
    "sla_timezone",
    "criticality",
    "sensitivity",
    "contains_pii",
    "certification_status",
    "lifecycle_status",
    "tags",
    "synced_at",
)


# --- Defining Validation Helpers
def normalize_optional_filter(
    value: str | None,
    field_name: str,
    allowed_values: set[str] | None = None,
) -> str | None:
    """
    Normalize and validate one optional metadata filter.

    Args:
        value: Optional filter value supplied by an operator or client.
        field_name: Public field name used in validation errors.
        allowed_values: Optional finite allowlist for categorical filters.

    Returns:
        Normalized lower-case value, or None when the filter is blank.

    Raises:
        ValueError: If the value is unsafe or outside its allowlist.
    """
    normalized = str(value or "").strip().lower()

    if not normalized:
        return None

    if allowed_values is not None and normalized not in allowed_values:
        expected = ", ".join(sorted(allowed_values))
        raise ValueError(f"{field_name} must be one of: {expected}.")

    if allowed_values is None:
        validate_clickhouse_identifier(normalized)

    return normalized


def normalize_search_query(query: str | None) -> str:
    """
    Normalize a bounded free-text metadata search query.

    Args:
        query: Optional operator search text.

    Returns:
        Trimmed query text.

    Raises:
        ValueError: If the search text exceeds the public contract bound.
    """
    normalized = str(query or "").strip()

    if len(normalized) > MAX_SEARCH_QUERY_CHARS:
        raise ValueError(
            f"Metadata search query cannot exceed {MAX_SEARCH_QUERY_CHARS} characters."
        )

    return normalized


# --- Building Read-Only SQL
def build_metadata_catalog_sql(
    query: str | None = None,
    qualified_name: str | None = None,
    domain: str | None = None,
    data_layer: str | None = None,
    certification_status: str | None = None,
    lifecycle_status: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> str:
    """
    Build a bounded static SELECT for active metadata registry assets.

    Args:
        query: Optional text matched against asset names, descriptions, owners, grain, and tags.
        qualified_name: Optional exact database.table identity.
        domain: Optional normalized data domain.
        data_layer: Optional raw, staging, or mart layer.
        certification_status: Optional metadata certification state.
        lifecycle_status: Optional active or deprecated lifecycle state.
        limit: Maximum metadata assets returned.

    Returns:
        Static read-only ClickHouse SQL with escaped literal filters and hard LIMIT.

    Raises:
        ValueError: If an identifier, categorical filter, query, or limit is invalid.
    """
    if not 1 <= int(limit) <= MAX_LIMIT:
        raise ValueError(f"Metadata catalog limit must be between 1 and {MAX_LIMIT}.")

    normalized_query         = normalize_search_query(query)
    normalized_domain        = normalize_optional_filter(domain, "domain")
    normalized_layer         = normalize_optional_filter(data_layer, "data_layer", ALLOWED_DATA_LAYERS)
    normalized_certification = normalize_optional_filter(
        certification_status,
        "certification_status",
        ALLOWED_CERTIFICATION,
    )
    normalized_lifecycle     = normalize_optional_filter(
        lifecycle_status,
        "lifecycle_status",
        ALLOWED_LIFECYCLE_STATUSES,
    )
    normalized_name          = str(qualified_name or "").strip()

    if normalized_name:
        validate_qualified_table_name(normalized_name)

    filters = ["is_active = 1"]

    if normalized_query:
        searchable_text = "concat(qualified_name, ' ', display_name, ' ', description, ' ', technical_owner, ' ', business_owner, ' ', grain, ' ', arrayStringConcat(tags, ' '))"
        filters.append(
            f"positionCaseInsensitiveUTF8({searchable_text}, {quote_sql_literal(normalized_query)}) > 0"
        )
    if normalized_name:
        filters.append(f"qualified_name = {quote_sql_literal(normalized_name)}")
    if normalized_domain:
        filters.append(f"domain = {quote_sql_literal(normalized_domain)}")
    if normalized_layer:
        filters.append(f"data_layer = {quote_sql_literal(normalized_layer)}")
    if normalized_certification:
        filters.append(
            f"certification_status = {quote_sql_literal(normalized_certification)}"
        )
    if normalized_lifecycle:
        filters.append(f"lifecycle_status = {quote_sql_literal(normalized_lifecycle)}")

    columns_sql = ",\n            ".join(PUBLIC_METADATA_COLUMNS)
    where_sql   = "\n          AND ".join(filters)

    return f"""
        SELECT
            {columns_sql}
        FROM {METADATA_ASSETS_TABLE} FINAL
        WHERE {where_sql}
        ORDER BY
            multiIf(certification_status = 'certified', 1, certification_status = 'candidate', 2, 3),
            multiIf(criticality = 'critical', 1, criticality = 'high', 2, criticality = 'medium', 3, 4),
            qualified_name
        LIMIT {int(limit)}
    """


# --- Normalizing Public Results
def normalize_metadata_asset(row: dict[str, Any]) -> dict[str, Any]:
    """
    Normalize one ClickHouse row into the public metadata contract.

    Args:
        row: Raw row dictionary returned by clickhouse-connect.

    Returns:
        JSON-safe metadata asset without internal hashes, versions, or source paths.
    """
    normalized: dict[str, Any] = {}

    for column in PUBLIC_METADATA_COLUMNS:
        value = row.get(column)

        if column == "contains_pii":
            normalized[column] = bool(value)
        elif column == "tags":
            normalized[column] = [clickhouse_text(item) for item in (value or [])]
        elif column == "synced_at":
            normalized[column] = value
        else:
            normalized[column] = clickhouse_text(value or "")

    return normalized


# --- Querying Metadata Registry
def query_metadata_catalog(
    query: str | None = None,
    qualified_name: str | None = None,
    domain: str | None = None,
    data_layer: str | None = None,
    certification_status: str | None = None,
    lifecycle_status: str | None = None,
    limit: int = DEFAULT_LIMIT,
    agent_run_id: UUID | str | None = None,
    alert_key: str = "",
    clickhouse_host: str | None = None,
    clickhouse_port: int | None = None,
) -> dict[str, Any]:
    """
    Query active metadata assets and persist a non-sensitive audit event.

    Args:
        query: Optional text search.
        qualified_name: Optional exact database.table identity.
        domain: Optional data domain filter.
        data_layer: Optional warehouse layer filter.
        certification_status: Optional certification filter.
        lifecycle_status: Optional lifecycle filter.
        limit: Maximum assets returned.
        agent_run_id: Optional audit correlation UUID.
        alert_key: Optional related alert key.
        clickhouse_host: Optional ClickHouse host override.
        clickhouse_port: Optional ClickHouse HTTP port override.

    Returns:
        Bounded metadata asset list and deterministic summary.
    """
    client                = build_clickhouse_client(host=clickhouse_host, port=clickhouse_port)
    resolved_agent_run_id = UUID(str(agent_run_id)) if agent_run_id else uuid4()
    started_monotonic     = time.monotonic()
    action                = "get_metadata_asset" if qualified_name else "search_metadata_assets"
    sql                   = build_metadata_catalog_sql(
        query=query,
        qualified_name=qualified_name,
        domain=domain,
        data_layer=data_layer,
        certification_status=certification_status,
        lifecycle_status=lifecycle_status,
        limit=limit,
    )
    input_payload = {
        "query": normalize_search_query(query),
        "qualified_name": str(qualified_name or "").strip(),
        "domain": normalize_optional_filter(domain, "domain"),
        "data_layer": normalize_optional_filter(data_layer, "data_layer", ALLOWED_DATA_LAYERS),
        "certification_status": normalize_optional_filter(
            certification_status,
            "certification_status",
            ALLOWED_CERTIFICATION,
        ),
        "lifecycle_status": normalize_optional_filter(
            lifecycle_status,
            "lifecycle_status",
            ALLOWED_LIFECYCLE_STATUSES,
        ),
        "limit": int(limit),
    }

    try:
        result      = client.query(sql)
        columns     = list(result.column_names or PUBLIC_METADATA_COLUMNS)
        raw_rows    = rows_to_dicts(columns=columns, rows=result.result_rows)
        assets      = [normalize_metadata_asset(row) for row in raw_rows]
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)
        summary     = f"Found {len(assets)} trusted metadata asset(s)."
        audit_status = "not_found" if qualified_name and not assets else "success"

        write_agent_audit_event(
            client=client,
            action=action,
            status=audit_status,
            agent_run_id=resolved_agent_run_id,
            alert_key=alert_key,
            tool_name=TOOL_NAME,
            duration_ms=duration_ms,
            input_payload=input_payload,
            output_payload={
                "row_count": len(assets),
                "qualified_names": [asset["qualified_name"] for asset in assets],
            },
            sql=sql,
            row_count=len(assets),
        )

        logger.info(
            "Fetched metadata catalog | action=%s query=%s qualified_name=%s rows=%d",
            action,
            input_payload["query"],
            input_payload["qualified_name"],
            len(assets),
        )

        return {
            "status": audit_status,
            "query": input_payload["query"],
            "filters": {
                key: value
                for key, value in input_payload.items()
                if key not in {"query", "limit"} and value
            },
            "limit": int(limit),
            "row_count": len(assets),
            "assets": assets,
            "summary": summary,
        }

    except Exception as exc:
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)
        logger.exception("Failed to fetch metadata catalog | action=%s", action)

        write_agent_audit_event(
            client=client,
            action=action,
            status="failed",
            agent_run_id=resolved_agent_run_id,
            alert_key=alert_key,
            tool_name=TOOL_NAME,
            duration_ms=duration_ms,
            input_payload=input_payload,
            output_payload={"error_type": type(exc).__name__},
            error_message=str(exc),
            sql=sql,
        )

        raise


def search_metadata_assets(
    query: str | None = None,
    domain: str | None = None,
    data_layer: str | None = None,
    certification_status: str | None = None,
    lifecycle_status: str | None = None,
    limit: int = DEFAULT_LIMIT,
    agent_run_id: UUID | str | None = None,
    alert_key: str = "",
    clickhouse_host: str | None = None,
    clickhouse_port: int | None = None,
) -> dict[str, Any]:
    """
    Search trusted warehouse assets using bounded discovery filters.

    Args:
        query: Optional operator search text.
        domain: Optional data domain filter.
        data_layer: Optional raw, staging, or mart filter.
        certification_status: Optional certification filter.
        lifecycle_status: Optional lifecycle filter.
        limit: Maximum assets returned.
        agent_run_id: Optional audit correlation UUID.
        alert_key: Optional related alert key.
        clickhouse_host: Optional ClickHouse host override.
        clickhouse_port: Optional ClickHouse HTTP port override.

    Returns:
        Bounded metadata search result.
    """
    return query_metadata_catalog(
        query=query,
        domain=domain,
        data_layer=data_layer,
        certification_status=certification_status,
        lifecycle_status=lifecycle_status,
        limit=limit,
        agent_run_id=agent_run_id,
        alert_key=alert_key,
        clickhouse_host=clickhouse_host,
        clickhouse_port=clickhouse_port,
    )


def get_metadata_asset(
    qualified_name: str,
    agent_run_id: UUID | str | None = None,
    alert_key: str = "",
    clickhouse_host: str | None = None,
    clickhouse_port: int | None = None,
) -> dict[str, Any]:
    """
    Load one exact trusted warehouse asset from the metadata registry.

    Args:
        qualified_name: Fully qualified database.table identity.
        agent_run_id: Optional audit correlation UUID.
        alert_key: Optional related alert key.
        clickhouse_host: Optional ClickHouse host override.
        clickhouse_port: Optional ClickHouse HTTP port override.

    Returns:
        Public metadata asset dictionary.

    Raises:
        LookupError: If no active metadata asset matches the identity.
    """
    normalized_name = validate_qualified_table_name(qualified_name.strip())
    result          = query_metadata_catalog(
        qualified_name=normalized_name,
        limit=1,
        agent_run_id=agent_run_id,
        alert_key=alert_key,
        clickhouse_host=clickhouse_host,
        clickhouse_port=clickhouse_port,
    )

    if not result["assets"]:
        raise LookupError(f"Metadata asset not found: {normalized_name}")

    return result["assets"][0]


# --- Defining CLI
def build_parser() -> argparse.ArgumentParser:
    """
    Build the metadata catalog command-line parser.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(description="Search the trusted ClickHouse metadata registry.")

    parser.add_argument("--qualified-name", default=None, help="Optional exact database.table asset name.")
    parser.add_argument("--query", default=None, help="Optional bounded free-text search.")
    parser.add_argument("--domain", default=None, help="Optional data domain filter.")
    parser.add_argument("--data-layer", default=None, choices=sorted(ALLOWED_DATA_LAYERS))
    parser.add_argument("--certification-status", default=None, choices=sorted(ALLOWED_CERTIFICATION))
    parser.add_argument("--lifecycle-status", default=None, choices=sorted(ALLOWED_LIFECYCLE_STATUSES))
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Maximum assets returned.")

    return parser


def main() -> None:
    """
    Parse CLI arguments and print a JSON metadata lookup result.

    Returns:
        None.
    """
    args = build_parser().parse_args()

    if args.qualified_name:
        result: dict[str, Any] = get_metadata_asset(args.qualified_name)
    else:
        result = search_metadata_assets(
            query=args.query,
            domain=args.domain,
            data_layer=args.data_layer,
            certification_status=args.certification_status,
            lifecycle_status=args.lifecycle_status,
            limit=args.limit,
        )

    print(json.dumps(result, indent=2, default=str))


# --- Running CLI Entrypoint
if __name__ == "__main__":
    main()
