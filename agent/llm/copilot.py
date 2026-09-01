####
## Copilot Narrative Helpers for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from uuid import UUID


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Add repo root before importing project packages when this file is executed by module path.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.display import build_alert_ref, build_alert_title
from agent.llm.client import LlmResponse, run_llm_task
from pipelines.common.logging import logger


# --- Defining Constants
COPILOT_ROUTE                = "cheap_summary"
MAX_CONTEXT_ALERTS           = 5
MAX_CONTEXT_EVIDENCE         = 5
MAX_CONTEXT_AUDIT            = 5
MAX_CONTEXT_INCIDENT_HISTORY = 5
MAX_CONTEXT_TEXT             = 1600

COPILOT_SYSTEM_PROMPT = (
    "You are a data reliability copilot for a local warehouse quality platform. "
    "Use only the structured context provided and answer in plain operational language. "
    "Start with the direct answer, then explain why it matters and give the safest next step. "
    "Keep the response to short paragraphs, explain technical terms only when needed, and distinguish observed facts from hypotheses. "
    "Use the human-facing Alert Ref instead of raw system keys unless the operator explicitly asks for debugging details. "
    "Do not invent missing facts, do not expose hidden reasoning, do not claim remediation was executed, and keep mutating actions approval-gated."
)


# --- Defining Generic Helpers
def get_model_value(model: Any, field_name: str, default: Any = None) -> Any:
    """
    Read a value from either a dictionary or a Pydantic-like model.

    Args:
        model: Dictionary, Pydantic model, or None.
        field_name: Field name to read.
        default: Fallback value when missing.

    Returns:
        Field value or default.
    """
    if model is None:
        return default

    if isinstance(model, dict):
        return model.get(field_name, default)

    return getattr(model, field_name, default)


def compact_alert(alert: Any) -> dict[str, Any]:
    """
    Convert an alert object into bounded context for LLM prompts.

    Args:
        alert: Alert dictionary or Pydantic model.

    Returns:
        Compact alert context dictionary.
    """
    return {
        "alert_key": get_model_value(alert, "alert_key", ""),
        "alert_ref": get_model_value(alert, "alert_display_id", "")
        or build_alert_ref(
            str(get_model_value(alert, "alert_key", "")),
            get_model_value(alert, "dt"),
        ),
        "issue_title": build_alert_title(alert),
        "status": get_model_value(alert, "status", ""),
        "severity": get_model_value(alert, "severity", ""),
        "table_name": get_model_value(alert, "table_name", ""),
        "metric": get_model_value(alert, "metric", ""),
        "dt": str(get_model_value(alert, "dt", "") or ""),
        "observed_value": get_model_value(alert, "observed_value", ""),
        "expected_value": get_model_value(alert, "expected_value", ""),
        "threshold_value": get_model_value(alert, "threshold_value", ""),
    }


