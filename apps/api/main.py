####
## FastAPI Backend for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import argparse
import hmac
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Add repo root before importing project packages when the API is launched by module path.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.graph import TriageRuntimeConfig, run_triage
from agent.llm.copilot import (
    MAX_CONTEXT_AUDIT,
    MAX_CONTEXT_EVIDENCE,
    build_operator_answer,
)
from agent.mcp.server import bounded_text, ensure_report_s3_uri_allowed
from agent.tools.alerts import list_alerts, load_alert
from agent.tools.approval_queue import (
    ApprovalRequest,
    ApprovalRequestCreate,
    create_approval_request,
    decide_approval_request,
    get_approval_request,
    list_approval_requests,
)
from agent.tools.audit_log import write_agent_audit_event
from agent.tools.dbt_lineage import fetch_dbt_blast_radius, fetch_dbt_lineage
from agent.tools.dq_history import fetch_dq_history
from agent.tools.life_history import LifeEvaluationHistoryResult, list_life_evaluation_history
from agent.tools.metadata_catalog import get_metadata_asset, search_metadata_assets
from agent.tools.pipeline_runs import fetch_pipeline_runs
from apps.api.schemas import (
    ApprovalDecisionBody,
    ApprovalRequestCreateBody,
    ApprovalRequestListResponse,
    ApprovalRequestResponse,
    CopilotAnswerRequest,
    CopilotAnswerResponse,
    DbtBlastRadiusResponse,
    HealthResponse,
    MessageResponse,
    MetadataAssetListResponse,
    MetadataAssetResponse,
    TriageRunRequest,
    TriageRunResponse,
)
from apps.common.llm_observability import enrich_audit_rows
from agent.tools.clickhouse_sql import rows_to_dicts
from pipelines.common.clickhouse import build_clickhouse_client, quote_sql_literal
from pipelines.common.logging import logger
from pipelines.seeding.helpers import parse_date
from pipelines.seeding.upload_to_s3 import build_s3_client


# --- Defining Constants
API_TITLE                   = "Agentic Data Quality Triage API"
API_VERSION                 = "0.7.0"
AUDIT_LOG_TABLE             = "dq.agent_audit_log"
COPILOT_REPORT_MAX_BYTES    = 200_000
APPROVAL_TOKEN_ENV_NAME     = "CONTROL_PLANE_APPROVAL_TOKEN"


# --- Creating FastAPI App
app = FastAPI(
    title=API_TITLE,
    version=API_VERSION,
    description=(
        "Backend-for-frontend API for local data reliability demos. "
        "The API reuses existing guarded agent tools and does not execute remediation automatically."
    ),
)


# --- Defining Approval Response Helpers
def approval_response(
    approval: ApprovalRequest,
    created_new: bool | None = None,
    state_changed: bool | None = None,
) -> ApprovalRequestResponse:
    """
    Convert an internal approval model into the public API response contract.

    Args:
        approval: Latest durable approval request state.
        created_new: Optional idempotent-create indicator.
        state_changed: Optional lifecycle-transition indicator.

    Returns:
        Serialized approval response without internal database objects.
    """
    payload                  = approval.model_dump(mode="json")
    payload["created_new"]   = created_new
    payload["state_changed"] = state_changed

    return ApprovalRequestResponse.model_validate(payload)


def require_approval_authorization(
    x_control_plane_token: str | None = Header(default=None),
) -> None:
    """
    Require a fail-closed shared token for approval mutation endpoints.

    Args:
        x_control_plane_token: Caller token supplied through the HTTP header.

    Returns:
        None when the configured token matches in constant time.

    Raises:
        HTTPException: If the server token is missing or caller is unauthorized.
    """
    expected_token = os.getenv(APPROVAL_TOKEN_ENV_NAME, "").strip()
    supplied_token = (x_control_plane_token or "").strip()

    if not expected_token:
        logger.error("Approval API is disabled because %s is not configured", APPROVAL_TOKEN_ENV_NAME)
        raise HTTPException(status_code=503, detail="Approval mutations are not configured.")

    if not supplied_token or not hmac.compare_digest(supplied_token, expected_token):
        logger.warning("Rejected unauthorized approval mutation request")
        raise HTTPException(status_code=401, detail="Approval authorization failed.")


