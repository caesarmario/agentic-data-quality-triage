####
## Discord Formatter Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

from datetime import date
from pathlib import Path
from uuid import UUID

import pytest

from apps.discord_bot.formatters import (
    DISCORD_SEPARATOR,
    format_alert_list,
    format_alert_summary,
    format_approval_recorded,
    format_backfill_preview,
    format_daily_summary,
    format_operator_answer,
    format_quality_summary,
    format_triage_result,
    build_discord_incident_payload,
    render_approval_embed,
    render_incident_embed,
    render_operator_answer_embed,
    render_quality_summary_embed,
    split_message,
    trim_message,
)
from agent.state import AiInvestigationNarrative, Alert, EvidenceItem, EvidenceType, Hypothesis, TriageReport


# --- Defining Fixtures
@pytest.fixture(autouse=True)
def isolated_workspace_config(monkeypatch):
    """Render fixtures must not inherit a runner's live workspace configuration."""
    monkeypatch.delenv("DISCORD_OPERATOR_UI_URL", raising=False)


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


# --- Testing Compact Embed Contracts
def test_incident_embed_bounds_content_and_disables_mentions() -> None:
    """Ensure compact incident cards stay within Discord embed constraints."""
    alert = sample_alert()
    alert["details_json"] = '{"impact":"' + ("impact " * 1200) + '"}'
    incident = build_discord_incident_payload(
        alert,
        narrative="@everyone " + ("evidence " * 1200),
        llm_runtime={"runtime_mode": "external_model"},
    )
    result = render_incident_embed(incident)
    embed  = result.payload["embeds"][0]
    total  = (
        len(embed["title"])
        + len(embed["description"])
        + len(embed["footer"]["text"])
        + sum(len(item["name"]) + len(item["value"]) for item in embed["fields"])
    )

    assert len(embed["title"]) <= 256
    assert len(embed["description"]) <= 4096
    assert all(len(item["value"]) <= 1024 for item in embed["fields"])
    assert total <= 6000
    assert "@everyone" not in embed["description"]
    assert result.payload["allowed_mentions"] == {"parse": []}
    assert result.narrative_source == "external_copilot"


def test_incident_embed_uses_deterministic_fallback_without_narrative() -> None:
    """Ensure provider absence still produces a useful operator card."""
    result = render_incident_embed(build_discord_incident_payload(sample_alert()))
    embed = result.payload["embeds"][0]
    field_names = {item["name"] for item in embed["fields"]}

    assert result.narrative_source == "deterministic_fallback"
    assert result.lifecycle == "OPEN"
    assert "Raw Orders Data" in embed["title"]
    assert "What changed" in field_names
    assert "Why it matters" in field_names
    assert "Next best action" in field_names
    assert not any("Command" in name for name in field_names)
    assert "System summary · External AI unavailable" == embed["footer"]["text"]


def test_incident_embed_has_skimmable_fields_and_interactive_components() -> None:
    """Incident cards expose compact facts and safe actions rather than command text."""
    incident = build_discord_incident_payload(
        sample_alert(),
        narrative="The partition appears empty. Confirm the upstream landing before considering a backfill.",
        llm_runtime={
            "runtime_mode": "external_model",
            "provider": "gemini",
            "model": "gemini-3.5-flash-lite",
        },
    )
    result = render_incident_embed(incident, operator_ui_url="https://example.test/incidents")
    embed = result.payload["embeds"][0]
    field_names = {item["name"] for item in embed["fields"]}
    components = result.payload["components"]
    buttons = [component for row in components for component in row["components"]]

    assert result.narrative_source == "external_copilot"
    assert "AI analysis · Gemini" in embed["footer"]["text"]
    assert "Grounded in platform evidence" in embed["footer"]["text"]
    assert "Command" not in field_names
    assert {"What changed", "Why it matters", "Next best action"} <= field_names
    assert [button["label"] for button in buttons] == [
        "Investigate",
        "Evidence",
        "Ask AI",
        "Open Workspace",
        "Resolve",
    ]
    assert [len(row["components"]) for row in components] == [3, 2]
    assert [button["style"] for button in buttons] == [1, 2, 2, 5, 2]
    assert [button["emoji"]["name"] for button in buttons[:3]] == ["\U0001F50D", "\U0001F4CE", "\U0001F4AC"]
    assert buttons[-2]["url"] == "https://example.test/incidents"
    assert all(len(button.get("custom_id", "")) <= 100 for button in buttons)


