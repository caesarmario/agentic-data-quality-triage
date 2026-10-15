####
## Discord Message Formatters for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from uuid import uuid4

from agent.display import build_alert_ref, build_report_id, humanize_metric_name, humanize_table_name
from apps.common.i18n import translate as tr
from apps.common.locales.discord import MESSAGES
from apps.common.llm_observability import llm_route_from_report
from apps.discord_bot.contracts import (
    DiscordActionRow,
    DiscordActionSpec,
    DiscordIncidentFacts,
    DiscordIncidentPayload,
    DiscordNarrative,
    DiscordNarrativeSource,
    DiscordRenderResult,
)
from pipelines.common.alert_identity import is_alert_ref
from pipelines.common.logging import logger


# --- Defining Constants
DISCORD_SEPARATOR   = "### ----------------------------------------"
DISCORD_SOFT_LIMIT  = 1900
MAX_EVIDENCE_ITEMS  = 5
MAX_ALERT_ITEMS     = 8

DISCORD_EMBED_TITLE_LIMIT       = 256
DISCORD_EMBED_DESCRIPTION_LIMIT = 4096
DISCORD_EMBED_FIELD_NAME_LIMIT  = 256
DISCORD_EMBED_FIELD_VALUE_LIMIT = 1024
DISCORD_EMBED_FIELD_LIMIT       = 25
DISCORD_EMBED_TOTAL_LIMIT       = 6000
DISCORD_COMPONENT_CUSTOM_ID_LIMIT = 100
DISCORD_COMPONENT_LABEL_LIMIT     = 80

DISCORD_COLORS = {
    "critical": 0xD83C3E,
    "warning": 0xE5A50A,
    "info": 0x2F80ED,
    "success": 0x2D9D78,
    "neutral": 0x5B6472,
}

LIFECYCLE_LABELS = {
    "open": "OPEN",
    "acknowledged": "OPEN",
    "triaged": "TRIAGED",
    "waiting_approval": "WAITING APPROVAL",
    "pending": "WAITING APPROVAL",
    "approved": "APPROVED",
    "rejected": "REJECTED",
    "resolved": "RESOLVED",
}

SEVERITY_ORDER = {
    "critical": 0,
    "warning": 1,
    "info": 2,
}

SEVERITY_LABELS = {
    "critical": "Critical Alerts",
    "warning": "Warning Alerts",
    "info": "Informational Alerts",
}

SEVERITY_ICONS = {
    "critical": "\U0001F6A8",
    "warning": "\u26A0\uFE0F",
    "info": "\u2705",
}

STATUS_ICONS = {
    "open": "\U0001F6A8",
    "acknowledged": "\U0001F9FE",
    "triaged": "\U0001F9ED",
    "resolved": "\u2705",
}

BUTTON_STYLE_VALUES = {
    "primary": 1,
    "secondary": 2,
    "success": 3,
    "danger": 4,
    "link": 5,
}


@dataclass(frozen=True)
class ResolvedDiscordIncidentPayload(DiscordIncidentPayload):
    """Closure projection carried without changing the shared incident contract."""

    resolution: dict[str, Any] = field(default_factory=dict)
    scope: dict[str, Any] = field(default_factory=dict)



# --- Defining Generic Helpers
def _state_label(value: Any, *, title_case: bool = False) -> str:
    """Localize controlled presentation states without changing contract values."""
    label = tr(str(value or "unknown").upper())
    return label.title() if title_case else label


def build_alert_title(alert: Any) -> str:
    """Localize generated issue copy, not the table, date, or metric identifier."""
    table = humanize_table_name(get_model_value(alert, "table_name", ""))
    if get_model_value(alert, "table_name", "") in {"dq.raw_orders", "dq.stg_orders", "dq.fct_orders_daily", ""}:
        table = tr(table)
    metric = tr(humanize_metric_name(get_model_value(alert, "metric", "")))
    dt = get_model_value(alert, "dt", "")
    suffix = tr(" on {date}", date=dt) if dt else ""
    return tr("{table} has {metric}{date_suffix}", table=table.title(), metric=metric, date_suffix=suffix)


def build_alert_one_liner(alert: Any) -> str:
    """Render deterministic observations using fixed copy and original values."""
    table = humanize_table_name(get_model_value(alert, "table_name", ""))
    if get_model_value(alert, "table_name", "") in {"dq.raw_orders", "dq.stg_orders", "dq.fct_orders_daily", ""}:
        table = tr(table)
    return tr(
        "The platform detected {metric} in {table} for {date}. "
        "Observed value is {observed}, while expected value is {expected}.",
        metric=tr(humanize_metric_name(get_model_value(alert, "metric", ""))), table=table,
        date=get_model_value(alert, "dt", tr("the selected date")),
        observed=get_model_value(alert, "observed_value", "N/A"),
        expected=get_model_value(alert, "expected_value", "N/A"),
    )


