"""Registry concurrency, Discord recovery, and scoped PostgreSQL claim checks."""

import asyncio
import os
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import Event, Lock
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import discord
import pytest

from apps.discord_bot import workspace
from apps.discord_bot.thread_store import (
    COLUMNS, IncidentThreadStore, IncidentWorkspaceState, WorkspaceLease,
    WorkspaceBusy, WorkspaceUncertain, starter_nonce,
)


REF = "DQ-20261005-3B55A6"
KEY = "orders|dq_failure|2026-10-05|dq.raw_orders|volume|row_count"
FACTS = {"alert_key": KEY, "alert_ref": REF, "table_name": "dq.raw_orders"}


def test_new_thread_name_is_readable_bounded_and_does_not_use_model_or_locale():
    from apps.common.i18n import language_context
    with language_context("id"):
        title = workspace.incident_thread_name(REF, {"metric": "segment_coverage__country_channel"})
    assert title == f"{REF} | missing country or channel segment"
    assert len(title) <= 80
    assert "@everyone" not in workspace.incident_thread_name(REF, {"metric": "@everyone\n" * 50})
    assert "\n" not in workspace.incident_thread_name(REF, {"metric": "@everyone\n" * 50})
    assert len(workspace.incident_thread_name(REF, {"metric": "x" * 500})) <= 80


def not_found():
    return discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "missing")


def forbidden():
    return discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "denied")


class MemoryThreadStore:
    """Retain intent/attempt state across runtimes, with the same fail-closed policy."""

    def __init__(self):
        self.state = None
        self.guard = Lock()
        self.attempts = {}
        self.events = []
        self.writes = []

    @contextmanager
    def locked(self, guild_id, alert_key, alert_ref, channel_id):
        if not self.guard.acquire(blocking=False):
            raise WorkspaceBusy()
        try:
            if self.state is None:
                self.state = IncidentWorkspaceState(guild_id, alert_key, alert_ref, channel_id,
                    starter_nonce=starter_nonce(guild_id, alert_key))
            yield self
        finally:
            self.guard.release()

    def save(self, **changes):
        self.state = replace(self.state, **changes)
        self.writes.append(changes)
        return self.state

    def replace_missing(self):
        self.events.append(("missing_404_replacement", self.state.thread_id or self.state.starter_message_id))
        return self.save(generation=self.state.generation + 1,
            starter_nonce=starter_nonce(self.state.guild_id, self.state.alert_key, self.state.generation + 1),
            starter_message_id="", thread_id="", starter_attempted=False, thread_attempted=False)

    def find_thread(self, guild_id, thread_id):
        return self.state if self.state and self.state.guild_id == guild_id and self.state.thread_id == thread_id else None

    def claim_investigation(self, guild_id, alert_key, request_id, reinvestigate=False):
        with self.guard:
            if request_id in self.attempts or any(value in {"claimed", "uncertain"} for value in self.attempts.values()):
                return False
            if any(value != "cancelled_before_start" for value in self.attempts.values()) and not reinvestigate:
                return False
            self.attempts[request_id] = "claimed"
            return True

    def cancel_unstarted_investigation(self, guild_id, alert_key, request_id):
        if self.attempts.get(request_id) == "claimed":
            self.attempts[request_id] = "cancelled_before_start"

    def finish_investigation(self, guild_id, alert_key, request_id, *, completed, report_id=""):
        self.attempts[request_id] = "complete" if completed else "uncertain"


