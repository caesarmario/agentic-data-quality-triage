####
## Streamlit Evidence-Gated Resolution Helper Tests
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

from __future__ import annotations

import ast
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from apps.common.control_plane import (
    ControlPlaneClient,
    ControlPlaneResponseError,
    ControlPlaneTransportError,
)
from apps.streamlit import resolution as ui


# --- Defining Synthetic Contracts And A Network-Free API Client
ALERT_REF = "DQ-20261005-3B55A6"
NOW = datetime(2026, 10, 11, 10, 0, tzinfo=timezone.utc)
APP_PATH = Path(__file__).resolve().parents[1] / "apps" / "streamlit" / "app.py"


def test_sync_notice_does_not_invent_a_closure_decision():
    assert 'No closure decision has been saved' in ui.synchronization_notice(False)
    assert 'The decision is saved' in ui.synchronization_notice(True)


def request_payload(status="pass", request_id="REV-test-1", revision=4):
    """Build one synthetic response without reading warehouse or runtime state."""
    return {
        "request_id": request_id, "alert_ref": ALERT_REF, "incident_revision": revision,
        "status": status, "created_at": (NOW - timedelta(minutes=2)).isoformat(),
        "finished_at": (NOW - timedelta(minutes=1)).isoformat() if status not in {"queued", "running"} else None,
        "expires_at": (NOW + timedelta(minutes=9)).isoformat() if status == "pass" else None,
        "dag_run_id": "manual__revalidation_test_1",
        "result": {"expected": 25, "observed": 25 if status == "pass" else 24,
                   "summary": "25 dari 25 segmen sudah tersedia." if status == "pass" else "Masih ada segmen yang belum lengkap.",
                   "evidence_references": ["synthetic://dq/check-1"]},
        "error_code": None,
    }


def preview_payload(request=None, kind="data_quality", **overrides):
    """Build authoritative scope and lifecycle state for a single incident."""
    return {
        "alert_ref": ALERT_REF, "incident_revision": 4, "status": "triaged",
        "scope": {"kind": kind, "table_name": "dq.fct_orders_daily", "dt": "2026-10-05",
                  "metric": "segment_coverage__country_channel", "dimension": "country_channel"},
        "blockers": [], "available": True, "request": request,
        "resolution": None, "synchronization_pending": False, **overrides,
    }


class FakeClient(ControlPlaneClient):
    """Record API calls; never execute HTTP, Airflow, warehouse, or provider work."""

    def __init__(self, *responses, token="test-only-not-a-real-token"):
        super().__init__("http://unit.invalid", approval_token=token)
        self.responses = list(responses)
        self.calls = []

    def request_json(self, method, path, **kwargs):
        self.calls.append({"method": method, "path": path, **kwargs})
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return deepcopy(result)


# --- Defining Readability And Safety Tests
@pytest.mark.parametrize("status, heading", [
    ("queued", "Check queued."),
    ("running", "Check running."),
    ("pass", "Check passed."),
    ("blocked", "An issue remains."),
    ("error", "Check incomplete."),
    ("expired", "Check evidence has expired."),
])
def test_business_status_not_dag_success_controls_closure(status, heading):
    request = request_payload(status)
    view = ui.build_resolution_view(preview_payload(request), now=NOW)
    assert view["heading"] == heading
    assert view["can_resolve"] is (status == "pass")
    assert view["can_revalidate"] is (status not in {"queued", "running"})
    assert view["resolved"] is False


@pytest.mark.parametrize("kind", ["dq", "data_quality", "dq_failure", "schema_drift"])
def test_data_quality_and_schema_use_same_api_gates(kind):
    view = ui.build_resolution_view(preview_payload(request_payload(), kind), now=NOW)
    assert view["can_resolve"] is True
    assert view["scope"]["kind"] == kind
    assert view["expected"] == view["observed"] == 25


def test_missing_request_is_not_treated_as_pass():
    view = ui.build_resolution_view(preview_payload(), now=NOW)
    assert view["can_resolve"] is False
    assert view["can_revalidate"] is True
    assert view["status"] == "none"


def test_unconfigured_airflow_shows_unavailable_and_disables_close():
    view = ui.build_resolution_view(preview_payload(request_payload(), available=False), now=NOW)
    assert view["heading"] == "Check unavailable."
    assert not view["can_resolve"] and not view["can_revalidate"]


def test_remediation_blockers_disable_closure_and_new_check_consistently_with_api():
    view = ui.build_resolution_view(preview_payload(request_payload(), blockers=["Remediation masih berjalan."]), now=NOW)
    assert view["blockers"] == ["Remediation masih berjalan."]
    assert not view["can_resolve"]
    assert not view["can_revalidate"]


