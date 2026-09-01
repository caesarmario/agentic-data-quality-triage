####
## Metadata And Lineage Specialist Agent for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Bounded LangGraph specialist for trusted metadata and dbt impact evidence."""

# --- Importing Libraries
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypedDict
from uuid import NAMESPACE_URL, UUID, uuid5

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
    METADATA_LINEAGE_SPECIALIST_NAME,
    enforce_task_capability,
    required_tools_for_task,
)
from agent.tools.audit_log import write_agent_audit_event
from agent.tools.dbt_lineage import fetch_dbt_blast_radius, fetch_dbt_lineage
from agent.tools.metadata_catalog import get_metadata_asset, search_metadata_assets
from pipelines.common.clickhouse import build_clickhouse_client, validate_qualified_table_name
from pipelines.common.logging import logger


# --- Defining Constants
SPECIALIST_TOOL_NAME      = "metadata_lineage_agent"
DEFAULT_RESULT_LIMIT      = 10
MAX_RESULT_LIMIT          = 25
DEFAULT_MAX_DEPTH         = 5
DEFAULT_MAX_NODES         = 100
TRUST_STATUS_TRUSTED      = "trusted"
TRUST_STATUS_REVIEW       = "review"
TRUST_STATUS_NOT_FOUND    = "not_found"
TRUST_STATUS_NOT_RECOMMENDED = "not_recommended"


# --- Defining Specialist Models
class MetadataLineageTaskInput(BaseModel):
    """
    Define bounded metadata and lineage input accepted by the specialist.

    Attributes:
        qualified_name: Exact database.table asset for context or impact analysis.
        query: Optional bounded metadata discovery query.
        domain: Optional metadata domain filter.
        data_layer: Optional raw, staging, or mart filter.
        certification_status: Optional trust certification filter.
        lifecycle_status: Optional active or deprecated filter.
        limit: Maximum metadata search results.
        max_depth: Maximum downstream dbt traversal depth.
        max_nodes: Maximum downstream dbt nodes returned.
    """

    model_config = ConfigDict(extra="forbid")

    qualified_name: str                = Field(default="", max_length=255)
    query: str                         = Field(default="", max_length=120)
    domain: str                        = Field(default="", max_length=80)
    data_layer: str                    = Field(default="", max_length=20)
    certification_status: str         = Field(default="", max_length=30)
    lifecycle_status: str             = Field(default="", max_length=30)
    limit: int                         = Field(default=DEFAULT_RESULT_LIMIT, ge=1, le=MAX_RESULT_LIMIT)
    max_depth: int                     = Field(default=DEFAULT_MAX_DEPTH, ge=1, le=10)
    max_nodes: int                     = Field(default=DEFAULT_MAX_NODES, ge=1, le=250)

    @field_validator(
        "qualified_name",
        "query",
        "domain",
        "data_layer",
        "certification_status",
        "lifecycle_status",
    )
    @classmethod
    def normalize_input_text(cls, value: str) -> str:
        """
        Normalize bounded specialist text fields.

        Args:
            value: Raw task input value.

        Returns:
            Trimmed text value.
        """
        return value.strip()


class MetadataLineageAgentOutput(BaseModel):
    """
    Define deterministic output returned by the metadata and lineage specialist.

    Attributes:
        task_type: Completed specialist task.
        requested_asset: Exact requested asset, when applicable.
        metadata_assets: Bounded public metadata records.
        lineage: Direct dbt lineage summary without raw SQL.
        blast_radius: Bounded downstream impact summary without raw SQL.
        trust_status: Explainable asset trust classification.
        trust_reasons: Deterministic reasons behind the classification.
        summary: Operator-facing specialist summary.
    """

    model_config = ConfigDict(extra="forbid")

    task_type: str
    requested_asset: str                 = ""
    metadata_assets: list[dict[str, Any]] = Field(default_factory=list, max_length=MAX_RESULT_LIMIT)
    lineage: dict[str, Any]               = Field(default_factory=dict)
    blast_radius: dict[str, Any]          = Field(default_factory=dict)
    trust_status: str
    trust_reasons: list[str]              = Field(default_factory=list, max_length=20)
    summary: str                          = Field(min_length=1, max_length=2_000)


