####
## Human Display Helpers for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import hashlib
from typing import Any
from uuid import UUID

from pipelines.common.alert_identity import build_alert_ref as build_date_aware_alert_ref
from pipelines.common.logging import logger


# --- Defining Constants
DEFAULT_DISPLAY_HASH_LENGTH = 8

TABLE_DISPLAY_NAMES = {
    "dq.raw_orders": "raw orders data",
    "dq.stg_orders": "cleaned orders data",
    "dq.fct_orders_daily": "daily orders mart",
}


# --- Defining Generic Helpers
def get_display_value(model: Any, field_name: str, default: Any = "") -> Any:
    """
    Read a field from either a dictionary or a model-like object.

    Args:
        model: Dictionary, Pydantic model, or None.
        field_name: Field name to read.
        default: Fallback value when the field is missing.

    Returns:
        Field value or the provided default.
    """
    if model is None:
        return default

    if isinstance(model, dict):
        return model.get(field_name, default)

    return getattr(model, field_name, default)


def build_short_hash(value: str, length: int = DEFAULT_DISPLAY_HASH_LENGTH) -> str:
    """
    Build a compact uppercase identifier from a stable input value.

    Args:
        value: Raw input value used to create the identifier.
        length: Number of characters to keep from the digest.

    Returns:
        Uppercase alphanumeric hash prefix.
    """
    safe_value  = value or "unknown"
    safe_length = max(6, min(length, 16))
    digest      = hashlib.sha256(safe_value.encode("utf-8")).hexdigest().upper()

    return digest[:safe_length]


def build_alert_ref(alert_key: str, dt: Any = None) -> str:
    """
    Build a short operator-facing alert reference from the system alert key.

    Args:
        alert_key: Stable system alert key.
        dt: Optional business date used to make the reference easier to scan.

    Returns:
        Human-friendly alert reference such as DQ-20260610-A1B2C3.
    """
    alert_ref = build_date_aware_alert_ref(alert_key=alert_key, dt=dt)

    logger.info("Built alert display reference | alert_ref=%s", alert_ref)

    return alert_ref


def build_report_id(agent_run_id: UUID | str, alert_key: str) -> str:
    """
    Build a short operator-facing report identifier for one triage run.

    Args:
        agent_run_id: Agent run UUID.
        alert_key: Stable system alert key.

    Returns:
        Human-friendly report id such as RPT-A1B2C3D4.
    """
    raw_value  = f"{agent_run_id}|{alert_key}"
    report_id = f"RPT-{build_short_hash(raw_value)}"

    logger.info("Built report display id | report_id=%s", report_id)

    return report_id


# --- Defining Human Text Helpers
def humanize_table_name(table_name: Any) -> str:
    """
    Convert a technical table name into a short human-readable label.

    Args:
        table_name: Technical table name such as dq.fct_orders_daily.

    Returns:
        Human-readable table label.
    """
    normalized = str(table_name or "").strip()

    if normalized in TABLE_DISPLAY_NAMES:
        return TABLE_DISPLAY_NAMES[normalized]

    return normalized.replace("dq.", "").replace("_", " ") or "the affected table"


def humanize_metric_name(metric: Any) -> str:
    """
    Convert a technical DQ metric name into plain-language text.

    Args:
        metric: Technical metric name.

    Returns:
        Human-readable metric explanation.
    """
    normalized = str(metric or "").lower()

    if "schema" in normalized and "drift" in normalized:
        return "a schema contract change"

    if "row_count" in normalized:
        return "missing or unusually low row count"

    if "freshness" in normalized:
        return "data freshness problem"

    if "segment" in normalized or "coverage" in normalized:
        return "missing country or channel segment"

    if "duplicate" in normalized:
        return "duplicate records spike"

    if "null" in normalized:
        return "unexpected null value spike"

    if "late_arriving" in normalized:
        return "late arriving data spike"

    return normalized.replace("__", " ").replace("_", " ") or "data quality issue"


def build_alert_title(alert: Any) -> str:
    """
    Build a human-readable issue title for an alert.

    Args:
        alert: Alert dictionary or Pydantic model.

    Returns:
        Plain-language alert title.
    """
    table_name   = humanize_table_name(get_display_value(alert, "table_name", ""))
    metric_text  = humanize_metric_name(get_display_value(alert, "metric", ""))
    dt_value     = get_display_value(alert, "dt", "")
    date_suffix  = f" on {dt_value}" if dt_value else ""

    return f"{table_name.title()} has {metric_text}{date_suffix}"


def build_alert_one_liner(alert: Any) -> str:
    """
    Build a one-sentence explanation for an alert.

    Args:
        alert: Alert dictionary or Pydantic model.

    Returns:
        Plain-language explanation suitable for Discord, UI, and reports.
    """
    table_name     = humanize_table_name(get_display_value(alert, "table_name", ""))
    metric_text    = humanize_metric_name(get_display_value(alert, "metric", ""))
    dt_value       = get_display_value(alert, "dt", "the selected date")
    observed_value = get_display_value(alert, "observed_value", "N/A")
    expected_value = get_display_value(alert, "expected_value", "N/A")

    return (
        f"The platform detected {metric_text} in {table_name} for {dt_value}. "
        f"Observed value is {observed_value}, while expected value is {expected_value}."
    )

