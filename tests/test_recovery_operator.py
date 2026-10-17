"""Shared API adapter and human-operator UI safety boundaries."""
from __future__ import annotations

import asyncio
import ast
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4
from unittest.mock import Mock

import pytest

from agent.recovery.contracts import RECOVERY_DAG_ID
from apps.common.recovery import RecoveryClient, can_decide, can_execute, review_token
from apps.discord_bot import recovery as discord_recovery
from apps.streamlit import recovery as streamlit_recovery


PLAN_ID = str(uuid4())
EXECUTION_ID = str(uuid4())
ALERT_REF = "DQ-20261014-ABC123"


@pytest.fixture(autouse=True)
def isolated_recovery_audit(monkeypatch):
    """Unit interactions must never append synthetic actor events to live audit."""
    recorder = Mock()
    monkeypatch.setattr(discord_recovery.interactions, "write_interaction_audit", recorder)
    return recorder


def plan_payload():
    return {"plan_id": PLAN_ID, "plan_hash": "a" * 64, "policy_version": "orders-recovery-v1",
            "runbook": "rebuild_derived_partition", "runbook_version": 1,
            "alert_ref": ALERT_REF, "alert_key": "orders|dq_failure|2026-10-14",
            "incident_revision": 2, "incident_episode": 1, "dt": "2026-10-14",
            "scope": {"dt": "2026-10-14"}, "targets": ["dq.stg_orders", "dq.fct_orders_daily"],
            "source_manifest": {"key": "snapshots/2026-10-14"}, "contract_hash": "b" * 64,
            "model_hash": "c" * 64, "pipeline_model_hash": "d" * 64,
            "publication_revision": 3, "publication_watermark": "wm-3", "input_fingerprint": "e" * 64,
            "partition_fingerprint": "f" * 64, "facts_hash": "1" * 64,
            "verification_scope": {"dt": "2026-10-14"}, "validation": ["quality checks"],
            "rollback": ["retain backup"], "reason": "Restore the approved source partition.",
            "created_at": "2026-10-15T08:00:00Z", "approval_request_id": "APR-ABC123"}


def bundle(status="pending", *, execution=None, requested_by="datasentry_recovery"):
    return {"plan": plan_payload(),
            "approval": {"request_id": "APR-ABC123", "status": status, "version": 4,
                         "requested_by": requested_by, "execution_status": "not_started"},
            "execution": execution}


def execution_payload():
    return {"execution_id": EXECUTION_ID, "plan_id": PLAN_ID, "plan_hash": "a" * 64,
            "approval_request_id": "APR-ABC123", "alert_ref": ALERT_REF, "dag_id": RECOVERY_DAG_ID,
            "dag_run_id": f"manual__incident_recovery_{EXECUTION_ID}", "actor": "operator:mario",
            "status": "queued", "trigger_state": "accepted", "validation_status": "not_checked",
            "created_at": "2026-10-15T08:00:00Z", "finished_at": None, "validation": None,
            "error_code": "", "synchronization_pending": True}


