####
## Seeding Helper Functions for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
import random
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict

from pipelines.common.logging import logger
from pipelines.seeding.config import OrdersSeedConfig


# --- Defining Constants
COUNTRIES = ["ID", "SG", "MY", "TH", "VN"]
CHANNELS  = ["organic", "paid", "referral", "direct", "affiliate"]
STATUSES  = ["paid", "cancelled", "refunded", "pending"]


COUNTRY_PROFILES: Dict[str, Dict] = {
    "ID": {
        "base_orders": 320,
        "currency": "IDR",
        "local_aov": 110_000,
        "fx_to_usd": 1 / 15_600,
    },
    "SG": {
        "base_orders": 60,
        "currency": "SGD",
        "local_aov": 36,
        "fx_to_usd": 0.74,
    },
    "MY": {
        "base_orders": 90,
        "currency": "MYR",
        "local_aov": 56,
        "fx_to_usd": 0.21,
    },
    "TH": {
        "base_orders": 110,
        "currency": "THB",
        "local_aov": 360,
        "fx_to_usd": 0.028,
    },
    "VN": {
        "base_orders": 130,
        "currency": "VND",
        "local_aov": 230_000,
        "fx_to_usd": 1 / 25_500,
    },
}


CHANNEL_PROFILES: Dict[str, Dict] = {
    "organic": {
        "order_mult": 1.10,
        "aov_mult": 1.00,
        "discount_range": (0.00, 0.06),
        "status_weights": {"paid": 0.87, "cancelled": 0.06, "refunded": 0.04, "pending": 0.03},
    },
    "paid": {
        "order_mult": 0.92,
        "aov_mult": 0.96,
        "discount_range": (0.02, 0.12),
        "status_weights": {"paid": 0.80, "cancelled": 0.10, "refunded": 0.05, "pending": 0.05},
    },
    "referral": {
        "order_mult": 0.58,
        "aov_mult": 1.02,
        "discount_range": (0.00, 0.08),
        "status_weights": {"paid": 0.83, "cancelled": 0.08, "refunded": 0.05, "pending": 0.04},
    },
    "direct": {
        "order_mult": 0.92,
        "aov_mult": 1.01,
        "discount_range": (0.00, 0.05),
        "status_weights": {"paid": 0.89, "cancelled": 0.05, "refunded": 0.03, "pending": 0.03},
    },
    "affiliate": {
        "order_mult": 0.36,
        "aov_mult": 0.95,
        "discount_range": (0.03, 0.15),
        "status_weights": {"paid": 0.78, "cancelled": 0.10, "refunded": 0.07, "pending": 0.05},
    },
}


# --- Defining Functions
def utc_now() -> datetime:
    """
    Return the current UTC timestamp.

    Returns:
        Timezone-aware datetime in UTC.
    """
    return datetime.now(timezone.utc)


def q2(x: float) -> float:
    """
    Round a numeric value to two decimal places.

    Args:
        x: Numeric value to round.

    Returns:
        Float rounded to two decimal places.
    """
    return round(float(x), 2)


def q6(x: float) -> float:
    """
    Round a numeric value to six decimal places.

    Args:
        x: Numeric value to round.

    Returns:
        Float rounded to six decimal places.
    """
    return round(float(x), 6)


WEEKDAY_NAME_BY_INDEX = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]


def parse_date(value: str) -> date:
    """
    Parse an ISO-8601 date string.

    Args:
        value: Date string in YYYY-MM-DD format.

    Returns:
        Parsed date object.

    Raises:
        ValueError: If the value is not a valid ISO date.
    """
    parsed = date.fromisoformat(value)
    logger.debug("Parsed date | value=%s parsed=%s", value, parsed)

    return parsed


def iter_dates(start_date: date, end_date: date) -> list[date]:
    """
    Build an inclusive list of dates for backfill-friendly loops.

    Args:
        start_date: Inclusive start date.
        end_date: Inclusive end date.

    Returns:
        List of dates between start_date and end_date, inclusive.

    Raises:
        ValueError: If end_date is earlier than start_date.
    """
    if end_date < start_date:
        raise ValueError(f"end_date must be >= start_date; start={start_date} end={end_date}")

    total_days = (end_date - start_date).days + 1
    dates      = [start_date + timedelta(days=offset) for offset in range(total_days)]

    logger.info("Built date range | start=%s end=%s total_days=%d", start_date, end_date, len(dates))

    return dates


def weekday_key(d: date) -> str:
    """
    Return the lowercase weekday key used by seeding YAML config.

    Args:
        d: Business date.

    Returns:
        Weekday key such as "monday" or "sunday".
    """
    return WEEKDAY_NAME_BY_INDEX[d.weekday()]


