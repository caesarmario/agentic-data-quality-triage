####
## Streamlit App for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import json
import os
import sys
import time
from datetime import date
from html import escape
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd
import requests
import streamlit as st


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Add repo root before importing project packages when Streamlit is launched from apps/streamlit.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.graph import DEFAULT_CONFIDENCE_TARGET, DEFAULT_MAX_EVIDENCE_LOOP, TriageRuntimeConfig, run_triage
from agent.llm import copilot as copilot_service
from agent.tools.alerts import list_alerts
from agent.tools.audit_log import write_agent_audit_event
from agent.tools.daily_summary import fetch_daily_quality_summary
from agent.tools.s3 import parse_s3_uri
from apps.common.control_plane import (
    MAX_REPORT_BYTES,
    ControlPlaneClient,
    ControlPlaneTransportError,
)
from apps.common.llm_observability import enrich_audit_rows, latest_llm_route_from_rows
from pipelines.common.clickhouse import build_clickhouse_client, quote_sql_literal
from pipelines.common.logging import logger
from pipelines.seeding.upload_to_s3 import build_s3_client


# --- Defining Constants
APP_TITLE               = "Agentic DQ Triage Platform"
DEFAULT_ALERT_LIMIT     = 50
DEFAULT_MANIFEST_S3_URI = "s3://dq-artifacts/dbt-artifacts/orders/latest/manifest.json"
DEFAULT_BLAST_RADIUS_DEPTH = 5
DEFAULT_BLAST_RADIUS_NODES = 100
CONTROL_PLANE_API_URL    = os.getenv("CONTROL_PLANE_API_URL", "").strip().rstrip("/")
COPILOT_API_TIMEOUT      = float(os.getenv("COPILOT_API_TIMEOUT_SECONDS", "15"))
CONTROL_PLANE_APPROVAL_TOKEN = os.getenv("CONTROL_PLANE_APPROVAL_TOKEN", "").strip()

SEVERITY_CSS_CLASSES = {
    "critical": "dq-severity-critical",
    "high": "dq-severity-critical",
    "warning": "dq-severity-warning",
    "medium": "dq-severity-warning",
    "info": "dq-severity-info",
    "low": "dq-severity-info",
}

COPILOT_GUIDED_PROMPTS = {
    "Explain this alert": "Explain this alert in plain language and tell me why it matters.",
    "Summarize evidence": "Summarize the evidence collected for this alert and separate facts from hypotheses.",
    "Review prior investigations": (
        "Has this Alert Ref been investigated before? Summarize earlier conclusions, "
        "then explain what still must be verified from current evidence."
    ),
    "Recommend next action": "Recommend the safest next investigation action for this alert.",
    "Draft backfill approval": "Draft an approval preview for a backfill if the evidence supports one.",
}

# --- Configuring Streamlit
st.set_page_config(
    page_title=APP_TITLE,
    layout="wide",
    initial_sidebar_state="expanded",
)


# --- Defining Control Plane Helpers
def build_streamlit_control_plane_client(
    api_base_url: str | None = None,
    timeout_seconds: float = COPILOT_API_TIMEOUT,
) -> ControlPlaneClient | None:
    """
    Build the optional shared API client used by Streamlit operations.

    Args:
        api_base_url: Optional explicit API URL. None uses the configured
            CONTROL_PLANE_API_URL value.
        timeout_seconds: HTTP timeout in seconds for one API request.

    Returns:
        Configured ControlPlaneClient when an API URL is available, otherwise
        None so local development can use deterministic project tools.
    """
    resolved_url = (
        CONTROL_PLANE_API_URL
        if api_base_url is None
        else api_base_url
    ).strip().rstrip("/")

    if not resolved_url:
        return None

    bounded_timeout = max(1.0, min(float(timeout_seconds), 60.0))

    return ControlPlaneClient(
        base_url=resolved_url,
        timeout_seconds=bounded_timeout,
    )


# --- Defining Style Helpers
def build_page_style_css() -> str:
    """
    Build semantic CSS that follows Streamlit's active light or dark theme.

    The `--st-*` variables are provided by Streamlit and change automatically
    when an operator switches theme from the app settings menu. Local `--dq-*`
    aliases keep project components consistent without duplicating color values.

    Returns:
        CSS text for cards, status surfaces, metrics, code, and long reports.
    """
    return """
        :root {
            color-scheme: light dark;
            --dq-surface: var(--st-secondary-background-color, #FFFFFF);
            --dq-surface-subtle: color-mix(
                in srgb,
                var(--st-secondary-background-color, #FFFFFF) 84%,
                var(--st-background-color, #F6F8F5)
            );
            --dq-text: var(--st-text-color, #18211B);
            --dq-muted: var(--st-gray-text-color, #3F4942);
            --dq-border: var(--st-border-color, #CBD5CE);
            --dq-border-light: var(--st-border-color-light, #E3E9E5);
            --dq-critical-bg: var(--st-red-background-color, #FBE9E7);
            --dq-critical-text: var(--st-red-text-color, #84261F);
            --dq-critical-border: var(--st-red-color, #B9382E);
            --dq-warning-bg: var(--st-orange-background-color, #FCECDD);
            --dq-warning-text: var(--st-orange-text-color, #713608);
            --dq-warning-border: var(--st-orange-color, #A45114);
            --dq-stable-bg: var(--st-green-background-color, #E5F5EC);
            --dq-stable-text: var(--st-green-text-color, #14543B);
            --dq-stable-border: var(--st-green-color, #1B6E4B);
            --dq-info-bg: var(--st-blue-background-color, #E8EFFB);
            --dq-info-text: var(--st-blue-text-color, #1F477F);
            --dq-info-border: var(--st-blue-color, #2B5CA8);
            --dq-shadow: 0 12px 32px color-mix(
                in srgb,
                var(--st-text-color, #18211B) 9%,
                transparent
            );
        }

        .stApp {
            background: var(--st-background-color, #F6F8F5);
            color: var(--dq-text);
        }

        .block-container {
            max-width: 1500px;
            padding-top: 2rem;
            padding-bottom: 3rem;
        }

        section[data-testid="stSidebar"] {
            border-right: 1px solid var(--dq-border);
        }

        div[data-testid="stMetric"] {
            min-height: 7.2rem;
            padding: 0.85rem 1rem;
            border: 1px solid var(--dq-border);
            border-radius: 14px;
            background: var(--dq-surface);
            box-shadow: 0 4px 16px color-mix(
                in srgb,
                var(--st-text-color, #18211B) 5%,
                transparent
            );
        }

        div[data-testid="stMetricLabel"] {
            color: var(--dq-muted);
        }

        div[data-testid="stMetricValue"] {
            color: var(--dq-text);
            font-size: 1.7rem;
            line-height: 1.15;
        }

        .dq-card {
            border: 1px solid var(--dq-border);
            border-radius: 16px;
            padding: 1.1rem 1.2rem;
            background: var(--dq-surface);
            color: var(--dq-text);
            box-shadow: var(--dq-shadow);
        }

        .dq-card code,
        .dq-health code {
            display: inline-block;
            max-width: 100%;
            overflow-wrap: anywhere;
            white-space: normal;
            color: inherit;
            background: color-mix(in srgb, currentColor 9%, transparent);
            border: 1px solid color-mix(in srgb, currentColor 18%, transparent);
            border-radius: 6px;
            padding: 0.12rem 0.38rem;
        }

        .dq-muted {
            color: var(--dq-muted);
            font-size: 0.92rem;
            line-height: 1.5;
        }

        .dq-separator {
            border-top: 1px solid var(--dq-border-light);
            margin: 1rem 0;
        }

        .dq-health {
            border-radius: 16px;
            padding: 1.05rem 1.2rem;
            margin: 0.6rem 0 1.2rem 0;
            border: 1px solid var(--dq-border);
            box-shadow: var(--dq-shadow);
        }

        .dq-health .dq-muted,
        .dq-severity-critical .dq-muted,
        .dq-severity-warning .dq-muted,
        .dq-severity-info .dq-muted {
            color: inherit;
            opacity: 0.86;
        }

        .dq-health-critical,
        .dq-severity-critical {
            background: var(--dq-critical-bg);
            color: var(--dq-critical-text);
            border-color: var(--dq-critical-border);
        }

        .dq-health-warning,
        .dq-severity-warning {
            background: var(--dq-warning-bg);
            color: var(--dq-warning-text);
            border-color: var(--dq-warning-border);
        }

        .dq-health-stable {
            background: var(--dq-stable-bg);
            color: var(--dq-stable-text);
            border-color: var(--dq-stable-border);
        }

        .dq-severity-info {
            background: var(--dq-info-bg);
            color: var(--dq-info-text);
            border-color: var(--dq-info-border);
        }

        div[data-testid="stExpander"],
        div[data-testid="stCodeBlock"],
        div[data-testid="stJson"] {
            border-color: var(--dq-border);
            border-radius: 12px;
        }

        .st-key-triage_report_document,
        .st-key-artifact_report_document {
            background: var(--dq-surface-subtle);
            border-color: var(--dq-border) !important;
        }

        .st-key-triage_report_document [data-testid="stMarkdownContainer"],
        .st-key-artifact_report_document [data-testid="stMarkdownContainer"] {
            max-width: 88ch;
        }

        .st-key-triage_report_document [data-testid="stMarkdownContainer"] p,
        .st-key-triage_report_document [data-testid="stMarkdownContainer"] li,
        .st-key-artifact_report_document [data-testid="stMarkdownContainer"] p,
        .st-key-artifact_report_document [data-testid="stMarkdownContainer"] li {
            line-height: 1.68;
        }

        @media (max-width: 768px) {
            .block-container {
                padding-top: 1.25rem;
                padding-left: 1rem;
                padding-right: 1rem;
            }

            div[data-testid="stMetric"] {
                min-height: auto;
            }
        }
    """


def apply_page_style() -> None:
    """
    Apply the semantic project CSS after Streamlit resolves the active theme.

    Returns:
        None.
    """
    active_theme = str(st.context.theme.type or "system")

    logger.info("Applying Streamlit semantic theme | active_theme=%s", active_theme)
    st.markdown(
        f"<style>{build_page_style_css()}</style>",
        unsafe_allow_html=True,
    )


def severity_css_class(value: Any) -> str:
    """
    Map one alert severity into an allowlisted semantic CSS class.

    Args:
        value: Raw alert severity from ClickHouse or a test fixture.

    Returns:
        Critical, warning, info, or neutral card class.
    """
    normalized = str(value or "").strip().lower()

    return SEVERITY_CSS_CLASSES.get(normalized, "dq-severity-neutral")


def escape_html_text(value: Any) -> str:
    """
    Escape one operator-facing value before inserting it into custom HTML.

    Args:
        value: Alert label, metric, table, reference, or system key.

    Returns:
        HTML-safe text without changing the underlying value.
    """
    return escape(str(value or ""), quote=True)


def render_report_document(
    markdown_text: str,
    container_key: str,
    source_label: str,
) -> None:
    """
    Render one long Markdown report inside a theme-aware reading surface.

    Args:
        markdown_text: Markdown report generated by triage or loaded from S3.
        container_key: Stable Streamlit key used by semantic CSS selectors.
        source_label: Short operator-facing description of the report source.

    Returns:
        None.
    """
    normalized_markdown = str(markdown_text or "").strip()

    if not normalized_markdown:
        st.info("The selected report is empty.")
        return

    logger.info(
        "Rendering Streamlit report document | source=%s | characters=%s",
        source_label,
        len(normalized_markdown),
    )

    # Keep long reports visually separate from controls and constrain line length for readability.
    with st.container(border=True, key=container_key):
        st.caption(source_label)
        st.markdown(normalized_markdown)


def render_header() -> None:
    """
    Render the Streamlit page header.

    Returns:
        None.
    """
    st.title(APP_TITLE)
    st.caption(
        "Local demo UI for ClickHouse alerts, guarded evidence, LangGraph triage reports, "
        "and approval-gated action previews."
    )


# --- Defining Data Helpers
def parse_json_object(value: Any) -> dict[str, Any]:
    """
    Parse a JSON object from text safely for UI rendering.

    Args:
        value: Raw JSON string, dictionary, or None.

    Returns:
        Parsed dictionary. Invalid or non-object JSON returns an empty dictionary.
    """
    if value is None:
        return {}

    if isinstance(value, dict):
        return value

    if not isinstance(value, str) or not value.strip():
        return {}

    try:
        parsed = json.loads(value)

    except json.JSONDecodeError:
        logger.warning("Failed to parse JSON object for Streamlit UI | value=%s", value[:200])
        return {}

    return parsed if isinstance(parsed, dict) else {}


