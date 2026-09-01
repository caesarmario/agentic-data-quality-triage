####
## Control Plane Supervisor Routing for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Classify bounded operator intent and build least-privilege specialist handoffs."""

# --- Importing Libraries
from __future__ import annotations

from uuid import UUID

from agent.specialists.contracts import AgentTaskEnvelope
from agent.specialists.incident_triage import build_incident_triage_task
from agent.specialists.metadata_lineage import build_metadata_lineage_task
from agent.specialists.schema_drift import build_schema_drift_task
from agent.specialists.sql_review import build_sql_review_task
from agent.specialists.registry import (
    INCIDENT_TRIAGE_SPECIALIST_NAME,
    METADATA_LINEAGE_SPECIALIST_NAME,
    SCHEMA_DRIFT_SPECIALIST_NAME,
    SQL_REVIEW_SPECIALIST_NAME,
)
from agent.supervisor.models import SupervisorIntent, SupervisorRequest, SupervisorRoute
from pipelines.common.logging import logger


# --- Defining Constants
TRIAGE_KEYWORDS = (
    "alert",
    "incident",
    "root cause",
    "triage",
    "why did",
    "what happened",
)

BLAST_RADIUS_KEYWORDS = (
    "blast radius",
    "downstream",
    "impact",
    "impacted",
)

ASSET_CONTEXT_KEYWORDS = (
    "asset context",
    "metadata",
    "owner",
    "ownership",
    "grain",
    "sla",
    "trust",
)

SEARCH_KEYWORDS = (
    "find table",
    "find asset",
    "search table",
    "search asset",
    "trusted table",
    "trusted asset",
)

ROUTE_BY_INTENT = {
    SupervisorIntent.TRIAGE_ALERT: SupervisorRoute(
        intent=SupervisorIntent.TRIAGE_ALERT,
        specialist_name=INCIDENT_TRIAGE_SPECIALIST_NAME,
        task_type="triage_alert",
        rationale="An explicit alert identity requires the evidence-driven incident workflow.",
    ),
    SupervisorIntent.ASSET_CONTEXT: SupervisorRoute(
        intent=SupervisorIntent.ASSET_CONTEXT,
        specialist_name=METADATA_LINEAGE_SPECIALIST_NAME,
        task_type="asset_context",
        rationale="An exact warehouse asset requires trusted metadata and direct lineage context.",
    ),
    SupervisorIntent.BLAST_RADIUS: SupervisorRoute(
        intent=SupervisorIntent.BLAST_RADIUS,
        specialist_name=METADATA_LINEAGE_SPECIALIST_NAME,
        task_type="blast_radius",
        rationale="A downstream impact request requires bounded transitive dbt lineage traversal.",
    ),
    SupervisorIntent.TRUSTED_ASSET_SEARCH: SupervisorRoute(
        intent=SupervisorIntent.TRUSTED_ASSET_SEARCH,
        specialist_name=METADATA_LINEAGE_SPECIALIST_NAME,
        task_type="trusted_asset_search",
        rationale="A discovery request requires bounded metadata trust registry search.",
    ),
    SupervisorIntent.REVIEW_SQL: SupervisorRoute(
        intent=SupervisorIntent.REVIEW_SQL,
        specialist_name=SQL_REVIEW_SPECIALIST_NAME,
        task_type="review_sql",
        rationale=(
            "A SQL proposal requires deterministic safety, metadata trust, and scan-risk "
            "review before any separate read-only execution request."
        ),
    ),
    SupervisorIntent.SCHEMA_DRIFT_ASSESSMENT: SupervisorRoute(
        intent=SupervisorIntent.SCHEMA_DRIFT_ASSESSMENT,
        specialist_name=SCHEMA_DRIFT_SPECIALIST_NAME,
        task_type="assess_schema_drift",
        rationale=(
            "An exact persisted detector run requires deterministic compatibility, metadata, "
            "and bounded downstream-impact assessment without schema mutation."
        ),
    ),
}


# --- Defining Routing Exceptions
class SupervisorRoutingError(ValueError):
    """Represent an ambiguous or unsupported deterministic routing request."""


# --- Defining Intent Classification
def contains_any(value: str, keywords: tuple[str, ...]) -> bool:
    """
    Check whether normalized text contains any deterministic keyword.

    Args:
        value: Normalized operator question.
        keywords: Bounded keyword policy.

    Returns:
        True when at least one keyword is present.
    """
    return any(keyword in value for keyword in keywords)


