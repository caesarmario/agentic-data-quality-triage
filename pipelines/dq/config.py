####
## Data Quality Config Loader for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Add repo root before importing project packages when this file is executed by path.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipelines.common.clickhouse import validate_column_name, validate_qualified_table_name
from pipelines.common.logging import logger


DEFAULT_DQ_CONTRACT_PATH = PROJECT_ROOT / "configs" / "dq" / "orders_contract.yml"


class ExpectedSegmentsConfig(BaseModel):
    """
    Expected business dimensions for the orders dataset.

    Attributes:
        countries: Country codes expected in daily aggregates.
        channels: Acquisition channels expected in daily aggregates.
        statuses: Order lifecycle statuses allowed in event rows.
    """

    countries: List[str]
    channels: List[str]
    statuses: List[str]

    @property
    def expected_segment_count(self) -> int:
        """
        Calculate the expected country-channel grain count.

        Returns:
            Number of expected country and channel combinations.
        """
        return len(self.countries) * len(self.channels)


class ProfilingConfig(BaseModel):
    """
    Configuration for deterministic profile metrics.

    Attributes:
        enabled: Whether profiling should run.
        row_count_tables: Tables where row counts are collected.
        null_rate_columns: Mapping of table name to columns profiled for null/blank rate.
        distinct_count_columns: Mapping of table name to columns profiled for distinct count.
        revenue_metric_table: Mart table used for revenue total profiling.
        segment_metric_table: Mart table used for country-channel coverage profiling.
    """

    enabled: bool                            = True
    row_count_tables: List[str]              = Field(default_factory=list)
    null_rate_columns: Dict[str, List[str]]  = Field(default_factory=dict)
    distinct_count_columns: Dict[str, List[str]] = Field(default_factory=dict)
    revenue_metric_table: str
    segment_metric_table: str

    @model_validator(mode="after")
    def validate_profile_targets(self) -> "ProfilingConfig":
        """
        Validate profiling table and column identifiers.

        Returns:
            Current config instance when all identifiers are safe.

        Raises:
            ValueError: If table or column identifiers are unsafe.
        """
        for table_name in self.row_count_tables:
            validate_qualified_table_name(table_name)

        for table_name, columns in self.null_rate_columns.items():
            validate_qualified_table_name(table_name)
            for column in columns:
                validate_column_name(column)

        for table_name, columns in self.distinct_count_columns.items():
            validate_qualified_table_name(table_name)
            for column in columns:
                validate_column_name(column)

        validate_qualified_table_name(self.revenue_metric_table)
        validate_qualified_table_name(self.segment_metric_table)

        return self


class FreshnessCheckConfig(BaseModel):
    """
    Freshness check settings for date-partitioned tables.

    Attributes:
        table_name: Fully qualified table name to check.
        date_column: Date column used to measure freshness.
        max_lag_days: Maximum acceptable day lag from the target dt.
        severity: Alert severity when freshness fails.
    """

    table_name: str
    date_column: str = "dt"
    max_lag_days: int = Field(ge=0)
    severity: str = "critical"

    @model_validator(mode="after")
    def validate_identifiers(self) -> "FreshnessCheckConfig":
        """
        Validate table and date column identifiers.

        Returns:
            Current config instance when identifiers are safe.
        """
        validate_qualified_table_name(self.table_name)
        validate_column_name(self.date_column)

        return self


class RowCountPositiveCheckConfig(BaseModel):
    """
    Row-count minimum check settings for one table.

    Attributes:
        table_name: Fully qualified table name to check.
        min_rows: Minimum rows expected for the target dt.
        severity: Alert severity when the check fails.
    """

    table_name: str
    min_rows: int = Field(ge=0)
    severity: str = "critical"

    @field_validator("table_name")
    @classmethod
    def validate_table_name(cls, value: str) -> str:
        """
        Validate the table identifier.

        Args:
            value: Fully qualified ClickHouse table name.

        Returns:
            The original table name after validation.
        """
        return validate_qualified_table_name(value)


class NotNullCheckConfig(BaseModel):
    """
    Required-column completeness check settings.

    Attributes:
        table_name: Fully qualified table name to check.
        columns: Columns that must not be null or blank.
        severity: Alert severity when the check fails.
    """

    table_name: str
    columns: List[str]
    severity: str = "critical"

    @model_validator(mode="after")
    def validate_identifiers(self) -> "NotNullCheckConfig":
        """
        Validate target table and column identifiers.

        Returns:
            Current config instance when identifiers are safe.
        """
        validate_qualified_table_name(self.table_name)
        for column in self.columns:
            validate_column_name(column)

        return self


