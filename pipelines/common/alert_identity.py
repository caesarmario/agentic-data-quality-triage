####
## Alert Identity Helpers for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from typing import Any

from pipelines.common.logging import logger


# --- Defining Constants
DEFAULT_ALERT_REF_PREFIX = "DQ"
ALERT_REF_PATTERN        = re.compile(r"^[A-Z]{2,8}-\d{8}-[A-Z0-9]{6,12}$")
DATE_PATTERN             = re.compile(r"\d{4}-\d{2}-\d{2}")


# --- Defining Generic Helpers
def get_payload_value(payload: Any, field_name: str, default: Any = None) -> Any:
    """
    Read a field from a dictionary-like or object-like payload.

    Args:
        payload: Dictionary, Pydantic model, object, or None.
        field_name: Field name to read.
        default: Fallback value when the field is missing.

    Returns:
        Field value or the provided default.
    """
    if payload is None:
        return default

    if isinstance(payload, dict):
        return payload.get(field_name, default)

    return getattr(payload, field_name, default)


def normalize_date_token(value: Any) -> str:
    """
    Convert a date-like value into a compact YYYYMMDD token.

    Args:
        value: Date, datetime, ISO date string, or None.

    Returns:
        YYYYMMDD date token, or 00000000 when the date is unknown.
    """
    if value is None or value == "":
        return "00000000"

    if isinstance(value, datetime):
        return value.date().strftime("%Y%m%d")

    if isinstance(value, date):
        return value.strftime("%Y%m%d")

    text = str(value).strip()

    if not text:
        return "00000000"

    # Keep the token readable even when ClickHouse returns datetime-like strings.
    cleaned = text[:10].replace("-", "")

    if cleaned.isdigit() and len(cleaned) == 8:
        return cleaned

    logger.warning("Unable to normalize alert date token | value=%s", text)

    return "00000000"


def infer_date_from_alert_key(alert_key: str) -> str:
    """
    Infer a YYYYMMDD date token from the stable system alert key.

    Args:
        alert_key: Internal alert key that usually contains a YYYY-MM-DD segment.

    Returns:
        YYYYMMDD token when found, otherwise 00000000.
    """
    match = DATE_PATTERN.search(alert_key or "")

    if not match:
        return "00000000"

    return normalize_date_token(match.group(0))


def build_short_hash(value: str, length: int = 6) -> str:
    """
    Build a short stable uppercase hash for a system identifier.

    Args:
        value: Source value to hash.
        length: Number of characters to keep from the hash.

    Returns:
        Uppercase hexadecimal hash prefix.
    """
    safe_length = max(6, min(length, 12))
    digest      = hashlib.sha1(value.encode("utf-8")).hexdigest().upper()

    return digest[:safe_length]


# --- Defining Alert Reference Helpers
def build_alert_ref(alert_key: str, dt: Any = None, prefix: str = DEFAULT_ALERT_REF_PREFIX) -> str:
    """
    Build a short human-facing alert reference from the stable system key.

    Args:
        alert_key: Internal stable alert key used by the platform.
        dt: Optional alert business date.
        prefix: Human-facing prefix for the reference.

    Returns:
        Stable display reference such as DQ-20260610-A1B2C3.
    """
    date_token = normalize_date_token(dt)

    if date_token == "00000000":
        date_token = infer_date_from_alert_key(alert_key)

    hash_token = build_short_hash(alert_key or "unknown-alert")
    safe_prefix = re.sub(r"[^A-Z0-9]", "", str(prefix or DEFAULT_ALERT_REF_PREFIX).upper()) or DEFAULT_ALERT_REF_PREFIX

    return f"{safe_prefix}-{date_token}-{hash_token}"


def is_alert_ref(value: str | None) -> bool:
    """
    Check whether a value looks like a human-facing alert reference.

    Args:
        value: Candidate reference string.

    Returns:
        True when the value matches the alert reference pattern.
    """
    return bool(value and ALERT_REF_PATTERN.match(value.strip().upper()))


def resolve_alert_ref(payload: Any) -> str:
    """
    Resolve a human-facing alert reference from an alert payload.

    Args:
        payload: Alert dictionary or Alert-like object.

    Returns:
        Existing alert_ref/alert_display_id, or a deterministic fallback.
    """
    existing_ref = (
        get_payload_value(payload, "alert_ref")
        or get_payload_value(payload, "alert_display_id")
        or get_payload_value(payload, "display_id")
    )

    if existing_ref:
        return str(existing_ref)

    alert_key = str(get_payload_value(payload, "alert_key", "") or "")
    dt        = get_payload_value(payload, "dt")

    return build_alert_ref(alert_key=alert_key, dt=dt)


# --- Defining Human Readability Helpers
def humanize_metric(metric: Any) -> str:
    """
    Convert an internal metric/check name into a plain-language issue label.

    Args:
        metric: Internal DQ metric or check name.

    Returns:
        Human-readable issue label.
    """
    normalized = str(metric or "").lower()

    if "row_count" in normalized:
        return "Missing or unusually low data volume"

    if "freshness" in normalized:
        return "Data freshness problem"

    if "segment" in normalized or "coverage" in normalized:
        return "Missing expected business segment"

    if "duplicate" in normalized:
        return "Duplicate records increased"

    if "null" in normalized:
        return "Unexpected null value increase"

    if "late_arriving" in normalized:
        return "Late-arriving data increased"

    if "accepted_values" in normalized:
        return "Unexpected value found"

    return "Data quality check needs review"


def build_alert_human_title(payload: Any) -> str:
    """
    Build a short human-readable title for an alert.

    Args:
        payload: Alert dictionary or Alert-like object.

    Returns:
        Plain-language alert title for bot/UI/report output.
    """
    issue      = humanize_metric(get_payload_value(payload, "metric"))
    table_name = str(get_payload_value(payload, "table_name", "unknown table") or "unknown table")
    dt         = str(get_payload_value(payload, "dt", "unknown date") or "unknown date")
    dimension  = str(get_payload_value(payload, "dimension", "") or "")

    if dimension:
        return f"{issue} in {table_name} for {dt} ({dimension})"

    return f"{issue} in {table_name} for {dt}"
