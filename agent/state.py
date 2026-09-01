####
## Agent State Models for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pipelines.common.alert_identity import build_alert_ref
from pipelines.common.logging import logger


# --- Defining Classes
class AlertStatus(str, Enum):
    """
    Supported alert lifecycle statuses.

    Values:
        OPEN: Alert is active and needs investigation.
        ACKNOWLEDGED: Alert has been seen by a human or automation.
        TRIAGED: Alert has a completed triage report.
        RESOLVED: Alert has been remediated or accepted.
    """

    OPEN         = "open"
    ACKNOWLEDGED = "acknowledged"
    TRIAGED      = "triaged"
    RESOLVED     = "resolved"


class Severity(str, Enum):
    """
    Supported severity labels used by DQ checks and alerts.

    Values:
        CRITICAL: High-impact data issue that likely needs action.
        WARNING: Lower-impact issue that still needs visibility.
        INFO: Informational finding.
    """

    CRITICAL = "critical"
    WARNING  = "warning"
    INFO     = "info"


class IncidentComplexityTier(str, Enum):
    """
    Supported deterministic incident-complexity tiers.

    Values:
        LOW: Narrow investigation with no strong reasoning requirement.
        MODERATE: Multiple signals exist, but bounded normal reasoning is sufficient.
        HIGH: Cross-signal investigation that requires a strong reasoning attempt.
    """

    LOW      = "low"
    MODERATE = "moderate"
    HIGH     = "high"


class EvidenceType(str, Enum):
    """
    Evidence categories collected during triage.

    Values:
        SQL_RESULT: Evidence returned from a guarded ClickHouse query.
        DQ_HISTORY: Historical DQ check context.
        INCIDENT_HISTORY: Prior durable investigation outcomes for comparison.
        LINEAGE: dbt lineage or artifact-derived context.
        PIPELINE_RUN: Pipeline execution status context.
        SCHEMA_DRIFT: Persisted schema contract comparison evidence.
        ARTIFACT: S3 artifact or report context.
        NOTE: Agent-authored observation that references other evidence.
    """

    SQL_RESULT      = "sql_result"
    DQ_HISTORY      = "dq_history"
    INCIDENT_HISTORY = "incident_history"
    LINEAGE         = "lineage"
    PIPELINE_RUN    = "pipeline_run"
    SCHEMA_DRIFT    = "schema_drift"
    ARTIFACT        = "artifact"
    NOTE            = "note"


class EvidenceCategory(str, Enum):
    """
    Allowlisted evidence categories that an evidence plan may request.

    Values:
        CURRENT_PARTITION_ROW_COUNT: Guarded row count for the affected partition.
        DQ_HISTORY: Recent deterministic DQ check history.
        INCIDENT_HISTORY: Exact-match bounded prior investigation outcomes.
        PIPELINE_RUNS: Recent Airflow/pipeline execution status.
        DBT_LINEAGE: Upstream and downstream dbt lineage context.
        SCHEMA_DRIFT: Exact persisted schema snapshot and contract findings.
        RECENT_PARTITION_TREND: Guarded recent partition row-count trend.
    """

    CURRENT_PARTITION_ROW_COUNT = "current_partition_row_count"
    DQ_HISTORY                 = "dq_history"
    INCIDENT_HISTORY           = "incident_history"
    PIPELINE_RUNS              = "pipeline_runs"
    DBT_LINEAGE                = "dbt_lineage"
    SCHEMA_DRIFT               = "schema_drift"
    RECENT_PARTITION_TREND     = "recent_partition_trend"


