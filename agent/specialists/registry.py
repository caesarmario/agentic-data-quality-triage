####
## Specialist Capability Registry for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Least-privilege capability and routing policy for bounded specialist agents."""

# --- Importing Libraries
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent.specialists.contracts import (
    AgentModelRoute,
    AgentResultEnvelope,
    AgentRiskTier,
    AgentTaskEnvelope,
    AgentTaskStatus,
    MODEL_ROUTE_ORDER,
)
from pipelines.common.logging import logger


# --- Defining Constants
INCIDENT_TRIAGE_SPECIALIST_NAME = "incident_triage_agent"

INCIDENT_TRIAGE_TASK_TYPES = (
    "triage_alert",
)

INCIDENT_TRIAGE_ALLOWED_TOOLS = (
    "alerts",
    "clickhouse_sql",
    "dq_history",
    "incident_history",
    "pipeline_runs",
    "dbt_lineage",
    "schema_drift",
    "llm_router",
    "s3_artifacts",
    "alert_lifecycle",
    "agent_audit_log",
)

INCIDENT_TRIAGE_REQUIRED_TOOLS = {
    "triage_alert": INCIDENT_TRIAGE_ALLOWED_TOOLS,
}

METADATA_LINEAGE_SPECIALIST_NAME = "metadata_lineage_agent"

METADATA_LINEAGE_TASK_TYPES = (
    "asset_context",
    "blast_radius",
    "trusted_asset_search",
)

METADATA_LINEAGE_ALLOWED_TOOLS = (
    "metadata_catalog",
    "dbt_lineage",
    "dbt_blast_radius",
    "agent_audit_log",
)

METADATA_LINEAGE_REQUIRED_TOOLS = {
    "asset_context": (
        "metadata_catalog",
        "dbt_lineage",
        "dbt_blast_radius",
        "agent_audit_log",
    ),
    "blast_radius": (
        "metadata_catalog",
        "dbt_blast_radius",
        "agent_audit_log",
    ),
    "trusted_asset_search": (
        "metadata_catalog",
        "agent_audit_log",
    ),
}

SQL_REVIEW_SPECIALIST_NAME = "sql_safety_review_agent"

SQL_REVIEW_TASK_TYPES = (
    "review_sql",
)

SQL_REVIEW_ALLOWED_TOOLS = (
    "sql_policy_review",
    "metadata_catalog",
    "warehouse_statistics",
    "agent_audit_log",
)

SQL_REVIEW_REQUIRED_TOOLS = {
    "review_sql": SQL_REVIEW_ALLOWED_TOOLS,
}

SCHEMA_DRIFT_SPECIALIST_NAME = "schema_drift_agent"

SCHEMA_DRIFT_TASK_TYPES = (
    "assess_schema_drift",
)

SCHEMA_DRIFT_ALLOWED_TOOLS = (
    "schema_drift",
    "metadata_catalog",
    "dbt_blast_radius",
    "agent_audit_log",
)

SCHEMA_DRIFT_REQUIRED_TOOLS = {
    "assess_schema_drift": SCHEMA_DRIFT_ALLOWED_TOOLS,
}

REQUIRED_TOOLS_BY_SPECIALIST = {
    INCIDENT_TRIAGE_SPECIALIST_NAME: INCIDENT_TRIAGE_REQUIRED_TOOLS,
    METADATA_LINEAGE_SPECIALIST_NAME: METADATA_LINEAGE_REQUIRED_TOOLS,
    SQL_REVIEW_SPECIALIST_NAME: SQL_REVIEW_REQUIRED_TOOLS,
    SCHEMA_DRIFT_SPECIALIST_NAME: SCHEMA_DRIFT_REQUIRED_TOOLS,
}

RISK_TIER_ORDER = {
    AgentRiskTier.LOW: 1,
    AgentRiskTier.MEDIUM: 2,
    AgentRiskTier.HIGH: 3,
    AgentRiskTier.CRITICAL: 4,
}


