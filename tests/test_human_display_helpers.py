####
## Human Display Helper Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

from datetime import date
from uuid import UUID

from agent.display import build_alert_ref, build_alert_title, build_report_id
from agent.graph import render_markdown_report
from agent.state import Alert, Hypothesis, TriageReport


# --- Defining Fixtures
def sample_report() -> TriageReport:
    """
    Build one compact triage report for display helper tests.

    Returns:
        TriageReport with deterministic ids and readable alert context.
    """
    alert = Alert(
        alert_key="orders|dq_failure|2026-06-10|dq.fct_orders_daily|segment_coverage__country_channel|country_channel",
        severity="critical",
        table_name="dq.fct_orders_daily",
        metric="segment_coverage__country_channel",
        dt=date(2026, 6, 10),
        observed_value=10,
        expected_value=12,
    )
    hypothesis = Hypothesis(
        title="Missing segment coverage",
        description="One or more expected country/channel segments are missing.",
        likelihood=0.77,
        confidence=0.77,
        root_cause_category="missing_segment",
        recommended_action="Regenerate and reload the affected date partition.",
    )
    agent_run_id = UUID("00000000-0000-0000-0000-000000000456")

    return TriageReport(
        agent_run_id=agent_run_id,
        alert=alert,
        summary="The daily orders mart is missing at least one expected segment.",
        impact="Segment-level dashboards may be incomplete.",
        hypotheses=[hypothesis],
        top_hypothesis=hypothesis,
        confidence=0.77,
        report_id=build_report_id(agent_run_id, alert.alert_key),
    )


# --- Defining Tests
def test_display_ids_are_short_and_prefixed() -> None:
    """
    Validate that display ids are short enough for operators to search and discuss.

    Returns:
        None.
    """
    alert_key = "orders|dq_failure|2026-06-10|dq.fct_orders_daily|segment_coverage__country_channel|country_channel"

    assert build_alert_ref(alert_key, date(2026, 6, 10)).startswith("DQ-20260610-")
    assert build_report_id("agent-run-1", alert_key).startswith("RPT-")
    assert len(build_alert_ref(alert_key, date(2026, 6, 10))) == 18
    assert build_alert_ref(alert_key).startswith("DQ-20260610-")


def test_alert_title_is_human_readable() -> None:
    """
    Validate that technical alert fields are converted into readable issue text.

    Returns:
        None.
    """
    report = sample_report()

    assert build_alert_title(report.alert) == "Daily Orders Mart has missing country or channel segment on 2026-06-10"


def test_markdown_report_uses_readable_title_and_keeps_system_key_reference() -> None:
    """
    Validate that report Markdown uses a readable title while preserving technical references.

    Returns:
        None.
    """
    report   = sample_report()
    markdown = render_markdown_report(report)
    first_line = markdown.splitlines()[0]

    assert first_line == "# Daily Orders Mart has missing country or channel segment on 2026-06-10"
    assert f"Report ID: `{report.report_id}`" in markdown
    assert "System Alert Key" in markdown
