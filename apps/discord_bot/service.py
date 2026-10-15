####
## Discord Bot Service Layer for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Shared data and control-plane operations used by the Discord adapter."""

# --- Importing Libraries
from __future__ import annotations

import os
import re
from typing import Any
from uuid import uuid4

from agent.graph import DEFAULT_CONFIDENCE_TARGET, DEFAULT_MAX_EVIDENCE_LOOP, TriageRuntimeConfig, run_triage
from agent.llm.copilot import build_operator_answer, build_triage_copilot_note
from agent.llm.conversation import bounded_conversation_context
from agent.tools.audit_log import build_llm_route_audit_payload, write_agent_audit_event, write_llm_route_audit_event
from pipelines.common.clickhouse import build_clickhouse_client
from agent.state import TriageReport
from agent.tools.alerts import list_alerts, load_alert
from agent.tools.daily_summary import fetch_daily_quality_summary
from agent.tools.quality_summary import (
    fetch_database_quality_summary,
    fetch_table_quality_summary,
    fetch_weekly_quality_summary,
)
from apps.common.control_plane import (
    ControlPlaneClient,
    ControlPlaneClientError,
    ControlPlaneResponseError,
    ControlPlaneTransportError,
)
from pipelines.common.logging import logger
from apps.common.i18n import current_language, normalize_language


# --- Defining Constants
DEFAULT_ALERT_STATUS    = "open"
DEFAULT_ALERT_LIMIT     = 10
DEFAULT_MANIFEST_S3_URI = "s3://dq-artifacts/dbt-artifacts/orders/latest/manifest.json"


# --- Defining Configuration Helpers
def build_control_plane_client(
    api_base_url: str | None = None,
) -> ControlPlaneClient | None:
    """
    Build the optional shared API client used by Discord commands.

    Args:
        api_base_url: Optional explicit API URL. None reads CONTROL_PLANE_API_URL.

    Returns:
        Configured ControlPlaneClient when an API URL exists, otherwise None.
    """
    resolved_url = (
        os.getenv("CONTROL_PLANE_API_URL", "")
        if api_base_url is None
        else api_base_url
    ).strip().rstrip("/")

    if not resolved_url:
        return None

    timeout_seconds = float(os.getenv("COPILOT_API_TIMEOUT_SECONDS", "15"))

    return ControlPlaneClient(
        base_url=resolved_url,
        timeout_seconds=timeout_seconds,
    )


def probe_control_plane_health(
    api_base_url: str | None = None,
) -> dict[str, Any]:
    """
    Probe FastAPI readiness without exposing credentials or raw URLs.

    Args:
        api_base_url: Optional API URL override.

    Returns:
        Secret-safe readiness payload.
    """
    api_client = build_control_plane_client(api_base_url=api_base_url)

    if api_client is None:
        return {"status": "not_configured"}

    try:
        payload = api_client.health()

    except ControlPlaneClientError as exc:
        logger.warning(
            "Discord control-plane readiness probe failed | error_type=%s",
            type(exc).__name__,
        )

        return {
            "status": "unavailable",
            "error_type": type(exc).__name__,
        }

    return {
        "status": "ready" if payload.get("status") == "ok" else "degraded",
        "service": str(payload.get("service") or ""),
        "version": str(payload.get("version") or ""),
    }


# --- Defining Data Helpers
def fetch_discord_daily_summary(
    dt: str,
    api_base_url: str | None = None,
) -> tuple[dict[str, Any], str]:
    """
    Fetch one daily summary through FastAPI with transport-only local fallback.

    Args:
        dt: Business date in YYYY-MM-DD format.
        api_base_url: Optional API URL override.

    Returns:
        Daily summary payload and transport label.

    Raises:
        ControlPlaneResponseError: If FastAPI rejects or violates the response contract.
    """
    api_client = build_control_plane_client(api_base_url=api_base_url)

    if api_client:
        try:
            return api_client.get_daily_summary(dt=dt), "api"

        except ControlPlaneTransportError as exc:
            logger.warning(
                "Discord daily summary API unavailable; using local tool | error_type=%s",
                type(exc).__name__,
            )

        except ControlPlaneResponseError:
            raise

    payload = fetch_daily_quality_summary(dt=dt)

    logger.info(
        "Fetched Discord daily summary through local fallback | dt=%s checks=%d alerts=%d",
        dt,
        payload["total_checks"],
        payload["total_open_alerts"],
    )

    return payload, "local"