class ToolStatus(str, Enum):
    """
    Tool execution status labels.

    Values:
        SUCCESS: Tool completed successfully.
        FAILED: Tool failed with an error.
        BLOCKED: Tool call was rejected by guardrails.
        SKIPPED: Tool call was intentionally skipped.
    """

    SUCCESS = "success"
    FAILED  = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class ApprovalActionType(str, Enum):
    """
    Approval-gated remediation action types.

    Values:
        BACKFILL: Trigger Airflow backfill dispatcher.
        RERUN_DBT: Rerun dbt transformations/tests.
        RERUN_DQ: Rerun profiling, DQ checks, and alert generation.
        CREATE_TICKET: Create a ticket in an external system.
        POST_NOTIFICATION: Post an external notification.
        ACKNOWLEDGE_ALERT: Mark an alert as acknowledged.
        RESOLVE_ALERT: Mark an alert as resolved.
    """

    BACKFILL          = "backfill"
    RERUN_DBT         = "rerun_dbt"
    RERUN_DQ          = "rerun_dq"
    CREATE_TICKET     = "create_ticket"
    POST_NOTIFICATION = "post_notification"
    ACKNOWLEDGE_ALERT = "acknowledge_alert"
    RESOLVE_ALERT     = "resolve_alert"


class Alert(BaseModel):
    """
    Alert context loaded from dq.alerts.

    Attributes:
        alert_id: ClickHouse alert UUID.
        alert_key: Stable idempotency key for the alert.
        alert_display_id: Short human-facing alert reference for operators.
        created_at: UTC timestamp when the alert was created.
        updated_at: UTC timestamp when the alert was last updated.
        status: Alert lifecycle status.
        alert_type: Alert category such as dq_failure.
        severity: Alert severity.
        table_name: Affected table name.
        metric: DQ check or metric that triggered the alert.
        dt: Business date associated with the issue.
        dimension: Optional affected dimension or checked column.
        observed_value: Observed metric value.
        expected_value: Expected metric value.
        threshold_value: Threshold used by the check.
        source_check_run_id: Source dq_check_results UUID.
        details: Structured details from the alert row.
        report_s3_uri: Optional report artifact URI.
    """

    model_config = ConfigDict(use_enum_values=True)

    alert_id: UUID | None              = None
    alert_key: str
    alert_display_id: str              = ""
    created_at: datetime | None        = None
    updated_at: datetime | None        = None
    status: AlertStatus | str          = AlertStatus.OPEN
    alert_type: str                    = "dq_failure"
    severity: Severity | str
    table_name: str
    metric: str
    dt: date | None                    = None
    dimension: str                     = ""
    observed_value: float | None       = None
    expected_value: float | None       = None
    threshold_value: float | None      = None
    source_check_run_id: UUID | None   = None
    details: dict[str, Any]            = Field(default_factory=dict)
    report_s3_uri: str                 = ""

    @classmethod
    def from_clickhouse_dict(cls, row: dict[str, Any]) -> "Alert":
        """
        Build an Alert model from a ClickHouse row dictionary.

        Args:
            row: Dictionary returned by a ClickHouse alert query.

        Returns:
            Alert model with details_json parsed into details.
        """
        payload = dict(row)

        if "details_json" in payload and "details" not in payload:
            payload["details"] = parse_json_object(payload.pop("details_json"))

        if not payload.get("alert_display_id"):
            payload["alert_display_id"] = build_alert_ref(
                alert_key=str(payload.get("alert_key") or ""),
                dt=payload.get("dt"),
            )

        logger.info("Building alert state from ClickHouse row | alert_key=%s", payload.get("alert_key"))

        return cls.model_validate(payload)

    @model_validator(mode="after")
    def ensure_alert_display_id(self) -> "Alert":
        """
        Ensure every Alert has a human-facing display identifier.

        Returns:
            Current Alert instance with alert_display_id populated.
        """
        if not self.alert_display_id:
            self.alert_display_id = build_alert_ref(alert_key=self.alert_key, dt=self.dt)

        return self

    @property
    def is_schema_drift(self) -> bool:
        """
        Identify alerts produced by deterministic schema contract checks.

        Returns:
            True when alert type or metric identifies a schema drift incident.
        """
        return (
            self.alert_type.strip().lower() == "schema_drift"
            or self.metric.strip().lower() == "schema_contract_drift"
        )