def test_revision_change_requires_fresh_check():
    view = ui.build_resolution_view(preview_payload(request_payload(revision=3)), now=NOW)
    assert not view["can_resolve"]
    assert view["heading"] == "The incident state has changed."


@pytest.mark.parametrize("minutes, fresh", [(0, True), (8, True), (9, False), (10, False)])
def test_pass_expires_at_strict_boundary(minutes, fresh):
    assert ui.evidence_is_current(request_payload(), now=NOW + timedelta(minutes=minutes)) is fresh


@pytest.mark.parametrize("field,value", [
    ("finished_at", None), ("expires_at", None), ("finished_at", "not-a-date"),
    ("expires_at", "2026-10-11T12:00:00"),
    ("finished_at", (NOW + timedelta(seconds=1)).isoformat()),
])
def test_malformed_naive_or_future_evidence_is_not_current(field, value):
    request = request_payload()
    request[field] = value
    assert ui.evidence_is_current(request, now=NOW) is False


def test_provider_or_backend_cannot_extend_ui_ttl_beyond_ten_minutes():
    request = request_payload()
    request["expires_at"] = (NOW + timedelta(hours=1)).isoformat()
    assert not ui.evidence_is_current(request, now=NOW + timedelta(minutes=9))


def test_pending_projection_is_not_hidden_by_success_state():
    view = ui.build_resolution_view(preview_payload(request_payload(), synchronization_pending=True), now=NOW)
    assert view["synchronization_pending"] is True
    assert view["resolved"] is False


def test_resolved_incident_does_not_offer_close_or_repeat_old_failure():
    request = request_payload("blocked")
    preview = preview_payload(request, status="resolved", resolution={
        "resolved_by": "Mario", "resolution_reason": "Bukti terbaru lengkap.",
        "resolution_request_id": "REV-earlier-pass",
    }, synchronization_pending=True)
    view = ui.build_resolution_view(preview, now=NOW)
    assert view["heading"] == "The incident is already closed."
    assert not view["can_resolve"] and not view["can_revalidate"]
    assert view["expected"] is None and view["observed"] is None and view["summary"] == ""
    assert view["resolution"]["resolved_by"] == "Mario"
    assert view["synchronization_pending"]


def test_closed_incident_shows_only_correlated_closure_evidence():
    request = request_payload()
    preview = preview_payload(request, status="resolved", resolution={"resolution_request_id": request["request_id"]})
    view = ui.build_resolution_view(preview, now=NOW)
    assert view["expected"] == view["observed"] == 25


def test_previous_resolution_does_not_close_a_new_incident_episode():
    preview = preview_payload(request_payload("blocked"), resolution={"resolution_reason": "Penutupan episode lama."})
    view = ui.build_resolution_view(preview, now=NOW)
    assert view["resolved"] is False
    assert view["heading"] == "An issue remains."


@pytest.mark.parametrize("actor", ["", "   ", "a" * 121, "Mario\nAdmin", "Mario\tAdmin"])
def test_operator_name_is_required_and_bounded(actor):
    with pytest.raises(ValueError):
        ui.validate_operator_input(actor, "Alasan cukup.")


@pytest.mark.parametrize("reason", ["", "    ", "abcd", "x" * 1001])
def test_reason_length_is_five_to_one_thousand_after_trim(reason):
    with pytest.raises(ValueError):
        ui.validate_operator_input("Mario", reason)


@pytest.mark.parametrize("reason", ["abcde", "x" * 1000, "  Bukti lengkap.  "])
def test_valid_reason_and_operator_are_trimmed(reason):
    assert ui.validate_operator_input("  Mario  ", reason) == ("Mario", reason.strip())


# --- Defining Session, Rerun, And Confirmation Tests
def test_session_init_and_rerun_never_create_requests():
    state = {}
    first = ui.resolution_session(state, ALERT_REF)
    second = ui.resolution_session(state, ALERT_REF)
    assert first is second
    assert first["request"] is None and first["create_attempt"] is None
    assert first["confirmation"] is None and not first["open"]


def test_sessions_are_incident_scoped_and_survive_switch_back():
    state = {}
    first = ui.resolution_session(state, ALERT_REF)
    first["actor"] = "Mario"
    first["request"] = request_payload("running")
    other = ui.resolution_session(state, "DQ-20261008-ABCDEF")
    assert other["actor"] == "" and other["request"] is None
    assert ui.resolution_session(state, ALERT_REF) is first


