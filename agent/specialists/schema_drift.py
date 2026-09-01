####
## Schema Drift Specialist Agent for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Bounded LangGraph specialist for persisted schema evidence and migration risk."""

# --- Importing Libraries
from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Literal, TypedDict
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent.specialists.contracts import (
    AgentModelRoute,
    AgentResultEnvelope,
    AgentRiskTier,
    AgentTaskEnvelope,
    AgentTaskStatus,
    ContextReference,
    ContextReferenceType,
    EvidenceReference,
)
from agent.specialists.registry import (
    SCHEMA_DRIFT_SPECIALIST_NAME,
    enforce_task_capability,
    required_tools_for_task,
)
from agent.tools.audit_log import write_agent_audit_event
from agent.tools.dbt_lineage import fetch_dbt_blast_radius
from agent.tools.metadata_catalog import get_metadata_asset
from agent.tools.schema_drift import (
    DEFAULT_FINDING_LIMIT,
    MAX_FINDING_LIMIT,
    fetch_schema_drift_run_context,
)
from pipelines.common.clickhouse import build_clickhouse_client, validate_qualified_table_name
from pipelines.common.logging import logger
from pipelines.schema_drift.detector import validate_schema_run_id


# --- Defining Constants
SPECIALIST_TOOL_NAME = "schema_drift_agent"
MAX_OUTPUT_FINDINGS  = 20
MAX_OUTPUT_IMPACTED  = 20

BREAKING_CHECK_TYPES = {
    "table_presence",
    "column_presence",
    "column_type",
}


# --- Defining Enumerations
class SchemaChangeAssessment(str, Enum):
    """Classify the compatibility of one persisted schema observation."""

    COMPATIBLE        = "compatible"
    REVIEW_REQUIRED   = "review_required"
    BREAKING_CHANGE   = "breaking_change"


class SchemaImpactLevel(str, Enum):
    """Represent deterministic downstream risk attached to schema findings."""

    NONE     = "none"
    LOW      = "low"
    MEDIUM   = "medium"
    HIGH     = "high"
    CRITICAL = "critical"


# --- Defining Specialist Models
class SchemaDriftTaskInput(BaseModel):
    """
    Define one exact persisted schema run requested for assessment.

    Attributes:
        source_schema_run_id: Detector DagRun ID retained in schema evidence tables.
        qualified_name: Exact database.table snapshot identity.
        finding_limit: Maximum finding rows included in specialist output.
        max_depth: Maximum downstream dbt lineage traversal depth.
        max_nodes: Maximum downstream dbt nodes included in blast radius.
    """

    model_config = ConfigDict(extra="forbid")

    source_schema_run_id: str = Field(min_length=1, max_length=250)
    qualified_name: str       = Field(min_length=3, max_length=255)
    finding_limit: int        = Field(default=DEFAULT_FINDING_LIMIT, ge=1, le=MAX_FINDING_LIMIT)
    max_depth: int            = Field(default=5, ge=1, le=10)
    max_nodes: int            = Field(default=100, ge=1, le=250)

    @field_validator("source_schema_run_id")
    @classmethod
    def validate_source_run(cls, value: str) -> str:
        """
        Validate the exact detector run identifier.

        Args:
            value: Raw source DagRun identifier.

        Returns:
            Normalized allowlisted run identifier.
        """
        return validate_schema_run_id(value)

    @field_validator("qualified_name")
    @classmethod
    def validate_asset_name(cls, value: str) -> str:
        """
        Validate the exact ClickHouse table identity.

        Args:
            value: Raw database.table identifier.

        Returns:
            Validated qualified table name.
        """
        return validate_qualified_table_name(value)