class MetadataLineageGraphState(TypedDict, total=False):
    """Represent serializable channels used by the specialist LangGraph subgraph."""

    task: dict[str, Any]
    task_input: dict[str, Any]
    metadata_assets: list[dict[str, Any]]
    lineage: dict[str, Any]
    blast_radius: dict[str, Any]
    output: dict[str, Any]
    evidence_references: list[dict[str, Any]]
    confidence: float
    recommended_next_step: str


@dataclass(frozen=True)
class MetadataLineageRuntimeConfig:
    """
    Inject deterministic tools and connection overrides into the specialist.

    Attributes:
        metadata_searcher: Bounded metadata catalog search callable.
        metadata_getter: Exact metadata asset lookup callable.
        lineage_fetcher: Direct dbt lineage callable.
        blast_radius_fetcher: Bounded transitive dbt impact callable.
        audit_client_factory: ClickHouse client factory used for handoff audit events.
        audit_writer: Audit event persistence callable.
        manifest_path: Optional system-owned local manifest path.
        manifest_s3_uri: Optional system-owned S3 manifest URI.
        s3_endpoint_url: Optional SeaweedFS endpoint override.
        clickhouse_host: Optional ClickHouse host override.
        clickhouse_port: Optional ClickHouse HTTP port override.
    """

    metadata_searcher: Callable[..., dict[str, Any]] = search_metadata_assets
    metadata_getter: Callable[..., dict[str, Any]] = get_metadata_asset
    lineage_fetcher: Callable[..., dict[str, Any]] = fetch_dbt_lineage
    blast_radius_fetcher: Callable[..., dict[str, Any]] = fetch_dbt_blast_radius
    audit_client_factory: Callable[..., Any] = build_clickhouse_client
    audit_writer: Callable[..., UUID] = write_agent_audit_event
    manifest_path: str | Path | None = None
    manifest_s3_uri: str | None = None
    s3_endpoint_url: str | None = None
    clickhouse_host: str | None = None
    clickhouse_port: int | None = None


# --- Defining Task Validation Helpers
def derive_metadata_lineage_parent_run_id(run_id: str) -> UUID:
    """
    Derive a stable UUID correlation key from an Airflow or operator run ID.

    Args:
        run_id: Stable external run identifier.

    Returns:
        Deterministic UUID used across handoff and tool audit events.

    Raises:
        ValueError: If the external run identifier is blank.
    """
    normalized = run_id.strip()

    if not normalized:
        raise ValueError("Metadata-lineage run_id cannot be blank.")

    return uuid5(NAMESPACE_URL, f"agentic-dq:metadata-lineage:{normalized}")


def validate_metadata_lineage_task(task: AgentTaskEnvelope) -> MetadataLineageTaskInput:
    """
    Validate capability policy and specialist-specific input together.

    Args:
        task: Typed task handoff supplied by the supervisor.

    Returns:
        Validated MetadataLineageTaskInput.

    Raises:
        PermissionError: If capability policy rejects the task.
        ValueError: If task-specific required inputs are missing or unsafe.
    """
    enforce_task_capability(task)
    task_input = MetadataLineageTaskInput.model_validate(task.input_payload)

    if task.task_type in {"asset_context", "blast_radius"}:
        if not task_input.qualified_name:
            raise ValueError(f"{task.task_type} requires qualified_name.")

        task_input.qualified_name = validate_qualified_table_name(task_input.qualified_name)

    logger.info(
        "Validated metadata-lineage task | task_id=%s task_type=%s qualified_name=%s",
        task.task_id,
        task.task_type,
        task_input.qualified_name,
    )

    return task_input


