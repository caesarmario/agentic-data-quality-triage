####
## Supervisor Routing Policy Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Validate deterministic specialist, model-route, risk, and approval policy."""

# --- Importing Libraries
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agent.llm.config import load_model_routing_config
from agent.specialists.incident_triage import load_llm_usage_summary
from agent.specialists.contracts import (
    AgentApprovalState,
    AgentModelRoute,
    AgentResultEnvelope,
    AgentRiskTier,
    AgentTaskEnvelope,
    AgentTaskStatus,
    EvidenceReference,
)
from agent.specialists.registry import (
    INCIDENT_TRIAGE_SPECIALIST_NAME,
    METADATA_LINEAGE_SPECIALIST_NAME,
    SCHEMA_DRIFT_SPECIALIST_NAME,
    SQL_REVIEW_SPECIALIST_NAME,
    enforce_result_contract,
    enforce_task_capability,
)
from agent.state import (
    Alert,
    EvidenceItem,
    EvidenceType,
    Hypothesis,
    IncidentComplexityTier,
    Severity,
    TriageState,
)
from agent.supervisor.models import SupervisorIntent, SupervisorRequest, SupervisorRoute
from agent.supervisor.policy import (
    IncidentComplexityReason,
    RoutingPolicyDisposition,
    RoutingPolicyReason,
    assess_incident_complexity,
    evaluate_post_handoff_policy,
    evaluate_pre_handoff_policy,
    resolve_report_reasoning_policy,
)
from agent.supervisor.routing import build_supervisor_handoff, resolve_supervisor_route
from agent.supervisor.runtime import terminal_approval_state
from pipelines.common.logging import logger


# --- Defining Test Configuration
PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH  = PROJECT_ROOT / "configs" / "agent" / "model_routing.yml"


# --- Defining Test Doubles
class FakeQueryResult:
    """Return clickhouse-connect-compatible audit rows."""

    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        """
        Build fixed JSON audit rows.

        Args:
            payloads: LLM route audit payloads returned by the fake client.

        Returns:
            None.
        """
        self.column_names = ["output_json"]
        self.result_rows  = [(json.dumps(payload),) for payload in payloads]


class FakeAuditClient:
    """Provide deterministic LLM route audit payloads to usage aggregation."""

    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        """
        Store fixed audit payloads.

        Args:
            payloads: LLM route completion payloads.

        Returns:
            None.
        """
        self.payloads = payloads

    def query(self, sql: str) -> FakeQueryResult:
        """
        Return fixed rows after checking the query remains bounded.

        Args:
            sql: Read-only child audit query.

        Returns:
            FakeQueryResult containing the configured payloads.
        """
        assert "action = 'llm_route_completed'" in sql
        assert "LIMIT 100" in sql

        return FakeQueryResult(self.payloads)


# --- Defining Test Helpers
def build_policy_task(request: SupervisorRequest) -> tuple[SupervisorRoute, AgentTaskEnvelope]:
    """
    Resolve and authorize one supervisor request through production routing policy.

    Args:
        request: Bounded supervisor request used by the test scenario.

    Returns:
        Resolved route and validated specialist handoff task.
    """
    route = resolve_supervisor_route(request)
    task  = build_supervisor_handoff(
        request=request,
        route=route,
        parent_run_id=uuid4(),
    )

    enforce_task_capability(task)

    logger.info(
        "Validated supervisor routing policy | intent=%s specialist=%s route=%s risk=%s",
        route.intent.value,
        task.specialist_name,
        task.model_route.value,
        task.risk_tier.value,
    )

    return route, task


def rebuild_task(task: AgentTaskEnvelope, **updates: object) -> AgentTaskEnvelope:
    """
    Revalidate a task after applying an explicit policy-tampering scenario.

    Args:
        task: Original policy-compliant specialist handoff.
        **updates: Contract fields changed for one negative test.

    Returns:
        Revalidated task envelope containing the requested changes.
    """
    payload = task.model_dump(mode="python")
    payload.update(updates)

    return AgentTaskEnvelope.model_validate(payload)