class SchemaDriftAgentOutput(BaseModel):
    """
    Return deterministic compatibility, impact, and migration guidance.

    Attributes:
        source_schema_run_id: Exact detector run used as source evidence.
        qualified_name: Exact assessed warehouse table.
        contract_name: Validated schema contract identity.
        contract_version: Schema contract version.
        contract_sha256: Validated schema contract hash.
        schema_sha256: Observed physical schema hash.
        snapshot_status: Persisted pass, warn, or fail status.
        highest_severity: Highest deterministic finding severity.
        assessment: Compatible, review-required, or breaking classification.
        impact_level: Deterministic downstream impact tier.
        finding_count: Complete persisted finding count.
        visible_finding_count: Findings included in this bounded result.
        findings_truncated: Findings omitted by the configured limit.
        finding_types: Distinct deterministic comparison categories.
        affected_columns: Distinct affected columns or table marker.
        findings: Bounded persisted warning and failure evidence.
        metadata_asset: Public metadata trust context.
        blast_radius: Bounded downstream dbt impact context.
        impacted_asset_count: Number of bounded downstream dbt assets.
        impacted_test_count: Number of bounded downstream dbt tests.
        migration_plan: Human-readable non-executing migration guidance.
        execution_performed: Always false; this specialist cannot alter schemas.
        summary: Operator-facing assessment summary.
    """

    model_config = ConfigDict(extra="forbid")

    source_schema_run_id: str
    qualified_name: str
    contract_name: str
    contract_version: int
    contract_sha256: str
    schema_sha256: str
    snapshot_status: str
    highest_severity: str
    assessment: SchemaChangeAssessment
    impact_level: SchemaImpactLevel
    finding_count: int                           = Field(ge=0)
    visible_finding_count: int                   = Field(ge=0)
    findings_truncated: int                      = Field(ge=0)
    finding_types: list[str]                     = Field(default_factory=list, max_length=25)
    affected_columns: list[str]                  = Field(default_factory=list, max_length=100)
    findings: list[dict[str, Any]]                = Field(default_factory=list, max_length=MAX_FINDING_LIMIT)
    metadata_asset: dict[str, Any]                = Field(default_factory=dict)
    blast_radius: dict[str, Any]                  = Field(default_factory=dict)
    impacted_asset_count: int                    = Field(default=0, ge=0)
    impacted_test_count: int                     = Field(default=0, ge=0)
    migration_plan: list[str]                    = Field(default_factory=list, max_length=10)
    execution_performed: Literal[False]          = False
    summary: str                                 = Field(min_length=1, max_length=2_000)


class SchemaDriftGraphState(TypedDict, total=False):
    """Represent serializable channels used by the Schema Drift Agent subgraph."""

    task: dict[str, Any]
    task_input: dict[str, Any]
    schema_context: dict[str, Any]
    metadata_asset: dict[str, Any]
    blast_radius: dict[str, Any]
    output: dict[str, Any]
    evidence_references: list[dict[str, Any]]
    confidence: float
    recommended_next_step: str
    requires_human_approval: bool


@dataclass(frozen=True)
class SchemaDriftRuntimeConfig:
    """
    Inject deterministic tools and connection overrides into the specialist.

    Attributes:
        schema_context_fetcher: Exact persisted schema-run evidence callable.
        metadata_getter: Exact public metadata asset lookup callable.
        blast_radius_fetcher: Bounded dbt downstream-impact callable.
        audit_client_factory: ClickHouse client factory used for handoff audits.
        audit_writer: Append-only audit event persistence callable.
        manifest_path: Optional system-owned local dbt manifest path.
        manifest_s3_uri: Optional system-owned S3 dbt manifest URI.
        s3_endpoint_url: Optional SeaweedFS endpoint override.
        clickhouse_host: Optional ClickHouse host override.
        clickhouse_port: Optional ClickHouse HTTP port override.
    """

    schema_context_fetcher: Callable[..., dict[str, Any]] = fetch_schema_drift_run_context
    metadata_getter: Callable[..., dict[str, Any]]         = get_metadata_asset
    blast_radius_fetcher: Callable[..., dict[str, Any]]    = fetch_dbt_blast_radius
    audit_client_factory: Callable[..., Any]               = build_clickhouse_client
    audit_writer: Callable[..., UUID]                      = write_agent_audit_event
    manifest_path: str | Path | None                       = None
    manifest_s3_uri: str | None                            = None
    s3_endpoint_url: str | None                            = None
    clickhouse_host: str | None                            = None
    clickhouse_port: int | None                            = None


# --- Defining Task Validation And Construction
def validate_schema_drift_task(task: AgentTaskEnvelope) -> SchemaDriftTaskInput:
    """
    Validate capability policy and schema-assessment input together.

    Args:
        task: Typed handoff supplied by the supervisor.

    Returns:
        Validated SchemaDriftTaskInput.

    Raises:
        PermissionError: If capability policy rejects the task.
        ValueError: If exact source evidence identifiers are missing or unsafe.
    """
    enforce_task_capability(task)
    task_input = SchemaDriftTaskInput.model_validate(task.input_payload)

    logger.info(
        "Validated schema drift task | task_id=%s source_run_id=%s table=%s",
        task.task_id,
        task_input.source_schema_run_id,
        task_input.qualified_name,
    )

    return task_input


