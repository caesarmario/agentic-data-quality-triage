"""Public contracts for human closure. Clients never provide validation facts."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RevalidationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=100)

    @field_validator("actor", "idempotency_key")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Tidak boleh kosong.")
        return value.strip()


class IncidentResolve(RevalidationCreate):
    resolution_request_id: UUID
    expected_incident_revision: int = Field(ge=1)
    reason: str = Field(min_length=5, max_length=1000)

    @field_validator("reason")
    @classmethod
    def reason_length(cls, value: str) -> str:
        value = value.strip()
        if not 5 <= len(value) <= 1000:
            raise ValueError("Alasan harus berisi 5 sampai 1.000 karakter.")
        return value


class RevalidationResult(BaseModel):
    status: Literal["pass", "blocked"]
    expected: Any = None
    observed: Any = None
    summary: str
    evidence_references: list[str] = Field(default_factory=list)
    contract_hash: str
    data_watermark: str = ""
    schema_hash: str = ""
    observed_at: datetime | None = None
    checks: list[dict[str, Any]] = Field(default_factory=list)


class RevalidationRequest(BaseModel):
    request_id: UUID
    alert_ref: str
    incident_revision: int
    status: Literal["queued", "running", "pass", "blocked", "error", "expired"]
    created_at: datetime
    deadline_at: datetime
    finished_at: datetime | None = None
    expires_at: datetime | None = None
    dag_run_id: str
    scope: dict[str, Any]
    result: RevalidationResult | None = None
    error_code: str = ""


class ResolutionPreview(BaseModel):
    alert_ref: str
    incident_revision: int
    status: str
    scope: dict[str, Any]
    blockers: list[str] = Field(default_factory=list)
    available: bool
    request: RevalidationRequest | None = None
    resolution: dict[str, Any] | None = None
    synchronization_pending: bool = False


class ResolutionDecision(BaseModel):
    alert_ref: str
    status: Literal["resolved"]
    resolved_by: str
    resolved_at: datetime
    resolution_reason: str
    resolution_request_id: UUID
    validation_dag_run_id: str
    evidence_references: list[str]
    contract_hash: str
    incident_revision: int = Field(ge=1)
    expected: Any = None
    observed: Any = None
    summary: str
    synchronization_pending: bool
