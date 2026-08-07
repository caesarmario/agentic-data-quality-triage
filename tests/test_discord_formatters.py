####
## Discord Formatter Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

from datetime import date
from pathlib import Path
from uuid import UUID

from apps.discord_bot.formatters import (
    DISCORD_SEPARATOR,
    format_alert_list,
    format_alert_summary,
    format_approval_recorded,
    format_backfill_preview,
    format_daily_summary,
    format_operator_answer,
    format_triage_result,
    split_message,
    trim_message,
)
from agent.state import Alert, EvidenceItem, EvidenceType, Hypothesis, TriageReport


# --- Defining Fixtures
def sample_alert() -> dict[str, object]:
    """
    Build one sample alert row for formatter tests.

    Returns:
        Alert row dictionary that matches dq.alerts output shape.
    """
    return {
        "alert_key": "orders|dq_failure|2026-05-04|dq.raw_orders|row_count_positive|table",
        "status": "open",
        "severity": "critical",
        "table_name": "dq.raw_orders",
        "metric": "row_count_positive",
        "dt": "2026-05-04",
        "observed_value": 0,
        "expected_value": 1,
        "threshold_value": 1,
        "details_json": "{}",
    }


# --- Defining Tests
def test_format_alert_summary_contains_core_fields_and_separator() -> None:
    """
    Validate that alert summaries include core operational fields.

    Returns:
        None.
    """
    message = format_alert_summary(sample_alert())

    assert "Critical Data Quality Alert" in message
    assert "Quick Read" in message
    assert "Key Facts" in message
    assert "Impact" in message
    assert "Recommended Next Step" in message
    assert "Technical Reference" in message
    assert "Alert Ref" in message
    assert "DQ-20260504-" in message
    assert "row_count_positive" in message
    assert "dq.raw_orders" in message
    assert DISCORD_SEPARATOR in message


def test_format_alert_list_handles_empty_results() -> None:
    """
    Validate that empty alert lists render as a useful message.

    Returns:
        None.
    """
    message = format_alert_list(alerts=[], status="open", dt="2026-05-04")

    assert "No alerts matched" in message
    assert "Health Summary" in message
    assert "Start Here" in message
    assert DISCORD_SEPARATOR in message


def test_format_alert_list_includes_copilot_readout() -> None:
    """
    Validate that alert lists can include a natural copilot readout.

    Returns:
        None.
    """
    message = format_alert_list(
        alerts=[sample_alert()],
        status="open",
        dt="2026-05-04",
        assistant_note="I would triage the raw orders alert first.",
        data_transport="api",
    )

    assert "Copilot Readout" in message
    assert "triage the raw orders alert" in message
    assert "Alert Data Transport api" in message
    assert message.index("Alert Data Transport api") > message.index("Start Here")
    assert DISCORD_SEPARATOR in message


def test_format_alert_list_groups_alerts_by_priority_and_uses_alert_ref() -> None:
    """
    Validate that alert lists group readable issue titles by severity without exposing system keys.

    Returns:
        None.
    """
    warning_alert             = sample_alert()
    warning_alert["alert_key"] = "orders|dq_failure|2026-05-04|dq.fct_orders_daily|segment_coverage|country_channel"
    warning_alert["severity"]  = "warning"
    warning_alert["table_name"] = "dq.fct_orders_daily"
    warning_alert["metric"]     = "segment_coverage"

    message = format_alert_list(alerts=[warning_alert, sample_alert()], status="open", dt="2026-05-04")

    assert "Raw Orders Data has missing or unusually low row count" in message
    assert "Critical Alerts" in message
    assert "Warning Alerts" in message
    assert "Alert Ref `DQ-20260504-" in message
    assert "Start Here" in message
    assert "System Alert Key" not in message
    assert DISCORD_SEPARATOR in message


