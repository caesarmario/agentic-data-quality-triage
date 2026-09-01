####
## Copilot Narrative Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

from datetime import date
from uuid import UUID

from agent.llm import copilot
from agent.state import Alert, EvidenceItem, EvidenceType, Hypothesis, TriageReport


# --- Defining Fixtures
def sample_alert() -> dict[str, object]:
    """
    Build one sample alert row for copilot narrative tests.

    Returns:
        Alert row dictionary.
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
    }


# --- Defining Tests
def test_alert_list_copilot_uses_natural_fallback_when_llm_fails(monkeypatch) -> None:
    """
    Validate that alert list copilot output remains useful when provider execution fails.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    def fail_llm_task(*args, **kwargs):
        """
        Simulate an unavailable LLM provider.

        Args:
            args: Positional arguments.
            kwargs: Keyword arguments.

        Raises:
            RuntimeError: Always raised to force fallback.
        """
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(copilot, "run_llm_task", fail_llm_task)

    text = copilot.build_alert_list_copilot_note(
        alerts=[sample_alert()],
        status="open",
        dt="2026-05-04",
    )

    assert "I found" in text
    assert "dq.raw_orders" in text
    assert "Alert Ref `DQ-20260504-" in text
    assert "Run `/triage alert_key:DQ-20260504-" in text
    assert "system key" not in text.lower()


def test_operator_answer_uses_alert_context_without_llm(monkeypatch) -> None:
    """
    Validate that free-form answers stay grounded in alert context when LLM fails.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    def fail_llm_task(*args, **kwargs):
        """
        Simulate an unavailable LLM provider.

        Args:
            args: Positional arguments.
            kwargs: Keyword arguments.

        Raises:
            RuntimeError: Always raised to force fallback.
        """
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(copilot, "run_llm_task", fail_llm_task)

    text = copilot.build_operator_answer(
        question="What should I do next?",
        alert=sample_alert(),
    )

    assert "dq.raw_orders" in text
    assert "Alert Ref `DQ-20260504-" in text
    assert "observed `0`" in text
    assert "`1` was expected" in text
    assert "run `/triage" in text.lower()
    assert "system key" not in text.lower()


def test_daily_summary_copilot_uses_failure_context_when_llm_fails(monkeypatch) -> None:
    """
    Validate that daily summary fallback calls out incident-worthy failures.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    def fail_llm_task(*args, **kwargs):
        """
        Simulate an unavailable LLM provider.

        Args:
            args: Positional arguments.
            kwargs: Keyword arguments.

        Raises:
            RuntimeError: Always raised to force fallback.
        """
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(copilot, "run_llm_task", fail_llm_task)

    text = copilot.build_daily_summary_copilot_note(
        dt="2026-05-04",
        check_rows=[{"status": "fail", "count": 2}],
        alert_rows=[{"severity": "critical", "count": 1}],
    )

    assert "needs attention" in text
    assert "2" in text
    assert "critical" in text


def test_triage_copilot_fallback_explains_confidence_and_approval(monkeypatch) -> None:
    """
    Validate that no-LLM triage output remains readable, evidence-aware, and approval-bounded.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    def fail_llm_task(*args, **kwargs):
        """
        Simulate an unavailable LLM provider.

        Args:
            args: Positional arguments.
            kwargs: Keyword arguments.

        Raises:
            RuntimeError: Always raised to force fallback.
        """
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(copilot, "run_llm_task", fail_llm_task)

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
        title="Missing ClickHouse partition",
        description="No rows were loaded for the affected date.",
        likelihood=0.88,
        confidence=0.88,
        root_cause_category="missing_partition",
        recommended_action="Prepare an approval-gated backfill.",
    )
    evidence = EvidenceItem(
        evidence_type=EvidenceType.SQL_RESULT,
        tool_name="clickhouse_sql",
        description="Check current partition row count.",
        summary="Current partition row count is zero.",
        row_count=1,
    )
    report = TriageReport(
        agent_run_id=UUID("00000000-0000-0000-0000-000000000123"),
        alert=alert,
        summary="The raw partition is missing.",
        impact="Downstream metrics may be incomplete.",
        hypotheses=[hypothesis],
        top_hypothesis=hypothesis,
        confidence=0.88,
        evidence=[evidence],
    )

    text = copilot.build_triage_copilot_note(report)

    assert "Alert Ref `DQ-20260504-" in text
    assert "missing clickhouse partition" in text.lower()
    assert "confidence `0.88`" in text
    assert "`1` evidence item" in text
    assert "nothing should be treated as automatically fixed" in text

def test_compact_context_rows_enforces_limit_and_field_allowlist() -> None:
    """
    Validate LLM context row limits and field-level data minimization.

    Returns:
        None.
    """
    rows = [
        {
            "tool_name": f"tool_{index}",
            "summary": "x" * 2000 if index == 0 else f"Evidence {index}",
            "secret": "must-not-leak",
        }
        for index in range(8)
    ]

    compact = copilot.compact_context_rows(
        rows=rows,
        allowed_fields=("tool_name", "summary"),
        limit=5,
    )

    assert len(compact) == 5
    assert all("secret" not in row for row in compact)
    assert compact[-1]["tool_name"] == "tool_4"


def test_operator_answer_fallback_summarizes_bounded_evidence(monkeypatch) -> None:
    """
    Validate readable evidence fallback when no external LLM is available.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    def fail_llm_task(*args, **kwargs):
        """
        Simulate an unavailable LLM provider.

        Args:
            args: Positional arguments.
            kwargs: Keyword arguments.

        Raises:
            RuntimeError: Always raised to force fallback.
        """
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(copilot, "run_llm_task", fail_llm_task)

    text = copilot.build_operator_answer(
        question="Please summarize the evidence.",
        alert=sample_alert(),
        evidence_rows=[
            {
                "tool_name": "clickhouse_sql",
                "summary": "The selected partition contains zero rows.",
                "row_count": 1,
            }
        ],
    )

    assert "1" in text
    assert "selected partition contains zero rows" in text.lower()
    assert "not proof" in text.lower()


