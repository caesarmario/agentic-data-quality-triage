####
## Discord Message Formatters for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import json
from datetime import date
from typing import Any

from agent.display import build_alert_one_liner, build_alert_ref, build_alert_title, build_report_id
from apps.common.llm_observability import llm_route_from_report
from pipelines.common.alert_identity import is_alert_ref
from pipelines.common.logging import logger


# --- Defining Constants
DISCORD_SEPARATOR   = "### ----------------------------------------"
DISCORD_SOFT_LIMIT  = 1900
MAX_EVIDENCE_ITEMS  = 5
MAX_ALERT_ITEMS     = 8

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



# --- Defining Generic Helpers
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
        logger.warning("Failed to parse Discord formatter JSON | value=%s", value[:200])
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
        return "Strong evidence"

    if confidence >= 0.70:
        return "Good evidence with some uncertainty"

    if confidence >= 0.50:
        return "Plausible, but more evidence is recommended"

    return "Low confidence; do not treat this as confirmed root cause"


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
        return "Needs Attention", "Critical signals are present. Review the highest-priority alert before trusting downstream data."

    if warnings:
        return "Review Recommended", "No critical signal is present, but warning-level checks should be reviewed."

    return "Healthy", "No failed checks or open critical alerts were found for this date."


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

    suffix = f"\n\nMessage trimmed for Discord.\n\n{DISCORD_SEPARATOR}"
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
        raise ValueError("Discord message limit must be at least 200 characters.")

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
        f"# {severity_icon(severity)} {severity.title()} Data Quality Alert",
        f"## {title}",
        "",
        "### Quick Read",
        plain_text,
        "Triage this alert before trusting downstream data for the affected date.",
        "",
        "### Key Facts",
        f"**Alert Ref** `{alert_ref}`",
        f"**Date** `{affected_date}`",
        f"**Affected Table** `{compact_value(alert.get('table_name'))}`",
        f"**Check** `{compact_value(alert.get('metric'))}`",
        f"**Observed / Expected** `{compact_value(alert.get('observed_value'))}` / `{compact_value(alert.get('expected_value'))}`",
        f"**Threshold** `{compact_value(alert.get('threshold_value'))}`",
        f"**Status** {status_icon(alert.get('status'))} {compact_value(alert.get('status')).title()}",
        "",
        "### Impact",
        "- Downstream tables, checks, and dashboards for this date may be incomplete or misleading.",
        "- Do not trigger a backfill or rerun until evidence confirms the likely cause.",
        "",
        "### Recommended Next Step",
        f"Run triage for `{alert_ref}` to collect ClickHouse, DQ history, pipeline run, and lineage evidence.",
        "",
        "### Commands",
        f"/triage alert_key:{alert_ref}",
        f"/daily_summary dt:{affected_date}",
    ]

    if evidence_uri:
        lines.extend(["", "### Evidence Artifact", f"`{evidence_uri}`"])

    lines.extend(["", "### Technical Reference", "For debugging and internal joins only.", f"System Alert Key `{alert_key}`"])

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
        f"# {status_icon(status)} DQ Alerts",
        f"## Status {status.title()}",
        "",
        "### Health Summary",
        f"**Date Filter** `{compact_value(dt, 'All dates')}`",
        f"**Total Alerts** `{len(alerts)}`",
        f"**Critical** `{severity_counts.get('critical', 0)}` | **Warning** `{severity_counts.get('warning', 0)}` | **Info** `{severity_counts.get('info', 0)}`",
        "",
    ]

    if assistant_note:
        lines.extend(["### Copilot Readout", assistant_note, ""])

    if not alerts:
        lines.extend(
            [
                "### Result",
                "No alerts matched the selected filter.",
                "",
                "### Start Here",
                f"Run /daily_summary dt:{compact_value(dt, '<YYYY-MM-DD>')} to review the broader daily status.",
            ]
        )

        if data_transport:
            lines.extend(
                [
                    "",
                    "### Technical Reference",
                    "For debugging and internal joins only.",
                    f"Alert Data Transport {data_transport}",
                ]
            )

        return join_lines(lines)

    for severity in SEVERITY_ORDER:
        group = [alert for alert in visible_alerts if str(alert.get("severity") or "info").lower() == severity]

        if not group:
            continue

        lines.extend([f"### {severity_icon(severity)} {SEVERITY_LABELS[severity]}"])

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
        lines.append(f"\nShowing {MAX_ALERT_ITEMS} of {len(alerts)} alerts. Use a tighter date filter for details.")

    first_alert_ref = resolve_alert_ref(sorted_alerts[0])
    lines.extend(
        [
            "### Start Here",
            f"Triage the highest-priority alert first: /triage alert_key:{first_alert_ref}",
        ]
    )

    if data_transport:
        lines.extend(
            [
                "",
                "### Technical Reference",
                "For debugging and internal joins only.",
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
    confidence         = float(get_model_value(report, "confidence", 0.0) or 0.0)
    title              = get_model_value(top_hypothesis, "title", "Unknown root cause")
    alert_key          = compact_value(get_model_value(alert, "alert_key"))
    report_id          = compact_value(get_model_value(report, "report_id"), build_report_id(get_model_value(report, "agent_run_id"), alert_key))
    alert_ref          = resolve_alert_ref(alert)
    issue_title        = build_alert_title(alert)
    confidence_summary = confidence_readout(confidence)
    llm_observation    = llm_route_from_report(report=report)
    llm_runtime        = llm_observation.to_public_dict() if llm_observation else None

    logger.info(
        "Formatting Discord triage result | report_id=%s alert_ref=%s confidence=%.2f evidence=%d actions=%d",
        report_id,
        alert_ref,
        confidence,
        len(evidence),
        len(actions),
    )

    lines = [
        "# \U0001F9ED Triage Result",
        f"## {issue_title}",
        "",
        "### Quick Read",
        f"The leading explanation is **{title}**.",
        f"Confidence is `{confidence:.2f}`: {confidence_summary}.",
        "",
        "### Key Facts",
        f"**Alert Ref** `{alert_ref}`",
        f"**Report ID** `{report_id}`",
        f"**Date** `{compact_value(get_model_value(alert, 'dt'))}`",
        f"**Severity** {severity_icon(get_model_value(alert, 'severity'))} {compact_value(get_model_value(alert, 'severity')).title()}",
        "",
    ]

    if assistant_note:
        lines.extend(["### Copilot Analysis", assistant_note, ""])

    if llm_runtime:
        mode_label = {
            "external_model": "External model",
            "heuristic_fallback": "Heuristic fallback",
            "failed": "Failed",
        }.get(str(llm_runtime.get("runtime_mode")), "Unknown")

        lines.extend(
            [
                "### AI Runtime",
                f"**Mode** {mode_label}",
                f"**Provider / Model** `{compact_value(llm_runtime.get('provider'))} / {compact_value(llm_runtime.get('model'))}`",
                f"**Route** `{compact_value(llm_runtime.get('requested_route'))} -> {compact_value(llm_runtime.get('executed_route'))}`",
                f"**Usage** `{int(llm_runtime.get('input_tokens') or 0)} input / {int(llm_runtime.get('output_tokens') or 0)} output tokens`",
                f"**Estimated Cost** `{compact_value(llm_runtime.get('estimated_cost_display'))}`",
            ]
        )

        if llm_runtime.get("fallback_summary"):
            lines.append(f"**Fallback** {compact_value(llm_runtime.get('fallback_summary'))}")

        lines.append("")

    lines.extend(
        [
            "### What Likely Happened",
            compact_value(get_model_value(top_hypothesis, "description"), "No hypothesis description was generated."),
            "",
            "### Why I Think So",
        ]
    )

    if evidence:
        for index, item in enumerate(evidence[:MAX_EVIDENCE_ITEMS], start=1):
            lines.append(f"{index}. {compact_value(get_model_value(item, 'summary'))}")
    else:
        lines.append("No evidence rows were returned.")

    recommended_action = compact_value(get_model_value(top_hypothesis, "recommended_action"), "Review the report before taking action.")

    lines.extend(
        [
            "",
            "### Recommended Next Step",
            recommended_action,
            "",
        ]
    )

    if actions:
        action = actions[0]
        lines.extend(
            [
                "### \U0001F9FE Approval Status",
                "**Approval required.** This recommendation has not been executed.",
                f"Action `{compact_value(get_model_value(action, 'action_type'))}` needs explicit approval before execution.",
                f"Target DAG `{compact_value(get_model_value(action, 'target_dag_id'))}`",
                "",
            ]
        )
    else:
        lines.extend(["### Approval Status", "No approval-gated action was recommended by this triage run.", ""])

    lines.extend(
        [
            "### \U0001F4C4 Report Links",
            f"Markdown `{compact_value(get_model_value(report, 'markdown_report_s3_uri'))}`",
            f"JSON `{compact_value(get_model_value(report, 'json_report_s3_uri'))}`",
            "",
            "### Technical Reference",
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
) -> str:
    """
    Format a daily DQ summary message.

    Args:
        dt: Business date in YYYY-MM-DD format.
        check_rows: DQ check status count rows.
        alert_rows: Alert severity count rows.
        assistant_note: Optional LLM-assisted natural language readout.

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
        "# \U0001F4CA DQ Daily Summary",
        f"## {dt}",
        "",
        "### Day Status",
        f"**{health_label}**",
        health_text,
        "",
        "### Check Results",
        f"\u2705 Passed `{check_counts.get('pass', 0)}`",
        f"\u26A0\uFE0F Warning `{check_counts.get('warn', 0)}`",
        f"\U0001F6A8 Failed `{check_counts.get('fail', 0)}`",
        f"\u23ED\uFE0F Skipped `{check_counts.get('skip', 0)}`",
        "",
    ]

    if assistant_note:
        lines.extend(["### Copilot Analysis", assistant_note, ""])

    lines.extend(
        [
            "### Alert Risk",
            f"\U0001F6A8 Open Critical `{alert_counts.get('critical', 0)}`",
            f"\u26A0\uFE0F Open Warning `{alert_counts.get('warning', 0)}`",
            "",
            "### Next Commands",
            f"/alerts dt:{dt} status:open limit:10",
            "/triage alert_key:<Alert Ref>",
        ]
    )

    return join_lines(lines)


def format_operator_answer(
    question: str,
    answer: str,
    alert_key: str = "",
    transport: str = "",
    agent_run_id: str = "",
) -> str:
    """
    Format a free-form copilot answer for Discord.

    Args:
        question: User question.
        answer: Copilot answer.
        alert_key: Optional alert key used as context.
        transport: Copilot transport such as api or local.
        agent_run_id: Optional API and audit correlation id.

    Returns:
        Discord Markdown message with a natural language answer.
    """
    alert_ref = ""

    if alert_key:
        alert_ref = alert_key if is_alert_ref(alert_key) else build_alert_ref(alert_key)

    logger.info("Formatting Discord copilot answer | alert_context=%s", alert_ref or "none")

    lines = [
        "# \U0001F916 DQ Copilot",
        "## Operator Answer",
        "",
        "### Direct Answer",
        answer,
        "",
    ]

    if alert_ref:
        lines.extend(["### Alert Context", f"Alert Ref `{alert_ref}`", ""])

    lines.extend(
        [
        "### Question",
        question,
        "",
        "### Guardrail",
        "I can explain, summarize, and recommend next steps, but I will not execute remediation without approval.",
        "",
        "### Suggested Next Command",
        f"/triage alert_key:{alert_ref}" if alert_ref else "Use /alerts to select an Alert Ref, then run /triage.",
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
        lines.extend(["", "### Technical Reference", "For debugging and internal joins only.", *technical_lines])

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
        "# \U0001F501 Backfill Approval Request",
        "## Durable Approval Queue",
        "",
        "### Quick Read",
        "The proposed backfill is stored for human review. No remediation has been executed.",
        "",
        "### Request Details",
        f"**Request ID** `{request_id}`",
        f"**Status** `{status}`",
        f"**Queue Result** `{'created' if created_new else 'reused existing request'}`",
        f"**Reason** {reason}",
        f"**Target DAG** `{target_dag_id}`",
        f"**Date Range** `{start_date}` to `{end_date}`",
        "",
        "### What Would Run After Separate Execution",
        "- Daily pipeline will run once per date.",
        "- Date partitions are expected to be replaced idempotently.",
        "",
        "### Safety Check",
        "No Airflow DAG was triggered. Creating this request only writes approval and audit state.",
        "",
        "### Decision Commands",
        f"/approve request_id:{request_id} comment:<review note>",
        f"/reject request_id:{request_id} comment:<review note>",
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

    decision_label = "Approved" if decision == "approve" else "Rejected"

    lines = [
        f"# \U0001F9FE Request {decision_label}",
        "## Durable Approval Decision",
        "",
        "### What Happened",
        "The human decision was stored in the approval queue and agent audit log.",
        "",
        "### Approval Details",
        f"**Request ID** `{request_id}`",
        f"**Decided By** `{approved_by}`",
        f"**Decision** `{decision}`",
        f"**Status** `{status}`",
        "",
        "### Safety Check",
        "No Airflow DAG was triggered. No backfill, rerun, or data mutation was executed.",
        "",
        "### Next Step",
        (
            f"An operator may run DAG `90_dag_dq_platform_backfill_dispatcher` with approval request `{request_id}`."
            if status == "approved"
            else "The rejected request cannot authorize dispatcher execution."
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
        "# \u2705 DQ Bot Online",
        "## Agentic Data Quality Triage",
        "",
        "Commands are ready for alert lookup, triage, daily summary, and approval previews.",
        "",
        "### Useful Commands",
        "/alerts dt:<YYYY-MM-DD> status:open limit:10",
        "/triage alert_key:<Alert Ref>",
        "/daily_summary dt:<YYYY-MM-DD>",
        "/ask question:<question> alert_key:<Alert Ref>",
        "`/backfill_preview start_date:<YYYY-MM-DD> end_date:<YYYY-MM-DD>`",
    ]

    return join_lines(lines)