def test_incident_embed_never_labels_heuristic_or_system_fallback_as_gemini() -> None:
    """Only a confirmed external model route may be attributed to Gemini."""
    heuristic = render_incident_embed(
        build_discord_incident_payload(
            sample_alert(),
            narrative="The row count is unexpectedly zero; verify the source partition.",
            llm_runtime={
                "provider": "heuristic",
                "model": "heuristic-v1",
                "used_heuristic": True,
            },
        )
    )
    deterministic = render_incident_embed(build_discord_incident_payload(sample_alert()))
    heuristic_footer = heuristic.payload["embeds"][0]["footer"]["text"]
    deterministic_footer = deterministic.payload["embeds"][0]["footer"]["text"]

    assert heuristic.narrative_source == "heuristic_copilot"
    assert heuristic_footer == "Heuristic analysis · No external model call"
    assert "Gemini" not in heuristic_footer
    assert deterministic.narrative_source == "deterministic_fallback"
    assert deterministic_footer == "System summary · External AI unavailable"
    assert "Gemini" not in deterministic_footer


def test_resolved_incident_keeps_only_read_only_buttons() -> None:
    """Resolved incidents cannot offer investigation or draft-action controls."""
    alert = sample_alert()
    alert["status"] = "resolved"
    result = render_incident_embed(build_discord_incident_payload(alert))
    buttons = [
        component
        for row in result.payload["components"]
        for component in row["components"]
    ]

    assert [button["label"] for button in buttons] == ["Evidence"]


def test_resolved_incident_overrides_stale_approval_state() -> None:
    """A closed incident cannot be reopened visually by a stale approval registry row."""
    alert = sample_alert()
    alert["status"] = "resolved"

    incident = build_discord_incident_payload(alert, approval_state="approved")

    assert incident.facts.lifecycle == "RESOLVED"
    assert incident.facts.approval_state == "approved"


def test_component_custom_id_is_rejected_instead_of_silently_truncated() -> None:
    """Truncation must not create an unhandled or ambiguously routed button."""
    alert = sample_alert()
    alert["alert_display_id"] = "DQ-" + ("A" * 120)

    with pytest.raises(ValueError, match="custom_id exceeds 100"):
        render_incident_embed(build_discord_incident_payload(alert))


def test_incident_contract_maps_alert_lifecycle_states() -> None:
    """Ensure alert lifecycle facts use the fixed operator vocabulary."""
    alert = sample_alert()

    for source_status, expected in (
        ("open", "OPEN"),
        ("acknowledged", "OPEN"),
        ("triaged", "TRIAGED"),
        ("resolved", "RESOLVED"),
    ):
        alert["status"] = source_status
        assert build_discord_incident_payload(alert).facts.lifecycle == expected


def test_operator_answer_embed_is_direct_and_keeps_technical_details_out() -> None:
    """Ensure /dq ask returns the answer instead of the old repeated envelope."""
    result = render_operator_answer_embed(
        answer="The raw partition is empty. Check landing availability first.",
        alert_key="orders|dq_failure|2026-05-04|dq.raw_orders|row_count_positive|table",
        transport="api",
        agent_run_id="run-secret-detail",
        llm_runtime={
            "provider": "gemini",
            "model": "gemini-flash",
            "runtime_mode": "external_model",
        },
    )
    serialized = str(result.payload)

    assert result.narrative_source == "external_copilot"
    assert "The raw partition is empty" in serialized
    assert "Question" not in serialized
    assert "Guardrail" not in serialized
    assert "run-secret-detail" not in serialized


def test_approval_embed_maps_all_lifecycle_states() -> None:
    """Ensure approval cards use the controlled lifecycle vocabulary."""
    assert render_approval_embed(
        request_id="APR-1", status="pending", summary="Pending review", next_step="Review"
    ).lifecycle == "WAITING APPROVAL"
    assert render_approval_embed(
        request_id="APR-1", status="approved", summary="Approved", next_step="Dispatch"
    ).lifecycle == "APPROVED"
    assert render_approval_embed(
        request_id="APR-1", status="rejected", summary="Rejected", next_step="Stop"
    ).lifecycle == "REJECTED"


