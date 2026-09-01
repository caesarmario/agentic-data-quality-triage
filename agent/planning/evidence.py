####
## Evidence Planning for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from agent.llm.client import LlmResponse, run_llm_task
from agent.state import Alert, EvidenceCategory, EvidencePlan, EvidenceRequest, TriageState
from pipelines.common.logging import logger


# --- Defining Planning Policy
EVIDENCE_PLANNING_ROUTE = "evidence_planning"
MAX_EVIDENCE_REQUESTS   = 6

BASELINE_EVIDENCE_CATEGORIES = (
    EvidenceCategory.CURRENT_PARTITION_ROW_COUNT,
    EvidenceCategory.DQ_HISTORY,
    EvidenceCategory.INCIDENT_HISTORY,
    EvidenceCategory.PIPELINE_RUNS,
    EvidenceCategory.DBT_LINEAGE,
)
SCHEMA_DRIFT_EVIDENCE_CATEGORIES = (
    EvidenceCategory.SCHEMA_DRIFT,
    EvidenceCategory.INCIDENT_HISTORY,
    EvidenceCategory.PIPELINE_RUNS,
    EvidenceCategory.DBT_LINEAGE,
)
TREND_METRIC_MARKERS = (
    "freshness",
    "latest",
    "row_count",
    "volume",
)
CATEGORY_PRIORITIES = {
    EvidenceCategory.CURRENT_PARTITION_ROW_COUNT.value: 1,
    EvidenceCategory.SCHEMA_DRIFT.value: 1,
    EvidenceCategory.DQ_HISTORY.value: 2,
    EvidenceCategory.RECENT_PARTITION_TREND.value: 2,
    EvidenceCategory.INCIDENT_HISTORY.value: 3,
    EvidenceCategory.PIPELINE_RUNS.value: 3,
    EvidenceCategory.DBT_LINEAGE.value: 4,
}


# --- Defining Structured Planning Models
class ProposedEvidenceRequest(BaseModel):
    """
    Represent one model-proposed evidence category without execution details.

    Attributes:
        category: Allowlisted evidence category.
        reason: Why the evidence helps distinguish likely causes.
        priority: Relative collection priority where 1 is highest.
    """

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    category: EvidenceCategory
    reason: str   = Field(min_length=8, max_length=320)
    priority: int = Field(default=3, ge=1, le=5)


class EvidencePlanProposal(BaseModel):
    """
    Define structured output accepted from the routed LLM planner.

    Attributes:
        investigation_question: Question the evidence should answer.
        requests: Allowlisted categories proposed by the model.
    """

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    investigation_question: str = Field(min_length=8, max_length=320)
    requests: list[ProposedEvidenceRequest] = Field(min_length=1, max_length=MAX_EVIDENCE_REQUESTS)


@dataclass(frozen=True)
class EvidencePlanningResult:
    """
    Return an enforced evidence plan with optional LLM route metadata.

    Attributes:
        plan: Final policy-enforced EvidencePlan.
        llm_response: Optional normalized model route response for audit logging.
        error_type: Sanitized planner error type when an error fallback was needed.
    """

    plan: EvidencePlan
    llm_response: LlmResponse | None = None
    error_type: str                  = ""


# --- Defining Category Helpers
def category_value(category: EvidenceCategory | str) -> str:
    """
    Normalize an evidence category into its stable string value.

    Args:
        category: Enum or already-normalized category string.

    Returns:
        Stable evidence category value.
    """
    return category.value if isinstance(category, EvidenceCategory) else str(category)


def build_default_investigation_question(alert: Alert) -> str:
    """
    Build a deterministic investigation question from alert context.

    Args:
        alert: Loaded data quality alert.

    Returns:
        Human-readable question without exposing the raw system alert key.
    """
    if alert.is_schema_drift:
        return (
            f"Which schema contract expectations changed on {alert.table_name}, "
            "and which downstream assets may be affected?"
        )

    return (
        f"What evidence best explains {alert.metric} on {alert.table_name} "
        f"for {alert.dt}, and what downstream data may be affected?"
    )