def fallback_chain(route_name: str) -> tuple[str, ...]:
    """
    Follow configured model fallbacks without calling an external provider.

    Args:
        route_name: Initial provider-agnostic route name.

    Returns:
        Ordered route names ending at a self-terminal or empty fallback.

    Raises:
        AssertionError: If the static fallback configuration contains a cycle.
    """
    config  = load_model_routing_config(config_path=CONFIG_PATH)
    visited: list[str] = []
    current = route_name

    while current and current not in visited:
        visited.append(current)
        configured_fallback = config.routes[current].fallback_route

        if not configured_fallback or configured_fallback == current:
            return tuple(visited)

        current = configured_fallback

    raise AssertionError(f"Model route fallback cycle detected in test policy: {current}")


def build_incident_result(
    task: AgentTaskEnvelope,
    *,
    confidence: float,
    model_route: AgentModelRoute,
    strong_review_requested: bool,
    strong_review_satisfied: bool,
    requires_human_approval: bool = False,
    approval_gated_action_count: int = 0,
    complexity_tier: str = "low",
    complexity_score: int = 0,
    complexity_reason_codes: list[str] | None = None,
) -> AgentResultEnvelope:
    """
    Build one evidence-backed incident result for terminal policy evaluation.

    Args:
        task: Authorized incident handoff.
        confidence: Final deterministic confidence.
        model_route: Highest capability proven by route telemetry.
        strong_review_requested: Whether the strong provider route was requested.
        strong_review_satisfied: Whether strong external output was returned.
        requires_human_approval: Specialist action approval flag.
        approval_gated_action_count: Number of proposed gated actions.
        complexity_tier: Deterministic low, moderate, or high tier.
        complexity_score: Additive deterministic complexity score.
        complexity_reason_codes: Stable evidence-derived complexity reasons.

    Returns:
        Valid AgentResultEnvelope correlated to the handoff.
    """
    llm_used = model_route != AgentModelRoute.NO_LLM_FALLBACK

    return AgentResultEnvelope(
        task_id=task.task_id,
        parent_run_id=task.parent_run_id,
        specialist_name=task.specialist_name,
        task_type=task.task_type,
        status=AgentTaskStatus.SUCCESS,
        evidence_references=[
            EvidenceReference(
                evidence_type="triage_report",
                source_tool="s3_artifacts",
                reference=f"s3://dq-artifacts/agent-reports/{task.task_id}/report.json",
                summary="Evidence-backed triage report used by terminal routing policy.",
            )
        ],
        structured_output={
            "requested_model_routes": (
                ["low_confidence_rca"]
                if strong_review_requested
                else ["triage_reasoning"]
            ),
            "executed_model_routes": (
                ["low_confidence_rca"]
                if strong_review_satisfied
                else ["triage_reasoning"]
            ),
            "strong_review_requested": strong_review_requested,
            "strong_review_satisfied": strong_review_satisfied,
            "approval_gated_action_count": approval_gated_action_count,
            "complexity_tier": complexity_tier,
            "complexity_score": complexity_score,
            "complexity_reason_codes": complexity_reason_codes or [],
        },
        confidence=confidence,
        model_route=model_route,
        model_call_count=1 if llm_used else 0,
        token_usage=320 if llm_used else 0,
        estimated_cost_usd=0.002 if llm_used else 0.0,
        duration_ms=125,
        recommended_next_step="Review the evidence before any operational action.",
        requires_human_approval=requires_human_approval,
    )


def build_complexity_state(
    *,
    severity: Severity = Severity.WARNING,
    evidence: list[EvidenceItem] | None = None,
    hypotheses: list[Hypothesis] | None = None,
    errors: list[str] | None = None,
) -> TriageState:
    """
    Build one bounded triage state for deterministic complexity policy tests.

    Args:
        severity: Alert severity retained as one weak contextual signal.
        evidence: Deterministic or narrative evidence items.
        hypotheses: Ranked evidence-backed hypotheses.
        errors: Unresolved tool or route errors retained by the graph.

    Returns:
        Valid TriageState containing the requested policy facts.
    """
    return TriageState(
        alert=Alert(
            alert_key="orders|dq_failure|2026-08-22|dq.raw_orders|row_count|table",
            severity=severity,
            table_name="dq.raw_orders",
            metric="row_count",
            dt="2026-08-22",
        ),
        evidence=evidence or [],
        hypotheses=hypotheses or [],
        errors=errors or [],
    )


