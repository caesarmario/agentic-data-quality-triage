####
## Control Plane Supervisor Models for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Typed request, route, and result models for deterministic specialist orchestration."""

# --- Importing Libraries
from __future__ import annotations

import re
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent.specialists.contracts import AgentTaskStatus, SupervisorState


# --- Defining Safe Storage Patterns
SAFE_S3_URI = re.compile(
    r"^s3://[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]"
    r"(?:/[A-Za-z0-9][A-Za-z0-9._/-]{0,2000})?$"
)
SAFE_S3_BUCKET = re.compile(
    r"^(?=.{3,63}$)(?!.*\.\.)(?!\d+\.\d+\.\d+\.\d+$)"
    r"[a-z0-9][a-z0-9.-]*[a-z0-9]$"
)
SAFE_ARTIFACT_PREFIX = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,199}$")


# --- Defining Enumerations
class SupervisorIntent(str, Enum):
    """Represent the bounded intents accepted by the first supervisor runtime."""

    AUTO                 = "auto"
    TRIAGE_ALERT         = "triage_alert"
    ASSET_CONTEXT        = "asset_context"
    BLAST_RADIUS         = "blast_radius"
    TRUSTED_ASSET_SEARCH = "trusted_asset_search"
    REVIEW_SQL           = "review_sql"
    SCHEMA_DRIFT_ASSESSMENT = "schema_drift_assessment"


class SupervisorExecutionMode(str, Enum):
    """Select backward-compatible single handoff or opt-in bounded fan-out."""

    SINGLE = "single"
    FANOUT = "fanout"


