####
## API Schemas for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


# --- Defining Response Schemas
class HealthResponse(BaseModel):
    """
    Health response for the FastAPI backend-for-frontend.

    Attributes:
        status: API service status.
        service: Service name.
        version: API version string.
    """

    status: str  = "ok"
    service: str = "agentic-dq-api"
    version: str = "0.1.0"


class MessageResponse(BaseModel):
    """
    Generic API message response.

    Attributes:
        status: Request status.
        message: Human-readable message.
        details: Optional structured details.
    """

    status: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


# --- Defining Shared Read-Only Response Schemas
class AlertResponse(BaseModel):
    """
    Public alert contract shared by operator interfaces.

    Attributes:
        alert_id: Durable alert UUID.
        alert_key: Stable system identity used for idempotency and correlation.
        alert_display_id: Human-facing Alert Ref used by operators.
        created_at: First alert creation timestamp.
        updated_at: Latest lifecycle update timestamp.
        status: Current alert lifecycle status.
        alert_type: Alert category such as dq_failure or schema_drift.
        severity: Operator-facing incident priority.
        table_name: Fully qualified affected warehouse table.
        metric: DQ check or metric that raised the alert.
        dt: Affected business date.
        dimension: Optional affected dimension or column.
        observed_value: Observed check value.
        expected_value: Expected check value.
        threshold_value: Applied rule threshold.
        source_check_run_id: Optional source DQ check run UUID.
        details: Structured alert context available only on detail responses.
        report_s3_uri: Optional persisted triage report URI.
    """

    model_config = ConfigDict(extra="ignore")

    alert_id: UUID | None            = None
    alert_key: str                   = Field(min_length=1, max_length=500)
    alert_display_id: str            = Field(default="", max_length=40)
    created_at: datetime | None      = None
    updated_at: datetime | None      = None
    status: str                      = Field(default="open", max_length=40)
    alert_type: str                  = Field(default="dq_failure", max_length=80)
    severity: str                    = Field(max_length=40)
    table_name: str                  = Field(max_length=255)
    metric: str                      = Field(max_length=255)
    dt: date | None                  = None
    dimension: str                   = Field(default="", max_length=255)
    observed_value: float | None     = None
    expected_value: float | None     = None
    threshold_value: float | None    = None
    source_check_run_id: UUID | None = None
    details: dict[str, Any]          = Field(default_factory=dict)
    report_s3_uri: str               = Field(default="", max_length=2048)


class AlertListResponse(BaseModel):
    """
    Bounded alert-list response for UI, Discord, and API clients.

    Attributes:
        status: Request result status.
        alert_status: Applied alert lifecycle filter.
        dt: Optional applied business-date filter.
        limit: Hard result limit requested by the caller.
        row_count: Number of returned alert rows.
        alerts: Public alert summaries ordered by the alert tool.
        summary: Deterministic operator-facing result summary.
    """

    model_config = ConfigDict(extra="ignore")

    status: str                 = "success"
    alert_status: str           = Field(max_length=40)
    dt: date | None             = None
    limit: int                  = Field(ge=1, le=100)
    row_count: int              = Field(ge=0, le=100)
    alerts: list[AlertResponse] = Field(default_factory=list, max_length=100)
    summary: str                = Field(max_length=500)

    @model_validator(mode="after")
    def validate_result_bounds(self) -> "AlertListResponse":
        """
        Ensure count metadata and caller bounds match the returned alerts.

        Returns:
            Validated response when row count and limit are consistent.

        Raises:
            ValueError: If the response count is inconsistent or unbounded.
        """
        if self.row_count != len(self.alerts):
            raise ValueError("Alert row_count must equal the returned alert count.")

        if self.row_count > self.limit:
            raise ValueError("Alert response exceeds the requested result limit.")

        return self


class DailyCheckCountResponse(BaseModel):
    """
    Aggregated DQ check count for one outcome status.

    Attributes:
        status: DQ check outcome such as pass, warn, fail, or skip.
        count: Number of check results with this status.
    """

    status: str = Field(min_length=1, max_length=40)
    count: int  = Field(ge=0)


class DailyAlertCountResponse(BaseModel):
    """
    Aggregated open-alert count for one severity.

    Attributes:
        severity: Alert severity such as critical or warning.
        count: Number of open alerts with this severity.
    """

    severity: str = Field(min_length=1, max_length=40)
    count: int    = Field(ge=0)


