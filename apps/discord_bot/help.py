"""Deterministic, scoped Discord onboarding without model or mutation tools."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from hashlib import sha256
import json
import os
import re
import time
from typing import Any, Literal
from urllib.parse import urlparse

import discord
from discord import app_commands

from apps.common.control_plane import ControlPlaneResponseError, ControlPlaneTransportError
from apps.common.i18n import language_context, translate as tr
from apps.discord_bot.contracts import DiscordActionRow, DiscordActionSpec, DiscordRenderResult
from apps.discord_bot.formatters import DISCORD_COLORS, _embed_result, render_alert_list_embed, serialize_action_rows
from apps.discord_bot.mention_store import MentionRequestStore
from apps.discord_bot.service import fetch_discord_alerts
from apps.discord_bot.thread_store import IncidentThreadStore
from apps.discord_bot.workspace import operator_language
from pipelines.common.logging import logger

HelpTopic = Literal["start", "commands", "buttons", "safety"]
TOPICS = ("start", "commands", "buttons", "safety")
PATTERN = re.compile(r"^dqhelp:v1:(?P<topic>start|commands|buttons|safety|incidents):(?P<page>[0-2])(?::(?P<nav>back|next))?$")
FOOTER = "System guide · No AI call"
CATEGORIES = (
    ("Getting Started", ("alerts", "triage", "ask", "language", "help")),
    ("Reports & Summaries", ("daily_summary", "weekly_summary", "table_summary", "db_summary")),
    ("Operations & Debug", ("backfill_preview", "approve", "reject", "debug")),
)
PURPOSES = {
    "alerts": "Find incidents that need attention.",
    "triage": "Open or start an investigation for one Alert Ref.",
    "ask": "Ask a question about evidence or an incident.",
    "language": "Choose English or Indonesia for your answers.",
    "help": "Open this guide. It does not call an AI model.",
    "daily_summary": "Review one business date. Use the date shown on an incident.",
    "weekly_summary": "Review seven days ending on your chosen date.",
    "table_summary": "Review one table over a date range.",
    "db_summary": "Review warehouse quality over a date range.",
    "backfill_preview": "Create a backfill plan for review. Nothing runs yet.",
    "approve": "Open approval controls. Approval is not execution.",
    "reject": "Open rejection controls and provide a reason.",
    "debug": "Read private technical details for one incident.",
}
BUTTONS = (
    ("Investigation buttons", (
        ("Investigate", "Open the latest report. If no report exists, start the first investigation."),
        ("Run New Investigation", "Explicitly run a new investigation. This may use an LLM."),
        ("Evidence", "Read supporting facts, not the model's guess."),
        ("Ask AI / Ask Follow-up", "Ask a question grounded in the incident. This may use an LLM."),
        ("View Report", "Read saved report attachments. No new investigation runs."),
        ("Open Workspace", "Open Streamlit for more detail."),
        ("Draft Action", "Draft a plan for review. It does not repair data."),
    )),
    ("Approval & Resolve buttons", (
        ("View Plan", "Read the proposed scope, risk, validation and rollback plan."),
        ("Approve", "Accept a plan after confirmation. It does not automatically execute it."),
        ("Reject", "Reject a request and provide a reason."),
        ("Resolve", "Start checking whether this incident can close."),
        ("Check Again", "Run a scoped read-only check through Airflow. It does not repair data."),
        ("Check Status", "Read the progress of the same check."),
        ("Close Incident", "Write a closing reason after a fresh passing check."),
        ("Confirm Close", "Save the closure only if every safety check still passes."),
        ("Cancel", "Close the panel or cancel the available step. This is not a data rollback."),
    )),
    ("Recovery buttons", (
        ("Review Fix", "Review a saved-source recovery plan for one incident and date. No data changes yet."),
        ("Approve Plan", "Accept the exact source, scope, checks and rollback. Nothing runs yet."),
        ("Run Approved Fix", "After separate confirmation, run the approved recovery through Airflow. This changes only the approved partition."),
        ("Track Execution", "Read the existing execution and data-check result. It does not start another fix."),
        ("Resolve", "After recovery, run a fresh passing check and confirm closure. Recovery never closes the incident automatically."),
    )),
)


@dataclass(frozen=True)
class HelpCommand:
    name: str
    required: tuple[str, ...]
    optional: tuple[str, ...]


def command_catalog(tree: Any, guild: Any) -> dict[str, HelpCommand]:
    """Read actual registration rather than inventing commands or input fields."""
    result = {}
    for command in tree.walk_commands(guild=guild):
        if not command.qualified_name.startswith("dq ") or not hasattr(command, "parameters"):
            continue
        name = command.qualified_name[3:]
        if name in PURPOSES:
            parameters = command.parameters
            result[name] = HelpCommand(name, tuple(p.name for p in parameters if p.required),
                                       tuple(p.name for p in parameters if not p.required))
    return result


def _catalog(client: Any) -> dict[str, HelpCommand]:
    provider = getattr(client, "datasentry_help_catalog", None)
    return provider() if provider is not None else {}


def _action(topic: str, label: str, page: int = 0, primary: bool = False, nav: str = "") -> DiscordActionSpec:
    return DiscordActionSpec("help", tr(label), "primary" if primary else "secondary",
                             custom_id=f"dqhelp:v1:{topic}:{page}" + (f":{nav}" if nav else ""))


def help_rows(topic: str, page: int, *, welcome: bool = False) -> tuple[DiscordActionRow, ...]:
    actions = [
        _action("start" if welcome or topic != "start" else "incidents",
                "Get Started" if welcome or topic != "start" else "Show Incidents", primary=True),
        _action("commands", "Commands"), _action("buttons", "Button Guide"), _action("safety", "Safety & AI"),
    ]
    rows = [DiscordActionRow(tuple(actions))]
    pages = 3 if topic in {"commands", "buttons"} else 1
    extras = []
    if page > 0:
        extras.append(_action(topic, "Back", page - 1, nav="back"))
    if page + 1 < pages:
        extras.append(_action(topic, "Next", page + 1, nav="next"))
    url = os.getenv("DISCORD_OPERATOR_UI_URL", "").strip()
    parsed = urlparse(url)
    if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment:
        extras.append(DiscordActionSpec("workspace", tr("Open Workspace"), "link", url=url))
    if extras:
        rows.append(DiscordActionRow(tuple(extras)))
    return tuple(rows)


def render_help(topic: str = "start", page: int = 0, *, catalog: dict[str, HelpCommand] | None = None,
                welcome: bool = False) -> DiscordRenderResult:
    if topic not in TOPICS or page < 0 or page >= (3 if topic in {"commands", "buttons"} else 1):
        raise ValueError("Unknown help topic/page")
    fields = []
    if welcome:
        title = "\U0001F9ED " + tr("DataSentry · Start Here")
        description = tr("I help you understand data-quality problems and choose a safe next step.")
        fields = [(tr(a), tr(b), False) for a, b in (
            ("Find a problem", "Open Show Incidents to see what needs attention."),
            ("Understand it", "Pick an Alert Ref, then open Investigate. Use Evidence for facts or Ask AI for an explanation."),
            ("Take action carefully", "Approving a plan does not run it. Resolving an incident does not repair its data."),
        )]
    elif topic == "start":
        title = "\U0001F9ED " + tr("DataSentry Help")
        description = tr("New here? Start with one incident.")
        fields = [(tr("Get Started"), tr("1. Choose Show Incidents.\n2. Pick its Alert Ref: the short ID beginning with DQ-.\n3. Open Investigate to read the explanation.\n4. Use Evidence or Ask AI if something is unclear."), False),
                  ("Alert Ref", tr("You do not need the long system key."), False)]
    elif topic == "commands":
        category, names = CATEGORIES[page]
        title = tr("Commands") + " · " + tr(category)
        description = tr("Choose one command. Discord will show its input fields.")
        for name in names:
            entry = (catalog or {}).get(name)
            if entry is None:
                continue
            text = tr(PURPOSES[name])
            text += "\n" + (tr("Required") + ": " + ", ".join(f"`{p}`" for p in entry.required)
                            if entry.required else tr("No required inputs."))
            if entry.optional:
                text += "\n" + tr("Optional") + ": " + ", ".join(f"`{p}`" for p in entry.optional)
            fields.append(("/dq " + name, text, False))
        if not fields:
            fields.append((tr("Commands"), tr("Commands are not available in the local command catalog yet."), False))
        fields.append(("Alert Ref", tr("The alert_key field accepts the short Alert Ref shown on an incident card."), False))
    elif topic == "buttons":
        category, entries = BUTTONS[page]
        title = tr("Button Guide") + " · " + tr(category)
        description = tr("These are explanations, not buttons that perform incident actions.")
        fields = [(tr(name), tr(text), False) for name, text in entries]
    else:
        title = tr("Safety & AI")
        description = tr("Help and welcome never call an LLM. Ask AI and a new investigation may use the configured provider. Provider names appear only when that provider actually produced the analysis.")
        fields = [(tr(a), tr(b), False) for a, b in (
            ("AI and evidence", "AI explains evidence and recommends a next step. It can be wrong or lack enough evidence. Evidence shows what the platform observed; an AI answer is not proof that data is healthy."),
            ("Approval and execution", "Approve means the plan was accepted. Execution is a separate step. Only authorized operators can make approval decisions."),
            ("Closing an incident", "Resolve needs a fresh passing check, a reason and confirmation. The proof is valid for ten minutes before confirmation. Resolve does not repair data or guarantee permanent health."),
        )]
    url = os.getenv("DISCORD_OPERATOR_UI_URL", "").strip()
    if urlparse(url).hostname in {"localhost", "127.0.0.1", "::1"}:
        fields.append((tr("Local workspace link"), tr("This localhost link works only on the computer running Streamlit."), False))
    if not welcome:
        fields.append(("/help", tr("Use /dq help or /help to return to this guide. Existing short aliases still work."), False))
    return _embed_result(title=title, description=description, fields=fields,
                         color=0x00C978 if welcome else DISCORD_COLORS["info"],
                         narrative_source="deterministic_fallback", footer=tr(FOOTER),
                         fallback_markdown=description, action_rows=help_rows(topic, page, welcome=welcome))


def render_help_error(exc: BaseException, operation: str = "") -> DiscordRenderResult:
    original = exc.original if isinstance(exc, app_commands.CommandInvokeError) else exc
    cause = getattr(original, "__cause__", None)
    status = getattr(getattr(cause, "response", None), "status_code", None)
    text = str(original).lower()[:2000]
    title, detail = "I couldn't confirm the result.", "Check the existing request or incident before trying again. Do not repeat an approval or investigation whose result is unknown."
    if isinstance(original, (PermissionError, app_commands.CheckFailure)) or status in {401, 403}:
        title, detail = "This action needs operator access.", "Please contact the server owner. The guide does not grant extra permissions."
    elif status == 404 or isinstance(original, LookupError) or (isinstance(original, ValueError) and any(word in text for word in ("not found", "no alert", "unknown alert"))):
        title, detail = "I couldn't find that incident.", "That Alert Ref was not found. Choose Show Incidents to find its ID."
    elif isinstance(original, ValueError) and any(word in text for word in ("copilot returned", "triage returned", "authoritative report")):
        pass
    elif isinstance(original, ValueError) and not isinstance(original, ControlPlaneResponseError):
        title = "I couldn't use that input."
        detail = "Please use a date like 2026-10-14. Use the business date shown on the incident." if any(word in text for word in ("date", "yyyy-mm-dd", "time data", "isoformat")) else "Use the command picker or choose Commands to check the required inputs."
    elif isinstance(original, (ControlPlaneTransportError, ConnectionError, TimeoutError)) and operation in {"Alert lookup", "Daily summary", "Weekly summary", "Table summary", "Warehouse summary", "Debug lookup", "Help incidents"}:
        title, detail = "I couldn't reach the data service.", "The guide is still available. Contact an operator if the problem continues."
    first = _embed_result(title="\u26A0\uFE0F " + tr(title), description=tr(detail), fields=[],
                          color=DISCORD_COLORS["critical"], narrative_source="deterministic_fallback",
                          footer=tr("System feedback"), fallback_markdown=tr(detail))
    second = _embed_result(title=tr("Need a starting point?"),
                           description=tr("Choose Show Incidents, copy an Alert Ref, then use the matching command. Help does not approve, repair or close incidents."),
                           fields=[], color=DISCORD_COLORS["info"], narrative_source="deterministic_fallback",
                           footer=tr(FOOTER), fallback_markdown="", action_rows=help_rows("start", 0))
    payload = {**second.payload, "embeds": first.payload["embeds"] + second.payload["embeds"]}
    return DiscordRenderResult(payload=payload,
                               narrative_source="deterministic_fallback", content_hash=sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
                               fallback_markdown=first.fallback_markdown, action_rows=second.action_rows)


async def validate_help_scope(client: Any, guild_id: Any, channel: Any, user: Any,
                              *, source_message: Any = None, mention: bool = False) -> None:
    expected = int(os.getenv("DISCORD_GUILD_ID", "0") or "0")
    triage = int(os.getenv("DISCORD_TRIAGE_CHANNEL_ID", "0") or "0")
    allowed = {int(os.getenv(key, "0") or "0") for key in
               ("DISCORD_ALERTS_CHANNEL_ID", "DISCORD_TRIAGE_CHANNEL_ID", "DISCORD_OPS_CHANNEL_ID")} - {0}
    if not expected or guild_id != expected or user.bot or channel is None:
        raise PermissionError("Help is scoped to the configured Discord guild")
    if source_message is not None and getattr(getattr(source_message, "author", None), "id", None) != getattr(client.user, "id", None):
        raise PermissionError("Help buttons must belong to DataSentry")
    if channel.type in {discord.ChannelType.public_thread, discord.ChannelType.private_thread}:
        if channel.type != discord.ChannelType.public_thread or channel.parent_id != triage or channel.locked:
            raise PermissionError("Help requires an available registered public thread")
        canonical = await asyncio.to_thread(IncidentThreadStore().find_thread, str(expected), str(channel.id))
        if canonical is not None and (canonical.guild_id != str(expected) or canonical.channel_id != str(triage)):
            raise PermissionError("Thread registration does not match this guild/channel")
        if canonical is None:
            root = await asyncio.to_thread(MentionRequestStore().find_thread, str(channel.id))
            if root is None or root.guild_id != str(expected) or root.channel_id != str(triage):
                raise PermissionError("Thread is not registered to DataSentry")
    elif channel.type != discord.ChannelType.text or channel.id not in ({triage} if mention else allowed):
        raise PermissionError("Help is unavailable in this channel")


async def _audit(interaction: Any, topic: str, status: str, entry: str) -> None:
    from apps.discord_bot.interactions import write_interaction_audit
    from apps.discord_bot.contracts import DiscordInteractionContext, DiscordInteractionResult
    context = DiscordInteractionContext(action="help_" + entry, target_ref=topic if topic in {*TOPICS, "incidents"} else "start",
        actor=f"discord:{interaction.user.id}", guild_id=str(interaction.guild_id),
        channel_id=str(getattr(interaction.channel, "id", "")), message_id=str(getattr(getattr(interaction, "message", None), "id", "")))
    try:
        await asyncio.to_thread(write_interaction_audit, context,
                               DiscordInteractionResult(status=status, action=context.action, target_ref=context.target_ref, message=""))
    except Exception as exc:
        logger.warning("Help audit unavailable | error_type=%s", type(exc).__name__)


async def deliver_personal(interaction: Any, render: DiscordRenderResult, *, edit: bool = False) -> None:
    from apps.discord_bot.interactions import build_discord_view
    payload = {"content": None, "embeds": [discord.Embed.from_dict(e) for e in render.payload["embeds"]],
               "view": build_discord_view(render), "allowed_mentions": discord.AllowedMentions.none()}
    if interaction.response.is_done():
        await interaction.edit_original_response(**payload)
    elif edit:
        await interaction.response.edit_message(**payload)
    else:
        await interaction.response.send_message(**payload, ephemeral=True)


async def show_error(interaction: Any, operation: str, exc: BaseException) -> None:
    if not interaction.response.is_done():
        await interaction.response.defer(thinking=True, ephemeral=True)
    async with operator_language(interaction.guild_id, interaction.user.id):
        await deliver_personal(interaction, render_help_error(exc, operation))
    await _audit(interaction, "start", "failed", "error")


async def show_help(interaction: Any, topic: str = "start", page: int = 0,
                    *, button: bool = False) -> None:
    personal = button and bool(getattr(getattr(interaction.message, "flags", None), "ephemeral", False))
    if not interaction.response.is_done():
        await interaction.response.defer(thinking=not personal, ephemeral=True)
    try:
        await validate_help_scope(interaction.client, interaction.guild_id, interaction.channel, interaction.user,
                                  source_message=interaction.message if button else None)
        async with operator_language(interaction.guild_id, interaction.user.id):
            if topic == "incidents":
                payload, transport = await asyncio.to_thread(fetch_discord_alerts, "active", None, 5)
                render = render_alert_list_embed(alerts=list(payload.get("alerts", []))[:5], status="active", dt=None,
                                                assistant_note="", data_transport=transport)
                rows = help_rows("start", 0)
                embeds = [{**embed, "footer": {"text": tr(FOOTER)}} for embed in render.payload["embeds"]]
                result_payload = {**render.payload, "embeds": embeds, "components": serialize_action_rows(rows)}
                render = DiscordRenderResult(payload=result_payload, narrative_source=render.narrative_source,
                    content_hash=sha256(json.dumps(result_payload, sort_keys=True).encode()).hexdigest(),
                    fallback_markdown=render.fallback_markdown, action_rows=rows)
            else:
                render = render_help(topic, page, catalog=_catalog(interaction.client))
            await deliver_personal(interaction, render, edit=personal)
        await _audit(interaction, topic, "success", "button" if button else "command")
    except Exception as exc:
        logger.warning("Help delivery failed | error_type=%s http_status=%s discord_code=%s",
                       type(exc).__name__, getattr(exc, "status", None), getattr(exc, "code", None))
        await show_error(interaction, "Help incidents" if topic == "incidents" else "Help", exc)


class HelpActionButton(discord.ui.DynamicItem[discord.ui.Button], template=PATTERN.pattern):
    """Navigation only; no incident/action target and no mutation permission."""
    def __init__(self, topic: str, page: int = 0, nav: str = ""):
        pages = 3 if topic in {"commands", "buttons"} else 1
        custom_id = f"dqhelp:v1:{topic}:{page}" + (f":{nav}" if nav else "")
        if not PATTERN.fullmatch(custom_id) or not 0 <= page < pages:
            raise ValueError("Unknown help action")
        super().__init__(discord.ui.Button(label="Help", custom_id=custom_id))
        self.topic, self.page = topic, page

    @classmethod
    async def from_custom_id(cls, interaction: Any, item: Any, match: Any):
        return cls(match["topic"], int(match["page"]), match["nav"] or "")

    async def callback(self, interaction: discord.Interaction) -> None:
        await show_help(interaction, self.topic, self.page, button=True)


async def handle_help_mention(client: Any, message: Any) -> bool:
    """Intercept explicit help requests before admission to the AI question flow."""
    bot = client.user
    if (bot is None or message.guild is None or message.author.bot or message.webhook_id is not None
            or not any(user.id == bot.id for user in message.mentions)
            or not re.search(rf"<@!?{bot.id}>", message.content)):
        return False
    text = re.sub(rf"<@!?{bot.id}>", "", message.content).strip().lower()
    parts = text.split()
    if not parts or parts[0] not in {"help", "bantuan", "/help"} or (len(parts) > 2 and parts[0] != "/help"):
        return False
    if len(parts) == 2 and parts[1] in {"me", "us", "saya", "aku"} and parts[0] != "/help":
        return False
    topic = parts[1] if len(parts) == 2 and parts[1] in TOPICS else "invalid" if len(parts) > 1 else "start"
    try:
        await validate_help_scope(client, message.guild.id, message.channel, message.author, mention=True)
    except Exception as exc:
        logger.warning("Mention help scope rejected | error_type=%s", type(exc).__name__)
        return True
    seen = getattr(client, "datasentry_help_mentions", {})
    now = time.monotonic()
    if now - seen.get(message.author.id, float("-inf")) < 10:
        return True
    client.datasentry_help_mentions = {key: value for key, value in seen.items() if now - value < 10}
    client.datasentry_help_mentions[message.author.id] = now
    status = "success"
    try:
        async with operator_language(message.guild.id, message.author.id):
            try:
                render = render_help(topic, catalog=_catalog(client))
            except ValueError as exc:
                status = "rejected"
                render = render_help_error(exc)
            from apps.discord_bot.interactions import build_discord_view
            reply = await message.reply(embeds=[discord.Embed.from_dict(e) for e in render.payload["embeds"]],
                                        view=build_discord_view(render), mention_author=False,
                                        allowed_mentions=discord.AllowedMentions.none())
    except Exception as exc:
        logger.warning("Mention help delivery failed | error_type=%s", type(exc).__name__)
        return True
    from types import SimpleNamespace
    context = SimpleNamespace(user=message.author, guild_id=message.guild.id, channel=message.channel, message=reply)
    await _audit(context, topic, status, "mention")
    return True