def parse_json_object(value: Any) -> dict[str, Any]:
    """
    Parse a JSON object from a Discord-facing payload value.

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
        logger.warning(
            "Failed to parse Discord formatter JSON | value_type=%s value_length=%d",
            type(value).__name__,
            len(value),
        )
        return {}

    return parsed if isinstance(parsed, dict) else {}


def compact_value(value: Any, empty: str = "N/A") -> str:
    """
    Convert optional values into compact Discord-safe text.

    Args:
        value: Raw value to render.
        empty: Text returned when the value is blank.

    Returns:
        Compact string representation.
    """
    if value is None or value == "":
        return empty

    return str(value)


def severity_icon(severity: Any) -> str:
    """
    Resolve an emoji anchor for a severity value.

    Args:
        severity: Raw severity label.

    Returns:
        Emoji anchor for the severity.
    """
    normalized = str(severity or "info").lower()

    return SEVERITY_ICONS.get(normalized, "\U0001F4CC")


def status_icon(status: Any) -> str:
    """
    Resolve an emoji anchor for an alert status.

    Args:
        status: Raw alert status.

    Returns:
        Emoji anchor for the status.
    """
    normalized = str(status or "open").lower()

    return STATUS_ICONS.get(normalized, "\U0001F4CC")


def resolve_alert_ref(alert: Any) -> str:
    """
    Resolve the primary human-facing identifier for an alert.

    Args:
        alert: Alert dictionary or Pydantic-like model.

    Returns:
        Existing Alert Ref or a deterministic reference derived from the system key.
    """
    alert_key = compact_value(get_model_value(alert, "alert_key"))
    alert_ref = get_model_value(alert, "alert_display_id", "")

    return compact_value(alert_ref, build_alert_ref(alert_key, get_model_value(alert, "dt")))


def count_alert_severities(alerts: list[dict[str, Any]]) -> dict[str, int]:
    """
    Count alerts by normalized severity.

    Args:
        alerts: Alert row dictionaries.

    Returns:
        Mapping from severity label to alert count.
    """
    counts = {severity: 0 for severity in SEVERITY_ORDER}

    for alert in alerts:
        severity         = str(alert.get("severity") or "info").lower()
        counts[severity] = counts.get(severity, 0) + 1

    return counts


def confidence_readout(confidence: float) -> str:
    """
    Translate a numeric confidence value into an operator-friendly label.

    Args:
        confidence: Confidence value between zero and one.

    Returns:
        Human-readable confidence label.
    """
    if confidence >= 0.90:
        return tr('Strong evidence')

    if confidence >= 0.70:
        return tr('Good evidence with some uncertainty')

    if confidence >= 0.50:
        return tr('Plausible, but more evidence is recommended')

    return tr('Low confidence; do not treat this as confirmed root cause')


def daily_health_status(check_counts: dict[str, int], alert_counts: dict[str, int]) -> tuple[str, str]:
    """
    Resolve the daily reliability status from check and alert counts.

    Args:
        check_counts: DQ check counts keyed by status.
        alert_counts: Alert counts keyed by severity.

    Returns:
        Tuple containing a short status label and operator-facing explanation.
    """
    failed   = check_counts.get("fail", 0)
    critical = alert_counts.get("critical", 0)
    warnings = check_counts.get("warn", 0) + alert_counts.get("warning", 0)

    if failed or critical:
        return tr('Needs Attention'), tr('Critical signals are present. Review the highest-priority alert before trusting downstream data.')

    if warnings:
        return tr('Review Recommended'), tr('No critical signal is present, but warning-level checks should be reviewed.')

    return tr('Healthy'), tr('No failed checks or open critical alerts were found for this date.')


def trim_message(text: str, limit: int = DISCORD_SOFT_LIMIT) -> str:
    """
    Trim a message to stay under Discord's hard message limit.

    Args:
        text: Full message body.
        limit: Soft maximum character count.

    Returns:
        Trimmed message body with separator preserved when possible.
    """
    if len(text) <= limit:
        return text

    suffix = tr("\n\nMessage trimmed for Discord.\n\n{v0}", v0=DISCORD_SEPARATOR)
    trimmed = text[: max(0, limit - len(suffix))].rstrip()

    return f"{trimmed}{suffix}"


def split_message(
    text: str,
    limit: int = DISCORD_SOFT_LIMIT,
) -> list[str]:
    """
    Split a long Discord message without discarding report sections.

    Args:
        text: Complete operator-facing message.
        limit: Maximum characters per Discord message chunk.

    Returns:
        Ordered non-empty chunks, each no longer than the configured limit.

    Raises:
        ValueError: If the configured limit is too small for safe splitting.
    """
    if limit < 200:
        raise ValueError(tr('Discord message limit must be at least 200 characters.'))

    remaining = text.strip()
    chunks: list[str] = []

    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break

        window = remaining[:limit]
        cut_at = max(
            window.rfind("\n\n"),
            window.rfind("\n"),
            window.rfind(" "),
        )

        # Avoid tiny chunks when no useful boundary exists near the limit.
        if cut_at < limit // 2:
            cut_at = limit

        chunks.append(remaining[:cut_at].rstrip())
        remaining = remaining[cut_at:].lstrip()

    return chunks or [""]


def join_lines(lines: list[str]) -> str:
    """
    Join Discord message lines and apply the standard separator.

    Args:
        lines: Message lines without the trailing separator.

    Returns:
        Discord message text.
    """
    body = "\n".join(lines).strip()

    return f"{body}\n\n{DISCORD_SEPARATOR}"


# --- Defining Alert Formatters
def describe_alert(alert: dict[str, Any]) -> str:
    """
    Build a short human-readable alert description.

    Args:
        alert: Alert row dictionary.

    Returns:
        Short alert description.
    """
    return build_alert_one_liner(alert)


def format_alert_summary(alert: dict[str, Any]) -> str:
    """
    Format one alert as a readable Discord message.

    Args:
        alert: Alert row dictionary from ClickHouse.

    Returns:
        Discord Markdown message for one alert.
    """
    details       = parse_json_object(alert.get("details_json") or alert.get("details"))
    evidence_uri  = details.get("evidence_s3_uri") or details.get("source_details", {}).get("evidence_s3_uri") or ""
    severity      = compact_value(alert.get("severity"), "info")
    title         = build_alert_title(alert)
    plain_text    = describe_alert(alert)
    alert_key     = compact_value(alert.get("alert_key"))
    alert_ref     = resolve_alert_ref(alert)
    affected_date = compact_value(alert.get("dt"))

    logger.info("Formatting Discord alert summary | alert_ref=%s severity=%s", alert_ref, severity)

    lines = [
        tr("# {v0} {v1} Data Quality Alert", v0=severity_icon(severity), v1=_state_label(severity, title_case=True)),
        f"## {title}",
        "",
        tr('### Quick Read'),
        plain_text,
        tr('Triage this alert before trusting downstream data for the affected date.'),
        "",
        tr('### Key Facts'),
        tr("**Alert Ref** `{v0}`", v0=alert_ref),
        tr("**Date** `{v0}`", v0=affected_date),
        tr("**Affected Table** `{v0}`", v0=compact_value(alert.get('table_name'))),
        tr("**Check** `{v0}`", v0=compact_value(alert.get('metric'))),
        tr("**Observed / Expected** `{v0}` / `{v1}`", v0=compact_value(alert.get('observed_value')), v1=compact_value(alert.get('expected_value'))),
        tr("**Threshold** `{v0}`", v0=compact_value(alert.get('threshold_value'))),
        tr("**Status** {v0} {v1}", v0=status_icon(alert.get('status')), v1=_state_label(alert.get('status'), title_case=True)),
        "",
        tr('### Impact'),
        tr('- Downstream tables, checks, and dashboards for this date may be incomplete or misleading.'),
        tr('- Do not trigger a backfill or rerun until evidence confirms the likely cause.'),
        "",
        tr('### Recommended Next Step'),
        tr("Run triage for `{v0}` to collect ClickHouse, DQ history, pipeline run, and lineage evidence.", v0=alert_ref),
        "",
        tr('### Commands'),
        f"/dq triage alert_key:{alert_ref}",
        f"/dq daily_summary dt:{affected_date}",
    ]

    if evidence_uri:
        lines.extend(["", tr('### Evidence Artifact'), f"`{evidence_uri}`"])

    lines.extend(["", tr('### Technical Reference'), tr('For debugging and internal joins only.'), f"System Alert Key `{alert_key}`"])

    return join_lines(lines)


def format_alert_list(
    alerts: list[dict[str, Any]],
    status: str,
    dt: str | None = None,
    assistant_note: str = "",
    data_transport: str = "",
) -> str:
    """
    Format a bounded list of alerts for Discord.

    Args:
        alerts: Alert row dictionaries.
        status: Alert status filter used by the command.
        dt: Optional business date filter.
        assistant_note: Optional LLM-assisted natural language readout.
        data_transport: Alert lookup transport such as api or local.

    Returns:
        Discord Markdown message listing alerts.
    """
    severity_counts = count_alert_severities(alerts)
    sorted_alerts    = sorted(
        alerts,
        key=lambda alert: (
            SEVERITY_ORDER.get(str(alert.get("severity") or "info").lower(), 99),
            str(alert.get("dt") or ""),
            str(alert.get("alert_key") or ""),
        ),
    )
    visible_alerts = sorted_alerts[:MAX_ALERT_ITEMS]

    logger.info(
        "Formatting Discord alert list | status=%s dt=%s alerts=%d critical=%d warning=%d",
        status,
        dt,
        len(alerts),
        severity_counts.get("critical", 0),
        severity_counts.get("warning", 0),
    )

    lines = [
        tr("# {v0} DQ Alerts", v0=status_icon(status)),
        tr("## Status {v0}", v0=_state_label(status, title_case=True)),
        "",
        tr('### Health Summary'),
        tr("**Date Filter** `{v0}`", v0=compact_value(dt, tr('All dates'))),
        tr("**Total Alerts** `{v0}`", v0=len(alerts)),
        tr("**Critical** `{v0}` | **Warning** `{v1}` | **Info** `{v2}`", v0=severity_counts.get('critical', 0), v1=severity_counts.get('warning', 0), v2=severity_counts.get('info', 0)),
        "",
    ]

    if assistant_note:
        lines.extend([tr('### Copilot Readout'), assistant_note, ""])

    if not alerts:
        lines.extend(
            [
                tr('### Result'),
                tr('No alerts matched the selected filter.'),
                "",
                tr('### Start Here'),
                tr("Run /dq daily_summary dt:{v0} to review the broader daily status.", v0=compact_value(dt, '<YYYY-MM-DD>')),
            ]
        )

        if data_transport:
            lines.extend(
                [
                    "",
                    tr('### Technical Reference'),
                    tr('For debugging and internal joins only.'),
                    f"Alert Data Transport {data_transport}",
                ]
            )

        return join_lines(lines)

    for severity in SEVERITY_ORDER:
        group = [alert for alert in visible_alerts if str(alert.get("severity") or "info").lower() == severity]

        if not group:
            continue

        lines.extend([f"### {severity_icon(severity)} {tr(SEVERITY_LABELS[severity])}"])

        for index, alert in enumerate(group, start=1):
            alert_ref = resolve_alert_ref(alert)
            title     = build_alert_title(alert)

            lines.extend(
                [
                    f"{index}. **{title}**",
                    f"   Alert Ref `{alert_ref}` | `{compact_value(alert.get('dt'))}` | `{compact_value(alert.get('table_name'))}`",
                ]
            )

        lines.append("")

    if len(alerts) > MAX_ALERT_ITEMS:
        lines.append(tr("\nShowing {v0} of {v1} alerts. Use a tighter date filter for details.", v0=MAX_ALERT_ITEMS, v1=len(alerts)))

    first_alert_ref = resolve_alert_ref(sorted_alerts[0])
    lines.extend(
        [
            tr('### Start Here'),
            tr("Triage the highest-priority alert first: /dq triage alert_key:{v0}", v0=first_alert_ref),
        ]
    )

    if data_transport:
        lines.extend(
            [
                "",
                tr('### Technical Reference'),
                tr('For debugging and internal joins only.'),
                f"Alert Data Transport {data_transport}",
            ]
        )

    return join_lines(lines)


# --- Defining Triage Formatters
def get_model_value(model: Any, field_name: str, default: Any = None) -> Any:
    """
    Read a field from a Pydantic model or dictionary.

    Args:
        model: Pydantic model, dictionary, or None.
        field_name: Field name to read.
        default: Fallback value.

    Returns:
        Field value or default.
    """
    if model is None:
        return default

    if isinstance(model, dict):
        return model.get(field_name, default)

    return getattr(model, field_name, default)


def format_triage_result(
    report: Any,
    assistant_note: str = "",
    execution_transport: str = "",
    narrative_transport: str = "",
) -> str:
    """
    Format a triage report as a Discord message.

    Args:
        report: TriageReport model returned by the LangGraph workflow.
        assistant_note: Optional LLM-assisted natural language readout.
        execution_transport: Triage execution transport.
        narrative_transport: Copilot narrative transport.

    Returns:
        Discord Markdown message for the triage result.
    """
    alert              = get_model_value(report, "alert", {})
    top_hypothesis     = get_model_value(report, "top_hypothesis")
    evidence           = list(get_model_value(report, "evidence", []) or [])
    actions            = list(get_model_value(report, "approval_gated_actions", []) or [])
    ai_investigation   = get_model_value(report, "ai_investigation")
    report_llm_runtime = get_model_value(report, "llm_runtime")
    confidence         = float(get_model_value(report, "confidence", 0.0) or 0.0)
    title              = get_model_value(top_hypothesis, "title", tr('Unknown root cause'))
    alert_key          = compact_value(get_model_value(alert, "alert_key"))
    report_id          = compact_value(get_model_value(report, "report_id"), build_report_id(get_model_value(report, "agent_run_id"), alert_key))
    alert_ref          = resolve_alert_ref(alert)
    issue_title        = build_alert_title(alert)
    confidence_summary = confidence_readout(confidence)
    llm_observation    = llm_route_from_report(report=report)
    llm_runtime        = llm_observation.to_public_dict() if llm_observation else None

    if int(get_model_value(report_llm_runtime, "route_event_count", 0) or 0) > 0:
        external_used = bool(get_model_value(report_llm_runtime, "external_model_used", False))
        heuristic_used = bool(get_model_value(report_llm_runtime, "heuristic_fallback_used", False))
        llm_runtime = {
            "runtime_mode": (
                "mixed"
                if external_used and heuristic_used
                else "external_model"
                if external_used
                else "heuristic_fallback"
            ),
            "route_event_count": int(get_model_value(report_llm_runtime, "route_event_count", 0) or 0),
            "providers": list(get_model_value(report_llm_runtime, "providers", []) or []),
            "models": list(get_model_value(report_llm_runtime, "models", []) or []),
            "requested_routes": list(get_model_value(report_llm_runtime, "requested_routes", []) or []),
            "executed_routes": list(get_model_value(report_llm_runtime, "executed_routes", []) or []),
            "input_tokens": int(get_model_value(report_llm_runtime, "input_tokens", 0) or 0),
            "output_tokens": int(get_model_value(report_llm_runtime, "output_tokens", 0) or 0),
            "estimated_cost_usd": float(get_model_value(report_llm_runtime, "estimated_cost_usd", 0.0) or 0.0),
            "duration_ms": int(get_model_value(report_llm_runtime, "duration_ms", 0) or 0),
        }

    logger.info(
        "Formatting Discord triage result | report_id=%s alert_ref=%s confidence=%.2f evidence=%d actions=%d",
        report_id,
        alert_ref,
        confidence,
        len(evidence),
        len(actions),
    )

    lines = [
        tr('# 🧭 Triage Result'),
        f"## {issue_title}",
        "",
        tr('### Quick Read'),
        compact_value(
            get_model_value(ai_investigation, "what_happened"),
            tr("The leading explanation is {v0}.", v0=title),
        ),
        tr("Confidence is `{v0}`: {v1}.", v0=f'{confidence:.2f}', v1=confidence_summary),
        "",
        tr('### Key Facts'),
        tr("**Alert Ref** `{v0}`", v0=alert_ref),
        tr("**Report ID** `{v0}`", v0=report_id),
        tr("**Date** `{v0}`", v0=compact_value(get_model_value(alert, 'dt'))),
        tr("**Severity** {v0} {v1}", v0=severity_icon(get_model_value(alert, 'severity')), v1=_state_label(get_model_value(alert, 'severity'), title_case=True)),
        "",
    ]

    if assistant_note:
        lines.extend([tr('### Copilot Follow-up'), assistant_note, ""])

    if llm_runtime:
        mode_label = {
            "external_model": tr('External model'),
            "heuristic_fallback": tr('Heuristic fallback'),
            "mixed": tr('External model with fallback'),
            "failed": tr('Failed'),
        }.get(str(llm_runtime.get("runtime_mode")), tr('Unknown'))

        providers = llm_runtime.get("providers") or [llm_runtime.get("provider")]
        models = llm_runtime.get("models") or [llm_runtime.get("model")]
        requested_routes = llm_runtime.get("requested_routes") or [llm_runtime.get("requested_route")]
        executed_routes = llm_runtime.get("executed_routes") or [llm_runtime.get("executed_route")]
        provider_model_label = ", ".join(
            f"{compact_value(provider)} / {compact_value(model)}"
            for provider, model in zip(providers, models)
        )
        route_label = (
            f"{', '.join(compact_value(item) for item in requested_routes)} -> "
            f"{', '.join(compact_value(item) for item in executed_routes)}"
        )
        estimated_cost = (
            llm_runtime.get("estimated_cost_display")
            or f"USD {float(llm_runtime.get('estimated_cost_usd') or 0.0):.8f}"
        )

        lines.extend(
            [
                tr('### AI Runtime'),
                tr("**Mode** {v0}", v0=mode_label),
                tr("**Route Events** `{v0}`", v0=int(llm_runtime.get('route_event_count') or 1)),
                tr("**Providers / Models** `{v0}`", v0=provider_model_label),
                tr("**Routes** `{v0}`", v0=route_label),
                tr("**Usage** `{v0} input / {v1} output tokens`", v0=int(llm_runtime.get('input_tokens') or 0), v1=int(llm_runtime.get('output_tokens') or 0)),
                tr("**Estimated Cost** `{v0}`", v0=compact_value(estimated_cost)),
            ]
        )

        if llm_runtime.get("fallback_summary"):
            lines.append(tr("**Fallback** {v0}", v0=compact_value(llm_runtime.get('fallback_summary'))))

        lines.append("")

    lines.extend(
        [
            tr('### What Likely Happened'),
            compact_value(
                get_model_value(ai_investigation, "what_happened"),
                get_model_value(top_hypothesis, "description", tr('No hypothesis description was generated.')),
            ),
            "",
            tr('### Why I Think So'),
        ]
    )

    ai_reasoning = list(get_model_value(ai_investigation, "evidence_reasoning", []) or [])

    if ai_reasoning:
        for index, item in enumerate(ai_reasoning[:MAX_EVIDENCE_ITEMS], start=1):
            lines.append(f"{index}. {compact_value(item)}")
    elif evidence:
        for index, item in enumerate(evidence[:MAX_EVIDENCE_ITEMS], start=1):
            lines.append(f"{index}. {compact_value(get_model_value(item, 'summary'))}")
    else:
        lines.append(tr('No evidence rows were returned.'))

    recommended_action = compact_value(
        get_model_value(ai_investigation, "recommended_next_step"),
        get_model_value(top_hypothesis, "recommended_action", tr('Review the report before taking action.')),
    )

    lines.extend(
        [
            "",
            tr('### Likely Impact'),
            compact_value(
                get_model_value(ai_investigation, "likely_impact"),
                get_model_value(report, "impact", tr('Downstream impact is still being assessed.')),
            ),
            "",
            tr('### What Is Still Uncertain'),
            compact_value(
                get_model_value(ai_investigation, "uncertainty"),
                tr('The evidence supports a leading hypothesis but does not prove every upstream condition.'),
            ),
            "",
            tr('### Recommended Next Step'),
            recommended_action,
            "",
        ]
    )

    if actions:
        action = actions[0]
        lines.extend(
            [
                tr('### 🧾 Approval Status'),
                tr('**Approval required.** This recommendation has not been executed.'),
                compact_value(
                    get_model_value(ai_investigation, "approval_rationale"),
                    tr('The action can change pipeline state and therefore requires explicit human review.'),
                ),
                tr("Action `{v0}` needs explicit approval before execution.", v0=compact_value(get_model_value(action, 'action_type'))),
                f"Target DAG `{compact_value(get_model_value(action, 'target_dag_id'))}`",
                "",
            ]
        )
    else:
        lines.extend([tr('### Approval Status'), tr('No approval-gated action was recommended by this triage run.'), ""])

    lines.extend(
        [
            tr('### 📄 Report Links'),
            f"Markdown `{compact_value(get_model_value(report, 'markdown_report_s3_uri'))}`",
            f"JSON `{compact_value(get_model_value(report, 'json_report_s3_uri'))}`",
            "",
            tr('### Technical Reference'),
            f"System Alert Key `{alert_key}`",
            f"Agent Run ID `{compact_value(get_model_value(report, 'agent_run_id'))}`",
            f"Triage Transport {compact_value(execution_transport, 'unknown')}",
            f"Narrative Transport {compact_value(narrative_transport, 'unknown')}",
        ]
    )

    return join_lines(lines)


# --- Defining Summary And Action Formatters
def format_daily_summary(
    dt: str,
    check_rows: list[dict[str, Any]],
    alert_rows: list[dict[str, Any]],
    assistant_note: str = "",
    data_transport: str = "",
) -> str:
    """
    Format a daily DQ summary message.

    Args:
        dt: Business date in YYYY-MM-DD format.
        check_rows: DQ check status count rows.
        alert_rows: Alert severity count rows.
        assistant_note: Optional LLM-assisted natural language readout.
        data_transport: Data source boundary used, such as api or local.

    Returns:
        Discord Markdown daily summary.
    """
    check_counts              = {str(row.get("status")): int(row.get("count") or 0) for row in check_rows}
    alert_counts              = {str(row.get("severity")): int(row.get("count") or 0) for row in alert_rows}
    health_label, health_text = daily_health_status(check_counts=check_counts, alert_counts=alert_counts)

    logger.info(
        "Formatting Discord daily summary | dt=%s health=%s failed=%d critical=%d",
        dt,
        health_label,
        check_counts.get("fail", 0),
        alert_counts.get("critical", 0),
    )

    lines = [
        tr('# 📊 DQ Daily Summary'),
        f"## {dt}",
        "",
        tr('### Day Status'),
        f"**{health_label}**",
        health_text,
        "",
        tr('### Check Results'),
        tr("\u2705 Passed `{v0}`", v0=check_counts.get('pass', 0)),
        tr("\u26a0\ufe0f Warning `{v0}`", v0=check_counts.get('warn', 0)),
        tr("\U0001f6a8 Failed `{v0}`", v0=check_counts.get('fail', 0)),
        tr("\u23ed\ufe0f Skipped `{v0}`", v0=check_counts.get('skip', 0)),
        "",
    ]

    if assistant_note:
        lines.extend([tr('### Copilot Analysis'), assistant_note, ""])

    lines.extend(
        [
            tr('### Alert Risk'),
            tr("\U0001f6a8 Open Critical `{v0}`", v0=alert_counts.get('critical', 0)),
            tr("\u26a0\ufe0f Open Warning `{v0}`", v0=alert_counts.get('warning', 0)),
            "",
            tr('### Next Commands'),
            f"/dq alerts dt:{dt} status:open limit:10",
            "/dq triage alert_key:<Alert Ref>",
        ]
    )

    if data_transport:
        lines.extend(
            [
                "",
                tr('### Technical Reference'),
                f"Data Transport `{compact_value(data_transport)}`",
            ]
        )

    return join_lines(lines)


def format_quality_summary(
    payload: dict[str, Any],
    assistant_note: str = "",
    data_transport: str = "",
) -> str:
    """Format weekly, table, and warehouse summaries with safe rerun advice."""
    scope          = compact_value(payload.get("scope"), "quality").lower()
    start_date     = compact_value(payload.get("start_date"))
    end_date       = compact_value(payload.get("end_date"))
    table_name     = compact_value(payload.get("table_name"), "")
    check_counts   = {
        str(row.get("status") or ""): int(row.get("count") or 0)
        for row in list(payload.get("check_counts") or [])
    }
    alert_counts = {
        str(row.get("severity") or ""): int(row.get("count") or 0)
        for row in list(payload.get("alert_counts") or [])
    }
    table_counts = sorted(
        list(payload.get("table_counts") or []),
        key=lambda row: (
            -int(row.get("critical_alerts") or 0),
            -int(row.get("failed_checks") or 0),
            str(row.get("table_name") or ""),
        ),
    )
    suggestions = list(payload.get("rerun_suggestions") or [])
    failed      = check_counts.get("fail", 0)
    critical    = alert_counts.get("critical", 0)
    health      = tr('Needs Attention') if failed or critical else tr('Review Recommended') if check_counts.get("warn", 0) else tr('Healthy')
    title       = {
        "weekly": tr('DQ Weekly Summary'),
        "table": tr('DQ Table Summary'),
        "database": tr('DQ Warehouse Summary'),
    }.get(scope, tr('DQ Quality Summary'))
    subject = tr("Table `{v0}`", v0=table_name) if scope == "table" else tr("Window `{v0}` to `{v1}`", v0=start_date, v1=end_date)

    lines = [
        f"# \U0001F4CA {title}",
        f"## {subject}",
        "",
        tr('### Quick Read'),
        f"**{health}**",
        tr("Observed `{v0}` failed check(s) and `{v1}` critical open alert(s) in the selected scope.", v0=failed, v1=critical),
        "",
    ]

    if assistant_note:
        lines.extend([tr('### Copilot Analysis'), assistant_note, ""])

    lines.extend(
        [
            tr('### Check Results'),
            tr("\u2705 Passed `{v0}`", v0=check_counts.get('pass', 0)),
            tr("\u26a0\ufe0f Warning `{v0}`", v0=check_counts.get('warn', 0)),
            tr("\U0001f6a8 Failed `{v0}`", v0=failed),
            tr("\u23ed\ufe0f Skipped `{v0}`", v0=check_counts.get('skip', 0)),
            "",
            tr('### Open Alerts'),
            tr("\U0001f6a8 Critical `{v0}`", v0=critical),
            tr("\u26a0\ufe0f Warning `{v0}`", v0=alert_counts.get('warning', 0)),
            tr("**Registered Metadata Assets** `{v0}`", v0=int(payload.get('registered_asset_count') or 0)),
            "",
        ]
    )

    if table_counts:
        lines.extend([tr('### Most Affected Tables')])

        for index, row in enumerate(table_counts[:5], start=1):
            lines.append(
                tr("{v0}. `{v1}` | failed `{v2}` | critical `{v3}`", v0=index, v1=compact_value(row.get('table_name')), v2=int(row.get('failed_checks') or 0), v3=int(row.get('critical_alerts') or 0))
            )

        lines.append("")

    lines.extend([tr('### Manual Rerun Guidance')])

    if suggestions:
        lines.append(tr('These are advisory candidates only. Nothing has been executed, and explicit approval is still required.'))

        for suggestion in suggestions:
            affected         = ", ".join(f"`{item}`" for item in list(suggestion.get("affected_tables") or []))
            affected_display = affected or "`N/A`"
            lines.append(
                tr("- Review `{v0}` for {v1}.", v0=compact_value(suggestion.get('target_dag_id')), v1=affected_display)
            )

    else:
        lines.append(tr('No allowlisted rerun is supported by this aggregate alone. Triage an alert before proposing an action.'))

    lines.extend(
        [
            "",
            tr('### Next Commands'),
            f"/dq alerts dt:{end_date} status:open limit:10",
            "/dq triage alert_key:<Alert Ref>",
        ]
    )

    if data_transport:
        lines.extend(
            [
                "",
                tr('### Technical Reference'),
                f"Scope `{scope}` | Data Transport `{compact_value(data_transport)}`",
            ]
        )

    return join_lines(lines)


def format_operator_answer(
    question: str,
    answer: str,
    alert_key: str = "",
    transport: str = "",
    agent_run_id: str = "",
    incident_history_count: int = 0,
    response_mode: str = "structured",
    llm_runtime: dict[str, Any] | None = None,
) -> str:
    """
    Format a free-form copilot answer for Discord.

    Args:
        question: User question.
        answer: Copilot answer.
        alert_key: Optional alert key used as context.
        transport: Copilot transport such as api or local.
        agent_run_id: Optional API and audit correlation id.
        incident_history_count: Earlier exact-match investigations supplied as
            comparison context.
        response_mode: Direct model answer or structured operator envelope.
        llm_runtime: Optional sanitized provider/model/usage proof.

    Returns:
        Discord Markdown message with a natural language answer.
    """
    alert_ref = ""

    if alert_key:
        alert_ref = alert_key if is_alert_ref(alert_key) else build_alert_ref(alert_key)

    logger.info("Formatting Discord copilot answer | alert_context=%s", alert_ref or "none")

    normalized_mode = response_mode.strip().lower()

    if normalized_mode not in {"direct", "structured"}:
        raise ValueError(tr('response_mode must be direct or structured.'))

    runtime      = dict(llm_runtime or {})
    provider     = compact_value(runtime.get("provider"), "local")
    model        = compact_value(runtime.get("model"), tr('bounded fallback'))
    runtime_mode = compact_value(runtime.get("runtime_mode"), "local_fallback")

    if normalized_mode == "direct":
        heading = tr('Gemini Answer') if provider.lower() == "gemini" else tr('Copilot Answer')
        lines = [
            f"# \u2728 {heading}",
            answer,
            "",
            tr('### Runtime Proof'),
            tr("**Provider / Model** `{v0} / {v1}`", v0=provider, v1=model),
            tr("**Mode** `{v0}`", v0=runtime_mode),
        ]

        if runtime:
            lines.extend(
                [
                    tr("**Route** `{v0}`", v0=compact_value(runtime.get('executed_route'))),
                    tr("**Usage** `{v0} input / {v1} output tokens`", v0=int(runtime.get('input_tokens') or 0), v1=int(runtime.get('output_tokens') or 0)),
                    tr("**Estimated Cost** `{v0}`", v0=compact_value(runtime.get('estimated_cost_display'), '$0.000000')),
                ]
            )

        if alert_ref:
            lines.append(tr("**Alert Ref** `{v0}`", v0=alert_ref))

        if agent_run_id:
            lines.append(tr("**Agent Run ID** `{v0}`", v0=agent_run_id))

        lines.extend(
            [
                "",
                tr('### Safety Boundary'),
                tr('This is the sanitized model answer grounded in bounded platform evidence. It did not execute SQL mutation, backfill, or remediation.'),
            ]
        )

        return join_lines(lines)

    lines = [
        tr('# 🤖 DQ Copilot'),
        tr('## Operator Answer'),
        "",
        tr('### Direct Answer'),
        answer,
        "",
    ]

    if alert_ref:
        lines.extend(
            [
                tr('### Alert Context'),
                f"Alert Ref `{alert_ref}`",
                tr("Previous Investigation Records `{v0}`", v0=incident_history_count),
                tr('Prior records are comparison context only, not proof of the current root cause.'),
                "",
            ]
        )

    lines.extend(
        [
        tr('### Question'),
        question,
        "",
        tr('### Guardrail'),
        tr('I can explain, summarize, and recommend next steps, but I will not execute remediation without approval.'),
        "",
        tr('### Suggested Next Command'),
        f"/dq triage alert_key:{alert_ref}"
        if alert_ref
        else tr('Use /dq alerts to select an Alert Ref, then run /dq triage.'),
        ]
    )

    technical_lines: list[str] = []

    if alert_key and not is_alert_ref(alert_key):
        technical_lines.append(f"System Alert Key {alert_key}")

    if transport:
        technical_lines.append(f"Copilot Transport {transport}")

    if agent_run_id:
        technical_lines.append(f"Agent Run ID {agent_run_id}")

    if technical_lines:
        lines.extend(["", tr('### Technical Reference'), tr('For debugging and internal joins only.'), *technical_lines])

    return join_lines(lines)


def format_backfill_preview(
    request_id: str,
    start_date: str,
    end_date: str,
    target_dag_id: str,
    reason: str,
    status: str = "pending",
    created_new: bool = True,
) -> str:
    """
    Format a durable backfill approval request message.

    Args:
        request_id: Human-readable request id.
        start_date: Inclusive start date.
        end_date: Inclusive end date.
        target_dag_id: Airflow DAG that would be triggered.
        reason: Human-readable reason for the request.
        status: Durable approval lifecycle status.
        created_new: Whether the API created a new request or reused an idempotent one.

    Returns:
        Discord Markdown durable approval request.
    """
    logger.info(
        "Formatting Discord backfill preview | request_id=%s start=%s end=%s target_dag_id=%s",
        request_id,
        start_date,
        end_date,
        target_dag_id,
    )

    lines = [
        tr('# 🔁 Backfill Approval Request'),
        tr('## Durable Approval Queue'),
        "",
        tr('### Quick Read'),
        tr('The proposed backfill is stored for human review. No remediation has been executed.'),
        "",
        tr('### Request Details'),
        tr("**Request ID** `{v0}`", v0=request_id),
        tr("**Status** `{v0}`", v0=status),
        tr("**Queue Result** `{v0}`", v0=tr('created') if created_new else tr('reused existing request')),
        tr("**Reason** {v0}", v0=reason),
        tr("**Target DAG** `{v0}`", v0=target_dag_id),
        tr("**Date Range** `{v0}` to `{v1}`", v0=start_date, v1=end_date),
        "",
        tr('### What Would Run After Separate Execution'),
        tr('- Daily pipeline will run once per date.'),
        tr('- Date partitions are expected to be replaced idempotently.'),
        "",
        tr('### Safety Check'),
        tr('No Airflow DAG was triggered. Creating this request only writes approval and audit state.'),
        "",
        tr('### Decision Commands'),
        f"/dq approve request_id:{request_id} comment:<review note>",
        f"/dq reject request_id:{request_id} comment:<review note>",
    ]

    return join_lines(lines)


def format_approval_recorded(
    request_id: str,
    approved_by: str,
    status: str = "approved",
    decision: str = "approve",
) -> str:
    """
    Format a durable approval decision without implying execution.

    Args:
        request_id: Request identifier from the preview command.
        approved_by: Discord user who made the decision.
        status: Durable approval status.
        decision: Approve or reject decision.

    Returns:
        Discord Markdown message for the durable approval decision.
    """
    logger.info("Formatting Discord approval result | request_id=%s status=%s", request_id, status)

    decision_label = tr('Approved') if decision == "approve" else tr('Rejected')

    lines = [
        tr("# \U0001f9fe Request {v0}", v0=decision_label),
        tr('## Durable Approval Decision'),
        "",
        tr('### What Happened'),
        tr('The human decision was stored in the approval queue and agent audit log.'),
        "",
        tr('### Approval Details'),
        tr("**Request ID** `{v0}`", v0=request_id),
        tr("**Decided By** `{v0}`", v0=approved_by),
        tr("**Decision** `{v0}`", v0=decision),
        tr("**Status** `{v0}`", v0=status),
        "",
        tr('### Safety Check'),
        tr('No Airflow DAG was triggered. No backfill, rerun, or data mutation was executed.'),
        "",
        tr('### Next Step'),
        (
            tr("An operator may run DAG `90_dag_dq_platform_backfill_dispatcher` with approval request `{v0}`.", v0=request_id)
            if status == "approved"
            else tr('The rejected request cannot authorize dispatcher execution.')
        ),
    ]

    return join_lines(lines)


def format_bot_online() -> str:
    """
    Format the bot startup notification.

    Returns:
        Discord Markdown bot online message.
    """
    lines = [
        tr('# ✅ DQ Bot Online'),
        tr('## Agentic Data Quality Triage'),
        "",
        tr('Commands are ready for alert lookup, triage, daily summary, and approval previews.'),
        "",
        tr('### Useful Commands'),
        "/dq alerts dt:<YYYY-MM-DD> status:open limit:10",
        "/dq triage alert_key:<Alert Ref>",
        "/dq daily_summary dt:<YYYY-MM-DD>",
        "/dq ask question:<question> alert_key:<Alert Ref>",
        "`/dq backfill_preview start_date:<YYYY-MM-DD> end_date:<YYYY-MM-DD>`",
    ]

    return join_lines(lines)


# --- Sanitizing Embed Content
def sanitize_discord_text(value: Any, limit: int) -> str:
    """Return bounded text that cannot create Discord mentions or control output."""
    normalized = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(value or "")).strip()
    normalized = normalized.replace("@", "@\u200b")

    if len(normalized) <= limit:
        return normalized

    return normalized[: max(0, limit - 1)].rstrip() + "…"


def _content_hash(payload: dict[str, Any]) -> str:
    """Build a stable hash for idempotent Discord message updates."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _short_issue_title(value: str) -> str:
    """Remove a redundant trailing business date from a card headline."""
    return re.sub(r"\s+(?:on|pada)\s+\d{4}-\d{2}-\d{2}\.?$", "", value.strip(), flags=re.IGNORECASE)