def build_schema_drift_task(
    parent_run_id: UUID,
    source_schema_run_id: str,
    qualified_name: str,
    finding_limit: int = DEFAULT_FINDING_LIMIT,
    max_depth: int = 5,
    max_nodes: int = 100,
    manifest_s3_uri: str = "",
    requester: str = "control_plane",
    alert_key: str = "",
) -> AgentTaskEnvelope:
    """
    Build one least-privilege schema assessment handoff.

    Args:
        parent_run_id: Parent supervisor correlation UUID.
        source_schema_run_id: Exact schema detector DagRun ID.
        qualified_name: Exact database.table snapshot identity.
        finding_limit: Maximum persisted findings returned.
        max_depth: Maximum downstream dbt traversal depth.
        max_nodes: Maximum downstream dbt nodes returned.
        manifest_s3_uri: Optional dbt manifest artifact reference.
        requester: Interface or system requesting the assessment.
        alert_key: Optional related alert correlation reference.

    Returns:
        Validated AgentTaskEnvelope with deterministic no-LLM routing.
    """
    task_input = SchemaDriftTaskInput(
        source_schema_run_id=source_schema_run_id,
        qualified_name=qualified_name,
        finding_limit=finding_limit,
        max_depth=max_depth,
        max_nodes=max_nodes,
    )
    context_references = [
        ContextReference(
            reference_type=ContextReferenceType.AUDIT_RUN,
            reference=task_input.source_schema_run_id,
            description="Exact deterministic schema detector run used as source evidence.",
        ),
        ContextReference(
            reference_type=ContextReferenceType.METADATA_ASSET,
            reference=task_input.qualified_name,
            description="Warehouse asset assessed for schema compatibility and downstream impact.",
        ),
    ]

    if manifest_s3_uri:
        context_references.append(
            ContextReference(
                reference_type=ContextReferenceType.DBT_MANIFEST,
                reference=manifest_s3_uri,
                description="dbt manifest used for bounded downstream impact analysis.",
            )
        )

    task = AgentTaskEnvelope(
        parent_run_id=parent_run_id,
        specialist_name=SCHEMA_DRIFT_SPECIALIST_NAME,
        task_type="assess_schema_drift",
        risk_tier=AgentRiskTier.MEDIUM,
        allowed_tools=required_tools_for_task(
            specialist_name=SCHEMA_DRIFT_SPECIALIST_NAME,
            task_type="assess_schema_drift",
        ),
        context_references=context_references,
        model_route=AgentModelRoute.NO_LLM_FALLBACK,
        model_call_budget=0,
        token_budget=0,
        estimated_cost_budget_usd=0.0,
        timeout_seconds=90,
        requester=requester,
        alert_key=alert_key,
        input_payload=task_input.model_dump(mode="json"),
    )

    validate_schema_drift_task(task)

    logger.info(
        "Built schema drift handoff | task_id=%s parent_run_id=%s source_run_id=%s table=%s",
        task.task_id,
        task.parent_run_id,
        task_input.source_schema_run_id,
        task_input.qualified_name,
    )

    return task


# --- Defining Deterministic Assessment Policy
def build_migration_plan(
    assessment: SchemaChangeAssessment,
    finding_types: list[str],
) -> list[str]:
    """
    Build non-executing migration guidance from deterministic finding types.

    Args:
        assessment: Deterministic compatibility classification.
        finding_types: Distinct persisted schema comparison categories.

    Returns:
        Ordered operator actions that remain human-controlled.
    """
    if assessment == SchemaChangeAssessment.COMPATIBLE:
        return [
            "No schema remediation is required; continue monitoring the next detector run.",
        ]

    if assessment == SchemaChangeAssessment.BREAKING_CHANGE:
        plan = [
            "Confirm whether the producer-side schema change is intentional and identify its owner.",
            "Hold promotion of the affected schema change until downstream compatibility is reviewed.",
            "Use a versioned column or table and a dual-read migration instead of an in-place destructive change.",
            "Update impacted dbt models, contracts, and tests through a reviewed pull request.",
            "Require human approval before any DDL, backfill, or downstream rerun is executed.",
            "Rerun schema detection and downstream DQ checks after the approved migration.",
        ]

        if "table_presence" in finding_types:
            plan.insert(1, "Restore or replace the missing producer table before downstream processing resumes.")

        return plan[:10]

    return [
        "Confirm whether the observed warning is an intentional producer change.",
        "Update the schema contract through a reviewed pull request when the change is accepted.",
        "Review downstream models and tests before promoting the contract update.",
        "Rerun schema detection after approval; do not alter production schema from the agent.",
    ]


