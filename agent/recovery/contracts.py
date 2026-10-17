"""Typed recovery boundaries. Clients cannot supply warehouse facts or commands."""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

RUNBOOKS = (
    "replay_raw_partition", "rebuild_derived_partition", "replay_and_rebuild_partition",
)
RECOVERY_DAG_ID = "90_05_dag_dq_incident_recovery"
RECONCILIATION_DAG_ID = "90_06_dag_dq_recovery_reconciliation"
SYSTEM_REQUESTER = "datasentry_recovery"
RECOVERY_POLICY_VERSION = "orders-recovery-v1"


def canonical_hash(value: Any) -> str:
    """Hash a JSON-compatible immutable contract, never a credential or runtime env."""
    content = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RecoveryProposal(StrictContract):
    """Request a server-selected plan; no source, target, model, or SQL override."""
    reason: str = Field(min_length=5, max_length=1000)
    expected_incident_revision: int = Field(ge=1)


class RecoveryExecutionCreate(StrictContract):
    """Confirm the exact plan and approval version previously reviewed."""
    actor: str = Field(min_length=1, max_length=200)
    expected_plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_approval_version: int = Field(ge=1)
    confirm_scope: Literal[True]

    @field_validator("confirm_scope", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("Explicit scope confirmation is required.")
        return value

    @field_validator("actor")
    @classmethod
    def human_actor(cls, value: str) -> str:
        if value.casefold() in {SYSTEM_REQUESTER, "system", "airflow", "agentic_triage"}:
            raise ValueError("An operator identity is required, not a system identity.")
        return value


class RecoveryPlan(StrictContract):
    """An immutable, single-date execution contract, separate from approval state."""
    plan_id: UUID
    plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    policy_version: Literal[RECOVERY_POLICY_VERSION] = RECOVERY_POLICY_VERSION
    runbook: Literal["replay_raw_partition", "rebuild_derived_partition", "replay_and_rebuild_partition"]
    runbook_version: Literal[1] = 1
    alert_ref: str
    alert_key: str
    incident_revision: int = Field(ge=1)
    incident_episode: int = Field(ge=1)
    dt: date
    scope: dict[str, Any]
    targets: list[str] = Field(min_length=1, max_length=3)
    source_manifest: dict[str, Any]
    contract_hash: str
    model_hash: str
    pipeline_model_hash: str
    publication_revision: int = Field(ge=0)
    publication_watermark: str
    input_fingerprint: str
    partition_fingerprint: str
    facts_hash: str
    verification_scope: dict[str, Any]
    validation: list[str]
    rollback: list[str]
    reason: str
    created_at: datetime
    approval_request_id: str


class RecoveryExecution(StrictContract):
    """Execution transport, business verification, and closure remain distinct."""
    execution_id: UUID
    plan_id: UUID
    plan_hash: str
    approval_request_id: str
    alert_ref: str
    dag_id: Literal[RECOVERY_DAG_ID] = RECOVERY_DAG_ID
    dag_run_id: str
    actor: str
    status: Literal["queued", "running", "succeeded", "failed", "unknown"]
    trigger_state: Literal["pending", "claimed", "accepted", "unknown", "rejected"] = "pending"
    validation_status: Literal["not_checked", "pass", "blocked", "error"] = "not_checked"
    created_at: datetime
    finished_at: datetime | None = None
    validation: dict[str, Any] | None = None
    error_code: str = ""
    synchronization_pending: bool = True


class RecoveryPlanResponse(StrictContract):
    plan: RecoveryPlan
    approval: dict[str, Any]
    execution: RecoveryExecution | None = None


class RecoveryPreview(StrictContract):
    alert_ref: str
    incident_revision: int
    eligible: bool
    available: bool
    runbook: str | None = None
    scope: dict[str, Any]
    targets: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    observations: dict[str, Any] = Field(default_factory=dict)
    source: dict[str, Any] | None = None
    latest_plan_id: str | None = None


def choose_runbook(*, table: str, source_valid: bool, raw_matches: bool,
                   raw_valid: bool, derived_matches: bool, kind: str = "dq_failure",
                   derived_present: bool = False) -> str | None:
    """Select the smallest proven-safe action; never infer source validity from an LLM."""
    if kind != "dq_failure" or not source_valid:
        return None
    if table == "dq.raw_orders":
        if raw_matches:
            return None
        # Replacing raw must not leave existing dependents built from bad input.
        return "replay_and_rebuild_partition" if derived_present else "replay_raw_partition"
    if table not in {"dq.stg_orders", "dq.fct_orders_daily"}:
        return None
    if not raw_matches:
        return "replay_and_rebuild_partition"
    if raw_valid and not derived_matches:
        return "rebuild_derived_partition"
    return None
