####
## Metadata Registry Sync Logic for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Plan, persist, and verify append-versioned warehouse metadata registry state."""

# --- Importing Libraries
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from pipelines.common.clickhouse import quote_sql_literal
from pipelines.common.logging import logger
from pipelines.metadata.config import MetadataAssetConfig, MetadataRegistryConfig, PROJECT_ROOT


# --- Defining Constants
METADATA_ASSETS_TABLE = "dq.metadata_assets"
METADATA_DDL_PATH     = PROJECT_ROOT / "infra" / "init" / "clickhouse" / "05_metadata_tables.sql"
MAX_REGISTRY_ROWS     = 1000

METADATA_ASSET_COLUMNS = (
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
    "source_config_path",
    "config_sha256",
    "is_active",
    "version",
    "synced_at",
)


# --- Defining Data Classes
@dataclass(frozen=True)
class MetadataRegistryEntry:
    """
    Represent one append-versioned metadata asset state.

    Attributes mirror the ClickHouse metadata_assets columns. `is_active`
    separates current contract membership from the business lifecycle state,
    allowing config removals to create auditable tombstones.
    """

    qualified_name: str
    database_name: str
    table_name: str
    display_name: str
    description: str
    dataset: str
    domain: str
    data_layer: str
    technical_owner: str
    business_owner: str
    grain: str
    refresh_frequency: str
    sla_time: str
    sla_timezone: str
    criticality: str
    sensitivity: str
    contains_pii: bool
    certification_status: str
    lifecycle_status: str
    tags: tuple[str, ...]
    source_config_path: str
    config_sha256: str
    is_active: bool
    version: int
    synced_at: datetime

    def as_insert_row(self) -> tuple[Any, ...]:
        """
        Convert the entry into ClickHouse insert column order.

        Returns:
            Tuple accepted by clickhouse-connect for metadata_assets insertion.
        """
        return (
            self.qualified_name,
            self.database_name,
            self.table_name,
            self.display_name,
            self.description,
            self.dataset,
            self.domain,
            self.data_layer,
            self.technical_owner,
            self.business_owner,
            self.grain,
            self.refresh_frequency,
            self.sla_time,
            self.sla_timezone,
            self.criticality,
            self.sensitivity,
            int(self.contains_pii),
            self.certification_status,
            self.lifecycle_status,
            list(self.tags),
            self.source_config_path,
            self.config_sha256,
            int(self.is_active),
            self.version,
            self.synced_at,
        )


@dataclass(frozen=True)
class MetadataSyncPlan:
    """
    Describe deterministic registry changes before persistence.

    Attributes:
        entries_to_insert: New active versions and non-destructive tombstones.
        unchanged_names: Assets whose latest active hash already matches config.
        activated_names: New, changed, or reactivated assets.
        tombstoned_names: Removed assets receiving inactive latest versions.
        source_config_path: Stable project-relative contract path.
    """

    entries_to_insert: tuple[MetadataRegistryEntry, ...]
    unchanged_names: tuple[str, ...]
    activated_names: tuple[str, ...]
    tombstoned_names: tuple[str, ...]
    source_config_path: str

    def as_dict(self) -> dict[str, Any]:
        """
        Build an audit-friendly plan summary without full metadata payloads.

        Returns:
            Dictionary containing bounded counts and affected asset names.
        """
        return {
            "source_config_path": self.source_config_path,
            "inserted_versions": len(self.entries_to_insert),
            "activated_count": len(self.activated_names),
            "activated_assets": list(self.activated_names),
            "tombstoned_count": len(self.tombstoned_names),
            "tombstoned_assets": list(self.tombstoned_names),
            "unchanged_count": len(self.unchanged_names),
            "unchanged_assets": list(self.unchanged_names),
        }