def assess_schema_change(
    schema_context: dict[str, Any],
    metadata_asset: dict[str, Any],
    blast_radius: dict[str, Any],
) -> tuple[
    SchemaChangeAssessment,
    SchemaImpactLevel,
    list[str],
    list[str],
    list[str],
    float,
    bool,
]:
    """
    Classify compatibility and impact from source-of-truth evidence.

    Args:
        schema_context: Exact persisted snapshot and bounded findings.
        metadata_asset: Public metadata ownership and trust context.
        blast_radius: Bounded downstream dbt impact context.

    Returns:
        Assessment, impact, types, columns, migration plan, confidence, and approval flag.
    """
    findings         = list(schema_context.get("findings", []))
    finding_count    = int(schema_context.get("finding_count", 0) or 0)
    finding_types    = sorted({str(item.get("check_type") or "unknown") for item in findings})
    affected_columns = sorted({str(item.get("column_name") or "<table>") for item in findings})
    highest_severity = str((schema_context.get("snapshot") or {}).get("highest_severity") or "info")
    impacted_assets  = int(blast_radius.get("impacted_asset_count", 0) or 0)
    has_breaking     = highest_severity == "critical" or bool(
        BREAKING_CHECK_TYPES.intersection(finding_types)
    )

    if finding_count == 0:
        assessment  = SchemaChangeAssessment.COMPATIBLE
        impact_level = SchemaImpactLevel.NONE
    elif has_breaking:
        assessment = SchemaChangeAssessment.BREAKING_CHANGE
        impact_level = (
            SchemaImpactLevel.CRITICAL
            if "table_presence" in finding_types or impacted_assets >= 5
            else SchemaImpactLevel.HIGH
        )
    else:
        assessment  = SchemaChangeAssessment.REVIEW_REQUIRED
        impact_level = SchemaImpactLevel.MEDIUM if impacted_assets else SchemaImpactLevel.LOW

    confidence = 0.95

    if not metadata_asset:
        confidence -= 0.10

    if not bool(blast_radius.get("matched", False)):
        confidence -= 0.10

    if bool(blast_radius.get("truncated", False)):
        confidence -= 0.15

    migration_plan = build_migration_plan(
        assessment=assessment,
        finding_types=finding_types,
    )

    return (
        assessment,
        impact_level,
        finding_types,
        affected_columns,
        migration_plan,
        max(0.50, confidence),
        assessment != SchemaChangeAssessment.COMPATIBLE,
    )


def build_bounded_blast_radius(blast_radius: dict[str, Any]) -> dict[str, Any]:
    """
    Reduce dbt impact evidence to a handoff-safe public summary.

    Args:
        blast_radius: Full bounded tool output retained by the specialist graph.

    Returns:
        Counts, root identity, summary, and small impacted-node samples.
    """
    scalar_keys = (
        "table_name",
        "matched",
        "node",
        "max_depth",
        "max_nodes",
        "max_depth_reached",
        "truncated",
        "total_impacted_nodes",
        "impacted_asset_count",
        "impacted_test_count",
        "unresolved_node_count",
        "resource_type_counts",
        "summary",
        "manifest_source",
    )
    bounded = {
        key: blast_radius[key]
        for key in scalar_keys
        if key in blast_radius
    }

    for collection_key in ("impacted_assets", "impacted_tests", "unresolved_nodes"):
        rows = list(blast_radius.get(collection_key, []))

        if rows:
            bounded[collection_key] = rows[:MAX_OUTPUT_IMPACTED]
            bounded[f"{collection_key}_omitted"] = max(
                0,
                len(rows) - MAX_OUTPUT_IMPACTED,
            )

    return bounded