# --- Testing Deterministic And Low-Cost Routing
@pytest.mark.parametrize(
    ("supervisor_request", "expected_specialist", "expected_task_type"),
    (
        pytest.param(
            SupervisorRequest(
                intent=SupervisorIntent.ASSET_CONTEXT,
                qualified_name="dq.raw_orders",
            ),
            METADATA_LINEAGE_SPECIALIST_NAME,
            "asset_context",
            id="asset-context",
        ),
        pytest.param(
            SupervisorRequest(
                intent=SupervisorIntent.BLAST_RADIUS,
                qualified_name="dq.raw_orders",
            ),
            METADATA_LINEAGE_SPECIALIST_NAME,
            "blast_radius",
            id="blast-radius",
        ),
        pytest.param(
            SupervisorRequest(
                intent=SupervisorIntent.TRUSTED_ASSET_SEARCH,
                query="orders",
            ),
            METADATA_LINEAGE_SPECIALIST_NAME,
            "trusted_asset_search",
            id="trusted-asset-search",
        ),
        pytest.param(
            SupervisorRequest(
                intent=SupervisorIntent.REVIEW_SQL,
                sql_proposal=(
                    "SELECT order_id FROM dq.raw_orders "
                    "WHERE dt = toDate('2026-08-22') LIMIT 10"
                ),
            ),
            SQL_REVIEW_SPECIALIST_NAME,
            "review_sql",
            id="sql-review",
        ),
        pytest.param(
            SupervisorRequest(
                intent=SupervisorIntent.SCHEMA_DRIFT_ASSESSMENT,
                schema_run_id="manual__schema_routing_policy",
                qualified_name="dq.raw_orders",
            ),
            SCHEMA_DRIFT_SPECIALIST_NAME,
            "assess_schema_drift",
            id="schema-drift",
        ),
    ),
)
def test_deterministic_tasks_use_no_llm_and_zero_model_budgets(
    supervisor_request: SupervisorRequest,
    expected_specialist: str,
    expected_task_type: str,
) -> None:
    """
    Prove deterministic specialist work cannot consume an external model budget.

    Args:
        supervisor_request: Bounded request selected by the parametrized policy case.
        expected_specialist: Registry specialist expected for the request.
        expected_task_type: Specialist task type expected for the request.

    Returns:
        None.
    """
    route, task = build_policy_task(supervisor_request)

    assert route.specialist_name == expected_specialist
    assert task.specialist_name == expected_specialist
    assert task.task_type == expected_task_type
    assert task.model_route == AgentModelRoute.NO_LLM_FALLBACK
    assert task.model_call_budget == 0
    assert task.token_budget == 0
    assert task.estimated_cost_budget_usd == 0.0


def test_incident_triage_receives_a_deepthink_capability_ceiling() -> None:
    """Prove triage may use strong reasoning without claiming that it already ran."""
    route, task = build_policy_task(
        SupervisorRequest(
            intent=SupervisorIntent.TRIAGE_ALERT,
            alert_key="DQ-20260822-A1B2C3",
        )
    )

    assert route.specialist_name == INCIDENT_TRIAGE_SPECIALIST_NAME
    assert task.model_route == AgentModelRoute.DEEPTHINK_LLM
    assert task.risk_tier == AgentRiskTier.MEDIUM
    assert task.model_call_budget > 0
    assert task.token_budget > 0
    assert task.estimated_cost_budget_usd > 0.0

    pre_policy = evaluate_pre_handoff_policy(task)

    assert pre_policy.authorized_model_route == AgentModelRoute.DEEPTHINK_LLM
    assert pre_policy.actual_model_route == AgentModelRoute.NO_LLM_FALLBACK
    assert pre_policy.reason_codes == (RoutingPolicyReason.LLM_CAPABILITY_AUTHORIZED,)


def test_explicit_and_auto_requests_resolve_to_the_same_policy_route() -> None:
    """Prove operator wording cannot make identical asset context route differently."""
    explicit_request = SupervisorRequest(
        intent=SupervisorIntent.ASSET_CONTEXT,
        qualified_name="dq.raw_orders",
    )
    automatic_request = SupervisorRequest(
        intent=SupervisorIntent.AUTO,
        question="Who owns this metadata asset?",
        qualified_name="dq.raw_orders",
    )

    explicit_route = resolve_supervisor_route(explicit_request)
    automatic_route = resolve_supervisor_route(automatic_request)

    assert automatic_route == explicit_route


def test_low_risk_narrative_route_has_bounded_fallback_to_heuristic() -> None:
    """Prove UI and Discord wording uses a cheap route with a local terminal fallback."""
    config = load_model_routing_config(config_path=CONFIG_PATH)
    route  = config.routes["cheap_summary"]

    assert route.reasoning_tier == "cheap"
    assert route.risk_tier == "low"
    assert fallback_chain("cheap_summary") == (
        "cheap_summary",
        "openai_summary",
        "evidence_summary",
    )
    assert config.routes["evidence_summary"].provider == "heuristic"


