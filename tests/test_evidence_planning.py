####
## Evidence Planning Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agent.graph import build_report_from_state
from agent.llm.client import LlmResponse
from agent.nodes import TriageNodeFactory
from agent.planning import evidence as evidence_planning
from agent.planning.evidence import (
    EVIDENCE_PLANNING_ROUTE,
    EvidencePlanProposal,
    build_evidence_plan_for_state,
    build_policy_requests,
)
from agent.state import (
    Alert,
    EvidenceCategory,
    EvidencePlan,
    EvidenceRequest,
    Hypothesis,
    TriageState,
)


# --- Defining Test Helpers
def build_alert(metric: str = "row_count_positive") -> Alert:
    """
    Build a representative orders alert for planning tests.

    Args:
        metric: DQ metric associated with the alert.

    Returns:
        Validated Alert instance.
    """
    return Alert(
        alert_key=f"orders|dq_failure|2026-06-10|dq.raw_orders|{metric}|table",
        severity="critical",
        table_name="dq.raw_orders",
        metric=metric,
        dt="2026-06-10",
        observed_value=0,
        expected_value=1,
    )


def build_schema_alert() -> Alert:
    """
    Build a schema drift alert for category-policy tests.

    Returns:
        Validated Alert with exact detector run correlation.
    """
    return Alert(
        alert_key="orders|schema_drift|2026-06-10|dq.raw_orders|schema_contract_drift|fingerprint",
        alert_type="schema_drift",
        severity="critical",
        table_name="dq.raw_orders",
        metric="schema_contract_drift",
        dt="2026-06-10",
        observed_value=2,
        expected_value=0,
        details={"source_schema_run_id": "manual__schema_planning_test"},
    )


def build_llm_response(structured_output: dict[str, object] | None) -> LlmResponse:
    """
    Build a normalized LLM response without an external provider call.

    Args:
        structured_output: Optional validated planning payload.

    Returns:
        LlmResponse suitable for planner tests.
    """
    return LlmResponse(
        agent_run_id=uuid4(),
        route_name=EVIDENCE_PLANNING_ROUTE,
        provider="gemini" if structured_output else "heuristic",
        model="gemini-3.5-flash-lite" if structured_output else "heuristic-v1",
        content=json.dumps(structured_output) if structured_output else "Local evidence policy fallback.",
        structured_output=structured_output,
        used_heuristic=structured_output is None,
        metadata={
            "requested_route": EVIDENCE_PLANNING_ROUTE,
            "structured_output_requested": True,
            "structured_output_status": "validated" if structured_output else "heuristic_fallback",
        },
    )


def build_node_factory() -> TriageNodeFactory:
    """
    Build a node factory whose collectors are inspected but never executed.

    Returns:
        TriageNodeFactory with inert callback dependencies.
    """
    config = SimpleNamespace(
        clickhouse_host=None,
        clickhouse_port=None,
        manifest_path=None,
        manifest_s3_uri=None,
        s3_endpoint_url=None,
        artifacts_bucket=None,
        artifacts_prefix="agent-reports",
    )

    return TriageNodeFactory(
        config=config,
        append_error=lambda state, message: state.errors.append(message),
        current_partition_sql=lambda alert: "SELECT 1",
        recent_partition_sql=lambda alert: "SELECT 1",
        sql_result_to_evidence=lambda result, description, summary: None,
        build_evidence_plan_for_state=lambda state: None,
        build_hypotheses_for_state=lambda state: [],
        frame_hypotheses_for_state=lambda state, hypotheses: None,
        build_llm_report_narrative=lambda state: None,
        llm_response_to_evidence=lambda response: None,
        build_report_from_state=lambda state, response: None,
        evidence_planning_route_name=EVIDENCE_PLANNING_ROUTE,
        hypothesis_framing_route_name="hypothesis_framing",
        llm_route_name="triage_reasoning",
        tool_name="langgraph_triage",
    )


# --- Defining Contract Tests
def test_evidence_request_rejects_unknown_category_and_executable_fields() -> None:
    """
    Ensure model output cannot introduce SQL or arbitrary tool categories.

    Returns:
        None.
    """
    with pytest.raises(ValidationError):
        EvidenceRequest(
            category="raw_sql",
            reason="Attempt to bypass the collector allowlist.",
            priority=1,
        )

    with pytest.raises(ValidationError):
        EvidenceRequest.model_validate(
            {
                "category": "dq_history",
                "reason": "Review recent deterministic DQ check results.",
                "priority": 1,
                "sql": "DROP TABLE dq.raw_orders",
            }
        )

    proposal_schema = json.dumps(EvidencePlanProposal.model_json_schema()).lower()

    assert '"sql"' not in proposal_schema
    assert '"command"' not in proposal_schema
    assert '"tool_name"' not in proposal_schema