class DailySummaryResponse(BaseModel):
    """
    Public deterministic daily quality summary for operator interfaces.

    Attributes:
        status: Request result status.
        dt: Exact business date summarized by the tool.
        check_counts: Bounded check counts grouped by status.
        alert_counts: Bounded open-alert counts grouped by severity.
        total_checks: Sum of all check counts.
        total_open_alerts: Sum of all open-alert counts.
        duration_ms: Tool execution duration.
        summary: Deterministic operator-facing summary.
    """

    model_config = ConfigDict(extra="ignore")

    status: str                                  = "success"
    dt: date
    check_counts: list[DailyCheckCountResponse]   = Field(default_factory=list, max_length=100)
    alert_counts: list[DailyAlertCountResponse]   = Field(default_factory=list, max_length=100)
    total_checks: int                             = Field(ge=0)
    total_open_alerts: int                        = Field(ge=0)
    duration_ms: int                              = Field(default=0, ge=0)
    summary: str                                  = Field(max_length=500)

    @model_validator(mode="after")
    def validate_aggregates(self) -> "DailySummaryResponse":
        """
        Ensure labels are unique and totals equal the exposed aggregates.

        Returns:
            Validated response when aggregate identity and totals are consistent.

        Raises:
            ValueError: If duplicate labels or inconsistent totals are found.
        """
        check_labels = [item.status for item in self.check_counts]
        alert_labels = [item.severity for item in self.alert_counts]

        if len(check_labels) != len(set(check_labels)):
            raise ValueError("Daily summary contains duplicate check statuses.")

        if len(alert_labels) != len(set(alert_labels)):
            raise ValueError("Daily summary contains duplicate alert severities.")

        if self.total_checks != sum(item.count for item in self.check_counts):
            raise ValueError("Daily summary total_checks does not equal the check aggregates.")

        if self.total_open_alerts != sum(item.count for item in self.alert_counts):
            raise ValueError("Daily summary total_open_alerts does not equal the alert aggregates.")

        return self


class LlmRouteResponse(BaseModel):
    """
    Sanitized LLM routing observation attached to public audit events.

    Attributes:
        requested_route: Capability route requested by the workflow.
        executed_route: Route that produced the final output.
        attempted_routes: Ordered provider or fallback routes attempted.
        provider: Final provider name.
        model: Final provider model identifier.
        input_tokens: Input token count or estimate.
        output_tokens: Output token count or estimate.
        total_tokens: Combined input and output tokens.
        estimated_cost_usd: Estimated provider cost.
        estimated_cost_display: Human-readable cost display.
        duration_ms: End-to-end model route duration.
        used_heuristic: Whether deterministic fallback generated the output.
        fallback_reason: Machine-readable fallback reason.
        fallback_summary: Operator-facing fallback explanation.
        runtime_mode: External model, heuristic fallback, or failed mode.
    """

    model_config = ConfigDict(extra="ignore")

    requested_route: str          = Field(default="", max_length=120)
    executed_route: str           = Field(default="", max_length=120)
    attempted_routes: list[str]   = Field(default_factory=list, max_length=10)
    provider: str                 = Field(default="", max_length=120)
    model: str                    = Field(default="", max_length=200)
    input_tokens: int             = Field(default=0, ge=0)
    output_tokens: int            = Field(default=0, ge=0)
    total_tokens: int             = Field(default=0, ge=0)
    estimated_cost_usd: float     = Field(default=0.0, ge=0.0)
    estimated_cost_display: str   = Field(default="$0.000000", max_length=40)
    duration_ms: int              = Field(default=0, ge=0)
    used_heuristic: bool          = False
    fallback_reason: str          = Field(default="", max_length=500)
    fallback_summary: str         = Field(default="", max_length=1000)
    runtime_mode: str             = Field(default="", max_length=40)


class AuditLogItemResponse(BaseModel):
    """
    Sanitized audit event without raw prompt payloads or SQL text.

    Attributes:
        audit_id: Durable audit event UUID.
        ts: Event timestamp.
        alert_id: Optional correlated alert UUID.
        agent_run_id: Correlated agent run UUID.
        actor: Component or operator that produced the event.
        action: Stable audit action name.
        tool_name: Optional guarded tool name.
        status: Event outcome status.
        duration_ms: Event duration in milliseconds.
        row_count: Optional bounded result count.
        report_s3_uri: Optional report artifact reference.
        error_message: Sanitized failure detail when present.
        llm_route: Optional sanitized model routing observation.
    """

    model_config = ConfigDict(extra="ignore")

    audit_id: UUID | None                 = None
    ts: datetime | None                   = None
    alert_id: UUID | None                 = None
    agent_run_id: UUID | None             = None
    actor: str                            = Field(default="", max_length=120)
    action: str                           = Field(max_length=160)
    tool_name: str                        = Field(default="", max_length=160)
    status: str                           = Field(max_length=40)
    duration_ms: int | None               = Field(default=None, ge=0)
    row_count: int | None                 = Field(default=None, ge=0)
    report_s3_uri: str                    = Field(default="", max_length=2048)
    error_message: str                    = Field(default="", max_length=2000)
    llm_route: LlmRouteResponse | None    = None