def fetch_discord_weekly_summary(
    end_date: str,
    api_base_url: str | None = None,
) -> tuple[dict[str, Any], str]:
    """Fetch one seven-day quality summary through the shared API boundary."""
    api_client = build_control_plane_client(api_base_url=api_base_url)

    if api_client:
        try:
            return api_client.get_weekly_summary(end_date=end_date), "api"

        except ControlPlaneTransportError as exc:
            logger.warning(
                "Discord weekly summary API unavailable; using local tool | error_type=%s",
                type(exc).__name__,
            )

        except ControlPlaneResponseError:
            raise

    payload = fetch_weekly_quality_summary(end_date=end_date)
    payload.pop("sql", None)

    return payload, "local"


def fetch_discord_table_summary(
    table_name: str,
    start_date: str,
    end_date: str,
    api_base_url: str | None = None,
) -> tuple[dict[str, Any], str]:
    """Fetch one bounded per-table quality summary through the shared API."""
    api_client = build_control_plane_client(api_base_url=api_base_url)

    if api_client:
        try:
            return api_client.get_table_summary(
                table_name=table_name,
                start_date=start_date,
                end_date=end_date,
            ), "api"

        except ControlPlaneTransportError as exc:
            logger.warning(
                "Discord table summary API unavailable; using local tool | error_type=%s",
                type(exc).__name__,
            )

        except ControlPlaneResponseError:
            raise

    payload = fetch_table_quality_summary(
        table_name=table_name,
        start_date=start_date,
        end_date=end_date,
    )
    payload.pop("sql", None)

    return payload, "local"


def fetch_discord_database_summary(
    start_date: str,
    end_date: str,
    api_base_url: str | None = None,
) -> tuple[dict[str, Any], str]:
    """Fetch one bounded warehouse quality summary through the shared API."""
    api_client = build_control_plane_client(api_base_url=api_base_url)

    if api_client:
        try:
            return api_client.get_database_summary(
                start_date=start_date,
                end_date=end_date,
            ), "api"

        except ControlPlaneTransportError as exc:
            logger.warning(
                "Discord warehouse summary API unavailable; using local tool | error_type=%s",
                type(exc).__name__,
            )

        except ControlPlaneResponseError:
            raise

    payload = fetch_database_quality_summary(start_date=start_date, end_date=end_date)
    payload.pop("sql", None)

    return payload, "local"


def fetch_discord_alerts(
    status: str,
    dt: str | None,
    limit: int,
    api_base_url: str | None = None,
) -> tuple[dict[str, Any], str]:
    """
    Fetch alerts through FastAPI with transport-only local fallback.

    Args:
        status: Alert lifecycle filter.
        dt: Optional business date.
        limit: Maximum alert rows.
        api_base_url: Optional API URL override.

    Returns:
        Alert payload and transport label.

    Raises:
        ControlPlaneResponseError: If FastAPI responds with an invalid or rejected contract.
    """
    api_client = build_control_plane_client(api_base_url=api_base_url)

    if api_client:
        try:
            return api_client.list_alerts(
                status=status,
                dt=dt,
                limit=limit,
            ), "api"

        except ControlPlaneTransportError as exc:
            logger.warning(
                "Discord alert API unavailable; using local tool | error_type=%s",
                type(exc).__name__,
            )

        except ControlPlaneResponseError:
            raise

    return list_alerts(
        status=status,
        dt=dt,
        limit=limit,
    ), "local"


