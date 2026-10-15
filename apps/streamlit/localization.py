"""Session-only localization for the console, without modifying Streamlit globals."""
from __future__ import annotations

from typing import Any, Callable, MutableMapping, Sequence

import streamlit as st

from apps.common.i18n import current_language, normalize_language, translate

LANGUAGE_KEY = "streamlit_language"
LANGUAGE_LABELS = {"en": "English", "id": "Indonesia"}
TABLE_LABELS = {
    "request_id": "Request ID", "approval_status": "Approval Status",
    "execution_status": "Execution Status", "target_dag_id": "Target DAG",
    "start_date": "Start Date", "end_date": "End Date", "requested_by": "Requested By",
    "decided_by": "Decided By", "execution_dag_run_id": "Execution DagRun",
    "execution_error": "Execution Error", "updated_at": "Updated At",
    "audit_id": "Audit ID", "timestamp": "Timestamp", "agent_run_id": "Agent Run",
    "action": "Action", "status": "Status", "alert_key": "Alert Key",
    "alert_id": "Alert ID", "alert_display_id": "Alert Ref", "report_s3_uri": "Report URI",
    "payload": "Payload", "label": "Label", "tool_name": "Tool", "summary": "Summary",
    "artifact_uri": "Artifact URI", "created_at": "Created At", "report_id": "Report Ref",
    "eval_status": "Evaluation Status", "overall_score": "Overall Score",
    "payload_valid": "Valid Payload", "requires_human_approval": "Requires Human Approval",
    "recommendation_count": "Recommendations", "finding_count": "Findings",
    "json_report_s3_uri": "JSON Report URI", "markdown_report_s3_uri": "Markdown Report URI",
    "evaluated_at": "Evaluated At", "run_id": "Run ID", "scenario_id": "Scenario ID",
    "failure_category": "Failure Category", "suggested_change_summary": "Suggested Change",
}


def table_label(value: str) -> str:
    return tr(TABLE_LABELS.get(value, value))


def incident_option_label(option: str, alert: dict[str, Any], *, language: str | None = None) -> str:
    """The selector's value stays canonical; only its formatted label changes."""
    index = option.split(" | ", 1)[0]
    language = selected_language() if language is None else normalize_language(language)
    label = lambda text: translate(text, language)
    return " | ".join((
        index, str(alert.get("alert_display_id") or label("Unassigned")),
        status_label(alert.get("severity") or "unknown", language=language),
        str(alert.get("table_name") or label("Unknown table")),
        str(alert.get("metric") or label("Unknown metric")),
        str(alert.get("dt") or label("No date")),
    ))


def selected_language(state: MutableMapping[str, Any] | None = None) -> str:
    """Read this session's preference; pure helpers can use language_context."""
    state = st.session_state if state is None else state
    value = state.get(LANGUAGE_KEY, current_language())
    return value if isinstance(value, str) and value in LANGUAGE_LABELS else "en"


def tr(text: str, **values: object) -> str:
    return translate(text, selected_language(), **values)


def language_formatter(*, statuses: bool = False) -> Callable[[Any], str]:
    """Capture a render's locale so widget serialization cannot see a later toggle."""
    language = selected_language()
    if statuses:
        return lambda value: status_label(value, language=language)
    return lambda value: translate(str(value), language)


def localized_choice(kind: str, label: str, *, options: Sequence[Any], key: str,
                     index: int = 0, **kwargs: Any) -> Any:
    """Recreate translated widget labels while retaining a canonical selection."""
    if kind not in {"radio", "selectbox"} or not options:
        raise ValueError("A localized choice requires an allowlisted widget and options.")
    selected = st.session_state.get(key, options[index])
    if selected not in options:
        selected = options[index]
    widget_key = f"{key}__{selected_language()}"
    st.session_state[key] = selected
    st.session_state[widget_key] = selected

    def remember_choice() -> None:
        value = st.session_state.get(widget_key)
        if value in options:
            st.session_state[key] = value

    value = getattr(st, kind)(label, options=options, key=widget_key,
                              on_change=remember_choice, **kwargs)
    st.session_state[key] = value
    return value


def render_language_selector() -> str:
    """Render before data reads, including when the API is unavailable."""
    value = st.session_state.get(LANGUAGE_KEY)
    if not isinstance(value, str) or value not in LANGUAGE_LABELS:
        st.session_state[LANGUAGE_KEY] = "en"
    with st.sidebar:
        language = st.segmented_control(
            tr("Language"), options=("en", "id"), required=True,
            format_func=LANGUAGE_LABELS.__getitem__, key=LANGUAGE_KEY,
            width="stretch",
        )
    return normalize_language(language or "en")


def workspace_tabs(labels: Sequence[str], *, key: str, on_change: str = "rerun"):
    """Keep canonical tab state while Streamlit tracks translated display labels."""
    if on_change != "rerun":
        raise ValueError("Localized workspace tabs require rerun tracking.")
    language = selected_language()
    display_labels = [translate(label, language) for label in labels]
    selected = st.session_state.get(key, labels[0])
    if selected not in labels:
        selected = labels[0]
    st.session_state[key] = selected
    widget_key = f"{key}__{language}"
    st.session_state[widget_key] = translate(selected, language)

    def remember_tab() -> None:
        display = st.session_state.get(widget_key)
        if display in display_labels:
            st.session_state[key] = labels[display_labels.index(display)]

    return st.tabs(display_labels, key=widget_key, on_change=remember_tab)


def status_label(value: Any, *, language: str | None = None) -> str:
    """Format state codes for display, never alter the underlying API values."""
    return translate(str(value).replace("_", " ").title(), selected_language() if language is None else language)
