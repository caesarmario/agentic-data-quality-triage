"""Network-free language, state identity, copy coverage and report safety checks."""
from __future__ import annotations

import ast
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from string import Formatter
from types import SimpleNamespace

import pytest
from streamlit.testing.v1 import AppTest

from apps.common.control_plane import ControlPlaneResponseError
from apps.common.i18n import language_context, translate
from apps.common.locales.streamlit import MESSAGES
from apps.streamlit import app, localization, resolution

ROOT = Path(__file__).resolve().parents[1]
URI = "s3://dq-artifacts/agent-reports/report.md"
NOW = datetime(2026, 10, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def language_state(monkeypatch):
    state = {}
    monkeypatch.setattr(localization, "st", SimpleNamespace(session_state=state))
    return state


def preview(status="pass"):
    return {
        "alert_ref": "DQ-language", "incident_revision": 3, "status": "triaged",
        "scope": {"kind": "dq", "table_name": "dq.orders", "dt": "2026-10-12", "metric": "coverage", "dimension": None},
        "blockers": [], "available": True, "resolution": None, "synchronization_pending": False,
        "request": {
            "request_id": "REV-language", "alert_ref": "DQ-language", "incident_revision": 3,
            "status": status, "created_at": (NOW - timedelta(minutes=2)).isoformat(),
            "finished_at": (NOW - timedelta(minutes=1)).isoformat(),
            "expires_at": (NOW + timedelta(minutes=8)).isoformat(),
            "dag_run_id": "manual__original", "error_code": None,
            "result": {"expected": "SELECT {raw} FROM orders", "observed": "25 dari 25 segmen",
                       "summary": "Pemeriksaan lolos.", "evidence_references": [URI]},
        },
    }


def test_language_is_english_first_session_only_and_strict(language_state):
    assert localization.selected_language(language_state) == "en"
    with language_context("id"):
        assert localization.selected_language(language_state) == "id"
    language_state[localization.LANGUAGE_KEY] = "unsupported"
    assert localization.selected_language(language_state) == "en"
    language_state[localization.LANGUAGE_KEY] = "id"
    assert localization.tr("Check Again") == "Periksa Lagi"
    assert localization.tr("SELECT '{id}' FROM orders") == "SELECT '{id}' FROM orders"
    assert translate("Pemeriksaan lolos.", "en") == "Check passed."


def test_catalog_placeholders_are_symmetric_and_all_calls_are_curated():
    def fields(value):
        return {field for _, field, _, _ in Formatter().parse(value) if field is not None}

    for english, indonesian in MESSAGES.items():
        assert english and indonesian
        assert fields(english) == fields(indonesian), english
    for module in (app, localization, resolution):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "tr":
                if node.args and isinstance(node.args[0], ast.Constant):
                    assert node.args[0].value in MESSAGES, (module.__name__, node.lineno)
    assert set(localization.TABLE_LABELS.values()).issubset(MESSAGES)
    assert set(app.EMPTY_STATE_MESSAGES.values()).issubset(MESSAGES)
    assert set(app.COPILOT_GUIDED_PROMPTS).issubset(MESSAGES)
    assert set(app.PRIMARY_WORKSPACE_TABS + app.INCIDENT_DETAIL_TABS).issubset(MESSAGES)


@pytest.mark.parametrize("status", ["queued", "running", "pass", "blocked", "error", "expired"])
def test_resolution_localizes_copy_not_evidence_or_gates(language_state, status):
    source = preview(status)
    original = deepcopy(source)
    english = resolution.build_resolution_view(source, now=NOW)
    language_state[localization.LANGUAGE_KEY] = "id"
    indonesian = resolution.build_resolution_view(source, now=NOW)
    assert english["heading"] != indonesian["heading"]
    for field in ("can_resolve", "can_revalidate", "status", "scope", "expected", "observed", "summary", "blockers"):
        assert english[field] == indonesian[field], field
    assert indonesian["summary"] == "Pemeriksaan lolos."
    assert source == original


def test_language_switch_preserves_confirmation_attempt_and_polling(language_state):
    source = preview()
    state = resolution.resolution_session(language_state, source["alert_ref"])
    state.update(open=True, actor="Mario", reason="Bukti lengkap.", request=source["request"])
    language_state.update(primary_workspace_tabs="Incidents", incident_detail_tabs="Overview")
    language_state[resolution.ACTIVE_PANEL_KEY] = source["alert_ref"]
    attempt = resolution.prepare_revalidation_attempt(state, "Mario")
    confirmation = resolution.prepare_resolution_confirmation(state, source, source["request"], actor="Mario", reason=state["reason"], now=NOW)
    original = deepcopy(state)
    language_state[localization.LANGUAGE_KEY] = "id"
    assert resolution.resolution_panel_is_active(language_state, source["alert_ref"])
    assert resolution.prepare_revalidation_attempt(state, "Mario") == attempt
    assert resolution.prepare_resolution_confirmation(state, source, source["request"], actor="Mario", reason=state["reason"], now=NOW) == confirmation
    assert state == original


def test_canonical_incident_options_do_not_change_for_missing_fields(language_state):
    rows = [{"alert_key": "stable-original", "severity": "critical"}]
    english = app.build_incident_option_rows(rows)
    option = next(iter(english))
    english_label = localization.incident_option_label(option, rows[0])
    language_state[localization.LANGUAGE_KEY] = "id"
    assert app.build_incident_option_rows(rows) == english
    assert localization.incident_option_label(option, rows[0]) != english_label


@pytest.mark.parametrize("language", ["en", "id"])
def test_copilot_forwards_selected_language_without_translating_question(monkeypatch, language):
    captured = {}

    def answer(self, **kwargs):
        captured.update(kwargs)
        return {"alert_key": "orders|original", "answer": "Original answer", "context_source": "alert_audit"}

    monkeypatch.setattr(app.ControlPlaneClient, "answer_copilot", answer)
    result = app.answer_ui_copilot_question(
        "Pertanyaan {original}", {"alert_key": "orders|original"}, None, [],
        api_base_url="http://unit.invalid", language=language,
    )
    assert captured["language"] == language
    assert captured["question"] == "Pertanyaan {original}"
    assert result["answer"] == "Original answer"


def test_invalid_copilot_language_fails_before_any_api_call(monkeypatch):
    monkeypatch.setattr(app.ControlPlaneClient, "answer_copilot", lambda **kwargs: pytest.fail("No API call allowed"))
    with pytest.raises(ValueError, match="en or id"):
        app.answer_ui_copilot_question("Original", {}, None, [], language="arbitrary prompt")


def test_local_copilot_receives_language_without_real_provider_call(monkeypatch):
    captured = {}

    def answer(**kwargs):
        captured.update(kwargs)
        return "Jawaban lokal."

    monkeypatch.setattr(app.copilot_service, "build_operator_answer", answer)
    result = app.answer_ui_copilot_question("Original question", {}, None, [], api_base_url="", language="id")
    assert captured["language"] == "id"
    assert captured["question"] == "Original question"
    assert result["answer"] == "Jawaban lokal."


def test_table_copy_translates_but_reasons_and_identifiers_remain_original(language_state):
    rows = [{"Status": "Resolved", "Table": "Pemeriksaan lolos.", "approval_status": "pending", "reason": "Periksa Lagi"}]
    original = deepcopy(rows)
    language_state[localization.LANGUAGE_KEY] = "id"
    frame = app.dataframe_from_rows(rows)
    assert frame.iloc[0]["Status"] == "Ditutup"
    assert frame.iloc[0]["Tabel"] == "Pemeriksaan lolos."
    assert frame.iloc[0]["Status Persetujuan"] == "Menunggu"
    assert frame.iloc[0]["reason"] == "Periksa Lagi"
    assert rows == original


@pytest.mark.parametrize("text,truncated,allowed", [
    ("Original {id}", False, True), ("a" * 200_000, False, True),
    ("a" * 200_001, False, False), ("\u00e9" * 100_000, False, True),
    ("\u00e9" * 100_001, False, False), ("short", True, False),
    ("short", None, False), ("short", 0, False), ("", False, False),
    ("\ud800", False, False),
])
def test_report_download_exact_utf8_byte_boundary(text, truncated, allowed):
    payload = app.report_download_data(text, truncated=truncated)
    assert (payload is not None) is allowed
    if allowed:
        assert payload == text.encode("utf-8")


@pytest.mark.parametrize("payload", [
    {"text": "short", "truncated": True}, {"text": "short"},
    {"text": "a" * 200_001, "truncated": False},
    {"text": "bad", "truncated": False, "bytes_read": 4, "returned_bytes": 3},
    {"text": "a", "truncated": False, "bytes_read": True, "returned_bytes": True},
])
def test_api_report_fails_closed_and_never_downloads_truncated_text(monkeypatch, payload):
    monkeypatch.setattr(app, "build_streamlit_control_plane_client", lambda **kwargs: SimpleNamespace(read_report_artifact=lambda **kwargs: payload))
    monkeypatch.setattr(app, "read_s3_text", lambda **kwargs: pytest.fail("No fallback for response contract errors"))
    with pytest.raises(ControlPlaneResponseError):
        app.read_report_text(URI)


def test_unapproved_bucket_never_reaches_reader(monkeypatch):
    monkeypatch.setattr(app, "build_streamlit_control_plane_client", lambda **kwargs: pytest.fail("No client for unapproved bucket"))
    with pytest.raises(ValueError):
        app.read_report_text("s3://unapproved-bucket/report.md")


@pytest.mark.parametrize("uri", [
    "s3://dq-artifacts/raw/orders.json", "s3://dq-artifacts/agent-reports/report.sql",
    "s3://dq-artifacts/agent-reports/../report.md", "s3://dq-artifacts/agent-reports//report.md",
    "https://public.invalid/report.md", "s3://dq-artifacts/agent-reports/report.md\n",
])
def test_report_uri_requires_approved_prefix_and_type_before_read(monkeypatch, uri):
    monkeypatch.setattr(app, "build_streamlit_control_plane_client", lambda **kwargs: pytest.fail("No client for invalid report URI"))
    with pytest.raises(ValueError):
        app.read_report_text(uri)


def test_widget_formatter_is_bound_to_its_render_language(language_state):
    english = localization.language_formatter()
    status_english = localization.language_formatter(statuses=True)
    language_state[localization.LANGUAGE_KEY] = "id"
    assert english("Choose date") == "Choose date"
    assert status_english("resolved") == "Resolved"
    assert localization.language_formatter()("Choose date") == "Pilih tanggal"


@pytest.mark.parametrize("payload", [b"original", b"a" * 6, b"\xff"])
def test_local_bounded_reader_closes_body_on_success_oversize_or_bad_utf8(monkeypatch, payload):
    body = BytesIO(payload)
    monkeypatch.setattr(app, "build_s3_client", lambda: SimpleNamespace(get_object=lambda **kwargs: {"Body": body}))
    if payload == b"original":
        assert app.read_s3_text(URI, max_bytes=8) == "original"
    else:
        with pytest.raises((ValueError, UnicodeDecodeError)):
            app.read_s3_text(URI, max_bytes=5)
    assert body.closed


def test_real_streamlit_language_toggle_keeps_tracked_tabs_and_form_state():
    # Synthetic in-process UI only: no API, Airflow, warehouse or provider calls.
    at = AppTest.from_string('''
import streamlit as st
from apps.streamlit.localization import render_language_selector, workspace_tabs, tr
render_language_selector()
if "primary_workspace_tabs" not in st.session_state:
    st.session_state["primary_workspace_tabs"] = "Approvals"
tabs = workspace_tabs(("Incidents", "Approvals", "Agent Reliability"), key="primary_workspace_tabs")
with tabs[1]:
    st.text_input(tr("Operator name"), key="original_actor")
    st.text_area(tr("Closure reason"), key="original_reason")
st.code(st.session_state["primary_workspace_tabs"])
''').run()
    assert not at.exception
    assert at.session_state[localization.LANGUAGE_KEY] == "en"
    assert [tab.label for tab in at.tabs] == ["Incidents", "Approvals", "Agent Reliability"]
    at.text_input[0].set_value("Mario").run()
    at.text_area[0].set_value("Original closure reason.").run()
    at.session_state[localization.LANGUAGE_KEY] = "id"
    at.run()
    assert not at.exception
    assert [tab.label for tab in at.tabs] == ["Incident", "Persetujuan", "Keandalan Agent"]
    assert at.session_state["primary_workspace_tabs"] == "Approvals"
    assert at.text_input[0].value == "Mario"
    assert at.text_area[0].value == "Original closure reason."
    at.session_state[localization.LANGUAGE_KEY] = "en"
    at.run()
    assert not at.exception
    assert at.session_state["primary_workspace_tabs"] == "Approvals"


def test_report_access_copy_and_html_use_original_escaped_values():
    at = AppTest.from_string('''
import streamlit as st
from apps.streamlit.app import render_report_access, render_selected_alert
st.session_state["streamlit_language"] = "id"
render_report_access("s3://dq-artifacts/agent-reports/report.md", "Original {id} report")
render_selected_alert({"status": "resolved", "table_name": "<script>alert(1)</script>", "metric": "raw_metric", "observed_value": "<b>raw</b>"})
''').run()
    assert not at.exception
    assert [item.value for item in at.code] == [URI, "dq-artifacts", "agent-reports/report.md"]
    card = at.markdown[-1].value
    assert translate("Business date", "id") in card
    assert translate("Observed", "id") in card and translate("Expected", "id") in card
    assert "&lt;script&gt;" in card and "<script>" not in card
    assert "raw_metric" in card and "&lt;b&gt;raw&lt;/b&gt;" in card


@pytest.mark.parametrize("language", ["en", "id"])
@pytest.mark.parametrize("dt", [None, "2026-10-16"])
def test_daily_empty_overview_is_localized_without_none_date(language, dt):
    at = AppTest.from_string(f'''
import streamlit as st
from apps.streamlit.app import render_reliability_overview
st.session_state["streamlit_language"] = {language!r}
render_reliability_overview([], daily_summary={{
    "status": "success", "availability": "empty", "dt": {dt!r},
    "check_counts": [], "alert_counts": [], "total_checks": 0, "total_open_alerts": 0,
}}, daily_summary_transport="synthetic")
''').run()

    assert not at.exception
    card = "\n".join(item.value for item in at.markdown)
    assert translate("No quality data yet", language) in card
    assert "None" not in card
    assert "dq-health-stable" not in card
    assert not at.error
    assert [item.value for item in at.metric] == ["0", "0", "0", "0"]
    assert (dt or translate("not available", language)) in card


def test_production_code_never_monkeypatches_global_streamlit_or_uses_browser_storage():
    for module in (app, localization, resolution):
        source = Path(module.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                assert all(not (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "st") for target in targets)
        assert "localStorage" not in source and "document.cookie" not in source


def test_selector_remains_visible_when_incident_loading_fails():
    at = AppTest.from_string('''
from apps.streamlit import app
from unittest.mock import patch
def unavailable(**kwargs):
    raise RuntimeError("synthetic outage")
with patch.object(app, "load_daily_quality_summary", return_value=({"dt": "2026-10-12", "check_counts": []}, "synthetic")), patch.object(app, "load_alert_rows", side_effect=unavailable):
    app.main()
''').run()
    assert not at.exception
    assert at.session_state[localization.LANGUAGE_KEY] == "en"
    assert len(at.sidebar.get("button_group")) == 1
    assert "Unable to load incidents" in at.error[0].value
    at.session_state[localization.LANGUAGE_KEY] = "id"
    at.run()
    assert not at.exception
    assert len(at.sidebar.get("button_group")) == 1
    assert "Tidak dapat memuat incident" in at.error[0].value


def test_report_is_copyable_without_audit_history():
    at = AppTest.from_string('''
from apps.streamlit import app
from unittest.mock import patch
with patch.object(app, "load_audit_rows", return_value=[]):
    app.render_audit_panel({"alert_key": "original", "report_s3_uri": "s3://dq-artifacts/agent-reports/report.md"})
''').run()
    assert not at.exception
    assert [item.value for item in at.code] == [URI, "dq-artifacts", "agent-reports/report.md"]


def test_localized_tab_callback_retains_canonical_values(monkeypatch):
    state = {localization.LANGUAGE_KEY: "id", "primary_workspace_tabs": "Approvals"}
    captured = {}

    def tabs(labels, **kwargs):
        captured.update(labels=labels, **kwargs)
        return []

    monkeypatch.setattr(localization, "st", SimpleNamespace(session_state=state, tabs=tabs))
    localization.workspace_tabs(app.PRIMARY_WORKSPACE_TABS, key="primary_workspace_tabs")
    state[captured["key"]] = translate("Agent Reliability", "id")
    captured["on_change"]()
    assert state["primary_workspace_tabs"] == "Agent Reliability"
    state[localization.LANGUAGE_KEY] = "en"
    localization.workspace_tabs(app.PRIMARY_WORKSPACE_TABS, key="primary_workspace_tabs")
    assert state[captured["key"]] == "Agent Reliability"


def test_localized_choice_preserves_canonical_filter_and_rebuilds_labels(monkeypatch):
    state = {localization.LANGUAGE_KEY: "en", "scope": "triaged"}
    calls = []
    def selectbox(label, **kwargs):
        calls.append(kwargs)
        return state[kwargs["key"]]
    monkeypatch.setattr(localization, "st", SimpleNamespace(session_state=state, selectbox=selectbox))
    for language in ("en", "id", "en"):
        state[localization.LANGUAGE_KEY] = language
        assert localization.localized_choice("selectbox", "Status", options=["active", "triaged"],
                                              key="scope", format_func=localization.language_formatter(statuses=True)) == "triaged"
    assert [item["key"] for item in calls] == ["scope__en", "scope__id", "scope__en"]
    assert calls[1]["format_func"]("triaged") == "Sudah ditriage"
    state[calls[1]["key"]] = "active"
    calls[1]["on_change"]()
    assert state["scope"] == "active"


def test_full_console_language_switch_never_submits_expensive_actions():
    at = AppTest.from_string('''
import streamlit as st
from unittest.mock import patch
from apps.streamlit import app
def forbidden(*args, **kwargs):
    raise AssertionError("Language reruns must not submit actions")
alerts = [{"alert_key": "orders|original", "alert_display_id": "DQ-original", "severity": "warning", "status": "triaged", "table_name": "dq.orders", "metric": "coverage", "dt": "2026-10-12"}]
with patch.object(app, "load_daily_quality_summary", return_value=({"dt": "2026-10-12", "check_counts": []}, "synthetic")), patch.object(app, "load_alert_rows", return_value=alerts), patch.object(app, "answer_ui_copilot_question", side_effect=forbidden), patch.object(app, "run_selected_alert_triage", side_effect=forbidden), patch.object(app, "create_ui_backfill_approval_request", side_effect=forbidden):
    app.main()
''').run()
    assert not at.exception
    selector_key = "render_incident_workspace_selectbox_0"
    selected_incident = at.session_state[selector_key]
    at.session_state["incident_detail_tabs"] = "AI Analysis"
    at.run()
    assert not at.exception
    for language in ("id", "en"):
        at.session_state[localization.LANGUAGE_KEY] = language
        at.run()
        assert not at.exception
        assert at.session_state[selector_key] == selected_incident
        assert at.session_state["incident_detail_tabs"] == "AI Analysis"
        assert at.session_state["primary_workspace_tabs"] == "Incidents"


def test_latest_snapshot_does_not_display_a_misleading_today_date():
    at = AppTest.from_string('''
from apps.streamlit import app
app.render_sidebar_filters()
''').run()
    assert not at.exception
    assert len(at.sidebar.date_input) == 0
    at.sidebar.radio[0].set_value("Choose date").run()
    assert not at.exception and len(at.sidebar.date_input) == 1
    at.sidebar.date_input[0].set_value(datetime(2026, 10, 5).date()).run()
    selected = at.sidebar.date_input[0].value
    at.session_state[localization.LANGUAGE_KEY] = "id"
    at.run()
    assert not at.exception and at.sidebar.date_input[0].value == selected


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_language_selection_uses_accessible_semantic_theme_tokens(theme):
    css = app.build_page_style_css(theme)
    assert 'button[data-variant="segmented_control"][aria-checked="true"]' in css
    assert "color: var(--dq-info-text) !important;" in css
    assert "background: var(--dq-info-bg) !important;" in css
    assert 'button[data-variant="segmented_control"]:focus-visible' in css
    assert ".stApp {" not in css, "Native Streamlit must own the live canvas theme."
    assert "color-scheme: light dark" not in css