def run_discord_triage(
    alert_key: str,
    api_base_url: str | None = None,
) -> tuple[TriageReport, str]:
    """
    Run triage through FastAPI with transport-only local fallback.

    Args:
        alert_key: Alert Ref or stable system alert key.
        api_base_url: Optional API URL override.

    Returns:
        Completed TriageReport and transport label.

    Raises:
        ControlPlaneResponseError: If FastAPI rejects or violates the response contract.
    """
    api_client = build_control_plane_client(api_base_url=api_base_url)

    if api_client:
        try:
            report = api_client.run_triage_report(
                alert_key=alert_key,
                confidence_threshold=DEFAULT_CONFIDENCE_TARGET,
                max_evidence_iterations=DEFAULT_MAX_EVIDENCE_LOOP,
                manifest_s3_uri=DEFAULT_MANIFEST_S3_URI,
            )

            return report, "api"

        except ControlPlaneTransportError as exc:
            logger.warning(
                "Discord triage API outcome unavailable; no local replay | alert_key=%s error_type=%s",
                alert_key,
                type(exc).__name__,
            )
            raise

        except ControlPlaneResponseError:
            raise

    logger.info("Running Discord triage locally | alert_key=%s", alert_key)

    report = run_triage(
        alert_key=alert_key,
        confidence_threshold=DEFAULT_CONFIDENCE_TARGET,
        max_evidence_iterations=DEFAULT_MAX_EVIDENCE_LOOP,
        config=TriageRuntimeConfig(manifest_s3_uri=DEFAULT_MANIFEST_S3_URI),
    )

    return report, "local"


def build_discord_triage_note(
    report: TriageReport,
    api_base_url: str | None = None,
) -> tuple[str, str]:
    """
    Build a natural triage readout through FastAPI when available.

    Args:
        report: Completed triage report.
        api_base_url: Optional API URL override.

    Returns:
        Assistant note and transport label.
    """
    api_client = build_control_plane_client(api_base_url=api_base_url)
    question   = (
        "Explain what likely happened, summarize the strongest evidence, "
        "state confidence, and recommend the safest approval-gated next step."
    )

    if api_client:
        try:
            response = api_client.answer_copilot(
                question=question,
                alert_key=report.alert.alert_key,
                report_json_s3_uri=report.json_report_s3_uri,
                audit_limit=10,
            )

            return str(response["answer"]), "api"

        except ControlPlaneTransportError as exc:
            logger.warning(
                "Discord narrative API unavailable; using local narrative | alert_ref=%s error_type=%s",
                report.alert.alert_display_id,
                type(exc).__name__,
            )

        except ControlPlaneResponseError:
            raise

    return build_triage_copilot_note(report), "local"