def stable_day_index(d: date, total_days: int) -> int:
    """
    Derive a deterministic trend position from a business date.

    Args:
        d: Business date being generated.
        total_days: Size of the synthetic trend window.

    Returns:
        Zero-based day index that is stable for the same date.

    Raises:
        ValueError: If total_days is less than one.
    """
    if total_days < 1:
        raise ValueError("total_days must be >= 1")

    # Date-derived indexing keeps reruns idempotent even when backfill windows differ.
    day_index = d.toordinal() % total_days

    logger.debug("Resolved stable day index | dt=%s total_days=%d day_index=%d", d, total_days, day_index)

    return day_index


def day_seasonality(d: date, config: OrdersSeedConfig | None = None) -> float:
    """
    Return the weekday volume multiplier for a business date.

    Args:
        d: Business date being generated.
        config: Optional orders seeding config. When omitted, legacy defaults are used.

    Returns:
        Positive seasonality multiplier. Fridays are slightly higher and
        weekends are slightly lower to mimic ecommerce patterns.
    """
    if config:
        multiplier = config.seasonality[weekday_key(d)]
        logger.debug("Resolved config seasonality | dt=%s multiplier=%.3f", d, multiplier)

        return multiplier

    weekday = d.weekday()

    if weekday == 4:  # Friday
        return 1.08

    if weekday in (5, 6):  # Weekend
        return 0.90

    return 1.00


def trend_multiplier(
    day_index: int,
    total_days: int,
    growth_ratio: float = 0.08,
    enabled: bool = True,
) -> float:
    """
    Return the gradual growth multiplier for a date range position.

    Args:
        day_index: Zero-based index of the generated date in the range.
        total_days: Total number of generated days in the range.
        growth_ratio: Total proportional growth across the full date range.
        enabled: Whether trend should be applied.

    Returns:
        Growth multiplier, starting at 1.0 and ending near 1 + growth_ratio.
    """
    if not enabled or total_days <= 1:
        return 1.0

    return 1.0 + (growth_ratio * (day_index / (total_days - 1)))


def weighted_choice(rng: random.Random, weights: Dict[str, float]) -> str:
    """
    Select one key from a probability-weight mapping.

    Args:
        rng: Random generator instance used for deterministic selection.
        weights: Mapping of item name to probability weight. Expected to sum to 1.0.

    Returns:
        Selected key from the weight mapping.
    """
    roll       = rng.random()
    cumulative = 0.0

    for key, weight in weights.items():
        cumulative += weight

        if roll <= cumulative:
            return key

    # Floating point drift can leave a tiny uncovered tail; return the final bucket.
    return list(weights.keys())[-1]


def random_time_in_day(rng: random.Random, d: date) -> datetime:
    """
    Generate a random UTC timestamp inside one business date.

    Args:
        rng: Random generator instance used for deterministic selection.
        d: Business date for the generated timestamp.

    Returns:
        Timezone-aware datetime in UTC within the provided date.
    """
    hour   = rng.randint(0, 23)
    minute = rng.randint(0, 59)
    second = rng.randint(0, 59)

    return datetime.combine(d, time(hour=hour, minute=minute, second=second), tzinfo=timezone.utc)


def build_order_count(
    rng: random.Random,
    order_date: date,
    country: str,
    channel: str,
    day_index: int,
    total_days: int,
    config: OrdersSeedConfig | None = None,
) -> int:
    """
    Compute synthetic order volume for one country/channel/date segment.

    Args:
        rng: Random generator instance used for deterministic volume noise.
        order_date: Business date being generated.
        country: Country code from COUNTRY_PROFILES.
        channel: Channel name from CHANNEL_PROFILES.
        day_index: Zero-based position in the generated date range.
        total_days: Total number of generated days in the range.
        config: Optional config-driven profile source. When omitted, legacy constants are used.

    Returns:
        Non-negative integer order count for the segment.
    """
    if config:
        country_base = config.countries[country].base_orders
        channel_mult = config.channels[channel].order_multiplier
        seasonal     = day_seasonality(order_date, config=config)
        trend        = trend_multiplier(
            day_index=day_index,
            total_days=total_days,
            growth_ratio=config.trend.total_growth_ratio,
            enabled=config.trend.enabled,
        )
        noise = rng.uniform(*config.order_count_noise_range)

    else:
        country_base = COUNTRY_PROFILES[country]["base_orders"]
        channel_mult = CHANNEL_PROFILES[channel]["order_mult"]
        seasonal     = day_seasonality(order_date)
        trend        = trend_multiplier(day_index, total_days)
        noise        = rng.uniform(0.90, 1.12)

    count       = int(round(country_base * channel_mult * seasonal * trend * noise))
    final_count = max(0, count)

    logger.debug(
        "Built order count | dt=%s country=%s channel=%s count=%d seasonal=%.3f trend=%.3f noise=%.3f",
        order_date,
        country,
        channel,
        final_count,
        seasonal,
        trend,
        noise,
    )
    return final_count