class AuditLogResponse(BaseModel):
    """
    Bounded audit history response for one exact alert key.

    Attributes:
        status: Request result status.
        alert_key: Exact stable alert key used for the lookup.
        limit: Hard event limit requested by the caller.
        row_count: Number of public audit events returned.
        rows: Sanitized events ordered newest first.
        llm_routes: Sanitized LLM observations found in the result window.
        latest_llm_route: Newest LLM observation when available.
        duration_ms: Read duration measured by the API.
        summary: Deterministic operator-facing result summary.
    """

    model_config = ConfigDict(extra="ignore")

    status: str                          = "success"
    alert_key: str                       = Field(min_length=1, max_length=500)
    limit: int                           = Field(ge=1, le=100)
    row_count: int                       = Field(ge=0, le=100)
    rows: list[AuditLogItemResponse]      = Field(default_factory=list, max_length=100)
    llm_routes: list[LlmRouteResponse]    = Field(default_factory=list, max_length=100)
    latest_llm_route: LlmRouteResponse | None = None
    duration_ms: int                     = Field(default=0, ge=0)
    summary: str                         = Field(max_length=500)

    @model_validator(mode="after")
    def validate_result_bounds(self) -> "AuditLogResponse":
        """
        Ensure audit count metadata and caller bounds are consistent.

        Returns:
            Validated response when rows remain inside the public contract.

        Raises:
            ValueError: If event counts are inconsistent or unbounded.
        """
        if self.row_count != len(self.rows):
            raise ValueError("Audit row_count must equal the returned event count.")

        if self.row_count > self.limit:
            raise ValueError("Audit response exceeds the requested event limit.")

        return self


class DqHistoryItemResponse(BaseModel):
    """
    Public deterministic DQ result used as triage evidence.

    Attributes:
        check_run_id: Source DQ check run UUID.
        run_at: Check execution timestamp.
        dt: Checked business date.
        table_name: Fully qualified checked table.
        check_name: Stable check name.
        check_type: DQ category such as freshness or completeness.
        status: Check outcome.
        severity: Operational check severity.
        observed_value: Observed metric value.
        expected_value: Expected metric value.
        threshold_value: Applied rule threshold.
        details: Parsed check metadata.
        evidence_s3_uri: Optional failed-record evidence artifact.
    """

    model_config = ConfigDict(extra="ignore")

    check_run_id: UUID | None        = None
    run_at: datetime | None          = None
    dt: date
    table_name: str                  = Field(max_length=255)
    check_name: str                  = Field(max_length=255)
    check_type: str                  = Field(default="", max_length=120)
    status: str                      = Field(max_length=40)
    severity: str                    = Field(default="", max_length=40)
    observed_value: float | None     = None
    expected_value: float | None     = None
    threshold_value: float | None    = None
    details: dict[str, Any]          = Field(default_factory=dict)
    evidence_s3_uri: str             = Field(default="", max_length=2048)


class DqHistoryResponse(BaseModel):
    """
    Bounded DQ history evidence around one table and date.

    Attributes:
        status: Request result status.
        table_name: Exact table used for the evidence lookup.
        dt: Target business date.
        check_name: Optional applied check-name filter.
        lookback_days: Applied historical window.
        limit: Hard result bound.
        row_count: Number of returned DQ results.
        rows: Public DQ results ordered newest first.
        status_counts: Result counts grouped by check status.
        summary: Deterministic operator-facing result summary.
    """

    model_config = ConfigDict(extra="ignore")

    status: str                       = "success"
    table_name: str                   = Field(max_length=255)
    dt: date
    check_name: str | None            = Field(default=None, max_length=255)
    lookback_days: int                = Field(ge=0, le=90)
    limit: int                        = Field(ge=1, le=500)
    row_count: int                    = Field(ge=0, le=500)
    rows: list[DqHistoryItemResponse] = Field(default_factory=list, max_length=500)
    status_counts: dict[str, int]     = Field(default_factory=dict)
    summary: str                      = Field(max_length=500)

    @model_validator(mode="after")
    def validate_result_bounds(self) -> "DqHistoryResponse":
        """
        Ensure DQ evidence counts match the bounded result window.

        Returns:
            Validated response when row count and limit are consistent.

        Raises:
            ValueError: If DQ evidence is inconsistent or unbounded.
        """
        if self.row_count != len(self.rows):
            raise ValueError("DQ history row_count must equal the returned result count.")

        if self.row_count > self.limit:
            raise ValueError("DQ history response exceeds the requested result limit.")

        return self


class PipelineRunItemResponse(BaseModel):
    """
    Public pipeline-run status used as incident evidence.

    Attributes:
        run_id: Durable pipeline run identifier.
        job_name: Pipeline job name.
        dag_id: Optional Airflow DAG identifier.
        task_id: Optional Airflow task identifier.
        logical_date: Optional Airflow logical business date.
        partition_dt: Optional affected data partition.
        status: Pipeline run outcome.
        started_at: Run start timestamp.
        ended_at: Run end timestamp.
        duration_ms: Run duration in milliseconds.
        rows_read: Number of source rows read.
        rows_written: Number of target rows written.
        source_uri: Optional source artifact URI.
        target_table: Optional target warehouse table.
        error_message: Sanitized run failure message.
        metadata: Parsed pipeline run metadata.
    """

    model_config = ConfigDict(extra="ignore")

    run_id: UUID
    job_name: str                     = Field(max_length=255)
    dag_id: str                       = Field(default="", max_length=255)
    task_id: str                      = Field(default="", max_length=255)
    logical_date: date | None         = None
    partition_dt: date | None         = None
    status: str                       = Field(max_length=40)
    started_at: datetime | None       = None
    ended_at: datetime | None         = None
    duration_ms: int | None           = Field(default=None, ge=0)
    rows_read: int | None             = Field(default=None, ge=0)
    rows_written: int | None          = Field(default=None, ge=0)
    source_uri: str                   = Field(default="", max_length=2048)
    target_table: str                 = Field(default="", max_length=255)
    error_message: str                = Field(default="", max_length=2000)
    metadata: dict[str, Any]          = Field(default_factory=dict)