def test_approval_embed_defaults_to_high_risk_and_truthful_recovery_boundary() -> None:
    """Approval cards must not imply automatic rollback or immediate execution."""
    result = render_approval_embed(
        request_id="APR-20261007-ABC123",
        status="pending",
        summary="Review the bounded recovery proposal.",
        next_step="Approve or reject after reviewing the evidence.",
        target_dag_id="00_dag_dq_platform_daily_orchestrator",
        start_date="2026-10-07",
        end_date="2026-10-07",
    )
    fields = {field["name"]: field["value"] for field in result.payload["embeds"][0]["fields"]}
    labels = {action.label for row in result.action_rows for action in row.actions}

    assert fields["Risk"] == "HIGH"
    assert "Recovery boundary" in fields
    assert "No automatic rollback is implemented" in fields["Recovery boundary"]
    assert "separate operator decision" in fields["Recovery boundary"]
    assert "rollback plan available" not in fields["Recovery boundary"].lower()
    assert labels == {"View Plan", "Approve", "Reject"}
    assert not labels.intersection({"Execute", "Run", "Dispatch", "Remediate"})


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
        ai_investigation=AiInvestigationNarrative(
            what_happened="The raw partition is empty even though the daily workflow reached validation.",
            evidence_reasoning=["[E1] The guarded row-count check returned zero rows."],
            likely_impact="Daily marts and dashboard totals may be incomplete.",
            uncertainty="The evidence does not yet prove whether the landing object was absent or unreadable.",
            recommended_next_step="Confirm the landing object before proposing a backfill.",
            approval_rationale="A backfill can replace downstream partitions and requires human review.",
            source="external_model",
            provider="gemini",
            model="gemini-3.5-flash-lite",
            route="triage_reasoning",
        ),
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
    assert "The guarded row-count check returned zero rows" in message
    assert "Likely Impact" in message
    assert "What Is Still Uncertain" in message
    assert "Confirm the landing object" in message
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
        data_transport="api",
    )

    assert "Day Status" in message
    assert "Needs Attention" in message
    assert "Check Results" in message
    assert "Alert Risk" in message
    assert "Copilot Analysis" in message
    assert "needs investigation" in message
    assert "Data Transport `api`" in message
    assert DISCORD_SEPARATOR in message


def test_format_quality_summary_is_readable_and_keeps_rerun_advisory() -> None:
    """Validate that extended summaries clearly separate evidence from action."""
    message = format_quality_summary(
        payload={
            "scope": "table",
            "start_date": "2026-06-04",
            "end_date": "2026-06-10",
            "table_name": "dq.fct_orders_daily",
            "check_counts": [{"status": "fail", "count": 2}],
            "alert_counts": [{"severity": "critical", "count": 1}],
            "registered_asset_count": 1,
            "table_counts": [
                {
                    "table_name": "dq.fct_orders_daily",
                    "failed_checks": 2,
                    "critical_alerts": 1,
                }
            ],
            "rerun_suggestions": [
                {
                    "target_dag_id": "20_dag_dq_orders_dbt_transform",
                    "affected_tables": ["dq.fct_orders_daily"],
                    "mode": "advisory_only",
                    "requires_approval": True,
                }
            ],
        },
        assistant_note="The mart needs investigation before reporting can be trusted.",
        data_transport="api",
    )

    assert "DQ Table Summary" in message
    assert "Quick Read" in message
    assert "Copilot Analysis" in message
    assert "Most Affected Tables" in message
    assert "Manual Rerun Guidance" in message
    assert "advisory candidates only" in message
    assert "Nothing has been executed" in message
    assert "explicit approval is still required" in message
    assert "20_dag_dq_orders_dbt_transform" in message
    assert "Data Transport `api`" in message
    assert DISCORD_SEPARATOR in message