def test_format_triage_result_uses_report_id_and_plain_issue_title() -> None:
    """
    Validate that triage output is searchable without using the long system alert key as the title.

    Returns:
        None.
    """
    alert = Alert(
        alert_key="orders|dq_failure|2026-05-04|dq.raw_orders|row_count_positive|table",
        severity="critical",
        table_name="dq.raw_orders",
        metric="row_count_positive",
        dt=date(2026, 5, 4),
        observed_value=0,
        expected_value=1,
    )
    hypothesis = Hypothesis(
        title="Missing or empty ClickHouse partition",
        description="The current date partition has no rows.",
        likelihood=0.88,
        confidence=0.88,
        root_cause_category="missing_partition",
        recommended_action="Backfill the affected date.",
    )
    report = TriageReport(
        agent_run_id=UUID("00000000-0000-0000-0000-000000000123"),
        alert=alert,
        summary="The raw orders partition is empty.",
        impact="Downstream metrics may be incomplete.",
        hypotheses=[hypothesis],
        top_hypothesis=hypothesis,
        confidence=0.88,
        report_id="RPT-ABC123EF",
    )

    message = format_triage_result(
        report,
        execution_transport="api",
        narrative_transport="api",
    )

    assert "RPT-ABC123EF" in message
    assert "Raw Orders Data has missing or unusually low row count" in message
    assert "Quick Read" in message
    assert "What Likely Happened" in message
    assert "Why I Think So" in message
    assert "Approval Status" in message
    assert "Report Links" in message
    assert "System Alert Key" in message
    assert "Triage Transport api" in message
    assert "Narrative Transport api" in message
    assert DISCORD_SEPARATOR in message


def test_format_triage_result_explains_llm_fallback_runtime() -> None:
    """
    Validate Discord triage output explains provider fallback without raw error syntax.

    Returns:
        None.
    """
    alert = Alert(
        alert_key="orders|dq_failure|2026-06-10|dq.stg_orders|freshness__max_dt_lag|max_dt_lag",
        severity="critical",
        table_name="dq.stg_orders",
        metric="freshness__max_dt_lag",
        dt=date(2026, 6, 10),
    )
    hypothesis = Hypothesis(
        title="Missing latest-day data",
        description="The staging table is behind its expected business date.",
        likelihood=0.85,
        confidence=0.85,
        root_cause_category="freshness_delay",
        recommended_action="Review ingestion before requesting a backfill.",
    )
    llm_evidence = EvidenceItem(
        evidence_type=EvidenceType.NOTE,
        tool_name="llm_router",
        description="LLM route metadata.",
        rows=[
            {
                "requested_route": "triage_reasoning",
                "executed_route": "evidence_summary",
                "attempted_routes": ["triage_reasoning", "evidence_summary"],
                "provider": "heuristic",
                "model": "heuristic-v1",
                "used_heuristic": True,
                "fallback_reason": "provider_error:openai:RateLimitError",
                "input_tokens": 720,
                "output_tokens": 407,
                "estimated_cost_usd": 0.0,
                "duration_ms": 4583,
            }
        ],
        row_count=1,
        summary="The configured external provider fell back safely.",
    )
    report = TriageReport(
        agent_run_id=UUID("00000000-0000-0000-0000-000000000456"),
        alert=alert,
        summary="The staging table is stale.",
        impact="Downstream marts may be stale.",
        hypotheses=[hypothesis],
        top_hypothesis=hypothesis,
        evidence=[llm_evidence],
        confidence=0.85,
        report_id="RPT-RUNTIME1",
    )

    message = format_triage_result(report)

    assert "AI Runtime" in message
    assert "Heuristic fallback" in message
    assert "heuristic / heuristic-v1" in message
    assert "triage_reasoning -> evidence_summary" in message
    assert "720 input / 407 output tokens" in message
    assert "quota or credit limit" in message
    assert "provider_error:openai:RateLimitError" not in message


def test_system_alert_key_appears_only_after_technical_reference() -> None:
    """
    Validate that the long system key is hidden until the technical reference section.

    Returns:
        None.
    """
    alert      = sample_alert()
    message    = format_alert_summary(alert)
    key_index  = message.index(str(alert["alert_key"]))
    tech_index = message.index("### Technical Reference")

    assert key_index > tech_index


def test_format_daily_summary_includes_copilot_readout() -> None:
    """
    Validate that daily summaries can include a natural copilot readout.

    Returns:
        None.
    """
    message = format_daily_summary(
        dt="2026-05-04",
        check_rows=[{"status": "fail", "count": 1}],
        alert_rows=[{"severity": "critical", "count": 1}],
        assistant_note="This day needs investigation before downstream metrics are trusted.",
    )

    assert "Day Status" in message
    assert "Needs Attention" in message
    assert "Check Results" in message
    assert "Alert Risk" in message
    assert "Copilot Analysis" in message
    assert "needs investigation" in message
    assert DISCORD_SEPARATOR in message