class PipelineRunEvidenceResponse(BaseModel):
    """
    Bounded pipeline-run evidence around one business date.

    Attributes:
        status: Request result status.
        dt: Target business date.
        lookback_days: Applied historical window.
        job_name: Optional applied job-name filter.
        limit: Hard result bound.
        row_count: Number of returned pipeline runs.
        rows: Public pipeline run rows ordered newest first.
        status_counts: Run counts grouped by status.
        summary: Deterministic operator-facing result summary.
    """

    model_config = ConfigDict(extra="ignore")

    status: str                         = "success"
    dt: date
    lookback_days: int                  = Field(ge=0, le=90)
    job_name: str | None                = Field(default=None, max_length=255)
    limit: int                          = Field(ge=1, le=500)
    row_count: int                      = Field(ge=0, le=500)
    rows: list[PipelineRunItemResponse] = Field(default_factory=list, max_length=500)
    status_counts: dict[str, int]       = Field(default_factory=dict)
    summary: str                        = Field(max_length=500)

    @model_validator(mode="after")
    def validate_result_bounds(self) -> "PipelineRunEvidenceResponse":
        """
        Ensure pipeline evidence counts match the bounded result window.

        Returns:
            Validated response when row count and limit are consistent.

        Raises:
            ValueError: If pipeline evidence is inconsistent or unbounded.
        """
        if self.row_count != len(self.rows):
            raise ValueError("Pipeline run row_count must equal the returned result count.")

        if self.row_count > self.limit:
            raise ValueError("Pipeline run response exceeds the requested result limit.")

        return self


class ReportArtifactResponse(BaseModel):
    """
    Bounded Markdown or JSON report artifact loaded from approved S3 storage.

    Attributes:
        status: Request result status.
        s3_uri: Exact approved report artifact URI.
        bucket: Approved SeaweedFS S3 bucket.
        key: Object key inside the bucket.
        media_type: JSON or Markdown content type derived from the key.
        bytes_read: Full artifact size before response truncation.
        returned_bytes: UTF-8 bytes returned to the caller.
        max_bytes: Hard byte limit requested by the caller.
        truncated: Whether the response contains only a prefix of the artifact.
        text: Bounded artifact text.
    """

    model_config = ConfigDict(extra="ignore")

    status: str          = "success"
    s3_uri: str          = Field(max_length=2048)
    bucket: str          = Field(max_length=100)
    key: str             = Field(max_length=1024)
    media_type: str      = Field(max_length=80)
    bytes_read: int      = Field(ge=0)
    returned_bytes: int  = Field(ge=0)
    max_bytes: int       = Field(ge=1, le=200_000)
    truncated: bool      = False
    text: str            = Field(max_length=200_000)

    @model_validator(mode="after")
    def validate_byte_bounds(self) -> "ReportArtifactResponse":
        """
        Ensure artifact metadata cannot claim bytes beyond the response limit.

        Returns:
            Validated response when byte counts are internally consistent.

        Raises:
            ValueError: If returned byte metadata exceeds configured bounds.
        """
        if self.returned_bytes > self.max_bytes:
            raise ValueError("Report artifact exceeds the requested response byte limit.")

        if self.returned_bytes > self.bytes_read:
            raise ValueError("Returned report bytes cannot exceed the source artifact size.")

        return self


class CopilotAnswerRequest(BaseModel):
    """
    Request body for an evidence-aware Copilot answer.

    Attributes:
        alert_key: Stable alert key used to load source-of-truth alert context.
        question: Operator question bounded for safe prompt construction.
        report_json_s3_uri: Optional approved JSON triage report artifact.
        audit_limit: Maximum recent audit events supplied to the narrative layer.
    """

    alert_key: str
    question: str                        = Field(min_length=3, max_length=1000)
    report_json_s3_uri: str | None       = None
    audit_limit: int                     = Field(default=10, ge=1, le=25)


class CopilotAnswerResponse(BaseModel):
    """
    Evidence-aware Copilot response for UI, Discord, MCP, and future web clients.

    Attributes:
        status: Request status.
        agent_run_id: Correlation UUID for LLM and audit events.
        alert_key: Stable system alert key.
        alert_display_id: Human-facing Alert Ref.
        answer: Natural-language operator answer.
        context_source: Context source such as alert_audit or alert_report_audit.
        report_id: Optional human-facing report identifier.
        evidence_count: Number of bounded report evidence items used.
        audit_count: Number of recent audit events used.
        incident_history_count: Number of earlier exact-match investigations used.
        approval_required: Whether the report proposes approval-gated actions.
    """

    status: str                         = "success"
    agent_run_id: str
    alert_key: str
    alert_display_id: str              = ""
    answer: str
    context_source: str
    report_id: str                     = ""
    evidence_count: int                = 0
    audit_count: int                   = 0
    incident_history_count: int        = Field(default=0, ge=0, le=5)
    approval_required: bool            = False