def build_schema_drift_output(
    task_input: SchemaDriftTaskInput,
    schema_context: dict[str, Any],
    metadata_asset: dict[str, Any],
    blast_radius: dict[str, Any],
) -> tuple[SchemaDriftAgentOutput, list[EvidenceReference], float, str, bool]:
    """
    Build bounded specialist output and evidence references.

    Args:
        task_input: Validated exact source identifiers and bounds.
        schema_context: Exact persisted schema detector evidence.
        metadata_asset: Public metadata asset context.
        blast_radius: Bounded downstream dbt impact context.

    Returns:
        Output, evidence references, confidence, next step, and approval requirement.
    """
    snapshot = dict(schema_context.get("snapshot", {}))
    (
        assessment,
        impact_level,
        finding_types,
        affected_columns,
        migration_plan,
        confidence,
        requires_approval,
    ) = assess_schema_change(
        schema_context=schema_context,
        metadata_asset=metadata_asset,
        blast_radius=blast_radius,
    )
    impacted_assets      = int(blast_radius.get("impacted_asset_count", 0) or 0)
    impacted_tests       = int(blast_radius.get("impacted_test_count", 0) or 0)
    output_findings      = list(schema_context.get("findings", []))[:MAX_OUTPUT_FINDINGS]
    persisted_findings   = int(schema_context.get("finding_count", 0) or 0)
    bounded_blast_radius = build_bounded_blast_radius(blast_radius)
    summary = (
        f"{task_input.qualified_name} is classified as {assessment.value} with "
        f"{schema_context.get('finding_count', 0)} schema finding(s). "
        f"The bounded dbt blast radius contains {impacted_assets} downstream asset(s) "
        f"and {impacted_tests} test(s). No schema change was executed."
    )
    output = SchemaDriftAgentOutput(
        source_schema_run_id=task_input.source_schema_run_id,
        qualified_name=task_input.qualified_name,
        contract_name=str(snapshot.get("contract_name") or ""),
        contract_version=int(snapshot.get("contract_version") or 0),
        contract_sha256=str(snapshot.get("contract_sha256") or ""),
        schema_sha256=str(snapshot.get("schema_sha256") or ""),
        snapshot_status=str(snapshot.get("snapshot_status") or ""),
        highest_severity=str(snapshot.get("highest_severity") or "info"),
        assessment=assessment,
        impact_level=impact_level,
        finding_count=persisted_findings,
        visible_finding_count=len(output_findings),
        findings_truncated=max(0, persisted_findings - len(output_findings)),
        finding_types=finding_types,
        affected_columns=affected_columns,
        findings=output_findings,
        metadata_asset=metadata_asset,
        blast_radius=bounded_blast_radius,
        impacted_asset_count=impacted_assets,
        impacted_test_count=impacted_tests,
        migration_plan=migration_plan,
        execution_performed=False,
        summary=summary,
    )
    references = [
        EvidenceReference(
            evidence_type="schema_snapshot",
            source_tool="schema_drift",
            reference=f"{task_input.source_schema_run_id}:{task_input.qualified_name}",
            summary=str(schema_context.get("summary") or "Exact schema detector evidence was collected."),
        ),
        EvidenceReference(
            evidence_type="metadata_asset",
            source_tool="metadata_catalog",
            reference=task_input.qualified_name,
            summary=(
                f"Asset ownership and trust context was loaded for {task_input.qualified_name}."
            ),
        ),
        EvidenceReference(
            evidence_type="blast_radius",
            source_tool="dbt_blast_radius",
            reference=str((blast_radius.get("node") or {}).get("unique_id") or task_input.qualified_name),
            summary=str(blast_radius.get("summary") or "Bounded downstream impact was collected."),
        ),
    ]
    next_step = (
        migration_plan[0]
        if assessment == SchemaChangeAssessment.COMPATIBLE
        else "Review the migration plan and blast radius before requesting any approval-gated action."
    )

    return output, references, confidence, next_step, requires_approval