# --- Defining Request And Result Models
class SupervisorRequest(BaseModel):
    """
    Carry one bounded operator request into deterministic supervisor policy.

    Attributes:
        intent: Explicit intent or deterministic auto classification.
        question: Optional operator wording used only for keyword classification.
        alert_id: Optional alert UUID for incident triage.
        alert_key: Optional system alert key or human-facing Alert Ref.
        qualified_name: Optional exact database.table metadata asset.
        query: Optional bounded trusted-asset search query.
        domain: Optional metadata domain filter.
        data_layer: Optional raw, staging, or mart filter.
        certification_status: Optional metadata certification filter.
        lifecycle_status: Optional metadata lifecycle filter.
        sql_proposal: SQL statement reviewed but never executed by the specialist.
        sql_purpose: Optional single-line operator reason for the SQL proposal.
        sql_hard_limit: Maximum result rows applied by the guarded SQL boundary.
        sql_require_date_filter: Whether known large tables require date predicates.
        sql_max_scan_bytes: Maximum conservative active-part scan upper bound.
        schema_run_id: Exact persisted schema detector DagRun identifier.
        schema_finding_limit: Maximum persisted drift findings returned.
        result_limit: Maximum metadata search results.
        max_depth: Maximum dbt lineage traversal depth.
        max_nodes: Maximum dbt lineage nodes.
        confidence_threshold: Triage evidence-loop confidence target.
        max_evidence_iterations: Maximum extra triage evidence loops.
        manifest_s3_uri: Optional dbt manifest artifact URI.
        artifacts_bucket: Optional report artifact bucket.
        artifacts_prefix: Report artifact prefix.
        requester: Calling interface or system identity.
        execution_mode: Single handoff by default or explicit bounded fan-out.
        max_workers: Maximum worker tasks allowed in one immutable plan.
        max_concurrency: Maximum worker tasks that may run at the same time.
        allow_external_llm: Explicit permission for policy-selected external routes.
        max_handoffs: Maximum specialist handoffs for the run.
        max_retries: Maximum retries allowed per handoff.
        max_model_calls: Maximum external provider attempts for the run.
        token_budget: Aggregate model token budget.
        estimated_cost_budget_usd: Aggregate model cost budget.
        latency_budget_ms: Aggregate specialist latency budget.
    """

    model_config = ConfigDict(extra="forbid")

    intent: SupervisorIntent               = SupervisorIntent.AUTO
    question: str                          = Field(default="", max_length=1_000)
    alert_id: str                          = Field(default="", max_length=36)
    alert_key: str                         = Field(default="", max_length=500)
    qualified_name: str                    = Field(default="", max_length=200)
    query: str                             = Field(default="", max_length=120)
    domain: str                            = Field(default="", max_length=80)
    data_layer: str                        = Field(default="", max_length=40)
    certification_status: str              = Field(default="", max_length=40)
    lifecycle_status: str                  = Field(default="", max_length=40)
    sql_proposal: str                      = Field(default="", max_length=20_000)
    sql_purpose: str                       = Field(default="", max_length=500)
    sql_hard_limit: int                    = Field(default=100, ge=1, le=1_000)
    sql_require_date_filter: bool          = True
    sql_max_scan_bytes: int                = Field(
        default=1024 * 1024 * 1024,
        ge=1024 * 1024,
        le=1024 * 1024 * 1024 * 1024,
    )
    schema_run_id: str                     = Field(
        default="",
        max_length=250,
        pattern=r"^[A-Za-z0-9_.:+-]*$",
    )
    schema_finding_limit: int              = Field(default=50, ge=1, le=100)
    result_limit: int                      = Field(default=10, ge=1, le=25)
    max_depth: int                         = Field(default=5, ge=1, le=10)
    max_nodes: int                         = Field(default=100, ge=1, le=250)
    confidence_threshold: float            = Field(default=0.70, ge=0.10, le=0.95)
    max_evidence_iterations: int           = Field(default=2, ge=0, le=5)
    manifest_s3_uri: str                   = Field(default="", max_length=2_048)
    artifacts_bucket: str                  = Field(default="", max_length=100)
    artifacts_prefix: str                  = Field(default="agent-reports", max_length=200)
    requester: str                         = Field(default="airflow", min_length=1, max_length=100)
    execution_mode: SupervisorExecutionMode = SupervisorExecutionMode.SINGLE
    max_workers: int                       = Field(default=1, ge=1, le=10)
    max_concurrency: int                   = Field(default=1, ge=1, le=3)
    allow_external_llm: bool               = False
    max_handoffs: int                      = Field(default=1, ge=1, le=10)
    max_retries: int                       = Field(default=0, ge=0, le=2)
    max_model_calls: int                   = Field(default=3, ge=0, le=10)
    token_budget: int                      = Field(default=16_384, ge=0, le=64_000)
    estimated_cost_budget_usd: float       = Field(default=0.05, ge=0.0, le=0.15)
    latency_budget_ms: int                 = Field(default=300_000, ge=1_000, le=900_000)

    @field_validator(
        "question",
        "alert_id",
        "alert_key",
        "qualified_name",
        "query",
        "domain",
        "data_layer",
        "certification_status",
        "lifecycle_status",
        "sql_purpose",
        "schema_run_id",
        "manifest_s3_uri",
        "artifacts_bucket",
        "artifacts_prefix",
        "requester",
    )
    @classmethod
    def normalize_text(cls, value: str) -> str:
        """
        Normalize supervisor request text and reject multiline injection payloads.

        Args:
            value: Raw request text.

        Returns:
            Trimmed single-line value.

        Raises:
            ValueError: If the value contains line breaks.
        """
        normalized = value.strip()

        if "\n" in normalized or "\r" in normalized:
            raise ValueError("Supervisor request text must remain single-line.")

        return normalized

    @field_validator("sql_proposal")
    @classmethod
    def normalize_sql_proposal(cls, value: str) -> str:
        """
        Normalize a SQL proposal without forcing valid SQL onto one line.

        Args:
            value: Raw SQL proposal.

        Returns:
            Trimmed SQL text.

        Raises:
            ValueError: If the proposal contains a null byte.
        """
        normalized = value.strip()

        if "\x00" in normalized:
            raise ValueError("Supervisor SQL proposal cannot contain null bytes.")

        return normalized

    @model_validator(mode="after")
    def validate_explicit_intent_inputs(self) -> "SupervisorRequest":
        """
        Reject incomplete or ambiguous explicit supervisor requests early.

        Returns:
            Current validated request.

        Raises:
            ValueError: If explicit intent lacks its required identity or query.
        """
        if self.alert_id and self.alert_key:
            raise ValueError("Provide only one of alert_id or alert_key.")

        if self.intent == SupervisorIntent.TRIAGE_ALERT:
            if not self.alert_id and not self.alert_key:
                raise ValueError("triage_alert requires alert_id or alert_key.")

        if self.intent in {SupervisorIntent.ASSET_CONTEXT, SupervisorIntent.BLAST_RADIUS}:
            if not self.qualified_name:
                raise ValueError(f"{self.intent.value} requires qualified_name.")

        if self.intent == SupervisorIntent.TRUSTED_ASSET_SEARCH:
            if not self.query and not self.question:
                raise ValueError("trusted_asset_search requires query or question.")

        if self.intent == SupervisorIntent.REVIEW_SQL and not self.sql_proposal:
            raise ValueError("review_sql requires sql_proposal.")

        if self.intent == SupervisorIntent.SCHEMA_DRIFT_ASSESSMENT:
            if not self.schema_run_id or not self.qualified_name:
                raise ValueError(
                    "schema_drift_assessment requires schema_run_id and qualified_name."
                )

        if self.sql_proposal and self.intent not in {
            SupervisorIntent.AUTO,
            SupervisorIntent.REVIEW_SQL,
        }:
            raise ValueError("sql_proposal is accepted only for auto or review_sql intent.")

        if self.schema_run_id and self.intent not in {
            SupervisorIntent.AUTO,
            SupervisorIntent.SCHEMA_DRIFT_ASSESSMENT,
        }:
            raise ValueError(
                "schema_run_id is accepted only for auto or schema_drift_assessment intent."
            )

        if self.schema_run_id and not self.qualified_name:
            raise ValueError("schema_run_id requires qualified_name.")

        if self.intent == SupervisorIntent.AUTO and not any(
            (
                self.question,
                self.alert_id,
                self.alert_key,
                self.qualified_name,
                self.query,
                self.sql_proposal,
                self.schema_run_id,
            )
        ):
            raise ValueError("auto supervisor intent requires bounded request context.")

        if self.execution_mode == SupervisorExecutionMode.SINGLE:
            if self.max_workers != 1 or self.max_concurrency != 1:
                raise ValueError(
                    "single execution mode requires max_workers=1 and max_concurrency=1."
                )

            if self.max_handoffs != 1:
                raise ValueError("single execution mode requires max_handoffs=1.")

        if self.execution_mode == SupervisorExecutionMode.FANOUT:
            if self.max_workers < 2:
                raise ValueError("fanout execution mode requires at least two workers.")

            if self.max_handoffs < self.max_workers:
                raise ValueError("fanout max_handoffs must cover every planned worker.")

            if self.max_concurrency > self.max_workers:
                raise ValueError("max_concurrency cannot exceed max_workers.")

        if not self.allow_external_llm and self.max_model_calls > 0:
            # Positive limits describe capacity only. Runtime permission remains
            # explicitly false and forces provider routes to local fallback.
            pass

        if self.manifest_s3_uri and not SAFE_S3_URI.fullmatch(self.manifest_s3_uri):
            raise ValueError("manifest_s3_uri must use a safe s3://bucket/path format.")

        if (
            self.manifest_s3_uri
            and ".." in self.manifest_s3_uri.removeprefix("s3://").split("/")
        ):
            raise ValueError("manifest_s3_uri cannot contain parent-directory traversal.")

        if self.artifacts_bucket and not SAFE_S3_BUCKET.fullmatch(self.artifacts_bucket):
            raise ValueError("artifacts_bucket must be a valid S3 bucket name.")

        if not SAFE_ARTIFACT_PREFIX.fullmatch(self.artifacts_prefix):
            raise ValueError("artifacts_prefix contains unsupported characters.")

        if ".." in self.artifacts_prefix.split("/"):
            raise ValueError("artifacts_prefix cannot contain parent-directory traversal.")

        return self


