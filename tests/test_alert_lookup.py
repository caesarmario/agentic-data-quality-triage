####
## Alert Lookup Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

from agent.tools.alerts import build_alert_lookup_sql


# --- Defining Tests
def test_alert_lookup_selects_display_id() -> None:
    """
    Validate that alert lookup exposes the human-facing alert display id.

    Returns:
        None.
    """
    sql = build_alert_lookup_sql(status="open", dt="2026-06-10", limit=5)

    assert "alert_display_id" in sql
    assert "LIMIT 5" in sql


def test_alert_lookup_filters_logical_latest_state_before_limiting() -> None:
    """Lifecycle filters and ordering must operate on merged alert versions."""
    sql = build_alert_lookup_sql(status="open", dt="2026-06-10", limit=5)

    assert "FROM dq.alerts FINAL" in sql
    assert sql.index("FROM dq.alerts FINAL") < sql.index("WHERE status = 'open'")
    assert "AND dt = toDate('2026-06-10')" in sql
    assert sql.index("WHERE status = 'open'") < sql.index("ORDER BY created_at DESC, alert_key ASC")
    assert sql.index("ORDER BY created_at DESC, alert_key ASC") < sql.index("LIMIT 5")


def test_alert_lookup_accepts_human_alert_ref() -> None:
    """
    Validate that DQ-style alert refs are resolved through alert_display_id.

    Returns:
        None.
    """
    sql = build_alert_lookup_sql(alert_key="DQ-20260610-A1B2C3", limit=1)

    assert "alert_display_id = 'DQ-20260610-A1B2C3'" in sql
    assert "OR alert_key = 'DQ-20260610-A1B2C3'" in sql
