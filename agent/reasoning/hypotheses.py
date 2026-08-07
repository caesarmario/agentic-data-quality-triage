####
## Hypothesis Framing for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent.llm.client import LlmResponse, run_llm_task
from agent.state import EvidenceItem, Hypothesis, HypothesisFraming, TriageState
from pipelines.common.logging import logger


# --- Defining Reasoning Policy
HYPOTHESIS_FRAMING_ROUTE = "hypothesis_framing"
MAX_HYPOTHESIS_PROPOSALS = 3
MAX_CONTEXT_EVIDENCE     = 12
MAX_SAMPLE_ROWS          = 3
MAX_SAMPLE_FIELDS        = 12
MAX_CONTEXT_VALUE_LENGTH = 240

AllowedRootCauseCategory = Literal[
    "missing_partition",
    "late_arriving",
    "freshness_gap",
    "missing_segment",
    "unknown_data_issue",
    "threshold_review",
]

UNSAFE_ACTION_MARKERS = (
    "airflow dags trigger",
    "alter table",
    "cmd.exe",
    "curl ",
    "delete from",
    "docker ",
    "drop table",
    "execute now",
    "insert into",
    "kubectl ",
    "powershell",
    "rm -",
    "sudo ",
    "truncate table",
    "update ",
)

SAFE_ACTION_MARKERS = (
    "approval",
    "check",
    "compare",
    "confirm",
    "inspect",
    "propose",
    "request",
    "review",
    "validate",
    "verify",
)


# --- Defining Structured Reasoning Models
class ProposedHypothesis(BaseModel):
    """
    Represent model-authored wording for one deterministic root-cause candidate.

    Attributes:
        root_cause_category: Candidate category selected from the fixed policy vocabulary.
        title: Short operator-friendly title.
        description: Evidence-grounded explanation without hidden reasoning.
        supporting_evidence_ids: Existing evidence IDs that support this candidate.
        opposing_evidence_ids: Existing evidence IDs that weaken this candidate.
        evidence_rationale: Short explanation of how cited evidence affects the candidate.
        recommended_action: Non-executing next action that remains subject to policy and approval.
    """

    model_config = ConfigDict(extra="forbid")

    root_cause_category: AllowedRootCauseCategory
    title: str                         = Field(min_length=8, max_length=160)
    description: str                   = Field(min_length=20, max_length=640)
    supporting_evidence_ids: list[str] = Field(default_factory=list, max_length=MAX_CONTEXT_EVIDENCE)
    opposing_evidence_ids: list[str]   = Field(default_factory=list, max_length=MAX_CONTEXT_EVIDENCE)
    evidence_rationale: str            = Field(min_length=12, max_length=1200)
    recommended_action: str            = Field(min_length=12, max_length=480)


class HypothesisFramingProposal(BaseModel):
    """
    Define the structured hypothesis wording accepted from a routed model.

    Attributes:
        hypotheses: Model-authored framing for one to three deterministic candidates.
    """

    model_config = ConfigDict(extra="forbid")

    hypotheses: list[ProposedHypothesis] = Field(
        min_length=1,
        max_length=MAX_HYPOTHESIS_PROPOSALS,
    )


@dataclass(frozen=True)
class HypothesisFramingResult:
    """
    Return policy-enforced hypotheses and route metadata to the graph node.

    Attributes:
        hypotheses: Final candidate list with deterministic confidence and ranking inputs.
        framing: Audit metadata describing the framing source and policy adjustments.
        llm_response: Optional normalized model response for route-level audit logging.
        error_type: Sanitized exception type when the error fallback path was used.
    """

    hypotheses: list[Hypothesis]
    framing: HypothesisFraming
    llm_response: LlmResponse | None = None
    error_type: str                  = ""