def _two_point_summary(value: str) -> str:
    """Keep an incident assessment to at most two scan-friendly statements."""
    normalized = re.sub(r"\s+", " ", value.strip())
    if not normalized:
        return ""

    sentences = [item.strip() for item in re.split(r"(?<=[.!?])\s+", normalized) if item.strip()]
    selected  = sentences[:2] or [normalized]

    return "\n".join(f"• {item}" for item in selected)


def _runtime_identity(
    runtime: dict[str, Any] | None,
    provider: str = "",
    model: str = "",
) -> tuple[str, str]:
    """Resolve one provider/model pair from route or aggregate runtime metadata."""
    values            = dict(runtime or {})
    resolved_provider = str(provider or values.get("provider") or "").strip()
    resolved_model    = str(model or values.get("model") or "").strip()

    if not resolved_provider:
        providers = values.get("providers") or []
        if isinstance(providers, list) and providers:
            resolved_provider = str(providers[-1] or "").strip()

    if not resolved_model:
        models = values.get("models") or []
        if isinstance(models, list) and models:
            resolved_model = str(models[-1] or "").strip()

    return resolved_provider, resolved_model


def _display_model_name(provider: str, model: str) -> str:
    """Build a readable provider/model label without inventing runtime identity."""
    normalized_provider = provider.strip().lower()
    normalized_model    = model.strip()

    if normalized_provider == "gemini":
        provider_label = "Gemini"
    elif provider.strip():
        provider_label = provider.strip().replace("_", " ").title()
    else:
        provider_label = tr('External model')

    if not normalized_model:
        return provider_label

    model_label = normalized_model
    if normalized_provider == "gemini" and normalized_model.lower().startswith("gemini-"):
        model_label = normalized_model[len("gemini-"):].replace("-", " ").title()

    if model_label.lower().startswith(provider_label.lower()):
        return model_label

    return f"{provider_label} {model_label}"