# --- Defining LangGraph Nodes
class SchemaDriftNodeFactory:
    """Build deterministic schema assessment nodes around existing audited tools."""

    def __init__(self, config: SchemaDriftRuntimeConfig) -> None:
        """
        Initialize the node factory.

        Args:
            config: Runtime tool dependencies and connection overrides.

        Returns:
            None.
        """
        self.config = config

    def collect_schema_evidence_node(self, graph_state: SchemaDriftGraphState) -> dict[str, Any]:
        """
        Load one exact persisted schema run through the audited evidence tool.

        Args:
            graph_state: Current specialist graph channels.

        Returns:
            State update containing bounded schema context.
        """
        task       = AgentTaskEnvelope.model_validate(graph_state["task"])
        task_input = SchemaDriftTaskInput.model_validate(graph_state["task_input"])
        context    = self.config.schema_context_fetcher(
            source_run_id=task_input.source_schema_run_id,
            qualified_name=task_input.qualified_name,
            finding_limit=task_input.finding_limit,
            agent_run_id=task.parent_run_id,
            alert_key=task.alert_key,
            clickhouse_host=self.config.clickhouse_host,
            clickhouse_port=self.config.clickhouse_port,
        )

        logger.info(
            "Schema specialist collected detector evidence | task_id=%s findings=%d",
            task.task_id,
            int(context.get("finding_count", 0) or 0),
        )

        return {"schema_context": context}

    def collect_impact_context_node(self, graph_state: SchemaDriftGraphState) -> dict[str, Any]:
        """
        Load exact metadata and bounded downstream impact for the affected asset.

        Args:
            graph_state: Current specialist graph channels.

        Returns:
            State update containing public metadata and dbt blast radius.
        """
        task       = AgentTaskEnvelope.model_validate(graph_state["task"])
        task_input = SchemaDriftTaskInput.model_validate(graph_state["task_input"])
        metadata_asset = self.config.metadata_getter(
            qualified_name=task_input.qualified_name,
            agent_run_id=task.parent_run_id,
            alert_key=task.alert_key,
            clickhouse_host=self.config.clickhouse_host,
            clickhouse_port=self.config.clickhouse_port,
        )
        blast_radius = self.config.blast_radius_fetcher(
            table_name=task_input.qualified_name,
            agent_run_id=task.parent_run_id,
            alert_key=task.alert_key,
            manifest_path=self.config.manifest_path,
            manifest_s3_uri=self.config.manifest_s3_uri,
            endpoint_url=self.config.s3_endpoint_url,
            max_depth=task_input.max_depth,
            max_nodes=task_input.max_nodes,
            clickhouse_host=self.config.clickhouse_host,
            clickhouse_port=self.config.clickhouse_port,
        )

        logger.info(
            "Schema specialist collected impact context | task_id=%s matched=%s impacted_assets=%d",
            task.task_id,
            bool(blast_radius.get("matched", False)),
            int(blast_radius.get("impacted_asset_count", 0) or 0),
        )

        return {
            "metadata_asset": metadata_asset,
            "blast_radius": blast_radius,
        }

    def assess_schema_change_node(self, graph_state: SchemaDriftGraphState) -> dict[str, Any]:
        """
        Build compatibility, impact, and non-executing migration guidance.

        Args:
            graph_state: Current specialist graph channels.

        Returns:
            State update containing typed output and evidence references.
        """
        task_input = SchemaDriftTaskInput.model_validate(graph_state["task_input"])
        output, references, confidence, next_step, requires_approval = build_schema_drift_output(
            task_input=task_input,
            schema_context=dict(graph_state.get("schema_context", {})),
            metadata_asset=dict(graph_state.get("metadata_asset", {})),
            blast_radius=dict(graph_state.get("blast_radius", {})),
        )

        logger.info(
            "Schema specialist assessed change | source_run_id=%s table=%s assessment=%s impact=%s",
            task_input.source_schema_run_id,
            task_input.qualified_name,
            output.assessment.value,
            output.impact_level.value,
        )

        return {
            "output": output.model_dump(mode="json"),
            "evidence_references": [item.model_dump(mode="json") for item in references],
            "confidence": confidence,
            "recommended_next_step": next_step,
            "requires_human_approval": requires_approval,
        }


# --- Building The Specialist Subgraph
def build_schema_drift_graph(config: SchemaDriftRuntimeConfig):
    """
    Compile the bounded Schema Drift Agent LangGraph subgraph.

    Args:
        config: Runtime tool dependencies and connection overrides.

    Returns:
        Compiled LangGraph application.
    """
    from langgraph.graph import END, StateGraph

    nodes    = SchemaDriftNodeFactory(config=config)
    workflow = StateGraph(SchemaDriftGraphState)

    workflow.add_node("collect_schema_evidence", nodes.collect_schema_evidence_node)
    workflow.add_node("collect_impact_context", nodes.collect_impact_context_node)
    workflow.add_node("assess_schema_change", nodes.assess_schema_change_node)

    workflow.set_entry_point("collect_schema_evidence")
    workflow.add_edge("collect_schema_evidence", "collect_impact_context")
    workflow.add_edge("collect_impact_context", "assess_schema_change")
    workflow.add_edge("assess_schema_change", END)

    logger.info("Compiled Schema Drift Agent subgraph")

    return workflow.compile()


