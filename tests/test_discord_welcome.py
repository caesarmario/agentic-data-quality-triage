"""Focused, offline welcome delivery tests; Airflow remains final acceptance."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from random import SystemRandom
from types import SimpleNamespace
from unittest.mock import Mock
import os

import discord
import pytest

from apps.discord_bot.contracts import DiscordRenderResult
from apps.discord_bot.welcome import (
    WelcomeBusy, WelcomeLease, WelcomeState, WelcomeStore, WelcomeUncertain,
    _lock_key, _nonce, sync_welcome,
)


GUILD = 938210011
CHANNEL = 938210022
BOT = 938210033


def render(title="Welcome"):
    return DiscordRenderResult(
        payload={"content": "", "embeds": [{"title": title, "description": "Use /dq help.",
                                            "footer": {"text": "DataSentry"}}],
                 "allowed_mentions": {"parse": []}},
        narrative_source="deterministic_fallback", content_hash=title,
        fallback_markdown=title,
    )


class MemoryLease:
    def __init__(self, state):
        self.state = state
        self.saves = []

    def save(self, **changes):
        from dataclasses import replace
        self.state = replace(self.state, **changes)
        self.saves.append(changes)
        return self.state

    def replace_missing(self):
        return self.save(message_id="", content_hash="", creation_pending=False,
                         edit_pending=False, generation=self.state.generation + 1,
                         nonce=_nonce(GUILD, CHANNEL, self.state.generation + 1))


class MemoryStore:
    def __init__(self, state=None):
        self.lease = MemoryLease(state or WelcomeState(GUILD, CHANNEL, nonce=_nonce(GUILD, CHANNEL, 0)))

    @contextmanager
    def locked(self, guild_id, channel_id):
        assert (guild_id, channel_id) == (GUILD, CHANNEL)
        yield self.lease


class FakeMessage:
    def __init__(self, channel, message_id, embeds, content=""):
        self.channel = channel
        self.id = message_id
        self.author = SimpleNamespace(id=BOT)
        self.embeds = embeds
        self.content = content
        self.edit_count = 0

    async def edit(self, *, content, embeds, view, allowed_mentions):
        self.edit_count += 1
        self.content = content or ""
        self.embeds = embeds
        return self


class FakeChannel:
    def __init__(self):
        self.id = CHANNEL
        self.guild = SimpleNamespace(id=GUILD)
        self.type = discord.ChannelType.text
        self.messages = []
        self.send_count = 0
        self.send_error = None
        self.lookup_error = None
        self.history_limit = None

    async def send(self, *, content, embeds, view, nonce, allowed_mentions):
        self.send_count += 1
        message = FakeMessage(self, 100 + self.send_count, embeds, content or "")
        self.messages.append(message)
        if self.send_error:
            raise self.send_error
        return message

    async def fetch_message(self, message_id):
        if self.lookup_error:
            raise self.lookup_error
        for message in self.messages:
            if message.id == message_id:
                return message
        raise discord.NotFound(Mock(status=404, reason="Not Found"), "missing")

    def history(self, *, limit):
        self.history_limit = limit

        async def messages():
            for message in reversed(self.messages[:limit]):
                yield message

        return messages()


def environment(store=None):
    channel = FakeChannel()
    client = SimpleNamespace(user=SimpleNamespace(id=BOT), fetch_channel=Mock())

    async def fetch(channel_id):
        assert channel_id == CHANNEL
        return channel

    client.fetch_channel.side_effect = fetch
    return client, channel, store or MemoryStore()


def run(client, store, card=None, view_factory=None):
    return asyncio.run(sync_welcome(client, GUILD, CHANNEL, card or render(),
                                    view_factory=view_factory, store=store))


def test_create_once_skip_unchanged_and_edit_changed_with_view():
    client, channel, store = environment()
    view = discord.ui.View(timeout=None)
    factory = Mock(return_value=view)
    first = run(client, store, view_factory=factory)
    assert first == {"status": "created", "message_id": "101"}
    assert store.lease.saves[0] == {"creation_pending": True}
    assert run(client, store, view_factory=factory)["status"] == "unchanged"
    assert run(client, store, render("Updated"), factory)["status"] == "updated"
    assert channel.send_count == 1 and channel.messages[0].edit_count == 1
    assert factory.call_count == 2
    assert store.lease.state.content_hash == "Updated"
    assert channel.messages[0].embeds[0].footer.text == "DataSentry"
    assert store.lease.state.nonce not in str(channel.messages[0].embeds[0].to_dict())


def test_uncertain_post_reconciles_without_second_post():
    client, channel, store = environment()
    channel.send_error = TimeoutError("secret token must not escape")
    result = run(client, store)
    assert result["message_id"] == "101"
    assert channel.send_count == 1 and channel.history_limit == 100
    assert run(client, store)["status"] == "unchanged"


def test_changed_hash_refreshes_view_even_when_embed_text_is_identical():
    client, channel, store = environment()
    run(client, store)
    changed_buttons = replace(render(), content_hash="same-text-new-buttons")
    assert run(client, store, changed_buttons, lambda _: discord.ui.View(timeout=None))["status"] == "updated"
    assert channel.send_count == 1 and channel.messages[0].edit_count == 1


def test_pending_without_history_match_never_reposts_even_when_render_changes():
    client, channel, store = environment()
    store.lease.save(creation_pending=True)
    with pytest.raises(WelcomeUncertain, match="no repeat POST"):
        run(client, store, render("Changed"))
    assert channel.send_count == 0
    assert store.lease.state.creation_pending


def test_pending_reconcile_rejects_duplicate_bot_markers():
    client, channel, store = environment()
    state = store.lease.save(creation_pending=True)
    for message_id in (11, 12):
        channel.messages.append(FakeMessage(channel, message_id,
                                            [discord.Embed.from_dict(render().payload["embeds"][0])]))
    with pytest.raises(WelcomeUncertain):
        run(client, store)
    assert channel.send_count == 0


def test_verified_404_replaces_with_next_generation_only():
    client, channel, store = environment()
    first = run(client, store)
    channel.messages.clear()
    second = run(client, store)
    assert first["message_id"] != second["message_id"]
    assert store.lease.state.generation == 1
    assert channel.send_count == 2


def test_forbidden_lookup_never_replaces():
    client, channel, store = environment()
    run(client, store)
    channel.lookup_error = discord.Forbidden(Mock(status=403, reason="Forbidden"), "secret")
    with pytest.raises(WelcomeUncertain, match="lookup failed") as error:
        run(client, store)
    assert "secret" not in str(error.value)
    assert channel.send_count == 1 and store.lease.state.generation == 0


@pytest.mark.parametrize("field,value", [("type", discord.ChannelType.public_thread),
                                          ("id", CHANNEL + 1), ("guild", SimpleNamespace(id=GUILD + 1))])
def test_foreign_or_non_text_channel_never_posts(field, value):
    client, channel, store = environment()
    setattr(channel, field, value)
    with pytest.raises(PermissionError):
        run(client, store)
    assert channel.send_count == 0


def test_foreign_message_author_blocks_edit_and_replacement():
    client, channel, store = environment()
    run(client, store)
    channel.messages[0].author.id = BOT + 1
    with pytest.raises(PermissionError):
        run(client, store, render("Changed"))
    assert channel.send_count == 1 and channel.messages[0].edit_count == 0


def test_post_exception_without_visible_message_remains_pending():
    client, channel, store = environment()

    async def lost_post(**kwargs):
        channel.send_count += 1
        raise TimeoutError("private transport detail")

    channel.send = lost_post
    with pytest.raises(WelcomeUncertain, match="no repeat POST") as error:
        run(client, store)
    assert "private transport detail" not in str(error.value)
    assert store.lease.state.creation_pending
    with pytest.raises(WelcomeUncertain):
        run(client, store)
    assert channel.send_count == 1


def test_cancellation_waits_for_owned_send_and_reconnect_skips_post():
    client, channel, store = environment()
    original_send = channel.send

    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def delayed_send(**kwargs):
            entered.set()
            await release.wait()
            return await original_send(**kwargs)

        channel.send = delayed_send
        task = asyncio.create_task(sync_welcome(client, GUILD, CHANNEL, render(), store=store))
        await asyncio.wait_for(entered.wait(), 3)
        assert store.lease.state.creation_pending
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        return await sync_welcome(client, GUILD, CHANNEL, render(), store=store)

    assert asyncio.run(scenario())["status"] == "unchanged"
    assert channel.send_count == 1


def test_mock_postgres_intent_commit_and_advisory_contention():
    cursor = Mock()
    cursor.__enter__ = Mock(return_value=cursor)
    cursor.__exit__ = Mock(return_value=False)
    cursor.rowcount = 1
    connection = Mock()
    connection.cursor.return_value = cursor
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)
    state = WelcomeState(GUILD, CHANNEL, nonce=_nonce(GUILD, CHANNEL, 0))
    cursor.fetchone.side_effect = [(True,), tuple(getattr(state, name) for name in state.__dataclass_fields__)]
    store = WelcomeStore(connection_factory=lambda: connection)
    with store.locked(GUILD, CHANNEL) as lease:
        assert connection.commit.call_count == 1
        lease.save(creation_pending=True)
        assert connection.commit.call_count == 2
    sql = [item.args[0] for item in cursor.execute.call_args_list]
    assert "pg_try_advisory_lock" in sql[0]
    assert "pg_advisory_unlock" in sql[-1]
    assert "ON CONFLICT" in sql[1]
    cursor.fetchone.side_effect = None
    cursor.fetchone.return_value = (False,)
    with pytest.raises(WelcomeBusy):
        with store.locked(GUILD, CHANNEL):
            pytest.fail("contender entered")


def test_lost_metadata_checkpoint_blocks_external_write():
    cursor = Mock()
    cursor.__enter__ = Mock(return_value=cursor)
    cursor.__exit__ = Mock(return_value=False)
    cursor.rowcount = 0
    connection = Mock()
    connection.cursor.return_value = cursor
    lease = WelcomeLease(connection, WelcomeState(GUILD, CHANNEL))
    with pytest.raises(RuntimeError, match="not persisted"):
        lease.save(creation_pending=True)
    connection.rollback.assert_called_once()


def test_migration_is_bounded_metadata_only():
    sql = (Path(__file__).resolve().parents[1] /
           "infra/init/postgres/08_discord_service_messages.sql").read_text()
    assert "PRIMARY KEY (guild_id, channel_id, kind)" in sql
    assert "creation_pending BOOLEAN" in sql and "generation INTEGER" in sql
    assert "content_hash TEXT" in sql and "nonce TEXT" in sql
    assert "DELETE " not in sql and "DROP TABLE" not in sql
    assert _lock_key(GUILD, CHANNEL) == _lock_key(GUILD, CHANNEL)


@pytest.mark.skipif(not os.getenv("DQ_CONTROL_DATABASE_URL"), reason="No scoped control DB")
def test_real_postgres_welcome_fixture_only():
    store = WelcomeStore()
    rng = SystemRandom()
    guild_id, channel_id = rng.randrange(10**16, 10**17), rng.randrange(10**16, 10**17)
    with store.connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass('dq_control.discord_service_messages')")
        if cursor.fetchone()[0] is None:
            pytest.skip("Welcome migration not installed; do not apply from this test")
    try:
        with store.locked(guild_id, channel_id) as lease:
            assert lease.state.kind == "welcome"
            lease.save(creation_pending=True)
        with store.locked(guild_id, channel_id) as lease:
            assert lease.state.creation_pending and lease.state.generation == 0
            lease.replace_missing()
            assert lease.state.generation == 1 and not lease.state.creation_pending
    finally:
        with store.connection() as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM dq_control.discord_service_messages "
                           "WHERE guild_id = %s AND channel_id = %s AND kind = 'welcome'",
                           (guild_id, channel_id))
