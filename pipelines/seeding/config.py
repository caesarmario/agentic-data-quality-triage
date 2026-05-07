####
## Seeding Config Loader for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import hashlib
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Add repo root before importing project packages when this file is executed by path.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipelines.common.logging import logger


# --- Defining Constants
DEFAULT_ORDERS_CONFIG_PATH = PROJECT_ROOT / "configs" / "seeding" / "orders.yml"
WEEKDAY_KEYS               = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]

DatasetName      = Literal["orders"]
TimezoneName     = Literal["UTC"]
LandingFormat    = Literal["parquet"]
CompressionCodec = Literal["snappy", "gzip", "brotli", "zstd", "none"]
PartitionColumn  = Literal["dt"]


# --- Defining Classes
class GenerationConfig(BaseModel):
    """
    Runtime controls for deterministic order-event generation.

    This model captures knobs that affect how one daily partition is generated,
    such as ID prefixes, ingestion delays, amount variance, and FX noise.
    Keeping these values typed prevents accidental invalid YAML from silently
    changing the shape of demo data.
    """

    timezone: TimezoneName             = "UTC"
    default_days: int                  = Field(default=60, ge=1, le=365)
    default_total_days_for_trend: int  = Field(default=90, ge=1, le=3650)
    order_id_prefix: str               = Field(default="ORD", min_length=1)
    customer_id_prefix: str            = Field(default="CUST", min_length=1)
    business_date_version: int         = Field(default=1, ge=1)
    is_test: bool                      = False
    amount_stddev_ratio: float         = Field(default=0.28, ge=0.0, le=2.0)
    fx_noise_range: List[float]        = Field(default_factory=lambda: [0.995, 1.005])
    ingestion_delay_minutes: List[int] = Field(default_factory=lambda: [1, 180])

    @field_validator("fx_noise_range")
    @classmethod
    def validate_fx_noise_range(cls, value: List[float]) -> List[float]:
        """
        Validate the configured FX noise range.

        Args:
            value: Two numeric values representing inclusive min/max FX multiplier noise.

        Returns:
            The original range when it is valid.

        Raises:
            ValueError: If the range is malformed or contains a negative lower bound.
        """
        return _validate_range(value, "fx_noise_range", min_value=0.0)

    @field_validator("ingestion_delay_minutes")
    @classmethod
    def validate_ingestion_delay_minutes(cls, value: List[int]) -> List[int]:
        """
        Validate ingestion lag bounds used by the synthetic event generator.

        Args:
            value: Two integers, [min_minutes, max_minutes].

        Returns:
            The original delay range when min/max are valid.

        Raises:
            ValueError: If the range is malformed or min/max are inconsistent.
        """
        if len(value) != 2 or value[0] < 0 or value[1] < value[0]:
            raise ValueError("ingestion_delay_minutes must be [min, max] with 0 <= min <= max")
        return value


class TrendConfig(BaseModel):
    """
    Optional long-term trend applied across a generated date range.

    The current generator uses this to create a small production-like growth
    pattern, so rowcount/revenue baselines are not perfectly flat.
    """

    enabled: bool               = True
    total_growth_ratio: float   = Field(default=0.08, ge=-0.95, le=10.0)


class CountryProfile(BaseModel):
    """
    Country-level distribution profile for generated orders.

    Attributes:
        base_orders: Baseline daily order volume before channel and seasonal effects.
        currency: Local transaction currency.
        local_aov: Average order value in local currency.
        fx_to_usd: Approximate conversion rate to USD.
    """

    base_orders: int    = Field(ge=0)
    currency: str       = Field(min_length=3, max_length=3)
    local_aov: float    = Field(gt=0)
    fx_to_usd: float    = Field(gt=0)


class ChannelProfile(BaseModel):
    """
    Channel-level distribution profile for generated orders.

    Attributes:
        order_multiplier: Volume multiplier applied to the country baseline.
        aov_multiplier: Average order value multiplier for the channel.
        discount_range: Min/max discount percentage applied to gross USD.
        status_weights: Probability distribution for order status values.
    """

    order_multiplier: float         = Field(gt=0)
    aov_multiplier: float           = Field(gt=0)
    discount_range: List[float]
    status_weights: Dict[str, float]

    @field_validator("discount_range")
    @classmethod
    def validate_discount_range(cls, value: List[float]) -> List[float]:
        """
        Validate the min/max discount percentage range.

        Args:
            value: Two floats, [min_discount, max_discount], each between 0 and 1.

        Returns:
            The original range when it is valid.

        Raises:
            ValueError: If min/max are malformed or out of percentage bounds.
        """
        return _validate_range(value, "discount_range", min_value=0.0, max_value=1.0)

    @field_validator("status_weights")
    @classmethod
    def validate_status_weights(cls, value: Dict[str, float]) -> Dict[str, float]:
        """
        Validate order status probabilities for one channel.

        Args:
            value: Mapping of status name to probability weight.

        Returns:
            The original weight mapping when it sums to roughly 1.0.

        Raises:
            ValueError: If the mapping is empty, negative, or not normalized.
        """
        return _validate_weights(value, "status_weights")