def classify_supervisor_intent(request: SupervisorRequest) -> SupervisorIntent:
    """
    Resolve one request through deterministic, non-LLM intent policy.

    Args:
        request: Typed operator request.

    Returns:
        Explicit or auto-classified SupervisorIntent.

    Raises:
        SupervisorRoutingError: If auto classification is ambiguous or unsupported.
    """
    if request.intent != SupervisorIntent.AUTO:
        logger.info("Using explicit supervisor intent | intent=%s", request.intent.value)

        return request.intent

    question  = request.question.lower()
    candidates: set[SupervisorIntent] = set()

    # A supplied SQL proposal is a stronger and less ambiguous signal than prose keywords.
    if request.sql_proposal:
        logger.info("Classified supervisor intent from explicit SQL proposal | resolved=review_sql")

        return SupervisorIntent.REVIEW_SQL

    # An exact persisted detector run is stronger than free-text schema wording.
    if request.schema_run_id:
        logger.info(
            "Classified supervisor intent from exact schema run | resolved=schema_drift_assessment"
        )

        return SupervisorIntent.SCHEMA_DRIFT_ASSESSMENT

    if request.alert_id or request.alert_key:
        if not question or contains_any(question, TRIAGE_KEYWORDS):
            candidates.add(SupervisorIntent.TRIAGE_ALERT)

    if request.qualified_name:
        if contains_any(question, BLAST_RADIUS_KEYWORDS):
            candidates.add(SupervisorIntent.BLAST_RADIUS)

        elif not question or contains_any(question, ASSET_CONTEXT_KEYWORDS):
            candidates.add(SupervisorIntent.ASSET_CONTEXT)

    if request.query or contains_any(question, SEARCH_KEYWORDS):
        candidates.add(SupervisorIntent.TRUSTED_ASSET_SEARCH)

    if len(candidates) != 1:
        candidate_names = ", ".join(sorted(item.value for item in candidates)) or "none"
        raise SupervisorRoutingError(
            "Supervisor auto routing requires exactly one supported intent; "
            f"resolved candidates={candidate_names}. Use an explicit intent."
        )

    resolved = next(iter(candidates))

    logger.info(
        "Classified supervisor intent deterministically | requested=auto resolved=%s",
        resolved.value,
    )

    return resolved


def resolve_supervisor_route(request: SupervisorRequest) -> SupervisorRoute:
    """
    Select one registered specialist from deterministic intent policy.

    Args:
        request: Typed supervisor request.

    Returns:
        Immutable SupervisorRoute.

    Raises:
        SupervisorRoutingError: If no route exists for the resolved intent.
    """
    intent = classify_supervisor_intent(request)
    route  = ROUTE_BY_INTENT.get(intent)

    if route is None:
        raise SupervisorRoutingError(f"No supervisor route exists for intent={intent.value}.")

    logger.info(
        "Selected supervisor specialist | intent=%s specialist=%s task_type=%s",
        route.intent.value,
        route.specialist_name,
        route.task_type,
    )

    return route


# --- Defining Handoff Construction
def build_supervisor_handoff(
    request: SupervisorRequest,
    route: SupervisorRoute,
    parent_run_id: UUID,
) -> AgentTaskEnvelope:
    """
    Build one task envelope using only the selected specialist's public builder.

    Args:
        request: Typed supervisor request.
        route: Deterministic specialist route.
        parent_run_id: Parent supervisor correlation UUID.

    Returns:
        Policy-validated AgentTaskEnvelope.
    """
    if route.specialist_name == INCIDENT_TRIAGE_SPECIALIST_NAME:
        return build_incident_triage_task(
            parent_run_id=parent_run_id,
            alert_id=request.alert_id,
            alert_key=request.alert_key,
            confidence_threshold=request.confidence_threshold,
            max_evidence_iterations=request.max_evidence_iterations,
            manifest_s3_uri=request.manifest_s3_uri,
            artifacts_bucket=request.artifacts_bucket,
            artifacts_prefix=request.artifacts_prefix,
            requester=request.requester,
        )

    if route.specialist_name == METADATA_LINEAGE_SPECIALIST_NAME:
        return build_metadata_lineage_task(
            parent_run_id=parent_run_id,
            task_type=route.task_type,
            qualified_name=request.qualified_name,
            query=request.query or request.question,
            domain=request.domain,
            data_layer=request.data_layer,
            certification_status=request.certification_status,
            lifecycle_status=request.lifecycle_status,
            limit=request.result_limit,
            max_depth=request.max_depth,
            max_nodes=request.max_nodes,
            requester=request.requester,
            alert_key=request.alert_key,
        )

    if route.specialist_name == SQL_REVIEW_SPECIALIST_NAME:
        return build_sql_review_task(
            parent_run_id=parent_run_id,
            sql_proposal=request.sql_proposal,
            purpose=request.sql_purpose or request.question,
            hard_limit=request.sql_hard_limit,
            require_date_filter=request.sql_require_date_filter,
            max_scan_bytes=request.sql_max_scan_bytes,
            requester=request.requester,
            alert_key=request.alert_key,
        )

    if route.specialist_name == SCHEMA_DRIFT_SPECIALIST_NAME:
        return build_schema_drift_task(
            parent_run_id=parent_run_id,
            source_schema_run_id=request.schema_run_id,
            qualified_name=request.qualified_name,
            finding_limit=request.schema_finding_limit,
            max_depth=request.max_depth,
            max_nodes=request.max_nodes,
            manifest_s3_uri=request.manifest_s3_uri,
            requester=request.requester,
            alert_key=request.alert_key,
        )

    raise SupervisorRoutingError(
        f"No handoff builder is registered for specialist={route.specialist_name}."
    )