# --- Defining Capability Models
class AgentCapabilitySpec(BaseModel):
    """
    Describe one registered specialist and its enforceable runtime boundary.

    Attributes:
        specialist_name: Stable specialist registry key.
        description: Bounded human-readable responsibility.
        accepted_task_types: Task types this specialist can execute.
        allowed_tools: Maximum tool capability set.
        output_model: Structured result model exposed by the specialist.
        maximum_risk_tier: Highest risk tier accepted without a stronger reviewer.
        default_model_route: Deterministic route selected by policy.
        mutation_allowed: Whether the specialist can mutate platform state.
        approval_required: Whether every task requires prior human approval.
        success_evidence_required: Whether successful or partial output must retain at
            least one deterministic evidence reference.
        allowed_side_effects: Append-only or lifecycle side effects permitted by policy.
        retry_safe: Whether the supervisor may retry transient failures without duplicating
            business mutations or non-idempotent report/lifecycle writes.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    specialist_name: str
    description: str                         = Field(min_length=1, max_length=1_000)
    accepted_task_types: tuple[str, ...]      = Field(min_length=1, max_length=20)
    allowed_tools: tuple[str, ...]            = Field(min_length=1, max_length=20)
    output_model: str                         = Field(min_length=1, max_length=120)
    maximum_risk_tier: AgentRiskTier          = AgentRiskTier.LOW
    default_model_route: AgentModelRoute      = AgentModelRoute.NO_LLM_FALLBACK
    mutation_allowed: bool                    = False
    approval_required: bool                   = False
    success_evidence_required: bool           = True
    allowed_side_effects: tuple[str, ...]      = ()
    retry_safe: bool                           = False

    @model_validator(mode="after")
    def validate_unique_capabilities(self) -> "AgentCapabilitySpec":
        """
        Reject ambiguous duplicate task or tool declarations.

        Returns:
            Current capability specification when declarations are unique.

        Raises:
            ValueError: If task types or tools contain duplicates.
        """
        if len(set(self.accepted_task_types)) != len(self.accepted_task_types):
            raise ValueError("Specialist accepted_task_types contains duplicates.")

        if len(set(self.allowed_tools)) != len(self.allowed_tools):
            raise ValueError("Specialist allowed_tools contains duplicates.")

        if self.retry_safe and self.mutation_allowed:
            raise ValueError("Retry-safe specialists cannot hold mutation permission.")

        if self.retry_safe and any(
            side_effect != "append_audit_events"
            for side_effect in self.allowed_side_effects
        ):
            raise ValueError(
                "Retry-safe specialists may only retain append-only audit side effects."
            )

        return self


# --- Registering Specialist Capabilities
AGENT_CAPABILITY_REGISTRY = {
    INCIDENT_TRIAGE_SPECIALIST_NAME: AgentCapabilitySpec(
        specialist_name=INCIDENT_TRIAGE_SPECIALIST_NAME,
        description=(
            "Run the existing evidence-driven LangGraph triage workflow for one alert, "
            "persist its reports and audit evidence, and return a bounded incident summary."
        ),
        accepted_task_types=INCIDENT_TRIAGE_TASK_TYPES,
        allowed_tools=INCIDENT_TRIAGE_ALLOWED_TOOLS,
        output_model="IncidentTriageAgentOutput",
        maximum_risk_tier=AgentRiskTier.MEDIUM,
        default_model_route=AgentModelRoute.DEEPTHINK_LLM,
        mutation_allowed=False,
        approval_required=False,
        allowed_side_effects=(
            "append_audit_events",
            "write_report_artifacts",
            "update_alert_lifecycle",
        ),
        retry_safe=False,
    ),
    METADATA_LINEAGE_SPECIALIST_NAME: AgentCapabilitySpec(
        specialist_name=METADATA_LINEAGE_SPECIALIST_NAME,
        description=(
            "Read trusted asset metadata and bounded dbt lineage evidence, then return an "
            "explainable trust and downstream-impact assessment without mutating platform state."
        ),
        accepted_task_types=METADATA_LINEAGE_TASK_TYPES,
        allowed_tools=METADATA_LINEAGE_ALLOWED_TOOLS,
        output_model="MetadataLineageAgentOutput",
        maximum_risk_tier=AgentRiskTier.MEDIUM,
        default_model_route=AgentModelRoute.NO_LLM_FALLBACK,
        mutation_allowed=False,
        approval_required=False,
        allowed_side_effects=("append_audit_events",),
        retry_safe=True,
    ),
    SQL_REVIEW_SPECIALIST_NAME: AgentCapabilitySpec(
        specialist_name=SQL_REVIEW_SPECIALIST_NAME,
        description=(
            "Review one SQL proposal using deterministic read-only policy, trusted metadata, "
            "and conservative ClickHouse scan evidence without executing the proposal."
        ),
        accepted_task_types=SQL_REVIEW_TASK_TYPES,
        allowed_tools=SQL_REVIEW_ALLOWED_TOOLS,
        output_model="SqlReviewAgentOutput",
        maximum_risk_tier=AgentRiskTier.MEDIUM,
        default_model_route=AgentModelRoute.NO_LLM_FALLBACK,
        mutation_allowed=False,
        approval_required=False,
        allowed_side_effects=("append_audit_events",),
        retry_safe=True,
    ),
    SCHEMA_DRIFT_SPECIALIST_NAME: AgentCapabilitySpec(
        specialist_name=SCHEMA_DRIFT_SPECIALIST_NAME,
        description=(
            "Assess one exact persisted schema detector run, combine deterministic findings "
            "with metadata and bounded dbt impact, and return non-executing migration guidance."
        ),
        accepted_task_types=SCHEMA_DRIFT_TASK_TYPES,
        allowed_tools=SCHEMA_DRIFT_ALLOWED_TOOLS,
        output_model="SchemaDriftAgentOutput",
        maximum_risk_tier=AgentRiskTier.MEDIUM,
        default_model_route=AgentModelRoute.NO_LLM_FALLBACK,
        mutation_allowed=False,
        approval_required=False,
        allowed_side_effects=("append_audit_events",),
        retry_safe=True,
    ),
}


# --- Defining Registry Helpers
def list_agent_capabilities() -> tuple[AgentCapabilitySpec, ...]:
    """
    Return registered specialist capabilities in stable name order.

    Returns:
        Tuple of immutable capability specifications.
    """
    capabilities = tuple(
        AGENT_CAPABILITY_REGISTRY[name]
        for name in sorted(AGENT_CAPABILITY_REGISTRY)
    )

    logger.info("Listing agent capability registry | count=%d", len(capabilities))

    return capabilities


def get_agent_capability(specialist_name: str) -> AgentCapabilitySpec:
    """
    Load one specialist capability from the registry.

    Args:
        specialist_name: Stable specialist registry key.

    Returns:
        Registered AgentCapabilitySpec.

    Raises:
        LookupError: If the specialist is not registered.
    """
    normalized = specialist_name.strip().lower()
    capability = AGENT_CAPABILITY_REGISTRY.get(normalized)

    if capability is None:
        raise LookupError(f"Specialist is not registered: {specialist_name}")

    return capability


def required_tools_for_task(specialist_name: str, task_type: str) -> tuple[str, ...]:
    """
    Return the exact least-privilege tool set required by one specialist task.

    Args:
        specialist_name: Registered specialist key.
        task_type: Task type accepted by the specialist.

    Returns:
        Tuple of required tool names.

    Raises:
        ValueError: If the specialist or task has no required-tool policy.
    """
    normalized_specialist = specialist_name.strip().lower()
    normalized_task       = task_type.strip().lower()

    specialist_policy = REQUIRED_TOOLS_BY_SPECIALIST.get(normalized_specialist)

    if specialist_policy is None:
        raise ValueError(f"No required-tool policy for specialist: {specialist_name}")

    required_tools = specialist_policy.get(normalized_task)

    if required_tools is None:
        raise ValueError(f"No required-tool policy for task: {task_type}")

    return required_tools


def enforce_task_capability(task: AgentTaskEnvelope) -> AgentCapabilitySpec:
    """
    Enforce specialist, task, risk, model-route, and least-privilege tool policy.

    Args:
        task: Typed supervisor-to-specialist handoff.

    Returns:
        Capability specification that authorized the task.

    Raises:
        PermissionError: If the task violates capability or routing policy.
    """
    capability = get_agent_capability(task.specialist_name)

    if task.task_type not in capability.accepted_task_types:
        raise PermissionError(
            f"Task type {task.task_type} is not accepted by {task.specialist_name}."
        )

    if RISK_TIER_ORDER[task.risk_tier] > RISK_TIER_ORDER[capability.maximum_risk_tier]:
        raise PermissionError(
            f"Risk tier {task.risk_tier.value} exceeds {task.specialist_name} capability."
        )

    if task.model_route != capability.default_model_route:
        raise PermissionError(
            f"Model route {task.model_route.value} is not allowed for {task.specialist_name}."
        )

    requested_tools = set(task.allowed_tools)
    capability_tools = set(capability.allowed_tools)
    required_tools   = set(required_tools_for_task(task.specialist_name, task.task_type))
    unauthorized     = requested_tools - capability_tools
    missing          = required_tools - requested_tools

    if unauthorized:
        raise PermissionError(
            "Task requested unauthorized specialist tools: " + ", ".join(sorted(unauthorized))
        )

    if missing:
        raise PermissionError(
            "Task omitted required specialist tools: " + ", ".join(sorted(missing))
        )

    logger.info(
        "Authorized specialist task | specialist=%s task_type=%s risk=%s tools=%s",
        task.specialist_name,
        task.task_type,
        task.risk_tier.value,
        task.allowed_tools,
    )

    return capability


def enforce_result_contract(
    task: AgentTaskEnvelope,
    result: AgentResultEnvelope,
) -> AgentResultEnvelope:
    """
    Validate one specialist result against its authorized source task.

    Args:
        task: Policy-authorized supervisor-to-specialist handoff.
        result: Structured specialist outcome returned across the handoff boundary.

    Returns:
        Revalidated AgentResultEnvelope when identity, evidence, route, and timeout
        invariants pass.

    Raises:
        PermissionError: If result identity, evidence source, model route, or duration
            violates the source task and specialist capability.
        ValueError: If the result is not a valid terminal AgentResultEnvelope.
    """
    capability       = enforce_task_capability(task)
    validated_result = AgentResultEnvelope.model_validate(
        result.model_dump(mode="python")
    )
    identity_pairs   = {
        "task_id": (task.task_id, validated_result.task_id),
        "parent_run_id": (task.parent_run_id, validated_result.parent_run_id),
        "specialist_name": (task.specialist_name, validated_result.specialist_name),
        "task_type": (task.task_type, validated_result.task_type),
    }
    mismatched_fields = [
        field_name
        for field_name, (expected, actual) in identity_pairs.items()
        if expected != actual
    ]

    if mismatched_fields:
        raise PermissionError(
            "Specialist result identity does not match its source task: "
            + ", ".join(mismatched_fields)
        )

    if (
        capability.success_evidence_required
        and validated_result.status in {AgentTaskStatus.SUCCESS, AgentTaskStatus.PARTIAL}
        and not validated_result.evidence_references
    ):
        raise PermissionError(
            f"{task.specialist_name} must retain deterministic evidence for "
            f"{validated_result.status.value} results."
        )

    unauthorized_evidence_tools = {
        reference.source_tool
        for reference in validated_result.evidence_references
        if reference.source_tool not in task.allowed_tools
    }

    if unauthorized_evidence_tools:
        raise PermissionError(
            "Specialist result references evidence from unauthorized tools: "
            + ", ".join(sorted(unauthorized_evidence_tools))
        )

    route_within_capability = (
        MODEL_ROUTE_ORDER[validated_result.model_route]
        <= MODEL_ROUTE_ORDER[task.model_route]
    )

    if not route_within_capability:
        raise PermissionError(
            "Specialist result model route exceeds its authorized task capability: "
            f"task={task.model_route.value} result={validated_result.model_route.value}"
        )

    if validated_result.duration_ms > task.timeout_seconds * 1_000:
        raise PermissionError(
            "Specialist result duration exceeds its source task timeout: "
            f"duration_ms={validated_result.duration_ms} "
            f"timeout_ms={task.timeout_seconds * 1_000}"
        )

    logger.info(
        "Validated specialist result contract | task_id=%s specialist=%s status=%s evidence=%d route=%s duration_ms=%d",
        task.task_id,
        task.specialist_name,
        validated_result.status.value,
        len(validated_result.evidence_references),
        validated_result.model_route.value,
        validated_result.duration_ms,
    )

    return validated_result
