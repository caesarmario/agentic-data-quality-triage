####
## Evidence-Gated Discord Resolution Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Inner-loop feedback only; Airflow and legitimate human clicks provide acceptance."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from apps.common.control_plane import ControlPlaneResponseError, ControlPlaneTransportError
from apps.common.i18n import current_language, language_context
from apps.discord_bot import formatters, interactions, preferences, resolution, service


REF = "DQ-20261005-3B55A6"
REQUEST_ID = "12345678-1234-1234-1234-123456789012"


def check_request(status="pass", **changes):
    now = datetime.now(timezone.utc)
    request = {
        "request_id": REQUEST_ID, "alert_ref": REF, "incident_revision": 4,
        "status": status, "created_at": now.isoformat(),
        "finished_at": (now - timedelta(seconds=10)).isoformat(),
        "expires_at": (now + timedelta(minutes=9)).isoformat(),
        "dag_run_id": "incident_revalidation__one",
        "scope": {"kind": "dq", "table_name": "dq.fct_orders_daily", "dt": "2026-10-05",
                  "metric": "segment_coverage__country_channel", "dimension": "country_channel"},
        "result": {"status": "pass" if status == "pass" else "blocked", "expected": 25, "observed": 25, "summary": "25 dari 25 segmen sudah tersedia.",
                   "evidence_references": ["dq.dq_check_results/check-one"]}, "error_code": "",
    }
    request.update(changes)
    return request


def preview(request=None, **changes):
    value = {
        "alert_ref": REF, "incident_revision": 4, "status": "triaged",
        "scope": check_request()["scope"], "available": True, "blockers": [],
        "request": request, "resolution": None, "synchronization_pending": False,
    }
    value.update(changes)
    return value


class Response:
    def __init__(self):
        self.done = False
        self.events = []
        self.modal = None

    def is_done(self):
        return self.done

    async def defer(self, **kwargs):
        self.done = True
        self.events.append(("defer", kwargs))

    async def send_message(self, content=None, **kwargs):
        self.done = True
        self.events.append(("send", {"content": content, **kwargs}))

    async def edit_message(self, **kwargs):
        self.done = True
        self.events.append(("edit", kwargs))

    async def send_modal(self, modal):
        self.done = True
        self.modal = modal
        self.events.append(("modal", modal))


def interaction(action="resolve", request_id="", **changes):
    custom_id = f"dq:v1:{action}:{REF}" + (f":{request_id}" if request_id else "")
    embed = discord.Embed.from_dict({"fields": [{"name": "Alert Ref", "value": f"`{REF}`"}]})
    ops = SimpleNamespace(guild=SimpleNamespace(id=10), permissions_for=lambda user: SimpleNamespace(view_channel=True))
    value = SimpleNamespace(
        id=900, user=SimpleNamespace(id=200, display_name="Operator", bot=False, roles=[]),
        guild_id=10, channel_id=300, channel=None,
        message=SimpleNamespace(id=700, author=SimpleNamespace(id=100), channel=SimpleNamespace(id=300),
                                embeds=[embed], components=[SimpleNamespace(children=[SimpleNamespace(custom_id=custom_id)])]),
        data={"custom_id": custom_id}, response=Response(),
        followup=SimpleNamespace(send=AsyncMock()), edit_original_response=AsyncMock(),
        client=SimpleNamespace(user=SimpleNamespace(id=100), get_channel=Mock(return_value=ops), fetch_channel=AsyncMock(return_value=ops)),
    )
    for key, item in changes.items():
        setattr(value, key, item)
    return value


def origin():
    return resolution.ResolutionOrigin(REF, REQUEST_ID, 200, 10, 300, 700, 100)


@pytest.fixture(autouse=True)
def bounded_environment(monkeypatch):
    monkeypatch.setattr(resolution, "get_user_language", Mock(return_value="en"))
    monkeypatch.setattr(preferences, "get_user_language", Mock(return_value="en"))
    monkeypatch.setattr(interactions, "GUILD_ID", 10)
    monkeypatch.setattr(interactions, "ALERTS_CHANNEL_ID", 400)
    monkeypatch.setattr(interactions, "TRIAGE_CHANNEL_ID", 300)
    monkeypatch.setattr(interactions, "OPS_CHANNEL_ID", 500)
    monkeypatch.setattr(interactions, "write_interaction_audit", Mock())
    monkeypatch.delenv("DISCORD_APPROVER_ROLE_IDS", raising=False)
    monkeypatch.delenv("DISCORD_OPERATOR_UI_URL", raising=False)
    for name in ("answer_discord_question", "run_discord_triage", "create_discord_backfill_approval_request",
                 "decide_discord_approval_request", "synchronize_incident_message"):
        monkeypatch.setattr(interactions, name, Mock(side_effect=AssertionError("Resolve must not call this operation")))


def buttons(render):
    return [item for row in render.payload.get("components", []) for item in row["components"]]


def test_incident_and_triage_have_resolve_without_overflow():
    rows = formatters.incident_action_rows(REF, "TRIAGED", "https://example.test/workspace")
    assert [item.label for item in rows[0].actions] == ["Investigate", "Evidence", "Ask AI"]
    assert [item.label for item in rows[1].actions] == ["Open Workspace", "Resolve"]
    triage = formatters.triage_action_rows(REF, has_report=True)
    assert [item.label for item in triage[0].actions] == ["Run New Investigation", "Evidence", "Ask AI", "View Report"]
    assert [item.label for item in triage[1].actions] == ["Resolve", "Draft Action"]
    assert all(len(row.actions) <= 5 for row in triage)