def provenance_footer(
    source: DiscordNarrativeSource,
    provider: str = "",
    model: str = "",
) -> str:
    """Return truthful, operator-facing AI provenance for one render."""
    if source == "external_copilot" and (provider or model):
        return tr("AI analysis \u00b7 {v0} \u00b7 Grounded in platform evidence", v0=_display_model_name(provider, model))

    if source == "external_copilot":
        return tr('AI analysis · External model · Grounded in platform evidence')

    if source == "heuristic_copilot":
        return tr('Heuristic analysis · No external model call')

    return tr('System summary · External AI unavailable')


def serialize_action_rows(action_rows: tuple[DiscordActionRow, ...]) -> list[dict[str, Any]]:
    """Serialize typed buttons into Discord REST API component rows."""
    serialized: list[dict[str, Any]] = []

    for row in action_rows[:5]:
        components = []

        for action in row.actions[:5]:
            label = sanitize_discord_text(action.label, DISCORD_COMPONENT_LABEL_LIMIT)
            if not label:
                continue

            component: dict[str, Any] = {
                "type": 2,
                "style": BUTTON_STYLE_VALUES[action.style],
                "label": label,
                "disabled": bool(action.disabled),
            }

            if action.emoji:
                component["emoji"] = {"name": sanitize_discord_text(action.emoji, 32)}

            if action.style == "link":
                if not re.match(r"^https?://", action.url.strip(), flags=re.IGNORECASE):
                    continue
                component["url"] = sanitize_discord_text(action.url, 512)
            else:
                if len(action.custom_id) > DISCORD_COMPONENT_CUSTOM_ID_LIMIT:
                    raise ValueError(tr('Discord component custom_id exceeds 100 characters.'))
                custom_id = sanitize_discord_text(action.custom_id, DISCORD_COMPONENT_CUSTOM_ID_LIMIT)
                if not custom_id:
                    continue
                component["custom_id"] = custom_id

            components.append(component)

        if components:
            serialized.append({"type": 1, "components": components})

    return serialized


def incident_action_rows(
    alert_ref: str,
    lifecycle: str,
    operator_ui_url: str = "",
    has_report: bool = False,
) -> tuple[DiscordActionRow, ...]:
    """Build safe incident actions; resolved incidents remain read-only."""
    resolved = lifecycle == "RESOLVED"
    actions = [] if resolved else [
        DiscordActionSpec("investigate", tr("Investigate"), "primary",
                          custom_id=f"dq:v1:investigate:{alert_ref}", emoji="\U0001F50D"),
    ]
    actions.append(DiscordActionSpec("evidence", tr("Evidence"), "secondary",
                                    custom_id=f"dq:v1:evidence:{alert_ref}", emoji="\U0001F4CE"))
    if resolved and has_report:
        actions.append(DiscordActionSpec("report", tr("View Report"), "secondary",
                                        custom_id=f"dq:v1:report:{alert_ref}", emoji="\U0001F4C4"))
    if not resolved:
        actions.append(DiscordActionSpec("ask", tr("Ask AI"), "secondary",
                                        custom_id=f"dq:v1:ask:{alert_ref}", emoji="\U0001F4AC"))
    navigation = []
    if operator_ui_url.strip():
        navigation.append(
            DiscordActionSpec(
                action_id="workspace",
                label=tr("Open Workspace"),
                style="link",
                url=operator_ui_url.strip(),
            )
        )

    if not resolved:
        navigation.append(DiscordActionSpec("resolve", tr("Resolve"), "secondary",
                                            custom_id=f"dq:v1:resolve:{alert_ref}"))
    rows = [DiscordActionRow(tuple(actions))]
    if navigation:
        rows.append(DiscordActionRow(tuple(navigation)))
    return tuple(rows)