class EvidenceRequest(BaseModel):
    """
    Describe one bounded evidence category requested by the planner.

    Attributes:
        category: Allowlisted evidence category; never raw SQL or a command.
        reason: Short explanation of why this evidence helps the investigation.
        priority: Relative collection priority where 1 is highest.
        required: Whether deterministic policy requires this category.
    """

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    category: EvidenceCategory
    reason: str              = Field(min_length=8, max_length=320)
    priority: int            = Field(default=3, ge=1, le=5)
    required: bool           = False


class EvidencePlan(BaseModel):
    """
    Typed and auditable plan used before deterministic evidence collection.

    Attributes:
        investigation_question: Human-readable question the evidence should answer.
        requests: Unique allowlisted evidence requests in collection order.
        planner_source: Whether the plan came from an LLM or a safe fallback.
        policy_added_categories: Categories added by deterministic policy.
        policy_adjusted_categories: Categories whose priority was corrected by policy.
        llm_route: Model route requested by the planner.
        llm_provider: Provider that handled the planning request.
        llm_model: Model selected by the route.
        created_at: UTC timestamp when the plan was finalized.
    """

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    investigation_question: str = Field(min_length=8, max_length=320)
    requests: list[EvidenceRequest] = Field(min_length=1, max_length=6)
    planner_source: Literal[
        "llm",
        "llm_with_policy",
        "provider_fallback",
        "error_fallback",
    ] = "provider_fallback"
    policy_added_categories: list[EvidenceCategory] = Field(default_factory=list)
    policy_adjusted_categories: list[EvidenceCategory] = Field(default_factory=list)
    llm_route: str             = ""
    llm_provider: str          = ""
    llm_model: str             = ""
    created_at: datetime       = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def validate_unique_categories(self) -> "EvidencePlan":
        """
        Reject duplicate categories before any collector is selected.

        Returns:
            Current plan when request categories are unique.

        Raises:
            ValueError: If one evidence category appears more than once.
        """
        categories = [str(item.category) for item in self.requests]

        if len(categories) != len(set(categories)):
            raise ValueError("Evidence plan categories must be unique")

        return self


class EvidenceItem(BaseModel):
    """
    One evidence item collected by a triage tool.

    Attributes:
        evidence_id: Stable UUID-like evidence identifier.
        evidence_type: Evidence category.
        tool_name: Tool that produced the evidence.
        description: Why this evidence was collected.
        query: SQL query or logical request used by the tool.
        rows: Tabular evidence rows, already JSON-serializable.
        summary: Human-readable observation from the evidence.
        row_count: Number of rows returned or represented.
        s3_uri: Optional S3 artifact URI.
        created_at: UTC timestamp when evidence was created.
        supports_hypotheses: Hypothesis ids supported by this evidence.
        contradicts_hypotheses: Hypothesis ids contradicted by this evidence.
    """

    evidence_id: str                     = Field(default_factory=lambda: str(uuid4()))
    evidence_type: EvidenceType | str
    tool_name: str
    description: str
    query: str                           = ""
    rows: list[dict[str, Any]]           = Field(default_factory=list)
    summary: str                         = ""
    row_count: int                       = 0
    s3_uri: str                          = ""
    created_at: datetime                 = Field(default_factory=lambda: datetime.now(timezone.utc))
    supports_hypotheses: list[str]       = Field(default_factory=list)
    contradicts_hypotheses: list[str]    = Field(default_factory=list)

    @model_validator(mode="after")
    def sync_row_count(self) -> "EvidenceItem":
        """
        Keep row_count aligned with rows when row_count is not explicitly set.

        Returns:
            Current EvidenceItem instance with row_count populated.
        """
        if self.row_count == 0 and self.rows:
            self.row_count = len(self.rows)

        return self