def test_resolution_uses_existing_registered_dynamic_class():
    registered = []
    interactions.register_persistent_interactions(SimpleNamespace(add_dynamic_items=lambda *items: registered.extend(items)))
    assert interactions.IncidentActionButton in registered
    view = interactions.build_discord_view(formatters.render_resolution_preview_embed(preview(), check_nonce="nonce123"))
    assert view.is_persistent()
    assert all(isinstance(item, interactions.IncidentActionButton) for item in view.children)
    assert view.children[0].request_id == "nonce123"


@pytest.mark.parametrize("action", ["resolve", "resolve_check", "resolve_status", "resolve_close", "resolve_evidence", "resolve_cancel"])
def test_persistent_ids_are_reconstructed(action):
    suffix = "" if action in {"resolve", "resolve_cancel"} else REQUEST_ID
    item = interactions.IncidentActionButton(action, REF, suffix)
    match = interactions.INCIDENT_ACTION_PATTERN.fullmatch(item.item.custom_id)
    rebuilt = asyncio.run(interactions.IncidentActionButton.from_custom_id(None, None, match))
    assert rebuilt.action == action and rebuilt.target == REF and rebuilt.request_id == suffix
    assert len(rebuilt.item.custom_id) <= 100


@pytest.mark.parametrize("action,request_id", [("resolve", "unexpected"), ("resolve_close", ""), ("evidence", "unexpected")])
def test_invalid_component_contract_is_rejected(action, request_id):
    with pytest.raises(ValueError):
        interactions.IncidentActionButton(action, REF, request_id)


def test_oversized_resolution_custom_id_is_rejected():
    with pytest.raises(ValueError):
        formatters.resolution_action("resolve_evidence", "Evidence", "DQ-" + "A" * 70, REQUEST_ID)


@pytest.mark.parametrize("status,title", [
    ("queued", "\u23F3 Check in progress"), ("running", "\u23F3 Check in progress"),
    ("blocked", "\u26A0\uFE0F Closure blocked"), ("error", "Check incomplete"),
    ("expired", "Check evidence expired"),
])
def test_non_pass_states_never_offer_closure(status, title):
    request = check_request(status)
    render = formatters.render_resolution_preview_embed(preview(request), request)
    assert render.payload["embeds"][0]["title"] == title
    assert "Close Incident" not in {button["label"] for button in buttons(render)}
    assert render.provider == render.model == ""
    assert "Gemini" not in json.dumps(render.payload)


def test_notification_renderer_exposes_system_result_and_recoverable_buttons():
    render = resolution.render_revalidation_result(check_request())
    assert render.payload["embeds"][0]["title"] == "\u2705 Check passed"
    assert "Close Incident" in {button["label"] for button in buttons(render)}
    assert "Check Status" in {button["label"] for button in buttons(render)}
    assert "Cancel" not in {button["label"] for button in buttons(render)}
    assert render.payload["allowed_mentions"] == {"parse": []}
    assert "No data repairs" in render.payload["embeds"][0]["footer"]["text"]
    # Re-delivery of a business-blocked request has a stable nonce/content hash.
    blocked = check_request("blocked")
    assert resolution.render_revalidation_result(blocked).content_hash == resolution.render_revalidation_result(blocked).content_hash


def test_missing_airflow_configuration_is_not_fake_pass():
    render = formatters.render_resolution_preview_embed(preview(available=False))
    assert render.payload["embeds"][0]["title"] == "Check unavailable"
    assert "Check Again" not in {button["label"] for button in buttons(render)}