def environment():
    store = MemoryThreadStore()
    guild = SimpleNamespace(id=1)
    client = SimpleNamespace(user=SimpleNamespace(id=99))
    channel = SimpleNamespace(id=20, guild=guild, type=discord.ChannelType.text)
    thread = SimpleNamespace(id=30, guild=guild, type=discord.ChannelType.public_thread,
                             parent_id=20, archived=False, locked=False, edit=AsyncMock(), send=AsyncMock())
    messages = []
    created = []
    starter = SimpleNamespace(id=30, author=client.user, channel=channel, nonce="", embeds=[])
    async def send(**kwargs):
        assert store.state.starter_attempted
        starter.nonce = kwargs["nonce"]
        starter.embeds = kwargs["embeds"]
        messages.append(starter)
        return starter
    async def create(**kwargs):
        assert store.state.starter_message_id == "30" and store.state.thread_attempted
        created.append(thread)
        return thread
    async def fetch(identifier):
        if identifier == 20:
            assert store.guard.locked()
            return channel
        if created and identifier == thread.id:
            return thread
        raise not_found()
    async def history(**kwargs):
        assert kwargs == {"limit": 100}
        for message in messages:
            yield message
    channel.send = AsyncMock(side_effect=send)
    channel.history = history
    channel.fetch_message = AsyncMock(return_value=starter)
    starter.create_thread = AsyncMock(side_effect=create)
    client.fetch_channel = AsyncMock(side_effect=fetch)
    report = SimpleNamespace(alert=SimpleNamespace(alert_key=KEY), report_id="report-1")
    triage = Mock(return_value=(report, "api"))
    latest = Mock(return_value=None)
    authority = Mock(return_value=FACTS)
    runtime = workspace.IncidentWorkspaceRuntime(client, 1, 20, store=store, authority=authority,
                                                 latest_report=latest, triage=triage)
    return SimpleNamespace(**locals())


def test_create_reuse_and_reconnect_share_one_starter_and_public_thread():
    env = environment()
    first = asyncio.run(env.runtime.ensure(REF))
    fresh = workspace.IncidentWorkspaceRuntime(env.client, 1, 20, store=env.store, authority=env.authority)
    second = asyncio.run(fresh.ensure(KEY))
    assert first.thread is second.thread
    assert first.url == "https://discord.com/channels/1/30"
    env.channel.send.assert_awaited_once()
    env.starter.create_thread.assert_awaited_once()
    assert env.store.state.starter_message_id == env.store.state.thread_id == "30"
    assert not env.store.guard.locked()
    kwargs = env.channel.send.call_args.kwargs
    assert kwargs["nonce"] == starter_nonce("1", KEY)
    assert kwargs["allowed_mentions"].to_dict() == {"parse": []}
    assert "content" not in kwargs
    embed = kwargs["embeds"][0]
    assert embed.title == "Investigation Workspace" and KEY not in str(embed.to_dict())
    assert kwargs["nonce"] not in str(embed.to_dict())
    assert {field.name: field.value for field in embed.fields} == {"Alert Ref": REF, "Table": "dq.raw_orders"}


def test_concurrent_workspace_claim_precedes_http_and_prevents_duplicate_creation():
    env = environment()
    original = env.channel.send.side_effect
    async def slow(**kwargs):
        await asyncio.sleep(.025)
        return await original(**kwargs)
    env.channel.send.side_effect = slow
    async def run():
        return await asyncio.gather(env.runtime.ensure(REF), env.runtime.ensure(REF), return_exceptions=True)
    results = asyncio.run(run())
    assert len([result for result in results if isinstance(result, WorkspaceBusy)]) == 1
    env.channel.send.assert_awaited_once()
    env.starter.create_thread.assert_awaited_once()


@pytest.mark.parametrize("lost_response", ["starter", "thread"])
def test_lost_response_is_reconciled_without_repeating_creation(lost_response):
    env = environment()
    if lost_response == "starter":
        original = env.channel.send.side_effect
        async def send(**kwargs):
            await original(**kwargs)
            raise TimeoutError()
        env.channel.send.side_effect = send
    else:
        original = env.starter.create_thread.side_effect
        async def create(**kwargs):
            await original(**kwargs)
            raise TimeoutError()
        env.starter.create_thread.side_effect = create
    result = asyncio.run(env.runtime.ensure(REF))
    assert result.thread.id == 30
    env.channel.send.assert_awaited_once()
    env.starter.create_thread.assert_awaited_once()