# --- Defining Helper Functions
def build_audit_log_sql(alert_key: str, limit: int) -> str:
    """
    Build a bounded audit log query for one alert key.

    Args:
        alert_key: Stable alert key.
        limit: Maximum audit rows to return.

    Returns:
        ClickHouse SQL query string.
    """
    safe_limit = max(1, min(limit, 100))

    return f"""
        SELECT
            audit_id,
            ts,
            alert_id,
            agent_run_id,
            actor,
            action,
            tool_name,
            status,
            duration_ms,
            row_count,
            report_s3_uri,
            error_message,
            input_json,
            output_json
        FROM {AUDIT_LOG_TABLE}
        WHERE alert_key = {quote_sql_literal(alert_key)}
        ORDER BY ts DESC
        LIMIT {safe_limit}
    """


def fetch_audit_log_rows(alert_key: str, limit: int = 50) -> dict[str, Any]:
    """
    Fetch recent agent audit events for an alert.

    Args:
        alert_key: Stable alert key.
        limit: Maximum rows to return.

    Returns:
        Dictionary containing audit rows and query metadata.
    """
    client     = build_clickhouse_client()
    sql        = build_audit_log_sql(alert_key=alert_key, limit=limit)
    started_at = time.monotonic()

    logger.info("API fetching audit logs | alert_key=%s limit=%s", alert_key, limit)

    result      = client.query(sql)
    columns     = list(result.column_names or [])
    raw_rows    = rows_to_dicts(columns=columns, rows=result.result_rows)
    rows, routes = enrich_audit_rows(rows=raw_rows)
    duration_ms = int((time.monotonic() - started_at) * 1000)

    return {
        "status": "success",
        "alert_key": alert_key,
        "rows": rows,
        "row_count": len(rows),
        "llm_routes": [route.to_public_dict() for route in routes],
        "latest_llm_route": routes[0].to_public_dict() if routes else None,
        "duration_ms": duration_ms,
        "sql": sql,
    }


def read_report_artifact(s3_uri: str, max_bytes: int) -> dict[str, Any]:
    """
    Read a bounded report artifact from approved local S3 buckets.

    Args:
        s3_uri: S3 URI for a Markdown or JSON report artifact.
        max_bytes: Maximum bytes to return.

    Returns:
        Dictionary containing bounded artifact text and metadata.
    """
    bucket, key       = ensure_report_s3_uri_allowed(s3_uri)
    client            = build_s3_client()
    response          = client.get_object(Bucket=bucket, Key=key)
    payload           = response["Body"].read()
    text, is_truncated = bounded_text(payload=payload, max_bytes=max_bytes)

    logger.info(
        "API read report artifact | uri=%s bytes=%d truncated=%s",
        s3_uri,
        len(payload),
        is_truncated,
    )

    return {
        "status": "success",
        "s3_uri": s3_uri,
        "bucket": bucket,
        "key": key,
        "bytes_read": len(payload),
        "returned_bytes": len(text.encode("utf-8")),
        "truncated": is_truncated,
        "text": text,
    }


def normalize_report_json_s3_uri(s3_uri: str) -> str:
    """
    Normalize and validate one approved triage report JSON URI.

    Args:
        s3_uri: Candidate Markdown or JSON report artifact URI.

    Returns:
        Approved JSON report S3 URI.

    Raises:
        ValueError: If the URI is not an approved report.json artifact.
    """
    normalized_uri = s3_uri.strip()

    if normalized_uri.endswith("/report.md"):
        normalized_uri = f"{normalized_uri[:-len('/report.md')]}/report.json"

    if not normalized_uri.endswith("/report.json"):
        raise ValueError("Copilot report context must reference a report.json artifact.")

    ensure_report_s3_uri_allowed(normalized_uri)

    return normalized_uri