def test_evidence_plan_rejects_duplicate_categories() -> None:
    """
    Ensure duplicate collectors cannot enter one planning handoff.

    Returns:
        None.
    """
    request = EvidenceRequest(
        category=EvidenceCategory.DQ_HISTORY,
        reason="Review recent deterministic DQ check outcomes.",
        priority=1,
        required=True,
    )

    with pytest.raises(ValidationError, match="categories must be unique"):
        EvidencePlan(
            investigation_question="Which DQ failures repeated for this alert?",
            requests=[request, request.model_copy()],
            planner_source="provider_fallback",
        )


def test_row_count_policy_requires_bounded_baseline_and_trend() -> None:
    """
    Ensure row-count incidents receive complete deterministic evidence policy.

    Returns:
        None.
    """
    requests   = build_policy_requests(alert=build_alert())
    categories = [str(request.category) for request in requests]

    assert categories == [
        "current_partition_row_count",
        "dq_history",
        "incident_history",
        "pipeline_runs",
        "dbt_lineage",
        "recent_partition_trend",
    ]
    assert all(request.required for request in requests)
    assert len(requests) == 6


def test_schema_alert_policy_uses_exact_schema_lineage_and_pipeline_evidence() -> None:
    """
    Ensure schema alerts do not run irrelevant partition or DQ-history collectors.

    Returns:
        None.
    """
    requests   = build_policy_requests(alert=build_schema_alert())
    categories = [str(request.category) for request in requests]

    assert categories == [
        "schema_drift",
        "incident_history",
        "pipeline_runs",
        "dbt_lineage",
    ]
    assert "current_partition_row_count" not in categories
    assert "dq_history" not in categories
    assert all(request.required for request in requests)


# --- Defining Planner Tests
def test_llm_plan_is_completed_by_deterministic_policy(monkeypatch) -> None:
    """
    Ensure an incomplete model proposal cannot remove mandatory evidence.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    structured_output = {
        "investigation_question": "Did recent DQ failures repeat for this partition?",
        "requests": [
            {
                "category": "dq_history",
                "reason": "Compare this failure with recent deterministic check outcomes.",
                "priority": 1,
            }
        ],
    }
    monkeypatch.setattr(
        evidence_planning,
        "run_llm_task",
        lambda **kwargs: build_llm_response(structured_output=structured_output),
    )
    state  = TriageState(alert=build_alert())
    result = build_evidence_plan_for_state(state=state)

    categories = [str(request.category) for request in result.plan.requests]

    assert result.plan.planner_source == "llm_with_policy"
    assert categories == [
        "current_partition_row_count",
        "dq_history",
        "recent_partition_trend",
        "incident_history",
        "pipeline_runs",
        "dbt_lineage",
    ]
    assert set(result.plan.policy_added_categories) == {
        "current_partition_row_count",
        "incident_history",
        "pipeline_runs",
        "dbt_lineage",
        "recent_partition_trend",
    }
    assert result.plan.policy_adjusted_categories == ["dq_history"]
    assert result.llm_response is not None


def test_unavailable_provider_uses_policy_fallback(monkeypatch) -> None:
    """
    Ensure no-key or no-credit provider routes preserve a complete evidence plan.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    response = build_llm_response(structured_output=None)
    monkeypatch.setattr(evidence_planning, "run_llm_task", lambda **kwargs: response)

    result = build_evidence_plan_for_state(state=TriageState(alert=build_alert(metric="segment_coverage")))

    assert result.plan.planner_source == "provider_fallback"
    assert [str(request.category) for request in result.plan.requests] == [
        "current_partition_row_count",
        "dq_history",
        "incident_history",
        "pipeline_runs",
        "dbt_lineage",
    ]
    assert result.plan.llm_provider == "heuristic"
    assert result.error_type == ""