def answer_discord_question(
    question: str,
    alert_key: str = "",
    api_base_url: str | None = None,
    conversation_context: list[dict[str, str]] | None = None,
    mention_mode: bool = False,
    language: str | None = None,
) -> dict[str, Any]:
    """
    Answer a Discord Copilot question with optional alert grounding.

    Args:
        question: Natural-language operator question.
        alert_key: Optional Alert Ref or stable system alert key.
        api_base_url: Optional API URL override.

    Returns:
        Answer, transport, correlation id, normalized alert key, and bounded
        incident-history count.
    """
    language = normalize_language(language) if language is not None else current_language()
    normalized_alert_key = alert_key.strip()
    api_client           = build_control_plane_client(api_base_url=api_base_url)
    turns = bounded_conversation_context(conversation_context)
    api_failed = False

    if api_client:
        try:
            if mention_mode:
                # The API may finish an external call after a client timeout.
                api_client.timeout_seconds = 60
            context_kwargs = {"conversation_context": turns} if conversation_context is not None else {}
            response = api_client.answer_copilot(
                question=question,
                language=language,
                alert_key=normalized_alert_key,
                audit_limit=10,
                **context_kwargs,
            )

            return {
                "answer": str(response["answer"]),
                "transport": "api",
                "agent_run_id": str(response.get("agent_run_id") or ""),
                "alert_key": str(response.get("alert_key") or normalized_alert_key),
                "llm_runtime": response.get("llm_runtime"),
                "alert_ref": str(response.get("alert_display_id") or ""),
                "incident_history_count": int(
                    response.get("incident_history_count") or 0
                ),
            }

        except ControlPlaneTransportError as exc:
            api_failed = True
            logger.warning(
                "Discord ask API unavailable; using local narrative | alert_key=%s error_type=%s",
                normalized_alert_key,
                type(exc).__name__,
            )

        except ControlPlaneResponseError:
            raise

    alert_context  = None
    platform_alerts: list[dict[str, Any]] = []

    if normalized_alert_key:
        logger.info(
            "Loading local alert context for Discord ask fallback | alert_key=%s",
            normalized_alert_key,
        )
        alert_context = load_alert(alert_key=normalized_alert_key)

    else:
        for status in ("open", "triaged"):
            payload = list_alerts(status=status, limit=5)
            platform_alerts.extend(list(payload.get("alerts") or []))

        platform_alerts = list(
            {
                str(row.get("alert_key") or row.get("alert_display_id") or index): row
                for index, row in enumerate(platform_alerts)
            }.values()
        )[:10]

    agent_run_id = uuid4()
    responses = []
    answer = build_operator_answer(
        question=question,
        language=language,
        alert=alert_context,
        platform_alerts=platform_alerts,
        conversation_context=turns,
        force_heuristic=api_failed,
        agent_run_id=agent_run_id,
        response_sink=responses,
    )
    runtime = build_llm_route_audit_payload(responses[-1]) if responses else None
    try:
        audit_client = build_clickhouse_client()
        try:
            if responses:
                write_llm_route_audit_event(
                    audit_client, responses[-1], agent_run_id=agent_run_id,
                    alert_id=getattr(alert_context, "alert_id", None),
                    alert_key=str(getattr(alert_context, "alert_key", "") or ""),
                )
            write_agent_audit_event(
                audit_client, action="copilot_answer", status="success", agent_run_id=agent_run_id,
                alert_key=str(getattr(alert_context, "alert_key", "") or ""), tool_name="llm_copilot",
                input_payload={"question_length": len(question), "conversation_turns": len(turns)},
                output_payload={"transport": "local", "llm_runtime": runtime},
            )
        finally:
            audit_client.close()
    except Exception as exc:
        logger.warning("Local Copilot audit unavailable | error_type=%s", type(exc).__name__)

    return {
        "answer": answer,
        "transport": "local",
        "agent_run_id": str(agent_run_id),
        "llm_runtime": runtime,
        "alert_ref": str(getattr(alert_context, "alert_display_id", "") or ""),
        "incident_history_count": 0,
        "alert_key": (
            alert_context.alert_key
            if alert_context is not None
            else normalized_alert_key
        ),
    }


def fetch_discord_latest_report(alert_key: str) -> TriageReport | None:
    """Read the authoritative artifact without rerunning an investigation or model."""
    alert = load_alert(alert_key=alert_key.strip())
    uri = _authoritative_incident_report_uri(alert)
    if not uri:
        return None
    api = build_control_plane_client()
    if api is None:
        raise ControlPlaneTransportError("Report reading requires the control-plane API.")
    report = TriageReport.model_validate(api.read_report_json(uri))
    if report.alert.alert_key != alert.alert_key:
        raise ControlPlaneResponseError("Report does not belong to the selected incident.")
    return report


def _authoritative_incident_report_uri(alert: Any) -> str:
    details = dict(alert.details or {})
    uri = str(details.get("json_report_s3_uri") or alert.report_s3_uri or "")
    if uri.endswith(".md"):
        uri = uri[:-3] + ".json"
    if uri:
        _incident_report_uri(uri)
    return uri


def _incident_report_uri(uri: str) -> tuple[str, str]:
    from urllib.parse import urlparse
    parsed = urlparse(uri)
    key = parsed.path.lstrip("/")
    if (parsed.scheme != "s3" or parsed.netloc != "dq-artifacts" or parsed.query or parsed.fragment
            or not key.startswith("agent-reports/") or not key.endswith((".md", ".json"))
            or ".." in key.split("/") or "\\" in key):
        raise ValueError("Only approved incident report artifacts can be attached.")
    return parsed.netloc, key


