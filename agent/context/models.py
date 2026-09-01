####
## Agent Context Models for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Define explicit run-scoped context and durable incident-memory records."""

# --- Importing Libraries
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent.specialists.contracts import (
    AgentApprovalState,
    AgentTaskStatus,
    ContextReference,
    EvidenceReference,
    validate_bounded_payload,
)
from pipelines.common.logging import logger


# --- Defining Constants
DEFAULT_RUN_CONTEXT_RETENTION_DAYS = 30
MAX_RUN_CONTEXT_RETENTION_DAYS     = 90
MAX_CONTEXT_REFERENCES             = 20
MAX_EVIDENCE_REFERENCES            = 100
MAX_MEMORY_SUMMARY_LENGTH          = 5_000

FORBIDDEN_PERSISTED_CONTEXT_KEYS = {
    "conversation",
    "conversation_history",
    "environment",
    "messages",
    "prompt",
    "raw_output",
    "raw_sql",
    "raw_tool_output",
    "sql",
    "sql_proposal",
    "tool_output",
}

RUN_CONTEXT_SEQUENCE = {
    "started": 10,
    "routed": 20,
    "completed": 30,
    "blocked": 30,
}


# --- Defining Enumerations
class RunContextPhase(str, Enum):
    """Represent the bounded lifecycle phases retained for one supervisor run."""

    STARTED   = "started"
    ROUTED    = "routed"
    COMPLETED = "completed"
    BLOCKED   = "blocked"


class IncidentMemoryType(str, Enum):
    """Classify durable incident facts without storing hidden conversation memory."""

    INVESTIGATION_OUTCOME = "investigation_outcome"
    RESOLUTION_APPROVED   = "resolution_approved"
    RESOLUTION_REJECTED   = "resolution_rejected"


# --- Defining Serialization Helpers
def normalize_single_line(value: str, field_name: str) -> str:
    """
    Normalize a bounded identifier and reject multiline context injection.

    Args:
        value: Raw text value.
        field_name: Human-readable field name used in validation errors.

    Returns:
        Trimmed single-line value.

    Raises:
        ValueError: If the value contains a line break.
    """
    normalized = value.strip()

    if "\n" in normalized or "\r" in normalized:
        raise ValueError(f"{field_name} must remain single-line.")

    return normalized