def build_order_event(
    rng: random.Random,
    order_date: date,
    country: str,
    channel: str,
    sequence: int,
    day_index: int,
    total_days: int,
    config: OrdersSeedConfig | None = None,
    incident_scenario: str = "baseline",
) -> Dict:
    """
    Build one event-level synthetic order record.

    Args:
        rng: Random generator instance used for deterministic values.
        order_date: Business date for the order.
        country: Country code from COUNTRY_PROFILES.
        channel: Channel name from CHANNEL_PROFILES.
        sequence: Monotonic sequence used to create a stable order_id.
        day_index: Zero-based position in the generated date range.
        total_days: Total number of generated days in the range.
        config: Optional config-driven profile source. When omitted, legacy constants are used.
        incident_scenario: Scenario label stored with the generated row for evaluation later.

    Returns:
        Dictionary representing one raw order event ready for DataFrame/Parquet output.
    """
    if config:
        country_cfg = config.countries[country]
        channel_cfg = config.channels[channel]

        currency              = country_cfg.currency
        local_aov_base        = country_cfg.local_aov
        fx_rate_base          = country_cfg.fx_to_usd
        aov_multiplier        = channel_cfg.aov_multiplier
        amount_stddev_ratio   = config.generation.amount_stddev_ratio
        fx_noise_range        = config.generation.fx_noise_range
        ingestion_delay_range = config.generation.ingestion_delay_minutes
        discount_range        = channel_cfg.discount_range
        status_weights        = channel_cfg.status_weights
        source_system_weights = config.source_system_weights
        order_id_prefix       = config.generation.order_id_prefix
        customer_id_prefix    = config.generation.customer_id_prefix
        is_test               = config.generation.is_test
        version               = config.generation.business_date_version

    else:
        country_cfg = COUNTRY_PROFILES[country]
        channel_cfg = CHANNEL_PROFILES[channel]

        currency              = country_cfg["currency"]
        local_aov_base        = country_cfg["local_aov"]
        fx_rate_base          = country_cfg["fx_to_usd"]
        aov_multiplier        = channel_cfg["aov_mult"]
        amount_stddev_ratio   = 0.28
        fx_noise_range        = [0.995, 1.005]
        ingestion_delay_range = [1, 180]
        discount_range        = channel_cfg["discount_range"]
        status_weights        = channel_cfg["status_weights"]
        source_system_weights = {"orders_api": 0.72, "mobile_checkout": 0.18, "partner_portal": 0.10}
        order_id_prefix       = "ORD"
        customer_id_prefix    = "CUST"
        is_test               = False
        version               = 1

    order_ts     = random_time_in_day(rng, order_date)
    ingestion_ts = order_ts + timedelta(minutes=rng.randint(*ingestion_delay_range))

    local_aov    = local_aov_base * aov_multiplier
    local_amount = max(1.0, rng.gauss(local_aov, local_aov * amount_stddev_ratio))
    local_amount = q2(local_amount)

    fx_rate   = q6(fx_rate_base * rng.uniform(*fx_noise_range))
    gross_usd = q2(local_amount * fx_rate)

    discount_pct = rng.uniform(*discount_range)
    discount_usd = q2(gross_usd * discount_pct)

    status                 = weighted_choice(rng, status_weights)
    refund_amount_usd      = 0.0
    recognized_revenue_usd = 0.0

    if status == "paid":
        recognized_revenue_usd = q2(max(gross_usd - discount_usd, 0))

    elif status == "refunded":
        refund_amount_usd      = q2(max(gross_usd - discount_usd, 0))
        recognized_revenue_usd = 0.0

    else:
        recognized_revenue_usd = 0.0

    source_system = weighted_choice(rng, source_system_weights)

    customer_id = f"{customer_id_prefix}-{country}-{rng.randint(100000, 999999)}"
    order_id    = f"{order_id_prefix}-{order_date.strftime('%Y%m%d')}-{country}-{channel[:2].upper()}-{sequence:08d}"

    # Keep per-row logging at DEBUG level; event generation can produce thousands of rows.
    logger.debug("Built order event | order_id=%s dt=%s country=%s channel=%s", order_id, order_date, country, channel)

    return {
        "dt": order_date,
        "order_id": order_id,
        "order_date": order_date,
        "order_ts": order_ts,
        "ingestion_ts": ingestion_ts,
        "customer_id": customer_id,
        "country": country,
        "channel": channel,
        "status": status,
        "currency": currency,
        "gross_amount_local": local_amount,
        "fx_rate_to_usd": fx_rate,
        "gross_amount_usd": gross_usd,
        "discount_usd": discount_usd,
        "refund_amount_usd": refund_amount_usd,
        "recognized_revenue_usd": recognized_revenue_usd,
        "source_system": source_system,
        "is_test": is_test,
        "business_date_version": version,
        "incident_scenario": incident_scenario,
    }
