"""Slash Ask shares durable claims, incident grounding and mention admission."""
import asyncio
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock
import threading

import discord
import pytest
from apps.discord_bot import bot, service
from apps.discord_bot import help as guide
from apps.common.control_plane import ControlPlaneTransportError
from apps.common.i18n import current_language, language_context

REF = "DQ-20261011-3E3C14"


def setup(monkeypatch, *, generic=False, language="en"):
    class Response:
        def __init__(self): self.done = False; self.deferred = []
        def is_done(self): return self.done
        async def defer(self, **kwargs): self.done = True; self.deferred.append(kwargs)
    thread = NS(id=700, parent_id=600, guild=NS(id=10), type=discord.ChannelType.public_thread,
                archived=False, locked=False)
    interaction = NS(id=900, guild_id=10, channel_id=700, channel=thread, user=NS(id=20),
                     response=Response(), followup=NS(send=AsyncMock()), client=bot.client,
                     message=None, edit_original_response=AsyncMock())
    handler = NS(admit=Mock(return_value=True), release=Mock(), release_after_completion=Mock(),
                 clock=lambda: 123.0, last_request={}, audit=Mock(), conversation=AsyncMock(return_value=[]),
                 store=NS(claim=Mock(return_value=True), save=Mock(), history=Mock(return_value=[]),
                          find_thread=Mock(return_value=NS(guild_id="10", channel_id="600", alert_ref=""))))
    runtime = NS(bind_thread=AsyncMock(return_value=None if generic else NS(alert_ref=REF)),
                 usable=AsyncMock(return_value=thread))
    monkeypatch.setattr(bot, "mention_ask", handler)
    monkeypatch.setattr(bot, "workspace_runtime", lambda *args: runtime)
    monkeypatch.setattr(bot, "get_user_language", lambda *args: language)
    monkeypatch.setattr("apps.discord_bot.preferences.get_user_language", lambda *args: language)
    monkeypatch.setattr(guide, "_audit", AsyncMock())
    monkeypatch.setattr(bot, "validate_interaction_scope", lambda *args, **kwargs: None)
    monkeypatch.setattr(bot, "GUILD_ID", 10)
    monkeypatch.setattr(bot, "TRIAGE_CHANNEL_ID", 600)
    monkeypatch.setattr(bot, "fetch_discord_debug_context", Mock(return_value={"alert_ref": REF}))
    monkeypatch.setattr(bot, "get_incident_workspace", AsyncMock(return_value=thread))
    monkeypatch.setattr(bot, "send_render_to_thread", AsyncMock(return_value=NS(id=1000)))
    answer = Mock(return_value={"answer": "Inspect the evidence first.", "alert_key": REF, "transport": "api", "agent_run_id": "run", "llm_runtime": None})
    monkeypatch.setattr(bot, "answer_discord_question", answer)
    return interaction, handler, runtime, answer


def test_slash_question_in_registered_general_thread_reuses_it(monkeypatch):
    interaction, handler, runtime, answer = setup(monkeypatch, generic=True)
    asyncio.run(bot.ask.callback(interaction, "What should I check?"))
    bot.get_incident_workspace.assert_not_awaited()
    runtime.usable.assert_awaited_once_with(interaction.channel)
    answer.assert_called_once()
    assert handler.last_request[20] == 123.0
    assert handler.store.save.call_args.args[0].source_kind == "command"


def test_slash_question_default_english_acknowledgement_preserves_workspace_link(monkeypatch):
    interaction, _, _, answer = setup(monkeypatch)
    assert current_language() == "en"
    asyncio.run(bot.ask.callback(interaction, "What should I check?", REF))
    answer.assert_called_once()
    sent = interaction.followup.send.call_args
    assert sent.args[0] == "Answer posted in your workspace: https://discord.com/channels/10/700"
    assert sent.kwargs["ephemeral"] is True
    assert current_language() == "en"