class OutputConfig(BaseModel):
    """
    Landing-file output contract for generated orders.

    This model centralizes local and S3 path conventions so the generator,
    uploader, loader, and Airflow DAGs can share the same partition layout.
    """

    local_dir: Path
    bucket: str                         = Field(min_length=3)
    prefix: str                         = Field(min_length=1)
    format: LandingFormat               = "parquet"
    compression: CompressionCodec       = "snappy"
    partition_column: PartitionColumn   = "dt"
    file_template: str                  = "orders_{dt}.parquet"

    def partition_prefix(self, dt: date) -> str:
        """
        Build the S3 prefix for one partition date.

        Args:
            dt: Business date represented by the landing partition.

        Returns:
            S3 key prefix such as "orders/dt=2026-05-03".
        """
        return f"{self.prefix}/{self.partition_column}={dt.isoformat()}"

    def object_key(self, dt: date) -> str:
        """
        Build the full S3 object key for one generated Parquet file.

        Args:
            dt: Business date represented by the landing partition.

        Returns:
            Full S3 object key under the configured dataset prefix.
        """
        return f"{self.partition_prefix(dt)}/{self.file_template.format(dt=dt.isoformat())}"

    def local_path(self, dt: date) -> Path:
        """
        Build the local Parquet output path for one partition date.

        Args:
            dt: Business date represented by the local partition.

        Returns:
            Path to the generated local Parquet file.
        """
        return self.local_dir / self.prefix / f"{self.partition_column}={dt.isoformat()}" / self.file_template.format(
            dt=dt.isoformat()
        )


class OrdersSeedConfig(BaseModel):
    """
    Top-level typed config for synthetic order-event generation.

    The YAML file is intentionally lightweight: it owns scenario-friendly
    parameters and data contracts, while Python keeps the actual generation
    logic testable and reusable.
    """

    dataset: DatasetName
    description: str                    = ""
    global_seed: int                    = Field(ge=0)
    generation: GenerationConfig
    seasonality: Dict[str, float]
    trend: TrendConfig                  = Field(default_factory=TrendConfig)
    order_count_noise_range: List[float]
    source_system_weights: Dict[str, float]
    countries: Dict[str, CountryProfile]
    channels: Dict[str, ChannelProfile]
    output: OutputConfig

    @field_validator("seasonality")
    @classmethod
    def validate_seasonality(cls, value: Dict[str, float]) -> Dict[str, float]:
        """
        Validate weekday seasonality multipliers.

        Args:
            value: Mapping of weekday name to positive volume multiplier.

        Returns:
            The original seasonality mapping when all weekdays are present.

        Raises:
            ValueError: If any weekday is missing or has a non-positive multiplier.
        """
        missing = [day for day in WEEKDAY_KEYS if day not in value]
        if missing:
            raise ValueError(f"seasonality missing weekday keys: {missing}")
        for day, multiplier in value.items():
            if multiplier <= 0:
                raise ValueError(f"seasonality multiplier must be positive for {day}")
        return value

    @field_validator("order_count_noise_range")
    @classmethod
    def validate_order_count_noise_range(cls, value: List[float]) -> List[float]:
        """
        Validate random volume noise bounds for generated order counts.

        Args:
            value: Two floats, [min_multiplier, max_multiplier].

        Returns:
            The original range when it is valid.

        Raises:
            ValueError: If min/max are malformed or negative.
        """
        return _validate_range(value, "order_count_noise_range", min_value=0.0)

    @field_validator("source_system_weights")
    @classmethod
    def validate_source_system_weights(cls, value: Dict[str, float]) -> Dict[str, float]:
        """
        Validate source-system probabilities for generated orders.

        Args:
            value: Mapping of source system name to probability weight.

        Returns:
            The original weight mapping when it sums to roughly 1.0.

        Raises:
            ValueError: If the mapping is empty, negative, or not normalized.
        """
        return _validate_weights(value, "source_system_weights")

    @model_validator(mode="after")
    def validate_profiles(self) -> "OrdersSeedConfig":
        """
        Validate that the config has enough profiles to generate data.

        Returns:
            The current config instance when country and channel profiles exist.

        Raises:
            ValueError: If countries or channels are empty.
        """
        if not self.countries:
            raise ValueError("countries must contain at least one profile")
        if not self.channels:
            raise ValueError("channels must contain at least one profile")
        return self

    @property
    def country_codes(self) -> List[str]:
        """
        Return configured country codes in YAML order.

        Returns:
            List of configured country code strings.
        """
        return list(self.countries.keys())

    @property
    def channel_names(self) -> List[str]:
        """
        Return configured channel names in YAML order.

        Returns:
            List of configured channel name strings.
        """
        return list(self.channels.keys())

    def seed_for_date(self, dt: date, namespace: str = "orders") -> int:
        """
        Build a stable deterministic random seed for one date.

        Args:
            dt: Business date being generated.
            namespace: Optional namespace to derive independent deterministic streams.

        Returns:
            Integer seed derived from global_seed, namespace, and date.
        """
        seed_input = f"{namespace}|{self.global_seed}|{dt.isoformat()}".encode("utf-8")
        return int(hashlib.sha256(seed_input).hexdigest()[:16], 16)