def active_state():
    state = {ui.ACTIVE_PANEL_KEY: ALERT_REF, "primary_workspace_tabs": "Incidents", "incident_detail_tabs": "Overview"}
    ui.resolution_session(state, ALERT_REF)["open"] = True
    return state


@pytest.mark.parametrize("changes", [
    {"primary_workspace_tabs": "Approvals"}, {"primary_workspace_tabs": "Agent Reliability"},
    {"incident_detail_tabs": "Evidence"}, {"incident_detail_tabs": "AI Analysis"},
    {"incident_detail_tabs": "Audit & Recovery"}, {ui.ACTIVE_PANEL_KEY: None},
    {ui.ACTIVE_PANEL_KEY: "DQ-20261008-ABCDEF"},
])
def test_polling_stops_when_panel_no_longer_active(changes):
    state = active_state()
    assert ui.resolution_panel_is_active(state, ALERT_REF)
    state.update(changes)
    assert not ui.resolution_panel_is_active(state, ALERT_REF)


def test_closed_or_uninitialized_panel_never_polls():
    assert not ui.resolution_panel_is_active({}, ALERT_REF)
    state = active_state()
    ui.resolution_session(state, ALERT_REF)["open"] = False
    assert not ui.resolution_panel_is_active(state, ALERT_REF)


def test_revalidation_key_remains_identical_after_rerun_or_ambiguous_timeout():
    state = ui.resolution_session({}, ALERT_REF)
    first = ui.prepare_revalidation_attempt(state, "Mario")
    second = ui.prepare_revalidation_attempt(state, " Mario ")
    assert first == second
    assert state["request"] is None
    first["actor"] = "changed outside state"
    assert state["create_attempt"]["actor"] == "Mario"


def test_uncertain_request_cannot_change_actor_for_retry():
    state = ui.resolution_session({}, ALERT_REF)
    ui.prepare_revalidation_attempt(state, "Mario")
    with pytest.raises(ValueError, match="previous request outcome is unknown"):
        ui.prepare_revalidation_attempt(state, "Operator lain")


def test_timeout_reconciliation_does_not_reuse_old_key_for_a_new_check():
    state = ui.resolution_session({}, ALERT_REF)
    old = request_payload("blocked", request_id="REV-old")
    state["request"] = old
    first = ui.prepare_revalidation_attempt(state, "Mario")
    ui.reconcile_revalidation_attempt(state, None)
    ui.reconcile_revalidation_attempt(state, old)
    assert ui.prepare_revalidation_attempt(state, "Mario") == first
    new = request_payload("blocked", request_id="REV-new")
    ui.reconcile_revalidation_attempt(state, new)
    state["request"] = new
    second = ui.prepare_revalidation_attempt(state, "Mario")
    assert first["idempotency_key"] != second["idempotency_key"]


def test_uncertain_first_check_is_reconciled_without_another_post():
    state = ui.resolution_session({}, ALERT_REF)
    ui.prepare_revalidation_attempt(state, "Mario")
    ui.reconcile_revalidation_attempt(state, request_payload("running"))
    assert state["create_attempt"] is None
    assert state["create_previous_request_id"] is None


def test_confirm_draft_is_stable_but_does_not_mutate_incident():
    state = ui.resolution_session({}, ALERT_REF)
    preview, request = preview_payload(), request_payload()
    first = ui.prepare_resolution_confirmation(state, preview, request, actor="Mario", reason="Bukti lengkap.", now=NOW)
    second = ui.prepare_resolution_confirmation(state, preview, request, actor="Mario", reason="Bukti lengkap.", now=NOW)
    assert first == second
    assert first["expected_incident_revision"] == 4
    assert state["request"] is None
    assert set(first) == {"resolution_request_id", "expected_incident_revision", "actor", "reason", "idempotency_key"}


def test_changed_decision_content_requires_a_new_confirmation_key():
    state = ui.resolution_session({}, ALERT_REF)
    preview, request = preview_payload(), request_payload()
    first = ui.prepare_resolution_confirmation(state, preview, request, actor="Mario", reason="Bukti lengkap.", now=NOW)
    second = ui.prepare_resolution_confirmation(state, preview, request, actor="Mario", reason="Segmen sudah lengkap.", now=NOW)
    assert first["idempotency_key"] != second["idempotency_key"]


@pytest.mark.parametrize("status", ["queued", "running", "blocked", "error", "expired"])
def test_cannot_prepare_confirmation_without_fresh_pass(status):
    with pytest.raises(ValueError, match="Cannot close yet"):
        ui.prepare_resolution_confirmation({}, preview_payload(), request_payload(status),
                                           actor="Mario", reason="Bukti lengkap.", now=NOW)