def build_metadata_lineage_task(
    parent_run_id: UUID,
    task_type: str,
    qualified_name: str = "",
    query: str = "",
    domain: str = "",
    data_layer: str = "",
    certification_status: str = "",
    lifecycle_status: str = "",
    limit: int = DEFAULT_RESULT_LIMIT,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_nodes: int = DEFAULT_MAX_NODES,
    requester: str = "control_plane",
    alert_key: str = "",
) -> AgentTaskEnvelope:
    """
    Build one policy-compliant task for the metadata and lineage specialist.

    Args:
        parent_run_id: Parent investigation correlation UUID.
        task_type: asset_context, blast_radius, or trusted_asset_search.
        qualified_name: Exact database.table identity for asset tasks.
        query: Optional metadata discovery query.
        domain: Optional metadata domain filter.
        data_layer: Optional warehouse layer filter.
        certification_status: Optional trust certification filter.
        lifecycle_status: Optional lifecycle filter.
        limit: Maximum metadata assets returned.
        max_depth: Maximum downstream dbt traversal depth.
        max_nodes: Maximum downstream dbt nodes returned.
        requester: Interface or system requesting the handoff.
        alert_key: Optional alert correlation key.

    Returns:
        Validated least-privilege AgentTaskEnvelope.
    """
    normalized_task_type = task_type.strip().lower()
    task_input            = MetadataLineageTaskInput(
        qualified_name=qualified_name,
        query=query,
        domain=domain,
        data_layer=data_layer,
        certification_status=certification_status,
        lifecycle_status=lifecycle_status,
        limit=limit,
        max_depth=max_depth,
        max_nodes=max_nodes,
    )

    if normalized_task_type == "trusted_asset_search":
        # Search tasks receive only query/filter context, never an unrelated exact asset.
        task_input.qualified_name = ""

    context_references: list[ContextReference] = []

    if task_input.qualified_name:
        context_references.append(
            ContextReference(
                reference_type=ContextReferenceType.METADATA_ASSET,
                reference=task_input.qualified_name,
                description="Warehouse asset requested for trusted metadata and dbt lineage context.",
            )
        )

    task = AgentTaskEnvelope(
        parent_run_id=parent_run_id,
        specialist_name=METADATA_LINEAGE_SPECIALIST_NAME,
        task_type=normalized_task_type,
        risk_tier=AgentRiskTier.LOW,
        allowed_tools=required_tools_for_task(
            specialist_name=METADATA_LINEAGE_SPECIALIST_NAME,
            task_type=normalized_task_type,
        ),
        context_references=context_references,
        model_route=AgentModelRoute.NO_LLM_FALLBACK,
        model_call_budget=0,
        token_budget=0,
        estimated_cost_budget_usd=0.0,
        timeout_seconds=60,
        requester=requester,
        alert_key=alert_key,
        input_payload=task_input.model_dump(mode="json"),
    )

    # Validate required task-specific fields before the envelope leaves its caller.
    validate_metadata_lineage_task(task)

    logger.info(
        "Built metadata-lineage handoff | task_id=%s parent_run_id=%s task_type=%s",
        task.task_id,
        task.parent_run_id,
        task.task_type,
    )

    return task