def fetch_discord_report_files(alert_key: str) -> list[dict[str, Any]]:
    """Return complete, bounded operator report copies; never fetch caller URLs."""
    report = fetch_discord_latest_report(alert_key)
    if report is None:
        return []
    authoritative_uri = _authoritative_incident_report_uri(load_alert(alert_key=alert_key.strip()))
    expected_markdown = authoritative_uri[:-5] + ".md" if authoritative_uri.endswith(".json") else ""
    if (not authoritative_uri or report.json_report_s3_uri != authoritative_uri
            or report.markdown_report_s3_uri not in {"", expected_markdown}):
        raise ControlPlaneResponseError("Report attachment locations do not match the selected authoritative report.")
    api = build_control_plane_client()
    if api is None:
        raise ControlPlaneTransportError("Report reading requires the control-plane API.")
    files = []
    for uri in (report.markdown_report_s3_uri, report.json_report_s3_uri):
        if not uri:
            continue
        _incident_report_uri(uri)
        artifact = api.read_report_artifact(uri, max_bytes=200_000)
        if artifact["truncated"]:
            raise ControlPlaneResponseError("Report exceeds the attachment limit; open Workspace for the full artifact.")
        text = str(artifact["text"])
        # Attachments are operator copies, never a way to expose credentials.
        if re.search(r"(?:sk-[A-Za-z0-9_-]{12,}|Authorization\s*[:=]|X-goog-api-key|[?&]X-Amz-Signature=)", text, re.I):
            raise ControlPlaneResponseError("Report attachment failed the credential safety check.")
        extension = "json" if uri.endswith(".json") else "md"
        filename = f"report-{report.report_id}.{extension}"
        files.append({"filename": filename, "content": text.encode("utf-8"), "media_type": artifact["media_type"], "s3_uri": uri})
    return files


def fetch_discord_debug_context(alert_key: str) -> dict[str, Any]:
    """Load sanitized technical correlation fields without invoking an LLM."""
    alert   = load_alert(alert_key=alert_key.strip())
    details = dict(alert.details or {})
    runtime = details.get("llm_runtime") if isinstance(details.get("llm_runtime"), dict) else {}
    report_id = str(details.get("report_id") or "")
    if not report_id:
        match = re.search(r"(?:^|/)report_id=(RPT-[A-Fa-f0-9]{8})(?:/|$)", str(alert.report_s3_uri or ""))
        report_id = match.group(1) if match else ""

    return {
        "alert_ref": alert.alert_display_id,
        "alert_key": alert.alert_key,
        "agent_run_id": str(details.get("agent_run_id") or ""),
        "report_id": report_id,
        "report_uri": alert.report_s3_uri,
        "provider": str(runtime.get("provider") or ""),
        "model": str(runtime.get("model") or ""),
        "transport": "local_read_only",
    }


def fetch_discord_evidence_context(alert_key: str) -> dict[str, Any]:
    """Load bounded deterministic incident facts for an ephemeral Evidence action."""
    alert   = load_alert(alert_key=alert_key.strip())
    details = dict(alert.details or {})
    summary = str(
        details.get("evidence_summary")
        or details.get("message")
        or (
            f"The `{alert.metric}` check observed `{alert.observed_value}` where "
            f"`{alert.expected_value}` was expected."
        )
    )

    resolution = details.get("resolution") if str(alert.status).lower() == "resolved" else None
    if not isinstance(resolution, dict):
        resolution = {}
    result = resolution.get("result") if isinstance(resolution.get("result"), dict) else resolution
    if str(alert.status).lower() == "resolved":
        summary = str(result.get("summary") or "Closure evidence is not available on this card. Open the history in Workspace.")

    return {
        "alert_ref": alert.alert_display_id,
        "alert_key": alert.alert_key,
        "status": str(alert.status),
        "severity": str(alert.severity),
        "table_name": alert.table_name,
        "affected_date": alert.dt.isoformat() if alert.dt else "",
        "metric": alert.metric,
        "observed_value": result.get("observed") if str(alert.status).lower() == "resolved" else alert.observed_value,
        "expected_value": result.get("expected") if str(alert.status).lower() == "resolved" else alert.expected_value,
        "summary": summary,
        "report_uri": alert.report_s3_uri,
        "resolution": resolution,
    }