@dataclass(frozen=True)
class MetadataVerificationResult:
    """
    Capture exact contract-to-registry verification results.

    Attributes:
        status: `pass` when all configured assets match latest ClickHouse state.
        expected_count: Number of assets declared by the source contract.
        active_count: Number of active latest rows owned by the source contract.
        errors: Bounded human-readable mismatch descriptions.
    """

    status: str
    expected_count: int
    active_count: int
    errors: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        """
        Convert verification state into JSON-serializable output.

        Returns:
            Dictionary suitable for Airflow task logs.
        """
        return {
            "status": self.status,
            "expected_count": self.expected_count,
            "active_count": self.active_count,
            "errors": list(self.errors),
        }


# --- Defining Pure Registry Functions
def clickhouse_text(value: Any) -> str:
    """
    Normalize ClickHouse String and FixedString values into Python text.

    Args:
        value: String-like value returned by clickhouse-connect.

    Returns:
        Decoded text with FixedString null padding removed.
    """
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8").rstrip("\x00")

    return str(value)


def metadata_asset_hash(asset: MetadataAssetConfig, dataset: str) -> str:
    """
    Build a stable SHA-256 hash for one normalized metadata asset contract.

    Args:
        asset: Validated asset configuration.
        dataset: Parent data-product identifier.

    Returns:
        Lower-case 64-character SHA-256 digest.
    """
    payload = {
        "asset": asset.model_dump(mode="json"),
        "dataset": dataset,
    }
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")

    return hashlib.sha256(encoded).hexdigest()


def project_relative_config_path(path: str | Path) -> str:
    """
    Convert a metadata config path into a stable project-relative POSIX path.

    Args:
        path: Absolute or project-relative source config path.

    Returns:
        Stable path such as `configs/metadata/orders.yml`.

    Raises:
        ValueError: If the path resolves outside the repository root.
    """
    candidate = Path(path)
    resolved  = candidate.resolve() if candidate.is_absolute() else (PROJECT_ROOT / candidate).resolve()

    try:
        relative = resolved.relative_to(PROJECT_ROOT.resolve())
    except ValueError as exc:
        raise ValueError(f"Metadata config must be inside the project root: {path}") from exc

    return relative.as_posix()


def entry_from_asset(
    asset: MetadataAssetConfig,
    dataset: str,
    source_config_path: str,
    version: int,
    synced_at: datetime,
) -> MetadataRegistryEntry:
    """
    Convert a validated asset contract into one active registry entry.

    Args:
        asset: Validated metadata asset.
        dataset: Parent registry dataset.
        source_config_path: Stable project-relative source path.
        version: Monotonic replacement version.
        synced_at: UTC synchronization timestamp.

    Returns:
        Active MetadataRegistryEntry ready for insertion.
    """
    return MetadataRegistryEntry(
        qualified_name=asset.qualified_name,
        database_name=asset.database_name,
        table_name=asset.table_name,
        display_name=asset.display_name,
        description=asset.description,
        dataset=dataset,
        domain=asset.domain,
        data_layer=asset.data_layer,
        technical_owner=asset.technical_owner,
        business_owner=asset.business_owner,
        grain=asset.grain,
        refresh_frequency=asset.refresh_frequency,
        sla_time=asset.sla_time,
        sla_timezone=asset.sla_timezone,
        criticality=asset.criticality,
        sensitivity=asset.sensitivity,
        contains_pii=asset.contains_pii,
        certification_status=asset.certification_status,
        lifecycle_status=asset.lifecycle_status,
        tags=tuple(asset.tags),
        source_config_path=source_config_path,
        config_sha256=metadata_asset_hash(asset=asset, dataset=dataset),
        is_active=True,
        version=version,
        synced_at=synced_at,
    )


