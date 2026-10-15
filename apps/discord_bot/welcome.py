"""Single, durable bot-owned welcome card for the configured triage channel.

The database contains only delivery metadata. A committed creation intent is
never cleared merely because a bounded Discord history read finds no match.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import contextmanager
from dataclasses import dataclass, replace
from hashlib import sha256
import os
from typing import Any, Callable, Iterator

import discord

from apps.discord_bot.contracts import DiscordRenderResult


TABLE = "dq_control.discord_service_messages"
HISTORY_LIMIT = 100
DISCORD_TIMEOUT_SECONDS = 45


class WelcomeBusy(RuntimeError):
    """A different process owns this welcome synchronization."""


class WelcomeUncertain(RuntimeError):
    """The Discord write may have succeeded; another POST is prohibited."""


@dataclass(frozen=True)
class WelcomeState:
    guild_id: int
    channel_id: int
    kind: str = "welcome"
    message_id: str = ""
    content_hash: str = ""
    creation_pending: bool = False
    edit_pending: bool = False
    generation: int = 0
    nonce: str = ""


COLUMNS = tuple(WelcomeState.__dataclass_fields__)


def _nonce(guild_id: int, channel_id: int, generation: int) -> str:
    return sha256(f"welcome\0{guild_id}\0{channel_id}\0{generation}".encode()).hexdigest()[:24]


def _lock_key(guild_id: int, channel_id: int) -> int:
    return int.from_bytes(
        sha256(f"discord-welcome\0{guild_id}\0{channel_id}".encode()).digest()[:8],
        "big", signed=True,
    )


class WelcomeLease:
    def __init__(self, connection: Any, state: WelcomeState) -> None:
        self.connection = connection
        self.state = state

    def save(self, **changes: Any) -> WelcomeState:
        state = replace(self.state, **changes)
        mutable = COLUMNS[3:]
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {TABLE} SET " + ", ".join(f"{name} = %s" for name in mutable)
                + " , updated_at = now() WHERE guild_id = %s AND channel_id = %s AND kind = 'welcome'",
                tuple(getattr(state, name) for name in mutable) + (state.guild_id, state.channel_id),
            )
            if cursor.rowcount != 1:
                self.connection.rollback()
                raise RuntimeError("Welcome metadata checkpoint was not persisted.")
        self.connection.commit()
        self.state = state
        return state

    def replace_missing(self) -> WelcomeState:
        old = self.state
        return self.save(
            message_id="", content_hash="", creation_pending=False, edit_pending=False,
            generation=old.generation + 1,
            nonce=_nonce(old.guild_id, old.channel_id, old.generation + 1),
        )


class WelcomeStore:
    """Session-locked PostgreSQL welcome metadata; no ephemeral retry lease."""

    def __init__(self, connection_factory: Callable | None = None) -> None:
        self.connection_factory = connection_factory

    @contextmanager
    def connection(self) -> Iterator[Any]:
        if self.connection_factory is not None:
            with self.connection_factory() as connection:
                yield connection
            return
        import psycopg
        url = os.getenv("DQ_CONTROL_DATABASE_URL", "").strip()
        if not url:
            raise RuntimeError("Discord welcome requires the control database.")
        with psycopg.connect(url, connect_timeout=5) as connection:
            yield connection

    @contextmanager
    def locked(self, guild_id: int, channel_id: int) -> Iterator[WelcomeLease]:
        _validate_ids(guild_id, channel_id)
        key = _lock_key(guild_id, channel_id)
        with self.connection() as connection:
            acquired = False
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_try_advisory_lock(%s)", (key,))
                    acquired = bool(cursor.fetchone()[0])
                    if not acquired:
                        raise WelcomeBusy("Welcome synchronization is already in progress.")
                    initial = WelcomeState(guild_id, channel_id, nonce=_nonce(guild_id, channel_id, 0))
                    cursor.execute(
                        f"INSERT INTO {TABLE} ({', '.join(COLUMNS)}) "
                        f"VALUES ({', '.join(['%s'] * len(COLUMNS))}) "
                        "ON CONFLICT (guild_id, channel_id, kind) DO NOTHING",
                        tuple(getattr(initial, name) for name in COLUMNS),
                    )
                    cursor.execute(
                        f"SELECT {', '.join(COLUMNS)} FROM {TABLE} "
                        "WHERE guild_id = %s AND channel_id = %s AND kind = 'welcome'",
                        (guild_id, channel_id),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise RuntimeError("Welcome metadata unavailable.")
                    state = WelcomeState(**dict(zip(COLUMNS, row)))
                    if state.nonce != _nonce(guild_id, channel_id, state.generation):
                        raise RuntimeError("Welcome metadata identity is inconsistent.")
                connection.commit()
                yield WelcomeLease(connection, state)
            finally:
                connection.rollback()
                if acquired:
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT pg_advisory_unlock(%s)", (key,))
                    connection.commit()


def _validate_ids(guild_id: int, channel_id: int) -> None:
    if not all(isinstance(value, int) and not isinstance(value, bool)
               and 0 < value < 2**63 for value in (guild_id, channel_id)):
        raise ValueError("A configured numeric guild and triage channel are required.")


def _embeds(render: DiscordRenderResult, state: WelcomeState) -> list[discord.Embed]:
    raw = render.payload.get("embeds")
    if not isinstance(raw, list) or len(raw) != 1 or not isinstance(raw[0], dict):
        raise ValueError("Welcome requires exactly one rendered embed.")
    embed = discord.Embed.from_dict(raw[0])
    footer = str((raw[0].get("footer") or {}).get("text") or "")
    if len(footer) > 2048:
        raise ValueError("Welcome footer exceeds the Discord limit.")
    if len(embed) > 6000 or len(str(render.payload.get("content") or "")) > 2000:
        raise ValueError("Welcome card exceeds the Discord message limit.")
    return [embed]


def _message_owned(message: Any, client: Any, state: WelcomeState) -> bool:
    return (getattr(getattr(message, "author", None), "id", None) == client.user.id
            and getattr(getattr(message, "channel", None), "id", None) == state.channel_id
            and getattr(message, "webhook_id", None) is None)


def _subset(expected: Any, actual: Any) -> bool:
    if isinstance(expected, dict) and isinstance(actual, dict):
        return all(key in actual and _subset(value, actual[key]) for key, value in expected.items())
    if isinstance(expected, list) and isinstance(actual, list):
        return len(expected) == len(actual) and all(_subset(a, b) for a, b in zip(expected, actual))
    return expected == actual


def _message_matches(message: Any, render: DiscordRenderResult, state: WelcomeState) -> bool:
    expected = _embeds(render, state)[0].to_dict()
    embeds = getattr(message, "embeds", ())
    return (len(embeds) == 1
            and str(getattr(message, "content", "") or "") == str(render.payload.get("content") or "")
            and _subset(expected, embeds[0].to_dict()))


async def _channel(client: Any, guild_id: int, channel_id: int) -> Any:
    try:
        channel = await client.fetch_channel(channel_id)
    except Exception:
        raise WelcomeUncertain("Welcome target lookup failed; no write was attempted.") from None
    if (getattr(channel, "id", None) != channel_id
            or getattr(channel, "type", None) != discord.ChannelType.text
            or getattr(getattr(channel, "guild", None), "id", None) != guild_id):
        raise PermissionError("Welcome target is not the configured guild text channel.")
    if getattr(getattr(client, "user", None), "id", None) is None:
        raise PermissionError("Welcome bot identity is unavailable.")
    return channel


async def _reconcile(channel: Any, client: Any, state: WelcomeState, render: DiscordRenderResult) -> Any:
    matches = []
    async for message in channel.history(limit=HISTORY_LIMIT):
        if (_message_owned(message, client, state)
                and (str(getattr(message, "nonce", "")) == state.nonce or _message_matches(message, render, state))):
            matches.append(message)
            if len(matches) > 1:
                break
    if len(matches) != 1:
        raise WelcomeUncertain("Welcome creation is uncertain; no repeat POST was attempted.")
    return matches[0]


async def _send(channel: Any, render: DiscordRenderResult, state: WelcomeState,
                view_factory: Callable | None) -> Any:
    view = view_factory(render) if view_factory is not None else None
    return await channel.send(
        content=str(render.payload.get("content") or "") or None,
        embeds=_embeds(render, state), view=view,
        nonce=state.nonce, allowed_mentions=discord.AllowedMentions.none(),
    )


async def _edit(message: Any, render: DiscordRenderResult, state: WelcomeState,
                view_factory: Callable | None) -> Any:
    view = view_factory(render) if view_factory is not None else None
    return await message.edit(
        content=str(render.payload.get("content") or "") or None,
        embeds=_embeds(render, state), view=view,
        allowed_mentions=discord.AllowedMentions.none(),
    )


def _run_sync(loop: asyncio.AbstractEventLoop, client: Any, guild_id: int, channel_id: int,
              render: DiscordRenderResult, view_factory: Callable | None, store: WelcomeStore) -> dict:
    def call(coro: Any) -> Any:
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(timeout=DISCORD_TIMEOUT_SECONDS)
        except FutureTimeout:
            future.cancel()
            raise WelcomeUncertain("Welcome Discord request timed out; outcome is uncertain.") from None

    with store.locked(guild_id, channel_id) as lease:
        state = lease.state
        channel = call(_channel(client, guild_id, channel_id))
        if state.message_id:
            try:
                message = call(channel.fetch_message(int(state.message_id)))
            except discord.NotFound:
                state = lease.replace_missing()
            except Exception:
                raise WelcomeUncertain("Welcome lookup failed; no replacement was posted.") from None
            else:
                if not _message_owned(message, client, state):
                    raise PermissionError("Stored welcome message identity does not match the bot/channel.")
        if not state.message_id:
            if state.creation_pending:
                try:
                    message = call(_reconcile(channel, client, state, render))
                except WelcomeUncertain:
                    raise
                except Exception:
                    raise WelcomeUncertain("Welcome reconciliation failed; no repeat POST was attempted.") from None
            else:
                state = lease.save(creation_pending=True)
                try:
                    message = call(_send(channel, render, state, view_factory))
                except Exception:
                    try:
                        message = call(_reconcile(channel, client, state, render))
                    except Exception:
                        raise WelcomeUncertain("Welcome creation is uncertain; no repeat POST was attempted.") from None
            if not _message_owned(message, client, state):
                raise WelcomeUncertain("Welcome response identity is uncertain; no repeat POST was attempted.")
            state = lease.save(message_id=str(message.id), creation_pending=False)
            if _message_matches(message, render, state):
                state = lease.save(content_hash=render.content_hash, edit_pending=False)
                return {"status": "created", "message_id": state.message_id}

        if (state.content_hash == render.content_hash and not state.edit_pending
                and _message_matches(message, render, state)):
            return {"status": "unchanged", "message_id": state.message_id}

        state = lease.save(edit_pending=True)
        try:
            edited = call(_edit(message, render, state, view_factory))
        except discord.NotFound:
            # A 404 on PATCH is not enough to authorize a replacement POST.
            raise WelcomeUncertain("Welcome edit target disappeared; retry to verify 404 by fetch.") from None
        except Exception:
            raise WelcomeUncertain("Welcome edit outcome is uncertain; no replacement was posted.") from None
        if (not _message_owned(edited, client, state)
                or not _message_matches(edited, render, state)):
            raise WelcomeUncertain("Welcome edit response identity is uncertain.")
        state = lease.save(content_hash=render.content_hash, edit_pending=False)
        return {"status": "updated", "message_id": state.message_id}


async def sync_welcome(client: Any, guild_id: int, channel_id: int,
                       render: DiscordRenderResult, *, view_factory: Callable | None = None,
                       store: WelcomeStore | None = None) -> dict:
    """Create once, update changed cards, and fail closed on uncertain POSTs."""
    _validate_ids(guild_id, channel_id)
    if not isinstance(render, DiscordRenderResult) or not render.content_hash:
        raise ValueError("Welcome requires a deterministic rendered card.")
    _embeds(render, WelcomeState(guild_id, channel_id, nonce=_nonce(guild_id, channel_id, 0)))
    loop = asyncio.get_running_loop()
    worker = asyncio.create_task(asyncio.to_thread(
        _run_sync, loop, client, guild_id, channel_id, render, view_factory,
        store if store is not None else WelcomeStore(),
    ))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(worker)
            break
        except asyncio.CancelledError:
            cancelled = True
            asyncio.current_task().uncancel()
            if worker.done():
                break
    if cancelled:
        if worker.done():
            worker.exception() if not worker.cancelled() else None
        raise asyncio.CancelledError
    return result