def triage_action_rows(
    alert_ref: str,
    has_report: bool = False,
    lifecycle: str = "TRIAGED",
    operator_ui_url: str = "",
) -> tuple[DiscordActionRow, ...]:
    """Build follow-up actions for a completed triage result."""
    if lifecycle == "RESOLVED":
        return incident_action_rows(alert_ref, lifecycle, operator_ui_url, has_report)
    first_row = [
        DiscordActionSpec("reinvestigate", tr("Run New Investigation"), "primary",
                          custom_id=f"dq:v1:reinvestigate:{alert_ref}", emoji="\U0001F50D"),
        DiscordActionSpec("evidence", tr("Evidence"), "secondary",
                          custom_id=f"dq:v1:evidence:{alert_ref}", emoji="\U0001F4CE"),
        DiscordActionSpec("ask", tr("Ask AI"), "secondary",
                          custom_id=f"dq:v1:ask:{alert_ref}", emoji="\U0001F4AC"),
    ]
    second_row = []
    if has_report:
        first_row.append(
            DiscordActionSpec(
                action_id="report",
                label=tr("View Report"),
                style="secondary",
                custom_id=f"dq:v1:report:{alert_ref}",
            )
        )

    if operator_ui_url.strip():
        second_row.append(DiscordActionSpec("workspace", tr("Open Workspace"), "link", url=operator_ui_url.strip()))
    second_row.extend([
        DiscordActionSpec("resolve", tr("Resolve"), "secondary", custom_id=f"dq:v1:resolve:{alert_ref}"),
        DiscordActionSpec("draft", tr("Draft Action"), "secondary", custom_id=f"dq:v1:draft:{alert_ref}"),
    ])
    return (DiscordActionRow(tuple(first_row)), DiscordActionRow(tuple(second_row)))


def copilot_action_rows(alert_ref: str) -> tuple[DiscordActionRow, ...]:
    """Build lightweight follow-up controls for one grounded Copilot answer."""
    return (
        DiscordActionRow(
            actions=(
                DiscordActionSpec(
                    action_id="ask",
                    label=tr('Ask Follow-up'),
                    style="primary",
                    custom_id=f"dq:v1:ask:{alert_ref}",
                ),
                DiscordActionSpec(
                    action_id="evidence",
                    label=tr('Evidence'),
                    style="secondary",
                    custom_id=f"dq:v1:evidence:{alert_ref}",
                ),
            )
        ),
    )


def approval_action_rows(request_id: str, status: str) -> tuple[DiscordActionRow, ...]:
    """Build approval controls without exposing an execution button."""
    actions = [
        DiscordActionSpec(
            action_id="view_plan",
            label=tr('View Plan'),
            style="secondary",
            custom_id=f"dq:v1:view_plan:{request_id}",
        )
    ]

    if status.strip().lower() == "pending":
        actions.extend(
            [
                DiscordActionSpec(
                    action_id="approve",
                    label=tr('Approve'),
                    style="success",
                    custom_id=f"dq:v1:approve:{request_id}",
                ),
                DiscordActionSpec(
                    action_id="reject",
                    label=tr('Reject'),
                    style="danger",
                    custom_id=f"dq:v1:reject:{request_id}",
                ),
            ]
        )

    return (DiscordActionRow(actions=tuple(actions)),)


def _narrative_source(
    narrative: str,
    llm_runtime: dict[str, Any] | None = None,
    explicit_source: str = "",
) -> DiscordNarrativeSource:
    """Classify narrative provenance without allowing it to modify incident facts."""
    normalized_source = explicit_source.strip().lower()
    runtime           = llm_runtime or {}
    runtime_mode      = str(runtime.get("runtime_mode") or "").lower()
    provider          = str(runtime.get("provider") or "").strip().lower()
    used_heuristic    = runtime.get("used_heuristic")
    external_model_used = bool(runtime.get("external_model_used"))

    if narrative.strip() and normalized_source in {"heuristic", "heuristic_fallback"}:
        return "heuristic_copilot"

    if narrative.strip() and (
        normalized_source == "external_model"
        or runtime_mode in {"external_model", "mixed"}
        or external_model_used
        or (provider not in {"", "heuristic", "local"} and used_heuristic is False)
    ):
        return "external_copilot"

    if narrative.strip():
        return "heuristic_copilot"

    return "deterministic_fallback"


def _embed_result(
    *,
    title: str,
    description: str,
    fields: list[tuple[str, str, bool]],
    color: int,
    narrative_source: DiscordNarrativeSource,
    fallback_markdown: str,
    footer: str | None = None,
    lifecycle: str | None = None,
    provider: str = "",
    model: str = "",
    action_rows: tuple[DiscordActionRow, ...] = (),
) -> DiscordRenderResult:
    """Build a bounded Discord embed and its stable delivery hash."""
    bounded_title       = sanitize_discord_text(title, DISCORD_EMBED_TITLE_LIMIT)
    bounded_description = (
        sanitize_discord_text(description, DISCORD_EMBED_DESCRIPTION_LIMIT)
        or tr('No narrative is available.')
    )
    bounded_fields      = []

    for name, value, inline in fields[:DISCORD_EMBED_FIELD_LIMIT]:
        bounded_fields.append(
            {
                "name": sanitize_discord_text(name, DISCORD_EMBED_FIELD_NAME_LIMIT),
                "value": sanitize_discord_text(value, DISCORD_EMBED_FIELD_VALUE_LIMIT) or "N/A",
                "inline": bool(inline),
            }
        )

    footer_text = sanitize_discord_text(tr("Data Reliability Copilot") if footer is None else footer, 2048)
    total_size  = (
        len(bounded_title)
        + len(bounded_description)
        + len(footer_text)
        + sum(len(item["name"]) + len(item["value"]) for item in bounded_fields)
    )

    if total_size > DISCORD_EMBED_TOTAL_LIMIT:
        overflow = total_size - DISCORD_EMBED_TOTAL_LIMIT
        reducible = max(0, len(bounded_description) - 1)
        reduction = min(overflow, reducible)
        bounded_description = sanitize_discord_text(
            bounded_description,
            max(1, len(bounded_description) - reduction),
        )
        overflow -= reduction

        for field in reversed(bounded_fields):
            if overflow <= 0:
                break
            reducible = max(0, len(field["value"]) - 1)
            reduction = min(overflow, reducible)
            field["value"] = sanitize_discord_text(
                field["value"],
                max(1, len(field["value"]) - reduction),
            )
            overflow -= reduction

        if overflow > 0:
            raise ValueError(tr('Discord embed metadata exceeds the 6000-character limit.'))

    embed = {
        "title": bounded_title,
        "description": bounded_description,
        "color": int(color),
        "fields": bounded_fields,
        "footer": {"text": footer_text},
    }
    payload = {
        "content": "",
        "embeds": [embed],
        "allowed_mentions": {"parse": []},
    }

    components = serialize_action_rows(action_rows)
    if components:
        payload["components"] = components

    hash_input = {
        "payload": payload,
        "fallback_markdown": fallback_markdown,
    }

    return DiscordRenderResult(
        payload=payload,
        narrative_source=narrative_source,
        content_hash=_content_hash(hash_input),
        fallback_markdown=fallback_markdown,
        lifecycle=lifecycle,
        provider=provider,
        model=model,
        action_rows=action_rows,
    )


# --- Building Incident Contracts
def build_discord_incident_payload(
    alert: Any,
    narrative: str = "",
    llm_runtime: dict[str, Any] | None = None,
    explicit_source: str = "",
    approval_state: str = "",
    report_id: str = "",
    agent_run_id: str = "",
    report_uri: str = "",
    transport: str = "",
) -> DiscordIncidentPayload:
    """Build one incident contract while keeping deterministic facts immutable."""
    details        = parse_json_object(get_model_value(alert, "details", None))
    if not details:
        details = parse_json_object(get_model_value(alert, "details_json", None))

    alert_key      = compact_value(get_model_value(alert, "alert_key"), "unknown-alert")
    alert_ref      = resolve_alert_ref(alert)
    severity       = compact_value(get_model_value(alert, "severity"), "info").lower()
    raw_status     = compact_value(get_model_value(alert, "status"), "open").lower().replace(" ", "_")
    resolved_approval_state = compact_value(
        approval_state or details.get("approval_state"),
        "not_required",
    ).lower()
    lifecycle_key  = raw_status if raw_status == "resolved" else (
        resolved_approval_state
        if resolved_approval_state in {"pending", "approved", "rejected"}
        else raw_status
    )
    lifecycle      = LIFECYCLE_LABELS.get(lifecycle_key, "OPEN")
    table_name     = compact_value(get_model_value(alert, "table_name"))
    affected_date  = compact_value(get_model_value(alert, "dt"))
    issue_title    = build_alert_title(alert)
    what_changed   = build_alert_one_liner(alert)
    impact         = compact_value(
        details.get("impact"),
        tr("Data products that depend on {v0} may be incomplete or stale.", v0=table_name),
    )
    next_action    = compact_value(
        details.get("recommended_action"),
        tr("Run /dq triage alert_key:{v0} before approving any remediation.", v0=alert_ref),
    )
    fallback       = build_alert_one_liner(alert)
    source         = _narrative_source(narrative, llm_runtime, explicit_source)
    bounded_text   = narrative.strip() or fallback
    runtime        = dict(llm_runtime or {})
    provider, model = _runtime_identity(runtime)
    report_id = compact_value(report_id or details.get("report_id"), "")
    report_uri = compact_value(
        report_uri or get_model_value(alert, "report_s3_uri", "")
        or details.get("report_uri") or details.get("markdown_report_s3_uri"), "",
    )

    payload_type = ResolvedDiscordIncidentPayload if lifecycle == "RESOLVED" else DiscordIncidentPayload
    extra = {}
    if lifecycle == "RESOLVED":
        resolution = parse_json_object(get_model_value(alert, "resolution", None)) or parse_json_object(details.get("resolution"))
        extra["resolution"] = resolution
        extra["scope"] = {
            "kind": compact_value(get_model_value(alert, "alert_type"), "dq_failure"),
            "table_name": table_name, "dt": affected_date,
        }
    return payload_type(
        facts=DiscordIncidentFacts(
            alert_key=alert_key,
            alert_ref=alert_ref,
            severity=severity,
            lifecycle=lifecycle,
            issue_title=_short_issue_title(issue_title),
            what_changed=what_changed,
            table_name=table_name,
            affected_date=affected_date,
            impact=impact,
            recommended_action=next_action,
            command_hint=f"/dq triage alert_key:{alert_ref}",
            approval_state=resolved_approval_state,
        ),
        narrative=DiscordNarrative(text=bounded_text, source=source),
        report_id=report_id,
        agent_run_id=agent_run_id,
        report_uri=report_uri,
        provider=provider,
        model=model,
        transport=transport,
        **extra,
    )