class AcceptedValuesCheckConfig(BaseModel):
    """
    Accepted-values check settings for categorical columns.

    Attributes:
        table_name: Fully qualified table name to check.
        columns: Mapping of column name to accepted values.
        severity: Alert severity when invalid values are found.
    """

    table_name: str
    columns: Dict[str, List[str]]
    severity: str = "critical"

    @model_validator(mode="after")
    def validate_identifiers(self) -> "AcceptedValuesCheckConfig":
        """
        Validate target table and column identifiers.

        Returns:
            Current config instance when identifiers are safe.
        """
        validate_qualified_table_name(self.table_name)
        for column in self.columns:
            validate_column_name(column)

        return self


class SegmentCoverageCheckConfig(BaseModel):
    """
    Segment coverage check settings for daily marts.

    Attributes:
        table_name: Fully qualified mart table name.
        min_coverage_ratio: Minimum observed/expected segment ratio.
        severity: Alert severity when segment coverage is incomplete.
    """

    table_name: str
    min_coverage_ratio: float = Field(ge=0.0, le=1.0)
    severity: str = "warning"

    @field_validator("table_name")
    @classmethod
    def validate_table_name(cls, value: str) -> str:
        """
        Validate the table identifier.

        Args:
            value: Fully qualified ClickHouse table name.

        Returns:
            The original table name after validation.
        """
        return validate_qualified_table_name(value)


class RowCountAnomalyCheckConfig(BaseModel):
    """
    Row-count anomaly check settings based on historical daily totals.

    Attributes:
        table_name: Fully qualified mart table name.
        lookback_days: Number of previous days used for baseline comparison.
        min_history_days: Minimum historical days required before evaluating.
        lower_ratio: Lower acceptable current/baseline ratio.
        upper_ratio: Upper acceptable current/baseline ratio.
        severity: Alert severity when the check fails.
    """

    table_name: str
    lookback_days: int = Field(ge=1)
    min_history_days: int = Field(ge=1)
    lower_ratio: float = Field(gt=0.0)
    upper_ratio: float = Field(gt=0.0)
    severity: str = "warning"

    @model_validator(mode="after")
    def validate_anomaly_settings(self) -> "RowCountAnomalyCheckConfig":
        """
        Validate row-count anomaly settings.

        Returns:
            Current config instance when settings are valid.

        Raises:
            ValueError: If lower_ratio is greater than upper_ratio.
        """
        validate_qualified_table_name(self.table_name)

        if self.lower_ratio > self.upper_ratio:
            raise ValueError("rowcount_anomaly.lower_ratio must be <= upper_ratio")

        return self


class RateCheckConfig(BaseModel):
    """
    Rate threshold check settings for duplicate and late-arriving metrics.

    Attributes:
        table_name: Fully qualified mart table name.
        max_rate: Maximum accepted rate.
        severity: Alert severity when the rate is above threshold.
    """

    table_name: str
    max_rate: float = Field(ge=0.0, le=1.0)
    severity: str = "warning"

    @field_validator("table_name")
    @classmethod
    def validate_table_name(cls, value: str) -> str:
        """
        Validate the table identifier.

        Args:
            value: Fully qualified ClickHouse table name.

        Returns:
            The original table name after validation.
        """
        return validate_qualified_table_name(value)


class RevenueNonNegativeCheckConfig(BaseModel):
    """
    Revenue floor check settings for mart metrics.

    Attributes:
        table_name: Fully qualified mart table name.
        metric_column: Revenue metric column that should not be negative.
        severity: Alert severity when negative revenue is observed.
    """

    table_name: str
    metric_column: str
    severity: str = "critical"

    @model_validator(mode="after")
    def validate_identifiers(self) -> "RevenueNonNegativeCheckConfig":
        """
        Validate target table and metric column identifiers.

        Returns:
            Current config instance when identifiers are safe.
        """
        validate_qualified_table_name(self.table_name)
        validate_column_name(self.metric_column)

        return self


