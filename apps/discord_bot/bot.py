####
## Discord Bot for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Discord slash-command adapter for the shared data reliability control plane."""

# --- Importing Libraries
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from dataclasses import replace
from functools import wraps
from pathlib import Path
from typing import Any, Sequence

import discord
from discord import app_commands


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Add the repository root when this module is executed directly by path.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.llm.copilot import (
    build_alert_list_copilot_note,
    build_daily_summary_copilot_note,
    build_quality_summary_copilot_note,
)
from apps.discord_bot.formatters import (
    render_alert_list_embed,
    render_approval_embed,
    render_daily_summary_embed,
    render_debug_embed,
    render_operator_answer_embed,
    render_quality_summary_embed,
    render_triage_embed,
    split_message,
)
from apps.discord_bot.contracts import DiscordRenderResult
from apps.discord_bot.interactions import (
    build_discord_view,
    register_persistent_interactions,
    validate_interaction_scope,
    get_incident_workspace,
    run_workspace_investigation,
    send_render_to_thread,
)
from apps.discord_bot.service import (
    DEFAULT_ALERT_LIMIT,
    DEFAULT_ALERT_STATUS,
    answer_discord_question,
    build_discord_triage_note,
    create_discord_backfill_approval_request,
    fetch_discord_daily_summary,
    fetch_discord_database_summary,
    fetch_discord_debug_context,
    fetch_discord_approval_request,
    fetch_discord_alerts,
    fetch_discord_table_summary,
    fetch_discord_weekly_summary,
    probe_control_plane_health,
    run_discord_triage,
)
from apps.discord_bot.delivery import synchronize_incident_message
from apps.discord_bot.mentions import MentionAskHandler
from apps.discord_bot.mention_store import MentionRequestState
from apps.discord_bot.workspace import get_incident_workspace as workspace_runtime
from apps.discord_bot.preferences import get_user_language, set_user_language
from apps.common.i18n import language_context, translate as tr
from apps.discord_bot.help import (
    command_catalog, handle_help_mention, render_help, show_error, show_help,
)
from agent.tools.audit_log import build_llm_route_audit_payload
from pipelines.common.logging import logger
from pipelines.seeding.helpers import parse_date


# --- Defining Constants
DEFAULT_TARGET_DAG_ID = "00_dag_dq_platform_daily_orchestrator"

GUILD_ID          = int(os.getenv("DISCORD_GUILD_ID", "0") or "0")
ALERTS_CHANNEL_ID = int(os.getenv("DISCORD_ALERTS_CHANNEL_ID", "0") or "0")
TRIAGE_CHANNEL_ID = int(os.getenv("DISCORD_TRIAGE_CHANNEL_ID", "0") or "0")
OPS_CHANNEL_ID    = int(os.getenv("DISCORD_OPS_CHANNEL_ID", "0") or "0")

REQUIRED_DISCORD_SETTINGS = (
    "DISCORD_BOT_TOKEN",
    "DISCORD_GUILD_ID",
    "DISCORD_ALERTS_CHANNEL_ID",
    "DISCORD_TRIAGE_CHANNEL_ID",
    "DISCORD_OPS_CHANNEL_ID",
)


# --- Creating Discord Client
intents        = discord.Intents.none()
intents.guilds = True
intents.guild_messages = True

client = discord.Client(intents=intents)
tree   = app_commands.CommandTree(client)
register_persistent_interactions(client)
mention_ask = MentionAskHandler(client, GUILD_ID, TRIAGE_CHANNEL_ID)


@client.event
async def on_message(message: discord.Message) -> None:
    """Accept explicit mentions without enabling privileged content access."""
    if await handle_help_mention(client, message):
        return
    await mention_ask.handle(message)


# --- Defining Configuration Helpers
def guild_object(guild_id: int | None = None) -> discord.Object | None:
    """
    Resolve the guild used for fast local slash-command registration.

    Args:
        guild_id: Optional explicit guild id. None uses DISCORD_GUILD_ID.

    Returns:
        Discord Object when a guild id exists, otherwise None for global commands.
    """
    resolved_id = GUILD_ID if guild_id is None else int(guild_id)

    return discord.Object(id=resolved_id) if resolved_id else None


def register_dq_command_group() -> app_commands.Group:
    """
    Register the canonical grouped command namespace for local Discord use.

    Returns:
        Mutable command group registered to the configured guild or globally.
    """
    group = app_commands.Group(
        name="dq",
        description="Data reliability alerts, triage, and approval-gated operations",
    )
    scope = guild_object()

    if scope:
        tree.add_command(group, guild=scope)
    else:
        tree.add_command(group)

    return group


# Register the group before child decorators are evaluated below.
dq_group = register_dq_command_group()
client.datasentry_help_catalog = lambda: command_catalog(tree, guild_object())


HELP_CHOICES = [app_commands.Choice(name=name, value=value) for name, value in (
    ("Quick Start", "start"), ("Commands", "commands"),
    ("Button Guide", "buttons"), ("Safety & AI", "safety"),
)]


@tree.command(name="help", description="Learn how to use DataSentry", guild=guild_object())
@app_commands.choices(topic=HELP_CHOICES)
async def help_command(interaction: discord.Interaction, topic: str = "start") -> None:
    await show_help(interaction, topic)


@dq_group.command(name="help", description="Learn how to use DataSentry")
@app_commands.choices(topic=HELP_CHOICES)
async def dq_help(interaction: discord.Interaction, topic: str = "start") -> None:
    await show_help(interaction, topic)