# --- Rendering Compact Incident Embeds
def render_incident_embed(
    incident: DiscordIncidentPayload,
    operator_ui_url: str | None = None,
) -> DiscordRenderResult:
    """Render one compact lifecycle-aware incident card."""
    if operator_ui_url is None:
        operator_ui_url = os.getenv("DISCORD_OPERATOR_UI_URL", "").strip()
    facts = incident.facts
    if facts.lifecycle == "RESOLVED":
        return render_resolved_incident_embed(
            alert_ref=facts.alert_ref,
            scope=getattr(incident, "scope", {"table_name": facts.table_name, "dt": facts.affected_date}),
            resolution=getattr(incident, "resolution", {}),
            issue_title=facts.issue_title,
            operator_ui_url=operator_ui_url,
            has_report=bool(incident.report_uri or incident.report_id),
        )
    color = DISCORD_COLORS.get(facts.severity, DISCORD_COLORS["neutral"])
    assessment_label = {
        "external_copilot": tr('AI assessment'),
        "heuristic_copilot": tr('Heuristic assessment'),
        "deterministic_fallback": tr('System assessment'),
    }[incident.narrative.source]
    fallback_markdown = "\n".join(
        [
            f"**{facts.issue_title}**",
            f"**{_state_label(facts.severity)} · {_state_label(facts.lifecycle)}** | `{facts.alert_ref}`",
            f"`{facts.table_name}` · `{facts.affected_date}`",
            tr("**What changed** {v0}", v0=facts.what_changed),
            tr("**Why it matters** {v0}", v0=facts.impact),
            f"**{assessment_label}** {incident.narrative.text}",
            tr("**Next best action** {v0}", v0=facts.recommended_action),
        ]
    )
    fields = [
        (tr('Severity / Status'), f"{_state_label(facts.severity)} · {_state_label(facts.lifecycle)}", True),
        (tr('Alert Ref'), f"`{facts.alert_ref}`", True),
        (tr('Table / Date'), f"`{facts.table_name}` · `{facts.affected_date}`", False),
        (tr('What changed'), facts.what_changed, False),
        (tr('Why it matters'), facts.impact, False),
    ]

    if incident.narrative.text.strip() and incident.narrative.text.strip() != facts.what_changed.strip():
        fields.append((assessment_label, _two_point_summary(incident.narrative.text), False))

    fields.append((tr('Next best action'), facts.recommended_action, False))
    actions = incident_action_rows(
        alert_ref=facts.alert_ref,
        lifecycle=facts.lifecycle,
        operator_ui_url=operator_ui_url,
    )

    return _embed_result(
        title=facts.issue_title,
        description=tr('Scan the facts below, then investigate before approving any data-changing action.'),
        fields=fields,
        color=color,
        narrative_source=incident.narrative.source,
        fallback_markdown=fallback_markdown,
        footer=provenance_footer(
            incident.narrative.source,
            incident.provider,
            incident.model,
        ),
        lifecycle=facts.lifecycle,
        provider=incident.provider,
        model=incident.model,
        action_rows=actions,
    )


def render_alert_list_embed(
    alerts: list[dict[str, Any]],
    status: str,
    dt: str | None,
    assistant_note: str = "",
    data_transport: str = "",
    llm_runtime: dict[str, Any] | None = None,
) -> DiscordRenderResult:
    """Render a compact alert inbox with Copilot narrative first."""
    visible = sorted(
        alerts,
        key=lambda item: (
            SEVERITY_ORDER.get(str(item.get("severity") or "info").lower(), 99),
            str(item.get("created_at") or ""),
        ),
    )[:MAX_ALERT_ITEMS]
    fallback = (
        tr('No matching incidents are active.')
        if not alerts
        else tr("{v0} incidents match the current filters. Start with the highest-severity item.", v0=len(alerts))
    )
    runtime     = dict(llm_runtime or {})
    source      = _narrative_source(assistant_note, runtime)
    provider, model = _runtime_identity(runtime)
    description = assistant_note.strip() or fallback
    fields      = []

    for alert in visible:
        incident = build_discord_incident_payload(alert)
        facts    = incident.facts
        fields.append(
            (
                f"{_state_label(facts.severity)} · {facts.alert_ref}",
                f"{facts.issue_title}\n`{facts.table_name}` · `{facts.affected_date}`",
                False,
            )
        )

    fields.append((tr('Start here'), "`/dq triage alert_key:<Alert Ref>`", False))
    scope = f"status={status}" + (f" · date={dt}" if dt else "")

    return _embed_result(
        title=tr('Data Quality Incident Inbox'),
        description=description,
        fields=fields,
        color=DISCORD_COLORS["critical"] if alerts else DISCORD_COLORS["success"],
        narrative_source=source,
        fallback_markdown=format_alert_list(alerts, status, dt, assistant_note, data_transport),
        footer=f"{provenance_footer(source, provider, model)} · {scope}",
        provider=provider,
        model=model,
    )


def render_triage_embed(
    report: Any,
    assistant_note: str = "",
    execution_transport: str = "",
    narrative_transport: str = "",
    operator_ui_url: str | None = None,
) -> DiscordRenderResult:
    """Render grounded RCA, uncertainty, and one safe next action."""
    if operator_ui_url is None:
        operator_ui_url = os.getenv("DISCORD_OPERATOR_UI_URL", "").strip()
    alert              = get_model_value(report, "alert")
    ai_investigation   = get_model_value(report, "ai_investigation")
    report_llm_runtime = get_model_value(report, "llm_runtime")
    runtime = (
        report_llm_runtime.model_dump(mode="json")
        if hasattr(report_llm_runtime, "model_dump")
        else dict(report_llm_runtime or {})
    )
    what_happened = compact_value(
        get_model_value(ai_investigation, "what_happened"),
        assistant_note or get_model_value(report, "summary", tr('Triage completed.')),
    )
    source = _narrative_source(
        what_happened if ai_investigation is not None else assistant_note,
        runtime,
        compact_value(get_model_value(ai_investigation, "source"), ""),
    )
    incident = build_discord_incident_payload(
        alert,
        narrative=what_happened,
        llm_runtime=runtime,
        explicit_source=compact_value(get_model_value(ai_investigation, "source"), ""),
        report_id=compact_value(get_model_value(report, "report_id"), ""),
        agent_run_id=compact_value(get_model_value(report, "agent_run_id"), ""),
        report_uri=compact_value(get_model_value(report, "markdown_report_s3_uri"), ""),
        transport=execution_transport,
    )
    if incident.facts.lifecycle == "RESOLVED":
        return render_incident_embed(incident, operator_ui_url)
    provider, model = _runtime_identity(
        runtime,
        provider=compact_value(get_model_value(ai_investigation, "provider"), ""),
        model=compact_value(get_model_value(ai_investigation, "model"), ""),
    )
    top_hypothesis = get_model_value(report, "top_hypothesis")
    confidence     = float(get_model_value(report, "confidence", 0.0) or 0.0)
    uncertainty    = compact_value(
        get_model_value(ai_investigation, "uncertainty"),
        tr('Evidence supports a leading hypothesis but does not prove every upstream condition.'),
    )
    next_step = compact_value(
        get_model_value(ai_investigation, "recommended_next_step"),
        get_model_value(top_hypothesis, "recommended_action", incident.facts.recommended_action),
    )
    evidence = list(get_model_value(ai_investigation, "evidence_reasoning", []) or [])

    if not evidence:
        evidence = [
            compact_value(get_model_value(item, "summary"))
            for item in list(get_model_value(report, "evidence", []) or [])[:3]
        ]

    fields = [
        (tr('Confidence'), f"{confidence:.0%}", True),
        (tr('Alert Ref'), f"`{incident.facts.alert_ref}`", True),
        (
            tr('Table / Date'),
            f"`{incident.facts.table_name}` · `{incident.facts.affected_date}`",
            False,
        ),
        (tr('Evidence'), "\n".join(f"• {item}" for item in evidence[:3]) or tr('No evidence returned.'), False),
        (
            tr('Likely impact'),
            compact_value(get_model_value(ai_investigation, "likely_impact"), get_model_value(report, "impact")),
            False,
        ),
        (tr('Still uncertain'), uncertainty, False),
        (tr('Recommended next step'), next_step, False),
    ]
    actions = triage_action_rows(
        alert_ref=incident.facts.alert_ref,
        has_report=bool(incident.report_uri or incident.report_id),
        lifecycle=incident.facts.lifecycle,
        operator_ui_url=operator_ui_url,
    )
    if execution_transport == "stored_report":
        fields.insert(0, (tr('Report context'), tr('Saved investigation, not a new check. Use Resolve -> Check Again for fresh closure evidence.'), False))

    return _embed_result(
        title=tr("\U0001F50D Investigation · {issue_title}", issue_title=incident.facts.issue_title),
        description=tr("**Likely cause**\n{v0}", v0=incident.narrative.text),
        fields=fields,
        color=DISCORD_COLORS.get(incident.facts.severity, DISCORD_COLORS["neutral"]),
        narrative_source=source,
        fallback_markdown=format_triage_result(
            report,
            assistant_note,
            execution_transport,
            narrative_transport,
        ),
        footer=(tr('Saved report · No new model call') + " · " if execution_transport == "stored_report" else "") + provenance_footer(source, provider, model),
        lifecycle="RESOLVED" if incident.facts.lifecycle == "RESOLVED" else "TRIAGED",
        provider=provider,
        model=model,
        action_rows=actions,
    )


def render_daily_summary_embed(
    dt: str,
    check_rows: list[dict[str, Any]],
    alert_rows: list[dict[str, Any]],
    assistant_note: str = "",
    data_transport: str = "",
    llm_runtime: dict[str, Any] | None = None,
) -> DiscordRenderResult:
    """Render one scan-friendly daily quality summary."""
    checks = {str(row.get("status") or "unknown").lower(): int(row.get("count") or 0) for row in check_rows}
    alerts = {str(row.get("severity") or "unknown").lower(): int(row.get("count") or 0) for row in alert_rows}
    total_checks = sum(checks.values())
    total_alerts = sum(alerts.values())
    runtime      = dict(llm_runtime or {})
    source       = _narrative_source(assistant_note, runtime)
    provider, model = _runtime_identity(runtime)
    fallback     = (
        tr("{v0} checks completed with {v1} active alerts.", v0=total_checks, v1=total_alerts)
        if total_checks or total_alerts
        else tr('No daily quality results are available for this date.')
    )
    fields = [
        (tr('Checks'), tr("{v0} total \u00b7 {v1} passed \u00b7 {v2} failed", v0=total_checks, v1=checks.get('pass', 0), v2=checks.get('fail', 0)), False),
        (tr('Incidents'), tr("{v0} total \u00b7 {v1} critical \u00b7 {v2} warning", v0=total_alerts, v1=alerts.get('critical', 0), v2=alerts.get('warning', 0)), False),
        (tr('Next'), "`/dq alerts status:active`", False),
    ]

    return _embed_result(
        title=tr("Daily Data Quality \u00b7 {v0}", v0=dt),
        description=assistant_note.strip() or fallback,
        fields=fields,
        color=DISCORD_COLORS["critical"] if total_alerts else DISCORD_COLORS["success"],
        narrative_source=source,
        fallback_markdown=format_daily_summary(dt, check_rows, alert_rows, assistant_note, data_transport),
        footer=provenance_footer(source, provider, model),
        provider=provider,
        model=model,
    )


