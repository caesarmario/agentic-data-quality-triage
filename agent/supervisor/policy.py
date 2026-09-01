####
## Supervisor Routing Policy for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Resolve model capability, effective risk, strong review, and approval policy."""

# --- Importing Libraries
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent.specialists.contracts import (
    AgentModelRoute,
    AgentResultEnvelope,
    AgentRiskTier,
    AgentTaskEnvelope,
    AgentTaskStatus,
    MODEL_ROUTE_ORDER,
)
from agent.state import (
    EvidenceType,
    IncidentComplexityAssessment,
    IncidentComplexityTier,
    TriageState,
)
from pipelines.common.logging import logger


# --- Defining Constants
NORMAL_REPORT_REASONING_ROUTE = "triage_reasoning"
STRONG_REPORT_REASONING_ROUTE = "low_confidence_rca"

RISK_TIER_ORDER = {
    AgentRiskTier.LOW: 1,
    AgentRiskTier.MEDIUM: 2,
    AgentRiskTier.HIGH: 3,
    AgentRiskTier.CRITICAL: 4,
}

HIGH_RISK_LEVELS = {
    "high": AgentRiskTier.HIGH,
    "critical": AgentRiskTier.CRITICAL,
}

DETERMINISTIC_COMPLEXITY_EVIDENCE_TYPES = {
    EvidenceType.SQL_RESULT.value,
    EvidenceType.DQ_HISTORY.value,
    EvidenceType.INCIDENT_HISTORY.value,
    EvidenceType.LINEAGE.value,
    EvidenceType.PIPELINE_RUN.value,
    EvidenceType.SCHEMA_DRIFT.value,
}

BROAD_EVIDENCE_TYPE_THRESHOLD = 5
COMPETING_HYPOTHESIS_GAP       = 0.12
WIDE_LINEAGE_ASSET_THRESHOLD  = 4
MODERATE_COMPLEXITY_THRESHOLD  = 2
HIGH_COMPLEXITY_THRESHOLD      = 4

SCHEMA_COMPLEXITY_FINDING_STATUSES = {
    "warn",
    "fail",
}

LINEAGE_REFERENCE_KEYS = (
    "parents",
    "children",
    "upstream",
    "downstream",
    "upstream_assets",
    "downstream_assets",
    "direct_upstream",
    "direct_downstream",
)


# --- Defining Enumerations
class RoutingPolicyStage(str, Enum):
    """Represent when a deterministic routing-policy decision was made."""

    PRE_HANDOFF   = "pre_handoff"
    POST_EVIDENCE = "post_evidence"


class RoutingPolicyDisposition(str, Enum):
    """Represent whether a result may complete or needs human review."""

    ALLOW                 = "allow"
    HUMAN_REVIEW_REQUIRED = "human_review_required"
    BLOCK                 = "block"


class RoutingPolicyReason(str, Enum):
    """Provide stable reason codes for audit, tests, and operator explanations."""

    DETERMINISTIC_TASK             = "deterministic_task"
    LLM_CAPABILITY_AUTHORIZED      = "llm_capability_authorized"
    STANDARD_CONFIDENCE            = "standard_confidence"
    LOW_CONFIDENCE                 = "low_confidence"
    HIGH_COMPLEXITY                = "high_complexity"
    STRONG_REVIEW_REQUESTED        = "strong_review_requested"
    STRONG_REVIEW_SATISFIED        = "strong_review_satisfied"
    STRONG_REVIEW_FALLBACK         = "strong_review_fallback"
    APPROVAL_GATED_ACTION          = "approval_gated_action"
    ELEVATED_QUERY_RISK            = "elevated_query_risk"
    BREAKING_SCHEMA_CHANGE         = "breaking_schema_change"
    ELEVATED_SCHEMA_IMPACT         = "elevated_schema_impact"
    HUMAN_REVIEW_ASSIGNED          = "human_review_assigned"
    SPECIALIST_TERMINAL_FAILURE    = "specialist_terminal_failure"