def test_operator_answer_fallback_marks_backfill_as_preview_only(monkeypatch) -> None:
    """
    Validate that backfill guidance never implies execution in fallback mode.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    def fail_llm_task(*args, **kwargs):
        """
        Simulate an unavailable LLM provider.

        Args:
            args: Positional arguments.
            kwargs: Keyword arguments.

        Raises:
            RuntimeError: Always raised to force fallback.
        """
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(copilot, "run_llm_task", fail_llm_task)

    text = copilot.build_operator_answer(
        question="Draft a backfill approval.",
        alert=sample_alert(),
        report_context={
            "recommended_action": "Backfill the missing business date.",
            "approval_required": True,
        },
    )

    assert "approval preview only" in text.lower()
    assert "no airflow dag" in text.lower()
    assert "has been executed" in text.lower()

def test_operator_answer_fallback_uses_existing_report_before_suggesting_triage(monkeypatch) -> None:
    """
    Validate that an existing report drives the no-LLM answer instead of redundant triage advice.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    monkeypatch.setattr(
        copilot,
        "run_llm_task",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("provider unavailable")
        ),
    )

    text = copilot.build_operator_answer(
        question="Explain what likely happened and recommend the safest next step.",
        alert=sample_alert(),
        report_context={
            "summary": "The expected raw partition was not loaded.",
            "top_hypothesis": "Missing ClickHouse partition",
            "confidence": 0.88,
            "recommended_action": "Prepare an approval-gated backfill preview.",
            "approval_required": True,
        },
        evidence_rows=[
            {
                "tool_name": "clickhouse_sql",
                "summary": "The selected partition contains zero rows.",
            }
        ],
    )

    assert "current triage report says" in text.lower()
    assert "missing clickhouse partition" in text.lower()
    assert "confidence 0.88" in text.lower()
    assert "approval-gated backfill preview" in text.lower()
    assert "run /triage" not in text.lower()
    assert "explicit approval" in text.lower()


def test_operator_answer_fallback_explains_prior_investigations_without_overclaiming(
    monkeypatch,
) -> None:
    """
    Validate no-provider history answers stay exact-match and non-authoritative.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    monkeypatch.setattr(
        copilot,
        "run_llm_task",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("provider unavailable")
        ),
    )

    text = copilot.build_operator_answer(
        question="Has this alert been investigated before?",
        alert=sample_alert(),
        incident_history_rows=[
            {
                "recorded_at": "2026-08-20T13:37:00+00:00",
                "outcome_status": "success",
                "summary": "A previous investigation found a missing segment.",
                "confidence": 0.72,
                "top_hypothesis_category": "missing_segment",
                "report_id": "RPT-27BDC120",
                "requires_human_approval": False,
                "evidence_reference_count": 4,
                "approval_state": "not_required",
                "memory_id": "must-not-reach-the-model",
                "parent_run_id": "must-not-reach-the-model",
                "alert_key": "must-not-reach-the-model",
                "decision_facts": {"hidden": True},
            }
        ],
    )

    assert "1" in text
    assert "missing segment" in text.lower()
    assert "RPT-27BDC120" in text
    assert "comparison context only" in text.lower()
    assert "does not prove the current root cause" in text.lower()
    assert "does not establish recurrence across different dates" in text.lower()
    assert "must-not-reach-the-model" not in text


def test_operator_answer_fallback_reports_no_exact_history_without_claiming_never(
    monkeypatch,
) -> None:
    """
    Ensure an empty exact-match result is not described as global incident absence.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    monkeypatch.setattr(
        copilot,
        "run_llm_task",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("provider unavailable")
        ),
    )

    text = copilot.build_operator_answer(
        question="Show previous investigation history.",
        alert=sample_alert(),
        incident_history_rows=[],
    )

    assert "no earlier investigation record" in text.lower()
    assert "does not prove" in text.lower()
    assert "another alert" in text.lower()