def dataframe_from_rows(rows: list[dict[str, Any]]) -> pd.DataFrame:
    """
    Convert alert or audit rows into a display-ready DataFrame.

    Args:
        rows: List of dictionaries returned from ClickHouse or agent tools.

    Returns:
        Pandas DataFrame for Streamlit rendering.
    """
    if not rows:
        return pd.DataFrame()

    return pd.DataFrame(rows)


def summarize_alert_rows(alerts: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Summarize loaded alert rows for operator-friendly reliability cards.

    Args:
        alerts: Alert rows currently loaded in the UI.

    Returns:
        Dictionary containing severity counts, status counts, affected dates, affected tables, and report count.
    """
    severity_counts: dict[str, int] = {}
    status_counts: dict[str, int]   = {}
    affected_dates                  = set()
    affected_tables                 = set()
    report_count                    = 0

    for alert in alerts:
        severity = str(alert.get("severity") or "unknown").lower()
        status   = str(alert.get("status") or "unknown").lower()

        severity_counts[severity] = severity_counts.get(severity, 0) + 1
        status_counts[status]     = status_counts.get(status, 0) + 1

        if alert.get("dt"):
            affected_dates.add(str(alert["dt"]))

        if alert.get("table_name"):
            affected_tables.add(str(alert["table_name"]))

        if alert.get("report_s3_uri"):
            report_count += 1

    summary = {
        "total_alerts": len(alerts),
        "severity_counts": severity_counts,
        "status_counts": status_counts,
        "affected_dates": sorted(affected_dates),
        "affected_tables": sorted(affected_tables),
        "affected_table_count": len(affected_tables),
        "report_count": report_count,
        "open_count": status_counts.get("open", 0),
        "critical_count": severity_counts.get("critical", 0),
        "warning_count": severity_counts.get("warning", 0),
    }

    logger.info(
        "Built Streamlit alert summary | total=%d critical=%d warning=%d open=%d",
        summary["total_alerts"],
        summary["critical_count"],
        summary["warning_count"],
        summary["open_count"],
    )

    return summary


def summarize_daily_quality_payload(payload: dict[str, Any]) -> dict[str, int]:
    """
    Normalize one daily DQ summary into fixed operator metric names.

    Args:
        payload: Validated daily summary returned by FastAPI or the local tool.

    Returns:
        Dictionary containing check outcomes and open-alert severity counts.
    """
    check_counts = {
        str(item.get("status") or "").strip().lower(): int(item.get("count") or 0)
        for item in payload.get("check_counts", [])
    }
    alert_counts = {
        str(item.get("severity") or "").strip().lower(): int(item.get("count") or 0)
        for item in payload.get("alert_counts", [])
    }

    summary = {
        "total_checks": int(payload.get("total_checks") or 0),
        "passed_checks": check_counts.get("pass", 0),
        "warning_checks": check_counts.get("warn", 0),
        "failed_checks": check_counts.get("fail", 0),
        "skipped_checks": check_counts.get("skip", 0),
        "total_open_alerts": int(payload.get("total_open_alerts") or 0),
        "critical_alerts": alert_counts.get("critical", 0),
        "warning_alerts": alert_counts.get("warning", 0),
    }

    logger.info(
        "Built Streamlit daily quality summary | dt=%s checks=%d failed=%d open_alerts=%d",
        payload.get("dt"),
        summary["total_checks"],
        summary["failed_checks"],
        summary["total_open_alerts"],
    )

    return summary


def classify_reliability_state(
    summary: dict[str, Any],
    daily_summary: dict[str, Any] | None = None,
    summary_error: str | None = None,
) -> dict[str, str]:
    """
    Classify reliability from daily checks plus the currently loaded alerts.

    Args:
        summary: Alert summary returned by summarize_alert_rows.
        daily_summary: Optional validated daily DQ summary payload.
        summary_error: Optional daily-summary loading error shown fail-closed.

    Returns:
        Dictionary containing label, CSS class, and explanation.
    """
    daily_counts = summarize_daily_quality_payload(daily_summary) if daily_summary else {}

    if (
        summary["critical_count"] > 0
        or daily_counts.get("failed_checks", 0) > 0
        or daily_counts.get("critical_alerts", 0) > 0
    ):
        return {
            "label": "Critical attention required",
            "css_class": "dq-health-critical",
            "message": "A failed DQ check or critical alert requires investigation before downstream data is trusted.",
        }

    if summary_error:
        return {
            "label": "Daily quality status unavailable",
            "css_class": "dq-health-warning",
            "message": "The daily DQ snapshot could not be loaded. Alert workflows remain available, but this state must not be treated as healthy.",
        }

    if daily_summary and daily_counts.get("total_checks", 0) == 0:
        return {
            "label": "Daily checks have not run",
            "css_class": "dq-health-warning",
            "message": "No DQ check result exists for the selected date. Wait for the scheduled pipeline or investigate the missing run before trusting the data.",
        }

    if (
        summary["warning_count"] > 0
        or summary["open_count"] > 0
        or daily_counts.get("warning_checks", 0) > 0
        or daily_counts.get("skipped_checks", 0) > 0
        or daily_counts.get("warning_alerts", 0) > 0
    ):
        return {
            "label": "Warning watchlist",
            "css_class": "dq-health-warning",
            "message": "The platform found non-critical alerts. Review evidence and decide whether triage or monitoring is enough.",
        }

    return {
        "label": "Stable for selected date and filters",
        "css_class": "dq-health-stable",
        "message": "No failed daily check or material alert is visible for the selected scope. Continue monitoring scheduled runs.",
    }


def fetch_streamlit_alert_rows(
    status: str,
    dt: str | None,
    limit: int,
    api_base_url: str | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """
    Fetch alerts through FastAPI with transport-only local fallback.

    Args:
        status: Alert lifecycle status filter.
        dt: Optional business date in YYYY-MM-DD format.
        limit: Maximum alerts to return.
        api_base_url: Optional API URL override used by tests or local tools.

    Returns:
        Tuple containing public alert rows and the selected transport label.

    Raises:
        ControlPlaneResponseError: If the API returns an invalid, rejected, or
            identity-mismatched response. These failures are not hidden by a
            local fallback.
    """
    api_client = build_streamlit_control_plane_client(api_base_url=api_base_url)

    if api_client:
        try:
            payload = api_client.list_alerts(
                status=status,
                dt=dt or None,
                limit=limit,
            )

            return list(payload["alerts"]), "api"

        except ControlPlaneTransportError as exc:
            logger.warning(
                "Streamlit alert API unavailable; using local alert tool | error_type=%s",
                type(exc).__name__,
            )

    # A local read is allowed only when the API is not configured or cannot be reached.
    payload = list_alerts(status=status, dt=dt or None, limit=limit)

    return list(payload["alerts"]), "local"


def fetch_streamlit_daily_summary(
    dt: str,
    api_base_url: str | None = None,
) -> tuple[dict[str, Any], str]:
    """
    Fetch one daily DQ snapshot through FastAPI with transport-only fallback.

    Args:
        dt: Exact business date in YYYY-MM-DD format.
        api_base_url: Optional API URL override used by tests or local tools.

    Returns:
        Tuple containing a public daily-summary payload and transport label.

    Raises:
        ControlPlaneResponseError: If the API response violates its public
            identity, aggregate, or privacy contract.
    """
    api_client = build_streamlit_control_plane_client(api_base_url=api_base_url)

    if api_client:
        try:
            payload = api_client.get_daily_summary(dt=dt)

            return payload, "api"

        except ControlPlaneTransportError as exc:
            logger.warning(
                "Streamlit daily-summary API unavailable; using local tool | error_type=%s",
                type(exc).__name__,
            )

    # Local fallback remains deterministic, audited, and stripped of internal SQL metadata.
    payload = dict(fetch_daily_quality_summary(dt=dt))
    payload.pop("sql", None)

    return payload, "local"


@st.cache_data(ttl=15, show_spinner=False)
def load_alert_rows(
    status: str,
    dt: str | None,
    limit: int,
    api_base_url: str | None = None,
) -> list[dict[str, Any]]:
    """
    Load and cache alert rows for the Streamlit operator surface.

    Args:
        status: Alert lifecycle status filter.
        dt: Optional business date in YYYY-MM-DD format.
        limit: Maximum alerts to return.
        api_base_url: Optional API URL override used by tests or local tools.

    Returns:
        List of public alert rows.
    """
    logger.info("Loading alerts for Streamlit | status=%s dt=%s limit=%s", status, dt, limit)

    rows, transport = fetch_streamlit_alert_rows(
        status=status,
        dt=dt,
        limit=limit,
        api_base_url=api_base_url,
    )

    logger.info("Loaded Streamlit alerts | transport=%s rows=%d", transport, len(rows))

    return rows


@st.cache_data(ttl=15, show_spinner=False)
def load_daily_quality_summary(
    dt: str,
    api_base_url: str | None = None,
) -> tuple[dict[str, Any], str]:
    """
    Load and cache one daily DQ snapshot for the Reliability Overview.

    Args:
        dt: Exact business date in YYYY-MM-DD format.
        api_base_url: Optional API URL override used by tests or local tools.

    Returns:
        Tuple containing the public daily summary and selected transport.
    """
    logger.info("Loading daily quality summary for Streamlit | dt=%s", dt)

    payload, transport = fetch_streamlit_daily_summary(
        dt=dt,
        api_base_url=api_base_url,
    )

    logger.info(
        "Loaded Streamlit daily quality summary | dt=%s transport=%s checks=%d open_alerts=%d",
        dt,
        transport,
        payload["total_checks"],
        payload["total_open_alerts"],
    )

    return payload, transport


def load_local_audit_rows(alert_key: str, limit: int) -> list[dict[str, Any]]:
    """
    Read and sanitize audit events directly from local ClickHouse.

    Args:
        alert_key: Stable system alert key.
        limit: Maximum audit events to return.

    Returns:
        Enriched audit rows without raw LLM input or output payloads.
    """
    client     = build_clickhouse_client()
    safe_limit = max(1, min(limit, 100))
    result     = client.query(
        f"""
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
        FROM dq.agent_audit_log
        WHERE alert_key = {quote_sql_literal(alert_key)}
        ORDER BY ts DESC
        LIMIT {safe_limit}
        """
    )
    columns    = list(result.column_names or [])
    raw_rows   = [dict(zip(columns, row)) for row in result.result_rows]
    rows, _    = enrich_audit_rows(rows=raw_rows)

    return rows


def fetch_streamlit_audit_rows(
    alert_key: str,
    limit: int = 25,
    api_base_url: str | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """
    Fetch audit history through FastAPI with transport-only local fallback.

    Args:
        alert_key: Stable system alert key.
        limit: Maximum audit events to return.
        api_base_url: Optional API URL override used by tests or local tools.

    Returns:
        Tuple containing sanitized audit rows and selected transport label.

    Raises:
        ControlPlaneResponseError: If the API response violates the public
            audit contract. Contract failures never trigger local reads.
    """
    safe_limit = max(1, min(limit, 100))
    api_client = build_streamlit_control_plane_client(api_base_url=api_base_url)

    if api_client:
        try:
            payload = api_client.get_audit_logs(
                alert_key=alert_key,
                limit=safe_limit,
            )

            return list(payload["rows"]), "api"

        except ControlPlaneTransportError as exc:
            logger.warning(
                "Streamlit audit API unavailable; using local ClickHouse read | "
                "alert_key=%s error_type=%s",
                alert_key,
                type(exc).__name__,
            )

    # Preserve local operability without masking API contract or identity failures.
    return load_local_audit_rows(alert_key=alert_key, limit=safe_limit), "local"


@st.cache_data(ttl=10, show_spinner=False)
def load_audit_rows(
    alert_key: str,
    limit: int = 25,
    api_base_url: str | None = None,
) -> list[dict[str, Any]]:
    """
    Load and cache recent agent audit rows for one alert.

    Args:
        alert_key: Stable alert key.
        limit: Maximum audit rows to return.
        api_base_url: Optional API URL override used by tests or local tools.

    Returns:
        List of recent audit log rows.
    """
    rows, transport = fetch_streamlit_audit_rows(
        alert_key=alert_key,
        limit=limit,
        api_base_url=api_base_url,
    )

    logger.info(
        "Loaded audit rows for Streamlit | alert_key=%s transport=%s rows=%d",
        alert_key,
        transport,
        len(rows),
    )

    return rows


def build_llm_runtime_summary(audit_rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """
    Build compact AI runtime metrics from enriched audit rows.

    Args:
        audit_rows: Recent audit rows ordered newest first.

    Returns:
        Operator-facing runtime summary or None when no LLM event exists.
    """
    route = latest_llm_route_from_rows(rows=audit_rows)

    if not route:
        return None

    mode_labels = {
        "external_model": "External model",
        "heuristic_fallback": "Heuristic fallback",
        "failed": "Failed",
    }

    return {
        **route,
        "mode_label": mode_labels.get(str(route.get("runtime_mode")), "Unknown"),
        "provider_model": f"{route.get('provider') or 'unknown'} / {route.get('model') or 'unknown'}",
        "route_label": f"{route.get('requested_route') or 'unknown'} -> {route.get('executed_route') or 'unknown'}",
        "token_label": f"{int(route.get('input_tokens') or 0)} in / {int(route.get('output_tokens') or 0)} out",
    }


def read_s3_text(s3_uri: str, max_bytes: int | None = None) -> str:
    """
    Read a text artifact from local S3-compatible storage.

    Args:
        s3_uri: S3 URI to read.
        max_bytes: Optional hard byte limit for bounded report fallback reads.

    Returns:
        UTF-8 decoded text body.

    Raises:
        ValueError: If the URI is malformed.
        ValueError: If the artifact exceeds max_bytes when a bound is provided.
        botocore.exceptions.BotoCoreError: If the artifact cannot be read.
    """
    bucket, key = parse_s3_uri(s3_uri)
    client      = build_s3_client()

    logger.info("Reading S3 text artifact for Streamlit | uri=%s", s3_uri)

    response = client.get_object(Bucket=bucket, Key=key)
    body     = (
        response["Body"].read()
        if max_bytes is None
        else response["Body"].read(max(1, int(max_bytes)) + 1)
    )

    if max_bytes is not None and len(body) > max(1, int(max_bytes)):
        raise ValueError(f"S3 text artifact exceeds the {max_bytes}-byte safety limit.")

    return body.decode("utf-8")


def read_report_text(
    s3_uri: str,
    api_base_url: str | None = None,
    max_bytes: int = MAX_REPORT_BYTES,
) -> tuple[str, str]:
    """
    Read one bounded triage report through the shared control-plane boundary.

    Args:
        s3_uri: Approved Markdown or JSON report artifact URI.
        api_base_url: Optional API URL override used by tests or local tools.
        max_bytes: Maximum UTF-8 report bytes that may be returned.

    Returns:
        Tuple containing report text and selected transport label.

    Raises:
        ControlPlaneResponseError: If the API rejects the artifact or violates
            identity and byte-bound contracts.
        ValueError: If the URI is invalid or the local fallback exceeds the
            same hard report size bound.
    """
    safe_max_bytes = max(1, min(int(max_bytes), MAX_REPORT_BYTES))
    api_client     = build_streamlit_control_plane_client(api_base_url=api_base_url)

    if api_client:
        try:
            payload = api_client.read_report_artifact(
                s3_uri=s3_uri,
                max_bytes=safe_max_bytes,
            )

            return str(payload["text"]), "api"

        except ControlPlaneTransportError as exc:
            logger.warning(
                "Streamlit report API unavailable; using bounded local S3 read | "
                "uri=%s error_type=%s",
                s3_uri,
                type(exc).__name__,
            )

    # Local fallback retains the public API byte bound instead of reading unbounded content.
    return read_s3_text(s3_uri=s3_uri, max_bytes=safe_max_bytes), "local"


def find_report_uri(alert: dict[str, Any], audit_rows: list[dict[str, Any]]) -> str:
    """
    Resolve the best available report URI for an alert.

    Args:
        alert: Selected alert row.
        audit_rows: Recent audit rows for the alert.

    Returns:
        Report S3 URI when available, otherwise an empty string.
    """
    if alert.get("report_s3_uri"):
        return str(alert["report_s3_uri"])

    for row in audit_rows:
        report_uri = row.get("report_s3_uri")

        if report_uri:
            return str(report_uri)

    details = parse_json_object(alert.get("details_json") or alert.get("details"))

    return str(details.get("report_s3_uri") or "")


# --- Defining Triage Helpers
def run_selected_alert_triage(
    alert_key: str,
    confidence_threshold: float,
    max_evidence_iterations: int,
    manifest_s3_uri: str,
    api_base_url: str | None = None,
) -> dict[str, Any]:
    """
    Run selected-alert triage through FastAPI with transport-only fallback.

    Args:
        alert_key: Stable alert key.
        confidence_threshold: Minimum confidence target before finalizing.
        max_evidence_iterations: Maximum bounded extra-evidence loops.
        manifest_s3_uri: S3 URI for dbt manifest lineage evidence.
        api_base_url: Optional API URL override used by tests or local tools.

    Returns:
        Triage report summary, transport metadata, and typed report model.

    Raises:
        ControlPlaneResponseError: If the API rejects the request, returns a
            malformed report, or violates alert and run identity contracts.
    """
    api_client      = build_streamlit_control_plane_client(api_base_url=api_base_url)
    started_at      = time.monotonic()
    transport       = "local"
    fallback_reason = "control_plane_api_not_configured" if api_client is None else ""

    if api_client:
        try:
            logger.info("Running Streamlit triage through API | alert_key=%s", alert_key)

            report    = api_client.run_triage_report(
                alert_key=alert_key,
                confidence_threshold=confidence_threshold,
                max_evidence_iterations=max_evidence_iterations,
                manifest_s3_uri=manifest_s3_uri,
            )
            transport = "api"

        except ControlPlaneTransportError as exc:
            fallback_reason = "control_plane_transport_unavailable"

            logger.warning(
                "Streamlit triage API unavailable; using local LangGraph fallback | "
                "alert_key=%s error_type=%s",
                alert_key,
                type(exc).__name__,
            )

    if transport == "local":
        config = TriageRuntimeConfig(manifest_s3_uri=manifest_s3_uri or None)

        logger.info(
            "Running Streamlit triage locally | alert_key=%s fallback_reason=%s",
            alert_key,
            fallback_reason,
        )

        report = run_triage(
            alert_key=alert_key,
            confidence_threshold=confidence_threshold,
            max_evidence_iterations=max_evidence_iterations,
            config=config,
        )

    duration_ms = int((time.monotonic() - started_at) * 1000)

    summary = {
        "status": "success",
        "transport": transport,
        "fallback_reason": fallback_reason,
        "duration_ms": duration_ms,
        "agent_run_id": str(report.agent_run_id),
        "alert_display_id": report.alert.alert_display_id,
        "alert_key": report.alert.alert_key,
        "confidence": report.confidence,
        "top_hypothesis": report.top_hypothesis.title if report.top_hypothesis else "",
        "markdown_report_s3_uri": report.markdown_report_s3_uri,
        "json_report_s3_uri": report.json_report_s3_uri,
        "approval_gated_actions": [action.model_dump(mode="json") for action in report.approval_gated_actions],
    }

    logger.info(
        "Completed Streamlit triage | alert_key=%s transport=%s duration_ms=%d",
        alert_key,
        transport,
        duration_ms,
    )

    return {"summary": summary, "report": report}


def matching_triage_result(
    alert_key: str,
    latest_triage_result: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """
    Return the latest triage result only when it belongs to the selected alert.

    Args:
        alert_key: Stable key for the currently selected alert.
        latest_triage_result: Optional result stored in Streamlit session state.

    Returns:
        Matching triage result, otherwise None.
    """
    if not latest_triage_result:
        return None

    summary = latest_triage_result.get("summary") or {}

    if str(summary.get("alert_key") or "") != alert_key:
        return None

    return latest_triage_result


def build_ui_copilot_context(
    alert: dict[str, Any],
    latest_triage_result: dict[str, Any] | None,
    audit_rows: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """
    Build bounded, alert-scoped context for the shared Copilot narrative service.

    Args:
        alert: Currently selected alert row.
        latest_triage_result: Optional triage result from Streamlit session state.
        audit_rows: Optional recent audit events for the selected alert.

    Returns:
        Dictionary containing report, evidence, audit, and context statistics.
    """
    alert_key      = str(alert.get("alert_key") or "")
    matching_result = matching_triage_result(
        alert_key=alert_key,
        latest_triage_result=latest_triage_result,
    )
    report_context: dict[str, Any] = {}
    evidence_rows: list[dict[str, Any]] = []

    if matching_result:
        report         = matching_result["report"]
        top_hypothesis = report.top_hypothesis
        recommended_action = (
            top_hypothesis.recommended_action
            if top_hypothesis and top_hypothesis.recommended_action
            else next(iter(report.recommended_actions), "")
        )
        report_context = {
            "summary": report.summary,
            "impact": report.impact,
            "top_hypothesis": top_hypothesis.title if top_hypothesis else "",
            "confidence": report.confidence,
            "recommended_action": recommended_action,
            "approval_required": bool(report.approval_gated_actions),
            "report_id": report.report_id,
        }
        evidence_rows = [
            {
                "tool_name": item.tool_name,
                "evidence_type": getattr(item.evidence_type, "value", str(item.evidence_type)),
                "summary": item.summary,
                "row_count": item.row_count,
                "s3_uri": item.s3_uri,
            }
            for item in report.evidence
        ]

    safe_audit_rows = list(audit_rows or [])

    return {
        "report_context": report_context,
        "evidence_rows": evidence_rows,
        "audit_rows": safe_audit_rows,
        "has_report": bool(matching_result),
        "evidence_count": len(evidence_rows),
        "audit_count": len(safe_audit_rows),
    }


def request_ui_copilot_api(
    question: str,
    alert: dict[str, Any],
    latest_triage_result: dict[str, Any] | None,
    api_base_url: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    """
    Request one Copilot answer from the shared control-plane client.

    Args:
        question: Guided or free-text operator question.
        alert: Currently selected alert row.
        latest_triage_result: Optional matching triage result.
        api_base_url: Control-plane API base URL.
        timeout_seconds: HTTP request timeout.

    Returns:
        Normalized API Copilot response for Streamlit rendering.

    Raises:
        ControlPlaneTransportError: If the API cannot be reached.
        ValueError: If response context does not match the selected alert.
    """
    alert_key       = str(alert.get("alert_key") or "")
    matching_result = matching_triage_result(
        alert_key=alert_key,
        latest_triage_result=latest_triage_result,
    )
    report_json_s3_uri = ""

    if matching_result:
        report_json_s3_uri = str(
            matching_result["report"].json_report_s3_uri or ""
        )

    logger.info(
        "Requesting Streamlit Copilot through API | alert_ref=%s report_context=%s",
        alert.get("alert_display_id"),
        bool(report_json_s3_uri),
    )

    api_client = ControlPlaneClient(
        base_url=api_base_url,
        timeout_seconds=timeout_seconds,
    )
    response_payload = api_client.answer_copilot(
        question=question,
        alert_key=alert_key,
        report_json_s3_uri=report_json_s3_uri,
        audit_limit=10,
    )

    if str(response_payload.get("alert_key") or "") != alert_key:
        raise ValueError("Copilot API response alert_key does not match the selected alert.")

    return {
        "question": question,
        "answer": str(response_payload.get("answer") or ""),
        "has_report": response_payload.get("context_source") == "alert_report_audit",
        "evidence_count": int(response_payload.get("evidence_count") or 0),
        "audit_count": int(response_payload.get("audit_count") or 0),
        "incident_history_count": int(
            response_payload.get("incident_history_count") or 0
        ),
        "agent_run_id": str(response_payload.get("agent_run_id") or ""),
        "transport": "api",
        "fallback_reason": "",
    }

def answer_ui_copilot_question(
    question: str,
    alert: dict[str, Any],
    latest_triage_result: dict[str, Any] | None,
    audit_rows: list[dict[str, Any]] | None,
    api_base_url: str | None = None,
    api_timeout: float = COPILOT_API_TIMEOUT,
) -> dict[str, Any]:
    """
    Answer one UI Copilot question through API-first shared LLM routing.

    Args:
        question: Guided or free-text operator question.
        alert: Currently selected alert row.
        latest_triage_result: Optional matching triage result.
        audit_rows: Optional recent audit events for local fallback.
        api_base_url: Optional API URL override. None uses environment configuration.
        api_timeout: HTTP timeout for the optional API transport.

    Returns:
        Copilot answer plus context and transport metadata.
    """
    resolved_api_url = CONTROL_PLANE_API_URL if api_base_url is None else api_base_url.strip().rstrip("/")
    fallback_reason  = ""

    if resolved_api_url:
        try:
            return request_ui_copilot_api(
                question=question,
                alert=alert,
                latest_triage_result=latest_triage_result,
                api_base_url=resolved_api_url,
                timeout_seconds=max(1.0, min(api_timeout, 60.0)),
            )

        except ControlPlaneTransportError as exc:
            fallback_reason = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "Streamlit Copilot API unavailable; using shared local fallback | alert_ref=%s error=%s",
                alert.get("alert_display_id"),
                fallback_reason,
            )

    context = build_ui_copilot_context(
        alert=alert,
        latest_triage_result=latest_triage_result,
        audit_rows=audit_rows,
    )

    logger.info(
        "Running Streamlit Copilot locally | alert_ref=%s has_report=%s evidence=%d audit=%d",
        alert.get("alert_display_id"),
        context["has_report"],
        context["evidence_count"],
        context["audit_count"],
    )

    answer = copilot_service.build_operator_answer(
        question=question,
        alert=alert,
        report_context=context["report_context"],
        evidence_rows=context["evidence_rows"],
        audit_rows=context["audit_rows"],
    )

    return {
        "question": question,
        "answer": answer,
        "has_report": context["has_report"],
        "evidence_count": context["evidence_count"],
        "audit_count": context["audit_count"],
        "incident_history_count": 0,
        "agent_run_id": "",
        "transport": "local",
        "fallback_reason": fallback_reason,
    }

def record_mock_external_action(alert: dict[str, Any], action_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """
    Record a mocked external Slack or ticket action in the UI and audit log.

    Args:
        alert: Selected alert row.
        action_name: Mock action name.
        payload: Action payload shown to the user.

    Returns:
        Mock action result payload.
    """
    client       = build_clickhouse_client()
    agent_run_id = uuid4()
    alert_key    = str(alert.get("alert_key") or "")
    alert_id     = alert.get("alert_id")

    result = {
        "status": "mocked",
        "action": action_name,
        "alert_display_id": alert.get("alert_display_id"),
        "alert_key": alert_key,
        "payload": payload,
    }

    write_agent_audit_event(
        client=client,
        action=action_name,
        status="mocked",
        agent_run_id=agent_run_id,
        alert_id=alert_id,
        alert_key=alert_key,
        actor="user",
        tool_name="streamlit_mock_action",
        input_payload=payload,
        output_payload=result,
        row_count=1,
    )

    logger.info("Recorded mocked external action | alert_key=%s action=%s", alert_key, action_name)

    return result


def build_ui_backfill_approval_payload(
    alert: dict[str, Any],
    action: Any,
    requested_by: str,
    agent_run_id: str = "",
) -> dict[str, Any]:
    """
    Convert one triage backfill recommendation into the durable API contract.

    Args:
        alert: Selected source alert row.
        action: ApprovalGatedAction model or serialized dictionary.
        requested_by: Human identity creating the approval request.
        agent_run_id: Optional source triage run UUID.

    Returns:
        Bounded approval creation keyword arguments.

    Raises:
        ValueError: If the action is not a complete backfill recommendation.
    """
    payload = (
        action.model_dump(mode="json")
        if hasattr(action, "model_dump")
        else dict(action)
    )
    action_type = payload.get("action_type")

    if hasattr(action_type, "value"):
        action_type = action_type.value

    if str(action_type) != "backfill":
        raise ValueError("Only backfill recommendations can create approval requests in the current UI.")

    parameters          = dict(payload.get("parameters") or {})
    target_dag_id       = str(parameters.pop("target_dag_id", "")).strip()
    parameter_requester = str(parameters.pop("requested_by", "")).strip()
    parameter_reason    = str(parameters.pop("reason", "")).strip()
    start_date          = str(payload.get("start_date") or "").strip()
    end_date            = str(payload.get("end_date") or "").strip()
    reason              = str(payload.get("reason") or parameter_reason).strip()

    if str(payload.get("target_dag_id") or "") != "90_dag_dq_platform_backfill_dispatcher":
        raise ValueError("Backfill recommendation must use the controlled Airflow dispatcher.")

    if not target_dag_id or not start_date or not end_date:
        raise ValueError("Backfill recommendation is missing target DAG or date scope.")

    return {
        "requested_by": requested_by.strip() or parameter_requester or "streamlit_operator",
        "reason": reason,
        "target_dag_id": target_dag_id,
        "start_date": start_date,
        "end_date": end_date,
        "parameters": parameters,
        "alert_id": str(alert.get("alert_id") or "") or None,
        "alert_key": str(alert.get("alert_key") or ""),
        "agent_run_id": agent_run_id or None,
    }


def create_ui_backfill_approval_request(
    alert: dict[str, Any],
    action: Any,
    requested_by: str,
    agent_run_id: str = "",
    api_base_url: str = CONTROL_PLANE_API_URL,
    approval_token: str = CONTROL_PLANE_APPROVAL_TOKEN,
) -> dict[str, Any]:
    """
    Create or reuse a durable backfill approval request through FastAPI.

    Args:
        alert: Selected source alert row.
        action: Triage-recommended approval action.
        requested_by: Human identity creating the request.
        agent_run_id: Optional source triage run UUID.
        api_base_url: Control-plane API URL.
        approval_token: Approval mutation token.

    Returns:
        Latest durable approval request state.

    Raises:
        ValueError: If the control-plane API is not configured.
        ControlPlaneClientError: If the API rejects or cannot persist the request.
    """
    if not api_base_url.strip():
        raise ValueError("CONTROL_PLANE_API_URL is required for durable approval requests.")

    api_client = ControlPlaneClient(
        base_url=api_base_url,
        timeout_seconds=COPILOT_API_TIMEOUT,
        approval_token=approval_token,
    )
    request_payload = build_ui_backfill_approval_payload(
        alert=alert,
        action=action,
        requested_by=requested_by,
        agent_run_id=agent_run_id,
    )

    logger.info(
        "Creating Streamlit approval request | alert_ref=%s target=%s dates=%s..%s",
        alert.get("alert_display_id"),
        request_payload["target_dag_id"],
        request_payload["start_date"],
        request_payload["end_date"],
    )

    return api_client.create_approval_request(**request_payload)


def decide_ui_approval_request(
    request_id: str,
    decision: str,
    decided_by: str,
    comment: str = "",
    api_base_url: str = CONTROL_PLANE_API_URL,
    approval_token: str = CONTROL_PLANE_APPROVAL_TOKEN,
) -> dict[str, Any]:
    """
    Apply a durable approve or reject decision without executing remediation.

    Args:
        request_id: Human-facing APR request identifier.
        decision: Approve or reject.
        decided_by: Human identity making the decision.
        comment: Optional decision rationale.
        api_base_url: Control-plane API URL.
        approval_token: Approval mutation token.

    Returns:
        Latest durable approval request state.

    Raises:
        ValueError: If API configuration or decision input is invalid.
        ControlPlaneClientError: If the decision cannot be persisted.
    """
    if not api_base_url.strip():
        raise ValueError("CONTROL_PLANE_API_URL is required for approval decisions.")

    api_client = ControlPlaneClient(
        base_url=api_base_url,
        timeout_seconds=COPILOT_API_TIMEOUT,
        approval_token=approval_token,
    )

    logger.info(
        "Applying Streamlit approval decision | request_id=%s decision=%s decided_by=%s",
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


def load_approval_queue_rows(
    status: str | None = None,
    limit: int = 25,
    api_base_url: str = CONTROL_PLANE_API_URL,
) -> list[dict[str, Any]]:
    """
    Load latest durable approval states through the read-only API boundary.

    Args:
        status: Optional pending, approved, or rejected approval filter.
        limit: Maximum latest-state rows returned.
        api_base_url: Control-plane API URL.

    Returns:
        Latest approval queue rows.

    Raises:
        ValueError: If the control-plane API is not configured.
        ControlPlaneClientError: If the queue response is unavailable or malformed.
    """
    if not api_base_url.strip():
        raise ValueError("CONTROL_PLANE_API_URL is required for Approval Queue visibility.")

    api_client = ControlPlaneClient(
        base_url=api_base_url,
        timeout_seconds=COPILOT_API_TIMEOUT,
    )
    payload = api_client.list_approval_requests(status=status, limit=limit)

    return list(payload["rows"])


@st.cache_data(ttl=30, show_spinner=False)
def load_life_evaluation_rows(
    eval_status: str | None = None,
    lookback_days: int = 30,
    limit: int = 10,
    api_base_url: str = CONTROL_PLANE_API_URL,
) -> list[dict[str, Any]]:
    """
    Load recent LIFE evaluation summaries through the read-only API boundary.

    Args:
        eval_status: Optional pass, review, or fail filter.
        lookback_days: Mandatory recent audit window in days.
        limit: Maximum evaluation summaries returned.
        api_base_url: Control-plane API URL.

    Returns:
        Sanitized LIFE evaluation summary rows.

    Raises:
        ValueError: If the control-plane API is not configured.
        ControlPlaneClientError: If the history response is unavailable or malformed.
    """
    if not api_base_url.strip():
        raise ValueError("CONTROL_PLANE_API_URL is required for LIFE evaluation visibility.")

    api_client = ControlPlaneClient(
        base_url=api_base_url,
        timeout_seconds=COPILOT_API_TIMEOUT,
    )
    payload = api_client.list_life_evaluations(
        eval_status=eval_status,
        lookback_days=lookback_days,
        limit=limit,
    )
    rows = list(payload["rows"])

    logger.info(
        "Loaded LIFE evaluation rows for Streamlit | rows=%d lookback_days=%d",
        len(rows),
        lookback_days,
    )

    return rows


# --- Defining Incident History Helpers
@st.cache_data(ttl=30, show_spinner=False)
def load_incident_history_rows(
    alert_reference: str,
    lookback_days: int = 90,
    limit: int = 10,
    api_base_url: str = CONTROL_PLANE_API_URL,
) -> list[dict[str, Any]]:
    """
    Load bounded previous investigations through the shared read-only API.

    Args:
        alert_reference: Human Alert Ref, system alert key, or alert UUID.
        lookback_days: Mandatory recent history window.
        limit: Maximum previous investigations returned.
        api_base_url: Control-plane API URL.

    Returns:
        Sanitized incident-history rows ordered newest first.

    Raises:
        ValueError: If API configuration or alert identity is missing.
        ControlPlaneClientError: If transport or response validation fails.
    """
    normalized_reference = alert_reference.strip()

    if not normalized_reference:
        raise ValueError("An Alert Ref is required for incident history.")

    if not api_base_url.strip():
        raise ValueError("CONTROL_PLANE_API_URL is required for incident history.")

    api_client = ControlPlaneClient(
        base_url=api_base_url,
        timeout_seconds=COPILOT_API_TIMEOUT,
    )
    payload = api_client.get_incident_history(
        alert_reference=normalized_reference,
        lookback_days=lookback_days,
        limit=limit,
    )
    rows = list(payload["rows"])

    logger.info(
        "Loaded incident history for Streamlit | alert_reference=%s rows=%d lookback_days=%d",
        normalized_reference,
        len(rows),
        lookback_days,
    )

    return rows


def summarize_incident_history_rows(rows: list[dict[str, Any]]) -> dict[str, int]:
    """
    Build operator metrics for previous durable investigations.

    Args:
        rows: Sanitized incident-history rows.

    Returns:
        Counts for total, successful, approval-gated, reports, and evidence pointers.
    """
    return {
        "total": len(rows),
        "successful": sum(row.get("outcome_status") == "success" for row in rows),
        "approval_required": sum(
            row.get("requires_human_approval") is True
            or row.get("approval_state") == "pending"
            for row in rows
        ),
        "reports": sum(bool(row.get("report_s3_uri")) for row in rows),
        "evidence_references": sum(
            int(row.get("evidence_reference_count") or 0)
            for row in rows
        ),
    }


def build_incident_history_display_rows(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Convert internal history contracts into human-readable table rows.

    Args:
        rows: Sanitized incident-history rows from the shared API.

    Returns:
        Operator-facing rows without parent UUIDs or long system alert keys.
    """
    display_rows: list[dict[str, Any]] = []

    for row in rows:
        category = str(row.get("top_hypothesis_category") or "")
        display_rows.append(
            {
                "Investigated At": row.get("recorded_at"),
                "Alert Ref": row.get("alert_display_id"),
                "Report Ref": row.get("report_id") or "Not assigned",
                "Status": str(row.get("outcome_status") or "unknown").replace("_", " ").title(),
                "Likely Cause": category.replace("_", " ").title() if category else "Not classified",
                "Confidence": row.get("confidence"),
                "Evidence": int(row.get("evidence_reference_count") or 0),
                "Approval": str(row.get("approval_state") or "not_required")
                .replace("_", " ")
                .title(),
                "Report": row.get("report_s3_uri"),
            }
        )

    return display_rows


# --- Defining Checkpoint Recovery Helpers
def request_ui_checkpoint_history(
    *,
    checkpoint_namespace: str,
    alert_id: str = "",
    alert_key: str = "",
    history_limit: int = 50,
    history_next_node: str = "store_report",
    api_base_url: str = CONTROL_PLANE_API_URL,
    api_timeout: float = COPILOT_API_TIMEOUT,
) -> dict[str, Any]:
    """
    Request sanitized checkpoint history through the shared control-plane API.

    Args:
        checkpoint_namespace: Existing source Airflow triage namespace.
        alert_id: Optional source alert UUID.
        alert_key: Optional stable source alert key.
        history_limit: Maximum newest-first checkpoints returned.
        history_next_node: Pending node used to select the replay candidate.
        api_base_url: Control-plane API URL.
        api_timeout: HTTP timeout in seconds.

    Returns:
        Validated read-only checkpoint history.

    Raises:
        ValueError: If the API boundary is not configured.
        ControlPlaneClientError: If transport or response validation fails.
    """
    if not api_base_url.strip():
        raise ValueError("CONTROL_PLANE_API_URL is required for checkpoint history.")

    api_client = ControlPlaneClient(
        base_url=api_base_url,
        timeout_seconds=api_timeout,
    )
    payload = api_client.get_checkpoint_history(
        checkpoint_namespace=checkpoint_namespace,
        alert_id=alert_id,
        alert_key=alert_key,
        history_limit=history_limit,
        history_next_node=history_next_node,
    )

    logger.info(
        "Loaded checkpoint history for Streamlit | namespace=%s history_count=%d matching=%d",
        checkpoint_namespace,
        int(payload["history_count"]),
        int(payload["matching_checkpoint_count"]),
    )

    return payload


def request_ui_checkpoint_replay_preview(
    *,
    checkpoint_namespace: str,
    checkpoint_id: str,
    alert_id: str = "",
    alert_key: str = "",
    replay_request_id: str = "",
    history_limit: int = 50,
    history_next_node: str = "store_report",
    api_base_url: str = CONTROL_PLANE_API_URL,
    api_timeout: float = COPILOT_API_TIMEOUT,
) -> dict[str, Any]:
    """
    Build a non-executing Airflow replay preview through the shared API.

    Args:
        checkpoint_namespace: Existing source Airflow triage namespace.
        checkpoint_id: Exact replay candidate selected from current history.
        alert_id: Optional source alert UUID.
        alert_key: Optional stable source alert key.
        replay_request_id: Optional explicit idempotency key.
        history_limit: Maximum history rows re-read by the API.
        history_next_node: Required pending node for replay.
        api_base_url: Control-plane API URL.
        api_timeout: HTTP timeout in seconds.

    Returns:
        Validated replay preview that has not triggered Airflow.

    Raises:
        ValueError: If the API boundary is not configured.
        ControlPlaneClientError: If transport or response validation fails.
    """
    if not api_base_url.strip():
        raise ValueError("CONTROL_PLANE_API_URL is required for checkpoint replay preview.")

    api_client = ControlPlaneClient(
        base_url=api_base_url,
        timeout_seconds=api_timeout,
    )
    payload = api_client.preview_checkpoint_replay(
        checkpoint_namespace=checkpoint_namespace,
        checkpoint_id=checkpoint_id,
        alert_id=alert_id,
        alert_key=alert_key,
        replay_request_id=replay_request_id,
        history_limit=history_limit,
        history_next_node=history_next_node,
    )

    logger.info(
        "Built Streamlit checkpoint replay preview | namespace=%s checkpoint_id=%s replay_request_id=%s",
        checkpoint_namespace,
        checkpoint_id,
        payload["replay_request_id"],
    )

    return payload


def build_checkpoint_history_display_rows(
    history: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Convert sanitized checkpoint metadata into concise operator table rows.

    Args:
        history: Newest-first checkpoint snapshot dictionaries.

    Returns:
        Human-readable checkpoint rows without raw graph state.
    """
    return [
        {
            "Created At": row.get("created_at"),
            "Step": row.get("step"),
            "Source": str(row.get("source") or "unknown").replace("_", " ").title(),
            "Pending Nodes": ", ".join(row.get("next_nodes") or []) or "Complete",
            "Complete": bool(row.get("is_complete")),
            "Checkpoint ID": row.get("checkpoint_id"),
        }
        for row in history
    ]


# --- Defining Lineage Impact Helpers
def request_ui_dbt_blast_radius(
    table_name: str,
    manifest_s3_uri: str = DEFAULT_MANIFEST_S3_URI,
    max_depth: int = DEFAULT_BLAST_RADIUS_DEPTH,
    max_nodes: int = DEFAULT_BLAST_RADIUS_NODES,
    api_base_url: str = CONTROL_PLANE_API_URL,
    api_timeout: float = COPILOT_API_TIMEOUT,
) -> dict[str, Any]:
    """
    Request deterministic downstream impact through the shared control-plane API.

    Args:
        table_name: Fully qualified warehouse table selected by the operator.
        manifest_s3_uri: S3 URI for the dbt manifest artifact.
        max_depth: Maximum downstream lineage depth.
        max_nodes: Maximum downstream nodes returned, excluding the root.
        api_base_url: Control-plane API URL.
        api_timeout: HTTP timeout in seconds.

    Returns:
        Validated blast-radius response with transport metadata.

    Raises:
        ValueError: If the table or API URL is missing.
        ControlPlaneClientError: If transport or response validation fails.
    """
    normalized_table = table_name.strip()
    normalized_api   = api_base_url.strip().rstrip("/")

    if not normalized_table:
        raise ValueError("A selected alert must include a table before impact analysis can run.")

    if not normalized_api:
        raise ValueError("CONTROL_PLANE_API_URL is required for lineage impact analysis.")

    logger.info(
        "Requesting Streamlit dbt blast radius | table=%s max_depth=%d max_nodes=%d",
        normalized_table,
        max_depth,
        max_nodes,
    )

    api_client = ControlPlaneClient(
        base_url=normalized_api,
        timeout_seconds=api_timeout,
    )
    payload = api_client.get_dbt_blast_radius(
        table_name=normalized_table,
        manifest_s3_uri=manifest_s3_uri,
        max_depth=max_depth,
        max_nodes=max_nodes,
    )

    return {
        **payload,
        "transport": "api",
    }


def matching_blast_radius_result(
    table_name: str,
    latest_result: Any,
) -> dict[str, Any] | None:
    """
    Return session blast-radius state only when it belongs to the selected table.

    Args:
        table_name: Current selected alert table.
        latest_result: Arbitrary Streamlit session-state value.

    Returns:
        Matching blast-radius dictionary or None when the state is stale or malformed.
    """
    if not isinstance(latest_result, dict):
        return None

    if str(latest_result.get("table_name") or "") != table_name.strip():
        return None

    return latest_result


def build_blast_radius_display_rows(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Convert dbt impact nodes into concise operator-facing table rows.

    Args:
        nodes: Sanitized impacted assets, tests, or unresolved manifest nodes.

    Returns:
        Display rows containing depth, asset, resource type, relation, and readable path.
    """
    display_rows: list[dict[str, Any]] = []

    for node in nodes:
        raw_path      = node.get("lineage_path") or []
        readable_path = " -> ".join(
            str(unique_id).rsplit(".", 1)[-1]
            for unique_id in raw_path
        )
        asset_name = str(node.get("alias") or node.get("name") or node.get("unique_id") or "unknown")

        display_rows.append(
            {
                "Depth": int(node.get("depth") or 0),
                "Asset": asset_name,
                "Type": str(node.get("resource_type") or "unknown"),
                "Relation": str(node.get("relation_name") or "not materialized"),
                "Lineage Path": readable_path,
            }
        )

    return display_rows


def summarize_approval_queue_rows(rows: list[dict[str, Any]]) -> dict[str, int]:
    """
    Summarize approval and execution states for operator metrics.

    Args:
        rows: Latest approval request states.

    Returns:
        Counts for pending decisions, approved requests, active executions, and failures.
    """
    active_execution_states = {"dispatching", "dispatched"}

    return {
        "pending": sum(str(row.get("status") or "") == "pending" for row in rows),
        "approved": sum(str(row.get("status") or "") == "approved" for row in rows),
        "active_executions": sum(
            str(row.get("execution_status") or "") in active_execution_states
            for row in rows
        ),
        "failed_executions": sum(
            str(row.get("execution_status") or "") == "failed"
            for row in rows
        ),
    }


def summarize_life_evaluation_rows(rows: list[dict[str, Any]]) -> dict[str, int]:
    """
    Summarize LIFE evaluation states for operator-facing metrics.

    Args:
        rows: Sanitized LIFE evaluation history rows.

    Returns:
        Counts for pass, review, fail, malformed payloads, and gated proposals.
    """
    return {
        "total": len(rows),
        "pass": sum(str(row.get("eval_status") or "") == "pass" for row in rows),
        "review": sum(str(row.get("eval_status") or "") == "review" for row in rows),
        "fail": sum(str(row.get("eval_status") or "") == "fail" for row in rows),
        "malformed": sum(row.get("payload_valid") is False for row in rows),
        "approval_required": sum(row.get("requires_human_approval") is True for row in rows),
    }

# --- Defining Render Helpers
def render_sidebar_filters() -> dict[str, Any]:
    """
    Render sidebar alert filters and triage settings.

    Returns:
        Dictionary of selected UI filter/settings values.
    """
    with st.sidebar:
        st.header("Alert Filters")

        status = st.selectbox(
            "Alert status",
            options=["open", "acknowledged", "triaged", "resolved"],
            index=0,
        )
        selected_dt       = st.date_input("Daily summary date", value=date.today())
        dt_filter_enabled = st.checkbox("Restrict alert list to summary date", value=False)
        limit             = st.slider("Alert limit", min_value=5, max_value=100, value=DEFAULT_ALERT_LIMIT, step=5)

        st.caption(
            "The date always controls the daily DQ snapshot. The checkbox only restricts the alert list."
        )

        st.divider()
        st.header("Triage Settings")

        confidence_threshold = st.slider(
            "Confidence threshold",
            min_value=0.10,
            max_value=0.95,
            value=float(DEFAULT_CONFIDENCE_TARGET),
            step=0.05,
        )
        max_evidence_iterations = st.slider(
            "Extra evidence loops",
            min_value=0,
            max_value=5,
            value=int(DEFAULT_MAX_EVIDENCE_LOOP),
            step=1,
        )
        manifest_s3_uri = st.text_input("dbt manifest S3 URI", value=DEFAULT_MANIFEST_S3_URI)

        refresh_clicked = st.button("Refresh alerts", use_container_width=True)

    if refresh_clicked:
        load_alert_rows.clear()
        load_daily_quality_summary.clear()
        load_audit_rows.clear()
        load_life_evaluation_rows.clear()

    return {
        "status": status,
        "dt": selected_dt.isoformat() if dt_filter_enabled else None,
        "summary_dt": selected_dt.isoformat(),
        "limit": limit,
        "confidence_threshold": confidence_threshold,
        "max_evidence_iterations": max_evidence_iterations,
        "manifest_s3_uri": manifest_s3_uri,
    }


def render_alert_metrics(alerts: list[dict[str, Any]]) -> None:
    """
    Render compact alert summary metrics.

    Args:
        alerts: Alert rows currently loaded in the UI.

    Returns:
        None.
    """
    summary = summarize_alert_rows(alerts)

    col_total, col_open, col_critical, col_warning, col_tables = st.columns(5)

    col_total.metric("Loaded Alerts", summary["total_alerts"])
    col_open.metric("Open", summary["open_count"])
    col_critical.metric("Critical", summary["critical_count"])
    col_warning.metric("Warning", summary["warning_count"])
    col_tables.metric("Affected Tables", summary["affected_table_count"])


def render_daily_quality_metrics(
    daily_summary: dict[str, Any],
    transport: str,
) -> None:
    """
    Render deterministic daily check and open-alert totals.

    Args:
        daily_summary: Validated daily quality summary payload.
        transport: API or local fallback transport label.

    Returns:
        None.
    """
    summary = summarize_daily_quality_payload(daily_summary)

    st.caption(
        f"Daily DQ snapshot | dt={daily_summary.get('dt')} | source={transport}"
    )

    col_checks, col_passed, col_warning, col_failed, col_alerts = st.columns(5)

    col_checks.metric("Daily Checks", summary["total_checks"])
    col_passed.metric("Passed", summary["passed_checks"])
    col_warning.metric("Warnings", summary["warning_checks"])
    col_failed.metric("Failed", summary["failed_checks"])
    col_alerts.metric("Open Alerts", summary["total_open_alerts"])


def render_reliability_overview(
    alerts: list[dict[str, Any]],
    daily_summary: dict[str, Any] | None = None,
    daily_summary_transport: str = "",
    daily_summary_error: str | None = None,
) -> None:
    """
    Render daily DQ health plus the currently loaded alert-filter context.

    Args:
        alerts: Alert rows currently loaded in the UI.
        daily_summary: Optional deterministic daily DQ snapshot.
        daily_summary_transport: API or local fallback transport label.
        daily_summary_error: Optional snapshot loading failure.

    Returns:
        None.
    """
    st.subheader("Reliability Overview")

    summary = summarize_alert_rows(alerts)
    state   = classify_reliability_state(
        summary=summary,
        daily_summary=daily_summary,
        summary_error=daily_summary_error,
    )

    dates_text  = ", ".join(summary["affected_dates"][:5]) if summary["affected_dates"] else "No affected dt loaded"
    tables_text = ", ".join(summary["affected_tables"][:5]) if summary["affected_tables"] else "No affected table loaded"
    dates_html  = escape_html_text(dates_text)
    tables_html = escape_html_text(tables_text)

    st.markdown(
        f"""
        <div class="dq-health {state["css_class"]}">
            <b>{state["label"]}</b><br>
            <span class="dq-muted">{state["message"]}</span>
            <div class="dq-separator"></div>
            <span class="dq-muted">Affected dates</span><br>
            <code>{dates_html}</code><br><br>
            <span class="dq-muted">Affected tables</span><br>
            <code>{tables_html}</code>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if daily_summary:
        render_daily_quality_metrics(
            daily_summary=daily_summary,
            transport=daily_summary_transport,
        )

    else:
        st.warning(
            "Daily DQ snapshot unavailable. Alert operations remain visible, but this page is not claiming a healthy state. "
            f"Detail: {daily_summary_error or 'No summary payload was returned.'}"
        )

    st.caption("Loaded alert filter context")
    render_alert_metrics(alerts)

    col_reports, col_status, col_next = st.columns([1, 1, 2])

    col_reports.metric("Reports Linked", summary["report_count"])
    col_status.metric("Lifecycle States", len(summary["status_counts"]))

    with col_next:
        st.caption(
            "Testing flow: run Airflow daily orchestrator, inspect alerts here, run triage, "
            "review the S3 report, then create and explicitly decide a durable approval request."
        )


def render_approval_queue_panel() -> None:
    """
    Render read-only approval and execution lifecycle visibility for operators.

    Returns:
        None.
    """
    st.subheader("Approval Queue")
    st.caption(
        "Approval decisions and execution are separate. This panel is read-only; "
        "DAG 90 remains the controlled execution boundary."
    )

    try:
        rows = load_approval_queue_rows(limit=25)

    except Exception as exc:
        st.info(f"Approval Queue is unavailable: {type(exc).__name__}: {exc}")
        return

    if not rows:
        st.caption("No durable approval request has been created yet.")
        return

    summary = summarize_approval_queue_rows(rows)
    col_pending, col_approved, col_active, col_failed = st.columns(4)

    col_pending.metric("Pending Decisions", summary["pending"])
    col_approved.metric("Approved", summary["approved"])
    col_active.metric("Active Execution", summary["active_executions"])
    col_failed.metric("Execution Failed", summary["failed_executions"])

    display_rows = [
        {
            "request_id": row.get("request_id"),
            "approval_status": row.get("status"),
            "execution_status": row.get("execution_status"),
            "target_dag_id": row.get("target_dag_id"),
            "start_date": row.get("start_date"),
            "end_date": row.get("end_date"),
            "requested_by": row.get("requested_by"),
            "decided_by": row.get("decided_by"),
            "execution_dag_run_id": row.get("execution_dag_run_id"),
            "execution_error": row.get("execution_error"),
            "updated_at": row.get("updated_at"),
        }
        for row in rows
    ]

    st.dataframe(
        dataframe_from_rows(display_rows),
        use_container_width=True,
        hide_index=True,
    )


def render_life_evaluation_history_panel() -> None:
    """
    Render recent agent reliability evaluation history for operators.

    Returns:
        None.
    """
    st.subheader("Agent Reliability Evaluations")
    st.caption(
        "Recent LIFE evaluations compare persisted triage reports with deterministic incident ground truth. "
        "They propose reviewable improvements but never change prompts, rules, data, or DAGs automatically."
    )

    try:
        rows = load_life_evaluation_rows(lookback_days=30, limit=10)

    except Exception as exc:
        logger.warning("LIFE evaluation history is unavailable | error=%s", exc)
        st.info(f"Agent reliability history is unavailable: {type(exc).__name__}: {exc}")
        return

    if not rows:
        st.caption("No LIFE evaluation has been recorded in the last 30 days.")
        return

    summary = summarize_life_evaluation_rows(rows)
    col_total, col_pass, col_review, col_fail, col_approval = st.columns(5)

    col_total.metric("Evaluations", summary["total"])
    col_pass.metric("Passed", summary["pass"])
    col_review.metric("Needs Review", summary["review"])
    col_fail.metric("Failed", summary["fail"])
    col_approval.metric("Human Review", summary["approval_required"])

    if summary["malformed"]:
        st.warning(
            f"{summary['malformed']} audit payload(s) could not be parsed. "
            "The raw payload remains hidden; inspect Airflow and ClickHouse logs using the audit ID."
        )

    display_columns = [
        "evaluated_at",
        "run_id",
        "scenario_id",
        "eval_status",
        "failure_category",
        "summary",
        "suggested_change_summary",
        "requires_human_approval",
        "json_report_s3_uri",
    ]
    display_rows = [
        {column: row.get(column) for column in display_columns}
        for row in rows
    ]

    st.dataframe(
        dataframe_from_rows(display_rows),
        use_container_width=True,
        hide_index=True,
    )


def render_incident_history_panel(alert: dict[str, Any]) -> None:
    """
    Render previous evidence-backed investigations for the selected alert.

    Args:
        alert: Currently selected alert row.

    Returns:
        None.
    """
    alert_ref = str(
        alert.get("alert_display_id")
        or alert.get("alert_key")
        or alert.get("alert_id")
        or ""
    ).strip()

    st.subheader("Previous Investigations")
    st.caption(
        "Durable investigation outcomes for this exact alert. The panel shows bounded facts and "
        "evidence references, not hidden prompts, raw SQL, or unrestricted conversation history."
    )

    if not alert_ref:
        st.info("This alert has no stable reference for history lookup.")
        return

    try:
        rows = load_incident_history_rows(
            alert_reference=alert_ref,
            lookback_days=90,
            limit=10,
        )

    except Exception as exc:
        logger.warning(
            "Incident history is unavailable | alert_reference=%s error=%s",
            alert_ref,
            exc,
        )
        st.info(f"Previous investigations are unavailable: {type(exc).__name__}: {exc}")
        return

    if not rows:
        st.caption(
            "No previous investigation was found in the last 90 days. Run triage to create the first evidence-backed record."
        )
        return

    summary = summarize_incident_history_rows(rows)
    latest  = rows[0]
    col_runs, col_success, col_evidence, col_reports, col_approval = st.columns(5)

    col_runs.metric("Investigations", summary["total"])
    col_success.metric("Completed", summary["successful"])
    col_evidence.metric("Evidence Refs", summary["evidence_references"])
    col_reports.metric("Reports", summary["reports"])
    col_approval.metric("Needs Approval", summary["approval_required"])

    st.markdown("#### Latest Readout")
    st.write(str(latest.get("summary") or "No investigation summary is available."))

    if latest.get("top_hypothesis_category"):
        category = str(latest["top_hypothesis_category"]).replace("_", " ").title()
        confidence = latest.get("confidence")
        confidence_text = (
            f"{float(confidence):.0%} confidence"
            if isinstance(confidence, (int, float)) and not isinstance(confidence, bool)
            else "confidence not recorded"
        )
        st.caption(f"Likely cause: {category} | {confidence_text}")

    st.dataframe(
        dataframe_from_rows(build_incident_history_display_rows(rows)),
        use_container_width=True,
        hide_index=True,
    )

    with st.expander("Technical references", expanded=False):
        st.json(
            {
                "memory_id": latest.get("memory_id"),
                "parent_run_id": latest.get("parent_run_id"),
                "system_alert_key": latest.get("alert_key"),
                "resolution_reference": latest.get("resolution_reference"),
            }
        )


def render_checkpoint_recovery_panel(alert: dict[str, Any]) -> None:
    """
    Render read-only checkpoint history and a non-executing replay preview.

    Args:
        alert: Currently selected source-of-truth alert row.

    Returns:
        None.
    """
    alert_id          = str(alert.get("alert_id") or "").strip()
    alert_key         = str(alert.get("alert_key") or "").strip()
    alert_display_id  = str(alert.get("alert_display_id") or alert_id or "checkpoint")
    identity_options: dict[str, tuple[str, str]] = {}

    if alert_id:
        identity_options["Alert ID"] = (alert_id, "alert_id")

    if alert_key:
        identity_options["System alert key"] = (alert_key, "alert_key")

    st.subheader("Checkpoint Recovery")
    st.caption(
        "Inspect sanitized LangGraph history and prepare an Airflow replay preview. "
        "This panel never reads raw graph state and never triggers DAG 40."
    )

    if not identity_options:
        st.info("This alert has no stable identity for checkpoint lookup.")
        return

    with st.expander("Inspect source triage checkpoints", expanded=False):
        identity_label = st.selectbox(
            "Identity used by the source DAG 40 run",
            options=list(identity_options),
            help=(
                "Choose the same identity passed to the original Airflow triage run. "
                "Using another identity produces a different checkpoint thread."
            ),
            key=f"checkpoint_identity_{alert_display_id}",
        )
        identity_value, identity_field = identity_options[identity_label]
        checkpoint_namespace = st.text_input(
            "Checkpoint namespace",
            placeholder="manual__triage_triage_YYYYMMDDTHHMMSSffffff",
            help="Use the checkpoint namespace printed in the source DAG 40 task log.",
            key=f"checkpoint_namespace_{alert_display_id}",
        ).strip()
        history_state_key = f"checkpoint_history_result_{alert_display_id}"
        preview_state_key = f"checkpoint_replay_preview_{alert_display_id}"

        if st.button(
            "Inspect checkpoint history",
            type="secondary",
            disabled=not checkpoint_namespace,
            key=f"inspect_checkpoint_{alert_display_id}",
        ):
            try:
                request_kwargs = {
                    "checkpoint_namespace": checkpoint_namespace,
                    "alert_id": identity_value if identity_field == "alert_id" else "",
                    "alert_key": identity_value if identity_field == "alert_key" else "",
                }
                history_payload = request_ui_checkpoint_history(**request_kwargs)
                st.session_state[history_state_key] = {
                    "checkpoint_namespace": checkpoint_namespace,
                    "alert_reference": identity_value,
                    "identity_field": identity_field,
                    "payload": history_payload,
                }
                st.session_state.pop(preview_state_key, None)

            except Exception as exc:
                logger.warning(
                    "Checkpoint history is unavailable | alert_ref=%s namespace=%s error=%s",
                    alert_display_id,
                    checkpoint_namespace,
                    exc,
                )
                st.error(f"Checkpoint history is unavailable: {type(exc).__name__}: {exc}")

        stored_history = st.session_state.get(history_state_key)

        if not isinstance(stored_history, dict):
            st.caption("Enter the source Airflow namespace, then inspect its sanitized history.")
            return

        if (
            stored_history.get("checkpoint_namespace") != checkpoint_namespace
            or stored_history.get("alert_reference") != identity_value
            or stored_history.get("identity_field") != identity_field
        ):
            st.info("Namespace or identity changed. Inspect again before preparing a replay preview.")
            return

        history_payload = stored_history["payload"]
        selected        = history_payload["selected_checkpoint"]
        col_history, col_matches, col_step, col_state = st.columns(4)

        col_history.metric("Checkpoints", int(history_payload["history_count"]))
        col_matches.metric("Replay Candidates", int(history_payload["matching_checkpoint_count"]))
        col_step.metric("Selected Step", int(selected["step"]))
        col_state.metric("Pending Node", ", ".join(selected["next_nodes"]))

        st.dataframe(
            dataframe_from_rows(build_checkpoint_history_display_rows(history_payload["history"])),
            use_container_width=True,
            hide_index=True,
        )
        st.info(
            "History inspection is read-only. The selected checkpoint only becomes executable "
            "after an operator explicitly triggers Airflow DAG 40."
        )

        replay_request_id = st.text_input(
            "Replay request ID (optional)",
            help=(
                "Leave blank to generate a stable idempotency key from the alert, namespace, "
                "and checkpoint. Reusing the key reuses the same replay child thread."
            ),
            key=f"checkpoint_replay_request_{alert_display_id}",
        ).strip()

        if st.button(
            "Build Airflow replay preview",
            type="primary",
            key=f"preview_checkpoint_replay_{alert_display_id}",
        ):
            try:
                preview_kwargs = {
                    "checkpoint_namespace": checkpoint_namespace,
                    "checkpoint_id": str(selected["checkpoint_id"]),
                    "alert_id": identity_value if identity_field == "alert_id" else "",
                    "alert_key": identity_value if identity_field == "alert_key" else "",
                    "replay_request_id": replay_request_id,
                }
                st.session_state[preview_state_key] = request_ui_checkpoint_replay_preview(
                    **preview_kwargs
                )

            except Exception as exc:
                logger.warning(
                    "Checkpoint replay preview failed | alert_ref=%s namespace=%s error=%s",
                    alert_display_id,
                    checkpoint_namespace,
                    exc,
                )
                st.error(f"Replay preview failed: {type(exc).__name__}: {exc}")

        replay_preview = st.session_state.get(preview_state_key)

        if isinstance(replay_preview, dict):
            st.success("Replay preview validated. No Airflow DagRun was triggered.")
            st.write(replay_preview["summary"])

            with st.expander("Airflow DAG 40 replay configuration", expanded=True):
                st.json(replay_preview["dag_run_conf"])

            with st.expander("Technical replay references", expanded=False):
                st.json(
                    {
                        "dag_id": replay_preview["dag_id"],
                        "source_thread_id": replay_preview["source_thread_id"],
                        "source_checkpoint_id": replay_preview["source_checkpoint_id"],
                        "replay_request_id": replay_preview["replay_request_id"],
                        "replay_thread_id": replay_preview["replay_thread_id"],
                        "airflow_triggered": replay_preview["airflow_triggered"],
                        "side_effects_executed": replay_preview["side_effects_executed"],
                    }
                )


def render_alert_table(alerts: list[dict[str, Any]]) -> None:
    """
    Render the alert table.

    Args:
        alerts: Alert rows currently loaded in the UI.

    Returns:
        None.
    """
    df = dataframe_from_rows(alerts)

    if df.empty:
        st.info("No alerts found for the selected filters.")
        return

    display_columns = [
        "created_at",
        "alert_display_id",
        "status",
        "severity",
        "dt",
        "table_name",
        "metric",
        "dimension",
        "observed_value",
        "expected_value",
        "threshold_value",
        "alert_key",
    ]
    safe_columns = [column for column in display_columns if column in df.columns]

    st.dataframe(
        df[safe_columns],
        use_container_width=True,
        hide_index=True,
    )


def render_selected_alert(alert: dict[str, Any]) -> None:
    """
    Render selected alert details.

    Args:
        alert: Selected alert row.

    Returns:
        None.
    """
    st.subheader("Selected Alert")

    severity_class = severity_css_class(alert.get("severity"))
    severity_text  = escape_html_text(str(alert.get("severity") or "unknown").upper())
    table_name     = escape_html_text(alert.get("table_name") or "Unknown table")
    metric_name    = escape_html_text(alert.get("metric") or "Unknown metric")
    alert_ref      = escape_html_text(alert.get("alert_display_id") or "Not assigned")
    system_key     = escape_html_text(alert.get("alert_key") or "Not available")

    col_left, col_right = st.columns([1.2, 1])

    with col_left:
        st.markdown(
            f"""
            <div class="dq-card {severity_class}">
                <b>{severity_text}</b><br>
                <span class="dq-muted">{table_name} / {metric_name}</span>
                <div class="dq-separator"></div>
                <span class="dq-muted">Alert Ref</span><br>
                <code>{alert_ref}</code><br><br>
                <span class="dq-muted">System Alert Key</span><br>
                <code>{system_key}</code>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with col_right:
        st.json(
            {
                "alert_id": str(alert.get("alert_id") or ""),
                "dt": str(alert.get("dt") or ""),
                "status": alert.get("status"),
                "observed_value": alert.get("observed_value"),
                "expected_value": alert.get("expected_value"),
                "threshold_value": alert.get("threshold_value"),
            }
        )

    details = parse_json_object(alert.get("details_json") or alert.get("details"))

    with st.expander("Alert details JSON", expanded=False):
        st.json(details)

    evidence_uri = str(details.get("evidence_s3_uri") or "")

    if evidence_uri:
        with st.expander("DQ failure evidence artifact", expanded=False):
            st.code(evidence_uri)

            if st.button("Read evidence artifact", use_container_width=True):
                try:
                    st.json(json.loads(read_s3_text(evidence_uri)))

                except Exception as exc:
                    st.error(f"Failed to read evidence artifact: {exc}")


def render_lineage_impact_panel(alert: dict[str, Any]) -> None:
    """
    Render deterministic dbt lineage and downstream blast-radius evidence.

    Args:
        alert: Selected alert row containing the affected warehouse table.

    Returns:
        None.
    """
    table_name = str(alert.get("table_name") or "").strip()
    alert_ref  = str(alert.get("alert_display_id") or table_name or "selected-alert")

    st.subheader("Lineage & Blast Radius")
    st.caption(
        "Trace downstream dbt assets from the selected table using a bounded, cycle-safe manifest traversal. "
        "This analysis is read-only and never exposes compiled SQL."
    )

    if not table_name:
        st.info("The selected alert does not identify a warehouse table, so downstream impact cannot be resolved.")
        return

    col_table, col_bounds = st.columns([1.4, 1])
    col_table.markdown(f"**Affected table**  \n`{table_name}`")
    col_bounds.markdown(
        f"**Safety bounds**  \nDepth `{DEFAULT_BLAST_RADIUS_DEPTH}` | Nodes `{DEFAULT_BLAST_RADIUS_NODES}`"
    )

    if st.button(
        "Analyze downstream impact",
        key=f"blast_radius_{alert_ref}",
        use_container_width=True,
    ):
        try:
            with st.spinner("Reading the latest dbt manifest and tracing downstream dependencies..."):
                result = request_ui_dbt_blast_radius(
                    table_name=table_name,
                    manifest_s3_uri=DEFAULT_MANIFEST_S3_URI,
                )

            st.session_state["latest_blast_radius"] = result

        except Exception as exc:
            logger.exception(
                "Streamlit blast-radius request failed | table=%s",
                table_name,
            )
            st.error(f"Downstream impact analysis failed: {exc}")

    result = matching_blast_radius_result(
        table_name=table_name,
        latest_result=st.session_state.get("latest_blast_radius"),
    )

    if result is None:
        st.caption("Run the analysis to see impacted models, tests, and traversal coverage for this table.")
        return

    if not result.get("matched"):
        st.info(str(result.get("summary") or f"No dbt manifest node matched {table_name}."))
        return

    st.markdown(f"**Quick read:** {result.get('summary') or 'Downstream impact resolved.'}")

    col_assets, col_tests, col_depth, col_status = st.columns(4)
    col_assets.metric("Downstream Assets", int(result.get("impacted_asset_count") or 0))
    col_tests.metric("dbt Tests", int(result.get("impacted_test_count") or 0))
    col_depth.metric("Depth Reached", int(result.get("max_depth_reached") or 0))
    col_status.metric("Traversal", "Truncated" if result.get("truncated") else "Complete")

    if result.get("truncated"):
        st.warning(
            "The impact graph reached its safety bound. Increase bounds only through an explicit reviewed change; "
            "do not treat this result as the complete downstream graph."
        )

    impacted_assets = list(result.get("impacted_assets") or [])

    if impacted_assets:
        st.markdown("#### Impacted Data Assets")
        st.dataframe(
            dataframe_from_rows(build_blast_radius_display_rows(impacted_assets)),
            use_container_width=True,
            hide_index=True,
        )

    else:
        st.success("No downstream data assets were found within the configured traversal bounds.")

    impacted_tests  = list(result.get("impacted_tests") or [])
    unresolved_nodes = list(result.get("unresolved_nodes") or [])

    if impacted_tests:
        with st.expander(f"Impacted dbt tests ({len(impacted_tests)})", expanded=False):
            st.dataframe(
                dataframe_from_rows(build_blast_radius_display_rows(impacted_tests)),
                use_container_width=True,
                hide_index=True,
            )

    if unresolved_nodes:
        with st.expander(f"Unresolved manifest references ({len(unresolved_nodes)})", expanded=False):
            st.warning(
                "These dependency identifiers exist in child_map but were not present in the supported manifest collections."
            )
            st.dataframe(
                dataframe_from_rows(build_blast_radius_display_rows(unresolved_nodes)),
                use_container_width=True,
                hide_index=True,
            )

    with st.expander("Technical traversal reference", expanded=False):
        st.json(
            {
                "root_node": result.get("node"),
                "manifest_source": result.get("manifest_source"),
                "resource_type_counts": result.get("resource_type_counts") or {},
                "max_depth": result.get("max_depth"),
                "max_nodes": result.get("max_nodes"),
                "total_impacted_nodes": result.get("total_impacted_nodes"),
                "transport": result.get("transport"),
            }
        )


def render_triage_panel(alert: dict[str, Any], settings: dict[str, Any]) -> None:
    """
    Render triage execution panel and latest report output.

    Args:
        alert: Selected alert row.
        settings: Sidebar triage settings.

    Returns:
        None.
    """
    st.subheader("Agentic Triage")

    alert_key = str(alert.get("alert_key") or "")

    if st.button("Run triage for selected alert", type="primary", use_container_width=True):
        try:
            with st.spinner("Running LangGraph triage and writing report artifacts..."):
                result = run_selected_alert_triage(
                    alert_key=alert_key,
                    confidence_threshold=float(settings["confidence_threshold"]),
                    max_evidence_iterations=int(settings["max_evidence_iterations"]),
                    manifest_s3_uri=str(settings["manifest_s3_uri"] or ""),
                )

            st.session_state["latest_triage_result"] = result
            load_audit_rows.clear()
            transport = str(result["summary"].get("transport") or "local")
            transport_label = (
                "Control Plane API"
                if transport == "api"
                else "bounded local fallback"
            )

            st.success(
                f"Triage completed through {transport_label} and report artifacts were stored."
            )

        except Exception as exc:
            logger.exception("Streamlit triage failed | alert_key=%s", alert_key)
            st.error(f"Triage failed: {exc}")

    latest = matching_triage_result(
        alert_key=alert_key,
        latest_triage_result=st.session_state.get("latest_triage_result"),
    )

    if not latest:
        st.caption("Run triage to generate a Markdown and JSON report for this alert.")
        return

    report  = latest["report"]
    summary = latest["summary"]

    st.json(summary)

    tab_report, tab_evidence, tab_actions = st.tabs(["Report", "Evidence", "Approval Actions"])

    with tab_report:
        render_report_document(
            markdown_text=report.markdown_report,
            container_key="triage_report_document",
            source_label="Generated triage report",
        )

    with tab_evidence:
        evidence_rows = [
            {
                "tool_name": item.tool_name,
                "evidence_type": item.evidence_type,
                "summary": item.summary,
                "row_count": item.row_count,
                "s3_uri": item.s3_uri,
            }
            for item in report.evidence
        ]
        st.dataframe(dataframe_from_rows(evidence_rows), use_container_width=True, hide_index=True)

    with tab_actions:
        if not report.approval_gated_actions:
            st.info("No approval-gated action was recommended by the current report.")
            return

        operator_identity = st.text_input(
            "Approval operator",
            value="streamlit_operator",
            help="Recorded as requested_by or decided_by in the durable approval queue.",
            key=f"approval_operator_{report.report_id}",
        )
        decision_comment = st.text_input(
            "Decision comment",
            value="Reviewed triage evidence and exact backfill scope.",
            key=f"approval_comment_{report.report_id}",
        )

        for action_index, action in enumerate(report.approval_gated_actions):
            payload = action.model_dump(mode="json")
            action_type = (
                action.action_type.value
                if hasattr(action.action_type, "value")
                else str(action.action_type)
            )
            state_key = f"approval_request_{report.report_id}_{action_index}"
            approval  = st.session_state.get(state_key)

            st.markdown(f"#### Proposed `{action_type}` action")
            st.json(payload)
            st.caption(
                "Creating or deciding this request does not trigger Airflow. "
                "Execution remains a separate DAG 90 operator action."
            )

            if st.button(
                "Create durable approval request",
                key=f"create_{state_key}",
                use_container_width=True,
                disabled=bool(approval),
            ):
                try:
                    approval = create_ui_backfill_approval_request(
                        alert=alert,
                        action=action,
                        requested_by=operator_identity,
                        agent_run_id=str(report.agent_run_id),
                    )
                    st.session_state[state_key] = approval
                    st.success(f"Approval request `{approval['request_id']}` is pending.")

                except Exception as exc:
                    st.error(f"Failed to create approval request: {type(exc).__name__}: {exc}")

            if approval:
                st.json(
                    {
                        "request_id": approval.get("request_id"),
                        "status": approval.get("status"),
                        "risk_level": approval.get("risk_level"),
                        "target_dag_id": approval.get("target_dag_id"),
                        "start_date": approval.get("start_date"),
                        "end_date": approval.get("end_date"),
                        "created_new": approval.get("created_new"),
                    }
                )

                if approval.get("status") == "pending":
                    col_approve, col_reject = st.columns(2)

                    with col_approve:
                        if st.button("Approve request", key=f"approve_{state_key}", use_container_width=True):
                            try:
                                approval = decide_ui_approval_request(
                                    request_id=str(approval["request_id"]),
                                    decision="approve",
                                    decided_by=operator_identity,
                                    comment=decision_comment,
                                )
                                st.session_state[state_key] = approval
                                st.success(
                                    f"Request `{approval['request_id']}` approved. No Airflow DAG was triggered."
                                )

                            except Exception as exc:
                                st.error(f"Approval failed: {type(exc).__name__}: {exc}")

                    with col_reject:
                        if st.button("Reject request", key=f"reject_{state_key}", use_container_width=True):
                            try:
                                approval = decide_ui_approval_request(
                                    request_id=str(approval["request_id"]),
                                    decision="reject",
                                    decided_by=operator_identity,
                                    comment=decision_comment,
                                )
                                st.session_state[state_key] = approval
                                st.warning(f"Request `{approval['request_id']}` rejected.")

                            except Exception as exc:
                                st.error(f"Rejection failed: {type(exc).__name__}: {exc}")


def render_audit_panel(alert: dict[str, Any]) -> None:
    """
    Render recent audit log rows for the selected alert.

    Args:
        alert: Selected alert row.

    Returns:
        None.
    """
    alert_key = str(alert.get("alert_key") or "")

    st.subheader("Audit Trail")

    try:
        audit_rows = load_audit_rows(alert_key=alert_key)

    except Exception as exc:
        st.warning(f"Audit log is not available yet: {exc}")
        return

    if not audit_rows:
        st.caption("No audit events found for this alert yet.")
        return

    llm_runtime = build_llm_runtime_summary(audit_rows=audit_rows)

    if llm_runtime:
        st.markdown("#### AI Runtime")

        col_mode, col_provider, col_tokens, col_cost = st.columns(4)

        col_mode.metric("Execution Mode", llm_runtime["mode_label"])
        col_provider.metric("Provider / Model", llm_runtime["provider_model"])
        col_tokens.metric("Token Usage", llm_runtime["token_label"])
        col_cost.metric("Estimated Cost", llm_runtime["estimated_cost_display"])

        st.caption(
            f"Route {llm_runtime['route_label']} | Duration {int(llm_runtime.get('duration_ms') or 0)} ms"
        )

        if llm_runtime.get("fallback_summary"):
            st.info(str(llm_runtime["fallback_summary"]))

    display_rows = [
        {key: value for key, value in row.items() if key != "llm_route"}
        for row in audit_rows
    ]

    st.dataframe(dataframe_from_rows(display_rows), use_container_width=True, hide_index=True)

    report_uri = find_report_uri(alert=alert, audit_rows=audit_rows)

    if report_uri:
        with st.expander("Read latest report artifact", expanded=False):
            st.code(report_uri)

            if st.button("Read report artifact", use_container_width=True):
                try:
                    report_text, transport = read_report_text(report_uri)
                    render_report_document(
                        markdown_text=report_text,
                        container_key="artifact_report_document",
                        source_label=(
                            "Report loaded through the Control Plane API"
                            if transport == "api"
                            else "Report loaded through bounded local S3 fallback"
                        ),
                    )

                except Exception as exc:
                    st.error(f"Failed to read report artifact: {exc}")


def render_copilot_panel(alert: dict[str, Any]) -> None:
    """
    Render the evidence-aware, action-bounded operator Copilot panel.

    Args:
        alert: Currently selected alert row.

    Returns:
        None.
    """
    alert_key = str(alert.get("alert_key") or "")
    alert_ref = str(alert.get("alert_display_id") or "selected alert")

    st.subheader("Evidence-Aware Copilot")
    st.caption(
        "Ask for an explanation, evidence summary, safe recommendation, or approval draft. "
        "The Copilot cannot run SQL mutations, trigger Airflow, or execute remediation."
    )

    guided_label = st.selectbox(
        "Guided task",
        options=list(COPILOT_GUIDED_PROMPTS.keys()),
        key=f"copilot_guided_task_{alert_ref}",
    )
    custom_question = st.text_area(
        "Optional custom question",
        placeholder="For example: Why is this alert important to downstream reporting?",
        key=f"copilot_custom_question_{alert_ref}",
        height=90,
    )
    question = custom_question.strip() or COPILOT_GUIDED_PROMPTS[guided_label]

    if st.button(
        "Ask Copilot",
        key=f"ask_copilot_{alert_ref}",
        use_container_width=True,
    ):
        try:
            with st.spinner("Reading bounded evidence and preparing an operator answer..."):
                try:
                    audit_rows = load_audit_rows(alert_key=alert_key)

                except Exception as audit_exc:
                    logger.warning(
                        "Streamlit Copilot audit context unavailable | alert_key=%s error=%s",
                        alert_key,
                        audit_exc,
                    )
                    audit_rows = []

                result = answer_ui_copilot_question(
                    question=question,
                    alert=alert,
                    latest_triage_result=st.session_state.get("latest_triage_result"),
                    audit_rows=audit_rows,
                )

            st.session_state["latest_copilot_result"] = {
                "alert_key": alert_key,
                "alert_ref": alert_ref,
                **result,
            }
            st.success("Copilot answer generated from the selected alert context.")

        except Exception as exc:
            logger.exception("Streamlit Copilot failed | alert_key=%s", alert_key)
            st.error(f"Copilot could not prepare an answer: {exc}")

    latest = st.session_state.get("latest_copilot_result") or {}

    # Do not display an answer generated for a previously selected alert.
    if str(latest.get("alert_key") or "") != alert_key:
        st.info(
            "Start with a guided task. Run triage first when you need evidence-backed root-cause or backfill guidance."
        )
        return

    st.markdown("#### Direct Answer")
    st.markdown(str(latest.get("answer") or "No answer is available."))

    transport = str(latest.get("transport") or "local")
    st.caption(f"Copilot transport: {transport}")

    if latest.get("fallback_reason"):
        st.warning(
            "The control-plane API was unavailable, so the shared local Copilot service answered this request. "
            "No evidence or action boundary was relaxed."
        )

    st.markdown("#### Alert Context")
    col_ref, col_report, col_evidence, col_history, col_audit = st.columns(5)
    col_ref.metric("Alert Ref", alert_ref)
    col_report.metric("Matching Report", "Yes" if latest.get("has_report") else "No")
    col_evidence.metric("Evidence Items", int(latest.get("evidence_count") or 0))
    col_history.metric(
        "Prior Investigations",
        int(latest.get("incident_history_count") or 0),
    )
    col_audit.metric("Audit Events", int(latest.get("audit_count") or 0))

    st.markdown("#### Guardrail")
    st.info(
        "This answer is advisory. Any backfill, rerun, ticket, notification, or data-changing action "
        "still requires explicit approval and execution through the controlled Airflow/API boundary."
    )

    st.markdown("#### Suggested Next Step")

    if latest.get("has_report"):
        st.write("Review the cited evidence and approval preview before using any remediation action.")

    else:
        st.write("Run triage for this Alert Ref first so the Copilot can use collected evidence and a matching report.")


def render_mock_action_panel(alert: dict[str, Any]) -> None:
    """
    Render manually mocked Slack and ticket actions.

    Args:
        alert: Selected alert row.

    Returns:
        None.
    """
    st.subheader("Mock External Actions")
    st.caption("These buttons only write an audit event. They do not call Slack, Jira, or any external service.")

    col_slack, col_ticket = st.columns(2)

    slack_payload = {
        "channel": "data-quality-alerts",
        "alert_display_id": alert.get("alert_display_id"),
        "alert_key": alert.get("alert_key"),
        "severity": alert.get("severity"),
        "dt": str(alert.get("dt") or ""),
        "message": "Mock Slack notification for selected DQ alert.",
    }
    ticket_payload = {
        "project": "DQ",
        "summary": f"{alert.get('severity')} DQ alert for {alert.get('table_name')}",
        "alert_display_id": alert.get("alert_display_id"),
        "alert_key": alert.get("alert_key"),
        "dt": str(alert.get("dt") or ""),
        "description": "Mock ticket creation for selected DQ alert.",
    }

    with col_slack:
        if st.button("Mock post to Slack", use_container_width=True):
            try:
                result = record_mock_external_action(
                    alert=alert,
                    action_name="mock_post_to_slack",
                    payload=slack_payload,
                )
                st.session_state["latest_mock_action"] = result
                st.success("Mock Slack action recorded.")

            except Exception as exc:
                st.error(f"Failed to record mock Slack action: {exc}")

    with col_ticket:
        if st.button("Mock create ticket", use_container_width=True):
            try:
                result = record_mock_external_action(
                    alert=alert,
                    action_name="mock_create_ticket",
                    payload=ticket_payload,
                )
                st.session_state["latest_mock_action"] = result
                st.success("Mock ticket action recorded.")

            except Exception as exc:
                st.error(f"Failed to record mock ticket action: {exc}")

    if "latest_mock_action" in st.session_state:
        with st.expander("Latest mocked action result", expanded=False):
            st.json(st.session_state["latest_mock_action"])


# --- Defining Main App
def main() -> None:
    """
    Configure and render the Streamlit application.

    Returns:
        None.
    """
    logger.info("Rendering Streamlit app")

    apply_page_style()
    render_header()

    settings = render_sidebar_filters()

    try:
        alerts = load_alert_rows(
            status=settings["status"],
            dt=settings["dt"],
            limit=int(settings["limit"]),
        )

    except Exception as exc:
        st.error(f"Unable to load alerts through the control-plane boundary: {exc}")
        st.stop()

    daily_summary           = None
    daily_summary_transport = ""
    daily_summary_error     = None

    try:
        daily_summary, daily_summary_transport = load_daily_quality_summary(
            dt=settings["summary_dt"],
        )

    except Exception as exc:
        daily_summary_error = f"{type(exc).__name__}: {exc}"
        logger.exception(
            "Unable to load Streamlit daily quality summary | dt=%s",
            settings["summary_dt"],
        )

    render_reliability_overview(
        alerts=alerts,
        daily_summary=daily_summary,
        daily_summary_transport=daily_summary_transport,
        daily_summary_error=daily_summary_error,
    )
    render_approval_queue_panel()
    render_life_evaluation_history_panel()
    render_alert_table(alerts)

    if not alerts:
        st.stop()

    alert_options = {
        f"{index:02d} | {row.get('alert_display_id')} | {row.get('severity')} | {row.get('dt')} | {row.get('table_name')} | {row.get('metric')}": row
        for index, row in enumerate(alerts, start=1)
    }
    selected_label = st.selectbox("Select alert for investigation", options=list(alert_options.keys()))
    selected_alert = alert_options[selected_label]

    st.divider()
    render_selected_alert(selected_alert)

    st.divider()
    render_incident_history_panel(selected_alert)

    st.divider()
    render_checkpoint_recovery_panel(selected_alert)

    st.divider()
    render_lineage_impact_panel(selected_alert)

    st.divider()
    col_triage, col_audit = st.columns([1.2, 1])

    with col_triage:
        render_triage_panel(alert=selected_alert, settings=settings)

    with col_audit:
        render_audit_panel(selected_alert)

    st.divider()
    render_copilot_panel(selected_alert)

    st.divider()
    render_mock_action_panel(selected_alert)


# --- Running CLI Entrypoint
if __name__ == "__main__":
    main()