def build_metadata_sync_plan(
    config: MetadataRegistryConfig,
    source_path: str | Path,
    current_entries: Iterable[MetadataRegistryEntry],
    base_version: int | None = None,
    synced_at: datetime | None = None,
) -> MetadataSyncPlan:
    """
    Compare config with latest registry state and build an idempotent append plan.

    Args:
        config: Validated metadata registry source contract.
        source_path: Source YAML path used for ownership and tombstone scope.
        current_entries: Latest ClickHouse rows after ReplacingMergeTree FINAL.
        base_version: Optional deterministic starting version used by tests.
        synced_at: Optional UTC synchronization timestamp used by tests.

    Returns:
        MetadataSyncPlan containing only required active versions and tombstones.

    Raises:
        ValueError: If another source file already owns a configured asset.
    """
    source_config_path = project_relative_config_path(source_path)
    current_by_name    = {entry.qualified_name: entry for entry in current_entries}
    target_by_name     = {asset.qualified_name: asset for asset in config.assets}
    current_versions   = [entry.version for entry in current_by_name.values()]
    requested_version  = base_version if base_version is not None else time.time_ns()
    next_version       = max([requested_version, *(version + 1 for version in current_versions)] or [requested_version])
    timestamp          = synced_at or datetime.now(timezone.utc)

    entries_to_insert: list[MetadataRegistryEntry] = []
    unchanged_names: list[str]                     = []
    activated_names: list[str]                     = []
    tombstoned_names: list[str]                    = []

    for qualified_name in sorted(target_by_name):
        asset   = target_by_name[qualified_name]
        current = current_by_name.get(qualified_name)

        if current is not None and current.source_config_path != source_config_path:
            raise ValueError(
                "Metadata asset ownership conflict: "
                f"asset={qualified_name} current_source={current.source_config_path} "
                f"requested_source={source_config_path}"
            )

        expected_hash = metadata_asset_hash(asset=asset, dataset=config.dataset)

        if current is not None and current.is_active and current.config_sha256 == expected_hash:
            unchanged_names.append(qualified_name)
            continue

        entries_to_insert.append(
            entry_from_asset(
                asset=asset,
                dataset=config.dataset,
                source_config_path=source_config_path,
                version=next_version,
                synced_at=timestamp,
            )
        )
        activated_names.append(qualified_name)
        next_version += 1

    # Config removal appends an inactive latest version instead of deleting audit history.
    for qualified_name in sorted(current_by_name):
        current = current_by_name[qualified_name]

        if (
            current.source_config_path == source_config_path
            and current.is_active
            and qualified_name not in target_by_name
        ):
            entries_to_insert.append(
                replace(
                    current,
                    is_active=False,
                    version=next_version,
                    synced_at=timestamp,
                )
            )
            tombstoned_names.append(qualified_name)
            next_version += 1

    return MetadataSyncPlan(
        entries_to_insert=tuple(entries_to_insert),
        unchanged_names=tuple(unchanged_names),
        activated_names=tuple(activated_names),
        tombstoned_names=tuple(tombstoned_names),
        source_config_path=source_config_path,
    )


# --- Defining ClickHouse Registry Functions
def ensure_metadata_registry_table(client: Any, ddl_path: Path = METADATA_DDL_PATH) -> None:
    """
    Apply the idempotent metadata table DDL for an existing local environment.

    Args:
        client: clickhouse-connect client.
        ddl_path: Path to the single-statement metadata DDL file.

    Returns:
        None.

    Raises:
        FileNotFoundError: If the modular bootstrap file is missing.
        ValueError: If the file does not contain exactly one CREATE TABLE statement.
    """
    if not ddl_path.is_file():
        raise FileNotFoundError(f"Metadata DDL not found: {ddl_path}")

    ddl = ddl_path.read_text(encoding="utf-8-sig").strip()

    if ddl.upper().count("CREATE TABLE") != 1:
        raise ValueError("Metadata DDL must contain exactly one CREATE TABLE statement.")

    logger.info("Ensuring ClickHouse metadata registry table | table=%s ddl=%s", METADATA_ASSETS_TABLE, ddl_path)
    client.command(ddl.removesuffix(";").strip())