def test_stronger_rca_route_remains_configured_but_not_caller_selectable() -> None:
    """Prove stronger reasoning is policy configuration, not a request override."""
    config = load_model_routing_config(config_path=CONFIG_PATH)
    route  = config.routes["low_confidence_rca"]

    assert route.reasoning_tier == "strong"
    assert route.risk_tier == "high"
    assert route.fallback_route == "triage_reasoning"

    with pytest.raises(ValidationError, match="model_route"):
        SupervisorRequest.model_validate(
            {
                "intent": "asset_context",
                "qualified_name": "dq.raw_orders",
                "model_route": "deepthinkllm",
            }
        )

    with pytest.raises(ValidationError, match="risk_tier"):
        SupervisorRequest.model_validate(
            {
                "intent": "triage_alert",
                "alert_key": "DQ-20260822-A1B2C3",
                "risk_tier": "low",
            }
        )


def test_report_route_policy_escalates_only_after_low_confidence() -> None:
    """Select normal reasoning for sufficient evidence and strong RCA for low confidence."""
    standard = resolve_report_reasoning_policy(
        confidence=0.82,
        confidence_threshold=0.70,
    )
    escalated = resolve_report_reasoning_policy(
        confidence=0.58,
        confidence_threshold=0.70,
    )

    assert standard.provider_route == "triage_reasoning"
    assert standard.capability_route == AgentModelRoute.QUICKTHINK_LLM
    assert standard.strong_review_required is False
    assert standard.reason_code == RoutingPolicyReason.STANDARD_CONFIDENCE
    assert escalated.provider_route == "low_confidence_rca"
    assert escalated.capability_route == AgentModelRoute.DEEPTHINK_LLM
    assert escalated.strong_review_required is True
    assert escalated.reason_code == RoutingPolicyReason.LOW_CONFIDENCE


def test_critical_severity_alone_does_not_spend_strong_reasoning() -> None:
    """Keep severity as context instead of treating it as a model-spend switch."""
    assessment = assess_incident_complexity(
        build_complexity_state(severity=Severity.CRITICAL)
    )
    route = resolve_report_reasoning_policy(
        confidence=0.82,
        confidence_threshold=0.70,
        complexity_assessment=assessment,
    )

    assert assessment.tier == IncidentComplexityTier.LOW
    assert assessment.score == 1
    assert assessment.reason_codes == (
        IncidentComplexityReason.CRITICAL_SEVERITY.value,
    )
    assert assessment.strong_reasoning_required is False
    assert route.provider_route == "triage_reasoning"
    assert route.capability_route == AgentModelRoute.QUICKTHINK_LLM


def test_wide_lineage_and_critical_context_trigger_high_complexity_route() -> None:
    """Escalate a cross-signal incident even when deterministic confidence is high."""
    state = build_complexity_state(
        severity=Severity.CRITICAL,
        evidence=[
            EvidenceItem(
                evidence_type=EvidenceType.LINEAGE,
                tool_name="dbt_lineage",
                description="Inspect direct blast radius.",
                rows=[
                    {
                        "parents": ["source.orders", "source.customers"],
                        "children": ["model.stg_orders", "model.fct_orders_daily"],
                    }
                ],
                summary="Four directly related lineage assets were found.",
            )
        ],
    )
    assessment = assess_incident_complexity(state)
    route = resolve_report_reasoning_policy(
        confidence=0.86,
        confidence_threshold=0.70,
        complexity_assessment=assessment,
    )

    assert assessment.tier == IncidentComplexityTier.HIGH
    assert assessment.score == 4
    assert assessment.lineage_asset_count == 4
    assert set(assessment.reason_codes) == {
        IncidentComplexityReason.CRITICAL_SEVERITY.value,
        IncidentComplexityReason.WIDE_LINEAGE_IMPACT.value,
    }
    assert route.provider_route == "low_confidence_rca"
    assert route.capability_route == AgentModelRoute.DEEPTHINK_LLM
    assert route.reason_code == RoutingPolicyReason.HIGH_COMPLEXITY


