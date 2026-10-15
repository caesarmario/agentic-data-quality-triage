"""Help is deterministic, personal, scoped and distinct from investigation actions."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from apps.common.i18n import language_context
from apps.common.control_plane import ControlPlaneTransportError
from apps.discord_bot import bot, help as guide, interactions


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setenv("DISCORD_GUILD_ID", "1")
    monkeypatch.setenv("DISCORD_ALERTS_CHANNEL_ID", "10")
    monkeypatch.setenv("DISCORD_TRIAGE_CHANNEL_ID", "20")
    monkeypatch.setenv("DISCORD_OPS_CHANNEL_ID", "30")
    monkeypatch.setenv("DISCORD_OPERATOR_UI_URL", "http://localhost:8501")
    monkeypatch.setattr(guide, "_audit", AsyncMock())
    monkeypatch.setattr("apps.discord_bot.preferences.get_user_language", Mock(return_value="en"))


def interaction(ephemeral=False):
    client = SimpleNamespace(user=SimpleNamespace(id=99),
        datasentry_help_catalog=lambda: guide.command_catalog(bot.tree, bot.guild_object()))
    response = SimpleNamespace(is_done=Mock(return_value=False), defer=AsyncMock(),
                               send_message=AsyncMock(), edit_message=AsyncMock())
    async def defer(**kwargs):
        response.is_done.return_value = True
    response.defer.side_effect = defer
    return SimpleNamespace(client=client, guild_id=1, user=SimpleNamespace(id=7, bot=False),
        channel=SimpleNamespace(id=20, type=discord.ChannelType.text), response=response,
        message=SimpleNamespace(author=client.user, id=123, flags=SimpleNamespace(ephemeral=ephemeral)),
        edit_original_response=AsyncMock())


def catalog():
    return guide.command_catalog(bot.tree, bot.guild_object())


@pytest.mark.parametrize("locale", ["en", "id"])
@pytest.mark.parametrize("topic,page", [("start", 0), ("commands", 0), ("commands", 1), ("commands", 2),
                                       ("buttons", 0), ("buttons", 1), ("safety", 0)])
def test_help_bounds_locales_and_readonly_actions(locale, topic, page):
    with language_context(locale):
        result = guide.render_help(topic, page, catalog=catalog())
    e = result.payload["embeds"][0]
    assert len(e["title"]) <= 256 and len(e["description"]) <= 4096
    assert len(e["fields"]) <= 25 and all(len(f["value"]) <= 1024 for f in e["fields"])
    total = len(e["title"]) + len(e["description"]) + len(e["footer"]["text"])
    assert total + sum(len(f["name"]) + len(f["value"]) for f in e["fields"]) <= 6000
    assert result.payload["allowed_mentions"] == {"parse": []}
    for row in result.payload["components"]:
        assert len(row["components"]) <= 5
        for item in row["components"]:
            assert item["type"] == 2 and len(item["label"]) <= 80
            if "custom_id" in item:
                assert guide.PATTERN.fullmatch(item["custom_id"])
                assert len(item["custom_id"]) <= 100 and "dq:v1:" not in item["custom_id"]
    custom_ids = [item["custom_id"] for row in result.payload["components"]
                  for item in row["components"] if "custom_id" in item]
    assert len(custom_ids) == len(set(custom_ids))
    assert "No AI call" in e["footer"]["text"] if locale == "en" else "Tidak ada panggilan AI" in e["footer"]["text"]


@pytest.mark.parametrize("topic,page", [("commands", 1), ("commands", 2), ("buttons", 1)])
def test_gateway_navigation_preserves_unique_serialized_custom_ids(topic, page):
    async def check():
        result = guide.render_help(topic, page, catalog=catalog())
        view = interactions.build_discord_view(result)
        expected = [a.custom_id for row in result.action_rows for a in row.actions if a.custom_id]
        actual = [item.custom_id for item in view.children if item.custom_id]
        assert actual == expected and len(actual) == len(set(actual))
        for item in view.children:
            if item.custom_id:
                match = guide.PATTERN.fullmatch(item.custom_id)
                replay = await guide.HelpActionButton.from_custom_id(None, item, match)
                assert replay.custom_id == item.custom_id
    asyncio.run(check())


def test_registration_and_catalog_use_actual_inputs_and_keep_aliases():
    commands = catalog()
    assert set(commands) == set(guide.PURPOSES)
    assert commands["alerts"].required == ()
    assert commands["daily_summary"].required == ("dt",)
    assert commands["ask"].required == ("question",)
    assert "alert_key" in commands["ask"].optional
    assert bot.tree.get_command("help", guild=bot.guild_object()) is not None
    assert bot.tree.get_command("alerts", guild=bot.guild_object()) is not None
    assert len(bot.HELP_CHOICES) == 4
    assert not bot.intents.message_content


def test_catalog_never_lists_unregistered_commands():
    partial = {"alerts": catalog()["alerts"]}
    result = guide.render_help("commands", catalog=partial)
    names = [f["name"] for f in result.payload["embeds"][0]["fields"]]
    assert "/dq alerts" in names and "/dq triage" not in names


@pytest.mark.parametrize("url", ["https://user:password@example.com", "https://example.com?token=secret", "javascript:alert(1)"])
def test_workspace_never_renders_credentials_or_untrusted_links(monkeypatch, url):
    monkeypatch.setenv("DISCORD_OPERATOR_UI_URL", url)
    assert not any(a.style == "link" for row in guide.help_rows("start", 0) for a in row.actions)


@pytest.mark.parametrize("exc,expected", [(ValueError("invalid date"), "date like"),
    (ValueError("No alert found for raw secret"), "Alert Ref was not found"),
    (PermissionError("Authorization: secret"), "operator access"),
    (TimeoutError("sk-secret"), "couldn't confirm"),
    (RuntimeError("secret @everyone"), "couldn't confirm")])
def test_error_then_guidance_never_echoes_sensitive_arguments(exc, expected):
    result = guide.render_help_error(exc)
    assert len(result.payload["embeds"]) == 2
    assert expected in str(result.payload["embeds"][0])
    assert result.payload["embeds"][1]["title"] == "Need a starting point?"
    assert "secret" not in str(result.payload) and "@everyone" not in str(result.payload)
    assert len(result.content_hash) == 64


def test_backend_read_failure_not_blame_user():
    result = guide.render_help_error(ControlPlaneTransportError("private endpoint"), "Help incidents")
    assert "reach the data service" in result.payload["embeds"][0]["title"]


def test_rejected_ai_identity_does_not_blame_user_or_claim_original_call_was_free():
    result = guide.render_help_error(ValueError("Copilot returned a different incident reference."), "Copilot answer")
    assert "confirm" in result.payload["embeds"][0]["title"]
    assert result.payload["embeds"][0]["footer"]["text"] == "System feedback"


def test_help_command_is_private_and_shared_welcome_never_edited():
    it = interaction(False)
    asyncio.run(guide.show_help(it, button=True))
    it.response.defer.assert_awaited_once_with(thinking=True, ephemeral=True)
    it.edit_original_response.assert_awaited_once()
    it.response.edit_message.assert_not_awaited()


def test_personal_navigation_updates_same_response():
    it = interaction(True)
    asyncio.run(guide.show_help(it, "buttons", 1, button=True))
    it.response.defer.assert_awaited_once_with(thinking=False, ephemeral=True)
    assert "Approval" in it.edit_original_response.call_args.kwargs["embeds"][0].title


def test_incident_lookup_is_bounded_private_and_does_not_call_copilot(monkeypatch):
    fetch = Mock(return_value=({"alerts": []}, "api"))
    monkeypatch.setattr(guide, "fetch_discord_alerts", fetch)
    monkeypatch.setattr("agent.llm.copilot.build_alert_list_copilot_note", Mock(side_effect=AssertionError("No model allowed")))
    it = interaction(True)
    asyncio.run(guide.show_help(it, "incidents", button=True))
    fetch.assert_called_once_with("active", None, 5)
    kwargs = it.edit_original_response.call_args.kwargs
    assert kwargs["embeds"][0].footer.text == guide.FOOTER
    assert kwargs["view"] is not None


@pytest.mark.parametrize("bad", ["guild", "channel", "bot", "author"])
def test_invalid_help_scope_cannot_read_incidents(monkeypatch, bad):
    it = interaction(True)
    if bad == "guild": it.guild_id = 2
    if bad == "channel": it.channel.id = 40
    if bad == "bot": it.user.bot = True
    if bad == "author": it.message.author = SimpleNamespace(id=88)
    fetch = Mock(side_effect=AssertionError("Unauthorized read"))
    monkeypatch.setattr(guide, "fetch_discord_alerts", fetch)
    asyncio.run(guide.show_help(it, "incidents", button=True))
    fetch.assert_not_called()
    assert len(it.edit_original_response.call_args.kwargs["embeds"]) == 2


def message(text="<@99> help"):
    return SimpleNamespace(id=333, guild=SimpleNamespace(id=1), author=SimpleNamespace(id=7, bot=False),
        webhook_id=None, mentions=[SimpleNamespace(id=99)], content=text,
        channel=SimpleNamespace(id=20, type=discord.ChannelType.text), reply=AsyncMock(return_value=SimpleNamespace(id=334)))


@pytest.mark.parametrize("text", ["<@99> help", "<@!99> bantuan", "<@99> help buttons"])
def test_mention_help_does_not_create_ai_claim_thread_or_model_request(text):
    msg = message(text)
    client = interaction().client
    assert asyncio.run(guide.handle_help_mention(client, msg))
    msg.reply.assert_awaited_once()
    assert msg.reply.call_args.kwargs["allowed_mentions"].to_dict() == {"parse": []}
    assert not hasattr(client, "mention_ask_handler")


@pytest.mark.parametrize("bad", ["unmentioned", "plain_name", "bot", "webhook", "guild", "dm", "channel"])
def test_ignored_help_messages_never_reply(bad):
    msg = message()
    if bad == "unmentioned": msg.mentions = []
    if bad == "plain_name": msg.content = "@DataSentry help"
    if bad == "bot": msg.author.bot = True
    if bad == "webhook": msg.webhook_id = 8
    if bad == "guild": msg.guild.id = 2
    if bad == "dm": msg.guild = None
    if bad == "channel": msg.channel.id = 30
    asyncio.run(guide.handle_help_mention(interaction().client, msg))
    msg.reply.assert_not_awaited()


def test_help_does_not_swallow_natural_ai_question():
    msg = message("<@99> help me understand this incident")
    assert not asyncio.run(guide.handle_help_mention(interaction().client, msg))


def test_mention_router_bypasses_ai_handler_for_help(monkeypatch):
    msg = message()
    handle = AsyncMock()
    monkeypatch.setattr(bot, "client", interaction().client)
    monkeypatch.setattr(bot.mention_ask, "handle", handle)
    asyncio.run(bot.on_message(msg))
    msg.reply.assert_awaited_once()
    handle.assert_not_awaited()


def test_invalid_help_topic_is_guidance_not_an_ai_question(monkeypatch):
    msg = message("<@99> help bogus")
    handle = AsyncMock()
    monkeypatch.setattr(bot, "client", interaction().client)
    monkeypatch.setattr(bot.mention_ask, "handle", handle)
    asyncio.run(bot.on_message(msg))
    assert len(msg.reply.call_args.kwargs["embeds"]) == 2
    assert "input" in msg.reply.call_args.kwargs["embeds"][0].title
    handle.assert_not_awaited()
    assert guide._audit.await_args.args[2] == "rejected"


def test_mention_help_is_bounded_separately_from_ai_admission():
    client = interaction().client
    a, b = message(), message()
    asyncio.run(guide.handle_help_mention(client, a))
    asyncio.run(guide.handle_help_mention(client, b))
    a.reply.assert_awaited_once()
    b.reply.assert_not_awaited()
    assert not hasattr(client, "active_users")


@pytest.mark.parametrize("registered", [True, False])
def test_help_in_threads_requires_durable_registration(monkeypatch, registered):
    it = interaction()
    it.channel = SimpleNamespace(id=50, type=discord.ChannelType.public_thread, parent_id=20, locked=False)
    monkeypatch.setattr(guide.IncidentThreadStore, "find_thread", Mock(return_value=None))
    monkeypatch.setattr(guide.MentionRequestStore, "find_thread", Mock(return_value=
        SimpleNamespace(guild_id="1", channel_id="20") if registered else None))
    if registered:
        asyncio.run(guide.validate_help_scope(it.client, 1, it.channel, it.user))
    else:
        with pytest.raises(PermissionError):
            asyncio.run(guide.validate_help_scope(it.client, 1, it.channel, it.user))


def test_guide_works_when_preference_database_is_unavailable(monkeypatch):
    monkeypatch.setattr("apps.discord_bot.preferences.get_user_language", Mock(side_effect=RuntimeError("offline")))
    it = interaction()
    asyncio.run(guide.show_help(it))
    assert it.edit_original_response.call_args.kwargs["embeds"][0].title.endswith("DataSentry Help")


def test_welcome_failure_does_not_break_command_readiness(monkeypatch):
    monkeypatch.setenv("DISCORD_WELCOME_ENABLED", "true")
    monkeypatch.setattr(bot.tree, "sync", AsyncMock())
    monkeypatch.setattr("apps.discord_bot.welcome.sync_welcome", AsyncMock(side_effect=RuntimeError("offline")))
    asyncio.run(bot.on_ready())
    bot.tree.sync.assert_awaited_once()


def test_dynamic_help_registration_and_id_limits():
    captured = []
    interactions.register_persistent_interactions(SimpleNamespace(add_dynamic_items=lambda *items: captured.extend(items)))
    assert guide.HelpActionButton in captured
    action = guide._action("buttons", "Button Guide", 1)
    item = interactions.action_to_item(action)
    assert isinstance(item, guide.HelpActionButton) and item.page == 1
    with pytest.raises(ValueError): guide.HelpActionButton("approve")


def test_error_before_ack_and_after_ack_only_one_response():
    for acknowledged in (False, True):
        it = interaction()
        it.response.is_done.return_value = acknowledged
        asyncio.run(guide.show_error(it, "Daily summary", ValueError("invalid date")))
        it.edit_original_response.assert_awaited_once()
        it.response.send_message.assert_not_awaited()