class TriageRunRequest(BaseModel):
    """
    Request body for running one alert triage workflow.

    Attributes:
        alert_id: Optional alert UUID.
        alert_key: Optional stable alert key.
        confidence_threshold: Confidence target for the evidence loop.
        max_evidence_iterations: Maximum bounded evidence loop iterations.
        manifest_s3_uri: Optional dbt manifest artifact URI.
        artifacts_bucket: Optional report artifact bucket override.
        artifacts_prefix: Optional report artifact S3 prefix.
    """

    alert_id: str | None                = None
    alert_key: str | None               = None
    confidence_threshold: float         = Field(default=0.70, ge=0.10, le=0.95)
    max_evidence_iterations: int        = Field(default=2, ge=0, le=5)
    manifest_s3_uri: str | None         = None
    artifacts_bucket: str | None        = None
    artifacts_prefix: str               = "agent-reports"

    @model_validator(mode="after")
    def require_alert_identifier(self) -> "TriageRunRequest":
        """
        Ensure one alert identifier is provided.

        Returns:
            Current request instance.

        Raises:
            ValueError: If both alert_id and alert_key are missing.
        """
        if not self.alert_id and not self.alert_key:
            raise ValueError("Provide alert_id or alert_key.")

        return self


class TriageRunResponse(BaseModel):
    """
    Compact response returned after a triage workflow finishes.

    Attributes:
        status: Request status.
        agent_run_id: Triage agent run UUID.
        alert_key: Stable alert key.
        alert_display_id: Human-facing alert reference.
        severity: Alert severity.
        confidence: Final top-hypothesis confidence.
        top_hypothesis: Top hypothesis title.
        markdown_report_s3_uri: Markdown report artifact URI.
        json_report_s3_uri: JSON report artifact URI.
        approval_gated_actions: Proposed actions requiring human approval.
    """

    status: str
    agent_run_id: str
    alert_key: str
    alert_display_id: str = ""
    severity: str
    confidence: float
    top_hypothesis: str | None = None
    markdown_report_s3_uri: str
    json_report_s3_uri: str
    approval_gated_actions: list[dict[str, Any]] = Field(default_factory=list)


class CheckpointSnapshotResponse(BaseModel):
    """
    Sanitized metadata for one persisted LangGraph checkpoint.

    Attributes:
        checkpoint_id: LangGraph checkpoint identifier used for exact replay.
        created_at: ISO timestamp recorded by the checkpoint backend.
        step: Graph super-step number.
        source: Checkpoint source such as input, loop, or update.
        next_nodes: Bounded nodes pending after this checkpoint.
        is_complete: Whether no graph node remains pending.
    """

    checkpoint_id: str            = Field(min_length=1, max_length=160)
    created_at: str               = Field(default="", max_length=80)
    step: int
    source: str                   = Field(default="unknown", max_length=80)
    next_nodes: list[str]         = Field(default_factory=list, max_length=20)
    is_complete: bool             = False


class CheckpointHistoryResponse(BaseModel):
    """
    Read-only checkpoint history exposed to operator interfaces.

    Attributes:
        status: Request result status.
        checkpoint_namespace: Source triage namespace supplied by the operator.
        thread_id: Derived checkpoint thread identifier.
        history_count: Number of sanitized snapshots returned.
        matching_checkpoint_count: Number waiting for the requested node.
        selected_checkpoint: Newest replay candidate for that node.
        history: Newest-first sanitized snapshot metadata.
        raw_state_exposed: Must remain false for the public boundary.
        read_only: Must remain true for checkpoint inspection.
        summary: Human-readable operator summary.
    """

    status: str                                  = "success"
    checkpoint_namespace: str                    = Field(min_length=1, max_length=160)
    thread_id: str                               = Field(min_length=1, max_length=160)
    history_count: int                           = Field(ge=1, le=100)
    matching_checkpoint_count: int               = Field(ge=1, le=100)
    selected_checkpoint: CheckpointSnapshotResponse
    history: list[CheckpointSnapshotResponse]    = Field(min_length=1, max_length=100)
    raw_state_exposed: bool                      = False
    read_only: bool                              = True
    summary: str                                 = Field(max_length=500)


class CheckpointReplayPreviewRequest(BaseModel):
    """
    Request body for one non-executing checkpoint replay preview.

    Attributes:
        alert_id: Optional source alert UUID.
        alert_key: Optional stable source alert key.
        checkpoint_namespace: Existing source triage namespace.
        checkpoint_id: Candidate selected from current checkpoint history.
        replay_request_id: Optional stable idempotency key; generated when blank.
        history_limit: Maximum source checkpoints re-read before preview.
        history_next_node: Exact pending node required by the preview.
    """

    alert_id: str | None           = None
    alert_key: str | None          = None
    checkpoint_namespace: str      = Field(min_length=1, max_length=160)
    checkpoint_id: str             = Field(min_length=1, max_length=160)
    replay_request_id: str         = Field(default="", max_length=160)
    history_limit: int             = Field(default=50, ge=1, le=100)
    history_next_node: str         = Field(default="store_report", min_length=1, max_length=80)

    @model_validator(mode="after")
    def require_exact_alert_identifier(self) -> "CheckpointReplayPreviewRequest":
        """
        Require exactly one source alert identifier.

        Returns:
            Current request instance when identity is unambiguous.

        Raises:
            ValueError: If both or neither alert identities are provided.
        """
        if bool(self.alert_id) == bool(self.alert_key):
            raise ValueError("Provide exactly one alert_id or alert_key.")

        return self