class Hypothesis(BaseModel):
    """
    Candidate explanation for an alert.

    Attributes:
        hypothesis_id: Stable hypothesis identifier.
        title: Short hypothesis title.
        description: Detailed explanation of what may have happened.
        likelihood: Relative likelihood from 0.0 to 1.0.
        confidence: Evidence-backed confidence from 0.0 to 1.0.
        root_cause_category: Category such as missing_partition or upstream_late_arrival.
        supporting_evidence_ids: Evidence ids that support the hypothesis.
        opposing_evidence_ids: Evidence ids that weaken the hypothesis.
        recommended_action: Recommended next action if this hypothesis is true.
        framing_source: Whether wording came from deterministic policy or a validated LLM proposal.
        framing_notes: Policy notes applied while accepting or rejecting model-authored wording.
    """

    hypothesis_id: str                 = Field(default_factory=lambda: str(uuid4()))
    title: str
    description: str
    likelihood: float                  = Field(ge=0.0, le=1.0)
    confidence: float                  = Field(ge=0.0, le=1.0)
    root_cause_category: str           = "unknown"
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    opposing_evidence_ids: list[str]   = Field(default_factory=list)
    recommended_action: str            = ""
    framing_source: Literal["deterministic", "llm"] = "deterministic"
    framing_notes: list[str]                         = Field(default_factory=list)


class HypothesisFraming(BaseModel):
    """
    Describe how candidate hypothesis wording was produced and policy-enforced.

    Attributes:
        source: Final framing path used for the current hypothesis set.
        requested_route: Model route requested by the hypothesis specialist.
        provider: Provider that handled the framing request or fallback.
        model: Model selected by the resolved route.
        accepted_categories: Root-cause categories whose model wording passed policy.
        policy_adjustments: Sanitized changes made by deterministic policy.
        created_at: UTC timestamp when framing completed.
    """

    model_config = ConfigDict(extra="forbid")

    source: Literal[
        "llm",
        "llm_with_policy",
        "provider_fallback",
        "error_fallback",
    ] = "provider_fallback"
    requested_route: str             = ""
    provider: str                    = ""
    model: str                       = ""
    accepted_categories: list[str]   = Field(default_factory=list)
    policy_adjustments: list[str]    = Field(default_factory=list)
    created_at: datetime             = Field(default_factory=lambda: datetime.now(timezone.utc))


class ApprovalGatedAction(BaseModel):
    """
    Remediation action that requires explicit approval before execution.

    Attributes:
        action_type: Type of approval-gated action.
        reason: Why the action is recommended.
        target_dag_id: Optional Airflow DAG id for backfill/remediation.
        start_date: Optional inclusive start date for backfill.
        end_date: Optional inclusive end date for backfill.
        parameters: Extra parameters passed to the action executor.
        requires_approval: Whether approval is required. Defaults to True.
    """

    action_type: ApprovalActionType | str
    reason: str
    target_dag_id: str                 = ""
    start_date: date | None            = None
    end_date: date | None              = None
    parameters: dict[str, Any]         = Field(default_factory=dict)
    requires_approval: bool            = True

    @model_validator(mode="after")
    def validate_date_range(self) -> "ApprovalGatedAction":
        """
        Validate approval-gated action date ranges.

        Returns:
            Current action when date ranges are valid.

        Raises:
            ValueError: If end_date is earlier than start_date.
        """
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("Approval action end_date must be >= start_date")

        return self


