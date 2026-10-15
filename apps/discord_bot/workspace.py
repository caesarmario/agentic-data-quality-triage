"""Canonical public incident threads with durable write-intent checkpoints.

Parent entrypoints can use ``get_incident_workspace(client, guild, channel)`` and
``ensure(ref)``. Service fetches are read-only authority; only explicit investigation
may invoke triage. Unknown external outcomes are deliberately not retried.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from io import BytesIO
import re
from typing import Any, Callable, AsyncIterator

import discord

from agent.display import humanize_metric_name
from apps.common.i18n import language_context
from apps.discord_bot import service
from apps.discord_bot.formatters import _embed_result, DISCORD_COLORS, sanitize_discord_text
from apps.discord_bot.thread_store import IncidentThreadStore, WorkspaceUncertain
from pipelines.common.logging import logger


ALERT_REF = re.compile(r"DQ-\d{8}-[A-F0-9]{6}", re.IGNORECASE)
MAX_ATTACHMENT_BYTES = 200_000
MEDIA_TYPES = {"text/markdown": ".md", "text/plain": ".txt", "application/json": ".json"}


def incident_thread_name(reference: str, facts: dict) -> str:
    """Name new shared threads from deterministic issue metadata, never a model."""
    issue = humanize_metric_name(str(facts.get("metric") or "data_quality_issue"))
    title = re.sub(r"\s+", " ", sanitize_discord_text(issue, 120)).strip()
    return f"{reference} | {title}"[:80]


@asynccontextmanager
async def operator_language(guild_id: Any, user_id: Any) -> AsyncIterator[None]:
    """Use the parent's optional preference boundary; default to English safely."""
    language = "en"
    try:
        from apps.discord_bot.preferences import get_user_language
        value = await asyncio.to_thread(get_user_language, guild_id, user_id)
        if value in {"en", "id"}:
            language = value
    except Exception:
        pass
    with language_context(language):
        yield


def workspace_starter(state: Any, facts: dict) -> Any:
    """Use shared embed sanitation/provenance with stable English recovery identity."""
    with language_context("en"):
        return _embed_result(
            title="Investigation Workspace",
            description="Use this thread for investigation, evidence, reports, and follow-up questions. "
                        "Keeping one workspace per incident avoids repeated channel updates.",
            fields=[("Alert Ref", state.alert_ref, True), ("Table", str(facts.get("table_name") or "Unknown"), True)],
            color=DISCORD_COLORS["success"], narrative_source="deterministic_fallback",
            fallback_markdown=f"Investigation workspace for {state.alert_ref}",
            footer=f"Workspace revision {state.generation + 1} | No model call",
        )


def fetch_discord_latest_report(ref: str) -> Any:
    """Use the parent's authoritative fetch; absence never means 'untriaged'."""
    fetch = getattr(service, "fetch_discord_latest_report", None)
    if fetch is None:
        raise RuntimeError("Authoritative latest-report fetch is not integrated yet.")
    return fetch(ref)


def fetch_discord_report_files(ref: str) -> list[dict]:
    """Use only parent-guarded artifact reads, never fetch arbitrary artifact URLs."""
    fetch = getattr(service, "fetch_discord_report_files", None)
    if fetch is None:
        raise RuntimeError("Guarded report-file fetch is not integrated yet.")
    return fetch(ref)


def report_attachments(items: list[dict]) -> list[discord.File]:
    """Validate safe text artifacts with a 200 KB total cap before public upload."""
    if not isinstance(items, list) or len(items) > 3:
        raise ValueError("Report attachments must be a bounded list of text artifacts.")
    validated = []
    total = 0
    names = set()
    for item in items:
        filename = str(item.get("filename") or "")
        media_type = item.get("media_type")
        extension = MEDIA_TYPES.get(media_type)
        if (not extension or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", filename)
                or not filename.lower().endswith(extension) or ".." in filename or filename in names):
            raise ValueError("Report attachment filename/media type is not allowlisted.")
        content = item.get("content")
        if isinstance(content, str):
            content = content.encode("utf-8")
        if not isinstance(content, bytes):
            raise ValueError("Report attachments must contain bytes or text, not paths/URLs.")
        total += len(content)
        if total > MAX_ATTACHMENT_BYTES:
            raise ValueError("Report attachments exceed the 200 KB upload boundary.")
        names.add(filename)
        validated.append((filename, content))
    return [discord.File(BytesIO(content), filename=filename) for filename, content in validated]


@dataclass(frozen=True)
class IncidentWorkspace:
    """Authoritative incident correlation plus a verified usable public thread."""

    state: Any
    thread: Any

    @property
    def url(self) -> str:
        return f"https://discord.com/channels/{self.state.guild_id}/{self.thread.id}"


async def settle_cancelled_claim(task: asyncio.Task) -> Any:
    """Drain owned SQL work despite repeated cancellation of its caller."""
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise


class IncidentWorkspaceRuntime:
    """Serialize creation and recover only by reading persisted starter/thread identity."""

    def __init__(self, client: Any, guild_id: int, triage_channel_id: int,
                 store: IncidentThreadStore | None = None, authority: Callable | None = None,
                 latest_report: Callable | None = None, triage: Callable | None = None) -> None:
        self.client = client
        self.guild_id = guild_id
        self.triage_channel_id = triage_channel_id
        self.store = store if store is not None else IncidentThreadStore()
        self.authority = authority or service.fetch_discord_evidence_context
        self.latest_report = latest_report or fetch_discord_latest_report
        self.triage = triage or service.run_discord_triage

    def verify_thread(self, thread: Any) -> None:
        """Fail closed on foreign/private/locked threads; never force unlock."""
        if (thread.type != discord.ChannelType.public_thread
                or thread.parent_id != self.triage_channel_id
                or thread.guild.id != self.guild_id):
            raise PermissionError("Canonical incident thread is outside configured dq-triage.")
        if thread.locked:
            raise PermissionError("Canonical incident thread is locked; operator review is required.")

    async def usable(self, thread: Any) -> Any:
        self.verify_thread(thread)
        if thread.archived:
            await thread.edit(archived=False, reason="Reuse canonical DQ incident workspace")
        return thread

    async def canonical_thread(self, thread_id: Any) -> Any:
        """Fetch, not cache, so replacement is based only on an authoritative 404."""
        thread = await self.client.fetch_channel(int(thread_id))
        return await self.usable(thread)

    async def bind_thread(self, channel: Any, target_ref: str = "") -> Any:
        """Use the registry as authority; a different reference never switches context."""
        if getattr(channel, "type", None) != discord.ChannelType.public_thread:
            return None
        state = await asyncio.to_thread(self.store.find_thread, str(self.guild_id), str(channel.id))
        if state is None:
            return None
        self.verify_thread(channel)
        if state.channel_id != str(self.triage_channel_id):
            raise PermissionError("Incident registry has a different configured parent channel.")
        if target_ref and target_ref.upper() != state.alert_ref.upper():
            raise PermissionError(f"This workspace belongs to {state.alert_ref}; open the other incident separately.")
        return state

    async def reconcile_starter(self, channel: Any, state: Any, facts: dict) -> Any:
        """A timeout with no returned ID permits reads only, never another starter send."""
        expected = workspace_starter(state, facts).payload["embeds"][0]
        matches = []
        async for message in channel.history(limit=100):
            if message.author.id != self.client.user.id:
                continue
            if str(getattr(message, "nonce", "") or "") == state.starter_nonce:
                matches.append(message)
                continue
            for embed in message.embeds:
                if (embed.title == expected["title"] and embed.footer.text == expected["footer"]["text"]
                        and [(field.name, field.value) for field in embed.fields]
                        == [(field["name"], field["value"]) for field in expected["fields"]]):
                    matches.append(message)
                    break
        if len(matches) == 1:
            return matches[0]
        raise WorkspaceUncertain("Starter creation is uncertain; no duplicate message was sent.")

    async def ensure(self, ref: str) -> IncidentWorkspace:
        """Create/reuse one durable public thread for authoritative alert_key + guild."""
        if not self.guild_id or not self.triage_channel_id:
            raise PermissionError("The guild and dq-triage boundaries must be configured.")
        facts = await asyncio.to_thread(self.authority, ref)
        key, reference = str(facts.get("alert_key") or ""), str(facts.get("alert_ref") or "")
        if not key or not ALERT_REF.fullmatch(reference):
            raise ValueError("The incident authority did not return a canonical key and Alert Ref.")
        manager = self.store.locked(str(self.guild_id), key, reference, str(self.triage_channel_id))
        enter_task = asyncio.create_task(asyncio.to_thread(manager.__enter__))
        try:
            lease = await asyncio.shield(enter_task)
        except asyncio.CancelledError:
            # Cancelling a to_thread waiter cannot cancel the lock acquisition itself.
            try:
                await asyncio.shield(enter_task)
            except Exception:
                pass
            else:
                await asyncio.shield(asyncio.to_thread(manager.__exit__, None, None, None))
            raise
        try:
            state = lease.state
            if state.thread_id:
                try:
                    thread = await self.canonical_thread(state.thread_id)
                    return IncidentWorkspace(state, thread)
                except discord.NotFound:
                    state = await asyncio.to_thread(lease.replace_missing)
            channel = await self.client.fetch_channel(self.triage_channel_id)
            if channel.type != discord.ChannelType.text or channel.guild.id != self.guild_id:
                raise PermissionError("dq-triage must be a text channel in the configured guild.")
            starter = None
            if state.starter_message_id:
                # The thread can survive deletion of its starter or a lost registry checkpoint.
                try:
                    thread = await self.canonical_thread(state.starter_message_id)
                except discord.NotFound:
                    try:
                        starter = await channel.fetch_message(int(state.starter_message_id))
                    except discord.NotFound:
                        state = await asyncio.to_thread(lease.replace_missing)
                else:
                    state = await asyncio.to_thread(lease.save, thread_id=str(thread.id))
                    return IncidentWorkspace(state, thread)
            if starter is None and state.starter_attempted:
                starter = await self.reconcile_starter(channel, state, facts)
            elif starter is None:
                state = await asyncio.to_thread(lease.save, starter_attempted=True)
                render = workspace_starter(state, facts)
                try:
                    starter = await channel.send(embeds=[discord.Embed.from_dict(item) for item in render.payload["embeds"]],
                                                 nonce=state.starter_nonce,
                                                 allowed_mentions=discord.AllowedMentions.none())
                except (discord.HTTPException, TimeoutError, OSError):
                    starter = await self.reconcile_starter(channel, state, facts)
            if starter.author.id != self.client.user.id or starter.channel.id != self.triage_channel_id:
                raise PermissionError("The canonical starter author/channel could not be verified.")
            state = await asyncio.to_thread(lease.save, starter_message_id=str(starter.id))
            try:
                thread = await self.canonical_thread(starter.id)
            except discord.NotFound:
                if state.thread_attempted:
                    raise WorkspaceUncertain("Thread creation is uncertain; no duplicate thread was created.")
                state = await asyncio.to_thread(lease.save, thread_attempted=True)
                try:
                    thread = await starter.create_thread(name=incident_thread_name(reference, facts), auto_archive_duration=1440)
                    thread = await self.usable(thread)
                except (discord.HTTPException, TimeoutError, OSError):
                    thread = await self.canonical_thread(starter.id)
            state = await asyncio.to_thread(lease.save, thread_id=str(thread.id))
            logger.info("Canonical incident workspace ready | guild_id=%s thread_id=%s generation=%s",
                        state.guild_id, state.thread_id, state.generation)
            return IncidentWorkspace(state, thread)
        finally:
            await asyncio.shield(asyncio.to_thread(manager.__exit__, None, None, None))

    async def investigate(self, workspace: IncidentWorkspace, request_id: str,
                          *, reinvestigate: bool = False) -> tuple[Any, str]:
        """Reuse latest authoritative reports; only a durable fresh claim can run triage."""
        state = workspace.state
        latest = await asyncio.to_thread(self.latest_report, state.alert_ref)
        if latest is not None and not reinvestigate:
            if latest.alert.alert_key != state.alert_key:
                raise ValueError("The authoritative report belongs to a different incident.")
            return latest, "stored_report"
        claim_task = asyncio.create_task(asyncio.to_thread(
            self.store.claim_investigation, state.guild_id, state.alert_key, request_id, reinvestigate,
        ))
        try:
            claimed = await asyncio.shield(claim_task)
        except asyncio.CancelledError:
            # No triage was started: retain the request identity, but release its gate.
            try:
                if await settle_cancelled_claim(claim_task):
                    cleanup = asyncio.create_task(asyncio.to_thread(
                        self.store.cancel_unstarted_investigation,
                        state.guild_id, state.alert_key, request_id,
                    ))
                    await settle_cancelled_claim(cleanup)
            except Exception as exc:
                logger.warning("Unstarted investigation claim retained | error_type=%s", type(exc).__name__)
            raise
        if not claimed:
            raise WorkspaceUncertain("Investigation already claimed or uncertain; no model call was repeated.")
        try:
            report, transport = await asyncio.to_thread(self.triage, state.alert_ref)
            if report.alert.alert_key != state.alert_key:
                raise ValueError("Triage returned a report for a different incident.")
            await asyncio.to_thread(self.store.finish_investigation, state.guild_id, state.alert_key,
                                    request_id, completed=True, report_id=str(getattr(report, "report_id", "")))
            return report, transport
        except BaseException:
            try:
                await asyncio.shield(asyncio.to_thread(self.store.finish_investigation, state.guild_id,
                                     state.alert_key, request_id, completed=False))
            except Exception:
                logger.warning("Investigation claim retained; no automatic provider replay")
            raise


def get_incident_workspace(client: Any, guild_id: int, triage_channel_id: int) -> IncidentWorkspaceRuntime:
    """Share one runtime between persistent actions, parent commands, and mentions."""
    runtime = getattr(client, "incident_workspace_runtime", None)
    if runtime is None:
        runtime = IncidentWorkspaceRuntime(client, guild_id, triage_channel_id)
        client.incident_workspace_runtime = runtime
    if runtime.guild_id != guild_id or runtime.triage_channel_id != triage_channel_id:
        raise PermissionError("The incident workspace runtime has different guild/channel boundaries.")
    return runtime