class IncidentComplexityReason(str, Enum):
    """Provide stable reasons for deterministic incident-complexity scoring."""

    CRITICAL_SEVERITY          = "critical_severity"
    BROAD_EVIDENCE             = "broad_evidence"
    COMPETING_HYPOTHESES       = "competing_hypotheses"
    CONTRADICTORY_EVIDENCE     = "contradictory_evidence"
    WIDE_LINEAGE_IMPACT        = "wide_lineage_impact"
    SCHEMA_DRIFT_CONTEXT       = "schema_drift_context"
    UNRESOLVED_TOOL_ERRORS     = "unresolved_tool_errors"


# --- Defining Policy Models
class ReportReasoningPolicy(BaseModel):
    """
    Select one provider route for the final evidence-backed report narrative.

    Attributes:
        provider_route: Route key from model_routing.yml.
        capability_route: Provider-agnostic capability represented by the route.
        strong_review_required: Whether confidence or complexity requires strong reasoning.
        reason_code: Stable policy reason for the selection.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_route: str
    capability_route: AgentModelRoute
    strong_review_required: bool
    reason_code: RoutingPolicyReason


class SupervisorRoutingPolicyDecision(BaseModel):
    """
    Record one deterministic supervisor routing and terminal safety decision.

    Attributes:
        stage: Pre-handoff or post-evidence evaluation stage.
        effective_risk_tier: Highest risk derived from trusted task and result facts.
        authorized_model_route: Maximum capability granted by task policy.
        actual_model_route: Highest capability proven by audited successful execution.
        requested_provider_routes: Provider route names requested by the child workflow.
        executed_provider_routes: Provider route names that returned usable output.
        strong_review_required: Whether policy required strong reasoning.
        strong_review_satisfied: Whether a strong route actually returned external output.
        human_approval_required: Whether the result must wait for operator review.
        disposition: Allow, human review required, or blocked.
        reason_codes: Stable deterministic reasons supporting the decision.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: RoutingPolicyStage
    effective_risk_tier: AgentRiskTier
    authorized_model_route: AgentModelRoute
    actual_model_route: AgentModelRoute             = AgentModelRoute.NO_LLM_FALLBACK
    requested_provider_routes: tuple[str, ...]      = ()
    executed_provider_routes: tuple[str, ...]       = ()
    strong_review_required: bool                    = False
    strong_review_satisfied: bool                   = False
    human_approval_required: bool                   = False
    disposition: RoutingPolicyDisposition           = RoutingPolicyDisposition.ALLOW
    reason_codes: tuple[RoutingPolicyReason, ...]   = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_policy_invariants(self) -> "SupervisorRoutingPolicyDecision":
        """
        Reject policy decisions that overstate model capability or safety.

        Returns:
            Current policy decision when every invariant is satisfied.

        Raises:
            ValueError: If route, strong-review, risk, or disposition claims conflict.
        """
        if MODEL_ROUTE_ORDER[self.actual_model_route] > MODEL_ROUTE_ORDER[self.authorized_model_route]:
            raise ValueError("Actual model route exceeds the authorized task capability.")

        if self.strong_review_satisfied and self.actual_model_route != AgentModelRoute.DEEPTHINK_LLM:
            raise ValueError("Strong review requires proven deepthinkllm execution.")

        if self.human_approval_required:
            if self.disposition != RoutingPolicyDisposition.HUMAN_REVIEW_REQUIRED:
                raise ValueError("Human approval must use human_review_required disposition.")

        elif self.disposition == RoutingPolicyDisposition.HUMAN_REVIEW_REQUIRED:
            raise ValueError("Human-review disposition requires human approval.")

        elevated_risk = RISK_TIER_ORDER[self.effective_risk_tier] >= RISK_TIER_ORDER[AgentRiskTier.HIGH]

        if (
            elevated_risk
            and not self.strong_review_satisfied
            and not self.human_approval_required
            and self.disposition != RoutingPolicyDisposition.BLOCK
        ):
            raise ValueError(
                "High-risk results require proven strong review, human approval, or blocking."
            )

        if len(set(self.reason_codes)) != len(self.reason_codes):
            raise ValueError("Routing policy reason codes must remain unique.")

        return self