class ToolAuditEvent(BaseModel):
    """
    In-memory representation of a tool audit event.

    Attributes:
        action: Logical action performed by the agent.
        tool_name: Tool name used for the action.
        status: Tool execution status.
        duration_ms: Tool execution duration in milliseconds.
        input_json: JSON-serializable input payload.
        output_json: JSON-serializable output payload.
        error_message: Optional error message.
        sql_hash: Optional SQL hash for query tools.
        row_count: Optional row count returned by the tool.
        created_at: UTC timestamp for the audit event.
    """

    action: str
    tool_name: str                     = ""
    status: ToolStatus | str
    duration_ms: int | None            = None
    input_json: dict[str, Any]         = Field(default_factory=dict)
    output_json: dict[str, Any]        = Field(default_factory=dict)
    error_message: str                 = ""
    sql_hash: str                      = ""
    row_count: int | None              = None
    created_at: datetime               = Field(default_factory=lambda: datetime.now(timezone.utc))


class IncidentComplexityAssessment(BaseModel):
    """
    Persist deterministic facts used to classify triage reasoning complexity.

    Attributes:
        tier: Final low, moderate, or high complexity tier.
        score: Additive deterministic complexity score.
        strong_reasoning_required: Whether policy requires a strong model attempt.
        reason_codes: Stable reasons that contributed to the score.
        deterministic_evidence_types: Trusted evidence types included in the assessment.
        hypothesis_count: Number of ranked hypotheses.
        top_hypothesis_gap: Confidence gap between the two highest hypotheses.
        contradiction_count: Explicit evidence or hypothesis contradiction references.
        lineage_asset_count: Unique directly related lineage assets found in evidence.
        schema_finding_count: Persisted schema findings included in evidence.
        unresolved_error_count: Non-fatal tool or route errors retained by the graph.
    """

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    tier: IncidentComplexityTier
    score: int                              = Field(ge=0, le=100)
    strong_reasoning_required: bool         = False
    reason_codes: tuple[str, ...]           = Field(default_factory=tuple, max_length=20)
    deterministic_evidence_types: tuple[str, ...] = Field(default_factory=tuple, max_length=20)
    hypothesis_count: int                   = Field(default=0, ge=0, le=100)
    top_hypothesis_gap: float | None        = Field(default=None, ge=0.0, le=1.0)
    contradiction_count: int                = Field(default=0, ge=0, le=10_000)
    lineage_asset_count: int                = Field(default=0, ge=0, le=10_000)
    schema_finding_count: int               = Field(default=0, ge=0, le=10_000)
    unresolved_error_count: int             = Field(default=0, ge=0, le=1_000)

    @model_validator(mode="after")
    def validate_strong_reasoning_tier(self) -> "IncidentComplexityAssessment":
        """
        Keep strong-reasoning policy aligned with the high-complexity tier.

        Returns:
            Current assessment when tier and strong-reasoning flag agree.

        Raises:
            ValueError: If a non-high tier claims strong reasoning or vice versa.
        """
        is_high = self.tier == IncidentComplexityTier.HIGH

        if self.strong_reasoning_required != is_high:
            raise ValueError(
                "Strong reasoning is required exactly when incident complexity is high."
            )

        if len(set(self.reason_codes)) != len(self.reason_codes):
            raise ValueError("Incident complexity reason codes must remain unique.")

        return self


class LlmRuntimeSummary(BaseModel):
    """
    Aggregate bounded LLM routing metadata retained by one triage report.

    The summary contains operational metadata only. It deliberately excludes
    prompts, credentials, hidden reasoning, and raw provider responses so the
    JSON report remains safe for operator-facing APIs and UI surfaces.

    Attributes:
        route_event_count: Number of LLM route evidence events in the run.
        requested_routes: Provider routes requested by deterministic policy.
        executed_routes: Routes that produced the retained response or fallback.
        providers: Provider names observed across route events.
        models: Model names observed across route events.
        external_model_used: Whether at least one non-heuristic provider returned output.
        heuristic_fallback_used: Whether any route completed through local heuristics.
        fallback_reasons: Sanitized reasons retained by fallback decisions.
        input_tokens: Aggregate provider-reported or estimated input tokens.
        output_tokens: Aggregate provider-reported or estimated output tokens.
        estimated_cost_usd: Aggregate estimated provider cost in USD.
        duration_ms: Aggregate route duration in milliseconds.
    """

    route_event_count: int              = Field(default=0, ge=0)
    requested_routes: list[str]         = Field(default_factory=list, max_length=20)
    executed_routes: list[str]          = Field(default_factory=list, max_length=20)
    providers: list[str]                = Field(default_factory=list, max_length=20)
    models: list[str]                   = Field(default_factory=list, max_length=20)
    external_model_used: bool           = False
    heuristic_fallback_used: bool       = False
    fallback_reasons: list[str]         = Field(default_factory=list, max_length=20)
    input_tokens: int                   = Field(default=0, ge=0)
    output_tokens: int                  = Field(default=0, ge=0)
    estimated_cost_usd: float           = Field(default=0.0, ge=0.0)
    duration_ms: int                    = Field(default=0, ge=0)


