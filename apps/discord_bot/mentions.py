"""Explicit bot mentions routed to the shared, read-only Copilot in public threads."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import re
import time
from typing import Any, Callable

import discord
from apps.common.i18n import tr

from agent.llm.conversation import bounded_conversation_context
from agent.tools.audit_log import write_agent_audit_event
from apps.discord_bot.formatters import render_operator_answer_embed
from apps.discord_bot.interactions import build_discord_view
from apps.discord_bot.mention_store import MentionRequestState, MentionRequestStore
from apps.discord_bot.service import answer_discord_question
from apps.discord_bot.workspace import get_incident_workspace, operator_language
from pipelines.common.clickhouse import build_clickhouse_client
from pipelines.common.logging import logger


PROCESSING = "\u23f3"
ANSWERED = "\u2705"
FAILED = "\u26a0\ufe0f"
ALERT_REF = re.compile(r"\bDQ-\d{8}-[A-F0-9]{6}\b", re.IGNORECASE)
THREAD_TYPES = {discord.ChannelType.public_thread, discord.ChannelType.private_thread,
                discord.ChannelType.news_thread}


def question_from_mention(content: str, bot_id: int) -> str:
    return re.sub(rf"<@!?{bot_id}>", "", content).strip()


def thread_title(question: str) -> str:
    clean = re.sub(r"<[^>]*>|[@#*_`\r\n]", " ", question)
    clean = " ".join(clean.split())
    return ("DataSentry: " + clean)[:80]


def audit_mention(state: MentionRequestState, event: str, result: dict | None = None) -> None:
    """Write identifiers and runtime proof only, never raw question/answer content."""
    try:
        runtime = dict((result or {}).get("llm_runtime") or {})
        allowed_runtime = {name: runtime.get(name) for name in (
            "provider", "model", "used_heuristic", "fallback_reason", "input_tokens",
            "output_tokens", "estimated_cost_usd", "duration_ms", "copilot_budget",
        ) if name in runtime}
        client = build_clickhouse_client()
        try:
            write_agent_audit_event(
                client, action=f"discord_mention_{event}", status=state.status,
                agent_run_id=state.agent_run_id or None, actor=f"discord:{state.actor_id}",
                tool_name="discord_mentions",
                input_payload={"source_message_id": state.source_message_id,
                               "guild_id": state.guild_id, "channel_id": state.channel_id,
                               "root_message_id": state.root_message_id},
                output_payload={"thread_id": state.thread_id, "answer_message_id": state.answer_message_id,
                                "alert_ref": state.alert_ref, "error_type": state.error_type,
                                "llm_runtime": allowed_runtime},
                error_message=state.error_type,
            )
        finally:
            client.close()
    except Exception as exc:
        logger.warning("Mention audit unavailable | error_type=%s", type(exc).__name__)


class MentionAskHandler:
    """One gateway-process admission boundary, backed by durable request claims."""

    def __init__(self, client: discord.Client, guild_id: int, triage_channel_id: int,
                 store: MentionRequestStore | None = None, answer: Callable | None = None,
                 audit: Callable | None = None, clock: Callable = time.monotonic,
                 workspace: Any = None) -> None:
        self.client = client
        self.guild_id = guild_id
        self.triage_channel_id = triage_channel_id
        self.store = store if store is not None else MentionRequestStore()
        self.answer = answer or answer_discord_question
        self.audit = audit or audit_mention
        self.clock = clock
        self.active_users: set[int] = set()
        self.active_threads: set[int] = set()
        self.last_request: dict[int, float] = {}
        self.active_count = 0
        self.request_timeout_seconds = 65
        self.workspace = workspace if workspace is not None else get_incident_workspace(client, guild_id, triage_channel_id)
        client.mention_ask_handler = self

    def admit(self, actor: int, thread_key: int) -> bool:
        """Share admission limits between explicit mentions and thread follow-up modals."""
        now = self.clock()
        if (self.active_count >= 3 or actor in self.active_users or thread_key in self.active_threads
                or now - self.last_request.get(actor, float('-inf')) < 10):
            return False
        self.active_count += 1
        self.active_users.add(actor)
        self.active_threads.add(thread_key)
        return True

    def release(self, actor: int, thread_key: int) -> None:
        self.active_count -= 1
        self.active_users.discard(actor)
        self.active_threads.discard(thread_key)

    def release_after_completion(self, task: asyncio.Task, actor: int, thread_key: int) -> None:
        # asyncio cancellation cannot stop an already-running synchronous HTTP call.
        try:
            task.result()
        except BaseException:
            pass
        self.release(actor, thread_key)

    async def feedback(self, message: Any, text: str) -> None:
        try:
            async with operator_language(self.guild_id, message.author.id):
                await message.reply(tr(text), mention_author=False, allowed_mentions=discord.AllowedMentions.none())
        except (discord.HTTPException, discord.Forbidden):
            logger.warning("Mention feedback could not be delivered | message_id=%s", message.id)

    async def reaction(self, message: Any, emoji: str, clear: bool = False) -> None:
        try:
            if clear:
                await message.remove_reaction(PROCESSING, self.client.user)
            await message.add_reaction(emoji)
        except (discord.HTTPException, discord.Forbidden):
            logger.warning("Mention reaction unavailable | message_id=%s", message.id)

    def permissions_available(self, message: Any, is_thread: bool) -> bool:
        member = message.guild.me
        permissions = message.channel.permissions_for(member)
        names = ("view_channel", "read_message_history", "embed_links", "add_reactions",
                 "send_messages_in_threads")
        if not is_thread:
            names += ("send_messages", "create_public_threads")
        return all(getattr(permissions, name, False) for name in names)

    async def conversation(self, channel: Any, history: list[MentionRequestState],
                           alert_ref: str, actor_id: str) -> list[dict[str, str]]:
        """Reverify visible turns; even a faulty store cannot leak another actor's context."""
        turns = []
        for entry in reversed(history[:2]):
            if (entry.alert_ref != alert_ref or entry.actor_id != actor_id
                    or entry.guild_id != str(self.guild_id)
                    or entry.channel_id != str(self.triage_channel_id)
                    or entry.thread_id != str(channel.id)):
                continue
            try:
                answer_message = await channel.fetch_message(int(entry.answer_message_id))
                if entry.source_kind not in {"modal", "command"}:
                    source_channel = channel.parent if entry.source_message_id == entry.root_message_id else channel
                    question_message = await source_channel.fetch_message(int(entry.source_message_id))
            except discord.HTTPException:
                continue
            if str(answer_message.author.id) != str(self.client.user.id):
                continue
            if entry.source_kind in {"modal", "command"}:
                question = next((field.value for embed in answer_message.embeds for field in embed.fields
                                 if field.name in {"Follow-up question", "Pertanyaan lanjutan"}), "")
            else:
                if question_message.author.bot or str(question_message.author.id) != entry.actor_id:
                    continue
                if not any(user.id == self.client.user.id for user in question_message.mentions):
                    continue
                question = question_from_mention(question_message.content, self.client.user.id)
            answer = "\n".join(embed.description or "" for embed in answer_message.embeds)
            if 3 <= len(question) <= 1000 and answer.strip():
                turns.append({"question": question, "answer": answer[:4000]})
        return bounded_conversation_context(turns)

    async def handle(self, message: Any) -> None:
        bot_user = self.client.user
        if (bot_user is None or message.author.bot or message.webhook_id is not None
                or message.guild is None or message.guild.id != self.guild_id):
            return
        if not any(user.id == bot_user.id for user in message.mentions):
            return
        if not re.search(rf"<@!?{bot_user.id}>", message.content):
            return
        is_thread = message.channel.type in THREAD_TYPES
        if (message.channel.parent_id if is_thread else message.channel.id) != self.triage_channel_id:
            return
        if is_thread and message.channel.type != discord.ChannelType.public_thread:
            return
        question = question_from_mention(message.content, bot_user.id)
        if not 3 <= len(question) <= 1000:
            await self.feedback(message, "Add a question of 3-1,000 characters after mentioning DataSentry.")
            return
        refs = {value.upper() for value in ALERT_REF.findall(question)}
        if len(refs) > 1:
            await self.feedback(message, "Use one Alert Ref per question to avoid mixing incident evidence.")
            return
        if not self.permissions_available(message, is_thread) or (is_thread and message.channel.locked):
            await self.feedback(message, "The required reaction/thread permissions are unavailable, or this thread is locked.")
            return
        actor = message.author.id
        thread_key = message.channel.id if is_thread else message.id
        now = self.clock()
        if not self.admit(actor, thread_key):
            await self.feedback(message, "A question is still running or the 10-second cooldown has not finished. Try again shortly.")
            return
        state = None
        claimed = False
        answer_task = None
        try:
            canonical = await self.workspace.bind_thread(message.channel, next(iter(refs), "")) if is_thread else None
            target_thread = message.channel if is_thread else None
            if not is_thread and refs:
                incident_workspace = await self.workspace.ensure(next(iter(refs)))
                canonical, target_thread = incident_workspace.state, incident_workspace.thread
                canonical_key = target_thread.id
                if canonical_key in self.active_threads:
                    await self.feedback(message, "A question is already running in this incident thread.")
                    return
                self.active_threads.discard(thread_key)
                self.active_threads.add(canonical_key)
                thread_key = canonical_key
            root = (await asyncio.to_thread(self.store.find_thread, str(thread_key))
                    if is_thread and canonical is None else None)
            if is_thread and canonical is None and root is None:
                await self.feedback(message, "Mention Ask is available only in registered DataSentry threads. Start a question in dq-triage.")
                return
            if root and (root.guild_id != str(self.guild_id) or root.channel_id != str(self.triage_channel_id)):
                return
            alert_ref = canonical.alert_ref if canonical else next(iter(refs), root.alert_ref if root else "")
            history = (await asyncio.to_thread(self.store.history, str(thread_key), str(actor), alert_ref,
                                              str(self.guild_id), str(self.triage_channel_id))
                       if is_thread or canonical else [])
            state = MentionRequestState(
                source_message_id=str(message.id), root_message_id=(str(message.id) if not is_thread else
                                                                  canonical.starter_message_id if canonical
                                                                  else root.root_message_id if root else str(message.id)),
                guild_id=str(message.guild.id), channel_id=str(self.triage_channel_id), actor_id=str(actor),
                thread_id=str(thread_key) if is_thread or canonical else "", alert_ref=alert_ref,
            )
            claimed = await asyncio.to_thread(self.store.claim, state)
            if not claimed:
                return
            self.last_request[actor] = now
            # Bound the cooldown map without retaining an unbounded user history.
            self.last_request = {key: value for key, value in self.last_request.items() if now - value < 10}
            await self.reaction(message, PROCESSING)
            await asyncio.to_thread(self.audit, state, "started")
            context = (await self.conversation(target_thread, history, alert_ref, str(actor))
                       if is_thread or canonical else [])
            async with operator_language(self.guild_id, actor):
                answer_task = asyncio.create_task(asyncio.to_thread(
                    self.answer, question, alert_ref, conversation_context=context, mention_mode=True,
                ))
                result = await asyncio.wait_for(asyncio.shield(answer_task), timeout=self.request_timeout_seconds)
            if not isinstance(result, dict) or not str(result.get("answer") or "").strip():
                raise ValueError("Invalid Copilot answer")
            returned_ref = str(result.get("alert_ref") or alert_ref)
            if alert_ref and returned_ref != alert_ref:
                raise ValueError("Copilot returned a different incident reference.")
            returned_key = str(result.get("alert_key") or "")
            if canonical and returned_key and returned_key not in {canonical.alert_key, canonical.alert_ref}:
                raise ValueError("Copilot returned a different authoritative incident key.")
            state = replace(state, agent_run_id=str(result.get("agent_run_id") or ""), alert_ref=returned_ref)
            async with operator_language(self.guild_id, actor):
                render = render_operator_answer_embed(
                    answer=result["answer"], alert_key=state.alert_ref or result.get("alert_key", ""),
                    transport=result.get("transport", ""), agent_run_id=state.agent_run_id,
                    llm_runtime=result.get("llm_runtime"),
                )
            if is_thread or canonical:
                thread = target_thread
                if canonical:
                    thread = await self.workspace.canonical_thread(thread.id)
            else:
                thread = message.thread
                if thread is None:
                    thread = await message.create_thread(name=thread_title(question), auto_archive_duration=60)
            state = replace(state, thread_id=str(thread.id))
            await asyncio.to_thread(self.store.save, state)
            kwargs = {"content": f"<@{actor}>",
                      "embeds": [discord.Embed.from_dict(embed) for embed in render.payload.get("embeds", [])],
                      "allowed_mentions": discord.AllowedMentions(users=[message.author], roles=False, everyone=False, replied_user=False)}
            if render.action_rows:
                kwargs["view"] = build_discord_view(render)
            answer_message = await thread.send(**kwargs)
            state = replace(state, answer_message_id=str(answer_message.id), status="answered")
            await asyncio.to_thread(self.store.save, state)
            await asyncio.to_thread(self.audit, state, "answered", {"llm_runtime": result.get("llm_runtime")})
            await self.reaction(message, ANSWERED, clear=True)
        except Exception as exc:
            error_type = type(exc).__name__
            logger.warning("Mention Ask failed safely | message_id=%s error_type=%s", message.id, error_type)
            if state is not None and claimed:
                state = replace(state, status="failed", error_type=error_type)
                try:
                    await asyncio.to_thread(self.store.save, state)
                except Exception:
                    logger.warning("Mention claim retained; no automatic replay | message_id=%s", message.id)
                await asyncio.to_thread(self.audit, state, "failed")
            await self.reaction(message, FAILED, clear=claimed)
            await self.feedback(message, "The question could not be completed. No remediation ran; review /dq debug before submitting a new question.")
        finally:
            if answer_task is not None and not answer_task.done():
                answer_task.add_done_callback(lambda task: self.release_after_completion(task, actor, thread_key))
            else:
                self.release(actor, thread_key)