def localized_command(callback):
    """Resolve actor preference within the response deadline; isolate async locale."""
    @wraps(callback)
    async def wrapped(interaction, *args, **kwargs):
        language = "en"
        if getattr(interaction, "guild_id", None) and getattr(interaction, "user", None):
            try:
                language = await asyncio.wait_for(asyncio.to_thread(
                    get_user_language, interaction.guild_id, interaction.user.id), timeout=1.0)
            except asyncio.TimeoutError:
                logger.warning("Discord language lookup timed out; defaulting to English")
        with language_context(language):
            return await callback(interaction, *args, **kwargs)
    return wrapped


@dq_group.command(name="language", description="Choose your response language; shared incident cards stay English")
@app_commands.choices(language=[app_commands.Choice(name="English", value="en"), app_commands.Choice(name="Indonesia", value="id")])
async def dq_language(interaction: discord.Interaction, language: app_commands.Choice[str]) -> None:
    await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        validate_interaction_scope(interaction, allowed_channels={value for value in {ALERTS_CHANNEL_ID, TRIAGE_CHANNEL_ID, OPS_CHANNEL_ID} if value}, require_bot_authored_message=False)
        selected = await asyncio.to_thread(set_user_language, interaction.guild_id, interaction.user.id, language.value)
        text = "Your language is now English. Shared incident cards remain English." if selected == "en" else "Bahasa respons Anda sekarang Indonesia. Kartu incident bersama tetap menggunakan bahasa Inggris."
        await interaction.followup.send(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
    except Exception as exc:
        await command_error(interaction, "Language preference", exc)


def registered_command_names() -> list[str]:
    """
    List every invokable command path, including grouped and legacy aliases.

    Returns:
        Sorted slash-command paths without exposing configuration values.
    """
    names = []

    for command in tree.get_commands(guild=guild_object()):
        if isinstance(command, app_commands.Group):
            names.extend(f"{command.name} {child.name}" for child in command.commands)
        else:
            names.append(command.name)

    return sorted(names)


def required_env(
    name: str,
    environment: dict[str, str] | None = None,
) -> str:
    """
    Read one required bot environment setting.

    Args:
        name: Environment variable name.
        environment: Optional mapping used by tests.

    Returns:
        Non-empty setting value.

    Raises:
        RuntimeError: If the setting is missing or blank.
    """
    values = os.environ if environment is None else environment
    value  = str(values.get(name, "")).strip()

    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")

    return value


def build_startup_diagnostics(
    environment: dict[str, str] | None = None,
) -> dict[str, Any]:
    """
    Build secret-safe Discord startup and slash-command diagnostics.

    Args:
        environment: Optional environment mapping used by tests.

    Returns:
        Diagnostic payload without token, URL, or credential values.
    """
    values  = os.environ if environment is None else environment
    missing = [
        name
        for name in REQUIRED_DISCORD_SETTINGS
        if not str(values.get(name, "")).strip()
    ]
    command_names = registered_command_names()

    return {
        "status": "ready" if not missing else "configuration_required",
        "required_settings": {
            name: bool(str(values.get(name, "")).strip())
            for name in REQUIRED_DISCORD_SETTINGS
        },
        "missing_settings": missing,
        "control_plane_api_configured": bool(
            str(values.get("CONTROL_PLANE_API_URL", "")).strip()
        ),
        "approval_mutations_configured": bool(
            str(values.get("CONTROL_PLANE_APPROVAL_TOKEN", "")).strip()
        ),
        "registered_commands": command_names,
        "registered_command_count": len(command_names),
        "message_content_intent_enabled": bool(intents.message_content),
        "command_scope": "guild" if str(values.get("DISCORD_GUILD_ID", "")).strip() else "global",
    }


def operator_identity(interaction: discord.Interaction) -> str:
    """
    Build a stable, auditable Discord operator identity.

    Args:
        interaction: Discord slash-command interaction.

    Returns:
        Identity containing the user id and display name.
    """
    user = interaction.user

    return f"discord:{user.id}:{user.display_name}"


# --- Defining Discord IO Helpers
async def send_text(channel_id: int, content: str) -> bool:
    """
    Send a possibly long result to a configured Discord channel.

    Args:
        channel_id: Destination Discord channel id. Zero skips sending.
        content: Complete operator-facing message.

    Returns:
        True when every message chunk was sent, otherwise False.
    """
    if not channel_id:
        logger.info("Discord channel id is not configured; skipping send")
        return False

    channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
    chunks  = split_message(content)

    logger.info(
        "Sending Discord message | channel_id=%s chunks=%d content_head=%s",
        channel_id,
        len(chunks),
        chunks[0][:80] if chunks else "",
    )

    for chunk in chunks:
        await channel.send(chunk)

    return True


async def send_render(channel_id: int, result: DiscordRenderResult) -> bool:
    """Send one compact embed with mentions disabled."""
    if not channel_id:
        logger.info("Discord channel id is not configured; skipping embed send")
        return False

    channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
    embeds  = [discord.Embed.from_dict(item) for item in result.payload.get("embeds", [])]
    view    = build_discord_view(result)

    await channel.send(
        content=str(result.payload.get("content") or "") or None,
        embeds=embeds,
        view=view,
        allowed_mentions=discord.AllowedMentions.none(),
    )

    return True


async def respond_with_message(
    interaction: discord.Interaction,
    content: str,
    ephemeral: bool = True,
) -> None:
    """
    Send bounded Discord interaction follow-up messages.

    Args:
        interaction: Discord interaction context.
        content: Complete response body.
        ephemeral: Whether only the initiating user can see the response.

    Returns:
        None.
    """
    for chunk in split_message(tr(content)):
        await interaction.followup.send(chunk, ephemeral=ephemeral, allowed_mentions=discord.AllowedMentions.none())


async def respond_with_render(
    interaction: discord.Interaction,
    result: DiscordRenderResult,
    ephemeral: bool = True,
) -> None:
    """Return one compact embed directly to the command caller."""
    embeds = [discord.Embed.from_dict(item) for item in result.payload.get("embeds", [])]
    view   = build_discord_view(result)

    send_kwargs: dict[str, Any] = {
        "content": str(result.payload.get("content") or "") or None,
        "embeds": embeds,
        "ephemeral": ephemeral,
        "allowed_mentions": discord.AllowedMentions.none(),
    }
    if view is not None:
        send_kwargs["view"] = view

    await interaction.followup.send(
        **send_kwargs,
    )


async def publish_render(
    interaction: discord.Interaction,
    channel_id: int,
    result: DiscordRenderResult,
    acknowledgement: str,
) -> None:
    """Publish a compact embed or return it ephemerally when no channel exists."""
    sent = await send_render(channel_id=channel_id, result=result)

    if sent:
        await respond_with_message(interaction=interaction, content=acknowledgement)
    else:
        await respond_with_render(interaction=interaction, result=result)


async def command_error(
    interaction: discord.Interaction,
    operation: str,
    exc: Exception,
) -> None:
    """
    Log complete command failures while returning sanitized operator text.

    Args:
        interaction: Discord interaction to answer.
        operation: Human-readable operation label.
        exc: Exception raised during processing.

    Returns:
        None.
    """
    logger.warning(
        "Discord command failed | operation=%s error_type=%s",
        operation,
        type(exc).__name__,
    )
    await show_error(interaction, operation, exc)


@tree.error
async def application_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    """Handle delivered interaction errors, never raw unknown slash messages."""
    operation = str(getattr(getattr(interaction, "command", None), "qualified_name", "Command"))
    await command_error(interaction, operation, error)


# --- Defining Discord Events
@client.event
async def on_ready() -> None:
    """
    Synchronize slash commands and retain readiness diagnostics in logs only.

    Returns:
        None.
    """
    scope = guild_object()

    if scope:
        await tree.sync(guild=scope)
    else:
        await tree.sync()

    diagnostics = build_startup_diagnostics()
    logger.info(
        "Discord bot ready | user=%s commands=%d scope=%s message_content_intent=%s",
        client.user,
        diagnostics["registered_command_count"],
        diagnostics["command_scope"],
        diagnostics["message_content_intent_enabled"],
    )
    if os.getenv("DISCORD_WELCOME_ENABLED", "true").strip().lower() == "true":
        try:
            from apps.discord_bot.welcome import sync_welcome
            with language_context("en"):
                render = render_help(welcome=True)
            result = await sync_welcome(client, GUILD_ID, TRIAGE_CHANNEL_ID, render,
                                        view_factory=build_discord_view)
            logger.info("Discord welcome synchronized | status=%s message_id=%s",
                        result.get("status"), result.get("message_id", ""))
        except Exception as exc:
            logger.warning("Discord welcome unavailable; commands remain ready | error_type=%s", type(exc).__name__)
    logger.info("Discord startup diagnostics retained in logs; no alert/ops startup message was created")


# --- Defining Slash Commands
@tree.command(
    name="alerts",
    description="List data quality alerts",
    guild=guild_object(),
)
@app_commands.describe(
    dt="Optional business date in YYYY-MM-DD format",
    status="Alert lifecycle status",
    limit="Maximum alerts to show",
)
@localized_command
async def alerts(
    interaction: discord.Interaction,
    dt: str = "",
    status: str = DEFAULT_ALERT_STATUS,
    limit: int = DEFAULT_ALERT_LIMIT,
) -> None:
    """
    List alerts with deterministic context and a grounded Copilot readout.

    Args:
        interaction: Discord interaction context.
        dt: Optional business date.
        status: Alert lifecycle status.
        limit: Maximum alerts to list.

    Returns:
        None.
    """
    await interaction.response.defer(thinking=True, ephemeral=True)

    try:
        normalized_dt = dt.strip() or None

        if normalized_dt:
            parse_date(normalized_dt)

        bounded_limit           = max(1, min(limit, 25))
        payload, data_transport = await asyncio.to_thread(
            fetch_discord_alerts,
            status.strip() or DEFAULT_ALERT_STATUS,
            normalized_dt,
            bounded_limit,
        )
        alert_rows = list(payload.get("alerts", []))
        llm_responses: list[Any] = []
        note       = await asyncio.to_thread(
            build_alert_list_copilot_note,
            alert_rows,
            status,
            normalized_dt,
            llm_responses,
        )
        llm_runtime = build_llm_route_audit_payload(llm_responses[-1]) if llm_responses else {}
        render_result = render_alert_list_embed(
            alerts=alert_rows,
            status=status,
            dt=normalized_dt,
            assistant_note=note,
            data_transport=data_transport,
            llm_runtime=llm_runtime,
        )
        await publish_render(
            interaction=interaction,
            channel_id=TRIAGE_CHANNEL_ID,
            result=render_result,
            acknowledgement="Alert list posted to the configured triage channel.",
        )

    except Exception as exc:
        await command_error(interaction=interaction, operation="Alert lookup", exc=exc)


@tree.command(
    name="daily_summary",
    description="Show the DQ summary for one business date",
    guild=guild_object(),
)
@app_commands.describe(dt="Business date in YYYY-MM-DD format")
@localized_command
async def daily_summary(interaction: discord.Interaction, dt: str) -> None:
    """
    Show daily DQ counts with a natural-language reliability readout.

    Args:
        interaction: Discord interaction context.
        dt: Business date in YYYY-MM-DD format.

    Returns:
        None.
    """
    await interaction.response.defer(thinking=True, ephemeral=True)

    try:
        normalized_dt                   = parse_date(dt.strip()).isoformat()
        summary_payload, data_transport = await asyncio.to_thread(
            fetch_discord_daily_summary,
            normalized_dt,
        )
        check_rows = list(summary_payload.get("check_counts") or [])
        alert_rows = list(summary_payload.get("alert_counts") or [])
        llm_responses: list[Any] = []
        note = await asyncio.to_thread(
            build_daily_summary_copilot_note,
            normalized_dt,
            check_rows,
            alert_rows,
            llm_responses,
        )
        llm_runtime = build_llm_route_audit_payload(llm_responses[-1]) if llm_responses else {}
        render_result = render_daily_summary_embed(
            dt=normalized_dt,
            check_rows=check_rows,
            alert_rows=alert_rows,
            assistant_note=note,
            data_transport=data_transport,
            llm_runtime=llm_runtime,
        )
        await publish_render(
            interaction=interaction,
            channel_id=TRIAGE_CHANNEL_ID,
            result=render_result,
            acknowledgement="Daily summary posted to the configured triage channel.",
        )

    except Exception as exc:
        await command_error(interaction=interaction, operation="Daily summary", exc=exc)


async def publish_extended_quality_summary(
    interaction: discord.Interaction,
    payload: dict[str, Any],
    data_transport: str,
    acknowledgement: str,
) -> None:
    """Build a bounded Copilot note and publish one extended quality summary."""
    llm_responses: list[Any] = []
    note = await asyncio.to_thread(build_quality_summary_copilot_note, payload, llm_responses)
    llm_runtime = build_llm_route_audit_payload(llm_responses[-1]) if llm_responses else {}
    render_result = render_quality_summary_embed(
        payload=payload,
        assistant_note=note,
        data_transport=data_transport,
        llm_runtime=llm_runtime,
    )
    await publish_render(
        interaction=interaction,
        channel_id=TRIAGE_CHANNEL_ID,
        result=render_result,
        acknowledgement=acknowledgement,
    )


@tree.command(
    name="weekly_summary",
    description="Show a seven-day DQ reliability summary",
    guild=guild_object(),
)
@app_commands.describe(end_date="Inclusive end date in YYYY-MM-DD format")
@localized_command
async def weekly_summary(interaction: discord.Interaction, end_date: str) -> None:
    """Show a deterministic seven-day summary with a bounded Copilot readout."""
    await interaction.response.defer(thinking=True, ephemeral=True)

    try:
        normalized_end = parse_date(end_date.strip()).isoformat()
        payload, transport = await asyncio.to_thread(
            fetch_discord_weekly_summary,
            normalized_end,
        )
        await publish_extended_quality_summary(
            interaction=interaction,
            payload=payload,
            data_transport=transport,
            acknowledgement="Weekly summary posted to the configured triage channel.",
        )

    except Exception as exc:
        await command_error(interaction=interaction, operation="Weekly summary", exc=exc)


@tree.command(
    name="table_summary",
    description="Show bounded DQ health for one warehouse table",
    guild=guild_object(),
)
@app_commands.describe(
    table_name="Qualified table such as dq.fct_orders_daily",
    start_date="Inclusive start date in YYYY-MM-DD format",
    end_date="Inclusive end date in YYYY-MM-DD format",
)
@localized_command
async def table_summary(
    interaction: discord.Interaction,
    table_name: str,
    start_date: str,
    end_date: str,
) -> None:
    """Show a bounded per-table quality summary and advisory rerun candidates."""
    await interaction.response.defer(thinking=True, ephemeral=True)

    try:
        normalized_start = parse_date(start_date.strip()).isoformat()
        normalized_end   = parse_date(end_date.strip()).isoformat()
        payload, transport = await asyncio.to_thread(
            fetch_discord_table_summary,
            table_name.strip(),
            normalized_start,
            normalized_end,
        )
        await publish_extended_quality_summary(
            interaction=interaction,
            payload=payload,
            data_transport=transport,
            acknowledgement="Table summary posted to the configured triage channel.",
        )

    except Exception as exc:
        await command_error(interaction=interaction, operation="Table summary", exc=exc)


@tree.command(
    name="db_summary",
    description="Show bounded warehouse-wide DQ and metadata health",
    guild=guild_object(),
)
@app_commands.describe(
    start_date="Inclusive start date in YYYY-MM-DD format",
    end_date="Inclusive end date in YYYY-MM-DD format",
)
@localized_command
async def db_summary(
    interaction: discord.Interaction,
    start_date: str,
    end_date: str,
) -> None:
    """Show the registered warehouse quality summary without arbitrary database access."""
    await interaction.response.defer(thinking=True, ephemeral=True)

    try:
        normalized_start = parse_date(start_date.strip()).isoformat()
        normalized_end   = parse_date(end_date.strip()).isoformat()
        payload, transport = await asyncio.to_thread(
            fetch_discord_database_summary,
            normalized_start,
            normalized_end,
        )
        await publish_extended_quality_summary(
            interaction=interaction,
            payload=payload,
            data_transport=transport,
            acknowledgement="Warehouse summary posted to the configured triage channel.",
        )

    except Exception as exc:
        await command_error(interaction=interaction, operation="Warehouse summary", exc=exc)


@tree.command(
    name="triage",
    description="Investigate one Alert Ref or system alert key",
    guild=guild_object(),
)
@app_commands.describe(alert_key="Alert Ref such as DQ-20260610-A1B2C3")
@localized_command
async def triage(interaction: discord.Interaction, alert_key: str) -> None:
    """
    Run evidence-driven triage for one alert.

    Args:
        interaction: Discord interaction context.
        alert_key: Alert Ref or stable system alert key.

    Returns:
        None.
    """
    await interaction.response.defer(thinking=True, ephemeral=True)

    try:
        context = await asyncio.to_thread(fetch_discord_debug_context, alert_key.strip())
        await run_workspace_investigation(interaction, context["alert_ref"])

    except Exception as exc:
        await command_error(interaction=interaction, operation="Triage", exc=exc)


@tree.command(
    name="ask",
    description="Ask the evidence-aware Data Reliability Copilot",
    guild=guild_object(),
)
@app_commands.describe(
    question="Natural-language data reliability question",
    alert_key="Optional Alert Ref or system alert key for grounding",
)
@localized_command
async def ask(
    interaction: discord.Interaction,
    question: str,
    alert_key: str = "",
) -> None:
    """
    Answer a natural-language question using bounded evidence context.

    Args:
        interaction: Discord interaction context.
        question: Natural-language operator question.
        alert_key: Optional alert context.

    Returns:
        None.
    """
    await interaction.response.defer(thinking=True, ephemeral=True)
    state = None
    answer_task = None
    admitted = False
    actor = getattr(getattr(interaction, "user", None), "id", 0)
    thread_key = int(interaction.id)
    try:
        validate_interaction_scope(interaction, {value for value in {ALERTS_CHANNEL_ID, TRIAGE_CHANNEL_ID} if value},
                                   require_bot_authored_message=False)
        question = question.strip()
        if not 3 <= len(question) <= 1000:
            raise ValueError("Question must contain 3-1,000 characters.")
        runtime = workspace_runtime(client, GUILD_ID, TRIAGE_CHANNEL_ID)
        explicit_refs = set(re.findall(r"DQ-[0-9]{8}-[A-Fa-f0-9]{6}", question))
        if len(explicit_refs) > 1:
            raise ValueError("Select one incident for this question.")
        if not alert_key.strip() and explicit_refs:
            alert_key = next(iter(explicit_refs)).upper()
        selected = await asyncio.to_thread(fetch_discord_debug_context, alert_key.strip()) if alert_key.strip() else None
        alert_ref = str((selected or {}).get("alert_ref") or "")
        bound = await runtime.bind_thread(interaction.channel, alert_ref)
        if bound:
            alert_ref = bound.alert_ref
        if alert_ref and explicit_refs and {ref.upper() for ref in explicit_refs} != {alert_ref.upper()}:
            raise ValueError("Question targets a different incident. Open that incident workspace instead.")
        thread = await get_incident_workspace(interaction, alert_ref) if alert_ref else None
        if thread is None and getattr(interaction.channel, "type", None) == discord.ChannelType.public_thread:
            root = await asyncio.to_thread(mention_ask.store.find_thread, str(interaction.channel.id))
            if (root is None or root.guild_id != str(interaction.guild_id)
                    or root.channel_id != str(TRIAGE_CHANNEL_ID)):
                raise ValueError("Start the question in dq-triage or a registered DataSentry thread.")
            if root.alert_ref:
                alert_ref = root.alert_ref
            thread = await runtime.usable(interaction.channel)
        if thread:
            thread_key = thread.id
        admitted = mention_ask.admit(actor, thread_key)
        if not admitted:
            await respond_with_message(interaction, "Another question is running or your cooldown is active. Please wait and try again.")
            return
        state = MentionRequestState(str(interaction.id), str(interaction.id), str(interaction.guild_id),
                                    str(TRIAGE_CHANNEL_ID), str(actor), thread_id=str(thread.id) if thread else "",
                                    alert_ref=alert_ref, source_kind="command")
        if not await asyncio.to_thread(mention_ask.store.claim, state):
            await respond_with_message(interaction, "This question was already submitted; no model call was repeated.")
            return
        mention_ask.last_request[actor] = mention_ask.clock()
        await asyncio.to_thread(mention_ask.audit, state, "started")
        if thread is None:
            channel = client.get_channel(TRIAGE_CHANNEL_ID) or await client.fetch_channel(TRIAGE_CHANNEL_ID)
            starter = await channel.send(embed=discord.Embed(title="Question workspace", description="A read-only question. Answers and follow-ups stay in this thread."),
                                         allowed_mentions=discord.AllowedMentions.none(), nonce=str(interaction.id))
            thread = await starter.create_thread(name="DataSentry question", auto_archive_duration=60)
            state = replace(state, root_message_id=str(starter.id), thread_id=str(thread.id))
            await asyncio.to_thread(mention_ask.store.save, state)
        history = await asyncio.to_thread(mention_ask.store.history, str(thread.id), str(actor), alert_ref,
                                         str(interaction.guild_id), str(TRIAGE_CHANNEL_ID))
        turns = await mention_ask.conversation(thread, history, alert_ref, str(actor))
        answer_task = asyncio.create_task(asyncio.to_thread(
            answer_discord_question,
            question,
            alert_ref,
            conversation_context=turns,
            mention_mode=True,
        ))
        result = await asyncio.wait_for(asyncio.shield(answer_task), timeout=65)
        if alert_ref:
            authority = selected or await asyncio.to_thread(fetch_discord_debug_context, alert_ref)
            allowed_keys = {alert_ref, str(authority.get("alert_key") or alert_ref)}
            if (str(result.get("alert_ref") or alert_ref) != alert_ref
                    or str(result.get("alert_key") or "") not in allowed_keys):
                raise ValueError("Copilot returned a different incident reference.")
        render_result = render_operator_answer_embed(
            answer=result["answer"],
            alert_key=alert_ref or result["alert_key"],
            transport=result["transport"],
            agent_run_id=result["agent_run_id"],
            llm_runtime=result.get("llm_runtime"),
        )
        payload = dict(render_result.payload)
        embeds = [dict(item) for item in payload.get("embeds", [])]
        if embeds:
            embeds[0]["fields"] = list(embeds[0].get("fields", [])) + [{"name": tr("Follow-up question"), "value": question, "inline": False}]
        payload["embeds"] = embeds
        sent = await send_render_to_thread(thread, replace(render_result, payload=payload))
        state = replace(state, status="answered", answer_message_id=str(sent.id), agent_run_id=str(result.get("agent_run_id") or ""))
        await asyncio.to_thread(mention_ask.store.save, state)
        await asyncio.to_thread(mention_ask.audit, state, "answered", {"llm_runtime": result.get("llm_runtime")})
        await respond_with_message(interaction, tr("Answer posted in your workspace: https://discord.com/channels/{v0}/{v1}", v0=interaction.guild_id, v1=thread.id))
    except Exception as exc:
        if state is not None:
            try:
                await asyncio.to_thread(mention_ask.store.save, replace(state, status="failed", error_type=type(exc).__name__))
            except Exception:
                logger.warning("Question claim retained; no automatic provider replay")
        await command_error(interaction=interaction, operation="Copilot answer", exc=exc)
    except asyncio.CancelledError:
        if state is not None:
            await asyncio.shield(asyncio.to_thread(mention_ask.store.save, replace(state, status="failed", error_type="CancelledError")))
        raise
    finally:
        if admitted:
            if answer_task is not None and not answer_task.done():
                answer_task.add_done_callback(lambda task: mention_ask.release_after_completion(task, actor, thread_key))
            else:
                mention_ask.release(actor, thread_key)


@tree.command(
    name="backfill_preview",
    description="Create a durable approval-gated backfill request",
    guild=guild_object(),
)
@app_commands.describe(
    start_date="Inclusive start date in YYYY-MM-DD format",
    end_date="Inclusive end date in YYYY-MM-DD format",
    target_dag_id="Allowlisted operational DAG",
    reason="Reason for the requested backfill",
)
@localized_command
async def backfill_preview(
    interaction: discord.Interaction,
    start_date: str,
    end_date: str,
    target_dag_id: str = DEFAULT_TARGET_DAG_ID,
    reason: str = "Manual Discord backfill preview",
) -> None:
    """
    Create a durable approval request without executing Airflow.

    Args:
        interaction: Discord interaction context.
        start_date: Inclusive business start date.
        end_date: Inclusive business end date.
        target_dag_id: Allowlisted target DAG.
        reason: Human-readable request reason.

    Returns:
        None.
    """
    await interaction.response.defer(thinking=True, ephemeral=True)

    try:
        parsed_start = parse_date(start_date.strip())
        parsed_end   = parse_date(end_date.strip())

        if parsed_end < parsed_start:
            raise ValueError("end_date must be greater than or equal to start_date")

        payload = await asyncio.to_thread(
            create_discord_backfill_approval_request,
            parsed_start.isoformat(),
            parsed_end.isoformat(),
            target_dag_id.strip(),
            reason.strip(),
            operator_identity(interaction),
        )
        request_id = str(payload["request_id"])
        status     = str(payload["status"])
        render_result = render_approval_embed(
            request_id=request_id,
            status=status,
            summary=(
                f"Backfill request for `{target_dag_id.strip()}` covering "
                f"`{parsed_start.isoformat()}` to `{parsed_end.isoformat()}`. {reason.strip()}"
            ),
            next_step="Review the exact scope, then use Approve or Reject.",
            target_dag_id=str(payload.get("target_dag_id") or target_dag_id.strip()),
            start_date=str(payload.get("start_date") or parsed_start.isoformat()),
            end_date=str(payload.get("end_date") or parsed_end.isoformat()),
            risk_level=str(payload.get("risk_level") or "HIGH"),
        )
        await publish_render(
            interaction=interaction,
            channel_id=OPS_CHANNEL_ID,
            result=render_result,
            acknowledgement=f"Backfill approval request {payload['request_id']} recorded.",
        )

        if payload.get("alert_key"):
            try:
                await asyncio.to_thread(
                    synchronize_incident_message,
                    alert_key=str(payload["alert_key"]),
                    approval_state=str(payload.get("status") or "pending"),
                )
            except Exception as notification_exc:
                logger.warning(
                    "Discord incident approval synchronization failed safely | "
                    "request_id=%s error_type=%s",
                    payload.get("request_id"),
                    type(notification_exc).__name__,
                )

    except Exception as exc:
        await command_error(interaction=interaction, operation="Backfill preview", exc=exc)


async def decide_approval(
    interaction: discord.Interaction,
    request_id: str,
    decision: str,
    comment: str,
) -> None:
    """
    Present authoritative approval controls without mutating approval state.

    Args:
        interaction: Discord interaction context.
        request_id: Human-facing APR identifier.
        decision: Approve or reject.
        comment: Human-readable review rationale.

    Returns:
        None.
    """
    try:
        validate_interaction_scope(
            interaction,
            allowed_channels={OPS_CHANNEL_ID} if OPS_CHANNEL_ID else set(),
            require_approver=True,
            require_bot_authored_message=False,
        )
        await interaction.response.defer(thinking=True, ephemeral=True)
        payload = await asyncio.to_thread(fetch_discord_approval_request, request_id.strip())
        request_id = str(payload["request_id"])
        status     = str(payload["status"])
        render_result = render_approval_embed(
            request_id=request_id,
            status=status,
            summary=(
                f"Review the authoritative request before recording a {decision} decision. "
                f"Operator note: {comment.strip()}"
            ),
            next_step=(
                "Use the card controls below. Approve requires confirmation; Reject requires a reason."
                if status == "pending"
                else "This request is no longer pending. View Plan remains read-only."
            ),
            target_dag_id=str(payload.get("target_dag_id") or ""),
            start_date=str(payload.get("start_date") or ""),
            end_date=str(payload.get("end_date") or ""),
            risk_level=str(payload.get("risk_level") or "HIGH"),
        )
        await respond_with_render(interaction, render_result, ephemeral=True)

    except Exception as exc:
        await command_error(interaction=interaction, operation=decision.title(), exc=exc)


@tree.command(
    name="approve",
    description="Approve a pending request without executing it",
    guild=guild_object(),
)
@app_commands.describe(
    request_id="APR identifier from a backfill preview",
    comment="Review rationale stored in the audit trail",
)
@localized_command
async def approve(
    interaction: discord.Interaction,
    request_id: str,
    comment: str = "Reviewed evidence and exact action scope.",
) -> None:
    """Persist an approval decision through the control-plane API."""
    await decide_approval(
        interaction=interaction,
        request_id=request_id,
        decision="approve",
        comment=comment,
    )


@tree.command(
    name="reject",
    description="Reject a pending approval request",
    guild=guild_object(),
)
@app_commands.describe(
    request_id="APR identifier from a backfill preview",
    comment="Rejection rationale stored in the audit trail",
)
@localized_command
async def reject(
    interaction: discord.Interaction,
    request_id: str,
    comment: str = "Rejected after reviewing evidence and action scope.",
) -> None:
    """Persist a rejection decision through the control-plane API."""
    await decide_approval(
        interaction=interaction,
        request_id=request_id,
        decision="reject",
        comment=comment,
    )


# --- Defining Canonical Grouped Command Aliases
@dq_group.command(name="alerts", description="List data quality alerts")
@app_commands.describe(
    dt="Optional business date in YYYY-MM-DD format",
    status="Alert lifecycle status",
    limit="Maximum alerts to show",
)
async def dq_alerts(
    interaction: discord.Interaction,
    dt: str = "",
    status: str = DEFAULT_ALERT_STATUS,
    limit: int = DEFAULT_ALERT_LIMIT,
) -> None:
    """Route the canonical grouped alert command to the existing handler."""
    await alerts.callback(interaction, dt, status, limit)


@dq_group.command(name="daily_summary", description="Show the DQ summary for one business date")
@app_commands.describe(dt="Business date in YYYY-MM-DD format")
async def dq_daily_summary(interaction: discord.Interaction, dt: str) -> None:
    """Route the canonical grouped daily summary to the existing handler."""
    await daily_summary.callback(interaction, dt)


@dq_group.command(name="weekly_summary", description="Show a seven-day DQ reliability summary")
@app_commands.describe(end_date="Inclusive end date in YYYY-MM-DD format")
async def dq_weekly_summary(interaction: discord.Interaction, end_date: str) -> None:
    """Route the canonical grouped weekly summary to the existing handler."""
    await weekly_summary.callback(interaction, end_date)


@dq_group.command(name="table_summary", description="Show DQ health for one warehouse table")
@app_commands.describe(
    table_name="Qualified table such as dq.fct_orders_daily",
    start_date="Inclusive start date in YYYY-MM-DD format",
    end_date="Inclusive end date in YYYY-MM-DD format",
)
async def dq_table_summary(
    interaction: discord.Interaction,
    table_name: str,
    start_date: str,
    end_date: str,
) -> None:
    """Route the canonical grouped table summary to the existing handler."""
    await table_summary.callback(interaction, table_name, start_date, end_date)


@dq_group.command(name="db_summary", description="Show bounded warehouse-wide DQ health")
@app_commands.describe(
    start_date="Inclusive start date in YYYY-MM-DD format",
    end_date="Inclusive end date in YYYY-MM-DD format",
)
async def dq_db_summary(
    interaction: discord.Interaction,
    start_date: str,
    end_date: str,
) -> None:
    """Route the canonical grouped warehouse summary to the existing handler."""
    await db_summary.callback(interaction, start_date, end_date)


@dq_group.command(name="triage", description="Investigate one Alert Ref or system alert key")
@app_commands.describe(alert_key="Alert Ref such as DQ-20260610-A1B2C3")
async def dq_triage(interaction: discord.Interaction, alert_key: str) -> None:
    """Route the canonical grouped triage command to the existing handler."""
    await triage.callback(interaction, alert_key)


@dq_group.command(name="ask", description="Ask the evidence-aware Data Reliability Copilot")
@app_commands.describe(
    question="Natural-language data reliability question",
    alert_key="Optional Alert Ref or system alert key for grounding",
)
async def dq_ask(
    interaction: discord.Interaction,
    question: str,
    alert_key: str = "",
) -> None:
    """Route the canonical grouped Copilot question to the existing handler."""
    await ask.callback(interaction, question, alert_key)


@dq_group.command(name="debug", description="Show ephemeral technical correlation for one alert")
@app_commands.describe(alert_key="Alert Ref or system alert key")
@localized_command
async def dq_debug(interaction: discord.Interaction, alert_key: str) -> None:
    """Return read-only technical identifiers without posting to an operations channel."""
    await interaction.response.defer(thinking=True, ephemeral=True)

    try:
        debug_context = await asyncio.to_thread(
            fetch_discord_debug_context,
            alert_key.strip(),
        )
        await respond_with_render(
            interaction=interaction,
            result=render_debug_embed(debug_context),
            ephemeral=True,
        )

    except Exception as exc:
        await command_error(interaction=interaction, operation="Debug lookup", exc=exc)


@dq_group.command(name="backfill_preview", description="Create an approval-gated backfill request")
@app_commands.describe(
    start_date="Inclusive start date in YYYY-MM-DD format",
    end_date="Inclusive end date in YYYY-MM-DD format",
    target_dag_id="Allowlisted operational DAG",
    reason="Reason for the requested backfill",
)
async def dq_backfill_preview(
    interaction: discord.Interaction,
    start_date: str,
    end_date: str,
    target_dag_id: str = DEFAULT_TARGET_DAG_ID,
    reason: str = "Manual Discord backfill preview",
) -> None:
    """Route the grouped preview command without bypassing approval controls."""
    await backfill_preview.callback(
        interaction,
        start_date,
        end_date,
        target_dag_id,
        reason,
    )


@dq_group.command(name="approve", description="Approve a pending request without executing it")
@app_commands.describe(
    request_id="APR identifier from a backfill preview",
    comment="Review rationale stored in the audit trail",
)
async def dq_approve(
    interaction: discord.Interaction,
    request_id: str,
    comment: str = "Reviewed evidence and exact action scope.",
) -> None:
    """Record grouped approval through the same durable control-plane boundary."""
    await decide_approval(interaction, request_id, "approve", comment)


@dq_group.command(name="reject", description="Reject a pending approval request")
@app_commands.describe(
    request_id="APR identifier from a backfill preview",
    comment="Rejection rationale stored in the audit trail",
)
async def dq_reject(
    interaction: discord.Interaction,
    request_id: str,
    comment: str = "Rejected after reviewing evidence and action scope.",
) -> None:
    """Record grouped rejection through the same durable control-plane boundary."""
    await decide_approval(interaction, request_id, "reject", comment)


# --- Defining CLI Helpers
def build_parser() -> argparse.ArgumentParser:
    """
    Build the Discord bot command-line parser.

    Returns:
        Configured ArgumentParser.
    """
    parser = argparse.ArgumentParser(
        description="Run or inspect the Agentic DQ Discord bot."
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Print secret-safe configuration and command diagnostics without Discord IO.",
    )
    parser.add_argument(
        "--require-settings",
        action="store_true",
        help="Fail smoke inspection when required Discord settings are missing.",
    )
    parser.add_argument(
        "--check-api",
        action="store_true",
        help="Probe the shared control-plane API during smoke inspection.",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """
    Run secret-safe smoke inspection or start the Discord client.

    Args:
        argv: Optional CLI arguments used by tests.

    Returns:
        Process-style status code.

    Raises:
        RuntimeError: If normal startup lacks required configuration.
    """
    args        = build_parser().parse_args(argv)
    diagnostics = build_startup_diagnostics()

    if args.check_api:
        diagnostics["control_plane_health"] = probe_control_plane_health()

    if args.smoke:
        print(json.dumps(diagnostics, indent=2, sort_keys=True))

        if args.require_settings and diagnostics["missing_settings"]:
            return 2

        if args.check_api and diagnostics["control_plane_health"]["status"] != "ready":
            return 3

        return 0

    if diagnostics["missing_settings"]:
        missing = ", ".join(diagnostics["missing_settings"])
        raise RuntimeError(f"Discord bot configuration is incomplete. Missing: {missing}")

    token = required_env("DISCORD_BOT_TOKEN")

    logger.info(
        "Starting Discord bot | commands=%d scope=%s control_plane_api_configured=%s",
        diagnostics["registered_command_count"],
        diagnostics["command_scope"],
        diagnostics["control_plane_api_configured"],
    )
    client.run(token)

    return 0


# --- Running CLI Entrypoint
if __name__ == "__main__":
    raise SystemExit(main())