def test_extended_quality_summary_embed_is_compact_and_has_truthful_provenance() -> None:
    """Extended summary presentation uses an embed and names Gemini only on provider evidence."""
    result = render_quality_summary_embed(
        payload={
            "scope": "table",
            "start_date": "2026-06-04",
            "end_date": "2026-06-10",
            "table_name": "dq.fct_orders_daily",
            "check_counts": [{"status": "fail", "count": 2}],
            "alert_counts": [{"severity": "critical", "count": 1}],
            "table_counts": [
                {"table_name": "dq.fct_orders_daily", "failed_checks": 2, "critical_alerts": 1}
            ],
        },
        assistant_note="The table has failed checks that need review.",
        llm_runtime={
            "runtime_mode": "external_model",
            "provider": "gemini",
            "model": "gemini-3.5-flash-lite",
        },
    )
    embed = result.payload["embeds"][0]
    serialized = str(result.payload)

    assert len(result.payload["embeds"]) == 1
    assert "dq.fct_orders_daily" in serialized
    assert "2 failed" in serialized
    assert result.narrative_source == "external_copilot"
    assert "AI analysis · Gemini" in embed["footer"]["text"]
    assert "Command" not in {item["name"] for item in embed["fields"]}


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
        incident_history_count=2,
    )

    assert "DQ Copilot" in message
    assert "Direct Answer" in message
    assert "Previous Investigation Records `2`" in message
    assert "comparison context only" in message
    assert "Suggested Next Command" in message
    assert "Run triage" in message
    assert "will not execute remediation without approval" in message
    assert message.index("System Alert Key") > message.index("Technical Reference")
    assert "Copilot Transport api" in message
    assert "Agent Run ID 44444444-4444-4444-4444-444444444444" in message
    assert DISCORD_SEPARATOR in message


def test_format_operator_answer_direct_mode_preserves_model_answer_and_provenance() -> None:
    """Validate direct mode keeps the model answer intact and proves its runtime."""
    answer = "The partition is empty. Review upstream landing before requesting a backfill."
    message = format_operator_answer(
        question="What happened?",
        answer=answer,
        alert_key="orders|dq_failure|2026-05-04|dq.raw_orders|row_count_positive|table",
        transport="api",
        agent_run_id="55555555-5555-4555-8555-555555555555",
        response_mode="direct",
        llm_runtime={
            "provider": "gemini",
            "model": "gemini-3.5-flash-lite",
            "runtime_mode": "external_model",
            "executed_route": "cheap_summary",
            "input_tokens": 120,
            "output_tokens": 24,
            "estimated_cost_display": "$0.000096",
        },
    )

    assert "Gemini Answer" in message
    assert answer in message
    assert "gemini / gemini-3.5-flash-lite" in message
    assert "120 input / 24 output tokens" in message
    assert "did not execute SQL mutation, backfill, or remediation" in message
    assert "### Question" not in message
    assert "### Suggested Next Command" not in message
    assert DISCORD_SEPARATOR in message


def test_incident_payload_uses_explicit_approval_state_without_changing_alert_facts() -> None:
    """Approval lifecycle may change the card state but not severity or identity."""
    alert = {
        "alert_key": "orders|dq_failure|2026-05-04|dq.raw_orders|row_count_positive|table",
        "alert_display_id": "DQ-20260504-A1B2C3",
        "severity": "critical",
        "status": "triaged",
        "table_name": "dq.raw_orders",
        "metric": "row_count_positive",
        "dt": "2026-05-04",
        "details_json": "{}",
    }

    payload = build_discord_incident_payload(alert, approval_state="approved")

    assert payload.facts.lifecycle == "APPROVED"
    assert payload.facts.approval_state == "approved"
    assert payload.facts.severity == "critical"
    assert payload.facts.alert_ref == "DQ-20260504-A1B2C3"


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
    assert "/dq approve" in message
    assert "/dq reject" in message
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
    assert "compact Discord embed" in document
    assert "external_copilot" in document
    assert "heuristic_copilot" in document
    assert "deterministic_fallback" in document
    assert "Single-message lifecycle" in document
    assert "dq.discord_incident_messages" in document
    assert "APR-20260504-A1B2C3D4" in document
    assert "mocked approval" not in document.lower()
    import re
    assert all(re.search(r"\[[^\]\n]*" + re.escape(label) + r"\]", document)
               for label in ("Investigate", "Evidence", "Ask AI", "Open Workspace", "Resolve"))
    assert "[View Plan] [Approve] [Reject]" in document
    assert "The transitional `/dq approve` and `/dq reject` aliases open these controls" in document
    assert "slash command" in document.lower()
    assert "/dq debug" in document

