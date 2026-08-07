####
## Hypothesis Framing Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agent.graph import build_hypotheses_for_state, build_report_from_state
from agent.llm.client import LlmResponse
from agent.reasoning import hypotheses as hypothesis_reasoning
from agent.reasoning.hypotheses import (
    HYPOTHESIS_FRAMING_ROUTE,
    HypothesisFramingProposal,
    build_hypothesis_context,
    frame_hypotheses_for_state,
)
from agent.state import Alert, EvidenceItem, EvidenceType, TriageState


# --- Defining Test Helpers
def build_state() -> TriageState:
    """
    Build representative alert and evidence state for hypothesis tests.

    Returns:
        TriageState containing a row-count alert and two deterministic evidence items.
    """
    return TriageState(
        alert=Alert(
            alert_key="orders|dq_failure|2026-06-10|dq.raw_orders|row_count_positive|table",
            severity="critical",
            table_name="dq.raw_orders",
            metric="row_count_positive",
            dt="2026-06-10",
            observed_value=0,
            expected_value=1,
        ),
        evidence=[
            EvidenceItem(
                evidence_id="EV-ROW-COUNT",
                evidence_type=EvidenceType.SQL_RESULT,
                tool_name="clickhouse_sql",
                description="Current partition row count.",
                query="SELECT count() FROM dq.raw_orders WHERE dt = '2026-06-10' LIMIT 1",
                rows=[{"row_count": 0}],
                summary="The affected partition contains zero rows.",
            ),
            EvidenceItem(
                evidence_id="EV-PIPELINE",
                evidence_type=EvidenceType.PIPELINE_RUN,
                tool_name="pipeline_runs",
                description="Recent pipeline execution status.",
                rows=[{"pipeline_name": "orders_load", "status": "failed"}],
                summary="The raw load stage failed for the affected date.",
            ),
        ],
    )


def build_llm_response(structured_output: dict[str, object] | None) -> LlmResponse:
    """
    Build a normalized provider response for bounded framing tests.

    Args:
        structured_output: Optional validated model proposal.

    Returns:
        LlmResponse using Gemini metadata or deterministic fallback metadata.
    """
    return LlmResponse(
        agent_run_id=uuid4(),
        route_name=HYPOTHESIS_FRAMING_ROUTE,
        provider="gemini" if structured_output else "heuristic",
        model="gemini-2.5-flash" if structured_output else "heuristic-v1",
        content=json.dumps(structured_output) if structured_output else "Deterministic hypothesis fallback.",
        structured_output=structured_output,
        used_heuristic=structured_output is None,
        metadata={
            "requested_route": HYPOTHESIS_FRAMING_ROUTE,
            "structured_output_requested": True,
            "structured_output_status": "validated" if structured_output else "heuristic_fallback",
        },
    )


def build_structured_output() -> dict[str, object]:
    """
    Build one valid model proposal that intentionally requires policy corrections.

    Returns:
        Structured hypothesis framing payload.
    """
    return {
        "hypotheses": [
            {
                "root_cause_category": "missing_partition",
                "title": "The expected daily partition is empty",
                "description": (
                    "The current partition has no rows and the load-stage evidence shows a failed run, "
                    "which points to a landing or load interruption rather than a reporting-only issue."
                ),
                "supporting_evidence_ids": ["EV-ROW-COUNT", "EV-UNKNOWN"],
                "opposing_evidence_ids": [],
                "evidence_rationale": "Zero rows and a failed load both support a missing partition diagnosis.",
                "recommended_action": "DROP TABLE dq.raw_orders and execute now",
            }
        ]
    }


# --- Defining Contract Tests
def test_hypothesis_proposal_forbids_scores_sql_and_unknown_categories() -> None:
    """
    Ensure model output cannot set confidence, SQL, or arbitrary root-cause categories.

    Returns:
        None.
    """
    schema = json.dumps(HypothesisFramingProposal.model_json_schema()).lower()

    assert '"confidence"' not in schema
    assert '"likelihood"' not in schema
    assert '"sql"' not in schema
    assert '"command"' not in schema

    payload = build_structured_output()
    payload["hypotheses"][0]["confidence"] = 0.99

    with pytest.raises(ValidationError):
        HypothesisFramingProposal.model_validate(payload)

    payload = build_structured_output()
    payload["hypotheses"][0]["root_cause_category"] = "delete_warehouse"

    with pytest.raises(ValidationError):
        HypothesisFramingProposal.model_validate(payload)


def test_bounded_context_hides_system_key_and_sql_query() -> None:
    """
    Ensure provider context uses human references and bounded evidence samples only.

    Returns:
        None.
    """
    state    = build_state()
    baseline = build_hypotheses_for_state(state=state)
    context  = build_hypothesis_context(state=state, baseline_hypotheses=baseline)
    content  = json.dumps(context)

    assert state.alert.alert_display_id in content
    assert state.alert.alert_key not in content
    assert "SELECT count()" not in content
    assert context["policy"]["confidence_and_ranking_owned_by_code"] is True