# --- Defining Policy Helpers
def highest_risk(current: AgentRiskTier, candidate: AgentRiskTier) -> AgentRiskTier:
    """
    Return the higher of two deterministic risk tiers.

    Args:
        current: Existing effective risk tier.
        candidate: New risk derived from trusted evidence.

    Returns:
        Higher risk tier according to the fixed policy order.
    """
    if RISK_TIER_ORDER[candidate] > RISK_TIER_ORDER[current]:
        return candidate

    return current


def stable_string_tuple(value: Any) -> tuple[str, ...]:
    """
    Normalize a JSON-like list into unique bounded route-name strings.

    Args:
        value: Raw structured-output value.

    Returns:
        Unique non-empty strings in original order.
    """
    if not isinstance(value, (list, tuple)):
        return ()

    normalized: list[str] = []

    for item in value:
        text = str(item).strip()

        if text and text not in normalized:
            normalized.append(text[:120])

    return tuple(normalized)


def normalized_enum_text(value: Any) -> str:
    """
    Convert an enum-like value into a normalized lowercase string.

    Args:
        value: Enum, string, or other JSON-like value.

    Returns:
        Lowercase text suitable for deterministic policy comparisons.
    """
    raw_value = value.value if isinstance(value, Enum) else value

    return str(raw_value or "").strip().lower()


def lineage_reference(value: Any) -> str:
    """
    Resolve one bounded lineage asset identifier from a row value.

    Args:
        value: Lineage string or mapping returned by the dbt lineage tool.

    Returns:
        Stable identifier when one is available, otherwise an empty string.
    """
    if isinstance(value, str):
        return value.strip()[:500]

    if not isinstance(value, dict):
        return ""

    for key in ("unique_id", "qualified_name", "name", "relation_name", "node_id"):
        candidate = str(value.get(key) or "").strip()

        if candidate:
            return candidate[:500]

    return ""


def count_lineage_assets(state: TriageState) -> int:
    """
    Count unique directly related lineage assets from deterministic evidence.

    Args:
        state: Current triage state after deterministic evidence collection.

    Returns:
        Number of unique parent, child, upstream, or downstream asset references.
    """
    references: set[str] = set()

    for evidence in state.evidence:
        if normalized_enum_text(evidence.evidence_type) != EvidenceType.LINEAGE.value:
            continue

        for row in evidence.rows:
            if not isinstance(row, dict):
                continue

            for key in LINEAGE_REFERENCE_KEYS:
                raw_references = row.get(key, [])

                if not isinstance(raw_references, (list, tuple, set)):
                    raw_references = [raw_references]

                for raw_reference in raw_references:
                    reference = lineage_reference(raw_reference)

                    if reference:
                        references.add(reference)

    return len(references)


def count_schema_findings(state: TriageState) -> int:
    """
    Count persisted warning or failure rows from deterministic schema evidence.

    Args:
        state: Current triage state after deterministic evidence collection.

    Returns:
        Number of visible schema findings with a warning or failure status.

    Notes:
        The count intentionally ignores EvidenceItem.row_count. That field may describe
        clean snapshot rows or change meaning in a future collector, while the persisted
        finding status is the source-of-truth signal used by complexity routing.
    """
    finding_count = 0

    for evidence in state.evidence:
        if normalized_enum_text(evidence.evidence_type) != EvidenceType.SCHEMA_DRIFT.value:
            continue

        for row in evidence.rows:
            if not isinstance(row, dict):
                continue

            status = normalized_enum_text(row.get("status"))

            if status in SCHEMA_COMPLEXITY_FINDING_STATUSES:
                finding_count += 1

    return finding_count