# --- Defining Approval Helpers
def create_discord_backfill_approval_request(
    start_date: str,
    end_date: str,
    target_dag_id: str,
    reason: str,
    requested_by: str,
    alert_key: str = "",
) -> dict[str, Any]:
    """
    Create or reuse one durable backfill approval request through FastAPI.

    Args:
        start_date: Inclusive YYYY-MM-DD start date.
        end_date: Inclusive YYYY-MM-DD end date.
        target_dag_id: Allowlisted operational DAG.
        reason: Human-readable approval reason.
        requested_by: Discord operator identity.
        alert_key: Optional authoritative incident correlation stored with the request.

    Returns:
        Latest durable approval request state.

    Raises:
        RuntimeError: If the control-plane API is not configured.
        ControlPlaneClientError: If the API rejects or cannot persist the request.
    """
    api_client = build_control_plane_client()

    if api_client is None:
        raise RuntimeError("CONTROL_PLANE_API_URL is required for durable approval requests.")

    logger.info(
        "Creating Discord approval request | target=%s dates=%s..%s requested_by=%s",
        target_dag_id,
        start_date,
        end_date,
        requested_by,
    )

    return api_client.create_approval_request(
        requested_by=requested_by,
        reason=reason,
        target_dag_id=target_dag_id,
        start_date=start_date,
        end_date=end_date,
        alert_key=alert_key.strip(),
        parameters={},
    )


def decide_discord_approval_request(
    request_id: str,
    decision: str,
    decided_by: str,
    comment: str,
) -> dict[str, Any]:
    """
    Apply a durable Discord approval decision without executing remediation.

    Args:
        request_id: Human-facing APR identifier.
        decision: Approve or reject.
        decided_by: Discord operator identity.
        comment: Human-readable decision rationale.

    Returns:
        Latest durable approval request state.

    Raises:
        RuntimeError: If the control-plane API is not configured.
        ControlPlaneClientError: If the API rejects or cannot persist the decision.
    """
    api_client = build_control_plane_client()

    if api_client is None:
        raise RuntimeError("CONTROL_PLANE_API_URL is required for approval decisions.")

    logger.info(
        "Applying Discord approval decision | request_id=%s decision=%s decided_by=%s",
        request_id,
        decision,
        decided_by,
    )

    return api_client.decide_approval_request(
        request_id=request_id,
        decision=decision,
        decided_by=decided_by,
        comment=comment,
    )


def fetch_discord_approval_request(request_id: str) -> dict[str, Any]:
    """Load one authoritative approval request through the shared API boundary."""
    api_client = build_control_plane_client()

    if api_client is None:
        raise RuntimeError("CONTROL_PLANE_API_URL is required for approval lookup.")

    return api_client.get_approval_request(request_id=request_id.strip())


# --- Defining API-Only Incident Resolution
def _resolution_api_client() -> ControlPlaneClient:
    """Resolution must never fall back to a local lifecycle write."""
    client = build_control_plane_client()
    if client is None:
        raise RuntimeError("Pemeriksaan belum tersedia. Shared API belum dikonfigurasi.")
    return client


def _resolution_reference(value: str, *, request: bool = False) -> str:
    """Accept bounded path identifiers, never system keys or caller-supplied paths."""
    normalized = value.strip()
    pattern = r"[A-Za-z0-9-]{1,50}" if request else r"DQ-\d{8}-[A-Z0-9]{6,12}"
    if not re.fullmatch(pattern, normalized):
        raise ValueError("Incident atau request pemeriksaan tidak valid.")
    return normalized