# --- Defining Context Helpers
def truncate_context_value(value: Any) -> Any:
    """
    Bound one evidence value before it is sent to an external model.

    Args:
        value: JSON-like scalar or nested value from a deterministic evidence row.

    Returns:
        Bounded scalar representation suitable for provider context.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value

    text = str(value)

    if len(text) <= MAX_CONTEXT_VALUE_LENGTH:
        return text

    return f"{text[:MAX_CONTEXT_VALUE_LENGTH]}..."


def build_bounded_sample_rows(evidence: EvidenceItem) -> list[dict[str, Any]]:
    """
    Build a small evidence sample without exposing SQL text or unlimited rows.

    Args:
        evidence: Deterministic evidence item collected by a guarded tool.

    Returns:
        At most three rows and twelve fields per row with bounded scalar values.
    """
    samples: list[dict[str, Any]] = []

    for row in evidence.rows[:MAX_SAMPLE_ROWS]:
        samples.append(
            {
                str(key): truncate_context_value(value)
                for key, value in list(row.items())[:MAX_SAMPLE_FIELDS]
            }
        )

    return samples


def build_hypothesis_context(
    state: TriageState,
    baseline_hypotheses: list[Hypothesis],
) -> dict[str, Any]:
    """
    Build bounded alert, candidate, and evidence context for hypothesis framing.

    Args:
        state: Current triage state with alert and deterministic evidence.
        baseline_hypotheses: Policy-generated root-cause candidates.

    Returns:
        Structured context containing no SQL, credentials, commands, or raw alert key.

    Raises:
        ValueError: If alert context is missing.
    """
    if not state.alert:
        raise ValueError("Alert must be loaded before hypothesis framing.")

    return {
        "alert": {
            "alert_ref": state.alert.alert_display_id,
            "alert_type": state.alert.alert_type,
            "severity": str(state.alert.severity),
            "table_name": state.alert.table_name,
            "metric": state.alert.metric,
            "dt": str(state.alert.dt or ""),
            "dimension": state.alert.dimension,
            "observed_value": state.alert.observed_value,
            "expected_value": state.alert.expected_value,
            "threshold_value": state.alert.threshold_value,
        },
        "deterministic_candidates": [
            {
                "root_cause_category": hypothesis.root_cause_category,
                "title": hypothesis.title,
                "description": hypothesis.description,
                "recommended_action": hypothesis.recommended_action,
                "supporting_evidence_ids": hypothesis.supporting_evidence_ids,
            }
            for hypothesis in baseline_hypotheses
        ],
        "evidence": [
            {
                "evidence_id": evidence.evidence_id,
                "evidence_type": str(evidence.evidence_type),
                "tool_name": evidence.tool_name,
                "summary": evidence.summary,
                "row_count": evidence.row_count,
                "sample_rows": build_bounded_sample_rows(evidence=evidence),
            }
            for evidence in state.evidence[:MAX_CONTEXT_EVIDENCE]
        ],
        "policy": {
            "confidence_and_ranking_owned_by_code": True,
            "allowed_categories": [item.root_cause_category for item in baseline_hypotheses],
            "allowed_evidence_ids": [item.evidence_id for item in state.evidence[:MAX_CONTEXT_EVIDENCE]],
            "blocked_outputs": ["confidence_score", "raw_sql", "shell_command", "remediation_execution"],
        },
    }


# --- Defining Policy Helpers
def is_safe_recommended_action(value: str) -> bool:
    """
    Check whether a model-authored action remains explanatory and non-executing.

    Args:
        value: Proposed recommended action text.

    Returns:
        True when no executable marker exists and the text signals review or approval.
    """
    lowered = value.lower()

    if any(marker in lowered for marker in UNSAFE_ACTION_MARKERS):
        return False

    return any(marker in lowered for marker in SAFE_ACTION_MARKERS)


def build_fallback_result(
    baseline_hypotheses: list[Hypothesis],
    source: Literal["provider_fallback", "error_fallback"],
    response: LlmResponse | None = None,
    error_type: str = "",
) -> HypothesisFramingResult:
    """
    Build a deterministic fallback while preserving provider observability.

    Args:
        baseline_hypotheses: Policy-generated candidates.
        source: Fallback source label.
        response: Optional normalized provider response.
        error_type: Sanitized exception type for error fallback.

    Returns:
        HypothesisFramingResult containing unchanged deterministic hypotheses.
    """
    hypotheses = [
        item.model_copy(
            update={
                "framing_source": "deterministic",
                "framing_notes": [source],
            }
        )
        for item in baseline_hypotheses
    ]
    framing = HypothesisFraming(
        source=source,
        requested_route=HYPOTHESIS_FRAMING_ROUTE,
        provider=response.provider if response else "",
        model=response.model if response else "",
        policy_adjustments=[error_type] if error_type else [],
    )

    return HypothesisFramingResult(
        hypotheses=hypotheses,
        framing=framing,
        llm_response=response,
        error_type=error_type,
    )


def merge_proposal_with_policy(
    state: TriageState,
    baseline_hypotheses: list[Hypothesis],
    proposal: HypothesisFramingProposal,
    response: LlmResponse,
) -> HypothesisFramingResult:
    """
    Merge model-authored wording into deterministic candidates without changing scores.

    Args:
        state: Current state containing collected evidence identifiers.
        baseline_hypotheses: Policy-owned candidates and confidence scores.
        proposal: Validated model proposal.
        response: Normalized provider response for audit metadata.

    Returns:
        Policy-enforced hypotheses with valid evidence references only.
    """
    baseline_by_category = {
        item.root_cause_category: item
        for item in baseline_hypotheses
    }
    allowed_evidence_ids = {
        item.evidence_id
        for item in state.evidence[:MAX_CONTEXT_EVIDENCE]
    }
    accepted: dict[str, Hypothesis] = {}
    accepted_categories: list[str] = []
    adjustments: list[str]         = []

    for proposed in proposal.hypotheses:
        category = str(proposed.root_cause_category)
        baseline = baseline_by_category.get(category)

        if not baseline:
            adjustments.append(f"rejected_unknown_category:{category}")
            continue

        if category in accepted:
            adjustments.append(f"rejected_duplicate_category:{category}")
            continue

        valid_supporting = [
            evidence_id
            for evidence_id in proposed.supporting_evidence_ids
            if evidence_id in allowed_evidence_ids
        ]
        valid_opposing = [
            evidence_id
            for evidence_id in proposed.opposing_evidence_ids
            if evidence_id in allowed_evidence_ids
        ]
        notes: list[str] = [proposed.evidence_rationale]

        if len(valid_supporting) != len(proposed.supporting_evidence_ids):
            adjustments.append(f"filtered_unknown_supporting_evidence:{category}")

        if len(valid_opposing) != len(proposed.opposing_evidence_ids):
            adjustments.append(f"filtered_unknown_opposing_evidence:{category}")

        if not valid_supporting:
            valid_supporting = [
                evidence_id
                for evidence_id in baseline.supporting_evidence_ids
                if evidence_id in allowed_evidence_ids
            ]
            adjustments.append(f"restored_policy_supporting_evidence:{category}")

        recommended_action = proposed.recommended_action

        if not is_safe_recommended_action(recommended_action):
            recommended_action = baseline.recommended_action
            adjustments.append(f"replaced_unsafe_action:{category}")

        accepted[category] = baseline.model_copy(
            update={
                "title": proposed.title,
                "description": proposed.description,
                "supporting_evidence_ids": valid_supporting,
                "opposing_evidence_ids": valid_opposing,
                "recommended_action": recommended_action,
                "framing_source": "llm",
                "framing_notes": notes,
            }
        )
        accepted_categories.append(category)

    hypotheses: list[Hypothesis] = []

    for baseline in baseline_hypotheses:
        framed = accepted.get(baseline.root_cause_category)

        if framed:
            hypotheses.append(framed)
            continue

        hypotheses.append(
            baseline.model_copy(
                update={
                    "framing_source": "deterministic",
                    "framing_notes": ["model_omitted_candidate"],
                }
            )
        )
        adjustments.append(f"restored_omitted_candidate:{baseline.root_cause_category}")

    source = "llm_with_policy" if adjustments else "llm"
    framing = HypothesisFraming(
        source=source,
        requested_route=response.route_name,
        provider=response.provider,
        model=response.model,
        accepted_categories=accepted_categories,
        policy_adjustments=adjustments,
    )

    return HypothesisFramingResult(
        hypotheses=hypotheses,
        framing=framing,
        llm_response=response,
    )


# --- Defining Public Reasoning Helper
def frame_hypotheses_for_state(
    state: TriageState,
    baseline_hypotheses: list[Hypothesis],
) -> HypothesisFramingResult:
    """
    Ask a routed model to frame deterministic candidates, then enforce policy.

    Args:
        state: Current triage state with a loaded alert and deterministic evidence.
        baseline_hypotheses: Policy-generated candidates with fixed confidence values.

    Returns:
        HypothesisFramingResult with safe wording or deterministic fallback.

    Raises:
        ValueError: If no alert or baseline hypothesis is available.
    """
    if not state.alert:
        raise ValueError("Alert must be loaded before hypothesis framing.")

    if not baseline_hypotheses:
        raise ValueError("At least one deterministic hypothesis is required before framing.")

    prompt = (
        "Rewrite the provided deterministic root-cause candidates into concise, human-readable hypotheses. "
        "Use only the listed candidate categories and evidence IDs. Explain what the evidence supports or "
        "contradicts. Do not provide confidence scores, SQL, commands, hidden reasoning, or direct execution. "
        "Keep each evidence rationale concise. Recommended actions must be review-oriented or approval-gated."
    )

    try:
        response = run_llm_task(
            route_name=HYPOTHESIS_FRAMING_ROUTE,
            prompt=prompt,
            context=build_hypothesis_context(
                state=state,
                baseline_hypotheses=baseline_hypotheses,
            ),
            agent_run_id=state.agent_run_id,
            response_model=HypothesisFramingProposal,
            response_schema_name="hypothesis_framing",
        )

    except Exception as exc:
        error_type = type(exc).__name__

        logger.warning(
            "Hypothesis framing failed; using deterministic candidates | agent_run_id=%s error_type=%s",
            state.agent_run_id,
            error_type,
        )

        return build_fallback_result(
            baseline_hypotheses=baseline_hypotheses,
            source="error_fallback",
            error_type=error_type,
        )

    if not response.structured_output:
        logger.info(
            "Hypothesis framing used provider fallback | agent_run_id=%s provider=%s model=%s",
            state.agent_run_id,
            response.provider,
            response.model,
        )

        return build_fallback_result(
            baseline_hypotheses=baseline_hypotheses,
            source="provider_fallback",
            response=response,
        )

    proposal = HypothesisFramingProposal.model_validate(response.structured_output)
    result   = merge_proposal_with_policy(
        state=state,
        baseline_hypotheses=baseline_hypotheses,
        proposal=proposal,
        response=response,
    )

    logger.info(
        "Hypothesis framing completed | agent_run_id=%s source=%s accepted=%s policy_adjustments=%s",
        state.agent_run_id,
        result.framing.source,
        result.framing.accepted_categories,
        result.framing.policy_adjustments,
    )

    return result