def test_format_operator_answer_includes_guardrail() -> None:
    """
    Validate that free-form copilot answers include an execution guardrail.

    Returns:
        None.
    """
    message = format_operator_answer(
        question="What should I do next?",
        answer="Run triage before approving a backfill.",
        alert_key="orders|dq_failure|2026-05-04|dq.raw_orders|row_count_positive|table",
        transport="api",
        agent_run_id="44444444-4444-4444-4444-444444444444",
    )

    assert "DQ Copilot" in message
    assert "Direct Answer" in message
    assert "Suggested Next Command" in message
    assert "Run triage" in message
    assert "will not execute remediation without approval" in message
    assert message.index("System Alert Key") > message.index("Technical Reference")
    assert "Copilot Transport api" in message
    assert "Agent Run ID 44444444-4444-4444-4444-444444444444" in message
    assert DISCORD_SEPARATOR in message


def test_format_backfill_preview_is_durable_and_non_mutating_by_text() -> None:
    """
    Validate that backfill previews state that no action is executed.

    Returns:
        None.
    """
    message = format_backfill_preview(
        request_id="APR-20260504-A1B2C3D4",
        start_date="2026-05-04",
        end_date="2026-05-04",
        target_dag_id="00_dag_dq_platform_daily_orchestrator",
        reason="Missing partition",
        status="pending",
        created_new=True,
    )

    assert "Backfill Approval Request" in message
    assert "Durable Approval Queue" in message
    assert "Safety Check" in message
    assert "No Airflow DAG was triggered" in message
    assert "APR-20260504-A1B2C3D4" in message
    assert "/approve" in message
    assert "/reject" in message
    assert DISCORD_SEPARATOR in message


def test_format_approval_recorded_is_durable_and_non_executing() -> None:
    """
    Validate that approval output records audit intent without implying execution.

    Returns:
        None.
    """
    message = format_approval_recorded(
        request_id="APR-20260504-A1B2C3D4",
        approved_by="mario",
        status="approved",
        decision="approve",
    )

    assert "Request Approved" in message
    assert "Durable Approval Decision" in message
    assert "approval queue and agent audit log" in message
    assert "No Airflow DAG was triggered" in message
    assert "No backfill, rerun, or data mutation was executed" in message
    assert "90_dag_dq_platform_backfill_dispatcher" in message
    assert DISCORD_SEPARATOR in message


def test_format_rejection_does_not_offer_execution() -> None:
    """
    Validate rejected requests cannot be mistaken for executable approvals.

    Returns:
        None.
    """
    message = format_approval_recorded(
        request_id="APR-20260504-A1B2C3D4",
        approved_by="reviewer",
        status="rejected",
        decision="reject",
    )

    assert "Request Rejected" in message
    assert "cannot authorize dispatcher execution" in message
    assert DISCORD_SEPARATOR in message


def test_trim_message_preserves_separator() -> None:
    """
    Validate that long messages are trimmed with a separator.

    Returns:
        None.
    """
    message = trim_message("x" * 2500, limit=300)

    assert len(message) <= 300
    assert DISCORD_SEPARATOR in message


def test_split_message_preserves_full_long_output() -> None:
    """
    Validate Discord chunking keeps the complete report without exceeding limits.

    Returns:
        None.
    """
    original = "\n\n".join(f"Section {index}: " + ("x" * 600) for index in range(8))
    chunks   = split_message(original, limit=900)

    assert len(chunks) > 1
    assert all(len(chunk) <= 900 for chunk in chunks)
    assert " ".join(" ".join(chunks).split()) == " ".join(original.split())


def test_documented_templates_match_discord_runtime_and_command_syntax() -> None:
    """
    Ensure portfolio examples do not regress to mojibake or stale transport output.

    Returns:
        None.
    """
    document = Path("docs/discord_output_templates.md").read_text(encoding="utf-8")

    assert "\u00f0\u0178" not in document
    assert "\u00e2\u0161" not in document
    assert "\U0001F6A8" in document
    assert "\U0001F916" in document
    assert "Triage Transport api" in document
    assert "Narrative Transport api" in document
    assert "APR-20260504-A1B2C3D4" in document
    assert "mocked approval" not in document.lower()
    assert "BACKFILL APPROVAL REQUEST" in document
    assert "/triage alert_key:DQ-20260504-A1B2C3" in document
    assert "/approve request_id:APR-20260504-A1B2C3D4" in document
    assert "slash command" in document.lower()
    assert "DISCORD_GUILD_ID" in document