class CheckpointReplayPreviewResponse(BaseModel):
    """
    Sanitized Airflow-only replay preview with no execution side effect.

    Attributes:
        status: Preview status.
        dag_id: Operational Airflow DAG that owns replay execution.
        action: Fixed replay action.
        alert_reference: Validated internal alert identity.
        checkpoint_namespace: Source triage namespace.
        source_thread_id: Derived source checkpoint thread.
        source_checkpoint_id: Exact selected historical checkpoint.
        source_next_nodes: Pending nodes bound to the selected checkpoint.
        replay_request_id: Stable replay idempotency key.
        replay_thread_id: Deterministic child replay thread.
        dag_run_conf: Allowlisted Airflow DagRun configuration preview.
        execution_boundary: Fixed Airflow DAG 40 boundary.
        operator_confirmation_required: Whether an operator must trigger execution explicitly.
        airflow_triggered: Must remain false for preview responses.
        side_effects_executed: Must remain false for preview responses.
        raw_state_exposed: Must remain false for the public boundary.
        summary: Human-readable safety statement.
    """

    status: str                             = "preview"
    dag_id: str
    action: str                             = "replay"
    alert_reference: str
    checkpoint_namespace: str
    source_thread_id: str
    source_checkpoint_id: str
    source_next_nodes: list[str]            = Field(default_factory=list, max_length=20)
    replay_request_id: str
    replay_thread_id: str
    dag_run_conf: dict[str, Any]
    execution_boundary: str                 = "airflow_dag_40"
    operator_confirmation_required: bool    = True
    airflow_triggered: bool                 = False
    side_effects_executed: bool             = False
    raw_state_exposed: bool                 = False
    summary: str                            = Field(max_length=500)


class IncidentEvidenceReferenceResponse(BaseModel):
    """
    Public evidence pointer retained by one durable incident investigation.

    Attributes:
        evidence_type: Stable evidence category.
        source_tool: Guarded tool that produced the evidence.
        reference: Stable artifact, table, run, or audit reference.
        summary: Bounded operator-facing evidence description.
    """

    evidence_type: str = Field(min_length=1, max_length=80)
    source_tool: str    = Field(min_length=1, max_length=80)
    reference: str      = Field(min_length=1, max_length=2_048)
    summary: str        = Field(min_length=1, max_length=1_000)


class IncidentHistoryItemResponse(BaseModel):
    """
    Sanitized durable incident outcome exposed to operator interfaces.

    Attributes:
        memory_id: Durable investigation identifier used for technical lookup.
        parent_run_id: Supervisor run correlation UUID.
        recorded_at: UTC time when the investigation outcome was persisted.
        memory_type: Investigation or resolution memory category.
        alert_id: Optional internal alert UUID.
        alert_key: Canonical system alert key retained for technical correlation.
        alert_display_id: Human-facing Alert Ref.
        outcome_status: Terminal investigation status.
        specialist_name: Specialist that produced the outcome.
        task_type: Bounded task completed by the specialist.
        summary: Human-readable investigation summary.
        confidence: Optional policy-owned confidence score.
        top_hypothesis_category: Bounded root-cause category, not hidden reasoning.
        report_id: Human-facing report identifier.
        requires_human_approval: Whether the outcome proposes a gated action.
        evidence_reference_count: Number of retained evidence pointers.
        evidence_references: Bounded evidence pointers without raw result rows.
        report_s3_uri: Persisted report artifact URI.
        approval_state: Approval lifecycle state at persistence time.
        resolution_reference: Optional approval or execution correlation reference.
    """

    memory_id: str
    parent_run_id: str
    recorded_at: datetime
    memory_type: str
    alert_id: str | None                                   = None
    alert_key: str                                         = ""
    alert_display_id: str                                  = ""
    outcome_status: str
    specialist_name: str                                   = ""
    task_type: str                                         = ""
    summary: str                                           = Field(default="", max_length=4_000)
    confidence: float | None                               = Field(default=None, ge=0.0, le=1.0)
    top_hypothesis_category: str                           = Field(default="", max_length=80)
    report_id: str                                         = Field(default="", max_length=40)
    requires_human_approval: bool                          = False
    evidence_reference_count: int                          = Field(ge=0, le=100)
    evidence_references: list[IncidentEvidenceReferenceResponse] = Field(
        default_factory=list,
        max_length=100,
    )
    report_s3_uri: str                                     = Field(default="", max_length=2_048)
    approval_state: str                                    = "not_required"
    resolution_reference: str                              = Field(default="", max_length=500)


