####
## Agent State Models for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pipelines.common.logging import logger


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


class EvidenceType(str, Enum):
    """
    Evidence categories collected during triage.

    Values:
        SQL_RESULT: Evidence returned from a guarded ClickHouse query.
        DQ_HISTORY: Historical DQ check context.
        LINEAGE: dbt lineage or artifact-derived context.
        PIPELINE_RUN: Pipeline execution status context.
        ARTIFACT: S3 artifact or report context.
        NOTE: Agent-authored observation that references other evidence.
    """

    SQL_RESULT   = "sql_result"
    DQ_HISTORY   = "dq_history"
    LINEAGE      = "lineage"
    PIPELINE_RUN = "pipeline_run"
    ARTIFACT     = "artifact"
    NOTE         = "note"


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

        logger.info("Building alert state from ClickHouse row | alert_key=%s", payload.get("alert_key"))

        return cls.model_validate(payload)


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
        confidence: Final confidence score from 0.0 to 1.0.
        recommended_actions: Non-mutating recommended next steps.
        approval_gated_actions: Actions that require user approval.
        residual_risks: Remaining risks or open questions.
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
    confidence: float                              = Field(ge=0.0, le=1.0)
    recommended_actions: list[str]                 = Field(default_factory=list)
    approval_gated_actions: list[ApprovalGatedAction] = Field(default_factory=list)
    residual_risks: list[str]                      = Field(default_factory=list)
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
        hypotheses: Candidate hypotheses generated so far.
        report: Final triage report.
        audit_events: In-memory tool audit events.
        errors: Non-fatal errors collected during triage.
        confidence_threshold: Minimum confidence required to finalize without extra evidence.
        evidence_iterations: Number of evidence-gathering loops completed.
        max_evidence_iterations: Maximum allowed evidence loops.
    """

    agent_run_id: UUID                              = Field(default_factory=uuid4)
    alert_id: UUID | None                           = None
    alert_key: str                                  = ""
    alert: Alert | None                             = None
    evidence: list[EvidenceItem]                    = Field(default_factory=list)
    hypotheses: list[Hypothesis]                    = Field(default_factory=list)
    report: TriageReport | None                     = None
    audit_events: list[ToolAuditEvent]              = Field(default_factory=list)
    errors: list[str]                               = Field(default_factory=list)
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