# --- Defining Deterministic Trust Logic
def assess_metadata_trust(
    metadata_assets: list[dict[str, Any]],
    lineage: dict[str, Any],
    blast_radius: dict[str, Any],
) -> tuple[str, list[str], float]:
    """
    Classify asset trust from registry and lineage facts without using an LLM.

    Args:
        metadata_assets: Public metadata registry records.
        lineage: Direct dbt lineage summary.
        blast_radius: Bounded downstream impact summary.

    Returns:
        Tuple containing trust status, explainable reasons, and confidence.
    """
    if not metadata_assets:
        return (
            TRUST_STATUS_NOT_FOUND,
            ["No active metadata asset matched the request."],
            0.20,
        )

    asset         = metadata_assets[0]
    certification = str(asset.get("certification_status", "")).lower()
    lifecycle     = str(asset.get("lifecycle_status", "")).lower()
    contains_pii  = bool(asset.get("contains_pii", False))
    reasons       = [
        f"Certification status is {certification or 'unknown'}.",
        f"Lifecycle status is {lifecycle or 'unknown'}.",
        f"Documented grain is {asset.get('grain') or 'not available'}",
        f"Technical owner is {asset.get('technical_owner') or 'not assigned'}.",
    ]

    if contains_pii:
        reasons.append("The metadata contract marks this asset as containing PII.")

    if lifecycle != "active" or certification in {"deprecated", "experimental"}:
        return TRUST_STATUS_NOT_RECOMMENDED, reasons, 0.95

    lineage_matched = bool(lineage.get("matched")) if lineage else False
    blast_matched   = bool(blast_radius.get("matched")) if blast_radius else False
    truncated       = bool(blast_radius.get("truncated")) if blast_radius else False

    if lineage and not lineage_matched:
        reasons.append("The asset did not match a dbt lineage node.")

    if blast_radius and not blast_matched:
        reasons.append("Downstream blast radius could not be resolved from the dbt manifest.")

    if truncated:
        reasons.append("Blast-radius traversal reached a configured safety bound.")

    if certification == "certified" and not truncated and (lineage_matched or blast_matched):
        return TRUST_STATUS_TRUSTED, reasons, 0.95

    if certification == "candidate" and not truncated and (lineage_matched or blast_matched):
        return TRUST_STATUS_REVIEW, reasons, 0.85

    return TRUST_STATUS_REVIEW, reasons, 0.75


def build_metadata_lineage_output(
    task: AgentTaskEnvelope,
    task_input: MetadataLineageTaskInput,
    metadata_assets: list[dict[str, Any]],
    lineage: dict[str, Any],
    blast_radius: dict[str, Any],
) -> tuple[MetadataLineageAgentOutput, list[EvidenceReference], float, str]:
    """
    Build bounded specialist output, evidence references, confidence, and next step.

    Args:
        task: Authorized specialist handoff.
        task_input: Validated specialist-specific input.
        metadata_assets: Public metadata records returned by the registry tool.
        lineage: Direct dbt lineage summary.
        blast_radius: Bounded transitive dbt impact summary.

    Returns:
        Tuple containing output model, evidence references, confidence, and next step.
    """
    trust_status, trust_reasons, confidence = assess_metadata_trust(
        metadata_assets=metadata_assets,
        lineage=lineage,
        blast_radius=blast_radius,
    )
    evidence_references: list[EvidenceReference] = []

    # Retain proof of the bounded catalog lookup even when no matching asset is found.
    evidence_references.append(
        EvidenceReference(
            evidence_type="metadata_catalog_query",
            source_tool="metadata_catalog",
            reference=f"task:{task.task_id}",
            summary=(
                f"Metadata catalog query for {task.task_type} completed and returned "
                f"{len(metadata_assets)} bounded asset(s)."
            ),
        )
    )

    for asset in metadata_assets:
        qualified_name = str(asset.get("qualified_name", "")).strip()

        if qualified_name:
            evidence_references.append(
                EvidenceReference(
                    evidence_type="metadata_asset",
                    source_tool="metadata_catalog",
                    reference=qualified_name,
                    summary=(
                        f"{qualified_name} is {asset.get('certification_status', 'unknown')} and owned by "
                        f"{asset.get('technical_owner', 'an unassigned team')}."
                    ),
                )
            )

    if lineage:
        node_reference = str((lineage.get("node") or {}).get("unique_id") or task_input.qualified_name)
        evidence_references.append(
            EvidenceReference(
                evidence_type="dbt_lineage",
                source_tool="dbt_lineage",
                reference=node_reference,
                summary=str(lineage.get("summary") or "Direct dbt lineage context was collected."),
            )
        )

    if blast_radius:
        node_reference = str((blast_radius.get("node") or {}).get("unique_id") or task_input.qualified_name)
        evidence_references.append(
            EvidenceReference(
                evidence_type="blast_radius",
                source_tool="dbt_blast_radius",
                reference=node_reference,
                summary=str(blast_radius.get("summary") or "Bounded downstream impact was collected."),
            )
        )

    if task.task_type == "trusted_asset_search":
        summary = (
            f"Found {len(metadata_assets)} bounded metadata asset(s); "
            f"the result set is classified as {trust_status}."
        )
        next_step = (
            "Review the highest-certification asset's grain, owner, and SLA before using it in a query."
            if metadata_assets
            else "Synchronize or improve the metadata registry before selecting a warehouse asset."
        )
    else:
        impacted_assets = int(blast_radius.get("impacted_asset_count", 0)) if blast_radius else 0
        impacted_tests  = int(blast_radius.get("impacted_test_count", 0)) if blast_radius else 0
        summary = (
            f"{task_input.qualified_name} is classified as {trust_status}; "
            f"bounded blast radius contains {impacted_assets} downstream asset(s) and "
            f"{impacted_tests} test(s)."
        )
        next_step = (
            "Use the returned ownership, grain, certification, and blast radius before approving any remediation."
        )

    output = MetadataLineageAgentOutput(
        task_type=task.task_type,
        requested_asset=task_input.qualified_name,
        metadata_assets=metadata_assets,
        lineage=lineage,
        blast_radius=blast_radius,
        trust_status=trust_status,
        trust_reasons=trust_reasons,
        summary=summary,
    )

    return output, evidence_references, confidence, next_step