@pytest.mark.parametrize("change", [
    {"status": "blocked"}, {"finished_at": None}, {"finished_at": "bad"},
    {"finished_at": datetime.now().isoformat()},
    {"finished_at": (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()},
    {"finished_at": (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()},
    {"expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()},
    {"result": {"summary": "pass", "evidence_references": []}}, {"result": None},
    {"result": {"status": "warn", "summary": "warning", "evidence_references": ["evidence-one"]}},
])
def test_incomplete_expired_or_future_proof_cannot_enable_close(change):
    request = check_request(**change)
    assert resolution.request_proof_is_fresh(request) is False
    assert resolution.closure_blocker(preview(request), request)


@pytest.mark.parametrize("change", [
    {"incident_revision": 5}, {"alert_ref": "DQ-20261008-72DF3B"},
    {"status": "resolved"}, {"available": False}, {"blockers": ["Remediation running"]},
    {"request": check_request(request_id="RES-new")},
])
def test_preview_changes_block_stale_pass(change):
    request = check_request()
    current = preview(request)
    current.update(change)
    assert resolution.closure_blocker(current, request)


def test_schema_date_is_historical_and_schema_warn_cannot_close():
    request = check_request("blocked", scope={"kind": "schema_drift", "table_name": "acceptance.orders", "dt": "2026-10-05"})
    render = resolution.render_revalidation_result(request)
    fields = {field["name"]: field["value"] for field in render.payload["embeds"][0]["fields"]}
    assert "Detection date (historical)" in fields
    assert "Data date" not in fields
    assert "Close Incident" not in {button["label"] for button in buttons(render)}


def test_resolved_primary_card_hides_old_rca_and_provider():
    request = check_request()
    alert = {
        "alert_key": "orders|dq_failure|2026-10-05|dq.fct_orders_daily|coverage|country_channel",
        "alert_display_id": REF, "status": "resolved", "severity": "warning",
        "table_name": "dq.fct_orders_daily", "metric": "segment_coverage__country_channel", "dt": "2026-10-05",
        "details": {"resolution": {
            "resolution_request_id": REQUEST_ID, "result": request["result"],
            "resolved_by": "discord:200:Mario", "resolved_at": "2026-10-11T12:00:00Z",
            "resolution_reason": "Segmen dimuat ulang. @everyone <@123> " + "x" * 1200,
        }},
    }
    payload = formatters.build_discord_incident_payload(
        alert, narrative="STALE RCA: still missing data", llm_runtime={"provider": "gemini", "model": "gemini-flash", "used_heuristic": False},
    )
    render = formatters.render_incident_embed(payload, "https://example.test/workspace")
    serialized = json.dumps(render.payload)
    assert "STALE RCA" not in serialized and "Gemini" not in serialized
    assert "25 dari 25" in serialized
    assert "discord:200" not in serialized
    assert [button["label"] for button in buttons(render)] == ["Evidence", "Open Workspace"]
    assert render.lifecycle == "RESOLVED" and render.narrative_source == "deterministic_fallback"
    assert all(len(field["value"]) <= 1024 for field in render.payload["embeds"][0]["fields"])
    assert render.payload["allowed_mentions"] == {"parse": []}
    assert "System check" in render.payload["embeds"][0]["footer"]["text"]


def test_historical_resolved_without_proof_does_not_invent_fresh_pass():
    render = formatters.render_resolved_incident_embed(REF, {}, {})
    assert "latest check passed" not in render.payload["embeds"][0]["title"]
    assert "Closure evidence is not available" in json.dumps(render.payload)


def test_resolved_evidence_does_not_reuse_original_failed_observation(monkeypatch):
    from datetime import date
    alert = SimpleNamespace(
        alert_display_id=REF, alert_key="system-key", status="resolved", severity="warning",
        table_name="dq.fct_orders_daily", dt=date(2026, 10, 5), metric="segment_coverage__country_channel",
        observed_value=24, expected_value=25, report_s3_uri="s3://report-history",
        details={"evidence_summary": "OLD FAILURE", "resolution": {"summary": "25 dari 25 segmen lengkap.", "expected": 25, "observed": 25}},
    )
    monkeypatch.setattr(service, "load_alert", Mock(return_value=alert))
    context = service.fetch_discord_evidence_context(REF)
    assert context["observed_value"] == 25
    render = formatters.render_evidence_embed(context)
    assert "OLD FAILURE" not in json.dumps(render.payload)
    assert render.payload["embeds"][0]["title"] == "\U0001F4CE Evidence · Incident Closure"


def test_resolved_triage_does_not_reintroduce_old_hypothesis():
    report = {"alert": {"alert_key": "system-key", "alert_display_id": REF, "status": "resolved"},
              "ai_investigation": {"what_happened": "OLD HYPOTHESIS", "source": "external_model", "provider": "gemini"}}
    render = formatters.render_triage_embed(report)
    assert "OLD HYPOTHESIS" not in json.dumps(render.payload)
    assert "Gemini" not in json.dumps(render.payload)
    assert [button["label"] for button in buttons(render)] == ["Evidence"]


def test_pending_synchronization_is_truthful():
    render = formatters.render_resolution_preview_embed(preview(status="resolved", synchronization_pending=True))
    assert "Synchronization pending" in json.dumps(render.payload)


def test_flat_backend_resolution_projection_renders_system_verification():
    decision = {"resolution_request_id": REQUEST_ID, "summary": "25 dari 25 segmen sudah tersedia.",
                "expected": 25, "observed": 25, "resolved_by": "Operator", "resolution_reason": "Pemeriksaan lolos."}
    render = formatters.render_resolved_incident_embed(REF, check_request()["scope"], decision)
    assert render.payload["embeds"][0]["title"] == "\u2705 RESOLVED - latest check passed"
    assert "25 dari 25" in json.dumps(render.payload)


def test_confirmation_card_including_reason_stays_within_discord_limits():
    request = check_request(result={"status": "pass", "summary": "x" * 5000, "evidence_references": ["evidence-one"]})
    render = formatters.render_resolution_confirmation_embed(preview(request), request, "r" * 1000)
    embed = render.payload["embeds"][0]
    size = len(embed["title"]) + len(embed["description"]) + len(embed["footer"]["text"])
    size += sum(len(field["name"]) + len(field["value"]) for field in embed["fields"])
    assert size <= 6000
    assert all(len(field["value"]) <= 1024 for field in embed["fields"])


@pytest.mark.parametrize("case", ["wrong_guild", "wrong_channel", "bot", "foreign_author", "missing_message", "wrong_target", "forged_component"])
def test_resolution_control_rejects_invalid_origin_before_api(monkeypatch, case):
    value = interaction()
    if case == "wrong_guild": value.guild_id = 11
    if case == "wrong_channel": value.channel_id = 900
    if case == "bot": value.user.bot = True
    if case == "foreign_author": value.message.author.id = 999
    if case == "missing_message": value.message.id = None
    if case == "wrong_target": value.message.embeds = []
    if case == "forged_component": value.message.components = []
    read = Mock()
    monkeypatch.setattr(resolution, "fetch_discord_resolution_preview", read)
    asyncio.run(resolution.handle_resolution_action(value, "resolve", REF))
    read.assert_not_called()
    assert value.response.events[0][1]["ephemeral"] is True


def test_preview_defers_before_backend_and_never_closes(monkeypatch):
    value = interaction()
    def read(reference):
        assert value.response.is_done()
        return preview()
    monkeypatch.setattr(resolution, "fetch_discord_resolution_preview", read)
    close = Mock()
    monkeypatch.setattr(resolution, "resolve_discord_incident", close)
    asyncio.run(resolution.handle_resolution_action(value, "resolve", REF))
    close.assert_not_called()
    assert value.followup.send.call_args.kwargs["ephemeral"] is True
    assert value.followup.send.call_args.kwargs["embeds"][0].title == "Close this incident?"


def test_check_requests_are_bounded_idempotent_and_do_not_wait_for_airflow(monkeypatch):
    value = interaction("resolve_check", "nonce123")
    monkeypatch.setattr(resolution, "fetch_discord_resolution_preview", Mock(return_value=preview()))
    create = Mock(return_value=check_request("queued", result=None, finished_at=None))
    monkeypatch.setattr(resolution, "create_discord_revalidation", create)
    asyncio.run(resolution.handle_resolution_action(value, "resolve_check", REF, "nonce123"))
    key = create.call_args.args[-1]
    asyncio.run(resolution.handle_resolution_action(interaction("resolve_check", "nonce123"), "resolve_check", REF, "nonce123"))
    assert create.call_args.args[-1] == key and len(key) == 64
    assert value.followup.send.call_args.kwargs["embeds"][0].title == "\u23F3 Check in progress"


@pytest.mark.parametrize("available,blockers,status", [(False, [], "open"), (True, ["pending remediation"], "open"), (True, [], "resolved")])
def test_recheck_cannot_bypass_backend_blockers(monkeypatch, available, blockers, status):
    monkeypatch.setattr(resolution, "fetch_discord_resolution_preview", Mock(return_value=preview(available=available, blockers=blockers, status=status)))
    create = Mock()
    monkeypatch.setattr(resolution, "create_discord_revalidation", create)
    asyncio.run(resolution.handle_resolution_action(interaction("resolve_check", "nonce123"), "resolve_check", REF, "nonce123"))
    create.assert_not_called()


def test_close_button_opens_modal_without_blocking_response_on_api(monkeypatch):
    read = Mock(side_effect=AssertionError("Modal must open before API reads"))
    monkeypatch.setattr(resolution, "fetch_discord_resolution_preview", read)
    value = interaction("resolve_close", REQUEST_ID)
    asyncio.run(interactions.IncidentActionButton("resolve_close", REF, REQUEST_ID).callback(value))
    assert isinstance(value.response.modal, resolution.ResolutionReasonModal)
    read.assert_not_called()


@pytest.mark.parametrize("case", ["no_ops", "denied_access", "wrong_ops_guild", "missing_role", "foreign_user", "foreign_message", "failed_check", "stale_revision", "different_request"])
def test_confirmation_rejects_before_resolution_post(monkeypatch, case):
    value = interaction()
    request = check_request()
    current = preview(request)
    if case == "no_ops": monkeypatch.setattr(interactions, "OPS_CHANNEL_ID", 0)
    if case == "denied_access": value.client.get_channel.return_value.permissions_for = lambda user: SimpleNamespace(view_channel=False)
    if case == "wrong_ops_guild": value.client.get_channel.return_value.guild.id = 11
    if case == "missing_role": monkeypatch.setenv("DISCORD_APPROVER_ROLE_IDS", "777")
    if case == "foreign_user": value.user.id = 999
    if case == "foreign_message": value.message.author.id = 999
    if case == "failed_check": request["status"] = "blocked"
    if case == "stale_revision": current["incident_revision"] = 5
    if case == "different_request": current["request"] = check_request(request_id="RES-new")
    monkeypatch.setattr(resolution, "fetch_discord_resolution_preview", Mock(return_value=current))
    monkeypatch.setattr(resolution, "fetch_discord_revalidation", Mock(return_value=request))
    close = Mock()
    monkeypatch.setattr(resolution, "resolve_discord_incident", close)
    view = resolution.ResolutionConfirmationView(origin(), 4, "Pemeriksaan terbaru lolos.")
    asyncio.run(view.confirm.callback(value))
    close.assert_not_called()
    assert value.followup.send.call_args.kwargs["ephemeral"] is True


def test_reason_requires_permission_and_explicit_confirmation(monkeypatch):
    request = check_request()
    monkeypatch.setattr(resolution, "fetch_discord_resolution_preview", Mock(return_value=preview(request)))
    monkeypatch.setattr(resolution, "fetch_discord_revalidation", Mock(return_value=request))
    close = Mock()
    monkeypatch.setattr(resolution, "resolve_discord_incident", close)
    value = interaction()
    value.message = None  # Discord modal submissions do not necessarily contain a message.
    modal = resolution.ResolutionReasonModal(origin())
    modal.reason._value = "Segmen lengkap dan pemeriksaan terbaru lolos."
    asyncio.run(modal.on_submit(value))
    close.assert_not_called()
    confirmation = value.followup.send.call_args.kwargs["view"]
    assert isinstance(confirmation, resolution.ResolutionConfirmationView)
    assert confirmation.incident_revision == 4
    assert "Confirm Close" in {item.label for item in confirmation.children}
    assert value.followup.send.call_args.kwargs["wait"] is True
    assert confirmation.confirmation_message is value.followup.send.return_value


@pytest.mark.parametrize("reason", ["    ", "abcd", "x" * 1001])
def test_reason_validation_runs_server_side(monkeypatch, reason):
    value = interaction()
    value.message = None
    modal = resolution.ResolutionReasonModal(origin())
    modal.reason._value = reason
    read = Mock()
    monkeypatch.setattr(resolution, "fetch_discord_resolution_preview", read)
    asyncio.run(modal.on_submit(value))
    read.assert_not_called()
    assert "5-1,000" in value.followup.send.call_args.args[0]


def test_concurrent_confirmation_makes_one_api_decision_and_no_repairs(monkeypatch):
    request = check_request()
    monkeypatch.setattr(resolution, "fetch_discord_resolution_preview", Mock(return_value=preview(request)))
    monkeypatch.setattr(resolution, "fetch_discord_revalidation", Mock(return_value=request))
    close = Mock(return_value={"alert_ref": REF, "status": "resolved", "resolved_by": "discord:200:Operator",
                               "resolved_at": "2026-10-11T12:00:00Z", "resolution_reason": "Pemeriksaan lolos.", "synchronization_pending": True})
    monkeypatch.setattr(resolution, "resolve_discord_incident", close)
    async def run():
        view = resolution.ResolutionConfirmationView(origin(), 4, "Pemeriksaan lolos.")
        one, two = interaction(), interaction()
        await asyncio.gather(view.confirm.callback(one), view.confirm.callback(two))
        return one, two
    one, two = asyncio.run(run())
    close.assert_called_once()
    assert close.call_args.args[:3] == (REF, REQUEST_ID, 4)
    assert close.call_args.args[-2:] == ("discord:200:Operator", "Pemeriksaan lolos.")
    outputs = json.dumps([str(one.followup.send.call_args), str(two.followup.send.call_args)])
    assert "already complete" in outputs
    assert interactions.synchronize_incident_message.call_count == 0


@pytest.mark.parametrize("current_pending", [False, True])
def test_resolved_card_uses_current_sync_state_not_decision_snapshot(current_pending):
    decision = {"resolution_request_id": REQUEST_ID, "summary": "Pemeriksaan lolos.",
                "resolved_by": "Operator", "synchronization_pending": True}
    rendered = formatters.render_resolved_incident_embed(
        REF, {}, decision, synchronization_pending=current_pending,
    )
    payload = json.dumps(rendered.payload["embeds"], ensure_ascii=False)
    assert ("Synchronization pending" in payload) is current_pending


def test_cancelled_confirmation_cannot_be_replayed(monkeypatch):
    close = Mock()
    monkeypatch.setattr(resolution, "resolve_discord_incident", close)
    async def run():
        view = resolution.ResolutionConfirmationView(origin(), 4, "Pemeriksaan lolos.")
        value = interaction()
        await view.cancel.callback(value)
        await view.confirm.callback(interaction())
        return value
    value = asyncio.run(run())
    close.assert_not_called()
    value.edit_original_response.assert_awaited_once()


@pytest.mark.parametrize("language", ["en", "id"])
def test_confirmation_timeout_retires_intent_without_decision_or_backend(monkeypatch, language):
    read, request, close, create = Mock(), Mock(), Mock(), Mock()
    monkeypatch.setattr(resolution, "fetch_discord_resolution_preview", read)
    monkeypatch.setattr(resolution, "fetch_discord_revalidation", request)
    monkeypatch.setattr(resolution, "resolve_discord_incident", close)
    monkeypatch.setattr(resolution, "create_discord_revalidation", create)
    message = SimpleNamespace(edit=AsyncMock())
    async def run():
        with language_context(language):
            view = resolution.ResolutionConfirmationView(origin(), 4, "Keep the original reason.")
        view.confirmation_message = message
        await view.on_timeout()
        await view.on_timeout()
        assert view._expired and not view._decided
        assert view.is_finished() and all(item.disabled for item in view.children)
        assert view.reason == "Keep the original reason."
        assert view.timeout == 120
        assert current_language() == "en"
        return view
    view = asyncio.run(run())
    message.edit.assert_awaited_once()
    edited = message.edit.call_args.kwargs
    assert edited["view"] is view
    assert edited["allowed_mentions"].to_dict() == {"parse": []}
    assert "embed" not in edited and "embeds" not in edited
    expected = "Confirmation expired. Open Resolve again" if language == "en" else "Konfirmasi kedaluwarsa. Buka Tutup Incident lagi"
    assert edited["content"].startswith(expected)
    read.assert_not_called()
    request.assert_not_called()
    close.assert_not_called()
    create.assert_not_called()
    resolution.get_user_language.assert_not_called()
    interactions.write_interaction_audit.assert_not_called()


@pytest.mark.parametrize("terminal", ["confirm", "cancel"])
def test_completed_confirmation_retires_origin_and_preserves_history(monkeypatch, terminal):
    check = check_request()
    monkeypatch.setattr(resolution, "fetch_discord_resolution_preview", Mock(return_value=preview(check)))
    monkeypatch.setattr(resolution, "fetch_discord_revalidation", Mock(return_value=check))
    close = Mock(return_value={"alert_ref": REF, "status": "resolved"})
    monkeypatch.setattr(resolution, "resolve_discord_incident", close)
    message = SimpleNamespace(edit=AsyncMock())
    value = interaction()
    async def run():
        view = resolution.ResolutionConfirmationView(origin(), 4, "Keep the original reason.")
        view.confirmation_message = message
        await getattr(view, terminal).callback(value)
        await view.on_timeout()
        assert view._decided and not view._expired and view.is_finished()
        assert all(item.disabled for item in view.children)
        assert view.reason == "Keep the original reason."
        return view
    view = asyncio.run(run())
    message.edit.assert_awaited_once()
    edited = message.edit.call_args.kwargs
    assert edited["view"] is view
    assert "embed" not in edited and "embeds" not in edited
    assert edited["allowed_mentions"].to_dict() == {"parse": []}
    value.edit_original_response.assert_not_awaited()
    if terminal == "confirm":
        close.assert_called_once()
        assert edited["content"] == "Closure saved through the shared API."
        assert "RESOLVED" in value.followup.send.call_args.kwargs["embeds"][0].title
    else:
        close.assert_not_called()
        assert edited["content"] == "Confirmation cancelled. No incident closure or data repairs."


@pytest.mark.parametrize("terminal", ["confirm", "cancel"])
@pytest.mark.parametrize("winner", ["timeout", "terminal"])
def test_confirmation_timeout_and_terminal_race_respects_lock(monkeypatch, terminal, winner):
    check = check_request()
    read = Mock(return_value=preview(check))
    request = Mock(return_value=check)
    close = Mock(return_value={"alert_ref": REF, "status": "resolved"})
    monkeypatch.setattr(resolution, "fetch_discord_resolution_preview", read)
    monkeypatch.setattr(resolution, "fetch_discord_revalidation", request)
    monkeypatch.setattr(resolution, "resolve_discord_incident", close)
    async def run():
        editing, release = asyncio.Event(), asyncio.Event()
        async def edit(**kwargs):
            editing.set()
            await release.wait()
        message = SimpleNamespace(edit=AsyncMock(side_effect=edit))
        view = resolution.ResolutionConfirmationView(origin(), 4, "Keep the original reason.")
        view.confirmation_message = message
        value = interaction()
        first = view.on_timeout() if winner == "timeout" else getattr(view, terminal).callback(value)
        first_task = asyncio.create_task(first)
        await asyncio.wait_for(editing.wait(), timeout=2)
        second = getattr(view, terminal).callback(value) if winner == "timeout" else view.on_timeout()
        second_task = asyncio.create_task(second)
        await asyncio.sleep(0)
        assert not second_task.done()
        release.set()
        await asyncio.wait_for(asyncio.gather(first_task, second_task), timeout=2)
        message.edit.assert_awaited_once()
        assert all(item.disabled for item in view.children) and view.is_finished()
        assert view._expired is (winner == "timeout")
        assert view._decided is (winner == "terminal")
        return message, value
    message, value = asyncio.run(run())
    if winner == "terminal" and terminal == "confirm":
        close.assert_called_once()
        assert message.edit.call_args.kwargs["content"] == "Closure saved through the shared API."
    else:
        close.assert_not_called()
        read.assert_not_called()
        request.assert_not_called()
    if winner == "timeout":
        assert message.edit.call_args.kwargs["content"].startswith("Confirmation expired.")
        assert value.followup.send.call_args.args[0].startswith("Confirmation expired.")
        interactions.write_interaction_audit.assert_not_called()


@pytest.mark.parametrize("terminal", ["timeout", "confirm", "cancel"])
def test_confirmation_edit_failure_never_creates_or_invalidates_decision(monkeypatch, terminal):
    check = check_request()
    read = Mock(return_value=preview(check))
    monkeypatch.setattr(resolution, "fetch_discord_resolution_preview", read)
    monkeypatch.setattr(resolution, "fetch_discord_revalidation", Mock(return_value=check))
    close = Mock(return_value={"alert_ref": REF, "status": "resolved"})
    monkeypatch.setattr(resolution, "resolve_discord_incident", close)
    warning = Mock()
    monkeypatch.setattr(resolution.logger, "warning", warning)
    error = AsyncMock()
    monkeypatch.setattr(resolution, "_resolution_error", error)
    message = SimpleNamespace(edit=AsyncMock(side_effect=RuntimeError("secret-key private-host raw-body")))
    value, late = interaction(), interaction()
    async def run():
        view = resolution.ResolutionConfirmationView(origin(), 4, "Keep the original reason.")
        view.confirmation_message = message
        if terminal == "timeout":
            await view.on_timeout()
        else:
            await getattr(view, terminal).callback(value)
        await view.on_timeout()
        await view.confirm.callback(late)
        assert view.is_finished() and all(item.disabled for item in view.children)
        assert view._expired is (terminal == "timeout")
        assert view._decided is (terminal != "timeout")
    asyncio.run(run())
    message.edit.assert_awaited_once()
    error.assert_not_awaited()
    warning.assert_called_once_with("Discord confirmation edit failed safely | error_type=%s", "RuntimeError")
    assert "secret-key" not in str(value.followup.send.call_args_list)
    assert "private-host" not in str(late.followup.send.call_args_list)
    if terminal == "confirm":
        close.assert_called_once()
        assert "RESOLVED" in value.followup.send.call_args.kwargs["embeds"][0].title
        outcomes = [call.args[1] for call in interactions.write_interaction_audit.call_args_list]
        assert len(outcomes) == 1 and outcomes[0].status == "success"
    else:
        close.assert_not_called()
        read.assert_not_called()


def test_api_timeout_does_not_retry_or_expose_raw_error(monkeypatch):
    value = interaction()
    read = Mock(side_effect=ControlPlaneTransportError("secret-key private-host raw-body"))
    monkeypatch.setattr(resolution, "fetch_discord_resolution_preview", read)
    asyncio.run(resolution.handle_resolution_action(value, "resolve", REF))
    read.assert_called_once()
    message = value.followup.send.call_args.args[0]
    assert "secret-key" not in message and "private-host" not in message
    assert "do not assume the incident is closed" in message


def test_confirmation_expiry_does_not_claim_unknown_api_outcome_is_uncommitted(monkeypatch):
    check = check_request()
    monkeypatch.setattr(resolution, "fetch_discord_resolution_preview", Mock(return_value=preview(check)))
    monkeypatch.setattr(resolution, "fetch_discord_revalidation", Mock(return_value=check))
    close = Mock(side_effect=ControlPlaneTransportError("unknown-remote-outcome"))
    monkeypatch.setattr(resolution, "resolve_discord_incident", close)
    message = SimpleNamespace(edit=AsyncMock())
    async def run():
        view = resolution.ResolutionConfirmationView(origin(), 4, "Keep the original reason.")
        view.confirmation_message = message
        await view.confirm.callback(interaction())
        await view.on_timeout()
        assert view._expired and not view._decided
    asyncio.run(run())
    close.assert_called_once()
    text = message.edit.call_args.kwargs["content"]
    assert "review the latest status" in text
    assert "No closure decision" not in text
    assert "unknown-remote-outcome" not in text


def test_cancel_does_not_erase_public_ops_history():
    value = interaction("resolve_cancel")
    asyncio.run(resolution.handle_resolution_action(value, "resolve_cancel", REF))
    assert value.response.events[0][0] == "defer"
    assert value.response.events[0][1]["ephemeral"] is True
    assert value.followup.send.call_args.kwargs["ephemeral"] is True
    value.edit_original_response.assert_not_called()


@pytest.mark.parametrize("action,suffix", [
    ("resolve", ""), ("resolve_check", "nonce123"), ("resolve_status", REQUEST_ID),
    ("resolve_evidence", REQUEST_ID), ("resolve_cancel", ""),
])
def test_resolution_actions_load_indonesian_preference_after_ack(monkeypatch, action, suffix):
    value = interaction(action, suffix)
    check = check_request()
    def language(guild_id, user_id):
        assert value.response.is_done()
        assert (guild_id, user_id) == (10, 200)
        return "id"
    monkeypatch.setattr(resolution, "get_user_language", Mock(side_effect=language))
    monkeypatch.setattr(resolution, "fetch_discord_resolution_preview", Mock(return_value=preview(check)))
    monkeypatch.setattr(resolution, "fetch_discord_revalidation", Mock(return_value=check))
    monkeypatch.setattr(resolution, "create_discord_revalidation", Mock(return_value=check))
    asyncio.run(resolution.handle_resolution_action(value, action, REF, suffix))
    resolution.get_user_language.assert_called_once_with(10, 200)
    sent = value.followup.send.call_args
    if action == "resolve_cancel":
        assert "Tidak ada perubahan incident" in sent.args[0]
    else:
        title = sent.kwargs["embeds"][0].title
        assert "Pemeriksaan" in title or "pemeriksaan" in title
        assert "System check" not in sent.kwargs["embeds"][0].footer.text
    assert sent.kwargs["ephemeral"] is True
    assert current_language() == "en"


def test_reason_modal_opens_with_indonesian_preference_before_ack(monkeypatch):
    value = interaction("resolve_close", REQUEST_ID)
    def language(guild_id, user_id):
        assert not value.response.is_done()
        return "id"
    monkeypatch.setattr(resolution, "get_user_language", Mock(side_effect=language))
    read = Mock(side_effect=AssertionError("No backend read before opening a modal"))
    monkeypatch.setattr(resolution, "fetch_discord_resolution_preview", read)
    asyncio.run(resolution.handle_resolution_action(value, "resolve_close", REF, REQUEST_ID))
    assert value.response.modal.title == "Alasan penutupan incident"
    assert value.response.modal.reason.placeholder.startswith("Contoh:")
    read.assert_not_called()
    assert current_language() == "en"


@pytest.mark.parametrize("boundary", ["reason", "confirm", "cancel"])
def test_modal_and_confirmation_callbacks_refresh_preference_after_ack(monkeypatch, boundary):
    check = check_request()
    monkeypatch.setattr(resolution, "fetch_discord_resolution_preview", Mock(return_value=preview(check)))
    monkeypatch.setattr(resolution, "fetch_discord_revalidation", Mock(return_value=check))
    close = Mock(return_value={"alert_ref": REF, "status": "resolved"})
    monkeypatch.setattr(resolution, "resolve_discord_incident", close)
    value = interaction()
    def language(guild_id, user_id):
        assert value.response.is_done()
        return "id"
    monkeypatch.setattr(resolution, "get_user_language", Mock(side_effect=language))
    async def run():
        if boundary == "reason":
            modal = resolution.ResolutionReasonModal(origin())
            modal.reason._value = "Check passed. Keep my original reason."
            value.message = None
            await modal.on_submit(value)
        else:
            view = resolution.ResolutionConfirmationView(origin(), 4, "Keep my original reason.")
            await getattr(view, boundary).callback(value)
    asyncio.run(run())
    resolution.get_user_language.assert_called_once_with(10, 200)
    if boundary == "reason":
        sent = value.followup.send.call_args.kwargs
        assert sent["embed"].title == "Konfirmasi penutupan incident"
        assert sent["view"].confirm.label == "Konfirmasi Penutupan"
        assert sent["view"].reason == "Check passed. Keep my original reason."
        close.assert_not_called()
    elif boundary == "confirm":
        assert "DITUTUP" in value.followup.send.call_args.kwargs["embeds"][0].title
        assert close.call_args.args[-1] == "Keep my original reason."
    else:
        assert "Konfirmasi dibatalkan" in value.edit_original_response.call_args.kwargs["content"]
        close.assert_not_called()
    assert current_language() == "en"


@pytest.mark.parametrize("boundary", ["reason", "confirm", "cancel"])
def test_indonesian_callback_still_rejects_foreign_owner(monkeypatch, boundary):
    value = interaction()
    value.user.id = 999
    monkeypatch.setattr(resolution, "get_user_language", Mock(return_value="id"))
    close, read = Mock(), Mock()
    monkeypatch.setattr(resolution, "resolve_discord_incident", close)
    monkeypatch.setattr(resolution, "fetch_discord_resolution_preview", read)
    async def run():
        if boundary == "reason":
            modal = resolution.ResolutionReasonModal(origin())
            modal.reason._value = "An unchanged operator reason."
            value.message = None
            await modal.on_submit(value)
        else:
            view = resolution.ResolutionConfirmationView(origin(), 4, "An unchanged operator reason.")
            await getattr(view, boundary).callback(value)
    asyncio.run(run())
    read.assert_not_called()
    close.assert_not_called()
    assert value.followup.send.call_args.args[0].startswith("Anda tidak dapat menggunakan kontrol ini.")


def test_indonesian_action_error_uses_preference_not_ambient_context(monkeypatch):
    value = interaction("resolve_status", REQUEST_ID)
    monkeypatch.setattr(resolution, "get_user_language", Mock(return_value="id"))
    monkeypatch.setattr(resolution, "fetch_discord_resolution_preview", Mock(return_value=preview()))
    monkeypatch.setattr(resolution, "fetch_discord_revalidation", Mock(return_value=check_request(alert_ref="DQ-20261008-72DF3B")))
    asyncio.run(resolution.handle_resolution_action(value, "resolve_status", REF, REQUEST_ID))
    assert "tidak cocok" in value.followup.send.call_args.args[0]
    assert current_language() == "en"


@pytest.mark.parametrize("failure", [RuntimeError("private-store-unavailable"), "unsupported"])
def test_preference_failure_or_invalid_locale_falls_back_to_english(monkeypatch, failure):
    value = interaction("resolve_close", REQUEST_ID)
    lookup = Mock(side_effect=failure) if isinstance(failure, Exception) else Mock(return_value=failure)
    monkeypatch.setattr(resolution, "get_user_language", lookup)
    asyncio.run(resolution.handle_resolution_action(value, "resolve_close", REF, REQUEST_ID))
    assert value.response.modal.title == "Incident closure reason"
    assert current_language() == "en"


def test_modal_preference_timeout_acknowledges_without_waiting_for_store(monkeypatch):
    assert resolution._LANGUAGE_LOOKUP_TIMEOUT <= 1.0
    monkeypatch.setattr(resolution, "_LANGUAGE_LOOKUP_TIMEOUT", 0.01)
    async def bounded_thread(callback, *args):
        if callback is resolution.get_user_language:
            await asyncio.sleep(60)
        return callback(*args)
    monkeypatch.setattr(resolution.asyncio, "to_thread", bounded_thread)
    value = interaction("resolve_close", REQUEST_ID)
    async def run():
        await asyncio.wait_for(resolution.handle_resolution_action(value, "resolve_close", REF, REQUEST_ID), timeout=0.25)
    asyncio.run(run())
    assert value.response.modal.title == "Incident closure reason"
    assert current_language() == "en"


def test_status_request_from_other_incident_is_rejected(monkeypatch):
    value = interaction("resolve_status", REQUEST_ID)
    monkeypatch.setattr(resolution, "fetch_discord_resolution_preview", Mock(return_value=preview(check_request())))
    monkeypatch.setattr(resolution, "fetch_discord_revalidation", Mock(return_value=check_request(alert_ref="DQ-20261008-72DF3B")))
    asyncio.run(resolution.handle_resolution_action(value, "resolve_status", REF, REQUEST_ID))
    assert "does not match" in value.followup.send.call_args.args[0]


def api_client(monkeypatch, payload):
    client = Mock()
    client.request_json.return_value = payload
    client.approval_headers.return_value = {"X-Control-Plane-Token": "test-token"}
    monkeypatch.setattr(service, "build_control_plane_client", Mock(return_value=client))
    return client


def test_service_preview_uses_only_shared_api(monkeypatch):
    client = api_client(monkeypatch, preview(check_request()))
    assert service.fetch_discord_resolution_preview(REF)["alert_ref"] == REF
    client.request_json.assert_called_once_with("GET", f"/api/v1/alerts/{REF}/resolution-preview")
    client.approval_headers.assert_not_called()


def test_revalidation_body_never_contains_client_scope_or_pass(monkeypatch):
    client = api_client(monkeypatch, check_request("queued"))
    service.create_discord_revalidation(REF, "discord:200:Operator", "one-key")
    assert client.request_json.call_args.args == ("POST", f"/api/v1/alerts/{REF}/revalidations")
    assert client.request_json.call_args.kwargs == {
        "headers": {"X-Control-Plane-Token": "test-token"},
        "json_body": {"actor": "discord:200:Operator", "idempotency_key": "one-key"},
    }


def test_resolve_body_requires_token_and_never_sends_derived_facts(monkeypatch):
    client = api_client(monkeypatch, {"alert_ref": REF, "status": "resolved"})
    service.resolve_discord_incident(REF, REQUEST_ID, 4, "key-one", "discord:200:Operator", "Fresh check passed.")
    body = client.request_json.call_args.kwargs["json_body"]
    assert set(body) == {"resolution_request_id", "expected_incident_revision", "idempotency_key", "actor", "reason"}
    assert body["expected_incident_revision"] == 4
    client.approval_headers.assert_called_once()


@pytest.mark.parametrize("operation,args", [
    ("fetch_discord_resolution_preview", (REF,)),
    ("fetch_discord_revalidation", (REQUEST_ID,)),
    ("create_discord_revalidation", (REF, "Operator", "key")),
    ("resolve_discord_incident", (REF, REQUEST_ID, 4, "key", "Operator", "Check passed.")),
])
def test_unconfigured_api_never_falls_back_to_local_writes(monkeypatch, operation, args):
    monkeypatch.setattr(service, "build_control_plane_client", Mock(return_value=None))
    monkeypatch.setattr(service, "load_alert", Mock(side_effect=AssertionError("No local fallback")))
    with pytest.raises(RuntimeError, match="Pemeriksaan belum tersedia"):
        getattr(service, operation)(*args)


@pytest.mark.parametrize("change", [{"alert_ref": "DQ-20261008-72DF3B"}, {"incident_revision": True}, {"status": "PASS"}, {"result": []}])
def test_service_rejects_malformed_request_response(monkeypatch, change):
    api_client(monkeypatch, check_request(**change))
    with pytest.raises(ControlPlaneResponseError):
        service.create_discord_revalidation(REF, "Operator", "key")


@pytest.mark.parametrize("identifier", ["../other", "orders|dq_failure|2026-10-05", "DQ-../../", "", REF + "/resolve"])
def test_request_identifiers_cannot_change_api_path(monkeypatch, identifier):
    client = api_client(monkeypatch, {})
    with pytest.raises(ValueError):
        service.fetch_discord_resolution_preview(identifier)
    client.request_json.assert_not_called()