def canonical_json(payload: Any) -> str:
    """
    Serialize one context payload deterministically for hashing and persistence.

    Args:
        payload: JSON-like value containing bounded context facts.

    Returns:
        Canonical ASCII JSON string with stable key order.
    """
    return json.dumps(
        payload,
        default=str,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def context_content_sha256(payload: dict[str, Any]) -> str:
    """
    Build a deterministic SHA-256 digest for one semantic context payload.

    Args:
        payload: Semantic context facts excluding write timestamps.

    Returns:
        Lowercase hexadecimal SHA-256 digest.
    """
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def find_forbidden_persisted_context_key(
    value: Any,
    path: str = "persisted_context",
) -> str | None:
    """
    Find hidden conversation, environment, raw SQL, or raw tool-output keys.

    Args:
        value: Arbitrary JSON-like context value.
        path: Human-readable traversal path used in validation errors.

    Returns:
        First forbidden key path when found, otherwise None.
    """
    if isinstance(value, dict):
        for raw_key, nested_value in value.items():
            normalized_key = str(raw_key).strip().lower()
            current_path   = f"{path}.{normalized_key}"

            if normalized_key in FORBIDDEN_PERSISTED_CONTEXT_KEYS:
                return current_path

            nested_match = find_forbidden_persisted_context_key(
                nested_value,
                path=current_path,
            )

            if nested_match:
                return nested_match

    elif isinstance(value, list):
        for index, nested_value in enumerate(value):
            nested_match = find_forbidden_persisted_context_key(
                nested_value,
                path=f"{path}[{index}]",
            )

            if nested_match:
                return nested_match

    return None


def validate_persisted_context_payload(
    payload: dict[str, Any],
    field_name: str,
) -> dict[str, Any]:
    """
    Enforce bounded, credential-free, and hidden-context-free persistence.

    Args:
        payload: JSON-like dictionary proposed for ClickHouse persistence.
        field_name: Model field name used in validation errors.

    Returns:
        Original payload when it satisfies persistence policy.

    Raises:
        ValueError: If payload is oversized or contains forbidden context.
    """
    validate_bounded_payload(payload, field_name=field_name)
    forbidden_path = find_forbidden_persisted_context_key(payload, path=field_name)

    if forbidden_path:
        raise ValueError(
            "Persisted agent context contains a forbidden hidden-context key: "
            f"{forbidden_path}"
        )

    return payload


def derive_run_context_event_id(parent_run_id: UUID, phase: RunContextPhase) -> UUID:
    """
    Derive an idempotent event UUID from one parent run and lifecycle phase.

    Args:
        parent_run_id: Stable supervisor run correlation UUID.
        phase: Bounded run-context lifecycle phase.

    Returns:
        Deterministic context event UUID.
    """
    return uuid5(
        NAMESPACE_URL,
        f"agentic-dq:run-context:{parent_run_id}:{phase.value}",
    )


def derive_incident_memory_id(
    parent_run_id: UUID,
    memory_type: IncidentMemoryType,
    alert_reference: str,
) -> UUID:
    """
    Derive an idempotent durable-memory UUID for one incident outcome.

    Args:
        parent_run_id: Stable supervisor run correlation UUID.
        memory_type: Durable memory category.
        alert_reference: Canonical system alert key, Alert Ref, or alert UUID.

    Returns:
        Deterministic incident-memory UUID.
    """
    return uuid5(
        NAMESPACE_URL,
        f"agentic-dq:incident-memory:{parent_run_id}:{memory_type.value}:{alert_reference}",
    )


# --- Defining Run Context Model
class RunContextEvent(BaseModel):
    """
    Persist one explicit and temporary supervisor context transition.

    Attributes:
        context_event_id: Deterministic event identifier.
        parent_run_id: Stable supervisor correlation UUID.
        external_run_id: Airflow or operator run identifier.
        event_sequence: Deterministic lifecycle ordering value.
        phase: Started, routed, completed, or blocked phase.
        occurred_at: UTC event write timestamp.
        expires_at: TTL boundary for temporary run context.
        requester: Calling interface or system identity.
        status: Supervisor or specialist status at this phase.
        selected_specialist: Policy-selected specialist name when available.
        task_type: Policy-selected specialist task when available.
        task_id: Optional typed handoff UUID.
        alert_id: Optional exact alert UUID.
        alert_key: Optional canonical system alert key.
        alert_display_id: Optional human-facing Alert Ref.
        context_references: Explicit non-secret context references.
        evidence_references: Deterministic evidence references from the specialist.
        decision_facts: Bounded policy facts without raw prompts or tool output.
        report_s3_uri: Optional persisted report artifact.
        approval_state: Human approval state for proposed actions.
        content_sha256: Digest of semantic context facts.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    context_event_id: UUID
    parent_run_id: UUID
    external_run_id: str                          = Field(min_length=1, max_length=250)
    event_sequence: int                           = Field(ge=1, le=100)
    phase: RunContextPhase
    occurred_at: datetime                         = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime
    requester: str                                = Field(min_length=1, max_length=100)
    status: AgentTaskStatus
    selected_specialist: str                      = Field(default="", max_length=80)
    task_type: str                                 = Field(default="", max_length=80)
    task_id: UUID | None                           = None
    alert_id: UUID | None                          = None
    alert_key: str                                 = Field(default="", max_length=500)
    alert_display_id: str                          = Field(default="", max_length=40)
    context_references: list[ContextReference]     = Field(
        default_factory=list,
        max_length=MAX_CONTEXT_REFERENCES,
    )
    evidence_references: list[EvidenceReference]   = Field(
        default_factory=list,
        max_length=MAX_EVIDENCE_REFERENCES,
    )
    decision_facts: dict[str, Any]                 = Field(default_factory=dict)
    report_s3_uri: str                             = Field(default="", max_length=2_048)
    approval_state: AgentApprovalState             = AgentApprovalState.NOT_REQUIRED
    content_sha256: str                            = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator(
        "external_run_id",
        "requester",
        "selected_specialist",
        "task_type",
        "alert_key",
        "alert_display_id",
        "report_s3_uri",
    )
    @classmethod
    def normalize_text_fields(cls, value: str, info: Any) -> str:
        """
        Normalize identifiers and artifact references retained in shared context.

        Args:
            value: Raw field value.
            info: Pydantic validation metadata containing the field name.

        Returns:
            Trimmed single-line value.
        """
        return normalize_single_line(value, info.field_name)

    @model_validator(mode="after")
    def validate_context_policy(self) -> "RunContextEvent":
        """
        Enforce lifecycle ordering, TTL, bounded decisions, and S3 URI policy.

        Returns:
            Current event when all context policies pass.

        Raises:
            ValueError: If phase, expiration, decision payload, or URI is invalid.
        """
        expected_sequence = RUN_CONTEXT_SEQUENCE[self.phase.value]

        if self.event_sequence != expected_sequence:
            raise ValueError("Run context event sequence does not match its lifecycle phase.")

        if self.expires_at <= self.occurred_at:
            raise ValueError("Run context expiration must be after its occurrence time.")

        if self.expires_at > self.occurred_at + timedelta(
            days=MAX_RUN_CONTEXT_RETENTION_DAYS
        ):
            raise ValueError(
                "Run context expiration exceeds the maximum retention policy."
            )

        if self.report_s3_uri and not self.report_s3_uri.startswith("s3://"):
            raise ValueError("Run context report_s3_uri must use the s3:// scheme.")

        validate_persisted_context_payload(
            self.decision_facts,
            field_name="decision_facts",
        )

        return self


# --- Defining Durable Incident Memory Model
class IncidentMemoryRecord(BaseModel):
    """
    Persist durable evidence, decision, report, and approval facts for one incident.

    Attributes:
        memory_id: Deterministic durable record UUID.
        memory_key: Stable SHA-256 idempotency key.
        parent_run_id: Supervisor investigation correlation UUID.
        recorded_at: UTC persistence timestamp.
        memory_type: Investigation or approved/rejected resolution category.
        alert_id: Optional exact alert UUID.
        alert_key: Canonical system alert key when available.
        alert_display_id: Human-facing Alert Ref when available.
        outcome_status: Terminal supervisor or specialist outcome.
        specialist_name: Specialist responsible for the outcome.
        task_type: Completed bounded task type.
        summary: Operator-facing result without hidden reasoning.
        evidence_references: Durable pointers to evidence and artifacts.
        decision_facts: Bounded policy-owned decision facts.
        report_s3_uri: Optional Markdown or JSON report artifact.
        approval_state: Human approval state at persistence time.
        resolution_reference: Optional approval request or execution reference.
        content_sha256: Digest binding the durable memory to its semantic facts.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    memory_id: UUID
    memory_key: str                              = Field(pattern=r"^[a-f0-9]{64}$")
    parent_run_id: UUID
    recorded_at: datetime                       = Field(default_factory=lambda: datetime.now(timezone.utc))
    memory_type: IncidentMemoryType
    alert_id: UUID | None                        = None
    alert_key: str                               = Field(default="", max_length=500)
    alert_display_id: str                        = Field(default="", max_length=40)
    outcome_status: AgentTaskStatus
    specialist_name: str                         = Field(default="", max_length=80)
    task_type: str                               = Field(default="", max_length=80)
    summary: str                                 = Field(default="", max_length=MAX_MEMORY_SUMMARY_LENGTH)
    evidence_references: list[EvidenceReference] = Field(
        default_factory=list,
        max_length=MAX_EVIDENCE_REFERENCES,
    )
    decision_facts: dict[str, Any]               = Field(default_factory=dict)
    report_s3_uri: str                           = Field(default="", max_length=2_048)
    approval_state: AgentApprovalState           = AgentApprovalState.NOT_REQUIRED
    resolution_reference: str                    = Field(default="", max_length=500)
    content_sha256: str                          = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator(
        "alert_key",
        "alert_display_id",
        "specialist_name",
        "task_type",
        "report_s3_uri",
        "resolution_reference",
    )
    @classmethod
    def normalize_identifier_fields(cls, value: str, info: Any) -> str:
        """
        Normalize durable identity fields and reject multiline values.

        Args:
            value: Raw identifier or URI.
            info: Pydantic validation metadata containing the field name.

        Returns:
            Trimmed single-line value.
        """
        return normalize_single_line(value, info.field_name)

    @field_validator("summary")
    @classmethod
    def normalize_summary(cls, value: str) -> str:
        """
        Collapse operator summary whitespace without retaining conversation history.

        Args:
            value: Raw deterministic final response.

        Returns:
            Single-line bounded operator summary.
        """
        return " ".join(value.split())

    @model_validator(mode="after")
    def validate_memory_policy(self) -> "IncidentMemoryRecord":
        """
        Require an alert identity and bounded non-secret durable decision facts.

        Returns:
            Current record when durable-memory policies pass.

        Raises:
            ValueError: If identity, decision payload, or report URI is invalid.
        """
        if not any((self.alert_id, self.alert_key, self.alert_display_id)):
            raise ValueError("Durable incident memory requires an alert identity.")

        if self.report_s3_uri and not self.report_s3_uri.startswith("s3://"):
            raise ValueError("Incident memory report_s3_uri must use the s3:// scheme.")

        if (
            self.memory_type == IncidentMemoryType.INVESTIGATION_OUTCOME
            and self.outcome_status == AgentTaskStatus.SUCCESS
            and (not self.summary or not self.evidence_references)
        ):
            raise ValueError(
                "Successful investigation memory requires a summary and evidence references."
            )

        validate_persisted_context_payload(
            self.decision_facts,
            field_name="decision_facts",
        )

        return self


# --- Defining Model Builders
def build_run_context_event(
    parent_run_id: UUID,
    external_run_id: str,
    phase: RunContextPhase,
    requester: str,
    status: AgentTaskStatus,
    selected_specialist: str = "",
    task_type: str = "",
    task_id: UUID | None = None,
    alert_id: UUID | None = None,
    alert_key: str = "",
    alert_display_id: str = "",
    context_references: list[ContextReference] | None = None,
    evidence_references: list[EvidenceReference] | None = None,
    decision_facts: dict[str, Any] | None = None,
    report_s3_uri: str = "",
    approval_state: AgentApprovalState = AgentApprovalState.NOT_REQUIRED,
    retention_days: int = DEFAULT_RUN_CONTEXT_RETENTION_DAYS,
    occurred_at: datetime | None = None,
) -> RunContextEvent:
    """
    Build one validated, idempotently identified run-context event.

    Args:
        parent_run_id: Stable supervisor correlation UUID.
        external_run_id: Airflow or operator run ID.
        phase: Bounded lifecycle phase.
        requester: Calling system identity.
        status: Current parent or specialist status.
        selected_specialist: Policy-selected specialist when available.
        task_type: Policy-selected task when available.
        task_id: Optional child handoff UUID.
        alert_id: Optional exact alert UUID.
        alert_key: Optional canonical system alert key.
        alert_display_id: Optional human-facing Alert Ref.
        context_references: Explicit non-secret source references.
        evidence_references: Deterministic evidence references.
        decision_facts: Bounded policy-owned decisions.
        report_s3_uri: Optional persisted report URI.
        approval_state: Current human approval state.
        retention_days: Temporary context retention period.
        occurred_at: Optional deterministic timestamp override.

    Returns:
        Validated RunContextEvent.

    Raises:
        ValueError: If retention exceeds the bounded policy.
    """
    if not 1 <= retention_days <= MAX_RUN_CONTEXT_RETENTION_DAYS:
        raise ValueError(
            f"Run context retention_days must be between 1 and {MAX_RUN_CONTEXT_RETENTION_DAYS}."
        )

    event_time         = occurred_at or datetime.now(timezone.utc)
    normalized_context = list(context_references or [])
    normalized_evidence = list(evidence_references or [])
    normalized_decision = dict(decision_facts or {})
    semantic_payload   = {
        "parent_run_id": str(parent_run_id),
        "external_run_id": external_run_id,
        "phase": phase.value,
        "status": status.value,
        "selected_specialist": selected_specialist,
        "task_type": task_type,
        "task_id": str(task_id or ""),
        "alert_id": str(alert_id or ""),
        "alert_key": alert_key,
        "alert_display_id": alert_display_id,
        "context_references": [item.model_dump(mode="json") for item in normalized_context],
        "evidence_references": [item.model_dump(mode="json") for item in normalized_evidence],
        "decision_facts": normalized_decision,
        "report_s3_uri": report_s3_uri,
        "approval_state": approval_state.value,
    }
    event = RunContextEvent(
        context_event_id=derive_run_context_event_id(parent_run_id, phase),
        parent_run_id=parent_run_id,
        external_run_id=external_run_id,
        event_sequence=RUN_CONTEXT_SEQUENCE[phase.value],
        phase=phase,
        occurred_at=event_time,
        expires_at=event_time + timedelta(days=retention_days),
        requester=requester,
        status=status,
        selected_specialist=selected_specialist,
        task_type=task_type,
        task_id=task_id,
        alert_id=alert_id,
        alert_key=alert_key,
        alert_display_id=alert_display_id,
        context_references=normalized_context,
        evidence_references=normalized_evidence,
        decision_facts=normalized_decision,
        report_s3_uri=report_s3_uri,
        approval_state=approval_state,
        content_sha256=context_content_sha256(semantic_payload),
    )

    logger.info(
        "Built run context event | parent_run_id=%s phase=%s event_id=%s",
        parent_run_id,
        phase.value,
        event.context_event_id,
    )

    return event


def build_incident_memory_record(
    parent_run_id: UUID,
    outcome_status: AgentTaskStatus,
    specialist_name: str,
    task_type: str,
    summary: str,
    alert_id: UUID | None = None,
    alert_key: str = "",
    alert_display_id: str = "",
    evidence_references: list[EvidenceReference] | None = None,
    decision_facts: dict[str, Any] | None = None,
    report_s3_uri: str = "",
    approval_state: AgentApprovalState = AgentApprovalState.NOT_REQUIRED,
    memory_type: IncidentMemoryType = IncidentMemoryType.INVESTIGATION_OUTCOME,
    resolution_reference: str = "",
    recorded_at: datetime | None = None,
) -> IncidentMemoryRecord:
    """
    Build one durable, idempotently identified incident-memory record.

    Args:
        parent_run_id: Stable supervisor correlation UUID.
        outcome_status: Terminal parent or specialist status.
        specialist_name: Specialist responsible for the outcome.
        task_type: Completed bounded task type.
        summary: Deterministic operator-facing outcome.
        alert_id: Optional exact alert UUID.
        alert_key: Optional canonical system alert key.
        alert_display_id: Optional human-facing Alert Ref.
        evidence_references: Durable evidence pointers.
        decision_facts: Bounded policy-owned decisions.
        report_s3_uri: Optional persisted report URI.
        approval_state: Human approval state at persistence time.
        memory_type: Investigation or resolution record category.
        resolution_reference: Optional approval or execution reference.
        recorded_at: Optional deterministic timestamp override.

    Returns:
        Validated IncidentMemoryRecord.
    """
    alert_reference     = alert_key or alert_display_id or str(alert_id or "")
    normalized_evidence = list(evidence_references or [])
    normalized_decision = dict(decision_facts or {})
    semantic_payload    = {
        "parent_run_id": str(parent_run_id),
        "memory_type": memory_type.value,
        "alert_id": str(alert_id or ""),
        "alert_key": alert_key,
        "alert_display_id": alert_display_id,
        "outcome_status": outcome_status.value,
        "specialist_name": specialist_name,
        "task_type": task_type,
        "summary": " ".join(summary.split()),
        "evidence_references": [item.model_dump(mode="json") for item in normalized_evidence],
        "decision_facts": normalized_decision,
        "report_s3_uri": report_s3_uri,
        "approval_state": approval_state.value,
        "resolution_reference": resolution_reference,
    }
    memory_key = context_content_sha256(
        {
            "parent_run_id": str(parent_run_id),
            "memory_type": memory_type.value,
            "alert_reference": alert_reference,
        }
    )
    record = IncidentMemoryRecord(
        memory_id=derive_incident_memory_id(parent_run_id, memory_type, alert_reference),
        memory_key=memory_key,
        parent_run_id=parent_run_id,
        recorded_at=recorded_at or datetime.now(timezone.utc),
        memory_type=memory_type,
        alert_id=alert_id,
        alert_key=alert_key,
        alert_display_id=alert_display_id,
        outcome_status=outcome_status,
        specialist_name=specialist_name,
        task_type=task_type,
        summary=summary,
        evidence_references=normalized_evidence,
        decision_facts=normalized_decision,
        report_s3_uri=report_s3_uri,
        approval_state=approval_state,
        resolution_reference=resolution_reference,
        content_sha256=context_content_sha256(semantic_payload),
    )

    logger.info(
        "Built incident memory record | parent_run_id=%s memory_id=%s type=%s",
        parent_run_id,
        record.memory_id,
        memory_type.value,
    )

    return record