def assess_incident_complexity(state: TriageState) -> IncidentComplexityAssessment:
    """
    Score incident complexity from trusted state facts without using an LLM.

    Args:
        state: Triage state containing deterministic evidence and ranked hypotheses.

    Returns:
        Typed complexity assessment used by report routing and persisted artifacts.

    Notes:
        Severity contributes context but never selects a strong model by itself. High
        complexity requires cross-signal evidence such as competing hypotheses,
        contradictions, broad lineage impact, schema findings, or unresolved errors.
    """
    deterministic_evidence_types = tuple(
        sorted(
            {
                normalized_enum_text(item.evidence_type)
                for item in state.evidence
                if normalized_enum_text(item.evidence_type)
                in DETERMINISTIC_COMPLEXITY_EVIDENCE_TYPES
            }
        )
    )
    ranked_confidences = sorted(
        (float(item.confidence) for item in state.hypotheses),
        reverse=True,
    )
    top_hypothesis_gap = (
        round(max(0.0, ranked_confidences[0] - ranked_confidences[1]), 4)
        if len(ranked_confidences) >= 2
        else None
    )
    contradiction_count = sum(
        len(item.contradicts_hypotheses)
        for item in state.evidence
    ) + sum(
        len(item.opposing_evidence_ids)
        for item in state.hypotheses
    )
    lineage_asset_count = count_lineage_assets(state)
    schema_finding_count = count_schema_findings(state)
    unresolved_error_count = len(
        [error for error in state.errors if str(error).strip()]
    )
    severity = normalized_enum_text(state.alert.severity) if state.alert else ""
    score    = 0
    reasons: list[IncidentComplexityReason] = []

    if severity == "critical":
        score += 1
        reasons.append(IncidentComplexityReason.CRITICAL_SEVERITY)

    if len(deterministic_evidence_types) >= BROAD_EVIDENCE_TYPE_THRESHOLD:
        score += 1
        reasons.append(IncidentComplexityReason.BROAD_EVIDENCE)

    if (
        top_hypothesis_gap is not None
        and top_hypothesis_gap <= COMPETING_HYPOTHESIS_GAP
    ):
        score += 3
        reasons.append(IncidentComplexityReason.COMPETING_HYPOTHESES)

    if contradiction_count > 0:
        score += 4
        reasons.append(IncidentComplexityReason.CONTRADICTORY_EVIDENCE)

    if lineage_asset_count >= WIDE_LINEAGE_ASSET_THRESHOLD:
        score += 3
        reasons.append(IncidentComplexityReason.WIDE_LINEAGE_IMPACT)

    if schema_finding_count > 0:
        score += 3
        reasons.append(IncidentComplexityReason.SCHEMA_DRIFT_CONTEXT)

    if unresolved_error_count > 0:
        score += 3
        reasons.append(IncidentComplexityReason.UNRESOLVED_TOOL_ERRORS)

    if score >= HIGH_COMPLEXITY_THRESHOLD:
        tier = IncidentComplexityTier.HIGH

    elif score >= MODERATE_COMPLEXITY_THRESHOLD:
        tier = IncidentComplexityTier.MODERATE

    else:
        tier = IncidentComplexityTier.LOW

    assessment = IncidentComplexityAssessment(
        tier=tier,
        score=score,
        strong_reasoning_required=tier == IncidentComplexityTier.HIGH,
        reason_codes=tuple(reason.value for reason in reasons),
        deterministic_evidence_types=deterministic_evidence_types,
        hypothesis_count=len(state.hypotheses),
        top_hypothesis_gap=top_hypothesis_gap,
        contradiction_count=contradiction_count,
        lineage_asset_count=lineage_asset_count,
        schema_finding_count=schema_finding_count,
        unresolved_error_count=unresolved_error_count,
    )

    logger.info(
        "Assessed incident complexity | agent_run_id=%s tier=%s score=%d strong_required=%s reasons=%s",
        state.agent_run_id,
        assessment.tier,
        assessment.score,
        assessment.strong_reasoning_required,
        list(assessment.reason_codes),
    )

    return assessment