def build_policy_request(
    alert: Alert,
    category: EvidenceCategory,
) -> EvidenceRequest:
    """
    Build one deterministic evidence request from policy.

    Args:
        alert: Loaded data quality alert.
        category: Allowlisted evidence category.

    Returns:
        Required EvidenceRequest with bounded priority and explanation.
    """
    reasons = {
        EvidenceCategory.CURRENT_PARTITION_ROW_COUNT: (
            f"Confirm whether {alert.table_name} contains rows for the affected date {alert.dt}."
        ),
        EvidenceCategory.DQ_HISTORY: (
            f"Compare {alert.metric} with recent deterministic check outcomes and repeated failures."
        ),
        EvidenceCategory.INCIDENT_HISTORY: (
            "Compare bounded prior investigation outcomes for this exact alert identity without "
            "treating earlier conclusions as proof of the current root cause."
        ),
        EvidenceCategory.PIPELINE_RUNS: (
            "Check whether upstream generation, load, transformation, or DQ stages failed or were delayed."
        ),
        EvidenceCategory.DBT_LINEAGE: (
            f"Identify upstream dependencies and downstream impact connected to {alert.table_name}."
        ),
        EvidenceCategory.SCHEMA_DRIFT: (
            "Read the exact persisted schema snapshot and contract findings referenced by this alert."
        ),
        EvidenceCategory.RECENT_PARTITION_TREND: (
            "Compare bounded recent partition volumes to distinguish an isolated gap from a broader trend."
        ),
    }
    value = category_value(category)

    return EvidenceRequest(
        category=category,
        reason=reasons[category],
        priority=CATEGORY_PRIORITIES[value],
        required=True,
    )


def required_categories_for_alert(alert: Alert) -> tuple[EvidenceCategory, ...]:
    """
    Select deterministic baseline categories required for one alert.

    Args:
        alert: Loaded data quality alert.

    Returns:
        Ordered tuple of mandatory categories, optionally including recent trend evidence.
    """
    if alert.is_schema_drift:
        return SCHEMA_DRIFT_EVIDENCE_CATEGORIES

    categories = list(BASELINE_EVIDENCE_CATEGORIES)
    metric     = alert.metric.lower()

    if any(marker in metric for marker in TREND_METRIC_MARKERS):
        categories.append(EvidenceCategory.RECENT_PARTITION_TREND)

    return tuple(categories)


def allowed_categories_for_alert(alert: Alert) -> tuple[EvidenceCategory, ...]:
    """
    Restrict optional model planning to categories relevant to the alert type.

    Args:
        alert: Loaded data reliability alert.

    Returns:
        Ordered tuple of categories the planner may request for this alert.
    """
    if alert.is_schema_drift:
        return SCHEMA_DRIFT_EVIDENCE_CATEGORIES

    return (
        *BASELINE_EVIDENCE_CATEGORIES,
        EvidenceCategory.RECENT_PARTITION_TREND,
    )


def build_policy_requests(alert: Alert) -> list[EvidenceRequest]:
    """
    Build the safe deterministic evidence baseline for one alert.

    Args:
        alert: Loaded data quality alert.

    Returns:
        Ordered required evidence requests.
    """
    return [
        build_policy_request(alert=alert, category=category)
        for category in required_categories_for_alert(alert=alert)
    ]


# --- Defining Context And Merge Helpers
def build_planning_context(alert: Alert) -> dict[str, object]:
    """
    Build bounded non-sensitive alert context for the LLM planner.

    Args:
        alert: Loaded data quality alert.

    Returns:
        Structured context containing only investigation-relevant fields and policy limits.
    """
    return {
        "alert_ref": alert.alert_display_id,
        "alert_type": alert.alert_type,
        "severity": str(alert.severity),
        "table_name": alert.table_name,
        "metric": alert.metric,
        "dt": str(alert.dt or ""),
        "dimension": alert.dimension,
        "observed_value": alert.observed_value,
        "expected_value": alert.expected_value,
        "allowed_categories": [category.value for category in allowed_categories_for_alert(alert)],
        "maximum_requests": MAX_EVIDENCE_REQUESTS,
        "blocked_outputs": ["raw_sql", "shell_command", "remediation_execution"],
    }