def test_unknown_starter_after_timeout_is_not_blindly_recreated_on_reconnect():
    env = environment()
    env.channel.send.side_effect = TimeoutError()
    for _ in range(2):
        with pytest.raises(WorkspaceUncertain):
            asyncio.run(env.runtime.ensure(REF))
    assert env.store.state.starter_attempted and not env.store.state.starter_message_id
    env.channel.send.assert_awaited_once()
    env.starter.create_thread.assert_not_awaited()


def test_unknown_thread_after_timeout_is_not_blindly_recreated_on_reconnect():
    env = environment()
    env.starter.create_thread.side_effect = TimeoutError()
    with pytest.raises(discord.NotFound):
        asyncio.run(env.runtime.ensure(REF))
    with pytest.raises(WorkspaceUncertain):
        asyncio.run(env.runtime.ensure(REF))
    env.channel.send.assert_awaited_once()
    env.starter.create_thread.assert_awaited_once()
    assert env.store.state.starter_message_id == "30"


def test_missing_starter_does_not_hide_existing_thread_after_lost_checkpoint():
    env = environment()
    asyncio.run(env.runtime.ensure(REF))
    env.store.save(thread_id="")
    env.channel.fetch_message.side_effect = not_found()
    result = asyncio.run(env.runtime.ensure(REF))
    assert result.thread.id == 30 and env.store.state.thread_id == "30"
    env.channel.fetch_message.assert_not_awaited()
    env.channel.send.assert_awaited_once()
    assert not env.store.events


def test_missing_starter_and_thread_both_404_replace_partial_creation_with_audit():
    env = environment()
    asyncio.run(env.runtime.ensure(REF))
    env.store.save(thread_id="")
    env.created.clear()
    env.channel.fetch_message.side_effect = not_found()
    env.starter.id = env.thread.id = 31
    async def create(**kwargs):
        assert env.store.state.starter_message_id == "31"
        env.created.append(env.thread)
        return env.thread
    env.starter.create_thread.side_effect = create
    result = asyncio.run(env.runtime.ensure(REF))
    assert result.thread.id == 31 and result.state.generation == 1
    assert env.store.events == [("missing_404_replacement", "30")]
    env.channel.fetch_message.assert_awaited_once_with(30)
    assert env.channel.send.await_count == 2


def test_partial_creation_starter_forbidden_is_not_treated_as_missing_404():
    env = environment()
    asyncio.run(env.runtime.ensure(REF))
    env.store.save(thread_id="")
    env.created.clear()
    env.channel.fetch_message.side_effect = forbidden()
    with pytest.raises(discord.Forbidden):
        asyncio.run(env.runtime.ensure(REF))
    assert not env.store.events
    env.channel.send.assert_awaited_once()


def test_embed_identity_reconciles_without_nonce_but_ignores_user_text_and_other_incidents():
    env = environment()
    original = env.channel.send.side_effect
    async def send(**kwargs):
        message = await original(**kwargs)
        message.nonce = None
        env.messages.insert(0, SimpleNamespace(author=SimpleNamespace(id=5), embeds=message.embeds,
                                               content=f"Investigation Workspace {REF}", nonce=kwargs["nonce"]))
        raise TimeoutError()
    env.channel.send.side_effect = send
    assert asyncio.run(env.runtime.ensure(REF)).thread.id == 30
    env.channel.send.assert_awaited_once()


def test_ambiguous_embed_identity_never_selects_an_arbitrary_starter():
    env = environment()
    original = env.channel.send.side_effect
    async def send(**kwargs):
        message = await original(**kwargs)
        env.messages.append(replace_message(message, id=31))
        raise TimeoutError()
    env.channel.send.side_effect = send
    with pytest.raises(WorkspaceUncertain):
        asyncio.run(env.runtime.ensure(REF))
    env.starter.create_thread.assert_not_awaited()