# --- Defining Failure And Audit Helpers
def sanitize_specialist_error(exc: Exception) -> str:
    """
    Build a bounded single-line schema specialist failure message.

    Args:
        exc: Policy, graph, tool, or audit exception.

    Returns:
        Sanitized error type and message.
    """
    return f"{type(exc).__name__}: {' '.join(str(exc).split())[:1_500]}"


def build_failed_result(
    task: AgentTaskEnvelope,
    status: AgentTaskStatus,
    error_message: str,
    duration_ms: int,
) -> AgentResultEnvelope:
    """
    Build one failure-isolated schema specialist result.

    Args:
        task: Source handoff task.
        status: Blocked or failed terminal state.
        error_message: Sanitized failure detail.
        duration_ms: Runtime before failure.

    Returns:
        Terminal result with no model usage or mutation.
    """
    return AgentResultEnvelope(
        task_id=task.task_id,
        parent_run_id=task.parent_run_id,
        specialist_name=task.specialist_name,
        task_type=task.task_type,
        status=status,
        confidence=0.0,
        model_route=AgentModelRoute.NO_LLM_FALLBACK,
        token_usage=0,
        estimated_cost_usd=0.0,
        duration_ms=duration_ms,
        errors=[error_message],
        recommended_next_step=(
            "Review the exact detector run, specialist policy, or deterministic tool failure before retrying."
        ),
        requires_human_approval=False,
    )


def write_handoff_audit(
    config: SchemaDriftRuntimeConfig,
    client: Any,
    task: AgentTaskEnvelope,
    action: str,
    status: str,
    duration_ms: int | None = None,
    output_payload: dict[str, Any] | None = None,
    error_message: str = "",
) -> None:
    """
    Persist one bounded schema specialist handoff event.

    Args:
        config: Runtime dependencies containing the audit writer.
        client: ClickHouse audit client.
        task: Correlated specialist handoff.
        action: Stable handoff or assessment action.
        status: running, success, blocked, failed, or assessment state.
        duration_ms: Optional handoff duration.
        output_payload: Optional bounded terminal metadata.
        error_message: Optional sanitized failure message.

    Returns:
        None.
    """
    config.audit_writer(
        client=client,
        action=action,
        status=status,
        agent_run_id=task.parent_run_id,
        alert_key=task.alert_key,
        actor="supervisor_lite",
        tool_name=SPECIALIST_TOOL_NAME,
        duration_ms=duration_ms,
        input_payload={
            "task_id": str(task.task_id),
            "parent_run_id": str(task.parent_run_id),
            "specialist_name": task.specialist_name,
            "task_type": task.task_type,
            "risk_tier": task.risk_tier.value,
            "allowed_tools": list(task.allowed_tools),
            "model_route": task.model_route.value,
            "model_call_budget": task.model_call_budget,
            "token_budget": task.token_budget,
            "estimated_cost_budget_usd": task.estimated_cost_budget_usd,
            "timeout_seconds": task.timeout_seconds,
            "requester": task.requester,
            "context_reference_count": len(task.context_references),
            "source_schema_run_id": str(task.input_payload.get("source_schema_run_id", "")),
            "qualified_name": str(task.input_payload.get("qualified_name", "")),
        },
        output_payload=output_payload or {},
        error_message=error_message,
    )


