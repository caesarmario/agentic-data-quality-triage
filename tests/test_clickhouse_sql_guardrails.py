####
## Guarded SQL Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import pytest

from agent.tools.clickhouse_sql import GuardrailViolation, SqlGuardrailConfig, guard_sql


# --- Defining Functions
def test_guard_sql_allows_date_filtered_alert_query() -> None:
    """
    Validate that a date-filtered alert query passes SQL guardrails.

    Returns:
        None.
    """
    guarded_sql, applied = guard_sql(
        "SELECT alert_key FROM dq.alerts WHERE dt = toDate('2026-05-04') LIMIT 5",
        SqlGuardrailConfig(hard_limit=100),
    )

    assert guarded_sql.endswith("LIMIT 5")
    assert "read_only_checked" in applied
    assert "date_filter_checked" in applied


def test_guard_sql_adds_limit_when_missing() -> None:
    """
    Validate that result queries receive a hard LIMIT when omitted.

    Returns:
        None.
    """
    guarded_sql, applied = guard_sql(
        "SELECT alert_key FROM dq.alerts WHERE dt = toDate('2026-05-04')",
        SqlGuardrailConfig(hard_limit=25),
    )

    assert guarded_sql.endswith("LIMIT 25")
    assert "limit_added_25" in applied


def test_guard_sql_caps_large_limit() -> None:
    """
    Validate that excessive LIMIT values are capped by the hard limit.

    Returns:
        None.
    """
    guarded_sql, applied = guard_sql(
        "SELECT alert_key FROM dq.alerts WHERE dt = toDate('2026-05-04') LIMIT 999",
        SqlGuardrailConfig(hard_limit=50),
    )

    assert guarded_sql.endswith("LIMIT 50")
    assert "limit_capped_999_to_50" in applied


def test_guard_sql_rejects_missing_date_filter_for_large_table() -> None:
    """
    Validate that large table queries must include a date-like predicate.

    Returns:
        None.
    """
    with pytest.raises(GuardrailViolation, match="requires a date filter"):
        guard_sql("SELECT alert_key FROM dq.alerts LIMIT 5", SqlGuardrailConfig(hard_limit=100))


def test_guard_sql_rejects_mutating_statement() -> None:
    """
    Validate that mutation statements are rejected before execution.

    Returns:
        None.
    """
    with pytest.raises(GuardrailViolation, match="Only read-only SQL is allowed"):
        guard_sql("DELETE FROM dq.alerts WHERE dt = toDate('2026-05-04')", SqlGuardrailConfig(hard_limit=100))


def test_guard_sql_rejects_multiple_statements() -> None:
    """
    Validate that semicolon-separated multiple statements are rejected.

    Returns:
        None.
    """
    with pytest.raises(GuardrailViolation, match="Only one SQL statement"):
        guard_sql(
            "SELECT alert_key FROM dq.alerts WHERE dt = toDate('2026-05-04'); SELECT 1",
            SqlGuardrailConfig(hard_limit=100),
        )