def fetch_latest_metadata_entries(client: Any, limit: int = MAX_REGISTRY_ROWS) -> list[MetadataRegistryEntry]:
    """
    Read latest append-versioned metadata entries from ClickHouse.

    Args:
        client: clickhouse-connect client.
        limit: Hard upper bound for the small registry dimension.

    Returns:
        Latest metadata entry for each qualified asset.

    Raises:
        ValueError: If limit is outside the bounded registry range.
    """
    if not 1 <= limit <= MAX_REGISTRY_ROWS:
        raise ValueError(f"Metadata registry limit must be between 1 and {MAX_REGISTRY_ROWS}.")

    columns_sql = ",\n            ".join(METADATA_ASSET_COLUMNS)
    result = client.query(
        f"""
        SELECT
            {columns_sql}
        FROM {METADATA_ASSETS_TABLE} FINAL
        ORDER BY qualified_name
        LIMIT {limit}
        """
    )

    columns = list(result.column_names or METADATA_ASSET_COLUMNS)
    entries = []

    for row in result.result_rows:
        values = dict(zip(columns, row, strict=False))
        entries.append(
            MetadataRegistryEntry(
                qualified_name=clickhouse_text(values["qualified_name"]),
                database_name=clickhouse_text(values["database_name"]),
                table_name=clickhouse_text(values["table_name"]),
                display_name=clickhouse_text(values["display_name"]),
                description=clickhouse_text(values["description"]),
                dataset=clickhouse_text(values["dataset"]),
                domain=clickhouse_text(values["domain"]),
                data_layer=clickhouse_text(values["data_layer"]),
                technical_owner=clickhouse_text(values["technical_owner"]),
                business_owner=clickhouse_text(values["business_owner"]),
                grain=clickhouse_text(values["grain"]),
                refresh_frequency=clickhouse_text(values["refresh_frequency"]),
                sla_time=clickhouse_text(values["sla_time"]),
                sla_timezone=clickhouse_text(values["sla_timezone"]),
                criticality=clickhouse_text(values["criticality"]),
                sensitivity=clickhouse_text(values["sensitivity"]),
                contains_pii=bool(values["contains_pii"]),
                certification_status=clickhouse_text(values["certification_status"]),
                lifecycle_status=clickhouse_text(values["lifecycle_status"]),
                tags=tuple(values["tags"] or ()),
                source_config_path=clickhouse_text(values["source_config_path"]),
                config_sha256=clickhouse_text(values["config_sha256"]),
                is_active=bool(values["is_active"]),
                version=int(values["version"]),
                synced_at=values["synced_at"],
            )
        )

    logger.info("Fetched latest metadata registry state | rows=%d", len(entries))

    return entries


def fetch_existing_clickhouse_assets(
    client: Any,
    assets: Iterable[MetadataAssetConfig],
) -> set[str]:
    """
    Resolve configured physical assets against ClickHouse system metadata.

    Args:
        client: clickhouse-connect client.
        assets: Validated metadata asset contracts.

    Returns:
        Set of qualified assets currently visible in system.tables.

    Raises:
        ValueError: If the contract exceeds the bounded registry size.
    """
    identities = sorted({(asset.database_name, asset.table_name) for asset in assets})

    if len(identities) > MAX_REGISTRY_ROWS:
        raise ValueError(f"Physical metadata verification supports at most {MAX_REGISTRY_ROWS} assets.")

    if not identities:
        return set()

    predicates = "\n            OR ".join(
        (
            f"(database = {quote_sql_literal(database)} "
            f"AND name = {quote_sql_literal(table)})"
        )
        for database, table in identities
    )
    result = client.query(
        f"""
        SELECT database, name
        FROM system.tables
        WHERE {predicates}
        ORDER BY database, name
        LIMIT {MAX_REGISTRY_ROWS}
        """
    )

    existing = {
        f"{clickhouse_text(database)}.{clickhouse_text(table)}"
        for database, table in result.result_rows
    }

    logger.info(
        "Verified physical ClickHouse asset inventory | configured=%d existing=%d",
        len(identities),
        len(existing),
    )

    return existing