# --- Defining API Boundary And Error Tests
def test_api_reads_use_shared_client_and_encoded_identifiers():
    request, preview = request_payload(), preview_payload()
    client = FakeClient(preview, request)
    assert ui.fetch_resolution_preview(client, ALERT_REF) == preview
    assert ui.fetch_revalidation(client, request["request_id"], alert_ref=ALERT_REF) == request
    assert client.calls == [
        {"method": "GET", "path": f"/api/v1/alerts/{ALERT_REF}/resolution-preview"},
        {"method": "GET", "path": "/api/v1/incident-revalidations/REV-test-1"},
    ]


def test_request_sends_identity_only_and_required_authorization():
    client = FakeClient(request_payload("queued"))
    ui.request_revalidation(client, ALERT_REF, actor=" Mario ", idempotency_key="ui-check-stable")
    call = client.calls[0]
    assert call["path"] == f"/api/v1/alerts/{ALERT_REF}/revalidations"
    assert call["method"] == "POST"
    assert call["json_body"] == {"actor": "Mario", "idempotency_key": "ui-check-stable"}
    assert call["headers"] == {"X-Control-Plane-Token": client.approval_token}


def test_final_decision_requires_explicit_confirmation_and_exact_payload():
    client = FakeClient({"status": "resolved", "synchronization_pending": True})
    draft = ui.prepare_resolution_confirmation({}, preview_payload(), request_payload(), actor="Mario", reason="Bukti lengkap.", now=NOW)
    with pytest.raises(ValueError):
        ui.confirm_resolution(client, ALERT_REF, draft, confirmed=False)
    with pytest.raises(ValueError):
        ui.confirm_resolution(client, ALERT_REF, {**draft, "status": "pass"}, confirmed=True)
    assert client.calls == []
    result = ui.confirm_resolution(client, ALERT_REF, draft, confirmed=True)
    assert result["synchronization_pending"] is True
    assert client.calls[0] == {"method": "POST", "path": f"/api/v1/alerts/{ALERT_REF}/resolve",
                               "headers": {"X-Control-Plane-Token": client.approval_token}, "json_body": draft}


def test_missing_api_or_token_never_falls_back_to_local_write():
    with pytest.raises(ControlPlaneResponseError):
        ui.request_revalidation(None, ALERT_REF, actor="Mario", idempotency_key="key")
    client = FakeClient(request_payload("queued"), token="")
    with pytest.raises(ControlPlaneResponseError):
        ui.request_revalidation(client, ALERT_REF, actor="Mario", idempotency_key="key")
    assert client.calls == []


def test_timeout_does_not_retry_or_switch_to_local_tools():
    client = FakeClient(ControlPlaneTransportError("SENSITIVE transport detail"))
    state = ui.resolution_session({}, ALERT_REF)
    attempt = ui.prepare_revalidation_attempt(state, "Mario")
    with pytest.raises(ControlPlaneTransportError):
        ui.request_revalidation(client, ALERT_REF, **attempt)
    assert len(client.calls) == 1
    assert ui.prepare_revalidation_attempt(state, "Mario") == attempt


@pytest.mark.parametrize("status, expected", [
    (401, "Closure access"), (403, "do not have access"),
    (404, "not found"), (409, "Cannot close yet"),
    (422, "Invalid input"), (503, "Check unavailable"),
])
def test_http_error_copy_hides_raw_error_and_credentials(status, expected):
    cause = RuntimeError("Bearer sensitive-key")
    cause.response = SimpleNamespace(status_code=status)
    error = ControlPlaneResponseError("sensitive response body")
    error.__cause__ = cause
    message = ui.resolution_error_message(error)
    assert expected in message
    assert "sensitive" not in message and "Bearer" not in message


def test_transport_error_reports_uncertainty_not_failed_or_successful_decision():
    message = ui.resolution_error_message(ControlPlaneTransportError("sensitive-key"))
    assert "request outcome is unknown" in message
    assert "sensitive-key" not in message


@pytest.mark.parametrize("field", ["scope", "available", "blockers", "incident_revision", "request", "resolution", "synchronization_pending"])
def test_incomplete_preview_fails_closed(field):
    preview = preview_payload()
    del preview[field]
    with pytest.raises(ControlPlaneResponseError):
        ui.fetch_resolution_preview(FakeClient(preview), ALERT_REF)


@pytest.mark.parametrize("changes", [
    {"alert_ref": "DQ-another"}, {"incident_revision": True}, {"incident_revision": "4"},
    {"available": "true"}, {"synchronization_pending": None}, {"status": "pass"}, {"blockers": [None]},
])
def test_malformed_preview_fails_closed(changes):
    with pytest.raises(ControlPlaneResponseError):
        ui.validate_preview(preview_payload(**changes), ALERT_REF)