# --- Defining LangGraph Nodes
class MetadataLineageNodeFactory:
    """
    Build deterministic specialist nodes around existing audited tools.

    Attributes:
        config: Runtime dependency and connection configuration.
    """

    def __init__(self, config: MetadataLineageRuntimeConfig) -> None:
        """
        Initialize the node factory.

        Args:
            config: Runtime tool dependencies and connection overrides.

        Returns:
            None.
        """
        self.config = config

    def collect_metadata_node(self, graph_state: MetadataLineageGraphState) -> dict[str, Any]:
        """
        Collect exact or searched metadata through the existing catalog tool.

        Args:
            graph_state: Current specialist graph channels.

        Returns:
            State update containing bounded public metadata assets.
        """
        task       = AgentTaskEnvelope.model_validate(graph_state["task"])
        task_input = MetadataLineageTaskInput.model_validate(graph_state["task_input"])

        if task.task_type == "trusted_asset_search":
            result = self.config.metadata_searcher(
                query=task_input.query or None,
                domain=task_input.domain or None,
                data_layer=task_input.data_layer or None,
                certification_status=task_input.certification_status or None,
                lifecycle_status=task_input.lifecycle_status or None,
                limit=task_input.limit,
                agent_run_id=task.parent_run_id,
                alert_key=task.alert_key,
                clickhouse_host=self.config.clickhouse_host,
                clickhouse_port=self.config.clickhouse_port,
            )
            metadata_assets = list(result.get("assets", []))

        else:
            metadata_asset = self.config.metadata_getter(
                qualified_name=task_input.qualified_name,
                agent_run_id=task.parent_run_id,
                alert_key=task.alert_key,
                clickhouse_host=self.config.clickhouse_host,
                clickhouse_port=self.config.clickhouse_port,
            )
            metadata_assets = [metadata_asset]

        logger.info(
            "Metadata specialist collected registry context | task_id=%s assets=%d",
            task.task_id,
            len(metadata_assets),
        )

        return {"metadata_assets": metadata_assets}

    def collect_lineage_node(self, graph_state: MetadataLineageGraphState) -> dict[str, Any]:
        """
        Collect only the direct and transitive lineage required by task policy.

        Args:
            graph_state: Current specialist graph channels.

        Returns:
            State update containing direct lineage and/or bounded blast radius.
        """
        task       = AgentTaskEnvelope.model_validate(graph_state["task"])
        task_input = MetadataLineageTaskInput.model_validate(graph_state["task_input"])
        lineage: dict[str, Any]      = {}
        blast_radius: dict[str, Any] = {}

        if task.task_type == "asset_context":
            lineage = self.config.lineage_fetcher(
                table_name=task_input.qualified_name,
                agent_run_id=task.parent_run_id,
                alert_key=task.alert_key,
                manifest_path=self.config.manifest_path,
                manifest_s3_uri=self.config.manifest_s3_uri,
                endpoint_url=self.config.s3_endpoint_url,
                clickhouse_host=self.config.clickhouse_host,
                clickhouse_port=self.config.clickhouse_port,
            )

        if task.task_type in {"asset_context", "blast_radius"}:
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
            "Metadata specialist collected lineage | task_id=%s direct=%s blast_radius=%s",
            task.task_id,
            bool(lineage),
            bool(blast_radius),
        )

        return {
            "lineage": lineage,
            "blast_radius": blast_radius,
        }

    def finalize_context_node(self, graph_state: MetadataLineageGraphState) -> dict[str, Any]:
        """
        Build deterministic trust and impact output from collected evidence.

        Args:
            graph_state: Current specialist graph channels.

        Returns:
            State update containing output, evidence references, confidence, and next step.
        """
        task       = AgentTaskEnvelope.model_validate(graph_state["task"])
        task_input = MetadataLineageTaskInput.model_validate(graph_state["task_input"])
        output, evidence_references, confidence, next_step = build_metadata_lineage_output(
            task=task,
            task_input=task_input,
            metadata_assets=list(graph_state.get("metadata_assets", [])),
            lineage=dict(graph_state.get("lineage", {})),
            blast_radius=dict(graph_state.get("blast_radius", {})),
        )

        logger.info(
            "Metadata specialist finalized context | task_id=%s trust=%s confidence=%.2f",
            task.task_id,
            output.trust_status,
            confidence,
        )

        return {
            "output": output.model_dump(mode="json"),
            "evidence_references": [
                reference.model_dump(mode="json")
                for reference in evidence_references
            ],
            "confidence": confidence,
            "recommended_next_step": next_step,
        }


