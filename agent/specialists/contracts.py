####
## Multi-Agent Handoff Contracts for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Strict task, result, evidence, and supervisor contracts for bounded agent handoffs."""

# --- Importing Libraries
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pipelines.common.logging import logger


# --- Defining Constants
MAX_CONTEXT_PAYLOAD_BYTES = 32_000

FORBIDDEN_CONTEXT_KEYS = {
    "api_key",
    "api_token",
    "authorization",
    "credential",
    "credentials",
    "password",
    "secret",
    "token",
}

SAFE_CONTRACT_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,79}$")


# --- Defining Enumerations
class AgentTaskStatus(str, Enum):
    """Represent one specialist handoff lifecycle state."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED  = "failed"
    BLOCKED = "blocked"


# Result envelopes cross a completed handoff boundary and must never carry
# in-flight lifecycle states that belong in HandoffRecord.
TERMINAL_RESULT_STATUSES = {
    AgentTaskStatus.SUCCESS,
    AgentTaskStatus.PARTIAL,
    AgentTaskStatus.FAILED,
    AgentTaskStatus.BLOCKED,
}


class AgentRiskTier(str, Enum):
    """Classify the operational risk attached to one specialist task."""

    LOW      = "low"
    MEDIUM   = "medium"
    HIGH     = "high"
    CRITICAL = "critical"


class AgentModelRoute(str, Enum):
    """Define provider-agnostic capability routes selected by deterministic policy."""

    NO_LLM_FALLBACK = "no_llm_fallback"
    QUICKTHINK_LLM  = "quickthinkllm"
    DEEPTHINK_LLM   = "deepthinkllm"


MODEL_ROUTE_ORDER = {
    AgentModelRoute.NO_LLM_FALLBACK: 0,
    AgentModelRoute.QUICKTHINK_LLM: 1,
    AgentModelRoute.DEEPTHINK_LLM: 2,
}


class AgentApprovalState(str, Enum):
    """Represent the human-approval state attached to a supervisor run."""

    NOT_REQUIRED = "not_required"
    REQUIRED     = "required"
    PENDING      = "pending"
    APPROVED     = "approved"
    REJECTED     = "rejected"


class ContextReferenceType(str, Enum):
    """Allow only explicit, non-secret context references between agents."""

    ALERT          = "alert"
    METADATA_ASSET = "metadata_asset"
    DBT_MANIFEST   = "dbt_manifest"
    S3_ARTIFACT    = "s3_artifact"
    AUDIT_RUN      = "audit_run"
    RUN_CONTEXT    = "run_context"
    INCIDENT_MEMORY = "incident_memory"


# --- Defining Validation Helpers
def find_forbidden_context_key(value: Any, path: str = "input_payload") -> str | None:
    """
    Find the first credential-like key in a nested handoff payload.

    Args:
        value: Arbitrary JSON-like payload supplied to a task or result contract.
        path: Human-readable traversal path used in validation errors.

    Returns:
        Forbidden key path when found, otherwise None.
    """
    if isinstance(value, dict):
        for raw_key, nested_value in value.items():
            normalized_key = str(raw_key).strip().lower()
            current_path   = f"{path}.{normalized_key}"

            if normalized_key in FORBIDDEN_CONTEXT_KEYS:
                return current_path

            nested_match = find_forbidden_context_key(nested_value, path=current_path)

            if nested_match:
                return nested_match

    elif isinstance(value, list):
        for index, nested_value in enumerate(value):
            nested_match = find_forbidden_context_key(nested_value, path=f"{path}[{index}]")

            if nested_match:
                return nested_match

    return None


def validate_bounded_payload(payload: dict[str, Any], field_name: str) -> dict[str, Any]:
    """
    Reject secrets and oversized JSON payloads before agent context is shared.

    Args:
        payload: JSON-like dictionary to validate.
        field_name: Contract field name used in validation errors.

    Returns:
        Original payload when it satisfies context-sharing policy.

    Raises:
        ValueError: If the payload contains credential-like keys or is too large.
    """
    forbidden_path = find_forbidden_context_key(payload, path=field_name)

    if forbidden_path:
        raise ValueError(f"Agent handoff payload contains forbidden context key: {forbidden_path}")

    payload_bytes = len(json.dumps(payload, default=str, ensure_ascii=True).encode("utf-8"))

    if payload_bytes > MAX_CONTEXT_PAYLOAD_BYTES:
        raise ValueError(
            f"Agent handoff payload exceeds {MAX_CONTEXT_PAYLOAD_BYTES} bytes: {field_name}"
        )

    return payload


# --- Defining Shared Context Models
class ContextReference(BaseModel):
    """
    Reference durable context without copying hidden conversation or raw environment state.

    Attributes:
        reference_type: Allowlisted context category.
        reference: Stable identifier, qualified asset name, audit run ID, or S3 URI.
        description: Short explanation of why the context is relevant.
    """

    model_config = ConfigDict(extra="forbid")

    reference_type: ContextReferenceType
    reference: str                           = Field(min_length=1, max_length=2_048)
    description: str                         = Field(default="", max_length=500)

    @field_validator("reference", "description")
    @classmethod
    def normalize_reference_text(cls, value: str) -> str:
        """
        Normalize bounded context text and reject multiline injection payloads.

        Args:
            value: Raw reference or description.

        Returns:
            Trimmed single-line text.

        Raises:
            ValueError: If a reference contains line breaks.
        """
        normalized = value.strip()

        if "\n" in normalized or "\r" in normalized:
            raise ValueError("Context references must be single-line values.")

        return normalized


class EvidenceReference(BaseModel):
    """
    Point to deterministic evidence returned by a specialist tool.

    Attributes:
        evidence_type: Stable evidence category.
        source_tool: Allowlisted tool that produced the evidence.
        reference: Stable asset, dbt node, artifact, or audit reference.
        summary: Bounded operator-facing evidence description.
    """

    model_config = ConfigDict(extra="forbid")

    evidence_type: str = Field(min_length=1, max_length=80)
    source_tool: str    = Field(min_length=1, max_length=80)
    reference: str      = Field(min_length=1, max_length=2_048)
    summary: str        = Field(min_length=1, max_length=1_000)


# --- Defining Handoff Contracts
class AgentTaskEnvelope(BaseModel):
    """
    Carry one explicit and policy-bounded task from a supervisor to a specialist.

    Attributes:
        task_id: Unique handoff identifier.
        parent_run_id: Correlation UUID shared across the investigation.
        specialist_name: Capability-registry specialist selected by policy.
        task_type: Allowlisted task accepted by the selected specialist.
        risk_tier: Operational risk classification.
        allowed_tools: Exact least-privilege tool allowlist for this handoff.
        context_references: Explicit references to shared run context.
        model_route: Capability route selected by deterministic policy.
        model_call_budget: Maximum external model calls available to the handoff.
        token_budget: Maximum model tokens available to the handoff.
        estimated_cost_budget_usd: Maximum estimated model cost for the handoff.
        timeout_seconds: Maximum specialist runtime.
        requester: Bounded actor or interface name.
        alert_key: Optional stable alert correlation key.
        input_payload: Specialist-specific structured input without credentials.
        created_at: UTC task creation timestamp.
    """

    model_config = ConfigDict(extra="forbid")

    task_id: UUID                                  = Field(default_factory=uuid4)
    parent_run_id: UUID
    specialist_name: str                           = Field(min_length=3, max_length=80)
    task_type: str                                 = Field(min_length=3, max_length=80)
    risk_tier: AgentRiskTier                       = AgentRiskTier.LOW
    allowed_tools: tuple[str, ...]                  = Field(min_length=1, max_length=12)
    context_references: list[ContextReference]      = Field(default_factory=list, max_length=20)
    model_route: AgentModelRoute                    = AgentModelRoute.NO_LLM_FALLBACK
    model_call_budget: int                          = Field(default=0, ge=0, le=3)
    token_budget: int                               = Field(default=0, ge=0, le=16_384)
    estimated_cost_budget_usd: float                = Field(default=0.0, ge=0.0, le=0.05)
    timeout_seconds: int                            = Field(default=30, ge=1, le=300)
    requester: str                                  = Field(default="control_plane", min_length=1, max_length=100)
    alert_key: str                                  = Field(default="", max_length=500)
    input_payload: dict[str, Any]                    = Field(default_factory=dict)
    created_at: datetime                            = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("specialist_name", "task_type")
    @classmethod
    def validate_contract_name(cls, value: str) -> str:
        """
        Validate stable snake-case specialist and task identifiers.

        Args:
            value: Raw identifier.

        Returns:
            Normalized identifier.

        Raises:
            ValueError: If the identifier is not safe snake case.
        """
        normalized = value.strip().lower()

        if not SAFE_CONTRACT_NAME_PATTERN.fullmatch(normalized):
            raise ValueError(f"Unsafe agent contract identifier: {value}")

        return normalized

    @field_validator("allowed_tools")
    @classmethod
    def validate_allowed_tools(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """
        Normalize and de-duplicate a handoff tool allowlist.

        Args:
            value: Raw tool-name tuple.

        Returns:
            Normalized tool names in caller order.

        Raises:
            ValueError: If a tool name is unsafe or duplicated.
        """
        normalized = tuple(item.strip().lower() for item in value)

        if any(not SAFE_CONTRACT_NAME_PATTERN.fullmatch(item) for item in normalized):
            raise ValueError("Agent tool names must use safe snake-case identifiers.")

        if len(set(normalized)) != len(normalized):
            raise ValueError("Agent handoff tool allowlist contains duplicates.")

        return normalized

    @field_validator("requester", "alert_key")
    @classmethod
    def normalize_optional_text(cls, value: str) -> str:
        """
        Normalize bounded operator and alert references.

        Args:
            value: Raw text value.

        Returns:
            Trimmed text value.
        """
        return value.strip()

    @model_validator(mode="after")
    def validate_route_and_context_policy(self) -> "AgentTaskEnvelope":
        """
        Enforce model budgets and safe context-sharing policy.

        Returns:
            Current envelope when all policies pass.

        Raises:
            ValueError: If model budget or shared context violates policy.
        """
        if self.model_route == AgentModelRoute.NO_LLM_FALLBACK:
            if any(
                (
                    self.model_call_budget,
                    self.token_budget,
                    self.estimated_cost_budget_usd,
                )
            ):
                raise ValueError(
                    "no_llm_fallback tasks must use zero model-call, token, and cost budgets."
                )

        if self.model_route != AgentModelRoute.NO_LLM_FALLBACK:
            if self.model_call_budget == 0 or self.token_budget == 0:
                raise ValueError(
                    "LLM-routed tasks require positive model-call and token budgets."
                )

        validate_bounded_payload(self.input_payload, field_name="input_payload")

        return self


class AgentResultEnvelope(BaseModel):
    """
    Return one structured specialist outcome without leaking private internal context.

    Attributes:
        task_id: Source handoff identifier.
        parent_run_id: Parent investigation correlation UUID.
        specialist_name: Specialist that produced the result.
        task_type: Completed task type.
        status: Terminal specialist status.
        evidence_references: Deterministic evidence references used by the result.
        structured_output: Bounded specialist-specific output contract.
        confidence: Deterministic or policy-owned confidence score.
        model_route: Capability route actually used.
        model_call_count: External provider attempts consumed by the specialist,
            including failed attempts retained conservatively by supervisor policy.
        token_usage: Total model tokens consumed by the specialist.
        estimated_cost_usd: Estimated model cost for the handoff.
        duration_ms: End-to-end specialist duration.
        errors: Sanitized failure or partial-result messages.
        recommended_next_step: Safe operator-facing next step.
        requires_human_approval: Whether a proposed action needs approval.
        completed_at: UTC completion timestamp.
    """

    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    parent_run_id: UUID
    specialist_name: str                           = Field(min_length=3, max_length=80)
    task_type: str                                 = Field(min_length=3, max_length=80)
    status: AgentTaskStatus
    evidence_references: list[EvidenceReference] = Field(default_factory=list, max_length=100)
    structured_output: dict[str, Any]             = Field(default_factory=dict)
    confidence: float                             = Field(default=0.0, ge=0.0, le=1.0)
    model_route: AgentModelRoute                  = AgentModelRoute.NO_LLM_FALLBACK
    model_call_count: int                         = Field(default=0, ge=0, le=100)
    token_usage: int                              = Field(default=0, ge=0)
    estimated_cost_usd: float                     = Field(default=0.0, ge=0.0)
    duration_ms: int                              = Field(default=0, ge=0)
    errors: list[str]                             = Field(default_factory=list, max_length=20)
    recommended_next_step: str                    = Field(default="", max_length=2_000)
    requires_human_approval: bool                 = False
    completed_at: datetime                        = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("specialist_name", "task_type")
    @classmethod
    def validate_result_contract_name(cls, value: str) -> str:
        """
        Normalize and validate stable result identity fields.

        Args:
            value: Raw specialist or task identifier.

        Returns:
            Normalized safe snake-case identifier.

        Raises:
            ValueError: If the identifier is not safe snake case.
        """
        normalized = value.strip().lower()

        if not SAFE_CONTRACT_NAME_PATTERN.fullmatch(normalized):
            raise ValueError(f"Unsafe agent result identifier: {value}")

        return normalized

    @field_validator("errors")
    @classmethod
    def validate_result_errors(cls, value: list[str]) -> list[str]:
        """
        Keep operator-facing result errors bounded, unique, and single-line.

        Args:
            value: Raw sanitized error messages returned by a specialist.

        Returns:
            Trimmed error messages in their original order.

        Raises:
            ValueError: If an error is blank, duplicated, multiline, or oversized.
        """
        normalized_errors: list[str] = []

        for error in value:
            normalized = error.strip()

            if not normalized:
                raise ValueError("Specialist result errors cannot be blank.")

            if "\n" in normalized or "\r" in normalized:
                raise ValueError("Specialist result errors must be single-line values.")

            if len(normalized) > 2_000:
                raise ValueError("Specialist result errors cannot exceed 2000 characters.")

            if normalized in normalized_errors:
                raise ValueError("Specialist result errors cannot contain duplicates.")

            normalized_errors.append(normalized)

        return normalized_errors

    @model_validator(mode="after")
    def validate_terminal_result(self) -> "AgentResultEnvelope":
        """
        Enforce consistent terminal status, error, and model-usage fields.

        Returns:
            Current result when consistency checks pass.

        Raises:
            ValueError: If status or model-usage fields contradict each other.
        """
        if self.status not in TERMINAL_RESULT_STATUSES:
            raise ValueError("AgentResultEnvelope requires a terminal specialist status.")

        if self.status in {AgentTaskStatus.FAILED, AgentTaskStatus.BLOCKED} and not self.errors:
            raise ValueError("Failed or blocked specialist results require at least one error.")

        if self.status == AgentTaskStatus.PARTIAL and not self.errors:
            raise ValueError("Partial specialist results require at least one retained error.")

        if self.status == AgentTaskStatus.SUCCESS and self.errors:
            raise ValueError("Successful specialist results cannot contain errors; use partial status.")

        if (
            self.status in {AgentTaskStatus.FAILED, AgentTaskStatus.BLOCKED}
            and self.confidence != 0.0
        ):
            raise ValueError("Failed or blocked specialist results must report zero confidence.")

        if (
            self.model_route == AgentModelRoute.NO_LLM_FALLBACK
            and self.model_call_count == 0
            and any((self.token_usage, self.estimated_cost_usd))
        ):
            # A pure heuristic result has no external usage. A heuristic fallback
            # may still retain conservative usage from failed provider attempts.
            raise ValueError(
                "Pure no_llm_fallback results must report zero usage and cost."
            )

        if (
            self.model_route != AgentModelRoute.NO_LLM_FALLBACK
            and self.model_call_count == 0
        ):
            raise ValueError("LLM-routed results must report a positive model_call_count.")

        validate_bounded_payload(self.structured_output, field_name="structured_output")

        return self


class HandoffRecord(BaseModel):
    """
    Track one auditable parent-to-specialist handoff in supervisor state.

    Attributes:
        task_id: Handoff task identifier.
        specialist_name: Selected specialist.
        task_type: Requested capability.
        status: Current or terminal handoff status.
        started_at: UTC handoff start timestamp.
        completed_at: Optional UTC terminal timestamp.
        duration_ms: Optional terminal duration.
        retry_count: Number of bounded retries attempted.
        error_message: Sanitized terminal failure message.
    """

    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    specialist_name: str
    task_type: str
    status: AgentTaskStatus
    started_at: datetime           = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None  = None
    duration_ms: int | None        = Field(default=None, ge=0)
    retry_count: int               = Field(default=0, ge=0, le=5)
    error_message: str             = Field(default="", max_length=2_000)


class SupervisorState(BaseModel):
    """
    Hold explicit supervisor run state, budgets, handoff history, and final output.

    Attributes:
        parent_run_id: Shared investigation correlation UUID.
        active_task: Currently executing specialist task.
        handoff_history: Bounded handoff lifecycle records.
        specialist_results: Structured results returned by specialists.
        run_context_event_ids: Persisted temporary context event references.
        incident_memory_ids: Persisted durable incident-memory references.
        approval_state: Human-approval state for any proposed mutation.
        max_handoffs: Maximum specialist handoffs allowed in this run.
        max_retries: Maximum retries allowed per handoff.
        max_model_calls: Maximum aggregate external model calls.
        token_budget: Maximum aggregate model tokens.
        estimated_cost_budget_usd: Maximum aggregate estimated model cost.
        latency_budget_ms: Maximum aggregate specialist latency.
        errors: Sanitized supervisor-level errors.
        final_response: Bounded response assembled for an operator interface.
    """

    model_config = ConfigDict(extra="forbid")

    parent_run_id: UUID                         = Field(default_factory=uuid4)
    active_task: AgentTaskEnvelope | None       = None
    handoff_history: list[HandoffRecord]         = Field(default_factory=list, max_length=20)
    specialist_results: list[AgentResultEnvelope] = Field(default_factory=list, max_length=20)
    run_context_event_ids: list[UUID]            = Field(default_factory=list, max_length=10)
    incident_memory_ids: list[UUID]              = Field(default_factory=list, max_length=10)
    approval_state: AgentApprovalState          = AgentApprovalState.NOT_REQUIRED
    max_handoffs: int                           = Field(default=5, ge=1, le=10)
    max_retries: int                            = Field(default=1, ge=0, le=5)
    max_model_calls: int                        = Field(default=3, ge=0, le=10)
    token_budget: int                           = Field(default=16_384, ge=0, le=64_000)
    estimated_cost_budget_usd: float            = Field(default=0.05, ge=0.0, le=0.15)
    latency_budget_ms: int                      = Field(default=300_000, ge=1_000, le=900_000)
    errors: list[str]                           = Field(default_factory=list, max_length=50)
    final_response: str                         = Field(default="", max_length=20_000)

    @model_validator(mode="after")
    def validate_aggregate_budgets(self) -> "SupervisorState":
        """
        Reject state snapshots that exceed deterministic supervisor budgets.

        Returns:
            Current state when aggregate budgets remain valid.

        Raises:
            ValueError: If handoff count, token, cost, or latency budgets are exceeded.
        """
        if len(self.handoff_history) > self.max_handoffs:
            raise ValueError("Supervisor handoff budget exceeded.")

        if len(set(self.run_context_event_ids)) != len(self.run_context_event_ids):
            raise ValueError("Supervisor run context contains duplicate event identifiers.")

        if len(set(self.incident_memory_ids)) != len(self.incident_memory_ids):
            raise ValueError("Supervisor state contains duplicate incident-memory identifiers.")

        total_retries    = sum(record.retry_count for record in self.handoff_history)
        total_model_calls = sum(
            result.model_call_count
            for result in self.specialist_results
        )
        total_tokens     = sum(result.token_usage for result in self.specialist_results)
        total_cost       = sum(result.estimated_cost_usd for result in self.specialist_results)
        total_latency    = sum(result.duration_ms for result in self.specialist_results)

        if total_retries > self.max_retries:
            raise ValueError("Supervisor retry budget exceeded.")

        if total_model_calls > self.max_model_calls:
            raise ValueError("Supervisor model-call budget exceeded.")

        if total_tokens > self.token_budget:
            raise ValueError("Supervisor token budget exceeded.")

        if total_cost > self.estimated_cost_budget_usd:
            raise ValueError("Supervisor estimated-cost budget exceeded.")

        if total_latency > self.latency_budget_ms:
            raise ValueError("Supervisor latency budget exceeded.")

        return self


# --- Defining Contract Helpers
def build_supervisor_state(parent_run_id: UUID | None = None) -> SupervisorState:
    """
    Build the initial typed state for a future control-plane supervisor run.

    Args:
        parent_run_id: Optional investigation correlation UUID.

    Returns:
        Empty SupervisorState with deterministic default budgets.
    """
    state = SupervisorState(parent_run_id=parent_run_id or uuid4())

    logger.info(
        "Built supervisor state | parent_run_id=%s max_handoffs=%d token_budget=%d",
        state.parent_run_id,
        state.max_handoffs,
        state.token_budget,
    )

    return state