def test_narrative_note_cannot_inflate_deterministic_complexity() -> None:
    """Ignore LLM-authored notes when calculating evidence breadth and lineage impact."""
    assessment = assess_incident_complexity(
        build_complexity_state(
            evidence=[
                EvidenceItem(
                    evidence_type=EvidenceType.NOTE,
                    tool_name="llm_narrative",
                    description="Model-authored explanation only.",
                    rows=[
                        {
                            "parents": ["a", "b", "c"],
                            "children": ["d", "e", "f"],
                        }
                    ],
                    summary="This note is not source-of-truth evidence.",
                )
            ]
        )
    )

    assert assessment.tier == IncidentComplexityTier.LOW
    assert assessment.score == 0
    assert assessment.deterministic_evidence_types == ()
    assert assessment.lineage_asset_count == 0
    assert assessment.reason_codes == ()


def test_clean_schema_history_cannot_inflate_incident_complexity() -> None:
    """Ignore pass-only schema history even when its evidence row count is non-zero."""
    assessment = assess_incident_complexity(
        build_complexity_state(
            evidence=[
                EvidenceItem(
                    evidence_type=EvidenceType.SCHEMA_DRIFT,
                    tool_name="schema_drift",
                    description="Inspect the latest persisted schema contract result.",
                    rows=[
                        {
                            "column_name": "order_id",
                            "check_type": "column_type",
                            "status": "pass",
                            "severity": "info",
                        }
                    ],
                    row_count=1,
                    summary="The persisted schema snapshot matches the contract.",
                )
            ]
        )
    )

    assert assessment.tier == IncidentComplexityTier.LOW
    assert assessment.score == 0
    assert assessment.schema_finding_count == 0
    assert IncidentComplexityReason.SCHEMA_DRIFT_CONTEXT.value not in assessment.reason_codes


def test_persisted_schema_findings_contribute_to_incident_complexity() -> None:
    """Count warning and failure rows while ignoring pass rows in the same evidence item."""
    assessment = assess_incident_complexity(
        build_complexity_state(
            severity=Severity.CRITICAL,
            evidence=[
                EvidenceItem(
                    evidence_type=EvidenceType.SCHEMA_DRIFT,
                    tool_name="schema_drift",
                    description="Inspect exact persisted schema contract findings.",
                    rows=[
                        {"column_name": "order_id", "status": "pass"},
                        {"column_name": "channel", "status": "warn"},
                        {"column_name": "amount", "status": "fail"},
                    ],
                    row_count=3,
                    summary="Two persisted schema findings require review.",
                )
            ],
        )
    )

    assert assessment.tier == IncidentComplexityTier.HIGH
    assert assessment.score == 4
    assert assessment.schema_finding_count == 2
    assert set(assessment.reason_codes) == {
        IncidentComplexityReason.CRITICAL_SEVERITY.value,
        IncidentComplexityReason.SCHEMA_DRIFT_CONTEXT.value,
    }


def test_contradictory_evidence_is_high_complexity_without_caller_override() -> None:
    """Treat explicit evidence contradiction as a deterministic strong-review signal."""
    assessment = assess_incident_complexity(
        build_complexity_state(
            evidence=[
                EvidenceItem(
                    evidence_type=EvidenceType.DQ_HISTORY,
                    tool_name="dq_history",
                    description="Compare the current issue with historical checks.",
                    summary="History conflicts with the current missing-partition hypothesis.",
                    contradicts_hypotheses=["missing_partition"],
                )
            ]
        )
    )

    assert assessment.tier == IncidentComplexityTier.HIGH
    assert assessment.score == 4
    assert assessment.contradiction_count == 1
    assert assessment.reason_codes == (
        IncidentComplexityReason.CONTRADICTORY_EVIDENCE.value,
    )


def test_strong_route_fallback_does_not_claim_deepthink_capability() -> None:
    """Treat a strong request that executed a mid route as quickthink, not deepthink."""
    usage = load_llm_usage_summary(
        client=FakeAuditClient(
            [
                {
                    "requested_route": "low_confidence_rca",
                    "executed_route": "triage_reasoning",
                    "attempted_routes": ["triage_reasoning"],
                    "provider": "openai",
                    "model": "gpt-4.1-mini",
                    "used_heuristic": False,
                    "fallback_reason": "provider_disabled:xai",
                    "input_tokens": 200,
                    "output_tokens": 120,
                    "estimated_cost_usd": 0.001,
                }
            ]
        ),
        child_agent_run_id=uuid4(),
        routing_config=load_model_routing_config(config_path=CONFIG_PATH),
    )

    assert usage.actual_model_route == AgentModelRoute.QUICKTHINK_LLM
    assert usage.strong_review_requested is True
    assert usage.strong_review_satisfied is False
    assert usage.requested_routes == ["low_confidence_rca"]
    assert usage.executed_routes == ["triage_reasoning"]
    assert usage.fallback_reasons == ["provider_disabled:xai"]