def resolve_report_reasoning_policy(
    confidence: float,
    confidence_threshold: float,
    complexity_assessment: IncidentComplexityAssessment | None = None,
) -> ReportReasoningPolicy:
    """
    Select normal or strong report reasoning from deterministic confidence facts.

    Args:
        confidence: Current top-hypothesis confidence.
        confidence_threshold: Required confidence after bounded evidence collection.
        complexity_assessment: Optional deterministic incident-complexity facts.

    Returns:
        Immutable report reasoning policy decision.
    """
    low_confidence  = confidence < confidence_threshold
    high_complexity = bool(
        complexity_assessment
        and complexity_assessment.strong_reasoning_required
    )

    if low_confidence or high_complexity:
        decision = ReportReasoningPolicy(
            provider_route=STRONG_REPORT_REASONING_ROUTE,
            capability_route=AgentModelRoute.DEEPTHINK_LLM,
            strong_review_required=True,
            reason_code=(
                RoutingPolicyReason.LOW_CONFIDENCE
                if low_confidence
                else RoutingPolicyReason.HIGH_COMPLEXITY
            ),
        )

    else:
        decision = ReportReasoningPolicy(
            provider_route=NORMAL_REPORT_REASONING_ROUTE,
            capability_route=AgentModelRoute.QUICKTHINK_LLM,
            strong_review_required=False,
            reason_code=RoutingPolicyReason.STANDARD_CONFIDENCE,
        )

    logger.info(
        "Resolved report reasoning policy | confidence=%.4f threshold=%.4f complexity=%s complexity_score=%s provider_route=%s capability=%s reason=%s",
        confidence,
        confidence_threshold,
        complexity_assessment.tier if complexity_assessment else "not_assessed",
        complexity_assessment.score if complexity_assessment else "not_assessed",
        decision.provider_route,
        decision.capability_route.value,
        decision.reason_code.value,
    )

    return decision


def evaluate_pre_handoff_policy(task: AgentTaskEnvelope) -> SupervisorRoutingPolicyDecision:
    """
    Record the capability ceiling selected before a specialist starts.

    Args:
        task: Policy-built specialist task.

    Returns:
        Immutable pre-handoff policy decision.
    """
    reason = (
        RoutingPolicyReason.DETERMINISTIC_TASK
        if task.model_route == AgentModelRoute.NO_LLM_FALLBACK
        else RoutingPolicyReason.LLM_CAPABILITY_AUTHORIZED
    )
    decision = SupervisorRoutingPolicyDecision(
        stage=RoutingPolicyStage.PRE_HANDOFF,
        effective_risk_tier=task.risk_tier,
        authorized_model_route=task.model_route,
        actual_model_route=AgentModelRoute.NO_LLM_FALLBACK,
        reason_codes=(reason,),
    )

    logger.info(
        "Evaluated pre-handoff routing policy | task_id=%s risk=%s authorized_route=%s reason=%s",
        task.task_id,
        decision.effective_risk_tier.value,
        decision.authorized_model_route.value,
        reason.value,
    )

    return decision