def test_schema_planner_cannot_add_irrelevant_partition_category(monkeypatch) -> None:
    """
    Ensure contextual policy drops valid-but-irrelevant categories from a model proposal.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    structured_output = {
        "investigation_question": "Which schema expectations changed and what depends on this table?",
        "requests": [
            {
                "category": "current_partition_row_count",
                "reason": "Attempt to add a row count that does not explain contract drift.",
                "priority": 1,
            },
            {
                "category": "schema_drift",
                "reason": "Read the persisted contract comparison for this exact detector run.",
                "priority": 1,
            },
        ],
    }
    monkeypatch.setattr(
        evidence_planning,
        "run_llm_task",
        lambda **kwargs: build_llm_response(structured_output=structured_output),
    )

    result     = build_evidence_plan_for_state(state=TriageState(alert=build_schema_alert()))
    categories = [str(request.category) for request in result.plan.requests]

    assert categories == [
        "schema_drift",
        "incident_history",
        "pipeline_runs",
        "dbt_lineage",
    ]
    assert "current_partition_row_count" not in categories


def test_planner_exception_uses_error_fallback(monkeypatch) -> None:
    """
    Ensure planner exceptions are reduced to error type and safe policy output.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    def raise_planner_error(**kwargs):
        """
        Raise a controlled planner failure.

        Args:
            kwargs: Ignored routed LLM inputs.

        Raises:
            RuntimeError: Always raised for fallback testing.
        """
        raise RuntimeError("provider details must not enter state")

    monkeypatch.setattr(evidence_planning, "run_llm_task", raise_planner_error)

    result = build_evidence_plan_for_state(state=TriageState(alert=build_alert()))

    assert result.plan.planner_source == "error_fallback"
    assert result.error_type == "RuntimeError"
    assert result.llm_response is None
    assert "provider details" not in result.plan.model_dump_json()


# --- Defining Collector Boundary Tests
def test_node_factory_resolves_only_allowlisted_collectors() -> None:
    """
    Ensure a plan selects internal collectors without dynamic tool resolution.

    Returns:
        None.
    """
    state = TriageState(
        alert=build_alert(metric="segment_coverage"),
        evidence_plan=EvidencePlan(
            investigation_question="Which checks and lineage assets explain the segment gap?",
            requests=[
                EvidenceRequest(
                    category=EvidenceCategory.DQ_HISTORY,
                    reason="Review historical segment coverage outcomes.",
                    priority=1,
                    required=True,
                ),
                EvidenceRequest(
                    category=EvidenceCategory.DBT_LINEAGE,
                    reason="Identify downstream models affected by the segment gap.",
                    priority=2,
                    required=True,
                ),
                EvidenceRequest(
                    category=EvidenceCategory.INCIDENT_HISTORY,
                    reason="Compare bounded prior outcomes for this exact alert identity.",
                    priority=3,
                    required=True,
                ),
            ],
            planner_source="provider_fallback",
        ),
    )
    builders = build_node_factory().resolve_evidence_builders(state=state)

    assert [name for name, _ in builders] == [
        "dq_history",
        "dbt_lineage",
        "incident_history",
    ]
    assert state.errors == []


def test_node_factory_routes_schema_alert_to_schema_specific_collector() -> None:
    """
    Ensure schema evidence plans resolve through the hardcoded collector allowlist.

    Returns:
        None.
    """
    state = TriageState(
        alert=build_schema_alert(),
        evidence_plan=EvidencePlan(
            investigation_question="Which persisted schema findings explain this contract alert?",
            requests=[
                EvidenceRequest(
                    category=EvidenceCategory.SCHEMA_DRIFT,
                    reason="Read exact persisted schema contract findings.",
                    priority=1,
                    required=True,
                )
            ],
            planner_source="provider_fallback",
        ),
    )
    builders = build_node_factory().resolve_evidence_builders(state=state)

    assert [name for name, _ in builders] == ["schema_drift"]
    assert builders[0][1].__name__ == "collect_schema_drift_for_state"


def test_report_contains_auditable_evidence_plan() -> None:
    """
    Ensure JSON and Markdown reports expose planning source and categories.

    Returns:
        None.
    """
    state = TriageState(
        alert=build_alert(metric="segment_coverage"),
        evidence_plan=EvidencePlan(
            investigation_question="Which evidence explains the missing country and channel segment?",
            requests=[
                EvidenceRequest(
                    category=EvidenceCategory.DQ_HISTORY,
                    reason="Review recent segment coverage check outcomes.",
                    priority=1,
                    required=True,
                )
            ],
            planner_source="provider_fallback",
            llm_route=EVIDENCE_PLANNING_ROUTE,
            llm_provider="heuristic",
            llm_model="heuristic-v1",
        ),
        hypotheses=[
            Hypothesis(
                title="Missing expected segment",
                description="One expected country and channel segment is absent.",
                likelihood=0.8,
                confidence=0.8,
                root_cause_category="segment_gap",
                recommended_action="Review upstream segment completeness before remediation.",
            )
        ],
    )

    report = build_report_from_state(state=state)

    assert report.evidence_plan is not None
    assert report.evidence_plan.planner_source == "provider_fallback"
    assert "## Evidence Plan" in report.markdown_report
    assert "`dq_history`" in report.markdown_report
    assert json.loads(report.model_dump_json())["evidence_plan"]["llm_provider"] == "heuristic"