def test_successful_strong_route_proves_deepthink_capability() -> None:
    """Require successful external strong-route output before claiming deepthink."""
    usage = load_llm_usage_summary(
        client=FakeAuditClient(
            [
                {
                    "requested_route": "low_confidence_rca",
                    "executed_route": "low_confidence_rca",
                    "attempted_routes": ["low_confidence_rca"],
                    "provider": "xai",
                    "model": "configured-strong-model",
                    "used_heuristic": False,
                    "fallback_reason": "",
                    "input_tokens": 240,
                    "output_tokens": 160,
                    "estimated_cost_usd": 0.004,
                }
            ]
        ),
        child_agent_run_id=uuid4(),
        routing_config=load_model_routing_config(config_path=CONFIG_PATH),
    )

    assert usage.actual_model_route == AgentModelRoute.DEEPTHINK_LLM
    assert usage.strong_review_requested is True
    assert usage.strong_review_satisfied is True


# --- Testing Risk And Approval Boundaries
def test_incident_task_cannot_downgrade_its_policy_model_route() -> None:
    """Reject a valid no-LLM envelope that attempts to weaken incident policy."""
    _, task = build_policy_task(
        SupervisorRequest(
            intent=SupervisorIntent.TRIAGE_ALERT,
            alert_key="DQ-20260822-A1B2C3",
        )
    )
    weakened_task = rebuild_task(
        task,
        model_route=AgentModelRoute.NO_LLM_FALLBACK,
        model_call_budget=0,
        token_budget=0,
        estimated_cost_budget_usd=0.0,
    )

    with pytest.raises(PermissionError, match="Model route no_llm_fallback is not allowed"):
        enforce_task_capability(weakened_task)


def test_stronger_model_route_cannot_bypass_high_risk_rejection() -> None:
    """Reject a high-risk task even when it already carries the strongest route."""
    _, task = build_policy_task(
        SupervisorRequest(
            intent=SupervisorIntent.TRIAGE_ALERT,
            alert_key="DQ-20260822-A1B2C3",
        )
    )
    high_risk_task = rebuild_task(task, risk_tier=AgentRiskTier.HIGH)

    assert high_risk_task.model_route == AgentModelRoute.DEEPTHINK_LLM

    with pytest.raises(PermissionError, match="Risk tier high exceeds"):
        enforce_task_capability(high_risk_task)


def test_low_confidence_fallback_enters_human_review_state() -> None:
    """Require human review when strong RCA falls back and confidence remains low."""
    _, task = build_policy_task(
        SupervisorRequest(
            intent=SupervisorIntent.TRIAGE_ALERT,
            alert_key="DQ-20260822-A1B2C3",
        )
    )
    result = build_incident_result(
        task,
        confidence=0.58,
        model_route=AgentModelRoute.QUICKTHINK_LLM,
        strong_review_requested=True,
        strong_review_satisfied=False,
    )
    validated_result = enforce_result_contract(task=task, result=result)
    policy           = evaluate_post_handoff_policy(task=task, result=validated_result)

    assert policy.effective_risk_tier == AgentRiskTier.HIGH
    assert policy.strong_review_required is True
    assert policy.strong_review_satisfied is False
    assert policy.human_approval_required is True
    assert policy.disposition == RoutingPolicyDisposition.HUMAN_REVIEW_REQUIRED
    assert RoutingPolicyReason.STRONG_REVIEW_FALLBACK in policy.reason_codes
    assert RoutingPolicyReason.HUMAN_REVIEW_ASSIGNED in policy.reason_codes
    assert terminal_approval_state(validated_result, policy) == AgentApprovalState.PENDING