class QualityConfig(BaseModel):
    """
    Full deterministic quality-check configuration for orders.

    Attributes:
        freshness: Freshness check settings.
        row_count_positive: Row-count minimum checks.
        not_null: Required-column completeness check settings.
        accepted_values: Accepted-values check settings.
        segment_coverage: Segment coverage check settings.
        rowcount_anomaly: Historical row-count anomaly settings.
        duplicate_rate: Duplicate-rate threshold settings.
        late_arriving_rate: Late-arriving-rate threshold settings.
        revenue_non_negative: Revenue floor check settings.
    """

    freshness: FreshnessCheckConfig
    row_count_positive: List[RowCountPositiveCheckConfig]
    not_null: NotNullCheckConfig
    accepted_values: AcceptedValuesCheckConfig
    segment_coverage: SegmentCoverageCheckConfig
    rowcount_anomaly: RowCountAnomalyCheckConfig
    duplicate_rate: RateCheckConfig
    late_arriving_rate: RateCheckConfig
    revenue_non_negative: RevenueNonNegativeCheckConfig


class AlertsConfig(BaseModel):
    """
    Alert generation settings derived from DQ check results.

    Attributes:
        fail_statuses: DQ statuses that create critical/failure alerts.
        warn_statuses: DQ statuses that create warning alerts.
        open_status: Status used for newly created alerts.
        alert_type: Alert type written to dq.alerts.
    """

    fail_statuses: List[str] = Field(default_factory=lambda: ["fail"])
    warn_statuses: List[str] = Field(default_factory=lambda: ["warn"])
    open_status: str = "open"
    alert_type: str = "dq_failure"


class OrdersDqContract(BaseModel):
    """
    Top-level typed contract for orders data quality and profiling.

    Attributes:
        dataset: Dataset name.
        description: Human-readable contract description.
        tables: Named ClickHouse table mapping.
        expected_segments: Expected country, channel, and status values.
        profiling: Profiling configuration.
        quality: Deterministic quality-check configuration.
        alerts: Alert generation configuration.
    """

    dataset: str
    description: str = ""
    tables: Dict[str, str]
    expected_segments: ExpectedSegmentsConfig
    profiling: ProfilingConfig
    quality: QualityConfig
    alerts: AlertsConfig

    @field_validator("tables")
    @classmethod
    def validate_tables(cls, value: Dict[str, str]) -> Dict[str, str]:
        """
        Validate named ClickHouse tables from the contract.

        Args:
            value: Mapping of logical table name to fully qualified ClickHouse table.

        Returns:
            The original table mapping after validation.
        """
        for table_name in value.values():
            validate_qualified_table_name(table_name)

        return value


def load_orders_dq_contract(path: str | Path | None = None) -> OrdersDqContract:
    """
    Load and validate the orders DQ YAML contract.

    Args:
        path: Optional config path. Defaults to configs/dq/orders_contract.yml.

    Returns:
        Validated OrdersDqContract instance.

    Raises:
        FileNotFoundError: If the contract file does not exist.
        ValueError: If YAML is malformed or violates the typed schema.
    """
    contract_path = Path(path) if path else DEFAULT_DQ_CONTRACT_PATH
    logger.info("Loading orders DQ contract | path=%s", contract_path)

    raw      = _load_yaml(contract_path)
    contract = OrdersDqContract.model_validate(raw)

    logger.info(
        "Orders DQ contract validated | dataset=%s tables=%d expected_segments=%d",
        contract.dataset,
        len(contract.tables),
        contract.expected_segments.expected_segment_count,
    )

    return contract


def _load_yaml(path: Path) -> Dict[str, Any]:
    """
    Read a YAML file and ensure the root object is a mapping.

    Args:
        path: Path to a YAML config file.

    Returns:
        Parsed YAML dictionary.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the YAML root object is not a mapping.
    """
    if not path.exists():
        logger.error("DQ contract file not found | path=%s", path)
        raise FileNotFoundError(f"DQ contract file not found: {path}")

    # Keep file parsing separate from Pydantic validation so errors stay easy to debug.
    with path.open("r", encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh) or {}

    if not isinstance(loaded, dict):
        logger.error("DQ contract root is not a mapping | path=%s root_type=%s", path, type(loaded).__name__)
        raise ValueError(f"DQ contract root must be a mapping: {path}")

    logger.info("YAML DQ contract loaded | path=%s keys=%s", path, sorted(loaded.keys()))

    return loaded


if __name__ == "__main__":
    contract = load_orders_dq_contract()
    print(
        {
            "dataset": contract.dataset,
            "tables": contract.tables,
            "expected_segments": contract.expected_segments.expected_segment_count,
        }
    )
