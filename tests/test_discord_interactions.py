####
## Discord Interaction Boundary Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from apps.discord_bot import interactions
from apps.discord_bot.contracts import DiscordRenderResult
from apps.discord_bot.formatters import render_approval_embed


@pytest.fixture(autouse=True)
def mocked_preferences(monkeypatch):
    from apps.discord_bot import preferences
    monkeypatch.setattr(preferences, "get_user_language", lambda *args: "en")


# --- Defining Test Doubles
class FakeResponse:
    """Capture deferred and direct interaction responses."""

    def __init__(self) -> None:
        self.deferred: list[dict[str, Any]] = []
        self.messages: list[dict[str, Any]] = []
        self._done = False

    async def defer(self, **kwargs: Any) -> None:
        self.deferred.append(kwargs)
        self._done = True

    async def send_message(self, content: str, **kwargs: Any) -> None:
        self.messages.append({"content": content, **kwargs})
        self._done = True

    def is_done(self) -> bool:
        return self._done


class FakeFollowup:
    """Capture ephemeral follow-up messages."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def send(self, content: str, **kwargs: Any) -> None:
        self.messages.append({"content": content, **kwargs})


class FakeMessage:
    """Capture edits to an authoritative DataSentry-authored message."""

    def __init__(self, author_id: int = 100) -> None:
        self.id = 700
        self.author = SimpleNamespace(id=author_id)
        self.edits: list[dict[str, Any]] = []

    async def edit(self, **kwargs: Any) -> None:
        self.edits.append(kwargs)


def build_interaction(*, channel_id: int = 300, user_id: int = 200) -> SimpleNamespace:
    """Build a minimum Discord interaction with explicit guild and author identity."""
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id, display_name="Operator", bot=False, roles=[]),
        guild_id=10,
        channel_id=channel_id,
        message=FakeMessage(),
        client=SimpleNamespace(user=SimpleNamespace(id=100)),
        response=FakeResponse(),
        followup=FakeFollowup(),
    )


# --- Defining Scope Tests
def test_interaction_scope_fails_closed_without_configured_channels(monkeypatch) -> None:
    """An empty allowlist must never become an allow-all policy."""
    monkeypatch.setattr(interactions, "GUILD_ID", 10)

    with pytest.raises(PermissionError, match="no configured channel boundary"):
        interactions.validate_interaction_scope(build_interaction(), allowed_channels=set())


def test_interaction_scope_rejects_wrong_channel_and_foreign_author(monkeypatch) -> None:
    """Controls are bounded to their configured channel and DataSentry-authored message."""
    monkeypatch.setattr(interactions, "GUILD_ID", 10)

    with pytest.raises(PermissionError, match="not allowed"):
        interactions.validate_interaction_scope(build_interaction(channel_id=301), {300})

    interaction = build_interaction(channel_id=300)
    interaction.message.author.id = 999
    with pytest.raises(PermissionError, match="DataSentry-authored"):
        interactions.validate_interaction_scope(interaction, {300})

    interaction = build_interaction(channel_id=300)
    interaction.message.author = None
    with pytest.raises(PermissionError, match="author could not be verified"):
        interactions.validate_interaction_scope(interaction, {300})


# --- Defining View Tests
def test_public_triage_thread_accepts_read_only_controls_but_not_approval(monkeypatch) -> None:
    """Thread answers retain their controls without widening approval permissions."""
    monkeypatch.setattr(interactions, "GUILD_ID", 10)
    monkeypatch.setattr(interactions, "TRIAGE_CHANNEL_ID", 300)
    interaction = build_interaction(channel_id=301)
    interaction.channel = Mock(spec=discord.Thread)
    interaction.channel.type = discord.ChannelType.public_thread
    interaction.channel.parent_id = 300

    interactions.validate_interaction_scope(interaction, {300})

    with pytest.raises(PermissionError, match="not allowed"):
        interactions.validate_interaction_scope(interaction, {300}, require_approver=True)

    interaction.message.author.id = 999
    with pytest.raises(PermissionError, match="DataSentry-authored"):
        interactions.validate_interaction_scope(interaction, {300})


@pytest.mark.parametrize("thread_type,parent_id", [
    (discord.ChannelType.private_thread, 300),
    (discord.ChannelType.news_thread, 300),
    (discord.ChannelType.public_thread, 999),
])
def test_thread_controls_reject_private_and_foreign_parent_threads(monkeypatch, thread_type, parent_id) -> None:
    """Only configured public triage threads qualify for the inherited boundary."""
    monkeypatch.setattr(interactions, "GUILD_ID", 10)
    monkeypatch.setattr(interactions, "TRIAGE_CHANNEL_ID", 300)
    interaction = build_interaction(channel_id=301)
    interaction.channel = Mock(spec=discord.Thread)
    interaction.channel.type = thread_type
    interaction.channel.parent_id = parent_id

    with pytest.raises(PermissionError, match="not allowed"):
        interactions.validate_interaction_scope(interaction, {300})


def test_followup_render_stays_in_public_thread_and_omits_none_view(monkeypatch) -> None:
    """Do not send thread follow-ups into the channel or pass discord.py view=None."""
    monkeypatch.setattr(interactions, "TRIAGE_CHANNEL_ID", 300)
    interaction = build_interaction(channel_id=301)
    interaction.channel = Mock(spec=discord.Thread)
    interaction.channel.type = discord.ChannelType.public_thread
    interaction.channel.parent_id = 300
    interaction.channel.send = AsyncMock()
    interaction.client.get_channel = Mock()
    render = DiscordRenderResult(
        payload={"embeds": [{"title": "Answer", "description": "Evidence-grounded follow-up."}]},
        narrative_source="deterministic_fallback", content_hash="thread-answer",
        fallback_markdown="Evidence-grounded follow-up.",
    )

    asyncio.run(interactions.send_render_to_channel(interaction, 300, render))

    interaction.client.get_channel.assert_not_called()
    interaction.channel.send.assert_awaited_once()
    kwargs = interaction.channel.send.call_args.kwargs
    assert "view" not in kwargs
    assert kwargs["allowed_mentions"].to_dict() == {"parse": []}
    assert kwargs["embeds"][0].title == "Answer"


def test_approval_view_has_decision_controls_but_no_execution_action() -> None:
    """Approval UI records a decision and never exposes direct remediation execution."""
    render = render_approval_embed(
        request_id="APR-20261007-ABC123",
        status="pending",
        summary="Review a bounded backfill proposal. No execution has occurred.",
        next_step="Review the exact scope, then approve or reject.",
        target_dag_id="00_dag_dq_platform_daily_orchestrator",
        start_date="2026-10-07",
        end_date="2026-10-07",
    )
    view = interactions.build_discord_view(render)

    assert view is not None
    labels = {
        str(getattr(item, "label", None) or getattr(item, "item").label)
        for item in view.children
    }
    assert labels == {"View Plan", "Approve", "Reject"}
    assert "Execute" not in labels
    assert "Run" not in labels
    assert "Dispatch" not in labels


def test_persistent_dynamic_handlers_are_registered() -> None:
    """Bot startup registers versioned handlers that survive reconnects."""
    captured: list[type[Any]] = []
    client = SimpleNamespace(add_dynamic_items=lambda *items: captured.extend(items))

    interactions.register_persistent_interactions(client)

    assert captured == [
        interactions.IncidentActionButton,
        interactions.ApprovalActionButton,
        __import__("apps.discord_bot.help", fromlist=["HelpActionButton"]).HelpActionButton,
        __import__("apps.discord_bot.recovery", fromlist=["RecoveryButton"]).RecoveryButton,
    ]


def test_ephemeral_render_omits_none_view_for_read_only_result() -> None:
    """discord.py rejects an explicit view=None on follow-up responses."""
    interaction = build_interaction()
    render = DiscordRenderResult(
        payload={"embeds": [{"title": "Evidence", "description": "Read-only facts."}]},
        narrative_source="deterministic_fallback",
        content_hash="evidence-hash",
        fallback_markdown="Evidence: read-only facts.",
    )

    asyncio.run(interactions.respond_with_render(interaction, render, ephemeral=True))

    assert len(interaction.followup.messages) == 1
    message = interaction.followup.messages[0]
    assert message["ephemeral"] is True
    assert "view" not in message
    assert message["embeds"][0].title == "Evidence"


@pytest.mark.parametrize("duplicate,foreign_user", [(False, False), (True, False), (False, True)])
def test_thread_ask_modal_preserves_dialogue_and_rejects_duplicate_or_foreign_submission(monkeypatch, duplicate, foreign_user) -> None:
    from apps.discord_bot.mentions import MentionAskHandler
    from apps.discord_bot.mention_store import MentionRequestState
    monkeypatch.setattr(interactions, "GUILD_ID", 10)
    monkeypatch.setattr(interactions, "TRIAGE_CHANNEL_ID", 300)
    monkeypatch.setattr(interactions, "ALERTS_CHANNEL_ID", 400)
    interaction = build_interaction(channel_id=301)
    interaction.id = 801
    interaction.channel = Mock(spec=discord.Thread)
    interaction.channel.id = 301
    interaction.channel.type = discord.ChannelType.public_thread
    interaction.channel.parent_id = 300
    interaction.message.channel = interaction.channel
    history = MentionRequestState("700", "700", "10", "300", "200", thread_id="301",
                                  answer_message_id="701", alert_ref="DQ-20261005-3B55A6", status="answered")
    store = Mock()
    store.history.return_value = [history]
    store.find_thread.return_value = history
    store.claim.return_value = not duplicate
    handler = MentionAskHandler(interaction.client, 10, 300, store=store, clock=lambda: 100)
    interaction.channel.locked = False
    interaction.client.incident_workspace_runtime = SimpleNamespace(
        guild_id=10, triage_channel_id=300, bind_thread=AsyncMock(return_value=None),
    )
    dialogue = [{"question": "What failed?", "answer": "Segment coverage is incomplete."}]
    handler.conversation = AsyncMock(return_value=dialogue)
    modal = interactions.AskIncidentModal("DQ-20261005-3B55A6", interaction.message, 200)
    modal.question._value = "Which evidence is still missing?"
    if foreign_user:
        interaction.user.id = 999
    answer = Mock(return_value={
        "answer": "The missing segment identities are not yet evidenced.", "alert_key": "DQ-20261005-3B55A6",
        "transport": "api", "agent_run_id": "run-1",
        "llm_runtime": {"provider": "heuristic", "model": "heuristic-v1", "used_heuristic": True},
    })
    send = AsyncMock(return_value=SimpleNamespace(id=802))
    monkeypatch.setattr(interactions, "answer_discord_question", answer)
    monkeypatch.setattr(interactions, "send_render_to_thread", send)
    monkeypatch.setattr(interactions, "write_interaction_audit", Mock())

    asyncio.run(modal.on_submit(interaction))

    assert handler.active_count == 0
    if duplicate or foreign_user:
        answer.assert_not_called()
        send.assert_not_awaited()
    else:
        assert answer.call_args.args[0] == "Which evidence is still missing?"
        assert answer.call_args.kwargs["conversation_context"] == dialogue
        assert answer.call_args.kwargs["mention_mode"] is True
        render = send.call_args.args[1]
        assert render.payload["embeds"][0]["fields"][-1]["name"] == "Follow-up question"
        saved = store.save.call_args.args[0]
        assert saved.source_kind == "modal" and saved.source_message_id == "801"
        assert saved.thread_id == "301" and saved.status == "answered"
        assert "https://discord.com/channels/10/301" in interaction.followup.messages[-1]["content"]


# --- Defining Approval Decision Tests
def test_approval_decision_updates_state_and_incident_without_execution(monkeypatch) -> None:
    """Approve records durable state and refreshes the linked incident without dispatching work."""
    interaction = build_interaction(channel_id=300)
    origin = interaction.message
    calls: dict[str, list[Any]] = {"decision": [], "sync": [], "audit": []}

    monkeypatch.setattr(interactions, "GUILD_ID", 10)
    monkeypatch.setattr(interactions, "OPS_CHANNEL_ID", 300)
    monkeypatch.setattr(interactions, "configured_approver_role_ids", lambda: set())
    monkeypatch.setattr(
        interactions,
        "decide_discord_approval_request",
        lambda request_id, decision, decided_by, comment: calls["decision"].append(
            (request_id, decision, decided_by, comment)
        )
        or {
            "request_id": request_id,
            "status": "approved",
            "risk_level": "high",
            "decided_by": decided_by,
            "target_dag_id": "00_dag_dq_platform_daily_orchestrator",
            "start_date": "2026-10-07",
            "end_date": "2026-10-07",
            "alert_key": "orders|dq_failure|2026-10-07|dq.raw_orders|volume|row_count",
            "parameters": {},
        },
    )
    monkeypatch.setattr(
        interactions,
        "synchronize_incident_message",
        lambda *args: calls["sync"].append(args),
    )
    monkeypatch.setattr(
        interactions,
        "write_interaction_audit",
        lambda *args: calls["audit"].append(args),
    )

    asyncio.run(
        interactions.perform_approval_decision(
            interaction=interaction,
            request_id="APR-20261007-ABC123",
            decision="approve",
            comment="Confirmed through the DataSentry approval control.",
            origin_message=origin,
        )
    )

    assert len(calls["decision"]) == 1
    assert len(calls["sync"]) == 1
    assert calls["sync"][0][0].startswith("orders|dq_failure|")
    assert calls["sync"][0][-1] == "approved"
    assert len(calls["audit"]) == 1
    assert len(origin.edits) == 1
    assert len(origin.edits[0]["embeds"]) == 1
    edited_embed = origin.edits[0]["embeds"][0]
    edited_fields = {field.name: field.value for field in edited_embed.fields}
    edited_labels = {
        str(getattr(item, "label", None) or getattr(item, "item").label)
        for item in origin.edits[0]["view"].children
    }
    assert edited_fields["Risk"] == "HIGH"
    assert "No automatic rollback is implemented" in edited_fields["Recovery boundary"]
    assert "Execute" not in edited_labels
    assert "Run" not in edited_labels
    assert "Dispatch" not in edited_labels
    assert interaction.followup.messages[0]["ephemeral"] is True
    assert "No Airflow DAG was triggered" in interaction.followup.messages[0]["content"]


def test_approval_decision_rejects_unconfigured_ops_channel(monkeypatch) -> None:
    """Missing ops-channel configuration rejects a decision before backend mutation."""
    interaction = build_interaction(channel_id=300)
    decision_calls: list[str] = []

    monkeypatch.setattr(interactions, "GUILD_ID", 10)
    monkeypatch.setattr(interactions, "OPS_CHANNEL_ID", 0)
    monkeypatch.setattr(
        interactions,
        "decide_discord_approval_request",
        lambda *args: decision_calls.append("unexpected"),
    )
    monkeypatch.setattr(interactions, "write_interaction_audit", lambda *args: None)

    asyncio.run(
        interactions.perform_approval_decision(
            interaction=interaction,
            request_id="APR-20261007-ABC123",
            decision="approve",
            comment="Should not persist.",
            origin_message=interaction.message,
        )
    )

    assert decision_calls == []
    assert interaction.response.deferred[0]["ephemeral"] is True
    assert interaction.followup.messages[0]["ephemeral"] is True
    assert "no configured channel boundary" in interaction.followup.messages[0]["content"]


def workspace_interaction(monkeypatch):
    """Provide mocked authority/storage/Discord with a canonical public thread."""
    from apps.discord_bot.workspace import IncidentWorkspace
    monkeypatch.setattr(interactions, "GUILD_ID", 10)
    monkeypatch.setattr(interactions, "TRIAGE_CHANNEL_ID", 300)
    monkeypatch.setattr(interactions, "ALERTS_CHANNEL_ID", 400)
    interaction = build_interaction(channel_id=400)
    interaction.id = 801
    interaction.channel = SimpleNamespace(id=400, type=discord.ChannelType.text)
    interaction.message.channel = interaction.channel
    thread = Mock(spec=discord.Thread)
    thread.id = 301
    thread.type = discord.ChannelType.public_thread
    thread.parent_id = 300
    thread.guild = SimpleNamespace(id=10)
    thread.locked = thread.archived = False
    thread.send = AsyncMock(return_value=SimpleNamespace(id=802))
    state = SimpleNamespace(guild_id="10", alert_key="system-key", alert_ref="DQ-20261005-3B55A6",
                            starter_message_id="301", thread_id="301")
    incident = IncidentWorkspace(state, thread)
    report = SimpleNamespace(alert=SimpleNamespace(alert_key="system-key"), ai_investigation=None,
                             summary="Stored deterministic investigation.", llm_runtime=Mock())
    report.llm_runtime.model_dump.return_value = {"used_heuristic": True}
    runtime = SimpleNamespace(guild_id=10, triage_channel_id=300, bind_thread=AsyncMock(return_value=None),
                              ensure=AsyncMock(return_value=incident), investigate=AsyncMock(return_value=(report, "stored_report")))
    interaction.client.incident_workspace_runtime = runtime
    monkeypatch.setattr(interactions, "write_interaction_audit", Mock())
    monkeypatch.setattr(interactions, "synchronize_incident_message", Mock())
    render = DiscordRenderResult(payload={"embeds": [{"title": "Investigation", "description": "Stored report."}]},
                                 narrative_source="deterministic_fallback", content_hash="hash", fallback_markdown="Stored report.")
    monkeypatch.setattr(interactions, "render_triage_embed", Mock(return_value=render))
    return SimpleNamespace(**locals())


def test_parent_workspace_export_returns_thread_and_acknowledges_ephemerally(monkeypatch):
    env = workspace_interaction(monkeypatch)
    returned = asyncio.run(interactions.get_incident_workspace(env.interaction, env.state.alert_ref))
    assert returned is env.thread
    assert env.interaction.response.deferred == [{"thinking": True, "ephemeral": True}]
    assert env.incident.url in env.interaction.followup.messages[-1]["content"]
    assert env.interaction.followup.messages[-1]["ephemeral"]
    env.thread.send.assert_not_awaited()


@pytest.mark.parametrize("rerun", [False, True])
def test_parent_investigation_export_posts_shared_render_in_thread_without_narrative_model(monkeypatch, rerun):
    env = workspace_interaction(monkeypatch)
    paid_note = Mock(side_effect=AssertionError("must never generate a second narrative"))
    monkeypatch.setattr(interactions, "build_discord_triage_note", paid_note)
    returned = asyncio.run(interactions.run_workspace_investigation(env.interaction, env.state.alert_ref, rerun=rerun))
    assert returned is env.report
    env.runtime.investigate.assert_awaited_once_with(env.incident, "801", reinvestigate=rerun)
    paid_note.assert_not_called()
    env.thread.send.assert_awaited_once()
    assert env.thread.send.call_args.kwargs["allowed_mentions"].to_dict() == {"parse": []}
    assert env.incident.url in env.interaction.followup.messages[-1]["content"]
    assert env.interaction.client.mention_ask_handler.active_count == 0


def test_locale_lookup_is_after_ack_off_event_loop_and_context_reaches_model_and_render(monkeypatch):
    import threading
    from apps.discord_bot import preferences
    from apps.common.i18n import current_language
    env = workspace_interaction(monkeypatch)
    loop_thread = threading.get_ident()
    def language(*args):
        assert env.interaction.response.is_done()
        assert threading.get_ident() != loop_thread
        return "id"
    async def investigate(*args, **kwargs):
        assert current_language() == "id"
        assert await asyncio.to_thread(current_language) == "id"
        return env.report, "stored_report"
    def render(**kwargs):
        assert current_language() == "id"
        return env.render
    monkeypatch.setattr(preferences, "get_user_language", language)
    env.runtime.investigate.side_effect = investigate
    monkeypatch.setattr(interactions, "render_triage_embed", render)
    asyncio.run(interactions.run_workspace_investigation(env.interaction, env.state.alert_ref))
    assert current_language() == "en"


@pytest.mark.parametrize("action", ["investigate", "reinvestigate"])
def test_persistent_investigation_buttons_use_same_parent_adapter(monkeypatch, action):
    env = workspace_interaction(monkeypatch)
    asyncio.run(interactions.IncidentActionButton(action, env.state.alert_ref).callback(env.interaction))
    env.runtime.investigate.assert_awaited_once_with(env.incident, "801", reinvestigate=action == "reinvestigate")
    env.thread.send.assert_awaited_once()


@pytest.mark.parametrize("action", ["evidence", "report"])
def test_evidence_and_report_actions_post_in_thread_with_ephemeral_link_not_channel_blast(monkeypatch, action):
    env = workspace_interaction(monkeypatch)
    monkeypatch.setattr(interactions, "fetch_discord_evidence_context", Mock(return_value={"alert_key": "system-key"}))
    monkeypatch.setattr(interactions, "fetch_discord_debug_context", Mock(return_value={"alert_key": "system-key"}))
    monkeypatch.setattr(interactions, "render_evidence_embed", Mock(return_value=env.render))
    monkeypatch.setattr(interactions, "render_report_reference_embed", Mock(return_value=env.render))
    artifacts = Mock(return_value=[{"filename": "report.md", "content": b"safe report", "media_type": "text/markdown"}])
    monkeypatch.setattr(interactions.workspace_module, "fetch_discord_report_files", artifacts)
    blast = AsyncMock(side_effect=AssertionError("no public channel blast"))
    monkeypatch.setattr(interactions, "send_render_to_channel", blast)
    asyncio.run(interactions.IncidentActionButton(action, env.state.alert_ref).callback(env.interaction))
    blast.assert_not_awaited()
    env.thread.send.assert_awaited_once()
    assert env.incident.url in env.interaction.followup.messages[-1]["content"]
    assert env.interaction.followup.messages[-1]["ephemeral"]
    assert ("files" in env.thread.send.call_args.kwargs) is (action == "report")


def test_reinvestigation_controls_deduplicate_and_are_absent_from_resolved_reports(monkeypatch):
    from dataclasses import replace
    env = workspace_interaction(monkeypatch)
    first = interactions.with_reinvestigate_control(env.render, env.state.alert_ref)
    second = interactions.with_reinvestigate_control(first, env.state.alert_ref)
    assert first is second
    assert first.action_rows[0].actions[0].label == "Run New Investigation"
    resolved = replace(env.render, lifecycle="RESOLVED")
    assert interactions.with_reinvestigate_control(resolved, env.state.alert_ref) is resolved
    assert not resolved.action_rows


@pytest.mark.parametrize("action,label,request_id", [
    ("resolve", "Resolve", ""), ("resolve_check", "Check Again", "nonce-1"),
    ("resolve_status", "Check Status", "request-1"), ("resolve_close", "Close Incident", "request-1"),
    ("resolve_evidence", "View Evidence", "request-1"), ("resolve_cancel", "Cancel", ""),
])
def test_persistent_default_resolution_labels_are_english_and_entry_style_is_secondary(action, label, request_id):
    button = interactions.IncidentActionButton(action, "DQ-20261005-3B55A6", request_id)
    assert button.item.label == label
    if action == "resolve":
        assert button.item.style == discord.ButtonStyle.secondary


def test_ask_button_acknowledges_before_preference_lookup_and_opens_personalized_modal(monkeypatch):
    from apps.discord_bot import preferences
    from apps.common.locales import discord as catalog
    env = workspace_interaction(monkeypatch)
    def language(*args):
        assert env.interaction.response.is_done()
        return "id"
    monkeypatch.setattr(preferences, "get_user_language", language)
    monkeypatch.setitem(catalog.MESSAGES, "Ask DataSentry", "Tanya DataSentry")
    monkeypatch.setitem(catalog.MESSAGES, "Question about this incident", "Pertanyaan tentang incident ini")
    asyncio.run(interactions.IncidentActionButton("ask", env.state.alert_ref).callback(env.interaction))
    message = env.interaction.followup.messages[-1]
    assert message["ephemeral"] and env.incident.url in message["content"]
    view = message["view"]
    assert view.modal.title == "Tanya DataSentry"
    assert view.modal.question.label == "Pertanyaan tentang incident ini"
    foreign = build_interaction(user_id=999)
    foreign.response.send_modal = AsyncMock()
    asyncio.run(view.open(foreign))
    foreign.response.send_modal.assert_not_awaited()
    own = build_interaction(user_id=200)
    own.response.send_modal = AsyncMock()
    asyncio.run(view.open(own))
    own.response.send_modal.assert_awaited_once_with(view.modal)


def test_canonical_ask_modal_needs_no_prior_mentions_and_queries_same_actor_incident(monkeypatch):
    from apps.discord_bot.mentions import MentionAskHandler
    env = workspace_interaction(monkeypatch)
    env.interaction.channel = env.thread
    env.interaction.channel_id = 301
    env.interaction.message.channel = env.thread
    env.runtime.bind_thread.return_value = env.state
    store = Mock()
    store.claim.return_value = True
    store.history.return_value = []
    handler = MentionAskHandler(env.interaction.client, 10, 300, store=store, workspace=env.runtime)
    answer = Mock(return_value={"answer": "A partition is missing.", "alert_ref": env.state.alert_ref,
                               "alert_key": "system-key", "agent_run_id": "run-1"})
    monkeypatch.setattr(interactions, "answer_discord_question", answer)
    modal = interactions.AskIncidentModal(env.state.alert_ref, env.interaction.message, 200)
    modal.question._value = "What evidence is missing?"
    asyncio.run(modal.on_submit(env.interaction))
    answer.assert_called_once()
    store.history.assert_called_once_with("301", "200", env.state.alert_ref, "10", "300")
    assert store.save.call_args.args[0].status == "answered"
    assert handler.active_count == 0
    env.thread.send.assert_awaited_once()


def test_modal_timeout_retains_shared_admission_until_background_call_finishes(monkeypatch):
    import time
    from apps.discord_bot.mentions import MentionAskHandler
    env = workspace_interaction(monkeypatch)
    store = Mock()
    store.claim.return_value = True
    store.history.return_value = []
    handler = MentionAskHandler(env.interaction.client, 10, 300, store=store, workspace=env.runtime)
    handler.request_timeout_seconds = .002
    def slow(*args, **kwargs):
        time.sleep(.06)
        return {"answer": "Stored answer", "alert_ref": env.state.alert_ref}
    monkeypatch.setattr(interactions, "answer_discord_question", slow)
    modal = interactions.AskIncidentModal(env.state.alert_ref, env.interaction.message, 200)
    modal.question._value = "What happened?"
    async def run():
        await modal.on_submit(env.interaction)
        assert handler.active_count == 1
        assert store.save.call_args.args[0].status == "failed"
        assert not handler.admit(200, 301)
        await asyncio.sleep(.1)
        assert handler.active_count == 0
    asyncio.run(run())
    env.thread.send.assert_not_awaited()