def test_high_complexity_fallback_enters_human_review_at_high_confidence() -> None:
    """Require review when high-complexity strong reasoning falls back."""
    _, task = build_policy_task(
        SupervisorRequest(
            intent=SupervisorIntent.TRIAGE_ALERT,
            alert_key="DQ-20260822-A1B2C3",
        )
    )
    result = build_incident_result(
        task,
        confidence=0.82,
        model_route=AgentModelRoute.QUICKTHINK_LLM,
        strong_review_requested=True,
        strong_review_satisfied=False,
        complexity_tier="high",
        complexity_score=4,
        complexity_reason_codes=["contradictory_evidence"],
    )
    validated_result = enforce_result_contract(task=task, result=result)
    policy           = evaluate_post_handoff_policy(task=task, result=validated_result)

    assert policy.effective_risk_tier == AgentRiskTier.HIGH
    assert policy.strong_review_required is True
    assert policy.strong_review_satisfied is False
    assert policy.human_approval_required is True
    assert RoutingPolicyReason.HIGH_COMPLEXITY in policy.reason_codes
    assert RoutingPolicyReason.LOW_CONFIDENCE not in policy.reason_codes
    assert RoutingPolicyReason.STRONG_REVIEW_FALLBACK in policy.reason_codes
    assert terminal_approval_state(validated_result, policy) == AgentApprovalState.PENDING


def test_successful_high_complexity_review_can_complete_without_mutation() -> None:
    """Allow evidence reporting after a proven strong review when no action is proposed."""
    _, task = build_policy_task(
        SupervisorRequest(
            intent=SupervisorIntent.TRIAGE_ALERT,
            alert_key="DQ-20260822-A1B2C3",
        )
    )
    result = build_incident_result(
        task,
        confidence=0.82,
        model_route=AgentModelRoute.DEEPTHINK_LLM,
        strong_review_requested=True,
        strong_review_satisfied=True,
        complexity_tier="high",
        complexity_score=4,
        complexity_reason_codes=["wide_lineage_impact", "critical_severity"],
    )
    validated_result = enforce_result_contract(task=task, result=result)
    policy           = evaluate_post_handoff_policy(task=task, result=validated_result)

    assert policy.effective_risk_tier == AgentRiskTier.HIGH
    assert policy.strong_review_required is True
    assert policy.strong_review_satisfied is True
    assert policy.human_approval_required is False
    assert policy.disposition == RoutingPolicyDisposition.ALLOW
    assert RoutingPolicyReason.HIGH_COMPLEXITY in policy.reason_codes
    assert terminal_approval_state(validated_result, policy) == AgentApprovalState.NOT_REQUIRED


def test_successful_strong_review_allows_low_confidence_report_without_action() -> None:
    """Allow a low-confidence report when strong review succeeded and no action is proposed."""
    _, task = build_policy_task(
        SupervisorRequest(
            intent=SupervisorIntent.TRIAGE_ALERT,
            alert_key="DQ-20260822-A1B2C3",
        )
    )
    result = build_incident_result(
        task,
        confidence=0.58,
        model_route=AgentModelRoute.DEEPTHINK_LLM,
        strong_review_requested=True,
        strong_review_satisfied=True,
    )
    validated_result = enforce_result_contract(task=task, result=result)
    policy           = evaluate_post_handoff_policy(task=task, result=validated_result)

    assert policy.effective_risk_tier == AgentRiskTier.HIGH
    assert policy.strong_review_satisfied is True
    assert policy.human_approval_required is False
    assert policy.disposition == RoutingPolicyDisposition.ALLOW
    assert terminal_approval_state(validated_result, policy) == AgentApprovalState.NOT_REQUIRED


def test_approval_gated_action_remains_pending_after_strong_review() -> None:
    """Keep remediation approval pending even when strong model review succeeded."""
    _, task = build_policy_task(
        SupervisorRequest(
            intent=SupervisorIntent.TRIAGE_ALERT,
            alert_key="DQ-20260822-A1B2C3",
        )
    )
    result = build_incident_result(
        task,
        confidence=0.82,
        model_route=AgentModelRoute.QUICKTHINK_LLM,
        strong_review_requested=False,
        strong_review_satisfied=False,
        requires_human_approval=True,
        approval_gated_action_count=1,
    )
    validated_result = enforce_result_contract(task=task, result=result)
    policy           = evaluate_post_handoff_policy(task=task, result=validated_result)

    assert policy.human_approval_required is True
    assert policy.disposition == RoutingPolicyDisposition.HUMAN_REVIEW_REQUIRED
    assert RoutingPolicyReason.APPROVAL_GATED_ACTION in policy.reason_codes
    assert terminal_approval_state(validated_result, policy) == AgentApprovalState.PENDING
