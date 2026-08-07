####
## API Schemas for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator


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