# --- Building And Running The Specialist Subgraph
def build_metadata_lineage_graph(config: MetadataLineageRuntimeConfig):
    """
    Compile the bounded Metadata and Lineage Agent LangGraph subgraph.

    Args:
        config: Runtime tool dependencies and connection overrides.

    Returns:
        Compiled LangGraph application.
    """
    from langgraph.graph import END, StateGraph

    nodes    = MetadataLineageNodeFactory(config=config)
    workflow = StateGraph(MetadataLineageGraphState)

    workflow.add_node("collect_metadata", nodes.collect_metadata_node)
    workflow.add_node("collect_lineage", nodes.collect_lineage_node)
    workflow.add_node("finalize_context", nodes.finalize_context_node)

    workflow.set_entry_point("collect_metadata")
    workflow.add_edge("collect_metadata", "collect_lineage")
    workflow.add_edge("collect_lineage", "finalize_context")
    workflow.add_edge("finalize_context", END)

    logger.info("Compiled Metadata and Lineage Agent subgraph")

    return workflow.compile()


def sanitize_specialist_error(exc: Exception) -> str:
    """
    Build a bounded single-line specialist failure message.

    Args:
        exc: Exception raised by policy, graph, tool, or audit execution.

    Returns:
        Sanitized error type and message.
    """
    message = " ".join(str(exc).split())[:1_500]

    return f"{type(exc).__name__}: {message}"