def resolve_copilot_report_json_uri(
    explicit_uri: str | None,
    alert: Any,
    audit_rows: list[dict[str, Any]],
) -> str | None:
    """
    Resolve the best approved JSON report artifact for one alert.

    Args:
        explicit_uri: Optional report URI supplied by a trusted internal client.
        alert: Source-of-truth alert model.
        audit_rows: Recent audit events for the same alert.

    Returns:
        Approved report.json URI, otherwise None when no report exists.

    Raises:
        ValueError: If an explicit URI is invalid.
    """
    candidates: list[tuple[str, bool]] = []

    if explicit_uri:
        candidates.append((explicit_uri, True))

    if getattr(alert, "report_s3_uri", ""):
        candidates.append((str(alert.report_s3_uri), False))

    candidates.extend(
        (str(row["report_s3_uri"]), False)
        for row in audit_rows
        if row.get("report_s3_uri")
    )

    for candidate, is_explicit in candidates:
        try:
            return normalize_report_json_s3_uri(candidate)

        except ValueError:
            if is_explicit:
                raise

    return None


def load_copilot_report_context(
    report_json_s3_uri: str,
    expected_alert_key: str,
) -> dict[str, Any]:
    """
    Load bounded Copilot context from one persisted triage report artifact.

    Args:
        report_json_s3_uri: Approved report.json S3 URI.
        expected_alert_key: Alert key that the report must describe.

    Returns:
        Report context, evidence rows, report id, and approval metadata.

    Raises:
        ValueError: If the artifact is truncated, malformed, or belongs to another alert.
    """
    artifact = read_report_artifact(
        s3_uri=report_json_s3_uri,
        max_bytes=COPILOT_REPORT_MAX_BYTES,
    )

    if artifact["truncated"]:
        raise ValueError("Triage report JSON exceeds the Copilot context size limit.")

    try:
        payload = json.loads(artifact["text"])

    except json.JSONDecodeError as exc:
        raise ValueError("Triage report artifact is not valid JSON.") from exc

    if not isinstance(payload, dict):
        raise ValueError("Triage report artifact must contain a JSON object.")

    alert_payload = payload.get("alert") or {}

    if str(alert_payload.get("alert_key") or "") != expected_alert_key:
        raise ValueError("Triage report alert_key does not match the requested alert.")

    top_hypothesis = payload.get("top_hypothesis") or {}
    actions        = payload.get("recommended_actions") or []
    evidence       = payload.get("evidence") or []

    recommended_action = str(top_hypothesis.get("recommended_action") or "")

    if not recommended_action and actions:
        recommended_action = str(actions[0])

    report_context = {
        "summary": payload.get("summary", ""),
        "impact": payload.get("impact", ""),
        "top_hypothesis": top_hypothesis.get("title", ""),
        "confidence": payload.get("confidence", 0.0),
        "recommended_action": recommended_action,
        "approval_required": bool(payload.get("approval_gated_actions")),
        "report_id": payload.get("report_id", ""),
    }
    evidence_rows = [row for row in evidence if isinstance(row, dict)]

    logger.info(
        "Loaded Copilot report context | alert_key=%s report_id=%s evidence=%d",
        expected_alert_key,
        report_context["report_id"],
        len(evidence_rows),
    )

    return {
        "report_context": report_context,
        "evidence_rows": evidence_rows,
        "report_id": str(report_context["report_id"] or ""),
        "approval_required": bool(report_context["approval_required"]),
    }