def insert_metadata_entries(client: Any, entries: Iterable[MetadataRegistryEntry]) -> int:
    """
    Insert planned metadata versions into ClickHouse.

    Args:
        client: clickhouse-connect client.
        entries: Planned active versions and tombstones.

    Returns:
        Number of append-versioned rows inserted.
    """
    rows = [entry.as_insert_row() for entry in entries]

    if not rows:
        logger.info("Metadata registry is unchanged; insert skipped")
        return 0

    logger.info("Inserting metadata registry versions | table=%s rows=%d", METADATA_ASSETS_TABLE, len(rows))
    client.insert(
        table=METADATA_ASSETS_TABLE,
        data=rows,
        column_names=METADATA_ASSET_COLUMNS,
    )

    return len(rows)


def sync_metadata_registry(
    client: Any,
    config: MetadataRegistryConfig,
    source_path: str | Path,
) -> MetadataSyncPlan:
    """
    Ensure schema, calculate the sync plan, and append required versions.

    Args:
        client: clickhouse-connect client.
        config: Validated metadata registry contract.
        source_path: YAML contract path.

    Returns:
        Applied MetadataSyncPlan.
    """
    ensure_metadata_registry_table(client)
    current = fetch_latest_metadata_entries(client)
    plan    = build_metadata_sync_plan(
        config=config,
        source_path=source_path,
        current_entries=current,
    )

    insert_metadata_entries(client=client, entries=plan.entries_to_insert)
    logger.info("Metadata registry sync completed | summary=%s", json.dumps(plan.as_dict(), sort_keys=True))

    return plan


def verify_metadata_registry(
    client: Any,
    config: MetadataRegistryConfig,
    source_path: str | Path,
) -> MetadataVerificationResult:
    """
    Verify latest ClickHouse state exactly matches the source metadata contract.

    Args:
        client: clickhouse-connect client.
        config: Validated metadata registry contract.
        source_path: YAML contract path.

    Returns:
        MetadataVerificationResult with exact mismatch evidence.
    """
    source_config_path = project_relative_config_path(source_path)
    current_entries    = fetch_latest_metadata_entries(client)
    current_by_name    = {entry.qualified_name: entry for entry in current_entries}
    expected_by_name   = {asset.qualified_name: asset for asset in config.assets}
    physical_assets    = fetch_existing_clickhouse_assets(client=client, assets=config.assets)
    errors: list[str]  = []

    for qualified_name, asset in sorted(expected_by_name.items()):
        current = current_by_name.get(qualified_name)

        if current is None:
            errors.append(f"missing asset: {qualified_name}")
            continue

        if current.source_config_path != source_config_path:
            errors.append(f"ownership mismatch: {qualified_name}")
        if not current.is_active:
            errors.append(f"asset is tombstoned: {qualified_name}")
        if current.config_sha256 != metadata_asset_hash(asset=asset, dataset=config.dataset):
            errors.append(f"config hash mismatch: {qualified_name}")
        if qualified_name not in physical_assets:
            errors.append(f"physical ClickHouse asset missing: {qualified_name}")

    active_for_source = {
        entry.qualified_name
        for entry in current_entries
        if entry.source_config_path == source_config_path and entry.is_active
    }
    unexpected = sorted(active_for_source - set(expected_by_name))
    errors.extend(f"unexpected active asset: {qualified_name}" for qualified_name in unexpected)

    result = MetadataVerificationResult(
        status="pass" if not errors else "fail",
        expected_count=len(expected_by_name),
        active_count=len(active_for_source),
        errors=tuple(errors),
    )

    logger.info("Metadata registry verification completed | result=%s", json.dumps(result.as_dict(), sort_keys=True))

    return result