class IncidentHistoryResponse(BaseModel):
    """
    Bounded incident-history response shared by Streamlit and future clients.

    Attributes:
        status: Request result status.
        alert_reference: Exact Alert Ref, key, or UUID used for lookup.
        lookback_days: Applied mandatory timestamp window.
        limit: Applied hard row limit.
        row_count: Number of returned durable investigations.
        rows: Sanitized investigation outcomes ordered newest first.
        summary: Deterministic operator-facing history summary.
    """

    status: str                             = "success"
    alert_reference: str
    lookback_days: int                      = Field(ge=1, le=365)
    limit: int                              = Field(ge=1, le=50)
    row_count: int                          = Field(ge=0, le=50)
    rows: list[IncidentHistoryItemResponse] = Field(default_factory=list, max_length=50)
    summary: str                            = Field(max_length=500)


class MetadataAssetResponse(BaseModel):
    """
    Public trust and ownership context for one warehouse asset.

    Attributes:
        qualified_name: Stable database.table identity.
        database_name: ClickHouse database containing the asset.
        table_name: Physical table or view name.
        display_name: Human-readable asset title.
        description: Intended asset purpose and usage.
        dataset: Parent data-product identifier.
        domain: Business or platform domain.
        data_layer: Raw, staging, or mart warehouse layer.
        technical_owner: Team responsible for technical reliability.
        business_owner: Team responsible for business meaning.
        grain: Explicit row-level grain.
        refresh_frequency: Expected refresh cadence.
        sla_time: Local wall-clock completion target.
        sla_timezone: IANA timezone used by the SLA.
        criticality: Operational impact tier.
        sensitivity: Data handling classification.
        contains_pii: Whether the asset contains personally identifiable data.
        certification_status: Data-product promotion or trust state.
        lifecycle_status: Active or deprecated lifecycle state.
        tags: Bounded discovery labels.
        synced_at: UTC timestamp of the latest registry version.
    """

    qualified_name: str
    database_name: str
    table_name: str
    display_name: str
    description: str
    dataset: str
    domain: str
    data_layer: str
    technical_owner: str
    business_owner: str
    grain: str
    refresh_frequency: str
    sla_time: str
    sla_timezone: str
    criticality: str
    sensitivity: str
    contains_pii: bool
    certification_status: str
    lifecycle_status: str
    tags: list[str]              = Field(default_factory=list, max_length=20)
    synced_at: datetime


class MetadataAssetListResponse(BaseModel):
    """
    Bounded metadata discovery response shared by UI and external clients.

    Attributes:
        status: Request status.
        query: Applied free-text search.
        filters: Applied normalized categorical filters.
        limit: Hard result bound.
        row_count: Number of returned assets.
        assets: Public metadata assets.
        summary: Deterministic operator-facing result summary.
    """

    status: str                              = "success"
    query: str                               = ""
    filters: dict[str, str]                  = Field(default_factory=dict)
    limit: int                               = Field(ge=1, le=100)
    row_count: int                           = Field(ge=0, le=100)
    assets: list[MetadataAssetResponse]       = Field(default_factory=list, max_length=100)
    summary: str


class DbtLineageNodeResponse(BaseModel):
    """
    Bounded dbt node metadata exposed by lineage APIs.

    Attributes:
        unique_id: dbt manifest unique identifier.
        resource_type: dbt resource type such as model, source, test, or exposure.
        name: Logical dbt resource name.
        alias: Optional physical relation alias.
        schema_name: Optional warehouse schema name.
        relation_name: Optional fully qualified warehouse relation.
        description: Bounded dbt resource description.
        path: Optional dbt project-relative resource path.
        original_file_path: Optional original dbt project file path.
        depth: Optional downstream distance from the selected root node.
        parent_unique_id: Optional parent selected by breadth-first traversal.
        lineage_path: Shortest unique-id path from the root to this node.
    """

    unique_id: str
    resource_type: str                    = "unknown"
    name: str | None                      = None
    alias: str | None                     = None
    schema_name: str | None               = Field(default=None, alias="schema")
    relation_name: str | None             = None
    description: str                      = ""
    path: str | None                      = None
    original_file_path: str | None        = None
    depth: int | None                     = Field(default=None, ge=1)
    parent_unique_id: str | None          = None
    lineage_path: list[str]               = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class DbtBlastRadiusResponse(BaseModel):
    """
    Typed, bounded transitive dbt blast-radius response.

    Attributes:
        table_name: Requested warehouse table.
        matched: Whether the table matched a dbt model or source.
        node: Matched root dbt node.
        manifest_source: Local path or S3 URI used for analysis.
        max_depth: Applied downstream depth bound.
        max_nodes: Applied downstream node-count bound.
        max_depth_reached: Deepest returned downstream level.
        truncated: Whether traversal stopped at a configured bound.
        total_impacted_nodes: Total returned assets, tests, and unresolved nodes.
        impacted_asset_count: Number of downstream non-test resources.
        impacted_test_count: Number of downstream dbt tests.
        unresolved_node_count: Number of child-map identifiers missing metadata.
        resource_type_counts: Returned node counts grouped by dbt resource type.
        impacted_assets: Downstream models, exposures, and other non-test resources.
        impacted_tests: Downstream dbt tests affected by the selected table.
        unresolved_nodes: Child-map references without matching manifest metadata.
        summary: Deterministic operator-facing impact summary.
    """

    table_name: str
    matched: bool
    node: DbtLineageNodeResponse | None       = None
    manifest_source: str
    max_depth: int                            = Field(ge=1, le=10)
    max_nodes: int                            = Field(ge=1, le=250)
    max_depth_reached: int                    = Field(ge=0, le=10)
    truncated: bool                           = False
    total_impacted_nodes: int                 = Field(ge=0)
    impacted_asset_count: int                 = Field(ge=0)
    impacted_test_count: int                  = Field(ge=0)
    unresolved_node_count: int                = Field(ge=0)
    resource_type_counts: dict[str, int]       = Field(default_factory=dict)
    impacted_assets: list[DbtLineageNodeResponse] = Field(default_factory=list)
    impacted_tests: list[DbtLineageNodeResponse]  = Field(default_factory=list)
    unresolved_nodes: list[DbtLineageNodeResponse] = Field(default_factory=list)
    summary: str