def write_copilot_api_audit_event(
    agent_run_id: UUID,
    alert: Any,
    request: CopilotAnswerRequest,
    response: CopilotAnswerResponse,
    report_json_s3_uri: str | None,
) -> None:
    """
    Persist one bounded API Copilot interaction to the agent audit log.

    Args:
        agent_run_id: Correlation UUID shared with the LLM route.
        alert: Source-of-truth alert model.
        request: Validated Copilot request.
        response: Copilot response returned to the caller.
        report_json_s3_uri: Resolved report artifact used for context.

    Returns:
        None.
    """
    client = build_clickhouse_client()

    # Store metadata rather than full question/answer text to reduce audit exposure.
    write_agent_audit_event(
        client=client,
        action="copilot_answer",
        status="success",
        agent_run_id=agent_run_id,
        alert_id=alert.alert_id,
        alert_key=alert.alert_key,
        actor="api_client",
        tool_name="llm_copilot",
        input_payload={
            "question_length": len(request.question),
            "report_json_s3_uri": report_json_s3_uri or "",
            "audit_limit": request.audit_limit,
        },
        output_payload={
            "answer_length": len(response.answer),
            "context_source": response.context_source,
            "report_id": response.report_id,
            "evidence_count": response.evidence_count,
            "audit_count": response.audit_count,
            "approval_required": response.approval_required,
        },
        row_count=1,
        report_s3_uri=report_json_s3_uri or "",
    )


def compact_triage_response(report: Any) -> TriageRunResponse:
    """
    Convert a TriageReport into an API response model.

    Args:
        report: TriageReport returned by run_triage.

    Returns:
        TriageRunResponse model.
    """
    return TriageRunResponse(
        status="success",
        agent_run_id=str(report.agent_run_id),
        alert_key=report.alert.alert_key,
        alert_display_id=report.alert.alert_display_id,
        severity=report.alert.severity,
        confidence=report.confidence,
        top_hypothesis=report.top_hypothesis.title if report.top_hypothesis else None,
        markdown_report_s3_uri=report.markdown_report_s3_uri,
        json_report_s3_uri=report.json_report_s3_uri,
        approval_gated_actions=[action.model_dump(mode="json") for action in report.approval_gated_actions],
    )