def replace_message(message, **changes):
    return SimpleNamespace(**{**vars(message), **changes})


def test_authoritative_missing_404_replacement_is_audited_and_uses_new_nonce():
    env = environment()
    first = asyncio.run(env.runtime.ensure(REF))
    old_nonce = first.state.starter_nonce
    env.created.clear()
    env.starter.id = env.thread.id = 31
    # The test checkpoint assertions apply to the replacement's new starter id.
    async def create(**kwargs):
        assert env.store.state.starter_message_id == "31"
        env.created.append(env.thread)
        return env.thread
    env.starter.create_thread.side_effect = create
    result = asyncio.run(env.runtime.ensure(REF))
    assert result.thread.id == 31 and result.state.generation == 1
    assert env.store.events == [("missing_404_replacement", "30")]
    assert result.state.starter_nonce != old_nonce
    assert env.channel.send.await_count == 2


@pytest.mark.parametrize("failure", ["forbidden", "timeout", "locked", "private", "parent", "guild"])
def test_existing_thread_failures_never_replace_or_unlock(failure):
    env = environment()
    asyncio.run(env.runtime.ensure(REF))
    if failure in {"forbidden", "timeout"}:
        env.client.fetch_channel.side_effect = forbidden() if failure == "forbidden" else TimeoutError()
    if failure == "locked": env.thread.locked = True
    if failure == "private": env.thread.type = discord.ChannelType.private_thread
    if failure == "parent": env.thread.parent_id = 21
    if failure == "guild": env.thread.guild = SimpleNamespace(id=2)
    with pytest.raises((PermissionError, discord.Forbidden, TimeoutError)):
        asyncio.run(env.runtime.ensure(REF))
    assert not env.store.events
    env.channel.send.assert_awaited_once()
    env.thread.edit.assert_not_awaited()


def test_archived_unlocked_thread_is_reused_without_force_unlock():
    env = environment()
    asyncio.run(env.runtime.ensure(REF))
    env.thread.archived = True
    asyncio.run(env.runtime.ensure(REF))
    kwargs = env.thread.edit.call_args.kwargs
    assert kwargs["archived"] is False and "locked" not in kwargs
    env.channel.send.assert_awaited_once()


def test_registry_grounding_requires_no_prior_mention_and_blocks_different_reference():
    env = environment()
    asyncio.run(env.runtime.ensure(REF))
    state = asyncio.run(env.runtime.bind_thread(env.thread))
    assert state.alert_ref == REF
    with pytest.raises(PermissionError, match="open the other incident"):
        asyncio.run(env.runtime.bind_thread(env.thread, "DQ-20261006-AABBCC"))


def test_existing_report_is_reused_without_any_model_or_narrative_call():
    env = environment()
    incident = asyncio.run(env.runtime.ensure(REF))
    env.latest.return_value = env.report
    result = asyncio.run(env.runtime.investigate(incident, "request-1"))
    assert result == (env.report, "stored_report")
    env.triage.assert_not_called()
    assert not env.store.attempts


def test_first_investigation_and_explicit_rerun_have_durable_single_attempt_claims():
    env = environment()
    incident = asyncio.run(env.runtime.ensure(REF))
    asyncio.run(env.runtime.investigate(incident, "request-1"))
    for rerun in (False, True):
        with pytest.raises(WorkspaceUncertain):
            asyncio.run(env.runtime.investigate(incident, "request-1", reinvestigate=rerun))
    with pytest.raises(WorkspaceUncertain):
        asyncio.run(env.runtime.investigate(incident, "request-2"))
    asyncio.run(env.runtime.investigate(incident, "request-2", reinvestigate=True))
    assert env.triage.call_count == 2
    assert set(env.store.attempts.values()) == {"complete"}