class FakeAPI:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def request_json(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return self.responses.pop(0)

    def approval_headers(self):
        return {"X-Control-Plane-Token": "test"}


def test_decision_request_carries_human_identity_and_reviewed_version_then_gets_state():
    api = FakeAPI([{}, {"plan": plan_payload(), "approval": {
        "request_id": "APR-ABC123", "status": "approved", "version": 5,
        "requested_by": "datasentry_recovery", "execution_status": "not_started"}, "execution": None}])

    RecoveryClient(api).decide(bundle(), actor="Mario", decision="approve", comment="Reviewed evidence.")

    method, path, call = api.calls[0]
    assert (method, path) == ("POST", "/api/v1/approvals/requests/APR-ABC123/decision")
    assert call["json_body"] == {"decided_by": "Mario", "decision": "approve",
                                  "comment": "Reviewed evidence.", "expected_version": 4}
    assert api.calls[1][:2] == ("GET", f"/api/v1/recovery-plans/{PLAN_ID}")


def test_execution_poll_is_get_only_and_never_dispatches_or_closes_incident():
    api = FakeAPI([execution_payload()])

    result = RecoveryClient(api).execution(EXECUTION_ID, plan=plan_payload())

    assert result["execution_id"] == EXECUTION_ID
    assert api.calls == [("GET", f"/api/v1/recovery-executions/{EXECUTION_ID}", {})]


@pytest.mark.parametrize("actor", ["datasentry_recovery", "system", "airflow", "agentic_triage"])
def test_operator_authorization_helpers_never_allow_service_or_self_approval(actor):
    current = bundle(requested_by="Mario")
    assert can_decide(current, "Different Operator")
    assert not can_decide(current, "mario")
    assert not can_decide(current, actor)


def test_execution_is_single_use_and_confirmation_token_freezes_plan_and_version():
    current = bundle("approved")
    assert can_execute(current)
    assert not can_execute(bundle("pending"))
    assert not can_execute(bundle("approved", execution=execution_payload()))
    assert not can_execute({**current, "approval": {**current["approval"], "execution_status": "unknown"}})
    assert review_token(current) == (PLAN_ID, "a" * 64, 4)


def test_streamlit_recovery_identity_survives_locale_changes_and_never_uses_locale_in_key():
    state = {}
    first = streamlit_recovery.recovery_session(state, ALERT_REF)
    first["active"] = True
    second = streamlit_recovery.recovery_session(state, ALERT_REF)

    assert second is first
    assert list(state) == ["recovery:" + ALERT_REF]
    assert "locale" not in state


def test_closed_streamlit_panel_does_not_call_recovery_api(monkeypatch):
    state = {}

    class FakeStreamlit:
        session_state = state

        @staticmethod
        def button(*_args, **_kwargs):
            return False

    class API:
        def __init__(self):
            self.calls = []

        def request_json(self, *args, **kwargs):
            self.calls.append((args, kwargs))

    api = API()
    monkeypatch.setattr(streamlit_recovery, "st", FakeStreamlit)
    monkeypatch.setattr(streamlit_recovery, "tr", lambda text: "id:" + text)
    streamlit_recovery.render_recovery_panel({"alert_display_id": ALERT_REF, "status": "open"}, api)

    assert api.calls == []
    assert "recovery:" + ALERT_REF in state


def test_discord_recovery_controls_use_persistent_versioned_namespace_and_are_state_gated():
    pending = discord_recovery.RecoveryView(bundle())
    approved = discord_recovery.RecoveryView(bundle("approved"))
    running = discord_recovery.RecoveryView(bundle("approved", execution=execution_payload()))

    assert pending.timeout is None and approved.timeout is None and running.timeout is None
    ids = {item.custom_id for item in pending.children}
    assert f"dqrec:v1:approve:{PLAN_ID}" in ids
    assert f"dqrec:v1:reject:{PLAN_ID}" in ids
    assert f"dqrec:v1:run:{PLAN_ID}" in {item.custom_id for item in approved.children}
    assert f"dqrec:v1:run:{PLAN_ID}" not in {item.custom_id for item in running.children}
    assert all(item.startswith("dqrec:v1:") for item in ids)


def test_discord_namespace_is_anchored_to_known_action_and_target_forms():
    assert discord_recovery.PATTERN.fullmatch(f"dqrec:v1:review:{ALERT_REF}")
    assert discord_recovery.PATTERN.fullmatch(f"dqrec:v1:track:{PLAN_ID}")
    for invalid in (f"prefix:dqrec:v1:review:{ALERT_REF}", f"dqrec:v2:review:{ALERT_REF}",
                    f"dqrec:v1:delete:{ALERT_REF}", f"dqrec:v1:run:{ALERT_REF}:extra"):
        assert discord_recovery.PATTERN.fullmatch(invalid) is None


def _discord_interaction(*, guild=1, channel=12, author=77, bot=False):
    return type("Interaction", (), {
        "guild_id": guild, "channel_id": channel,
        "user": type("User", (), {"id": 101, "bot": bot, "roles": []})(),
        "message": type("Message", (), {"author": type("Author", (), {"id": author})()})(),
        "client": type("Client", (), {"user": type("BotUser", (), {"id": 77})()})(),
        "channel": None,
    })()


def test_discord_ops_channel_requires_verified_bot_message_and_operator_access(monkeypatch):
    from apps.discord_bot import interactions
    from apps.discord_bot import resolution

    monkeypatch.setattr(interactions, "GUILD_ID", 1)
    monkeypatch.setattr(interactions, "ALERTS_CHANNEL_ID", 10)
    monkeypatch.setattr(interactions, "TRIAGE_CHANNEL_ID", 11)
    monkeypatch.setattr(interactions, "OPS_CHANNEL_ID", 12)
    access = []

    async def allow_operator(interaction):
        access.append(interaction.user.id)

    monkeypatch.setattr(resolution, "require_resolution_operator_access", allow_operator)
    interaction = _discord_interaction(channel=12)
    asyncio.run(discord_recovery.require_operator(interaction))

    assert access == [101]


@pytest.mark.parametrize("interaction,match", [
    (_discord_interaction(guild=2), "different Discord server"),
    (_discord_interaction(author=88), "DataSentry-authored"),
    (_discord_interaction(bot=True), "Bot users"),
])
def test_discord_operator_gate_rejects_wrong_guild_author_bot_and_channel(monkeypatch, interaction, match):
    from apps.discord_bot import interactions
    from apps.discord_bot import resolution

    monkeypatch.setattr(interactions, "GUILD_ID", 1)
    monkeypatch.setattr(interactions, "ALERTS_CHANNEL_ID", 10)
    monkeypatch.setattr(interactions, "TRIAGE_CHANNEL_ID", 11)
    monkeypatch.setattr(interactions, "OPS_CHANNEL_ID", 12)
    monkeypatch.setattr(resolution, "require_resolution_operator_access", lambda _interaction: None)
    with pytest.raises(PermissionError, match=match):
        asyncio.run(discord_recovery.require_operator(interaction))


def test_discord_operator_module_has_no_local_pipeline_or_provider_mutation_imports():
    tree = ast.parse(Path(discord_recovery.__file__).read_text(encoding="utf-8"))
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    assert not any(name.startswith(("pipelines.", "agent.llm", "agent.nodes", "agent.specialists"))
                   for name in imports)


def test_discord_confirmation_is_owner_only_before_any_api_action(monkeypatch):
    calls = []

    class Response:
        async def send_message(self, *args, **kwargs):
            calls.append(("send", args, kwargs))

    class Interaction:
        user = type("User", (), {"id": 202})()
        response = Response()

    async def forbidden_operator(_interaction):
        raise AssertionError("non-owner must be rejected before operator or API work")

    monkeypatch.setattr(discord_recovery, "require_operator", forbidden_operator)
    monkeypatch.setattr(discord_recovery, "recovery_client", lambda: calls.append("client") or None)
    view = discord_recovery.ConfirmRecovery(bundle("approved"), "run", "", owner=101)
    confirm_button = next(item for item in view.children if item.label == "Confirm")
    asyncio.run(confirm_button.callback(Interaction()))

    assert calls and calls[0][0] == "send"
    assert "client" not in calls
    assert "Only the operator" in calls[0][1][0]


def test_discord_confirmation_routes_only_through_recovery_api_client(monkeypatch, isolated_recovery_audit):
    calls = []

    class Response:
        async def defer(self, **_kwargs):
            pass

    class Followup:
        async def send(self, *_args, **_kwargs):
            pass

    class Interaction:
        user = type("User", (), {"id": 101})()
        response, followup = Response(), Followup()

        async def edit_original_response(self, **_kwargs):
            pass

    class Client:
        def execute(self, *_args, **kwargs):
            calls.append(("execute", kwargs))
            return execution_payload()

        def plan(self, *_args, **_kwargs):
            calls.append(("plan", {}))
            return bundle("approved")

    async def allowed(_interaction):
        return None

    monkeypatch.setattr(discord_recovery, "require_operator", allowed)
    monkeypatch.setattr(discord_recovery, "recovery_client", Client)
    view = discord_recovery.ConfirmRecovery(bundle("approved"), "run", "", owner=101)
    confirm_button = next(item for item in view.children if item.label == "Confirm")
    asyncio.run(confirm_button.callback(Interaction()))

    assert [name for name, _ in calls] == ["execute", "plan"]
    assert calls[0][1] == {"actor": "discord:101", "confirm_scope": True}
    assert not any("generator" in name or "seed" in name for name, _ in calls)
    isolated_recovery_audit.assert_called_once()
    context, outcome = isolated_recovery_audit.call_args.args
    assert context.actor == "discord:101" and context.action == "recovery_run"
    assert outcome.status == "success"