def build_failed_result(
    task: AgentTaskEnvelope,
    status: AgentTaskStatus,
    error_message: str,
    duration_ms: int,
) -> AgentResultEnvelope:
    """
    Build a failure-isolated result instead of propagating specialist exceptions.

    Args:
        task: Source handoff task.
        status: Failed or blocked terminal state.
        error_message: Sanitized failure message.
        duration_ms: End-to-end duration before failure.

    Returns:
        Terminal AgentResultEnvelope with no model usage or mutation action.
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
            "Review the rejected handoff policy or deterministic tool failure before retrying."
        ),
        requires_human_approval=False,
    )


def write_handoff_audit(
    config: MetadataLineageRuntimeConfig,
    client: Any,
    task: AgentTaskEnvelope,
    action: str,
    status: str,
    duration_ms: int | None = None,
    output_payload: dict[str, Any] | None = None,
    error_message: str = "",
) -> None:
    """
    Persist one bounded specialist handoff event to ClickHouse.

    Args:
        config: Runtime dependencies containing the audit writer.
        client: ClickHouse audit client.
        task: Correlated specialist handoff.
        action: Handoff lifecycle action.
        status: success, failed, or blocked status.
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
            "qualified_name": str(task.input_payload.get("qualified_name", "")),
        },
        output_payload=output_payload or {},
        error_message=error_message,
    )


def run_metadata_lineage_agent(
    task: AgentTaskEnvelope,
    config: MetadataLineageRuntimeConfig | None = None,
) -> AgentResultEnvelope:
    """
    Execute one bounded specialist handoff with audit and failure isolation.

    Args:
        task: Typed and correlated specialist task.
        config: Optional runtime dependency overrides.

    Returns:
        Success, blocked, or failed AgentResultEnvelope. Tool exceptions do not escape.
    """
    runtime       = config or MetadataLineageRuntimeConfig()
    started       = time.monotonic()
    audit_client: Any | None = None

    try:
        audit_client = runtime.audit_client_factory(
            host=runtime.clickhouse_host,
            port=runtime.clickhouse_port,
        )

        try:
            task_input = validate_metadata_lineage_task(task)

        except (LookupError, PermissionError, ValueError) as exc:
            duration_ms  = int((time.monotonic() - started) * 1_000)
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
                "Metadata specialist handoff rejected | task_id=%s error=%s",
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

        graph  = build_metadata_lineage_graph(config=runtime)
        result = graph.invoke(
            {
                "task": task.model_dump(mode="json"),
                "task_input": task_input.model_dump(mode="json"),
                "metadata_assets": [],
                "lineage": {},
                "blast_radius": {},
            }
        )
        duration_ms = int((time.monotonic() - started) * 1_000)
        output      = MetadataLineageAgentOutput.model_validate(result["output"])
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
            requires_human_approval=False,
        )
        qualified_names = [
            str(asset.get("qualified_name", ""))
            for asset in output.metadata_assets
            if asset.get("qualified_name")
        ]

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
                "qualified_names": qualified_names,
                "trust_status": output.trust_status,
                "impacted_asset_count": int(output.blast_radius.get("impacted_asset_count", 0)),
                "impacted_test_count": int(output.blast_radius.get("impacted_test_count", 0)),
                "model_route": final_result.model_route.value,
                "model_call_count": final_result.model_call_count,
                "token_usage": final_result.token_usage,
                "estimated_cost_usd": final_result.estimated_cost_usd,
                "requires_human_approval": final_result.requires_human_approval,
            },
        )

        logger.info(
            "Metadata specialist handoff completed | task_id=%s parent_run_id=%s evidence=%d",
            task.task_id,
            task.parent_run_id,
            len(final_result.evidence_references),
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
            "Metadata specialist handoff failed | task_id=%s parent_run_id=%s",
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
                    "Failed to persist specialist failure audit | task_id=%s",
                    task.task_id,
                )

        return failed_result
