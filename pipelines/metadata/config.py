####
## Metadata Contract Loader for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Load and validate bounded metadata contracts used by the control plane."""

# --- Importing Libraries
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[2]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipelines.common.logging import logger


# --- Defining Constants
IDENTIFIER_PATTERN = re.compile(r"^[a-z_][a-z0-9_]*$")
SLA_TIME_PATTERN   = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
TAG_PATTERN        = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

METADATA_REGISTRY_PATHS = {
    "orders": PROJECT_ROOT / "configs" / "metadata" / "orders.yml",
}

METADATA_REGISTRY_NAMES = tuple(sorted(METADATA_REGISTRY_PATHS))

DataLayer          = Literal["raw", "staging", "mart"]
Criticality        = Literal["low", "medium", "high", "critical"]
Sensitivity        = Literal["public", "internal", "confidential", "restricted"]
CertificationState = Literal["experimental", "candidate", "certified", "deprecated"]
LifecycleState     = Literal["active", "deprecated"]


# --- Defining Config Models
class MetadataAssetConfig(BaseModel):
    """
    Describe one warehouse asset and its operational trust contract.

    Attributes:
        qualified_name: Stable ClickHouse identifier in database.table format.
        database_name: ClickHouse database containing the asset.
        table_name: ClickHouse table or view name.
        display_name: Human-readable asset name used by UI and Copilot output.
        description: Concise statement of purpose and expected usage.
        domain: Business or data-product domain owning the asset.
        data_layer: Raw, staging, or mart role in the warehouse flow.
        technical_owner: Team responsible for pipeline and schema reliability.
        business_owner: Team responsible for business meaning and approved usage.
        grain: Explicit row-level grain used to prevent invalid joins or aggregations.
        refresh_frequency: Human-readable refresh cadence.
        sla_time: Local wall-clock completion target in HH:MM format.
        sla_timezone: IANA timezone used to interpret the SLA.
        criticality: Operational impact tier for incidents involving the asset.
        sensitivity: Data handling classification.
        contains_pii: Whether the curated asset contains personally identifiable data.
        certification_status: Trust promotion state for consumer usage.
        lifecycle_status: Active or deprecated lifecycle state.
        tags: Normalized discovery labels.
    """

    qualified_name: str                    = Field(min_length=3, max_length=255)
    database_name: str                     = Field(min_length=1, max_length=128)
    table_name: str                        = Field(min_length=1, max_length=128)
    display_name: str                      = Field(min_length=3, max_length=160)
    description: str                       = Field(min_length=20, max_length=1000)
    domain: str                            = Field(min_length=2, max_length=80)
    data_layer: DataLayer
    technical_owner: str                   = Field(min_length=3, max_length=160)
    business_owner: str                    = Field(min_length=3, max_length=160)
    grain: str                             = Field(min_length=10, max_length=500)
    refresh_frequency: str                 = Field(min_length=2, max_length=80)
    sla_time: str
    sla_timezone: str                      = "Asia/Bangkok"
    criticality: Criticality
    sensitivity: Sensitivity
    contains_pii: bool                     = False
    certification_status: CertificationState
    lifecycle_status: LifecycleState       = "active"
    tags: list[str]                         = Field(min_length=1, max_length=20)

    @field_validator("database_name", "table_name", "domain")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        """
        Validate normalized database, table, and domain identifiers.

        Args:
            value: Candidate lower-case identifier.

        Returns:
            Original identifier after validation.

        Raises:
            ValueError: If the value contains unsupported identifier characters.
        """
        if not IDENTIFIER_PATTERN.fullmatch(value):
            raise ValueError("Identifiers must use lower-case letters, digits, and underscores.")

        return value

    @field_validator("sla_time")
    @classmethod
    def validate_sla_time(cls, value: str) -> str:
        """
        Validate the local SLA completion time.

        Args:
            value: Wall-clock time in 24-hour HH:MM format.

        Returns:
            Original time after validation.

        Raises:
            ValueError: If the value is not a valid 24-hour time.
        """
        if not SLA_TIME_PATTERN.fullmatch(value):
            raise ValueError("sla_time must use 24-hour HH:MM format.")

        return value

    @field_validator("sla_timezone")
    @classmethod
    def validate_sla_timezone(cls, value: str) -> str:
        """
        Validate the IANA timezone used by the asset SLA.

        Args:
            value: Candidate IANA timezone name.

        Returns:
            Original timezone after validation.

        Raises:
            ValueError: If the timezone is unknown to the local runtime.
        """
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown IANA timezone: {value}") from exc

        return value

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, values: list[str]) -> list[str]:
        """
        Normalize tags into a unique deterministic order.

        Args:
            values: Discovery tags from the metadata YAML contract.

        Returns:
            Sorted lower-case tags with duplicates removed.

        Raises:
            ValueError: If a tag contains unsupported characters.
        """
        normalized = sorted({value.strip().lower() for value in values if value.strip()})

        if not normalized:
            raise ValueError("tags must contain at least one non-empty value.")

        invalid = [value for value in normalized if not TAG_PATTERN.fullmatch(value)]
        if invalid:
            raise ValueError(f"Invalid metadata tags: {invalid}")

        return normalized

    @model_validator(mode="after")
    def validate_qualified_name(self) -> "MetadataAssetConfig":
        """
        Ensure the qualified name matches its database and table fields.

        Returns:
            Current validated metadata asset.

        Raises:
            ValueError: If qualified_name is malformed or inconsistent.
        """
        expected = f"{self.database_name}.{self.table_name}"

        if self.qualified_name != expected:
            raise ValueError(f"qualified_name must equal {expected}")

        return self