@pytest.mark.parametrize("changes", [
    {"alert_ref": "DQ-other"}, {"request_id": "another"}, {"status": "success"},
    {"status": "warn"}, {"incident_revision": None}, {"result": None}, {"finished_at": None},
])
def test_mismatched_or_malformed_request_is_not_closure_evidence(changes):
    request = {**request_payload(), **changes}
    with pytest.raises(ControlPlaneResponseError):
        ui.fetch_revalidation(FakeClient(request), "REV-test-1", alert_ref=ALERT_REF)


@pytest.mark.parametrize("references", [[], "synthetic://evidence", [""], [None]])
def test_passing_result_requires_evidence_references(references):
    request = request_payload()
    request["result"]["evidence_references"] = references
    with pytest.raises(ControlPlaneResponseError):
        ui.validate_revalidation(request, alert_ref=ALERT_REF)


@pytest.mark.parametrize("field", ["expected", "observed"])
def test_pass_with_missing_observation_cannot_enable_close(field):
    request = request_payload()
    request["result"][field] = None
    with pytest.raises(ControlPlaneResponseError):
        ui.validate_revalidation(request, alert_ref=ALERT_REF)


def test_path_segments_are_encoded_not_interpreted_as_endpoints():
    ref = "DQ/example?token=no"
    client = FakeClient(preview_payload(alert_ref=ref))
    ui.fetch_resolution_preview(client, ref)
    assert client.calls[0]["path"] == "/api/v1/alerts/DQ%2Fexample%3Ftoken%3Dno/resolution-preview"


# --- Defining Static Integration Tests (No Live UI Or Mutation)
def test_closure_notice_preserves_identity_and_closes_its_panel():
    state = {}
    ui.resolution_session(state, ALERT_REF)["open"] = True
    state[ui.ACTIVE_PANEL_KEY] = ALERT_REF
    ui.record_resolution_success(state, ALERT_REF, {"alert_ref": ALERT_REF, "status": "resolved", "synchronization_pending": False})
    assert state[ui.RESOLUTION_NOTICE_KEY]["alert_ref"] == ALERT_REF
    assert state[ui.ACTIVE_PANEL_KEY] is None
    assert not ui.resolution_session(state, ALERT_REF)["open"]
    with pytest.raises(ControlPlaneResponseError):
        ui.record_resolution_success(state, ALERT_REF, {"alert_ref": "another", "status": "resolved"})


def test_overview_and_recovery_integration_preserve_tabs_and_theme():
    source = APP_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = {node.name: ast.get_source_segment(source, node) for node in tree.body if isinstance(node, ast.FunctionDef)}
    overview = functions["render_incident_workspace"]
    recovery = functions["render_audit_recovery_workspace"]
    assert overview.index("if overview_tab.open:") < overview.index("render_resolution_panel(") < overview.index("elif evidence_tab.open:")
    assert "render_resolution_technical_details(" in recovery
    assert 'st.session_state[ACTIVE_PANEL_KEY] = None' in functions["main"]
    assert "on_resolved=load_alert_rows.clear" in overview
    assert 'on_change="rerun"' in overview


def test_resolution_module_never_imports_mutation_or_llm_tools():
    source = Path(ui.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    assert all(not item.startswith(("agent.", "pipelines.", "apps.discord_bot")) for item in imports)
    assert "subprocess" not in source
    assert "EXTERNAL_LLM_ENABLED" not in source


def test_timer_guard_precedes_all_reads_and_posts_require_buttons():
    source = Path(ui.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    render = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_render_resolution_fragment")
    assert ast.unparse(render.decorator_list[0]) == "st.fragment(run_every='5s')"
    body = ast.get_source_segment(source, render)
    assert body.index("if not resolution_panel_is_active") < body.index("fetch_resolution_preview")
    parents = {}
    for parent in ast.walk(render):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    calls = [node for node in ast.walk(render) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
             and node.func.id in {"request_revalidation", "confirm_resolution"}]
    assert len(calls) == 2
    for call in calls:
        ancestor = parents.get(call)
        found_button_guard = False
        while ancestor is not None:
            if isinstance(ancestor, ast.If) and isinstance(ancestor.test, ast.Call):
                function = ancestor.test.func
                found_button_guard |= isinstance(function, ast.Attribute) and function.attr == "button"
            ancestor = parents.get(ancestor)
        assert found_button_guard, "Mutation must require an explicit button click, not timer/rerun execution."