def evaluate_post_handoff_policy(
    task: AgentTaskEnvelope,
    result: AgentResultEnvelope,
) -> SupervisorRoutingPolicyDecision:
    """
    Derive effective risk, actual capability, strong review, and approval requirements.

    Args:
        task: Authorized source handoff.
        result: Contract-validated terminal specialist result.

    Returns:
        Immutable terminal policy decision based only on typed result facts.
    """
    structured      = result.structured_output
    requested_routes = stable_string_tuple(structured.get("requested_model_routes"))
    executed_routes  = stable_string_tuple(structured.get("executed_model_routes"))
    strong_requested = bool(structured.get("strong_review_requested", False))
    strong_satisfied = bool(structured.get("strong_review_satisfied", False))
    effective_risk   = task.risk_tier
    reasons: list[RoutingPolicyReason] = []

    if result.status in {AgentTaskStatus.FAILED, AgentTaskStatus.BLOCKED}:
        return SupervisorRoutingPolicyDecision(
            stage=RoutingPolicyStage.POST_EVIDENCE,
            effective_risk_tier=effective_risk,
            authorized_model_route=task.model_route,
            actual_model_route=result.model_route,
            requested_provider_routes=requested_routes,
            executed_provider_routes=executed_routes,
            disposition=RoutingPolicyDisposition.BLOCK,
            reason_codes=(RoutingPolicyReason.SPECIALIST_TERMINAL_FAILURE,),
        )

    confidence_threshold = float(task.input_payload.get("confidence_threshold", 0.70) or 0.70)
    low_confidence = task.task_type == "triage_alert" and result.confidence < confidence_threshold
    complexity_tier = str(structured.get("complexity_tier", "")).strip().lower()
    high_complexity = task.task_type == "triage_alert" and complexity_tier == "high"

    if low_confidence:
        effective_risk = highest_risk(effective_risk, AgentRiskTier.HIGH)
        strong_requested = True
        reasons.append(RoutingPolicyReason.LOW_CONFIDENCE)

    else:
        reasons.append(RoutingPolicyReason.STANDARD_CONFIDENCE)

    if high_complexity:
        effective_risk = highest_risk(effective_risk, AgentRiskTier.HIGH)
        strong_requested = True
        reasons.append(RoutingPolicyReason.HIGH_COMPLEXITY)

    query_risk = str(structured.get("query_risk_level", "")).strip().lower()

    if query_risk in HIGH_RISK_LEVELS:
        effective_risk = highest_risk(effective_risk, HIGH_RISK_LEVELS[query_risk])
        reasons.append(RoutingPolicyReason.ELEVATED_QUERY_RISK)

    schema_assessment = str(structured.get("assessment", "")).strip().lower()
    schema_impact     = str(structured.get("impact_level", "")).strip().lower()

    if schema_assessment and schema_assessment not in {"compatible", "no_change"}:
        effective_risk = highest_risk(effective_risk, AgentRiskTier.HIGH)
        reasons.append(RoutingPolicyReason.BREAKING_SCHEMA_CHANGE)

    if schema_impact in HIGH_RISK_LEVELS:
        effective_risk = highest_risk(effective_risk, HIGH_RISK_LEVELS[schema_impact])
        reasons.append(RoutingPolicyReason.ELEVATED_SCHEMA_IMPACT)

    approval_action_count = int(structured.get("approval_gated_action_count", 0) or 0)

    if result.requires_human_approval or approval_action_count > 0:
        effective_risk = highest_risk(effective_risk, AgentRiskTier.HIGH)
        reasons.append(RoutingPolicyReason.APPROVAL_GATED_ACTION)

    if strong_requested:
        reasons.append(RoutingPolicyReason.STRONG_REVIEW_REQUESTED)

        if strong_satisfied:
            reasons.append(RoutingPolicyReason.STRONG_REVIEW_SATISFIED)

        else:
            reasons.append(RoutingPolicyReason.STRONG_REVIEW_FALLBACK)

    elevated_risk = RISK_TIER_ORDER[effective_risk] >= RISK_TIER_ORDER[AgentRiskTier.HIGH]
    human_approval_required = (
        result.requires_human_approval
        or approval_action_count > 0
        or (elevated_risk and not strong_satisfied)
    )
    disposition = (
        RoutingPolicyDisposition.HUMAN_REVIEW_REQUIRED
        if human_approval_required
        else RoutingPolicyDisposition.ALLOW
    )

    if human_approval_required:
        reasons.append(RoutingPolicyReason.HUMAN_REVIEW_ASSIGNED)

    decision = SupervisorRoutingPolicyDecision(
        stage=RoutingPolicyStage.POST_EVIDENCE,
        effective_risk_tier=effective_risk,
        authorized_model_route=task.model_route,
        actual_model_route=result.model_route,
        requested_provider_routes=requested_routes,
        executed_provider_routes=executed_routes,
        strong_review_required=low_confidence or high_complexity,
        strong_review_satisfied=strong_satisfied,
        human_approval_required=human_approval_required,
        disposition=disposition,
        reason_codes=tuple(dict.fromkeys(reasons)),
    )

    logger.info(
        "Evaluated post-handoff routing policy | task_id=%s risk=%s actual_route=%s strong_required=%s strong_satisfied=%s approval_required=%s disposition=%s reasons=%s",
        task.task_id,
        decision.effective_risk_tier.value,
        decision.actual_model_route.value,
        decision.strong_review_required,
        decision.strong_review_satisfied,
        decision.human_approval_required,
        decision.disposition.value,
        [reason.value for reason in decision.reason_codes],
    )

    return decision
