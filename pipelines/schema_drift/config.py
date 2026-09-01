####
## Schema Contract Loader for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Load and validate allowlisted ClickHouse schema contracts."""

# --- Importing Libraries
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[2]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipelines.common.clickhouse import validate_column_name, validate_qualified_table_name
from pipelines.common.logging import logger


# --- Defining Constants
SCHEMA_CONTRACT_PATHS = {
    "orders": PROJECT_ROOT / "configs" / "contracts" / "orders_schema.yml",
}

SCHEMA_CONTRACT_NAMES = tuple(sorted(SCHEMA_CONTRACT_PATHS))

CLICKHOUSE_TYPE_PATTERN = re.compile(r"^[A-Za-z0-9_(),.' ]+$")
DEFAULT_KIND_PATTERN    = re.compile(r"^(?:|DEFAULT|MATERIALIZED|ALIAS|EPHEMERAL)$")
CONTRACT_NAME_PATTERN   = re.compile(r"^[a-z][a-z0-9_]*$")

Severity = Literal["info", "warning", "critical"]


# --- Defining Config Models
class SchemaColumnConfig(BaseModel):
    """
    Define one expected ClickHouse column.

    Attributes:
        name: Expected column identifier.
        data_type: Exact normalized ClickHouse type returned by system.columns.
        default_kind: Optional ClickHouse default kind such as DEFAULT.
        default_expression: Optional normalized default expression.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    data_type: str                  = Field(min_length=1, max_length=500)
    default_kind: str               = Field(default="", max_length=32)
    default_expression: str         = Field(default="", max_length=1000)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        """
        Validate the expected column identifier.

        Args:
            value: Candidate ClickHouse column name.

        Returns:
            Validated column name.
        """
        return validate_column_name(value)

    @field_validator("data_type")
    @classmethod
    def validate_data_type(cls, value: str) -> str:
        """
        Validate a comparison-only ClickHouse type string.

        Args:
            value: Type expression expected from system.columns.

        Returns:
            Trimmed type expression.

        Raises:
            ValueError: If the expression contains unsupported control syntax.
        """
        normalized = value.strip()

        if not CLICKHOUSE_TYPE_PATTERN.fullmatch(normalized):
            raise ValueError(f"Unsupported ClickHouse data type expression: {value}")

        return normalized

    @field_validator("default_kind")
    @classmethod
    def validate_default_kind(cls, value: str) -> str:
        """
        Normalize and validate the ClickHouse default kind.

        Args:
            value: Empty value or a supported system.columns default kind.

        Returns:
            Upper-case normalized default kind.
        """
        normalized = value.strip().upper()

        if not DEFAULT_KIND_PATTERN.fullmatch(normalized):
            raise ValueError(f"Unsupported ClickHouse default kind: {value}")

        return normalized

    @field_validator("default_expression")
    @classmethod
    def validate_default_expression(cls, value: str) -> str:
        """
        Reject control characters from comparison-only default expressions.

        Args:
            value: Expected default expression.

        Returns:
            Trimmed default expression.

        Raises:
            ValueError: If the expression contains control characters.
        """
        normalized = value.strip()

        if any(ord(character) < 32 for character in normalized):
            raise ValueError("default_expression cannot contain control characters.")

        return normalized


class SchemaTableConfig(BaseModel):
    """
    Define the expected schema behavior for one ClickHouse table.

    Attributes:
        qualified_name: Fully qualified database.table identifier.
        check_column_order: Whether ordinal-position changes should be reported.
        check_defaults: Whether default kind and expression changes should be reported.
        columns: Ordered expected column definitions.
    """

    model_config = ConfigDict(extra="forbid")

    qualified_name: str
    check_column_order: bool           = True
    check_defaults: bool               = True
    columns: list[SchemaColumnConfig]  = Field(min_length=1, max_length=500)

    @field_validator("qualified_name")
    @classmethod
    def validate_table_name(cls, value: str) -> str:
        """
        Validate the fully qualified ClickHouse table name.

        Args:
            value: Candidate database.table identifier.

        Returns:
            Validated identifier.
        """
        return validate_qualified_table_name(value)

    @model_validator(mode="after")
    def validate_unique_columns(self) -> "SchemaTableConfig":
        """
        Ensure each expected column appears exactly once.

        Returns:
            Current validated table contract.

        Raises:
            ValueError: If duplicate columns are configured.
        """
        names      = [column.name for column in self.columns]
        duplicates = sorted({name for name in names if names.count(name) > 1})

        if duplicates:
            raise ValueError(f"Duplicate schema contract columns for {self.qualified_name}: {duplicates}")

        return self


class SchemaDriftPolicyConfig(BaseModel):
    """
    Map deterministic drift categories to operational severity.

    Attributes:
        missing_table: Severity for an absent contracted table.
        missing_column: Severity for an absent contracted column.
        type_mismatch: Severity for an incompatible observed type.
        unexpected_columns: Severity for columns not declared by the contract.
        position_mismatch: Severity for ordinal-position drift.
        default_mismatch: Severity for default kind or expression drift.
        fail_on_severity: Lowest severity that fails the operational DAG gate.
    """

    model_config = ConfigDict(extra="forbid")

    missing_table: Severity       = "critical"
    missing_column: Severity      = "critical"
    type_mismatch: Severity       = "critical"
    unexpected_columns: Severity = "warning"
    position_mismatch: Severity   = "warning"
    default_mismatch: Severity    = "warning"
    fail_on_severity: Severity    = "critical"


class SchemaContractConfig(BaseModel):
    """
    Define one versioned dataset schema contract.

    Attributes:
        contract_version: Schema contract format version.
        contract_name: Stable contract identifier.
        dataset: Data-product identifier.
        description: Human-readable purpose.
        policy: Drift severity and gate policy.
        tables: Unique expected ClickHouse table schemas.
    """

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal[1]
    contract_name: str                  = Field(min_length=3, max_length=120)
    dataset: str                        = Field(min_length=2, max_length=80)
    description: str                    = Field(min_length=20, max_length=1000)
    policy: SchemaDriftPolicyConfig
    tables: list[SchemaTableConfig]     = Field(min_length=1, max_length=200)

    @field_validator("contract_name", "dataset")
    @classmethod
    def validate_contract_identifier(cls, value: str) -> str:
        """
        Validate stable lower-case contract identifiers.

        Args:
            value: Candidate contract or dataset identifier.

        Returns:
            Validated identifier.

        Raises:
            ValueError: If the identifier is not normalized.
        """
        if not CONTRACT_NAME_PATTERN.fullmatch(value):
            raise ValueError("Contract identifiers must use lower-case letters, digits, and underscores.")

        return value

    @model_validator(mode="after")
    def validate_unique_tables(self) -> "SchemaContractConfig":
        """
        Ensure each table is owned once by the contract.

        Returns:
            Current validated schema contract.

        Raises:
            ValueError: If duplicate table contracts are present.
        """
        names      = [table.qualified_name for table in self.tables]
        duplicates = sorted({name for name in names if names.count(name) > 1})

        if duplicates:
            raise ValueError(f"Duplicate schema contract tables: {duplicates}")

        return self


# --- Defining Config Functions
def resolve_schema_contract_path(contract_name: str) -> Path:
    """
    Resolve one allowlisted runtime contract name.

    Args:
        contract_name: Contract alias supplied by CLI or Airflow.

    Returns:
        Absolute YAML path.

    Raises:
        ValueError: If the alias is not allowlisted.
    """
    normalized = contract_name.strip().lower()

    if normalized not in SCHEMA_CONTRACT_PATHS:
        raise ValueError(f"Unknown schema contract: {contract_name}")

    return SCHEMA_CONTRACT_PATHS[normalized]


def load_schema_contract(path: str | Path) -> SchemaContractConfig:
    """
    Load and validate one schema contract YAML file.

    Args:
        path: Contract YAML path.

    Returns:
        Validated SchemaContractConfig.

    Raises:
        FileNotFoundError: If the contract does not exist.
        ValueError: If the YAML root or typed contract is invalid.
    """
    config_path = Path(path)
    logger.info("Loading schema contract | path=%s", config_path)

    if not config_path.is_file():
        raise FileNotFoundError(f"Schema contract not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as handle:
        raw: Any = yaml.safe_load(handle) or {}

    if not isinstance(raw, dict):
        raise ValueError(f"Schema contract root must be a mapping: {config_path}")

    config = SchemaContractConfig.model_validate(raw)

    logger.info(
        "Schema contract validated | contract=%s dataset=%s tables=%d columns=%d",
        config.contract_name,
        config.dataset,
        len(config.tables),
        sum(len(table.columns) for table in config.tables),
    )

    return config


def load_named_schema_contract(contract_name: str) -> tuple[SchemaContractConfig, Path]:
    """
    Resolve and load one allowlisted schema contract.

    Args:
        contract_name: Allowlisted contract alias.

    Returns:
        Tuple containing validated config and source path.
    """
    path   = resolve_schema_contract_path(contract_name)
    config = load_schema_contract(path)

    return config, path