# --- Running The Specialist
def run_schema_drift_agent(
    task: AgentTaskEnvelope,
    config: SchemaDriftRuntimeConfig | None = None,
) -> AgentResultEnvelope:
    """
    Execute one bounded schema assessment with audit and failure isolation.

    Args:
        task: Typed and correlated specialist task.
        config: Optional runtime dependency overrides.

    Returns:
        Success, blocked, or failed result; tool exceptions do not escape.
    """
    runtime       = config or SchemaDriftRuntimeConfig()
    started       = time.monotonic()
    audit_client: Any | None = None

    try:
        audit_client = runtime.audit_client_factory(
            host=runtime.clickhouse_host,
            port=runtime.clickhouse_port,
        )

        try:
            task_input = validate_schema_drift_task(task)

        except (LookupError, PermissionError, ValueError) as exc:
            duration_ms   = int((time.monotonic() - started) * 1_000)
            error_message = sanitize_specialist_error(exc)
            blocked_result = build_failed_result(
                task=task,
                status=AgentTaskStatus.BLOCKED,
                error_message=error_message,
                duration_ms=duration_ms,
            )
            write_handoff_audit(
                config=runtime,
                client=audit_client,
                task=task,
                action="specialist_handoff_rejected",
                status="blocked",
                duration_ms=duration_ms,
                output_payload={"result_status": blocked_result.status.value},
                error_message=error_message,
            )

            logger.warning(
                "Schema specialist handoff rejected | task_id=%s error=%s",
                task.task_id,
                error_message,
            )

            return blocked_result

        write_handoff_audit(
            config=runtime,
            client=audit_client,
            task=task,
            action="specialist_handoff_started",
            status="running",
        )

        graph  = build_schema_drift_graph(config=runtime)
        result = graph.invoke(
            {
                "task": task.model_dump(mode="json"),
                "task_input": task_input.model_dump(mode="json"),
                "schema_context": {},
                "metadata_asset": {},
                "blast_radius": {},
            }
        )
        duration_ms = int((time.monotonic() - started) * 1_000)
        output      = SchemaDriftAgentOutput.model_validate(result["output"])
        references  = [
            EvidenceReference.model_validate(item)
            for item in result.get("evidence_references", [])
        ]
        final_result = AgentResultEnvelope(
            task_id=task.task_id,
            parent_run_id=task.parent_run_id,
            specialist_name=task.specialist_name,
            task_type=task.task_type,
            status=AgentTaskStatus.SUCCESS,
            evidence_references=references,
            structured_output=output.model_dump(mode="json"),
            confidence=float(result.get("confidence", 0.0)),
            model_route=AgentModelRoute.NO_LLM_FALLBACK,
            token_usage=0,
            estimated_cost_usd=0.0,
            duration_ms=duration_ms,
            recommended_next_step=str(result.get("recommended_next_step", "")),
            requires_human_approval=bool(result.get("requires_human_approval", False)),
        )

        write_handoff_audit(
            config=runtime,
            client=audit_client,
            task=task,
            action="assess_schema_drift_policy",
            status=output.assessment.value,
            duration_ms=duration_ms,
            output_payload={
                "source_schema_run_id": output.source_schema_run_id,
                "qualified_name": output.qualified_name,
                "assessment": output.assessment.value,
                "impact_level": output.impact_level.value,
                "finding_count": output.finding_count,
                "impacted_asset_count": int(output.blast_radius.get("impacted_asset_count", 0) or 0),
                "impacted_test_count": output.impacted_test_count,
                "execution_performed": output.execution_performed,
                "requires_human_approval": final_result.requires_human_approval,
            },
        )
        write_handoff_audit(
            config=runtime,
            client=audit_client,
            task=task,
            action="specialist_handoff_completed",
            status="success",
            duration_ms=duration_ms,
            output_payload={
                "result_status": final_result.status.value,
                "confidence": final_result.confidence,
                "evidence_reference_count": len(final_result.evidence_references),
                "source_schema_run_id": output.source_schema_run_id,
                "qualified_name": output.qualified_name,
                "assessment": output.assessment.value,
                "impact_level": output.impact_level.value,
                "finding_count": output.finding_count,
                "impacted_asset_count": int(output.blast_radius.get("impacted_asset_count", 0) or 0),
                "impacted_test_count": output.impacted_test_count,
                "execution_performed": output.execution_performed,
                "model_route": final_result.model_route.value,
                "model_call_count": final_result.model_call_count,
                "token_usage": final_result.token_usage,
                "estimated_cost_usd": final_result.estimated_cost_usd,
                "requires_human_approval": final_result.requires_human_approval,
            },
        )

        logger.info(
            "Schema specialist handoff completed | task_id=%s assessment=%s execution_performed=false",
            task.task_id,
            output.assessment.value,
        )

        return final_result

    except Exception as exc:
        duration_ms   = int((time.monotonic() - started) * 1_000)
        error_message = sanitize_specialist_error(exc)
        failed_result = build_failed_result(
            task=task,
            status=AgentTaskStatus.FAILED,
            error_message=error_message,
            duration_ms=duration_ms,
        )

        logger.exception(
            "Schema specialist handoff failed | task_id=%s parent_run_id=%s",
            task.task_id,
            task.parent_run_id,
        )

        if audit_client is not None:
            try:
                write_handoff_audit(
                    config=runtime,
                    client=audit_client,
                    task=task,
                    action="specialist_handoff_failed",
                    status="failed",
                    duration_ms=duration_ms,
                    output_payload={"result_status": failed_result.status.value},
                    error_message=error_message,
                )

            except Exception:
                logger.exception(
                    "Failed to persist schema specialist failure audit | task_id=%s",
                    task.task_id,
                )

        return failed_result
