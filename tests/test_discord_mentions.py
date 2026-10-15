"""Mention admission, thread delivery, replay safety, and bounded dialogue tests."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, create_autospec

import discord
import pytest
from pydantic import ValidationError

from agent.llm.conversation import bounded_conversation_context
from apps.api.schemas import CopilotAnswerRequest
from apps.discord_bot.mentions import MentionAskHandler, PROCESSING, ANSWERED, FAILED, thread_title
from apps.discord_bot.mention_store import MentionRequestState, MentionRequestStore


@pytest.fixture(autouse=True)
def mocked_preferences(monkeypatch):
    """Keep every boundary test independent from live preference/control storage."""
    from apps.discord_bot import preferences
    monkeypatch.setattr(preferences, "get_user_language", lambda *args: "en")


class MemoryStore:
    def __init__(self):
        self.rows = {}

    def claim(self, state):
        if state.source_message_id in self.rows:
            return False
        self.rows[state.source_message_id] = state
        return True

    def save(self, state):
        self.rows[state.source_message_id] = state

    def history(self, thread_id, actor_id, alert_ref, guild_id, channel_id):
        return [row for row in reversed(list(self.rows.values()))
                if row.thread_id == thread_id and row.status == "answered"
                and row.actor_id == actor_id and row.alert_ref == alert_ref
                and row.guild_id == guild_id and row.channel_id == channel_id][:2]

    def find_thread(self, thread_id):
        return next((row for row in self.rows.values()
                     if row.thread_id == thread_id and row.status == "answered"), None)


def environment():
    client = SimpleNamespace(user=SimpleNamespace(id=99, bot=True))
    permissions = SimpleNamespace(**{key: True for key in (
        "view_channel", "read_message_history", "embed_links", "add_reactions",
        "send_messages_in_threads", "send_messages", "create_public_threads")})
    channel = SimpleNamespace(id=20, type=discord.ChannelType.text,
                              permissions_for=Mock(return_value=permissions), fetch_message=AsyncMock())
    thread = SimpleNamespace(id=30, type=discord.ChannelType.public_thread, parent_id=20,
                             parent=channel, locked=False, permissions_for=channel.permissions_for,
                             fetch_message=AsyncMock())
    def message(identifier=10, content="<@99> Apakah ada error hari ini?", author_id=5, in_thread=False):
        return SimpleNamespace(
            id=identifier, content=content, author=SimpleNamespace(id=author_id, bot=False),
            mentions=[client.user], guild=SimpleNamespace(id=1, me=client.user), webhook_id=None,
            channel=thread if in_thread else channel, thread=None,
            reply=AsyncMock(), create_thread=AsyncMock(return_value=thread),
            add_reaction=AsyncMock(), remove_reaction=AsyncMock(),
        )
    answer = Mock(return_value={"answer": "Ada satu alert. Periksa evidence sebelum meminta approval.",
                                "alert_key": "", "agent_run_id": "", "transport": "api",
                                "llm_runtime": {"provider": "heuristic", "model": "heuristic-v1", "used_heuristic": True}})
    async def send(**kwargs):
        return SimpleNamespace(id=40, author=client.user, embeds=kwargs["embeds"])
    thread.send = AsyncMock(side_effect=send)
    clock = Mock(return_value=100)
    store = MemoryStore()
    audit = Mock()
    workspace = SimpleNamespace(bind_thread=AsyncMock(return_value=None), canonical_thread=AsyncMock(return_value=thread))
    workspace.ensure = AsyncMock(return_value=SimpleNamespace(
        state=SimpleNamespace(alert_ref="DQ-20261005-3B55A6", alert_key="system-key", starter_message_id="30"), thread=thread,
    ))
    handler = MentionAskHandler(client, 1, 20, store=store, answer=answer, audit=audit, clock=clock, workspace=workspace)
    return SimpleNamespace(**locals())


def test_root_mention_creates_one_thread_and_pings_only_requester():
    env = environment()
    message = env.message()
    asyncio.run(env.handler.handle(message))
    message.create_thread.assert_awaited_once()
    kwargs = env.thread.send.call_args.kwargs
    assert kwargs["content"] == "<@5>"
    assert kwargs["allowed_mentions"].to_dict() == {"users": [5], "parse": []}
    assert "view" not in kwargs
    assert "No external model call" in kwargs["embeds"][0].footer.text
    assert env.store.rows["10"].status == "answered"
    assert env.store.rows["10"].thread_id == "30"
    assert [call.args[0] for call in message.add_reaction.await_args_list] == [PROCESSING, ANSWERED]
    assert [call.args[1] for call in env.audit.call_args_list] == ["started", "answered"]
    assert not any(word in str(env.audit.call_args_list) for word in ["Apakah ada", "Ada satu alert"])


@pytest.mark.parametrize("ignored", ["bot", "webhook", "dm", "guild", "channel", "no_mention", "role_only"])
def test_messages_outside_direct_mention_scope_do_not_call_copilot(ignored):
    env = environment()
    message = env.message()
    if ignored == "bot": message.author.bot = True
    if ignored == "webhook": message.webhook_id = 9
    if ignored == "dm": message.guild = None
    if ignored == "guild": message.guild.id = 8
    if ignored == "channel": message.channel.id = 21
    if ignored == "no_mention": message.mentions = []
    if ignored == "role_only": message.content = "<@&99> Apakah ada error?"
    asyncio.run(env.handler.handle(message))
    env.answer.assert_not_called()
    message.create_thread.assert_not_awaited()


@pytest.mark.parametrize("content", [
    "@DataSentry untuk DQ-20261005-3B55A6, bukti apa yang belum tersedia?",
    "```\n@DataSentry apakah ada error?\n```",
    "<@123> apakah ada error?",
])
def test_unresolved_or_other_bot_mentions_do_not_claim_or_call_provider(content):
    env = environment()
    message = env.message(content=content)
    message.mentions = [SimpleNamespace(id=123)] if content.startswith("<@123>") else []
    asyncio.run(env.handler.handle(message))
    assert not env.store.rows
    env.answer.assert_not_called()
    env.audit.assert_not_called()
    message.add_reaction.assert_not_awaited()
    message.create_thread.assert_not_awaited()


@pytest.mark.parametrize("question", ["", "xx", "a" * 1001])
def test_invalid_question_length_is_rejected_before_claim_and_call(question):
    env = environment()
    message = env.message(content="<@99> " + question)
    asyncio.run(env.handler.handle(message))
    assert not env.store.rows
    env.answer.assert_not_called()
    message.reply.assert_awaited_once()


def test_multiple_incident_references_are_not_mixed():
    env = environment()
    message = env.message(content="<@99> Bandingkan DQ-20261005-3B55A6 DQ-20261006-AABBCC")
    asyncio.run(env.handler.handle(message))
    env.answer.assert_not_called()


def test_duplicate_gateway_event_and_new_handler_do_not_repeat_model_call():
    env = environment()
    message = env.message()
    asyncio.run(env.handler.handle(message))
    env.clock.return_value = 111
    fresh = MentionAskHandler(env.client, 1, 20, store=env.store, answer=env.answer, audit=env.audit)
    asyncio.run(fresh.handle(message))
    env.answer.assert_called_once()
    message.create_thread.assert_awaited_once()


def test_interrupted_durable_claim_is_not_replayed():
    env = environment()
    env.store.claim(MentionRequestState("10", "10", "1", "20", "5"))
    asyncio.run(env.handler.handle(env.message()))
    env.answer.assert_not_called()
    assert env.store.rows["10"].status == "claimed"


def test_followup_uses_same_thread_and_only_visible_completed_exchange():
    env = environment()
    first = env.message(content="<@99> Jelaskan DQ-20261005-3B55A6")
    asyncio.run(env.handler.handle(first))
    reply = SimpleNamespace(author=env.client.user, embeds=env.thread.send.call_args.kwargs["embeds"])
    env.channel.fetch_message.return_value = first
    env.thread.fetch_message.return_value = reply
    env.clock.return_value = 111
    followup = env.message(identifier=11, content="<@!99> Kenapa bisa begitu?", in_thread=True)
    asyncio.run(env.handler.handle(followup))
    followup.create_thread.assert_not_awaited()
    assert env.thread.send.await_count == 2
    assert env.answer.call_args.args[1] == "DQ-20261005-3B55A6"
    context = env.answer.call_args.kwargs["conversation_context"]
    assert len(context) == 1
    assert "Jelaskan DQ-" in context[0]["question"]
    env.channel.fetch_message.assert_awaited_once_with(10)


@pytest.mark.parametrize("failure", ["permissions", "locked", "unknown_thread", "capacity", "user_active", "thread_active", "cooldown"])
def test_rejections_never_spend_provider_budget(failure):
    env = environment()
    message = env.message(in_thread=failure in {"locked", "unknown_thread"})
    if failure == "permissions": env.permissions.create_public_threads = False
    if failure == "locked": env.thread.locked = True
    if failure == "capacity": env.handler.active_count = 3
    if failure == "user_active": env.handler.active_users.add(5)
    if failure == "thread_active": env.handler.active_threads.add(10)
    if failure == "cooldown": env.handler.last_request[5] = 95
    asyncio.run(env.handler.handle(message))
    env.answer.assert_not_called()
    message.reply.assert_awaited_once()


@pytest.mark.parametrize("where", ["provider", "thread", "send", "store", "malformed"])
def test_failures_are_sanitized_and_never_retry_provider(where):
    env = environment()
    message = env.message()
    secret_error = RuntimeError("Authorization: secret must not appear in logs")
    if where == "provider": env.answer.side_effect = secret_error
    if where == "thread": message.create_thread.side_effect = secret_error
    if where == "send": env.thread.send.side_effect = secret_error
    if where == "store": env.store.claim = Mock(side_effect=secret_error)
    if where == "malformed": env.answer.return_value = {"answer": ""}
    asyncio.run(env.handler.handle(message))
    assert env.answer.call_count <= 1
    assert env.handler.active_count == 0
    assert not env.handler.active_users
    assert message.add_reaction.await_args_list[-1].args[0] == FAILED
    assert "secret" not in str(message.reply.call_args_list)
    assert "secret" not in str(env.audit.call_args_list)


def test_context_and_thread_names_are_bounded():
    turns = [{"question": "a" * 1000, "answer": "b" * 4000}] * 3
    context = bounded_conversation_context(turns)
    assert len(context) <= 2
    assert sum(len(row["question"]) + len(row["answer"]) for row in context) <= 4000
    title = thread_title("@everyone <@99> **wrong**\n" + "a" * 100)
    assert len(title) <= 80
    assert "<@" not in title and "\n" not in title and "*" not in title


def test_api_conversation_validation_is_backward_compatible_and_bounded():
    assert CopilotAnswerRequest(question="Any incidents?").conversation_context == []
    with pytest.raises(ValidationError):
        CopilotAnswerRequest(question="Any incidents?", conversation_context=[{"question": "hey", "answer": "a"}] * 3)
    with pytest.raises(ValidationError):
        CopilotAnswerRequest(question="Any incidents?", conversation_context=[{"question": "hey", "answer": "a" * 4000}])
    with pytest.raises(ValidationError):
        CopilotAnswerRequest(question="Any incidents?", conversation_context=[{"question": "hey", "answer": "a", "system_prompt": "bypass"}])


def test_postgres_claim_uses_uniqueness_and_parameter_binding():
    cursor = Mock()
    cursor.__enter__ = Mock(return_value=cursor)
    cursor.__exit__ = Mock()
    cursor.fetchone.return_value = ("10",)
    connection = Mock()
    connection.cursor.return_value = cursor
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock()
    store = MentionRequestStore(connection_factory=lambda: connection)
    assert store.claim(MentionRequestState("10", "10", "1", "20", "5"))
    sql, parameters = cursor.execute.call_args.args
    assert "ON CONFLICT (source_message_id) DO NOTHING" in sql
    assert "RETURNING source_message_id" in sql
    assert parameters[0] == "10"
    assert "10" not in sql


def test_timeout_retains_admission_until_background_request_finishes():
    env = environment()
    result = env.answer.return_value
    def slow_answer(*args, **kwargs):
        time.sleep(0.08)
        return result
    env.answer.side_effect = slow_answer
    env.handler.request_timeout_seconds = 0.002
    async def run():
        await env.handler.handle(env.message())
        assert env.handler.active_count == 1
        assert 5 in env.handler.active_users
        await asyncio.sleep(0.12)
        assert env.handler.active_count == 0
    asyncio.run(run())
    env.thread.send.assert_not_awaited()
    assert env.store.rows["10"].status == "failed"


def test_external_gemini_provenance_and_no_nested_thread():
    env = environment()
    env.answer.return_value["llm_runtime"] = {
        "provider": "gemini", "model": "gemini-3.5-flash-lite", "used_heuristic": False,
        "runtime_mode": "external_model", "response_mode": "external_model",
    }
    asyncio.run(env.handler.handle(env.message()))
    embed = env.thread.send.call_args.kwargs["embeds"][0]
    assert "Gemini" in embed.footer.text
    assert "No external model call" not in embed.footer.text


def test_deleted_or_untrusted_history_is_not_forwarded():
    env = environment()
    first = env.message()
    asyncio.run(env.handler.handle(first))
    first.author.bot = True
    env.channel.fetch_message.return_value = first
    env.thread.fetch_message.return_value = SimpleNamespace(
        author=env.client.user, embeds=env.thread.send.call_args.kwargs["embeds"])
    env.clock.return_value = 111
    asyncio.run(env.handler.handle(env.message(identifier=11, in_thread=True)))
    assert env.answer.call_args.kwargs["conversation_context"] == []


@pytest.mark.parametrize("field_name", ["Follow-up question", "Pertanyaan lanjutan"])
def test_modal_dialogue_is_loaded_from_verified_visible_answer_not_interaction_id(field_name):
    env = environment()
    history = [MentionRequestState("123", "10", "1", "20", "5", thread_id="30",
                                   answer_message_id="40", status="answered", source_kind="modal")]
    embed = discord.Embed(description="The exact missing segments are not evidenced yet.")
    embed.add_field(name=field_name, value="What evidence is missing?")
    env.thread.fetch_message.return_value = SimpleNamespace(author=env.client.user, embeds=[embed])
    context = asyncio.run(env.handler.conversation(env.thread, history, "", "5"))
    assert context == [{"question": "What evidence is missing?", "answer": embed.description}]
    env.thread.fetch_message.assert_awaited_once_with(40)
    env.channel.fetch_message.assert_not_awaited()


def test_canonical_thread_with_no_mention_history_uses_registry_alert_ref():
    env = environment()
    env.workspace.bind_thread.return_value = SimpleNamespace(alert_ref="DQ-20261005-3B55A6",
                                                             starter_message_id="30", alert_key="system-key")
    message = env.message(in_thread=True)
    asyncio.run(env.handler.handle(message))
    assert env.answer.call_args.args[1] == "DQ-20261005-3B55A6"
    assert env.answer.call_args.kwargs["conversation_context"] == []
    message.create_thread.assert_not_awaited()
    assert env.store.rows["10"].root_message_id == "30"
    env.workspace.canonical_thread.assert_awaited_once_with(30)


def test_different_explicit_ref_in_canonical_thread_is_blocked_before_claim_or_provider():
    env = environment()
    env.workspace.bind_thread.side_effect = PermissionError("Open the other incident separately")
    asyncio.run(env.handler.handle(env.message(in_thread=True, content="<@99> Explain DQ-20261006-AABBCC")))
    assert not env.store.rows
    env.answer.assert_not_called()
    assert env.handler.active_count == 0


def test_explicit_incident_mention_reuses_workspace_not_a_second_question_thread():
    env = environment()
    message = env.message(content="<@99> Explain DQ-20261005-3B55A6")
    asyncio.run(env.handler.handle(message))
    message.create_thread.assert_not_awaited()
    env.workspace.ensure.assert_awaited_once_with("DQ-20261005-3B55A6")
    assert env.store.rows["10"].thread_id == "30"


def test_second_actor_in_question_thread_has_no_access_to_first_actors_turns():
    env = environment()
    asyncio.run(env.handler.handle(env.message()))
    env.clock.return_value = 111
    asyncio.run(env.handler.handle(env.message(identifier=11, author_id=6, in_thread=True)))
    assert env.answer.call_args.kwargs["conversation_context"] == []
    env.channel.fetch_message.assert_not_awaited()
    env.thread.fetch_message.assert_not_awaited()


@pytest.mark.parametrize("field,value", [("actor_id", "6"), ("alert_ref", "DQ-20261006-AABBCC"),
                                         ("guild_id", "2"), ("channel_id", "21"), ("thread_id", "31")])
def test_conversation_defends_scope_even_if_history_store_returns_foreign_row(field, value):
    from dataclasses import replace
    env = environment()
    row = MentionRequestState("10", "10", "1", "20", "5", thread_id="30", answer_message_id="40", status="answered")
    row = replace(row, **{field: value})
    assert asyncio.run(env.handler.conversation(env.thread, [row], "", "5")) == []
    env.thread.fetch_message.assert_not_awaited()


def test_command_dialogue_uses_same_verified_question_field_as_modal():
    env = environment()
    row = MentionRequestState("123", "123", "1", "20", "5", thread_id="30", answer_message_id="40",
                              status="answered", source_kind="command")
    embed = discord.Embed(description="The partition is missing.")
    embed.add_field(name="Follow-up question", value="What is missing?")
    env.thread.fetch_message.return_value = SimpleNamespace(author=env.client.user, embeds=[embed])
    assert asyncio.run(env.handler.conversation(env.thread, [row], "", "5")) == [
        {"question": "What is missing?", "answer": "The partition is missing."},
    ]
    env.channel.fetch_message.assert_not_awaited()


def test_history_query_is_same_actor_incident_guild_parent_and_last_two():
    cursor = Mock()
    cursor.__enter__ = Mock(return_value=cursor)
    cursor.__exit__ = Mock(return_value=False)
    cursor.fetchall.return_value = []
    connection = Mock()
    connection.cursor.return_value = cursor
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)
    store = MentionRequestStore(connection_factory=lambda: connection)
    assert store.history("30", "5", "DQ-20261005-3B55A6", "1", "20") == []
    sql, params = cursor.execute.call_args.args
    assert params == ("30", "5", "DQ-20261005-3B55A6", "1", "20")
    assert all(clause in sql for clause in ("actor_id = %s", "alert_ref = %s", "guild_id = %s",
                                           "channel_id = %s", "LIMIT 2"))


@pytest.mark.parametrize("language,expected", [("en", "Use one Alert Ref per question to avoid mixing incident evidence."),
                                             ("id", "Gunakan satu Alert Ref per pertanyaan agar bukti incident tidak tercampur.")])
def test_feedback_uses_pref_context_catalog_and_suppresses_mentions(monkeypatch, language, expected):
    import threading
    from apps.discord_bot import preferences
    from apps.common.locales import discord as catalog
    env = environment()
    loop_thread = threading.get_ident()
    def language_preference(*args):
        assert threading.get_ident() != loop_thread
        return language
    monkeypatch.setattr(preferences, "get_user_language", language_preference)
    key = "Use one Alert Ref per question to avoid mixing incident evidence."
    monkeypatch.setitem(catalog.MESSAGES, key, "Gunakan satu Alert Ref per pertanyaan agar bukti incident tidak tercampur.")
    message = env.message(content="<@99> Compare DQ-20261005-3B55A6 and DQ-20261006-AABBCC")
    asyncio.run(env.handler.handle(message))
    assert message.reply.call_args.args[0] == expected
    assert message.reply.call_args.kwargs["allowed_mentions"].to_dict() == {"parse": []}
    env.answer.assert_not_called()


def test_latest_question_priority_is_explicit_in_shared_copilot_prompt(monkeypatch):
    from agent.llm import copilot
    generate = Mock(return_value="Missing segment identities are not evidenced.")
    monkeypatch.setattr(copilot, "run_copilot_task", generate)
    copilot.build_operator_answer("Which evidence is missing?", conversation_context=[
        {"question": "What failed?", "answer": "Segment coverage is incomplete."}])
    kwargs = generate.call_args.kwargs
    assert kwargs["context"]["question"] == "Which evidence is missing?"
    assert "do not answer an earlier question" in kwargs["prompt"]
    assert "focus on what the latest question adds" in kwargs["prompt"]
    assert "does not prove that evidence is absent" in kwargs["prompt"]
    assert "/dq triage alert_key:<Alert Ref>" in kwargs["prompt"]


def test_overview_answer_clearly_discloses_that_no_incident_is_selected():
    from apps.discord_bot.formatters import render_operator_answer_embed
    result = render_operator_answer_embed(answer="Select one alert to load incident-specific evidence.")
    fields = result.payload["embeds"][0]["fields"]
    assert fields == [{"name": "Context", "value": "Platform overview - no incident selected", "inline": False}]
    assert not result.action_rows


def test_gateway_event_is_wired_without_privileged_intent():
    from apps.discord_bot import bot
    assert callable(bot.client.on_message)
    assert bot.intents.guild_messages
    assert not bot.intents.message_content


def test_personal_copilot_opt_in_is_scoped_to_api_not_scheduled_runner():
    from pathlib import Path
    import yaml
    root = Path(__file__).resolve().parents[1]
    compose = yaml.safe_load((root / "infra/docker-compose.yml").read_text(encoding="utf-8"))
    assert compose["services"]["api"]["environment"]["EXTERNAL_LLM_ENABLED"] == "${COPILOT_EXTERNAL_LLM_ENABLED:-false}"
    assert "COPILOT_EXTERNAL_LLM_ENABLED" not in str(compose["services"]["dq-runner"]["environment"])
    assert "COPILOT_EXTERNAL_LLM_ENABLED" not in str(compose["services"]["discord-bot"]["environment"])
    assert "COPILOT_EXTERNAL_LLM_ENABLED=false" in (root / ".env.example").read_text(encoding="utf-8")


def test_service_timeout_never_runs_another_external_model(monkeypatch):
    from apps.discord_bot import service
    from apps.common.control_plane import ControlPlaneTransportError
    api = Mock()
    api.answer_copilot.side_effect = ControlPlaneTransportError("timeout")
    monkeypatch.setattr(service, "build_control_plane_client", lambda **kwargs: api)
    monkeypatch.setattr(service, "list_alerts", lambda **kwargs: {"alerts": []})
    build = Mock(return_value="External AI unavailable; no active evidence.")
    monkeypatch.setattr(service, "build_operator_answer", build)
    audit_client = Mock()
    monkeypatch.setattr(service, "build_clickhouse_client", Mock(return_value=audit_client))
    monkeypatch.setattr(service, "write_agent_audit_event", Mock())
    service.answer_discord_question("Any incidents?", conversation_context=[], mention_mode=True)
    assert api.timeout_seconds == 60
    assert build.call_args.kwargs["force_heuristic"] is True
    api.answer_copilot.assert_called_once()
    audit_client.close.assert_called_once()


@pytest.mark.parametrize("incident_grounded", [False, True])
def test_local_copilot_audits_route_and_answer_with_complete_correlation(monkeypatch, incident_grounded):
    from apps.discord_bot import service
    from agent.llm.client import LlmResponse
    from uuid import uuid4
    alert = SimpleNamespace(alert_id=uuid4(), alert_key="system-key", alert_display_id="DQ-20261005-3B55A6")
    monkeypatch.setattr(service, "build_control_plane_client", lambda **kwargs: None)
    monkeypatch.setattr(service, "list_alerts", lambda **kwargs: {"alerts": []})
    monkeypatch.setattr(service, "load_alert", lambda **kwargs: alert)
    client = Mock()
    monkeypatch.setattr(service, "build_clickhouse_client", lambda: client)
    route_audit = create_autospec(service.write_llm_route_audit_event)
    answer_audit = create_autospec(service.write_agent_audit_event)
    monkeypatch.setattr(service, "write_llm_route_audit_event", route_audit)
    monkeypatch.setattr(service, "write_agent_audit_event", answer_audit)

    def build_answer(**kwargs):
        kwargs["response_sink"].append(LlmResponse(
            agent_run_id=kwargs["agent_run_id"], route_name="cheap_summary",
            provider="heuristic", model="heuristic-v1", content="Local answer.", used_heuristic=True,
        ))
        return "Local answer."
    monkeypatch.setattr(service, "build_operator_answer", build_answer)

    result = service.answer_discord_question("Any incidents?", alert.alert_display_id if incident_grounded else "")

    route_audit.assert_called_once()
    answer_audit.assert_called_once()
    assert route_audit.call_args.kwargs["alert_id"] == (alert.alert_id if incident_grounded else None)
    assert route_audit.call_args.kwargs["alert_key"] == (alert.alert_key if incident_grounded else "")
    assert str(route_audit.call_args.kwargs["agent_run_id"]) == result["agent_run_id"]
    assert answer_audit.call_args.kwargs["agent_run_id"] == route_audit.call_args.kwargs["agent_run_id"]
    assert result["alert_ref"] == (alert.alert_display_id if incident_grounded else "")
    assert result["llm_runtime"]["used_heuristic"]
    client.close.assert_called_once()


def test_copilot_budget_is_one_external_attempt_and_context_is_untrusted(monkeypatch):
    from agent.llm import copilot
    from agent.llm.client import LlmResponse
    from agent.supervisor.budgets import active_supervisor_llm_budget, SupervisorLlmBudgetExceeded
    from uuid import uuid4
    captured = {}
    def generate(**kwargs):
        ledger = active_supervisor_llm_budget()
        assert ledger.max_model_calls == 1
        ledger.reserve_model_call(100, 0.001)
        with pytest.raises(SupervisorLlmBudgetExceeded):
            ledger.reserve_model_call(100, 0.001)
        captured.update(kwargs)
        return LlmResponse(agent_run_id=uuid4(), route_name="cheap_summary", provider="gemini",
                           model="test-model", content="Grounded answer.")
    monkeypatch.setattr(copilot, "run_llm_task", generate)
    responses = []
    answer = copilot.build_operator_answer("Why?", conversation_context=[{"question": "What failed?", "answer": "A partition."}], response_sink=responses)
    assert answer == "Grounded answer."
    assert "untrusted dialogue" in captured["prompt"]
    assert captured["context"]["conversation_context"][0]["answer"] == "A partition."
    assert responses[0].metadata["copilot_budget"]["model_calls"] == 1


def test_real_router_provider_failure_makes_at_most_one_completion_call(monkeypatch):
    from agent.llm import client as llm_client, copilot
    monkeypatch.setenv("EXTERNAL_LLM_ENABLED", "true")
    monkeypatch.setenv("GEMINI_API_KEY", "test-not-a-key")
    create = Mock(side_effect=RuntimeError("simulated provider failure"))
    monkeypatch.setattr(llm_client, "create_openai_compatible_client",
                        lambda **kwargs: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    responses = []
    copilot.build_operator_answer("Any active incidents?", response_sink=responses)
    assert create.call_count == 1
    assert responses[-1].used_heuristic
    assert responses[-1].metadata["copilot_budget"]["model_calls"] == 1