def raise_api_error(exc: Exception) -> None:
    """
    Convert internal exceptions into HTTP-friendly errors.

    Args:
        exc: Exception raised by an underlying tool.

    Returns:
        None.

    Raises:
        HTTPException: Always raised with an appropriate HTTP status code.
    """
    if isinstance(exc, LookupError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    logger.exception("API request failed")
    raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc


# --- Defining API Routes
@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """
    Return API health metadata.

    Returns:
        HealthResponse with service status.
    """
    return HealthResponse(version=API_VERSION)


@app.get("/api/v1/alerts")
def api_list_alerts(
    status: str = Query(default="open", description="Alert lifecycle status."),
    dt: str | None = Query(default=None, description="Optional business date in YYYY-MM-DD format."),
    limit: int = Query(default=50, ge=1, le=100, description="Maximum alerts to return."),
) -> dict[str, Any]:
    """
    List DQ alerts for UI clients.

    Args:
        status: Alert lifecycle status.
        dt: Optional business date.
        limit: Maximum alerts to return.

    Returns:
        Alert lookup payload from the existing alert tool.
    """
    try:
        return list_alerts(status=status, dt=dt, limit=limit)

    except Exception as exc:
        raise_api_error(exc)


@app.get("/api/v1/alerts/detail")
def api_get_alert(
    alert_id: str | None = Query(default=None, description="Optional alert UUID."),
    alert_key: str | None = Query(default=None, description="Optional stable alert key."),
) -> dict[str, Any]:
    """
    Load one alert by id or key.

    Args:
        alert_id: Optional alert UUID.
        alert_key: Optional stable alert key.

    Returns:
        Alert model serialized as JSON.
    """
    try:
        alert = load_alert(alert_id=alert_id, alert_key=alert_key)

        return alert.model_dump(mode="json")

    except Exception as exc:
        raise_api_error(exc)


@app.get("/api/v1/audit/logs")
def api_get_audit_logs(
    alert_key: str = Query(description="Stable alert key."),
    limit: int = Query(default=50, ge=1, le=100, description="Maximum audit rows to return."),
) -> dict[str, Any]:
    """
    Return recent agent audit events for one alert.

    Args:
        alert_key: Stable alert key.
        limit: Maximum rows to return.

    Returns:
        Audit log result payload.
    """
    try:
        return fetch_audit_log_rows(alert_key=alert_key, limit=limit)

    except Exception as exc:
        raise_api_error(exc)


@app.get("/api/v1/evaluations/life", response_model=LifeEvaluationHistoryResult)
def api_list_life_evaluations(
    eval_status: str | None = Query(
        default=None,
        pattern="^(pass|review|fail)$",
        description="Optional LIFE evaluation status.",
    ),
    scenario_id: str | None = Query(
        default=None,
        min_length=1,
        max_length=64,
        description="Optional incident scenario identifier.",
    ),
    lookback_days: int = Query(
        default=30,
        ge=1,
        le=365,
        description="Mandatory recent audit window in days.",
    ),
    limit: int = Query(default=25, ge=1, le=100, description="Maximum evaluations to return."),
) -> LifeEvaluationHistoryResult:
    """
    Return recent sanitized LIFE evaluation summaries.

    Args:
        eval_status: Optional pass, review, or fail filter.
        scenario_id: Optional incident scenario identifier.
        lookback_days: Mandatory recent audit timestamp window in days.
        limit: Maximum evaluation summaries to return.

    Returns:
        Bounded LIFE evaluation history without raw audit payloads.
    """
    try:
        return list_life_evaluation_history(
            client=build_clickhouse_client(),
            eval_status=eval_status,
            scenario_id=scenario_id,
            lookback_days=lookback_days,
            limit=limit,
        )

    except Exception as exc:
        raise_api_error(exc)


@app.get(
    "/api/v1/metadata/assets",
    response_model=MetadataAssetListResponse,
)
def api_search_metadata_assets(
    query: str | None = Query(
        default=None,
        max_length=120,
        description="Optional text matched against asset names, descriptions, owners, grain, and tags.",
    ),
    domain: str | None = Query(default=None, max_length=80, description="Optional normalized data domain."),
    data_layer: str | None = Query(default=None, description="Optional raw, staging, or mart layer."),
    certification_status: str | None = Query(default=None, description="Optional certification state."),
    lifecycle_status: str | None = Query(default=None, description="Optional active or deprecated state."),
    limit: int = Query(default=25, ge=1, le=100, description="Maximum metadata assets returned."),
) -> MetadataAssetListResponse:
    """
    Search trusted warehouse assets through the audited metadata catalog tool.

    Args:
        query: Optional bounded free-text search.
        domain: Optional data domain filter.
        data_layer: Optional warehouse layer filter.
        certification_status: Optional certification filter.
        lifecycle_status: Optional lifecycle filter.
        limit: Maximum assets returned.

    Returns:
        Typed metadata discovery response without internal hashes or config paths.
    """
    try:
        payload = search_metadata_assets(
            query=query,
            domain=domain,
            data_layer=data_layer,
            certification_status=certification_status,
            lifecycle_status=lifecycle_status,
            limit=limit,
        )

        return MetadataAssetListResponse.model_validate(payload)

    except Exception as exc:
        raise_api_error(exc)


@app.get(
    "/api/v1/metadata/assets/{qualified_name}",
    response_model=MetadataAssetResponse,
)
def api_get_metadata_asset(qualified_name: str) -> MetadataAssetResponse:
    """
    Return exact ownership, grain, SLA, sensitivity, and certification context.

    Args:
        qualified_name: Fully qualified database.table asset identity.

    Returns:
        Typed public metadata asset.
    """
    try:
        return MetadataAssetResponse.model_validate(
            get_metadata_asset(qualified_name=qualified_name)
        )

    except Exception as exc:
        raise_api_error(exc)


@app.get("/api/v1/lineage/dbt")
def api_get_dbt_lineage(
    table_name: str = Query(description="Fully qualified table name, for example dq.fct_orders_daily."),
    manifest_path: str | None = Query(default=None, description="Optional local manifest path."),
    manifest_s3_uri: str | None = Query(default=None, description="Optional S3 URI for manifest.json."),
) -> dict[str, Any]:
    """
    Return dbt lineage context for one table.

    Args:
        table_name: Fully qualified table name.
        manifest_path: Optional local dbt manifest path.
        manifest_s3_uri: Optional S3 manifest URI.

    Returns:
        dbt lineage summary payload.
    """
    try:
        return fetch_dbt_lineage(
            table_name=table_name,
            manifest_path=manifest_path,
            manifest_s3_uri=manifest_s3_uri,
        )

    except Exception as exc:
        raise_api_error(exc)


@app.get(
    "/api/v1/lineage/dbt/blast-radius",
    response_model=DbtBlastRadiusResponse,
)
def api_get_dbt_blast_radius(
    table_name: str = Query(
        min_length=1,
        max_length=255,
        description="Fully qualified table name, for example dq.raw_orders.",
    ),
    manifest_path: str | None = Query(default=None, description="Optional local manifest path."),
    manifest_s3_uri: str | None = Query(default=None, description="Optional S3 URI for manifest.json."),
    max_depth: int = Query(default=5, ge=1, le=10, description="Maximum downstream lineage depth."),
    max_nodes: int = Query(default=100, ge=1, le=250, description="Maximum downstream nodes returned."),
) -> DbtBlastRadiusResponse:
    """
    Return bounded transitive downstream dbt impact for one table.

    Args:
        table_name: Fully qualified warehouse table name.
        manifest_path: Optional local dbt manifest path.
        manifest_s3_uri: Optional S3 manifest URI.
        max_depth: Maximum child-map depth below the selected table.
        max_nodes: Maximum downstream nodes, excluding the root node.

    Returns:
        Typed, audited blast-radius response without raw or compiled SQL.
    """
    try:
        payload = fetch_dbt_blast_radius(
            table_name=table_name,
            manifest_path=manifest_path,
            manifest_s3_uri=manifest_s3_uri,
            max_depth=max_depth,
            max_nodes=max_nodes,
        )

        return DbtBlastRadiusResponse.model_validate(payload)

    except Exception as exc:
        raise_api_error(exc)


@app.get("/api/v1/evidence/dq-history")
def api_get_dq_history(
    table_name: str = Query(description="Fully qualified table name."),
    dt: str = Query(description="Business date in YYYY-MM-DD format."),
    check_name: str | None = Query(default=None, description="Optional DQ check name."),
    lookback_days: int = Query(default=14, ge=0, le=90, description="Lookback window in days."),
    limit: int = Query(default=100, ge=1, le=500, description="Maximum rows to return."),
) -> dict[str, Any]:
    """
    Return DQ history evidence for one table/check/date window.

    Args:
        table_name: Fully qualified table name.
        dt: Business date in YYYY-MM-DD format.
        check_name: Optional DQ check name.
        lookback_days: Historical lookback window.
        limit: Maximum rows to return.

    Returns:
        DQ history payload from the existing evidence tool.
    """
    try:
        return fetch_dq_history(
            table_name=table_name,
            dt=parse_date(dt),
            check_name=check_name,
            lookback_days=lookback_days,
            limit=limit,
        )

    except Exception as exc:
        raise_api_error(exc)


@app.get("/api/v1/evidence/pipeline-runs")
def api_get_pipeline_runs(
    dt: str = Query(description="Business date in YYYY-MM-DD format."),
    lookback_days: int = Query(default=7, ge=0, le=90, description="Lookback window in days."),
    job_name: str | None = Query(default=None, description="Optional job name filter."),
    limit: int = Query(default=100, ge=1, le=500, description="Maximum rows to return."),
) -> dict[str, Any]:
    """
    Return pipeline run evidence around one business date.

    Args:
        dt: Business date in YYYY-MM-DD format.
        lookback_days: Historical lookback window.
        job_name: Optional job name filter.
        limit: Maximum rows to return.

    Returns:
        Pipeline run evidence payload.
    """
    try:
        return fetch_pipeline_runs(
            dt=parse_date(dt),
            lookback_days=lookback_days,
            job_name=job_name,
            limit=limit,
        )

    except Exception as exc:
        raise_api_error(exc)


@app.get("/api/v1/reports/read")
def api_read_report(
    s3_uri: str = Query(description="Report artifact S3 URI."),
    max_bytes: int = Query(default=100_000, ge=1, le=200_000, description="Maximum bytes to return."),
) -> dict[str, Any]:
    """
    Return a bounded Markdown/JSON triage report artifact.

    Args:
        s3_uri: Report artifact S3 URI.
        max_bytes: Maximum bytes to return.

    Returns:
        Bounded report artifact payload.
    """
    try:
        return read_report_artifact(s3_uri=s3_uri, max_bytes=max_bytes)

    except Exception as exc:
        raise_api_error(exc)


@app.post("/api/v1/copilot/answer", response_model=CopilotAnswerResponse)
def api_answer_copilot(request: CopilotAnswerRequest) -> CopilotAnswerResponse:
    """
    Answer one bounded operator question using source-of-truth platform context.

    Args:
        request: Validated Copilot question and optional report artifact URI.

    Returns:
        Evidence-aware answer with context and audit metadata.
    """
    agent_run_id = uuid4()

    try:
        alert         = load_alert(alert_key=request.alert_key)
        audit_payload = fetch_audit_log_rows(
            alert_key=alert.alert_key,
            limit=request.audit_limit,
        )
        audit_rows = audit_payload["rows"]
        report_uri = resolve_copilot_report_json_uri(
            explicit_uri=request.report_json_s3_uri,
            alert=alert,
            audit_rows=audit_rows,
        )
        report_data = {
            "report_context": {},
            "evidence_rows": [],
            "report_id": "",
            "approval_required": False,
        }

        if report_uri:
            report_data = load_copilot_report_context(
                report_json_s3_uri=report_uri,
                expected_alert_key=alert.alert_key,
            )

        answer = build_operator_answer(
            question=request.question,
            alert=alert,
            report_context=report_data["report_context"],
            evidence_rows=report_data["evidence_rows"],
            audit_rows=audit_rows,
            agent_run_id=agent_run_id,
        )
        context_source = "alert_report_audit" if report_uri else "alert_audit"
        response       = CopilotAnswerResponse(
            agent_run_id=str(agent_run_id),
            alert_key=alert.alert_key,
            alert_display_id=alert.alert_display_id,
            answer=answer,
            context_source=context_source,
            report_id=report_data["report_id"],
            evidence_count=min(len(report_data["evidence_rows"]), MAX_CONTEXT_EVIDENCE),
            audit_count=min(len(audit_rows), MAX_CONTEXT_AUDIT),
            approval_required=report_data["approval_required"],
        )

        write_copilot_api_audit_event(
            agent_run_id=agent_run_id,
            alert=alert,
            request=request,
            response=response,
            report_json_s3_uri=report_uri,
        )

        logger.info(
            "API Copilot answer completed | agent_run_id=%s alert_ref=%s source=%s evidence=%d audit=%d",
            agent_run_id,
            alert.alert_display_id,
            context_source,
            response.evidence_count,
            response.audit_count,
        )

        return response

    except Exception as exc:
        raise_api_error(exc)


@app.post("/api/v1/triage/run", response_model=TriageRunResponse)
def api_run_triage(request: TriageRunRequest) -> TriageRunResponse:
    """
    Run one agentic triage workflow for a selected alert.

    Args:
        request: Triage run request body.

    Returns:
        Compact triage run response with report artifact URIs.
    """
    try:
        config = TriageRuntimeConfig(
            manifest_s3_uri=request.manifest_s3_uri,
            artifacts_bucket=request.artifacts_bucket,
            artifacts_prefix=request.artifacts_prefix,
        )
        report = run_triage(
            alert_id=request.alert_id,
            alert_key=request.alert_key,
            confidence_threshold=request.confidence_threshold,
            max_evidence_iterations=request.max_evidence_iterations,
            config=config,
        )

        return compact_triage_response(report)

    except Exception as exc:
        raise_api_error(exc)


@app.post(
    "/api/v1/approvals/requests",
    response_model=ApprovalRequestResponse,
    dependencies=[Depends(require_approval_authorization)],
)
def api_create_approval_request(request: ApprovalRequestCreateBody) -> ApprovalRequestResponse:
    """
    Create or reuse one idempotent backfill approval request.

    Args:
        request: Bounded approval proposal from UI, Discord, MCP, or another client.

    Returns:
        Latest durable approval state and whether a new record was created.
    """
    try:
        approval, created_new = create_approval_request(
            ApprovalRequestCreate.model_validate(request.model_dump())
        )

        return approval_response(approval, created_new=created_new)

    except Exception as exc:
        raise_api_error(exc)


@app.get("/api/v1/approvals/requests", response_model=ApprovalRequestListResponse)
def api_list_approval_requests(
    status: str | None = Query(
        default=None,
        pattern="^(pending|approved|rejected)$",
        description="Optional latest-state approval status.",
    ),
    limit: int = Query(default=50, ge=1, le=100, description="Maximum requests to return."),
) -> ApprovalRequestListResponse:
    """
    List bounded latest-state approval requests.

    Args:
        status: Optional pending, approved, or rejected lifecycle filter.
        limit: Maximum latest-state requests to return.

    Returns:
        Bounded approval queue response.
    """
    try:
        approvals = list_approval_requests(client=build_clickhouse_client(), status=status, limit=limit)
        rows      = [approval_response(approval) for approval in approvals]

        return ApprovalRequestListResponse(row_count=len(rows), rows=rows)

    except Exception as exc:
        raise_api_error(exc)


@app.get("/api/v1/approvals/requests/{request_id}", response_model=ApprovalRequestResponse)
def api_get_approval_request(request_id: str) -> ApprovalRequestResponse:
    """
    Return the latest state for one approval request.

    Args:
        request_id: Human-facing approval request reference.

    Returns:
        Latest approval request state.
    """
    try:
        approval = get_approval_request(build_clickhouse_client(), request_id)

        if approval is None:
            raise LookupError(f"Approval request was not found: {request_id}")

        return approval_response(approval)

    except Exception as exc:
        raise_api_error(exc)


@app.post(
    "/api/v1/approvals/requests/{request_id}/decision",
    response_model=ApprovalRequestResponse,
    dependencies=[Depends(require_approval_authorization)],
)
def api_decide_approval_request(
    request_id: str,
    request: ApprovalDecisionBody,
) -> ApprovalRequestResponse:
    """
    Apply one idempotent terminal human decision to an approval request.

    Args:
        request_id: Human-facing approval request reference.
        request: Explicit approve or reject decision metadata.

    Returns:
        Latest durable approval state and whether state changed.
    """
    try:
        approval, state_changed = decide_approval_request(
            request_id=request_id,
            decision=request.decision,
            decided_by=request.decided_by,
            comment=request.comment,
        )

        return approval_response(approval, state_changed=state_changed)

    except Exception as exc:
        raise_api_error(exc)


# --- Defining CLI Helpers
def build_parser() -> argparse.ArgumentParser:
    """
    Build a small CLI parser for API smoke inspection.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(description="Inspect the FastAPI backend-for-frontend app.")

    parser.add_argument("--smoke", action="store_true", help="Print app metadata and route count.")

    return parser


def main() -> None:
    """
    Run lightweight API app inspection from the command line.

    Returns:
        None.
    """
    parser = build_parser()
    args   = parser.parse_args()

    if args.smoke:
        payload = {
            "status": "success",
            "title": app.title,
            "version": app.version,
            "route_count": len(app.routes),
            "routes": sorted(route.path for route in app.routes),
        }
        print(json.dumps(payload, indent=2))

        return

    parser.print_help()


# --- Running CLI Entrypoint
if __name__ == "__main__":
    main()