def render_quality_summary_embed(
    payload: dict[str, Any],
    assistant_note: str = "",
    data_transport: str = "",
    llm_runtime: dict[str, Any] | None = None,
) -> DiscordRenderResult:
    """Render weekly, table, and warehouse summaries without Markdown blasts."""
    scope        = compact_value(payload.get("scope"), "quality").lower()
    start_date   = compact_value(payload.get("start_date"))
    end_date     = compact_value(payload.get("end_date"))
    table_name   = compact_value(payload.get("table_name"), "")
    check_counts = {
        str(row.get("status") or "").lower(): int(row.get("count") or 0)
        for row in list(payload.get("check_counts") or [])
    }
    alert_counts = {
        str(row.get("severity") or "").lower(): int(row.get("count") or 0)
        for row in list(payload.get("alert_counts") or [])
    }
    table_counts = sorted(
        list(payload.get("table_counts") or []),
        key=lambda row: (
            -int(row.get("critical_alerts") or 0),
            -int(row.get("failed_checks") or 0),
            str(row.get("table_name") or ""),
        ),
    )
    runtime         = dict(llm_runtime or {})
    source          = _narrative_source(assistant_note, runtime)
    provider, model = _runtime_identity(runtime)
    failed          = check_counts.get("fail", 0)
    critical        = alert_counts.get("critical", 0)
    warnings        = check_counts.get("warn", 0)
    health          = tr('Needs attention') if failed or critical else tr('Review recommended') if warnings else tr('Healthy')
    title           = {
        "weekly": tr('Weekly data reliability'),
        "table": tr('Table data reliability'),
        "database": tr('Warehouse data reliability'),
    }.get(scope, tr('Data reliability summary'))
    subject = f"`{table_name}`" if scope == "table" else tr("`{v0}` to `{v1}`", v0=start_date, v1=end_date)
    affected = "\n".join(
        tr("\u2022 `{v0}` \u00b7 {v1} failed \u00b7 {v2} critical", v0=compact_value(row.get('table_name')), v1=int(row.get('failed_checks') or 0), v2=int(row.get('critical_alerts') or 0))
        for row in table_counts[:3]
    ) or tr('No affected table was identified in this bounded summary.')
    suggestions = list(payload.get("rerun_suggestions") or [])
    action_text = (
        tr('Review the highest-priority incident before drafting an approval-gated rerun.')
        if suggestions
        else tr('No allowlisted rerun can be inferred from this aggregate alone; inspect an incident first.')
    )
    fields = [
        (tr('Scope'), subject, True),
        (tr('Health'), health, True),
        (
            tr('Checks'),
            tr("{v0} passed \u00b7 {v1} warning \u00b7 {v2} failed", v0=check_counts.get('pass', 0), v1=warnings, v2=failed),
            False,
        ),
        (
            tr('Active incidents'),
            tr("{v0} critical \u00b7 {v1} warning", v0=critical, v1=alert_counts.get('warning', 0)),
            False,
        ),
        (tr('Most affected'), affected, False),
        (tr('Next best action'), action_text, False),
    ]
    fallback = format_quality_summary(
        payload=payload,
        assistant_note=assistant_note,
        data_transport=data_transport,
    )

    return _embed_result(
        title=title,
        description=assistant_note.strip() or tr("{v0} for the selected {v1} scope.", v0=health, v1=scope),
        fields=fields,
        color=DISCORD_COLORS["critical"] if failed or critical else DISCORD_COLORS["warning"] if warnings else DISCORD_COLORS["success"],
        narrative_source=source,
        fallback_markdown=fallback,
        footer=provenance_footer(source, provider, model),
        provider=provider,
        model=model,
    )


def render_evidence_embed(context: dict[str, Any]) -> DiscordRenderResult:
    """Render deterministic evidence facts for an ephemeral interaction response."""
    alert_ref = compact_value(context.get("alert_ref"))
    fields = [
        (tr('Alert Ref'), f"`{alert_ref}`", True),
        (tr('Status'), _state_label(context.get("status")), True),
        (
            tr('Table / Date'),
            f"`{compact_value(context.get('table_name'))}` · `{compact_value(context.get('affected_date'))}`",
            False,
        ),
        (tr('Check'), f"`{compact_value(context.get('metric'))}`", True),
        (
            tr('Observed / Expected'),
            f"`{compact_value(context.get('observed_value'))}` / `{compact_value(context.get('expected_value'))}`",
            True,
        ),
        (tr('Evidence summary'),
         (_resolution_summary(context.get("summary")) if str(context.get("status") or "").lower() == "resolved"
          else compact_value(context.get("summary"), tr('No bounded evidence summary is available.')))
         or tr('No bounded evidence summary is available.'), False),
        (tr('Report'), tr('Available') if context.get("report_uri") else tr('Not generated yet'), True),
    ]

    return _embed_result(
        title=tr("\U0001F4CE Evidence · Incident Closure") if str(context.get("status") or "").lower() == "resolved" else tr("\U0001F4CE Evidence"),
        description=(
            tr('Closure evidence at check time, not the original failure. Resolve does not repair data.')
            if str(context.get("status") or "").lower() == "resolved"
            else tr('Read-only facts loaded from the data reliability control plane.')
        ),
        fields=fields,
        color=DISCORD_COLORS["neutral"],
        narrative_source="deterministic_fallback",
        fallback_markdown="\n".join(f"{name}: {value}" for name, value, _ in fields),
        footer=tr('System evidence · No model call'),
    )


def render_report_reference_embed(context: dict[str, Any]) -> DiscordRenderResult:
    """Render a sanitized persisted-report reference without exposing internal debug data."""
    report_uri = compact_value(context.get("report_uri"), tr('Not generated yet'))
    from urllib.parse import urlparse
    parsed = urlparse(report_uri)
    locator_fields = [(tr('Artifact'), f"```text\n{report_uri}\n```", False)]
    if parsed.scheme == "s3" and parsed.netloc == "dq-artifacts" and not parsed.query and not parsed.fragment:
        locator_fields.extend([
            (tr('Bucket'), f"```text\n{parsed.netloc}\n```", True),
            (tr('Object key'), f"```text\n{parsed.path.lstrip('/')}\n```", False),
        ])
    return _embed_result(
        title=tr("\U0001F4C4 Investigation Report"),
        description=(
            tr('The report is stored as a controlled artifact. Use the operator workspace or S3-compatible artifact browser to inspect it.')
        ),
        fields=[
            (tr('Alert Ref'), f"`{compact_value(context.get('alert_ref'))}`", True),
            (tr('Report ID'), f"`{compact_value(context.get('report_id'))}`", True),
        ] + locator_fields,
        color=DISCORD_COLORS["info"],
        narrative_source="deterministic_fallback",
        fallback_markdown=tr("Report for {v0}: {v1}", v0=compact_value(context.get('alert_ref')), v1=report_uri),
        footer=tr('Persisted artifact reference · No model call'),
    )


def render_approval_plan_embed(payload: dict[str, Any]) -> DiscordRenderResult:
    """Render authoritative approval scope for an ephemeral View Plan response."""
    request_id = compact_value(payload.get("request_id"))
    status     = compact_value(payload.get("status"), "unknown")
    return _embed_result(
        title=tr("Approval plan \u00b7 {v0}", v0=request_id),
        description=compact_value(payload.get("reason"), tr('No operator reason was recorded.')),
        fields=[
            (tr('Status'), _state_label(status), True),
            (tr('Action'), compact_value(payload.get("action_type"), "backfill"), True),
            (tr('Target DAG'), f"`{compact_value(payload.get('target_dag_id'))}`", False),
            (
                tr('Date range'),
                tr("`{v0}` to `{v1}`", v0=compact_value(payload.get('start_date')), v1=compact_value(payload.get('end_date'))),
                False,
            ),
            (tr('Execution boundary'), tr('This approval state does not execute Airflow or mutate data.'), False),
        ],
        color=DISCORD_COLORS["warning"],
        narrative_source="deterministic_fallback",
        fallback_markdown=tr("Approval {v0}: {v1}", v0=request_id, v1=status),
        footer=tr('Authoritative approval scope · No execution'),
    )


def render_operator_answer_embed(
    *,
    answer: str,
    alert_key: str = "",
    transport: str = "",
    agent_run_id: str = "",
    llm_runtime: dict[str, Any] | None = None,
) -> DiscordRenderResult:
    """Render the direct Copilot answer without repeated template sections."""
    runtime = dict(llm_runtime or {})
    source  = _narrative_source(answer, runtime)
    fields  = []
    provider, model = _runtime_identity(runtime)
    alert_ref = alert_key if is_alert_ref(alert_key) else build_alert_ref(alert_key) if alert_key else ""

    if alert_ref:
        fields.append((tr('Alert Ref'), f"`{alert_ref}`", True))
    else:
        fields.append((tr('Context'), tr('Platform overview - no incident selected'), False))

    action_rows = copilot_action_rows(alert_ref) if alert_ref else ()

    return _embed_result(
        title=tr('AI investigation answer') if source == "external_copilot" else tr('Data reliability answer'),
        description=answer,
        fields=fields,
        color=DISCORD_COLORS["info"],
        narrative_source=source,
        fallback_markdown=format_operator_answer(
            question="",
            answer=answer,
            alert_key=alert_key,
            transport=transport,
            agent_run_id=agent_run_id,
            response_mode="direct",
            llm_runtime=runtime,
        ),
        footer=provenance_footer(source, provider, model),
        provider=provider,
        model=model,
        action_rows=action_rows,
    )


def render_approval_embed(
    *,
    request_id: str,
    status: str,
    summary: str,
    next_step: str,
    target_dag_id: str = "",
    start_date: str = "",
    end_date: str = "",
    risk_level: str = "HIGH",
    validation_plan: str | None = None,
    rollback_plan: str | None = None,
) -> DiscordRenderResult:
    """Render approval state without implying Airflow execution."""
    if validation_plan is None:
        validation_plan = tr("Re-run DQ checks and verify the affected partition before closing the incident.")
    if rollback_plan is None:
        rollback_plan = tr(
            "No automatic rollback is implemented. If execution is dispatched later, recovery requires "
            "a separate operator decision using the documented Airflow runbook."
        )
    normalized_status = status.strip().lower()
    lifecycle         = LIFECYCLE_LABELS.get(normalized_status, "WAITING APPROVAL")
    color             = (
        DISCORD_COLORS["success"]
        if lifecycle == "APPROVED"
        else DISCORD_COLORS["critical"]
        if lifecycle == "REJECTED"
        else DISCORD_COLORS["warning"]
    )

    fields = [
        (tr('Status'), _state_label(lifecycle), True),
        (tr('Risk'), _state_label(risk_level), True),
    ]

    if target_dag_id:
        fields.append((tr('Proposed scope'), tr("`{v0}`\n`{v1}` to `{v2}`", v0=target_dag_id, v1=start_date or 'N/A', v2=end_date or 'N/A'), False))

    fields.extend(
        [
            (tr('Validation plan'), validation_plan, False),
            (tr('Recovery boundary'), rollback_plan, False),
            (tr('Next step'), next_step, False),
        ]
    )

    return _embed_result(
        title=tr("Approval \u00b7 {v0}", v0=request_id),
        description=summary,
        fields=fields,
        color=color,
        narrative_source="deterministic_fallback",
        fallback_markdown=summary,
        footer=tr('Human approval boundary'),
        lifecycle=lifecycle,
        action_rows=approval_action_rows(request_id=request_id, status=normalized_status),
    )


def render_debug_embed(debug: dict[str, Any]) -> DiscordRenderResult:
    """Render sanitized technical identifiers for an ephemeral debug response."""
    fields = [
        (tr('Alert Ref'), f"`{compact_value(debug.get('alert_ref'))}`", True),
        (tr('System Alert Key'), f"`{compact_value(debug.get('alert_key'))}`", False),
        (tr('Agent Run ID'), f"`{compact_value(debug.get('agent_run_id'))}`", False),
        (tr('Report ID'), f"`{compact_value(debug.get('report_id'))}`", True),
        (tr('Report URI'), f"`{compact_value(debug.get('report_uri'))}`", False),
        (
            tr('Runtime'),
            f"`{compact_value(debug.get('provider'), 'local')} / {compact_value(debug.get('model'), 'fallback')}`",
            False,
        ),
        (tr('Transport'), f"`{compact_value(debug.get('transport'), 'local')}`", True),
    ]

    return _embed_result(
        title=tr('DQ Debug Context'),
        description=tr('Technical correlation data for operator troubleshooting.'),
        fields=fields,
        color=DISCORD_COLORS["neutral"],
        narrative_source="deterministic_fallback",
        fallback_markdown="\n".join(f"{name}: {value}" for name, value, _ in fields),
        footer=tr('Ephemeral · no model call'),
    )