def test_uncertain_provider_failure_blocks_reconnect_and_explicit_rerun():
    env = environment()
    incident = asyncio.run(env.runtime.ensure(REF))
    env.triage.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        asyncio.run(env.runtime.investigate(incident, "request-1"))
    for rerun in (False, True):
        with pytest.raises(WorkspaceUncertain):
            asyncio.run(env.runtime.investigate(incident, "request-2", reinvestigate=rerun))
    env.triage.assert_called_once()
    assert env.store.attempts == {"request-1": "uncertain"}


def test_concurrent_investigation_claims_do_not_repeat_provider():
    env = environment()
    async def run():
        incident = await env.runtime.ensure(REF)
        return await asyncio.gather(env.runtime.investigate(incident, "first"),
                                    env.runtime.investigate(incident, "second"), return_exceptions=True)
    results = asyncio.run(run())
    assert any(isinstance(result, WorkspaceUncertain) for result in results)
    env.triage.assert_called_once()


@pytest.mark.parametrize("repeat_cancel", [False, True])
def test_cancellation_during_claim_retires_only_unstarted_work(repeat_cancel):
    env = environment()
    entered, release = Event(), Event()
    original = env.store.claim_investigation

    def delayed_claim(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    env.store.claim_investigation = delayed_claim

    async def run():
        incident = await env.runtime.ensure(REF)
        task = asyncio.create_task(env.runtime.investigate(incident, "cancelled"))
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        if repeat_cancel:
            task.cancel()
            await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        env.triage.assert_not_called()
        assert env.store.attempts == {"cancelled": "cancelled_before_start"}
        env.store.claim_investigation = original
        with pytest.raises(WorkspaceUncertain):
            await env.runtime.investigate(incident, "cancelled", reinvestigate=True)
        await env.runtime.investigate(incident, "new-request")

    try:
        asyncio.run(run())
    finally:
        release.set()
    env.triage.assert_called_once()
    assert env.store.attempts["new-request"] == "complete"


def test_cancellation_after_triage_starts_remains_uncertain():
    env = environment()
    entered, release = Event(), Event()

    def delayed_triage(*args):
        entered.set()
        assert release.wait(5)
        return env.report, "api"

    env.triage.side_effect = delayed_triage

    async def run():
        incident = await env.runtime.ensure(REF)
        task = asyncio.create_task(env.runtime.investigate(incident, "started"))
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert env.store.attempts == {"started": "uncertain"}
        with pytest.raises(WorkspaceUncertain):
            await env.runtime.investigate(incident, "new-request", reinvestigate=True)
        release.set()

    try:
        asyncio.run(run())
    finally:
        release.set()
    env.triage.assert_called_once()


@pytest.fixture
def real_claim_case():
    """Use new synthetic control rows only; never touch Discord or warehouse data."""
    if not os.getenv("DQ_CONTROL_DATABASE_URL"):
        pytest.skip("Local PostgreSQL control store unavailable")
    store = IncidentThreadStore()
    key = "workspace_claim_fixture_" + uuid4().hex
    try:
        yield store, key
    finally:
        assert key.startswith("workspace_claim_fixture_")
        with store.connection() as conn, conn.cursor() as cursor:
            cursor.execute("DELETE FROM dq_control.discord_investigation_claims "
                           "WHERE guild_id = %s AND alert_key = %s", ("1", key))


@pytest.mark.parametrize("repeat_cancel", [False, True])
def test_real_postgres_cancellation_reconciles_claim_without_provider_execution(real_claim_case, repeat_cancel):
    store, key = real_claim_case
    env = environment()
    env.runtime.store = store
    incident = workspace.IncidentWorkspace(IncidentWorkspaceState("1", key, REF, "20"), env.thread)
    entered, release = Event(), Event()
    original = store.claim_investigation

    def delayed_claim(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    store.claim_investigation = delayed_claim

    async def run():
        task = asyncio.create_task(env.runtime.investigate(incident, "cancelled"))
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        if repeat_cancel:
            task.cancel()
            await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        asyncio.run(run())
    finally:
        release.set()
    env.triage.assert_not_called()
    with store.connection() as conn, conn.cursor() as cursor:
        cursor.execute("SELECT request_id,status FROM dq_control.discord_investigation_claims "
                       "WHERE guild_id=%s AND alert_key=%s", ("1", key))
        assert cursor.fetchall() == [("cancelled", "cancelled_before_start")]
    assert not original("1", key, "cancelled", True)
    assert original("1", key, "new-request")


@pytest.mark.parametrize("completed", [False, True])
def test_real_postgres_cancel_does_not_release_finished_or_uncertain_work(real_claim_case, completed):
    store, key = real_claim_case
    assert store.claim_investigation("1", key, "started")
    store.finish_investigation("1", key, "started", completed=completed, report_id="report" if completed else "")
    store.cancel_unstarted_investigation("1", key, "started")
    with store.connection() as conn, conn.cursor() as cursor:
        cursor.execute("SELECT status FROM dq_control.discord_investigation_claims "
                       "WHERE guild_id=%s AND alert_key=%s AND request_id=%s", ("1", key, "started"))
        assert cursor.fetchone()[0] == ("complete" if completed else "uncertain")
    assert not store.claim_investigation("1", key, "started", True)
    assert store.claim_investigation("1", key, "new-request", True) is completed


@pytest.mark.parametrize("where", ["read_failure", "latest_identity", "fresh_identity"])
def test_authoritative_read_failure_or_identity_mismatch_never_becomes_untriaged(where):
    env = environment()
    incident = asyncio.run(env.runtime.ensure(REF))
    wrong = SimpleNamespace(alert=SimpleNamespace(alert_key="another-incident"))
    if where == "read_failure": env.latest.side_effect = ValueError("artifact unavailable")
    if where == "latest_identity": env.latest.return_value = wrong
    if where == "fresh_identity": env.triage.return_value = (wrong, "api")
    with pytest.raises(ValueError):
        asyncio.run(env.runtime.investigate(incident, "request-1"))
    assert env.triage.call_count == (1 if where == "fresh_identity" else 0)


@pytest.mark.parametrize("item", [
    {"filename": "../report.md", "media_type": "text/markdown", "content": b"safe"},
    {"filename": "report.exe", "media_type": "text/markdown", "content": b"safe"},
    {"filename": "report.html", "media_type": "text/html", "content": b"safe"},
    {"filename": "report.md", "media_type": "text/markdown", "content": b"x" * 200_001},
    {"filename": "report.md", "media_type": "text/markdown", "content": Path("secret.md")},
])
def test_attachments_reject_unsafe_names_media_paths_and_oversize_content(item):
    with pytest.raises(ValueError):
        workspace.report_attachments([item])


def test_attachments_are_bounded_as_a_group_and_accept_guarded_service_metadata():
    item = {"filename": "report.md", "media_type": "text/markdown", "content": b"x" * 100_001,
            "s3_uri": "s3://dq-artifacts/agent-reports/example.md"}
    with pytest.raises(ValueError):
        workspace.report_attachments([item, {**item, "filename": "another.md"}])
    files = workspace.report_attachments([{**item, "content": b"safe report"}])
    assert files[0].filename == "report.md" and files[0].fp.read() == b"safe report"
    files[0].close()


def sql_environment():
    cursor = Mock()
    cursor.__enter__ = Mock(return_value=cursor)
    cursor.__exit__ = Mock(return_value=False)
    connection = Mock()
    connection.cursor.return_value = cursor
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)
    return connection, cursor


def test_postgres_lock_and_intent_are_committed_before_external_writes():
    conn, cursor = sql_environment()
    state = IncidentWorkspaceState("1", KEY, REF, "20", starter_nonce=starter_nonce("1", KEY))
    cursor.fetchone.side_effect = [(True,), tuple(vars(state).values())]
    store = IncidentThreadStore(connection_factory=lambda: conn)
    with store.locked("1", KEY, REF, "20") as lease:
        assert conn.commit.call_count == 1
        lease.save(starter_attempted=True)
        assert conn.commit.call_count == 2
    sql = [call.args[0] for call in cursor.execute.call_args_list]
    assert "pg_try_advisory_lock" in sql[0]
    assert "ON CONFLICT (guild_id, alert_key) DO NOTHING" in sql[1]
    assert "pg_advisory_unlock" in sql[-1]
    assert KEY not in " ".join(sql)


def test_postgres_contention_never_inserts_or_unlocks_another_owners_lock():
    conn, cursor = sql_environment()
    cursor.fetchone.return_value = (False,)
    with pytest.raises(WorkspaceBusy):
        with IncidentThreadStore(connection_factory=lambda: conn).locked("1", KEY, REF, "20"):
            pytest.fail("must not enter a competing workspace")
    assert cursor.execute.call_count == 1


def test_replacement_audit_and_generation_commit_are_atomic():
    conn, cursor = sql_environment()
    lease = WorkspaceLease(conn, IncidentWorkspaceState("1", KEY, REF, "20", thread_id="30"))
    lease.replace_missing()
    assert conn.commit.call_count == 1
    assert "missing_404_replacement" in cursor.execute.call_args_list[0].args[0]
    assert lease.state.generation == 1 and not lease.state.thread_id


@pytest.mark.parametrize("previous,rerun,expected", [
    ([], False, True), ([("first", "complete")], False, False),
    ([("first", "complete")], True, True), ([("first", "uncertain")], True, False),
    ([("first", "claimed")], True, False), ([("new", "complete")], True, False),
    ([("first", "cancelled_before_start")], False, True),
    ([("new", "cancelled_before_start")], True, False),
    ([("first", "cancelled_before_start"), ("second", "complete")], False, False),
])
def test_postgres_investigation_claim_policy(previous, rerun, expected):
    conn, cursor = sql_environment()
    cursor.fetchall.return_value = previous
    cursor.fetchone.return_value = ("new",)
    store = IncidentThreadStore(connection_factory=lambda: conn)
    assert store.claim_investigation("1", KEY, "new", rerun) is expected
    sql = [call.args[0] for call in cursor.execute.call_args_list]
    assert "pg_advisory_xact_lock" in sql[0]
    assert any("INSERT" in query for query in sql) is expected


def test_postgres_cancel_preserves_identity_and_never_retires_started_or_completed_work():
    conn, cursor = sql_environment()
    store = IncidentThreadStore(connection_factory=lambda: conn)
    store.cancel_unstarted_investigation("1", KEY, "cancelled")
    sql, params = cursor.execute.call_args.args
    assert "status = 'cancelled_before_start'" in sql
    assert "AND status = 'claimed'" in sql
    assert "DELETE" not in sql and KEY not in sql
    assert params == ("1", KEY, "cancelled")


def test_cancellation_migration_is_transactional_and_preserves_existing_claim_rows():
    sql = (Path(__file__).resolve().parents[1] /
           "infra/init/postgres/07_discord_investigation_cancellation.sql").read_text()
    assert "BEGIN;" in sql and "COMMIT;" in sql
    assert "DROP CONSTRAINT IF EXISTS discord_investigation_claims_status_check" in sql
    assert "'cancelled_before_start'" in sql
    assert "DELETE " not in sql and "DROP TABLE" not in sql and "UPDATE " not in sql


def test_migration_contains_durable_unique_keys_and_no_content_or_retry_lease():
    sql = (Path(__file__).resolve().parents[1] / "infra/init/postgres/04_discord_workspaces.sql").read_text()
    assert "PRIMARY KEY (guild_id, alert_key)" in sql
    assert "WHERE status IN ('claimed', 'uncertain')" in sql
    assert "PRIMARY KEY (guild_id, alert_key, request_id)" in sql
    assert "actor_id, alert_ref" in sql
    assert "question TEXT" not in sql and "answer TEXT" not in sql and "expires_at" not in sql