class TriageReport(BaseModel):
    """
    Final triage report generated for an alert.

    Attributes:
        agent_run_id: Agent run UUID.
        alert: Alert being investigated.
        summary: Executive summary.
        impact: Impact assessment.
        hypotheses: Ranked hypotheses.
        top_hypothesis: Most likely root cause hypothesis.
        evidence: Evidence items reviewed.
        evidence_plan: Bounded plan that selected deterministic evidence categories.
        hypothesis_framing: Audit metadata for model-assisted hypothesis wording.
        llm_runtime: Sanitized aggregate model-route usage retained by the report.
        complexity_assessment: Deterministic reasoning-complexity decision and facts.
        investigation_errors: Bounded non-fatal evidence or narrative gaps retained by the run.
        confidence: Final confidence score from 0.0 to 1.0.
        recommended_actions: Non-mutating recommended next steps.
        approval_gated_actions: Actions that require user approval.
        residual_risks: Remaining risks or open questions.
        report_id: Short operator-facing report identifier.
        markdown_report: Markdown report body.
        json_report_s3_uri: S3 URI for JSON report.
        markdown_report_s3_uri: S3 URI for Markdown report.
        created_at: UTC timestamp when the report was created.
    """

    agent_run_id: UUID
    alert: Alert
    summary: str
    impact: str
    hypotheses: list[Hypothesis]
    top_hypothesis: Hypothesis | None              = None
    evidence: list[EvidenceItem]                   = Field(default_factory=list)
    evidence_plan: EvidencePlan | None             = None
    hypothesis_framing: HypothesisFraming | None   = None
    llm_runtime: LlmRuntimeSummary                  = Field(default_factory=LlmRuntimeSummary)
    complexity_assessment: IncidentComplexityAssessment | None = None
    investigation_errors: list[str]                = Field(default_factory=list, max_length=20)
    confidence: float                              = Field(ge=0.0, le=1.0)
    recommended_actions: list[str]                 = Field(default_factory=list)
    approval_gated_actions: list[ApprovalGatedAction] = Field(default_factory=list)
    residual_risks: list[str]                      = Field(default_factory=list)
    report_id: str                                 = ""
    markdown_report: str                           = ""
    json_report_s3_uri: str                        = ""
    markdown_report_s3_uri: str                    = ""
    created_at: datetime                           = Field(default_factory=lambda: datetime.now(timezone.utc))