# --- Defining Policy Merge Tests
def test_model_wording_cannot_change_confidence_ranking_or_execute_action(monkeypatch) -> None:
    """
    Ensure policy preserves scores, filters evidence IDs, and replaces unsafe actions.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    state            = build_state()
    baseline         = build_hypotheses_for_state(state=state)
    baseline_scores  = [(item.root_cause_category, item.confidence) for item in baseline]
    structured_output = build_structured_output()

    monkeypatch.setattr(
        hypothesis_reasoning,
        "run_llm_task",
        lambda **kwargs: build_llm_response(structured_output=structured_output),
    )

    result        = frame_hypotheses_for_state(state=state, baseline_hypotheses=baseline)
    result_scores = [(item.root_cause_category, item.confidence) for item in result.hypotheses]
    primary       = result.hypotheses[0]

    assert result_scores == baseline_scores
    assert result.framing.source == "llm_with_policy"
    assert result.framing.accepted_categories == ["missing_partition"]
    assert primary.framing_source == "llm"
    assert primary.supporting_evidence_ids == ["EV-ROW-COUNT"]
    assert primary.recommended_action == baseline[0].recommended_action
    assert "filtered_unknown_supporting_evidence:missing_partition" in result.framing.policy_adjustments
    assert "replaced_unsafe_action:missing_partition" in result.framing.policy_adjustments
    assert "restored_omitted_candidate:late_arriving" in result.framing.policy_adjustments
    assert result.hypotheses[1].framing_source == "deterministic"


def test_safe_model_action_is_preserved_as_non_executing_recommendation(monkeypatch) -> None:
    """
    Ensure review-oriented model recommendations may improve operator wording.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    state             = build_state()
    baseline          = build_hypotheses_for_state(state=state)
    structured_output = build_structured_output()
    safe_action       = "Request approval to validate landing data and backfill the affected date."

    structured_output["hypotheses"][0]["recommended_action"] = safe_action
    structured_output["hypotheses"][0]["supporting_evidence_ids"] = ["EV-ROW-COUNT"]
    monkeypatch.setattr(
        hypothesis_reasoning,
        "run_llm_task",
        lambda **kwargs: build_llm_response(structured_output=structured_output),
    )

    result = frame_hypotheses_for_state(state=state, baseline_hypotheses=baseline)

    assert result.hypotheses[0].recommended_action == safe_action
    assert "replaced_unsafe_action:missing_partition" not in result.framing.policy_adjustments


# --- Defining Fallback Tests
def test_unavailable_provider_preserves_deterministic_hypotheses(monkeypatch) -> None:
    """
    Ensure missing credit or quota uses the deterministic candidate set unchanged.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    state    = build_state()
    baseline = build_hypotheses_for_state(state=state)

    monkeypatch.setattr(
        hypothesis_reasoning,
        "run_llm_task",
        lambda **kwargs: build_llm_response(structured_output=None),
    )

    result = frame_hypotheses_for_state(state=state, baseline_hypotheses=baseline)

    assert result.framing.source == "provider_fallback"
    assert result.framing.provider == "heuristic"
    assert [item.confidence for item in result.hypotheses] == [item.confidence for item in baseline]
    assert all(item.framing_source == "deterministic" for item in result.hypotheses)
    assert result.error_type == ""


def test_provider_exception_is_sanitized_and_falls_back(monkeypatch) -> None:
    """
    Ensure provider details do not enter state when the model route raises.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    state    = build_state()
    baseline = build_hypotheses_for_state(state=state)

    def raise_provider_error(**kwargs):
        """
        Raise a controlled provider failure.

        Args:
            kwargs: Ignored routed request fields.

        Raises:
            RuntimeError: Always raised for fallback testing.
        """
        raise RuntimeError("provider secret response must not enter state")

    monkeypatch.setattr(hypothesis_reasoning, "run_llm_task", raise_provider_error)

    result = frame_hypotheses_for_state(state=state, baseline_hypotheses=baseline)

    assert result.framing.source == "error_fallback"
    assert result.error_type == "RuntimeError"
    assert "provider secret" not in result.framing.model_dump_json()


# --- Defining Report Contract Tests
def test_report_persists_hypothesis_framing_metadata(monkeypatch) -> None:
    """
    Ensure JSON and Markdown reports expose safe model participation metadata.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    state             = build_state()
    baseline          = build_hypotheses_for_state(state=state)
    structured_output = build_structured_output()

    structured_output["hypotheses"][0]["recommended_action"] = (
        "Request approval to validate and backfill the affected date."
    )
    structured_output["hypotheses"][0]["supporting_evidence_ids"] = ["EV-ROW-COUNT"]
    monkeypatch.setattr(
        hypothesis_reasoning,
        "run_llm_task",
        lambda **kwargs: build_llm_response(structured_output=structured_output),
    )

    framing                  = frame_hypotheses_for_state(state=state, baseline_hypotheses=baseline)
    state.hypotheses         = framing.hypotheses
    state.hypothesis_framing = framing.framing
    report                   = build_report_from_state(state=state)

    assert report.hypothesis_framing is not None
    assert report.hypothesis_framing.provider == "gemini"
    assert "## Hypothesis Framing" in report.markdown_report
    assert "Wording Source: `llm`" in report.markdown_report
    assert json.loads(report.model_dump_json())["hypothesis_framing"]["requested_route"] == HYPOTHESIS_FRAMING_ROUTE