class MetadataRegistryConfig(BaseModel):
    """
    Define one bounded metadata registry contract.

    Attributes:
        contract_version: Contract schema version understood by this loader.
        registry_name: Allowlisted runtime name used by Airflow and CLI commands.
        dataset: Data-product identifier represented by the registry.
        description: Human-readable registry purpose.
        assets: Unique warehouse assets managed by this source file.
    """

    contract_version: Literal[1]
    registry_name: str             = Field(min_length=2, max_length=80)
    dataset: str                   = Field(min_length=2, max_length=80)
    description: str               = Field(min_length=20, max_length=1000)
    assets: list[MetadataAssetConfig] = Field(min_length=1, max_length=200)

    @field_validator("registry_name", "dataset")
    @classmethod
    def validate_registry_identifier(cls, value: str) -> str:
        """
        Validate registry and dataset identifiers.

        Args:
            value: Candidate registry or dataset identifier.

        Returns:
            Original identifier after validation.

        Raises:
            ValueError: If the identifier is unsafe or inconsistent.
        """
        if not IDENTIFIER_PATTERN.fullmatch(value):
            raise ValueError("Registry identifiers must use lower-case letters, digits, and underscores.")

        return value

    @model_validator(mode="after")
    def validate_unique_assets(self) -> "MetadataRegistryConfig":
        """
        Ensure a source contract owns each qualified asset only once.

        Returns:
            Current validated metadata registry.

        Raises:
            ValueError: If duplicate qualified asset names are configured.
        """
        qualified_names = [asset.qualified_name for asset in self.assets]
        duplicates      = sorted({name for name in qualified_names if qualified_names.count(name) > 1})

        if duplicates:
            raise ValueError(f"Duplicate metadata assets: {duplicates}")

        return self


# --- Defining Config Functions
def resolve_metadata_registry_path(registry_name: str) -> Path:
    """
    Resolve one allowlisted metadata registry name to its project path.

    Args:
        registry_name: Registry name supplied by CLI or Airflow configuration.

    Returns:
        Absolute path to the corresponding YAML contract.

    Raises:
        ValueError: If the registry is not allowlisted.
    """
    normalized = registry_name.strip().lower()

    if normalized not in METADATA_REGISTRY_PATHS:
        raise ValueError(f"Unknown metadata registry: {registry_name}")

    return METADATA_REGISTRY_PATHS[normalized]


def load_metadata_registry(path: str | Path) -> MetadataRegistryConfig:
    """
    Load and validate one metadata registry YAML file.

    Args:
        path: Project metadata YAML path.

    Returns:
        Validated MetadataRegistryConfig instance.

    Raises:
        FileNotFoundError: If the source contract does not exist.
        ValueError: If YAML root content or the typed contract is invalid.
    """
    config_path = Path(path)
    logger.info("Loading metadata registry | path=%s", config_path)

    if not config_path.is_file():
        raise FileNotFoundError(f"Metadata registry not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as handle:
        raw: Any = yaml.safe_load(handle) or {}

    if not isinstance(raw, dict):
        raise ValueError(f"Metadata registry root must be a mapping: {config_path}")

    config = MetadataRegistryConfig.model_validate(raw)

    logger.info(
        "Metadata registry validated | registry=%s dataset=%s assets=%d",
        config.registry_name,
        config.dataset,
        len(config.assets),
    )

    return config


def load_named_metadata_registry(registry_name: str) -> tuple[MetadataRegistryConfig, Path]:
    """
    Resolve and load one allowlisted metadata registry.

    Args:
        registry_name: Allowlisted registry name.

    Returns:
        Tuple containing the validated config and absolute source path.
    """
    path   = resolve_metadata_registry_path(registry_name)
    config = load_metadata_registry(path)

    if config.registry_name != registry_name.strip().lower():
        raise ValueError(
            f"Metadata registry identity mismatch: requested={registry_name} configured={config.registry_name}"
        )

    return config, path