def _validate_revalidation_payload(
    payload: dict[str, Any], *, alert_ref: str = "", request_id: str = "",
) -> dict[str, Any]:
    """Reject mismatched identity/state before rendering a closure control."""
    _resolution_reference(str(payload.get("request_id") or ""), request=True)
    _resolution_reference(str(payload.get("alert_ref") or ""))
    if alert_ref and payload["alert_ref"] != alert_ref:
        raise ControlPlaneResponseError("Pemeriksaan tidak cocok dengan incident yang dipilih.")
    if request_id and payload["request_id"] != request_id:
        raise ControlPlaneResponseError("Request pemeriksaan tidak cocok.")
    revision = payload.get("incident_revision")
    if type(revision) is not int or revision < 0:
        raise ControlPlaneResponseError("Revision pemeriksaan tidak tersedia.")
    if payload.get("status") not in {"queued", "running", "pass", "blocked", "error", "expired"}:
        raise ControlPlaneResponseError("Status pemeriksaan tidak valid.")
    if payload.get("result") is not None and not isinstance(payload["result"], dict):
        raise ControlPlaneResponseError("Hasil pemeriksaan tidak valid.")
    return payload


def fetch_discord_resolution_preview(alert_ref: str) -> dict[str, Any]:
    """Read authoritative scope, blockers, and the latest durable closure request."""
    reference = _resolution_reference(alert_ref)
    payload = _resolution_api_client().request_json(
        "GET", f"/api/v1/alerts/{reference}/resolution-preview",
    )
    revision = payload.get("incident_revision")
    if (payload.get("alert_ref") != reference or type(revision) is not int or revision < 0
            or not isinstance(payload.get("scope"), dict)
            or type(payload.get("available")) is not bool
            or not isinstance(payload.get("blockers"), list)
            or not all(isinstance(item, str) for item in payload["blockers"])):
        raise ControlPlaneResponseError("Preview penutupan tidak lengkap atau tidak cocok.")
    if payload.get("request") is not None:
        if not isinstance(payload["request"], dict):
            raise ControlPlaneResponseError("Request pemeriksaan tidak valid.")
        _validate_revalidation_payload(payload["request"], alert_ref=reference)
    return payload


def create_discord_revalidation(
    alert_ref: str, actor: str, idempotency_key: str,
) -> dict[str, Any]:
    """Request one read-only Airflow check; the API owns scope and triggering."""
    reference = _resolution_reference(alert_ref)
    client = _resolution_api_client()
    if not actor.strip() or not idempotency_key.strip():
        raise ValueError("Operator dan idempotency key wajib diisi.")
    payload = client.request_json(
        "POST", f"/api/v1/alerts/{reference}/revalidations",
        headers=client.approval_headers(),
        json_body={"actor": actor.strip(), "idempotency_key": idempotency_key.strip()},
    )
    return _validate_revalidation_payload(payload, alert_ref=reference)


def fetch_discord_revalidation(request_id: str) -> dict[str, Any]:
    """Read a stored result without polling Airflow inside a Discord handler."""
    reference = _resolution_reference(request_id, request=True)
    payload = _resolution_api_client().request_json(
        "GET", f"/api/v1/incident-revalidations/{reference}",
    )
    return _validate_revalidation_payload(payload, request_id=reference)


def resolve_discord_incident(
    alert_ref: str, resolution_request_id: str, expected_incident_revision: int,
    idempotency_key: str, actor: str, reason: str,
) -> dict[str, Any]:
    """Record human confirmation through the API, never approve or execute repairs."""
    reference = _resolution_reference(alert_ref)
    request_id = _resolution_reference(resolution_request_id, request=True)
    normalized_reason = reason.strip()
    if not 5 <= len(normalized_reason) <= 1000:
        raise ValueError("Alasan penutupan wajib berisi 5-1.000 karakter.")
    if type(expected_incident_revision) is not int or expected_incident_revision < 0:
        raise ValueError("Revision incident tidak valid.")
    if not actor.strip() or not idempotency_key.strip():
        raise ValueError("Operator dan idempotency key wajib diisi.")
    client = _resolution_api_client()
    payload = client.request_json(
        "POST", f"/api/v1/alerts/{reference}/resolve",
        headers=client.approval_headers(),
        json_body={
            "resolution_request_id": request_id,
            "expected_incident_revision": expected_incident_revision,
            "idempotency_key": idempotency_key.strip(),
            "actor": actor.strip(), "reason": normalized_reason,
        },
    )
    if payload.get("alert_ref") != reference or str(payload.get("status") or "").lower() != "resolved":
        raise ControlPlaneResponseError("Konfirmasi penutupan belum dapat diverifikasi.")
    return payload