# --- Rendering Evidence-Gated Resolution
def _resolution_summary(value: Any) -> str:
    """Map only known system-result copy, never reports, operator reasons or evidence."""
    text = str(value or "")
    for english in (
        "Check passed. The issue was not found in the latest check.",
        "Closure blocked. Problems remain or check evidence is incomplete.",
        "Closure evidence is not available on this card. Open the history in Workspace.",
    ):
        if text in {english, MESSAGES[english]}:
            return tr(english)
    return text


def _resolution_blocker_text(value: Any) -> str:
    """Translate fixed backend blockers while preserving their request identifiers."""
    text = str(value)
    for english in (
        "Check unavailable: the Airflow connection is not configured.",
        "Check again after remediation finishes.",
    ):
        if text in {english, MESSAGES[english]}:
            return tr(english)
    match = re.fullmatch(r"Remediation ([A-Za-z0-9-]{1,100}) belum selesai atau belum dibatalkan\.", text)
    if match:
        return tr("Remediation {request_id} is incomplete or not cancelled.", request_id=match[1])
    return text


def _resolution_value(value: Any) -> str:
    """Display bounded check values without dumping an evidence document."""
    if isinstance(value, dict):
        return "; ".join(f"{key}: {item}" for key, item in list(value.items())[:6]) or tr('Not available yet')
    if isinstance(value, list):
        return "; ".join(str(item) for item in value[:6]) or tr('Not available yet')
    return tr('Not available yet') if value is None else str(value)


def _resolution_scope_fields(scope: dict[str, Any]) -> list[tuple[str, str, bool]]:
    fields = [(tr('Table'), str(scope.get("table_name") or tr('Not available yet')), False)]
    if scope.get("dt"):
        date_label = tr('Detection date (historical)') if scope.get("kind") == "schema_drift" else tr('Data date')
        fields.append((date_label, str(scope["dt"]), True))
    return fields


def resolution_action(
    action: str, label: str, alert_ref: str, request_id: str = "", style: str = "secondary",
) -> DiscordActionSpec:
    """Keep all durable resolution controls inside the existing dynamic handler."""
    custom_id = f"dq:v1:{action}:{alert_ref}" + (f":{request_id}" if request_id else "")
    if len(custom_id) > DISCORD_COMPONENT_CUSTOM_ID_LIMIT:
        raise ValueError(tr('Discord component custom_id exceeds 100 characters.'))
    return DiscordActionSpec(action_id=action, label=label, style=style, custom_id=custom_id)


def render_resolution_preview_embed(
    preview: dict[str, Any], request: dict[str, Any] | None = None, *,
    can_close: bool = False, blocked_message: str = "", operator_ui_url: str = "",
    check_nonce: str = "", show_cancel: bool = True,
) -> DiscordRenderResult:
    """Render simple closure steps; PASS is never a client-side lifecycle mutation."""
    alert_ref = str(preview["alert_ref"])
    scope = dict(preview.get("scope") or {})
    if str(preview.get("status") or "").lower() == "resolved":
        return render_resolved_incident_embed(
            alert_ref, scope, dict(preview.get("resolution") or {}),
            operator_ui_url=operator_ui_url,
            synchronization_pending=bool(preview.get("synchronization_pending")),
            has_report=bool(preview.get("report_uri") or preview.get("report_id")),
        )
    active_request = request if request is not None else preview.get("request")
    active_request = dict(active_request or {})
    result = dict(active_request.get("result") or {})
    status = str(active_request.get("status") or "")
    request_id = str(active_request.get("request_id") or "")
    available = preview.get("available") is True
    blockers = list(preview.get("blockers") or [])
    fields = [(tr('Alert Ref'), f"`{alert_ref}`", True), *_resolution_scope_fields(scope)]
    if scope.get("metric"):
        fields.append((tr('Check'), str(scope["metric"]), False))
    actions: list[DiscordActionSpec] = []
    color = DISCORD_COLORS["neutral"]

    if status in {"queued", "running"}:
        title = "\u23F3 " + tr('Check in progress')
        description = tr('The incident is not closed. We are only checking data or schema conditions.')
    elif status == "pass" and can_close:
        title = "\u2705 " + tr('Check passed')
        description = tr('The issue was not found in the latest check. Provide a reason and confirm before closing.')
        color = DISCORD_COLORS["success"]
        actions.append(resolution_action("resolve_close", tr('Close Incident'), alert_ref, request_id))
    elif status == "pass":
        title = "\u26A0\uFE0F " + tr('Evidence needs another check')
        description = blocked_message or tr('This evidence cannot yet support closure. Check again for a fresh result.')
        color = DISCORD_COLORS["warning"]
    elif status == "blocked":
        title = "\u26A0\uFE0F " + tr('Closure blocked')
        description = tr('The latest check still found a problem. Investigate the cause or continue remediation.')
        color = DISCORD_COLORS["warning"]
    elif status in {"error", "expired"}:
        title = tr('Check incomplete') if status == "error" else tr('Check evidence expired')
        description = tr('The system does not have enough evidence to close the incident. It remains active.')
        color = DISCORD_COLORS["warning"]
    elif not available:
        title = tr('Check unavailable')
        description = tr('The shared API or Airflow integration is not ready. The incident is not closed.')
    else:
        title = tr('Close this incident?')
        description = tr('Before closing, we need to check the data again. This check does not change data.')

    if result:
        fields.extend([
            (tr('Expected'), _resolution_value(result.get("expected")), True),
            (tr('Observed'), _resolution_value(result.get("observed")), True),
            (tr('Check result'), _resolution_summary(result.get("summary")) or tr('Detailed results are not available yet.'), False),
        ])
    if blockers:
        fields.append((tr('Closure blockers'), "\n".join(_resolution_blocker_text(item) for item in blockers[:5]), False))
    if blocked_message and status != "pass":
        fields.append((tr('Needs review'), blocked_message, False))
    if status:
        fields.append((tr('Check status'), _state_label(status), True))

    if request_id:
        actions.append(resolution_action("resolve_status", tr('Check Status'), alert_ref, request_id))
        if result:
            actions.append(resolution_action("resolve_evidence", tr('View Evidence'), alert_ref, request_id))
    if available and not blockers and status not in {"queued", "running"} and not can_close:
        # The nonce is retained by the rendered button, so a repeated click has the same API key.
        actions.insert(0, resolution_action("resolve_check", tr('Check Again'), alert_ref, check_nonce or uuid4().hex, "primary"))
    if show_cancel:
        actions.append(resolution_action("resolve_cancel", tr('Cancel'), alert_ref))
    if operator_ui_url.strip():
        actions.append(DiscordActionSpec("workspace", tr('Open Workspace'), "link", url=operator_ui_url.strip()))

    return _embed_result(
        title=title, description=description, fields=fields, color=color,
        narrative_source="deterministic_fallback",
        fallback_markdown=f"{title}\n{description}",
        footer=tr('System check · No data repairs or LLM calls'),
        action_rows=(DiscordActionRow(tuple(actions)),),
    )


def render_resolution_evidence_embed(preview: dict[str, Any], request: dict[str, Any]) -> DiscordRenderResult:
    """Expose check facts, not SQL, technical keys, or stale triage claims."""
    result = dict(request.get("result") or {})
    refs = result.get("evidence_references") or []
    return _embed_result(
        title=tr("\U0001F4CE Evidence · Closure Check"),
        description=_resolution_summary(result.get("summary")) or tr('The check has not produced complete evidence. The incident is not closed.'),
        fields=[
            (tr('Alert Ref'), f"`{preview['alert_ref']}`", True),
            *_resolution_scope_fields(dict(preview.get("scope") or {})),
            (tr('Check status'), _state_label(request.get("status")), True),
            (tr('Expected'), _resolution_value(result.get("expected")), True),
            (tr('Observed'), _resolution_value(result.get("observed")), True),
            (tr('Checked at'), str(request.get("finished_at") or tr('Not finished')), False),
            (tr('Stored evidence'), tr("{v0} references. Technical details are available in Audit & Recovery in Workspace.", v0=len(refs)), False),
        ],
        color=DISCORD_COLORS["success"] if request.get("status") == "pass" else DISCORD_COLORS["warning"],
        narrative_source="deterministic_fallback", fallback_markdown=tr('Closure check evidence.'),
        footer=tr('Evidence at check time · Not a guarantee of warehouse-wide health'),
    )


def render_resolution_confirmation_embed(
    preview: dict[str, Any], request: dict[str, Any], reason: str,
) -> DiscordRenderResult:
    """Bound the complete confirmation card, including a long operator reason."""
    result = dict(request.get("result") or {})
    return _embed_result(
        title=tr('Confirm incident closure'),
        description=tr('The latest check passed. Click Confirm Close to close only this incident. No data repairs are executed.'),
        fields=[
            (tr('Alert Ref'), f"`{preview['alert_ref']}`", True),
            *_resolution_scope_fields(dict(preview.get("scope") or {})),
            (tr('Check result'), _resolution_summary(result.get("summary")), False),
            (tr('Your reason'), reason, False),
        ],
        color=DISCORD_COLORS["success"], narrative_source="deterministic_fallback",
        fallback_markdown=tr('Confirm incident closure. Closing does not execute data repairs.'),
        footer=tr('System check · Point-in-time evidence · No LLM call'),
    )


def render_resolved_incident_embed(
    alert_ref: str, scope: dict[str, Any], resolution: dict[str, Any], *,
    issue_title: str = "", operator_ui_url: str = "", synchronization_pending: bool = False,
    has_report: bool = False,
) -> DiscordRenderResult:
    """Replace historical RCA with recorded system verification and human closure facts."""
    result = dict(resolution.get("result") or resolution.get("validation_result") or {})
    summary = _resolution_summary(result.get("summary") or resolution.get("summary") or resolution.get("validation_summary"))
    verified = bool(summary and resolution.get("resolution_request_id"))
    actor = str(resolution.get("resolved_by") or resolution.get("actor") or tr('Not available yet'))
    if actor.startswith("discord:"):
        actor = actor.split(":", 2)[-1]
    fields = [(tr('Status'), _state_label("RESOLVED"), True), (tr('Alert Ref'), f"`{alert_ref}`", True)]
    if issue_title:
        fields.append((tr('Original issue'), issue_title, False))
    fields.extend(_resolution_scope_fields(scope))
    expected = result.get("expected", resolution.get("expected"))
    observed = result.get("observed", resolution.get("observed"))
    if expected is not None or observed is not None:
        fields.extend([
            (tr('Expected'), _resolution_value(expected), True),
            (tr('Observed'), _resolution_value(observed), True),
        ])
    fields.extend([
        (tr('Check result'), summary or tr('Closure evidence is not available on this card. Open the history in Workspace.'), False),
        (tr('Closed by'), actor, True),
        (tr('Closed at'), str(resolution.get("resolved_at") or tr('Not available yet')), True),
        (tr('Reason'), str(resolution.get("resolution_reason") or resolution.get("reason") or tr('Not available yet')), False),
    ])
    if synchronization_pending:
        fields.append((tr('Synchronization pending'), tr('The decision is saved. Some displayed status is still awaiting synchronization.'), False))
    title = "\u2705 " + (tr('RESOLVED - latest check passed') if verified else tr('RESOLVED - closure history'))
    return _embed_result(
        title=title,
        description=tr('Closing does not execute data repairs. Historical analysis remains available but is not the latest condition.'),
        fields=fields, color=DISCORD_COLORS["success"], narrative_source="deterministic_fallback",
        fallback_markdown=f"{title}\n{summary or tr('Closure evidence is not available.')}",
        footer=tr('System check · Only this incident closed · No LLM call'),
        lifecycle="RESOLVED", action_rows=incident_action_rows(
            alert_ref, "RESOLVED", operator_ui_url,
            has_report or bool(resolution.get("report_uri") or resolution.get("report_id")),
        ),
    )