class TriageState(BaseModel):
    """
    Mutable LangGraph-compatible state for one alert triage run.

    Attributes:
        agent_run_id: Agent run UUID.
        alert_id: Optional alert UUID to load.
        alert_key: Optional stable alert key to load.
        alert: Loaded alert context.
        evidence: Evidence collected so far.
        evidence_plan: Typed plan controlling allowlisted evidence collection.
        hypotheses: Candidate hypotheses generated so far.
        hypothesis_framing: Metadata for bounded model-assisted hypothesis wording.
        report: Final triage report.
        audit_events: In-memory tool audit events.
        errors: Non-fatal errors collected during triage.
        runtime_contract_hash: Stable hash of evidence and side-effect runtime targets.
        confidence_threshold: Minimum confidence required to finalize without extra evidence.
        evidence_iterations: Number of evidence-gathering loops completed.
        max_evidence_iterations: Maximum allowed evidence loops.
    """

    agent_run_id: UUID                              = Field(default_factory=uuid4)
    alert_id: UUID | None                           = None
    alert_key: str                                  = ""
    alert: Alert | None                             = None
    evidence: list[EvidenceItem]                    = Field(default_factory=list)
    evidence_plan: EvidencePlan | None              = None
    hypotheses: list[Hypothesis]                    = Field(default_factory=list)
    hypothesis_framing: HypothesisFraming | None    = None
    report: TriageReport | None                     = None
    audit_events: list[ToolAuditEvent]              = Field(default_factory=list)
    errors: list[str]                               = Field(default_factory=list)
    runtime_contract_hash: str                      = Field(default="", pattern=r"^(?:[0-9a-f]{64})?$")
    confidence_threshold: float                     = Field(default=0.70, ge=0.0, le=1.0)
    evidence_iterations: int                        = Field(default=0, ge=0)
    max_evidence_iterations: int                    = Field(default=2, ge=0, le=10)

    @field_validator("alert_key")
    @classmethod
    def normalize_alert_key(cls, value: str) -> str:
        """
        Normalize alert_key values from CLI or UI inputs.

        Args:
            value: Raw alert key string.

        Returns:
            Trimmed alert key string.
        """
        return value.strip()

    @property
    def top_hypothesis(self) -> Hypothesis | None:
        """
        Return the highest-confidence hypothesis.

        Returns:
            Hypothesis with the highest confidence, or None when no hypotheses exist.
        """
        if not self.hypotheses:
            return None

        return max(self.hypotheses, key=lambda item: item.confidence)

    @property
    def should_collect_more_evidence(self) -> bool:
        """
        Decide whether the agent should run another evidence collection loop.

        Returns:
            True when top confidence is below threshold and iteration budget remains.
        """
        top = self.top_hypothesis

        if top is None:
            return self.evidence_iterations < self.max_evidence_iterations

        return top.confidence < self.confidence_threshold and self.evidence_iterations < self.max_evidence_iterations

    def add_evidence(self, item: EvidenceItem) -> "TriageState":
        """
        Add an evidence item to the state.

        Args:
            item: Evidence item collected by a tool.

        Returns:
            Current TriageState instance for chaining.
        """
        logger.info("Adding evidence to triage state | agent_run_id=%s evidence_id=%s", self.agent_run_id, item.evidence_id)
        self.evidence.append(item)

        return self

    def add_audit_event(self, event: ToolAuditEvent) -> "TriageState":
        """
        Add an audit event to the in-memory state.

        Args:
            event: Tool audit event.

        Returns:
            Current TriageState instance for chaining.
        """
        logger.info(
            "Adding audit event to triage state | agent_run_id=%s tool=%s status=%s",
            self.agent_run_id,
            event.tool_name,
            event.status,
        )
        self.audit_events.append(event)

        return self


# --- Defining Functions
def parse_json_object(value: str | dict[str, Any] | None) -> dict[str, Any]:
    """
    Parse a JSON object from a string or return an existing dictionary.

    Args:
        value: JSON string, dictionary, or None.

    Returns:
        Parsed dictionary. Invalid or non-object JSON returns an empty dictionary.
    """
    if value is None:
        return {}

    if isinstance(value, dict):
        return value

    try:
        parsed = json.loads(value)

    except json.JSONDecodeError:
        logger.warning("Failed to parse JSON object | value=%s", value[:200])
        return {}

    if not isinstance(parsed, dict):
        logger.warning("Parsed JSON is not an object | type=%s", type(parsed).__name__)
        return {}

    return parsed