class SupervisorRoute(BaseModel):
    """
    Describe one deterministic intent-to-specialist policy decision.

    Attributes:
        intent: Resolved bounded intent.
        specialist_name: Registered specialist selected by policy.
        task_type: Accepted specialist task type.
        rationale: Human-readable deterministic routing reason.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    intent: SupervisorIntent
    specialist_name: str
    task_type: str
    rationale: str = Field(min_length=1, max_length=500)


class SupervisorRunResult(BaseModel):
    """
    Return one auditable supervisor outcome without exposing hidden context.

    Attributes:
        status: Parent run terminal status.
        parent_run_id: Stable supervisor correlation UUID.
        requested_intent: Intent supplied by the caller.
        resolved_intent: Intent selected by deterministic policy.
        selected_specialist: Registered specialist selected for the handoff.
        task_type: Specialist task type.
        task_id: Optional child handoff identifier.
        final_response: Bounded operator-facing response.
        supervisor_state: Explicit handoff, budget, result, and approval state.
        failure_isolated: Whether a specialist failure was contained at the boundary.
        audit_summary: Bounded parent-run audit expectations.
        execution_mode: Runtime mode used for this result.
        execution_plan_hash: Deterministic fan-out plan identity when applicable.
        worker_count: Number of specialist worker tasks retained by the run.
        aggregation: Bounded fan-in summary for fan-out runs.
    """

    model_config = ConfigDict(extra="forbid")

    status: AgentTaskStatus
    parent_run_id: UUID
    requested_intent: SupervisorIntent
    resolved_intent: SupervisorIntent | None = None
    selected_specialist: str                = ""
    task_type: str                           = ""
    task_id: UUID | None                     = None
    final_response: str                      = Field(default="", max_length=20_000)
    supervisor_state: SupervisorState
    failure_isolated: bool                   = False
    audit_summary: dict[str, Any]             = Field(default_factory=dict)
    execution_mode: SupervisorExecutionMode  = SupervisorExecutionMode.SINGLE
    execution_plan_hash: str                 = Field(default="", max_length=64)
    worker_count: int                        = Field(default=1, ge=0, le=10)
    aggregation: dict[str, Any]              = Field(default_factory=dict)