class ApprovalRequestCreateBody(BaseModel):
    """
    API request body for one bounded backfill approval proposal.

    Attributes:
        action_type: Approval action type. Current API supports backfill only.
        alert_id: Optional source alert UUID string.
        alert_key: Stable source alert key.
        agent_run_id: Optional source triage run UUID string.
        requested_by: Human or system identity creating the request.
        reason: Human-readable reason presented to approvers.
        target_dag_id: Allowlisted operational DAG to backfill.
        start_date: Inclusive business start date.
        end_date: Inclusive business end date.
        parameters: Bounded execution flags bound to the decision.
    """

    action_type: str                   = "backfill"
    alert_id: str | None               = None
    alert_key: str                     = ""
    agent_run_id: str | None           = None
    requested_by: str                  = Field(min_length=1, max_length=200)
    reason: str                        = Field(min_length=5, max_length=2000)
    target_dag_id: str
    start_date: date
    end_date: date
    parameters: dict[str, Any]         = Field(default_factory=dict)


class ApprovalDecisionBody(BaseModel):
    """
    API request body for an explicit human approval decision.

    Attributes:
        decision: Approve or reject decision.
        decided_by: Human identity making the decision.
        comment: Optional bounded decision rationale.
    """

    decision: str                     = Field(pattern="^(approve|reject)$")
    decided_by: str                   = Field(min_length=1, max_length=200)
    comment: str                      = Field(default="", max_length=2000)


class ApprovalRequestResponse(BaseModel):
    """
    Latest state returned for one durable approval request.

    Attributes:
        request_id: Human-facing deterministic approval reference.
        created_at: UTC request creation timestamp.
        updated_at: UTC latest-state timestamp.
        alert_id: Optional source alert UUID string.
        alert_key: Stable source alert key.
        agent_run_id: Optional source triage run UUID string.
        action_type: Bounded action type.
        risk_level: Operator-facing risk classification.
        status: Pending, approved, or rejected lifecycle state.
        requested_by: Requesting identity.
        reason: Human-readable approval reason.
        dispatcher_dag_id: Airflow dispatcher authorized for the action.
        target_dag_id: Operational target DAG.
        start_date: Inclusive business start date.
        end_date: Inclusive business end date.
        parameters: Canonical execution flags bound to the decision.
        dry_run: Whether the approved action is preview-only.
        idempotency_key: Stable action-scope hash.
        decided_by: Human identity that made the decision.
        decided_at: UTC terminal decision timestamp.
        decision_comment: Optional decision rationale.
        execution_dag_run_id: Parent Airflow dispatcher DagRun correlation ID.
        execution_status: Current single-use execution lifecycle state.
        execution_error: Bounded dispatch or child execution failure detail.
        created_new: Whether a create request inserted a new queue entry.
        state_changed: Whether a decision changed lifecycle state.
    """

    request_id: str
    created_at: datetime
    updated_at: datetime
    alert_id: str | None              = None
    alert_key: str                    = ""
    agent_run_id: str | None          = None
    action_type: str
    risk_level: str
    status: str
    requested_by: str
    reason: str
    dispatcher_dag_id: str
    target_dag_id: str
    start_date: date | None           = None
    end_date: date | None             = None
    parameters: dict[str, Any]        = Field(default_factory=dict)
    dry_run: bool                     = False
    idempotency_key: str
    decided_by: str                   = ""
    decided_at: datetime | None       = None
    decision_comment: str             = ""
    execution_dag_run_id: str         = ""
    execution_status: str             = "not_started"
    execution_error: str              = ""
    created_new: bool | None          = None
    state_changed: bool | None        = None


class ApprovalRequestListResponse(BaseModel):
    """
    Bounded approval queue response.

    Attributes:
        status: Request status.
        row_count: Number of latest-state requests returned.
        rows: Serialized approval request states.
    """

    status: str                              = "success"
    row_count: int
    rows: list[ApprovalRequestResponse]       = Field(default_factory=list)