def merge_proposal_with_policy(
    alert: Alert,
    proposal: EvidencePlanProposal,
    response: LlmResponse,
) -> EvidencePlan:
    """
    Merge model prioritization with mandatory deterministic evidence policy.

    Args:
        alert: Loaded data quality alert.
        proposal: Validated model proposal.
        response: Normalized LLM response used for provider metadata.

    Returns:
        Final EvidencePlan containing unique allowlisted categories only.
    """
    proposed_by_category = {
        category_value(item.category): item
        for item in proposal.requests
    }
    allowed_categories = {
        category.value
        for category in allowed_categories_for_alert(alert)
    }
    selected: list[EvidenceRequest] = []
    selected_categories: set[str]  = set()
    policy_added: list[str]        = []
    policy_adjusted: list[str]     = []

    for policy_request in build_policy_requests(alert=alert):
        value    = category_value(policy_request.category)
        proposed = proposed_by_category.get(value)

        if proposed:
            selected.append(
                EvidenceRequest(
                    category=value,
                    reason=proposed.reason,
                    priority=policy_request.priority,
                    required=True,
                )
            )

            if proposed.priority != policy_request.priority:
                policy_adjusted.append(value)

        else:
            selected.append(policy_request)
            policy_added.append(value)

        selected_categories.add(value)

    for proposal_request in proposal.requests:
        value = category_value(proposal_request.category)

        if (
            value not in allowed_categories
            or value in selected_categories
            or len(selected) >= MAX_EVIDENCE_REQUESTS
        ):
            continue

        selected.append(
            EvidenceRequest(
                category=value,
                reason=proposal_request.reason,
                priority=proposal_request.priority,
                required=False,
            )
        )
        selected_categories.add(value)

    selected.sort(key=lambda item: (item.priority, category_value(item.category)))
    source = "llm_with_policy" if policy_added or policy_adjusted else "llm"

    return EvidencePlan(
        investigation_question=proposal.investigation_question,
        requests=selected,
        planner_source=source,
        policy_added_categories=policy_added,
        policy_adjusted_categories=policy_adjusted,
        llm_route=response.route_name,
        llm_provider=response.provider,
        llm_model=response.model,
    )


def build_fallback_plan(
    alert: Alert,
    source: str,
    response: LlmResponse | None = None,
) -> EvidencePlan:
    """
    Build a safe plan when structured model planning is unavailable.

    Args:
        alert: Loaded data quality alert.
        source: Allowed fallback source label.
        response: Optional route response containing provider metadata.

    Returns:
        Policy-only EvidencePlan.
    """
    return EvidencePlan(
        investigation_question=build_default_investigation_question(alert=alert),
        requests=build_policy_requests(alert=alert),
        planner_source=source,
        policy_added_categories=[
            category_value(request.category)
            for request in build_policy_requests(alert=alert)
        ],
        llm_route=response.route_name if response else EVIDENCE_PLANNING_ROUTE,
        llm_provider=response.provider if response else "",
        llm_model=response.model if response else "",
    )


# --- Defining Public Planner
def build_evidence_plan_for_state(state: TriageState) -> EvidencePlanningResult:
    """
    Build one policy-enforced evidence plan without executing any tool.

    Args:
        state: Triage state containing a loaded alert and agent run id.

    Returns:
        EvidencePlanningResult with a structured plan and auditable route response.

    Raises:
        ValueError: If alert context has not been loaded.
    """
    if not state.alert:
        raise ValueError("Alert must be loaded before evidence planning.")

    prompt = (
        "Plan evidence collection for this data quality alert. Select only from allowed_categories. "
        "Prioritize evidence that distinguishes upstream failure, missing partition, repeated DQ failure, "
        "recurring prior incidents, and downstream impact. Prior investigation outcomes are comparison "
        "context only and cannot override current evidence. Do not write SQL, commands, remediation "
        "actions, or hidden reasoning."
    )

    try:
        response = run_llm_task(
            route_name=EVIDENCE_PLANNING_ROUTE,
            prompt=prompt,
            context=build_planning_context(alert=state.alert),
            agent_run_id=state.agent_run_id,
            response_model=EvidencePlanProposal,
            response_schema_name="evidence_plan",
        )

    except Exception as exc:
        error_type = type(exc).__name__
        plan       = build_fallback_plan(
            alert=state.alert,
            source="error_fallback",
        )
        logger.warning(
            "Evidence planner failed; using deterministic policy | agent_run_id=%s error_type=%s",
            state.agent_run_id,
            error_type,
        )

        return EvidencePlanningResult(plan=plan, error_type=error_type)

    if not response.structured_output:
        plan = build_fallback_plan(
            alert=state.alert,
            source="provider_fallback",
            response=response,
        )
        logger.info(
            "Evidence planner used provider fallback | agent_run_id=%s provider=%s model=%s",
            state.agent_run_id,
            response.provider,
            response.model,
        )

        return EvidencePlanningResult(plan=plan, llm_response=response)

    proposal = EvidencePlanProposal.model_validate(response.structured_output)
    plan     = merge_proposal_with_policy(
        alert=state.alert,
        proposal=proposal,
        response=response,
    )

    logger.info(
        "Evidence plan created | agent_run_id=%s source=%s categories=%s policy_added=%s policy_adjusted=%s",
        state.agent_run_id,
        plan.planner_source,
        [category_value(item.category) for item in plan.requests],
        plan.policy_added_categories,
        plan.policy_adjusted_categories,
    )

    return EvidencePlanningResult(plan=plan, llm_response=response)