def count_by_key(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    """
    Count small row lists by a selected key.

    Args:
        rows: Row dictionaries.
        key: Dictionary key to count.

    Returns:
        Mapping of normalized value to count.
    """
    counts: dict[str, int] = {}

    for row in rows:
        value         = str(row.get(key) or "unknown").lower()
        counts[value] = counts.get(value, 0) + 1

    return counts


def compact_context_rows(
    rows: list[dict[str, Any]] | None,
    allowed_fields: tuple[str, ...],
    limit: int,
) -> list[dict[str, Any]]:
    """
    Bound structured rows before sending context to an LLM provider.

    Args:
        rows: Optional source row dictionaries.
        allowed_fields: Fields safe and useful for the narrative task.
        limit: Maximum rows to retain.

    Returns:
        Bounded dictionaries containing only allowlisted fields.
    """
    compact_rows: list[dict[str, Any]] = []

    for row in list(rows or [])[: max(0, limit)]:
        compact_row: dict[str, Any] = {}

        for field_name in allowed_fields:
            value = row.get(field_name)

            if value in (None, ""):
                continue

            compact_row[field_name] = value[:MAX_CONTEXT_TEXT] if isinstance(value, str) else value

        compact_rows.append(compact_row)

    return compact_rows


def compact_report_context(report_context: dict[str, Any] | None) -> dict[str, Any]:
    """
    Bound report context to fields needed by the Copilot narrative.

    Args:
        report_context: Optional structured triage report summary.

    Returns:
        Bounded report context without full report bodies or hidden state.
    """
    if not report_context:
        return {}

    allowed_fields = (
        "summary",
        "impact",
        "top_hypothesis",
        "confidence",
        "recommended_action",
        "approval_required",
        "report_id",
    )
    compact: dict[str, Any] = {}

    for field_name in allowed_fields:
        value = report_context.get(field_name)

        if value in (None, ""):
            continue

        if isinstance(value, str):
            compact[field_name] = value[:MAX_CONTEXT_TEXT]

        else:
            compact[field_name] = value

    return compact


def is_low_value_llm_content(response: LlmResponse) -> bool:
    """
    Detect generic fallback content that should be replaced with a domain-specific local narrative.

    Args:
        response: Routed LLM response.

    Returns:
        True when response is heuristic or too generic for operator-facing copy.
    """
    content = response.content.strip().lower()

    return response.used_heuristic or content.startswith("heuristic fallback response")


def run_copilot_task(
    prompt: str,
    context: dict[str, Any],
    fallback_text: str,
    agent_run_id: UUID | str | None = None,
    route_name: str = COPILOT_ROUTE,
) -> str:
    """
    Run a bounded copilot LLM task with a natural deterministic fallback.

    Args:
        prompt: Task prompt for the routed LLM.
        context: Structured evidence context.
        fallback_text: Natural local fallback text when LLM execution is unavailable.
        agent_run_id: Optional agent run UUID for correlation.
        route_name: Model route name from configs/agent/model_routing.yml.

    Returns:
        Natural language copilot note.
    """
    try:
        response = run_llm_task(
            route_name=route_name,
            prompt=prompt,
            system_prompt=COPILOT_SYSTEM_PROMPT,
            context=context,
            agent_run_id=agent_run_id,
        )

    except Exception as exc:
        logger.warning("Copilot LLM task failed; using local fallback | route=%s error=%s", route_name, exc)
        return fallback_text

    if is_low_value_llm_content(response=response):
        logger.info("Copilot route used heuristic fallback; replacing with domain fallback | route=%s", route_name)
        return fallback_text

    logger.info(
        "Copilot LLM note generated | route=%s provider=%s model=%s",
        response.route_name,
        response.provider,
        response.model,
    )

    return response.content.strip()


# --- Defining Fallback Builders
def build_alert_list_fallback(alerts: list[dict[str, Any]], status: str, dt: str | None = None) -> str:
    """
    Build a natural local fallback for alert list responses.

    Args:
        alerts: Alert rows.
        status: Alert status filter.
        dt: Optional business date filter.

    Returns:
        Natural language alert summary.
    """
    if not alerts:
        date_text = f" for {dt}" if dt else ""
        return f"I do not see any `{status}` alerts{date_text}. The current filter looks clean, but keep checking pipeline runs and DQ results if you are testing a new incident scenario."

    severity_counts = count_by_key(alerts, "severity")
    severity_order  = {"critical": 0, "warning": 1, "info": 2}
    first_row       = min(alerts, key=lambda item: severity_order.get(str(item.get("severity") or "info").lower(), 99))
    first_alert     = compact_alert(first_row)
    date_text       = f" for `{dt}`" if dt else ""
    critical_count  = severity_counts.get("critical", 0)
    warning_count   = severity_counts.get("warning", 0)

    return (
        f"I found `{len(alerts)}` `{status}` alert(s){date_text}. "
        f"The current set includes `{critical_count}` critical and `{warning_count}` warning alert(s). "
        f"Start with Alert Ref `{first_alert['alert_ref']}` because it reports {first_alert['issue_title'].lower()} "
        f"on `{first_alert['table_name']}`. "
        f"Run `/triage alert_key:{first_alert['alert_ref']}` before approving any backfill or rerun."
    )


def build_daily_summary_fallback(dt: str, check_rows: list[dict[str, Any]], alert_rows: list[dict[str, Any]]) -> str:
    """
    Build a natural local fallback for daily DQ summary responses.

    Args:
        dt: Business date.
        check_rows: DQ check status count rows.
        alert_rows: Alert severity count rows.

    Returns:
        Natural language daily summary.
    """
    check_counts = {str(row.get("status")): int(row.get("count") or 0) for row in check_rows}
    alert_counts = {str(row.get("severity")): int(row.get("count") or 0) for row in alert_rows}
    failed       = check_counts.get("fail", 0)
    warnings     = check_counts.get("warn", 0)
    critical     = alert_counts.get("critical", 0)

    if failed or critical:
        return (
            f"For `{dt}`, the day needs attention: `{failed}` checks failed and `{critical}` critical alert(s) are open. "
            "Review the top alert first, then run triage so the recommendation is backed by ClickHouse, DQ history, pipeline run, and lineage evidence."
        )

    return (
        f"For `{dt}`, I do not see a critical DQ signal in the summary. "
        f"There are `{warnings}` warning check(s), so this looks more like a monitoring review than an immediate incident unless downstream users report an issue."
    )


def build_triage_fallback(report: Any) -> str:
    """
    Build a natural local fallback for triage report responses.

    Args:
        report: TriageReport model.

    Returns:
        Natural language triage explanation.
    """
    alert          = get_model_value(report, "alert", {})
    top_hypothesis = get_model_value(report, "top_hypothesis")
    confidence     = float(get_model_value(report, "confidence", 0.0) or 0.0)
    action_count    = len(list(get_model_value(report, "approval_gated_actions", []) or []))

    evidence_count = len(list(get_model_value(report, "evidence", []) or []))
    alert_ref      = compact_alert(alert)["alert_ref"]

    return (
        f"The most likely explanation for Alert Ref `{alert_ref}` is "
        f"{str(get_model_value(top_hypothesis, 'title', 'the leading hypothesis')).lower()}. "
        f"This conclusion is based on `{evidence_count}` evidence item(s), with confidence `{confidence:.2f}`. "
        f"There are `{action_count}` approval-gated action(s), so nothing should be treated as automatically fixed. "
        "Review the evidence and report links before approving a backfill or rerun."
    )


def build_operator_answer_fallback(
    question: str,
    alert: Any | None = None,
    report_context: dict[str, Any] | None = None,
    evidence_rows: list[dict[str, Any]] | None = None,
    audit_rows: list[dict[str, Any]] | None = None,
    incident_history_rows: list[dict[str, Any]] | None = None,
) -> str:
    """
    Build a natural local fallback for evidence-aware operator questions.

    Args:
        question: User question from Discord or UI.
        alert: Optional alert context.
        report_context: Optional bounded triage report summary.
        evidence_rows: Optional evidence summaries collected by guarded tools.
        audit_rows: Optional recent audit events for the alert.
        incident_history_rows: Optional sanitized outcomes from earlier investigations
            of the same exact Alert Ref.

    Returns:
        Natural language answer bounded by available context.
    """
    if not alert:
        return (
            "I can explain alerts, summarize evidence, recommend an investigation step, and draft approval-gated action previews. "
            "Select an Alert Ref first so I can stay grounded in one incident instead of guessing."
        )

    compact_alert_context = compact_alert(alert)
    compact_report        = compact_report_context(report_context)
    compact_evidence      = compact_context_rows(
        evidence_rows,
        allowed_fields=("tool_name", "evidence_type", "summary", "row_count", "s3_uri"),
        limit=MAX_CONTEXT_EVIDENCE,
    )
    compact_audit         = compact_context_rows(
        audit_rows,
        allowed_fields=("ts", "action", "tool_name", "status", "error_message", "report_s3_uri"),
        limit=MAX_CONTEXT_AUDIT,
    )
    compact_history       = compact_context_rows(
        incident_history_rows,
        allowed_fields=(
            "recorded_at",
            "outcome_status",
            "summary",
            "confidence",
            "top_hypothesis_category",
            "report_id",
            "requires_human_approval",
            "evidence_reference_count",
            "approval_state",
        ),
        limit=MAX_CONTEXT_INCIDENT_HISTORY,
    )
    normalized_question = question.strip().lower()
    alert_ref           = compact_alert_context["alert_ref"]

    history_intent = any(
        phrase in normalized_question
        for phrase in (
            "history",
            "previous investigation",
            "prior investigation",
            "investigated before",
            "happened before",
            "recurring",
            "recurrence",
        )
    )

    if history_intent:
        if not compact_history:
            return (
                f"I found no earlier investigation record for Alert Ref `{alert_ref}` "
                "within the bounded history window. "
                "That does not prove the underlying data issue never occurred under another alert. "
                "Use the current triage evidence before deciding on remediation."
            )

        latest_history = compact_history[0]
        latest_summary = str(
            latest_history.get("summary")
            or "The latest prior investigation has no readable summary."
        )
        latest_report = str(latest_history.get("report_id") or "not recorded")

        return (
            f"I found `{len(compact_history)}` earlier investigation record(s) "
            f"for the same Alert Ref `{alert_ref}`. "
            f"The latest prior record says: {latest_summary} Report reference: `{latest_report}`. "
            "This history is comparison context only. It does not prove the current root cause, "
            "and it does not establish recurrence across different dates or alerts."
        )

    if "evidence" in normalized_question:
        if not compact_evidence:
            return (
                f"I do not have triage evidence for Alert Ref `{alert_ref}` yet. "
                f"Run `/triage alert_key:{alert_ref}` first; do not approve remediation from alert metadata alone."
            )

        first_summary = str(compact_evidence[0].get("summary") or "No readable evidence summary was returned.")

        return (
            f"I found `{len(compact_evidence)}` bounded evidence item(s) for Alert Ref `{alert_ref}`. "
            f"The first available signal says: {first_summary} "
            "Treat this as investigation evidence, not proof that remediation has already run."
        )

    if "backfill" in normalized_question or "approval" in normalized_question:
        recommendation = str(
            compact_report.get("recommended_action")
            or "Confirm the missing or incomplete partition with triage evidence before proposing a backfill."
        )

        return (
            f"For Alert Ref `{alert_ref}`, the current draft recommendation is: {recommendation} "
            "This is an approval preview only. No Airflow DAG, backfill, rerun, or data mutation has been executed."
        )

    if "report" in normalized_question or "root cause" in normalized_question:
        if compact_report:
            hypothesis = str(compact_report.get("top_hypothesis") or "No top hypothesis is available.")
            confidence = compact_report.get("confidence", "unknown")

            return (
                f"The current report for Alert Ref `{alert_ref}` ranks {hypothesis.lower()} as the leading explanation "
                f"with confidence `{confidence}`. Review the linked evidence before treating this as confirmed root cause."
            )

        return (
            f"No matching triage report is loaded for Alert Ref `{alert_ref}`. "
            f"Run `/triage alert_key:{alert_ref}` before asking for a root-cause conclusion."
        )

    if compact_report:
        summary        = str(compact_report.get("summary") or compact_alert_context["issue_title"])
        hypothesis     = str(compact_report.get("top_hypothesis") or "No leading hypothesis is available.")
        confidence_value = compact_report.get("confidence", "unknown")

        try:
            confidence = f"{float(confidence_value):.2f}"

        except (TypeError, ValueError):
            confidence = str(confidence_value)

        recommendation = str(
            compact_report.get("recommended_action")
            or "Review the report evidence before proposing remediation."
        )

        return (
            f"For Alert Ref {alert_ref}, the current triage report says: {summary} "
            f"The leading explanation is {hypothesis.lower()} with confidence {confidence}. "
            f"Safest next step: {recommendation} "
            "This remains advisory and any remediation still requires explicit approval."
        )

    audit_text = ""

    if compact_audit:
        latest_action = compact_audit[0]
        audit_text    = (
            f" The latest audit event is `{latest_action.get('action', 'unknown')}` "
            f"with status `{latest_action.get('status', 'unknown')}`."
        )

    return (
        f"Alert Ref `{alert_ref}` reports {compact_alert_context['issue_title'].lower()}. "
        f"The check observed `{compact_alert_context['observed_value']}` where `{compact_alert_context['expected_value']}` "
        f"was expected on `{compact_alert_context['table_name']}`.{audit_text} "
        f"Run `/triage alert_key:{alert_ref}` next, then review the evidence before approving a backfill or rerun."
    )


# --- Defining Public Copilot Builders
def build_alert_list_copilot_note(alerts: list[dict[str, Any]], status: str, dt: str | None = None) -> str:
    """
    Generate a natural copilot note for a Discord alert list.

    Args:
        alerts: Alert rows from ClickHouse.
        status: Alert status filter.
        dt: Optional business date filter.

    Returns:
        Natural language copilot note.
    """
    compact_alerts = [compact_alert(alert) for alert in alerts[:MAX_CONTEXT_ALERTS]]
    fallback_text  = build_alert_list_fallback(alerts=alerts, status=status, dt=dt)
    context        = {
        "status_filter": status,
        "dt_filter": dt,
        "alert_count": len(alerts),
        "severity_counts": count_by_key(alerts, "severity"),
        "alerts": compact_alerts,
    }
    prompt = (
        "Give a direct operator readout of this DQ alert list. "
        "State the urgency, identify the first Alert Ref to inspect, explain why it has priority, and provide the safest next command. "
        "Do not repeat raw system alert keys."
    )

    return run_copilot_task(prompt=prompt, context=context, fallback_text=fallback_text)


def build_daily_summary_copilot_note(dt: str, check_rows: list[dict[str, Any]], alert_rows: list[dict[str, Any]]) -> str:
    """
    Generate a natural copilot note for a daily DQ summary.

    Args:
        dt: Business date.
        check_rows: DQ check status count rows.
        alert_rows: Alert severity count rows.

    Returns:
        Natural language copilot note.
    """
    fallback_text = build_daily_summary_fallback(dt=dt, check_rows=check_rows, alert_rows=alert_rows)
    context       = {
        "dt": dt,
        "dq_check_counts": check_rows,
        "alert_counts": alert_rows,
    }
    prompt = (
        "Give a direct daily reliability assessment for a data engineer. "
        "Classify the date as healthy, review recommended, or needs attention, explain the strongest observed signal, and give one safe next step."
    )

    return run_copilot_task(prompt=prompt, context=context, fallback_text=fallback_text)


def build_triage_copilot_note(report: Any) -> str:
    """
    Generate a natural copilot note for a triage report.

    Args:
        report: TriageReport model.

    Returns:
        Natural language copilot note.
    """
    alert          = get_model_value(report, "alert", {})
    top_hypothesis = get_model_value(report, "top_hypothesis")
    evidence       = list(get_model_value(report, "evidence", []) or [])
    fallback_text  = build_triage_fallback(report=report)
    context        = {
        "alert": compact_alert(alert),
        "top_hypothesis": {
            "title": get_model_value(top_hypothesis, "title", ""),
            "description": get_model_value(top_hypothesis, "description", ""),
            "recommended_action": get_model_value(top_hypothesis, "recommended_action", ""),
            "confidence": get_model_value(top_hypothesis, "confidence", 0.0),
        },
        "confidence": get_model_value(report, "confidence", 0.0),
        "evidence": [
            {
                "tool_name": get_model_value(item, "tool_name", ""),
                "summary": get_model_value(item, "summary", ""),
                "row_count": get_model_value(item, "row_count", 0),
            }
            for item in evidence[:MAX_CONTEXT_EVIDENCE]
        ],
        "approval_gated_action_count": len(list(get_model_value(report, "approval_gated_actions", []) or [])),
    }
    prompt = (
        "Explain this triage result in plain operational language. "
        "State what likely happened, which evidence supports it, how certain the conclusion is, and the safest approval-gated next action. "
        "Do not present a hypothesis as an observed fact."
    )

    return run_copilot_task(prompt=prompt, context=context, fallback_text=fallback_text, agent_run_id=get_model_value(report, "agent_run_id"))


def build_operator_answer(
    question: str,
    alert: Any | None = None,
    report_context: dict[str, Any] | None = None,
    evidence_rows: list[dict[str, Any]] | None = None,
    audit_rows: list[dict[str, Any]] | None = None,
    incident_history_rows: list[dict[str, Any]] | None = None,
    agent_run_id: UUID | str | None = None,
) -> str:
    """
    Generate an evidence-aware answer for Discord or UI Copilot questions.

    Args:
        question: User question.
        alert: Optional alert context used to ground the answer.
        report_context: Optional bounded triage report summary.
        evidence_rows: Optional evidence summaries from guarded tools.
        audit_rows: Optional recent audit events for the selected alert.
        incident_history_rows: Optional sanitized prior outcomes for the same
            exact alert identity.
        agent_run_id: Optional correlation UUID shared with API or workflow audit events.

    Returns:
        Natural language answer that cannot execute remediation.
    """
    compact_report   = compact_report_context(report_context)
    compact_evidence = compact_context_rows(
        evidence_rows,
        allowed_fields=("tool_name", "evidence_type", "summary", "row_count", "s3_uri"),
        limit=MAX_CONTEXT_EVIDENCE,
    )
    compact_audit    = compact_context_rows(
        audit_rows,
        allowed_fields=("ts", "action", "tool_name", "status", "error_message", "report_s3_uri"),
        limit=MAX_CONTEXT_AUDIT,
    )
    compact_history  = compact_context_rows(
        incident_history_rows,
        allowed_fields=(
            "recorded_at",
            "outcome_status",
            "summary",
            "confidence",
            "top_hypothesis_category",
            "report_id",
            "requires_human_approval",
            "evidence_reference_count",
            "approval_state",
        ),
        limit=MAX_CONTEXT_INCIDENT_HISTORY,
    )
    fallback_text = build_operator_answer_fallback(
        question=question,
        alert=alert,
        report_context=compact_report,
        evidence_rows=compact_evidence,
        audit_rows=compact_audit,
        incident_history_rows=compact_history,
    )
    context = {
        "question": question,
        "alert": compact_alert(alert) if alert else None,
        "report": compact_report,
        "evidence": compact_evidence,
        "audit_events": compact_audit,
        "prior_investigations": compact_history,
        "allowed_actions": [
            "explain_alert",
            "summarize_evidence",
            "recommend_next_step",
            "draft_approval_preview",
        ],
        "blocked_actions": [
            "execute_sql_mutation",
            "trigger_backfill_without_approval",
            "edit_production_data",
        ],
    }
    prompt = (
        "Answer the operator question directly in plain language. "
        "Use the selected alert, report, evidence, audit, and prior-investigation context when available. "
        "Treat prior investigations as comparison context only, never as proof "
        "of the current root cause or cross-date recurrence. "
        "Distinguish observed evidence from hypotheses and say exactly what is missing instead of guessing. "
        "Recommend one safe next step, do not expose raw system keys, and do not imply remediation has executed."
    )

    return run_copilot_task(
        prompt=prompt,
        context=context,
        fallback_text=fallback_text,
        agent_run_id=agent_run_id,
    )

