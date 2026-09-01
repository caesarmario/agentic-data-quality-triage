####
## Discord Bot Service Layer for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Shared data and control-plane operations used by the Discord adapter."""

# --- Importing Libraries
from __future__ import annotations

import os
from typing import Any

from agent.graph import DEFAULT_CONFIDENCE_TARGET, DEFAULT_MAX_EVIDENCE_LOOP, TriageRuntimeConfig, run_triage
from agent.llm.copilot import build_operator_answer, build_triage_copilot_note
from agent.state import TriageReport
from agent.tools.alerts import list_alerts, load_alert
from agent.tools.daily_summary import fetch_daily_quality_summary
from apps.common.control_plane import (
    ControlPlaneClient,
    ControlPlaneClientError,
    ControlPlaneResponseError,
    ControlPlaneTransportError,
)
from pipelines.common.logging import logger


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
                "Discord triage API unavailable; using local workflow | alert_key=%s error_type=%s",
                alert_key,
                type(exc).__name__,
            )

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
    normalized_alert_key = alert_key.strip()
    api_client           = build_control_plane_client(api_base_url=api_base_url)

    if normalized_alert_key and api_client:
        try:
            response = api_client.answer_copilot(
                question=question,
                alert_key=normalized_alert_key,
                audit_limit=10,
            )

            return {
                "answer": str(response["answer"]),
                "transport": "api",
                "agent_run_id": str(response.get("agent_run_id") or ""),
                "alert_key": str(response.get("alert_key") or normalized_alert_key),
                "incident_history_count": int(
                    response.get("incident_history_count") or 0
                ),
            }

        except ControlPlaneTransportError as exc:
            logger.warning(
                "Discord ask API unavailable; using local narrative | alert_key=%s error_type=%s",
                normalized_alert_key,
                type(exc).__name__,
            )

        except ControlPlaneResponseError:
            raise

    alert_context = None

    if normalized_alert_key:
        logger.info(
            "Loading local alert context for Discord ask fallback | alert_key=%s",
            normalized_alert_key,
        )
        alert_context = load_alert(alert_key=normalized_alert_key)

    answer = build_operator_answer(
        question=question,
        alert=alert_context,
    )

    return {
        "answer": answer,
        "transport": "local",
        "agent_run_id": "",
        "incident_history_count": 0,
        "alert_key": (
            alert_context.alert_key
            if alert_context is not None
            else normalized_alert_key
        ),
    }


# --- Defining Approval Helpers
def create_discord_backfill_approval_request(
    start_date: str,
    end_date: str,
    target_dag_id: str,
    reason: str,
    requested_by: str,
) -> dict[str, Any]:
    """
    Create or reuse one durable backfill approval request through FastAPI.

    Args:
        start_date: Inclusive YYYY-MM-DD start date.
        end_date: Inclusive YYYY-MM-DD end date.
        target_dag_id: Allowlisted operational DAG.
        reason: Human-readable approval reason.
        requested_by: Discord operator identity.

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