# --- Defining Functions
def load_orders_config(path: str | Path | None = None) -> OrdersSeedConfig:
    """
    Load and validate the orders seeding YAML config.

    Args:
        path: Optional config path. Defaults to configs/seeding/orders.yml.

    Returns:
        Validated OrdersSeedConfig instance.

    Raises:
        FileNotFoundError: If the config file does not exist.
        ValueError: If YAML is malformed or does not match the expected schema.
    """
    config_path = Path(path) if path else DEFAULT_ORDERS_CONFIG_PATH
    logger.info("Loading orders seed config | path=%s", config_path)

    raw = _load_yaml(config_path)
    config = OrdersSeedConfig.model_validate(raw)

    logger.info(
        "Orders seed config validated | dataset=%s countries=%d channels=%d output=s3://%s/%s",
        config.dataset,
        len(config.countries),
        len(config.channels),
        config.output.bucket,
        config.output.prefix,
    )
    return config


def _load_yaml(path: Path) -> Dict[str, Any]:
    """
    Read a YAML file and ensure the root object is a mapping.

    Args:
        path: Path to a YAML config file.

    Returns:
        Parsed YAML dictionary.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the root YAML object is not a mapping.
    """
    if not path.exists():
        logger.error("Config file not found | path=%s", path)
        raise FileNotFoundError(f"Config file not found: {path}")

    # Parse YAML before Pydantic validation so schema errors stay separate from file errors.
    with path.open("r", encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh) or {}

    if not isinstance(loaded, dict):
        logger.error("Config root is not a mapping | path=%s root_type=%s", path, type(loaded).__name__)
        raise ValueError(f"Config root must be a mapping: {path}")
    logger.info("YAML config loaded | path=%s keys=%s", path, sorted(loaded.keys()))
    return loaded


def _validate_range(
    value: List[float],
    field_name: str,
    min_value: float | None = None,
    max_value: float | None = None,
) -> List[float]:
    """
    Validate a two-value numeric min/max range.

    Args:
        value: Two-value list in [min, max] order.
        field_name: Name used in validation error messages.
        min_value: Optional inclusive lower bound for the min value.
        max_value: Optional inclusive upper bound for the max value.

    Returns:
        The original range when valid.

    Raises:
        ValueError: If the range shape or bounds are invalid.
    """
    if len(value) != 2 or value[1] < value[0]:
        raise ValueError(f"{field_name} must be [min, max] with min <= max")
    if min_value is not None and value[0] < min_value:
        raise ValueError(f"{field_name} min must be >= {min_value}")
    if max_value is not None and value[1] > max_value:
        raise ValueError(f"{field_name} max must be <= {max_value}")
    return value


def _validate_weights(value: Dict[str, float], field_name: str) -> Dict[str, float]:
    """
    Validate a probability-weight mapping.

    Args:
        value: Mapping of item name to non-negative probability.
        field_name: Name used in validation error messages.

    Returns:
        The original mapping when weights sum to approximately 1.0.

    Raises:
        ValueError: If weights are empty, negative, or not normalized.
    """
    if not value:
        raise ValueError(f"{field_name} must not be empty")
    if any(weight < 0 for weight in value.values()):
        raise ValueError(f"{field_name} must not contain negative weights")
    total = sum(value.values())
    if not 0.999 <= total <= 1.001:
        raise ValueError(f"{field_name} must sum to 1.0; observed={total:.6f}")
    return value


# --- Running CLI Entrypoint
if __name__ == "__main__":
    config = load_orders_config()
    print(
        {
            "dataset": config.dataset,
            "countries": config.country_codes,
            "channels": config.channel_names,
            "output_bucket": config.output.bucket,
        }
    )