@pytest.mark.parametrize("language", ["en", "id"])
def test_slash_question_context_keeps_original_answer_question_and_identity(monkeypatch, language):
    interaction, handler, _, answer = setup(monkeypatch, language=language)
    question = "Jelaskan bukti ini. {original_question}"
    stored_answer = "No evidence returned. {original_answer}"
    worker_languages = []

    def original_answer(*args, **kwargs):
        worker_languages.append(current_language())
        return {"answer": stored_answer, "alert_key": REF, "transport": "api",
                "agent_run_id": "run-original", "llm_runtime": None}

    answer.side_effect = original_answer
    with language_context("id" if language == "en" else "en"):
        caller_language = current_language()
        asyncio.run(bot.ask.callback(interaction, question, REF))
        assert current_language() == caller_language
    assert current_language() == "en"
    assert worker_languages == [language]
    assert answer.call_args.args == (question, REF)
    render = bot.send_render_to_thread.call_args.args[1]
    payload = json.dumps(render.payload, ensure_ascii=False)
    assert stored_answer in payload
    assert question in payload
    assert REF in payload
    state = handler.store.save.call_args.args[0]
    assert (state.alert_ref, state.actor_id, state.thread_id, state.agent_run_id) == (REF, "20", "700", "run-original")


def test_slash_question_rejects_different_incident_before_call(monkeypatch):
    interaction, handler, runtime, answer = setup(monkeypatch)
    asyncio.run(bot.ask.callback(interaction, "Explain DQ-20261010-AAAAAA", REF))
    answer.assert_not_called()
    handler.store.claim.assert_not_called()


@pytest.mark.parametrize("field", ["alert_ref", "alert_key"])
def test_slash_question_rejects_mismatched_returned_identity_and_persists_failure(monkeypatch, field):
    interaction, handler, _, answer = setup(monkeypatch)
    # The selected authority has only alert_ref, so only REF is a valid returned key.
    assert bot.fetch_discord_debug_context.return_value == {"alert_ref": REF}
    result = dict(answer.return_value, alert_ref=REF)
    result[field] = "DQ-20261010-AAAAAA"
    answer.return_value = result

    asyncio.run(bot.ask.callback(interaction, "Explain this incident", REF))

    answer.assert_called_once()
    assert answer.call_args.args == ("Explain this incident", REF)
    handler.store.claim.assert_called_once()
    bot.send_render_to_thread.assert_not_awaited()
    handler.store.save.assert_called_once()
    state = handler.store.save.call_args.args[0]
    assert state.status == "failed"
    assert state.error_type == "ValueError"
    assert (state.alert_ref, state.actor_id, state.thread_id) == (REF, "20", "700")
    assert not state.answer_message_id
    handler.audit.assert_called_once()
    assert handler.audit.call_args.args[1] == "started"
    handler.release.assert_called_once_with(20, 700)
    sent = interaction.edit_original_response.call_args
    assert len(sent.kwargs["embeds"]) == 2
    assert sent.kwargs["embeds"][1].title == "Need a starting point?"
    assert "ValueError" not in str([embed.to_dict() for embed in sent.kwargs["embeds"]])
    assert interaction.response.deferred[0]["ephemeral"] is True


def test_cancelled_question_keeps_admission_until_worker_completes(monkeypatch):
    interaction, handler, runtime, answer = setup(monkeypatch)
    started, finish = threading.Event(), threading.Event()
    def wait_answer(*args, **kwargs):
        started.set()
        finish.wait(2)
        return {"answer": "done"}
    answer.side_effect = wait_answer
    async def run():
        task = asyncio.create_task(bot.ask.callback(interaction, "Explain this incident", REF))
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        handler.release.assert_not_called()
        finish.set()
        for _ in range(50):
            if handler.release_after_completion.called: break
            await asyncio.sleep(.01)
        assert handler.release_after_completion.call_count == 1
    asyncio.run(run())
    answer.assert_called_once()


def test_triage_timeout_never_starts_local_workflow(monkeypatch):
    api = NS(run_triage_report=Mock(side_effect=ControlPlaneTransportError("uncertain")))
    monkeypatch.setattr(service, "build_control_plane_client", lambda **kwargs: api)
    local = Mock()
    monkeypatch.setattr(service, "run_triage", local)
    with pytest.raises(ControlPlaneTransportError): service.run_discord_triage(REF)
    local.assert_not_called()
