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
import sys
from pathlib import Path
from typing import Any, Sequence

import discord
from discord import app_commands


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Add the repository root when this module is executed directly by path.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.llm.copilot import build_alert_list_copilot_note, build_daily_summary_copilot_note
from apps.discord_bot.formatters import (
    format_alert_list,
    format_approval_recorded,
    format_backfill_preview,
    format_bot_online,
    format_daily_summary,
    format_operator_answer,
    format_triage_result,
    split_message,
)
from apps.discord_bot.service import (
    DEFAULT_ALERT_LIMIT,
    DEFAULT_ALERT_STATUS,
    answer_discord_question,
    build_discord_triage_note,
    create_discord_backfill_approval_request,
    decide_discord_approval_request,
    fetch_discord_daily_summary,
    fetch_discord_alerts,
    probe_control_plane_health,
    run_discord_triage,
)
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
)


# --- Creating Discord Client
intents        = discord.Intents.none()
intents.guilds = True

client = discord.Client(intents=intents)
tree   = app_commands.CommandTree(client)


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
    for chunk in split_message(content):
        await interaction.followup.send(chunk, ephemeral=ephemeral)


async def publish_result(
    interaction: discord.Interaction,
    channel_id: int,
    message: str,
    acknowledgement: str,
) -> None:
    """
    Publish a command result and provide a concise interaction acknowledgement.

    Args:
        interaction: Initiating Discord interaction.
        channel_id: Configured destination channel.
        message: Formatted operator result.
        acknowledgement: Short response shown to the command caller.

    Returns:
        None.
    """
    sent = await send_text(channel_id=channel_id, content=message)

    if sent:
        await respond_with_message(interaction=interaction, content=acknowledgement)
    else:
        await respond_with_message(interaction=interaction, content=message)


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
    logger.exception(
        "Discord command failed | operation=%s error_type=%s",
        operation,
        type(exc).__name__,
    )
    await respond_with_message(
        interaction=interaction,
        content=f"{operation} failed safely. Error type: {type(exc).__name__}.",
    )


# --- Defining Discord Events
@client.event
async def on_ready() -> None:
    """
    Synchronize slash commands and announce bot readiness.

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
    await send_text(channel_id=ALERTS_CHANNEL_ID, content=format_bot_online())


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
        note       = await asyncio.to_thread(
            build_alert_list_copilot_note,
            alert_rows,
            status,
            normalized_dt,
        )
        message = format_alert_list(
            alerts=alert_rows,
            status=status,
            dt=normalized_dt,
            assistant_note=note,
            data_transport=data_transport,
        )
        await publish_result(
            interaction=interaction,
            channel_id=ALERTS_CHANNEL_ID,
            message=message,
            acknowledgement="Alert list posted to the configured alerts channel.",
        )

    except Exception as exc:
        await command_error(interaction=interaction, operation="Alert lookup", exc=exc)


@tree.command(
    name="daily_summary",
    description="Show the DQ summary for one business date",
    guild=guild_object(),
)
@app_commands.describe(dt="Business date in YYYY-MM-DD format")
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
        note = await asyncio.to_thread(
            build_daily_summary_copilot_note,
            normalized_dt,
            check_rows,
            alert_rows,
        )
        message = format_daily_summary(
            dt=normalized_dt,
            check_rows=check_rows,
            alert_rows=alert_rows,
            assistant_note=note,
            data_transport=data_transport,
        )
        await publish_result(
            interaction=interaction,
            channel_id=ALERTS_CHANNEL_ID,
            message=message,
            acknowledgement="Daily summary posted to the configured alerts channel.",
        )

    except Exception as exc:
        await command_error(interaction=interaction, operation="Daily summary", exc=exc)


@tree.command(
    name="triage",
    description="Investigate one Alert Ref or system alert key",
    guild=guild_object(),
)
@app_commands.describe(alert_key="Alert Ref such as DQ-20260610-A1B2C3")
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
        report, triage_transport = await asyncio.to_thread(
            run_discord_triage,
            alert_key.strip(),
        )
        assistant_note, narrative_transport = await asyncio.to_thread(
            build_discord_triage_note,
            report,
        )
        message = format_triage_result(
            report=report,
            assistant_note=assistant_note,
            execution_transport=triage_transport,
            narrative_transport=narrative_transport,
        )
        await publish_result(
            interaction=interaction,
            channel_id=TRIAGE_CHANNEL_ID,
            message=message,
            acknowledgement="Triage completed and posted to the configured triage channel.",
        )

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

    try:
        result = await asyncio.to_thread(
            answer_discord_question,
            question.strip(),
            alert_key.strip(),
        )
        message = format_operator_answer(
            question=question.strip(),
            answer=result["answer"],
            alert_key=result["alert_key"],
            transport=result["transport"],
            agent_run_id=result["agent_run_id"],
            incident_history_count=int(
                result.get("incident_history_count") or 0
            ),
        )
        await publish_result(
            interaction=interaction,
            channel_id=TRIAGE_CHANNEL_ID,
            message=message,
            acknowledgement="Copilot answer posted to the configured triage channel.",
        )

    except Exception as exc:
        await command_error(interaction=interaction, operation="Copilot answer", exc=exc)


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
        message = format_backfill_preview(
            request_id=str(payload["request_id"]),
            start_date=parsed_start.isoformat(),
            end_date=parsed_end.isoformat(),
            target_dag_id=target_dag_id.strip(),
            reason=reason.strip(),
            status=str(payload["status"]),
            created_new=bool(payload.get("created_new")),
        )
        await publish_result(
            interaction=interaction,
            channel_id=OPS_CHANNEL_ID or TRIAGE_CHANNEL_ID,
            message=message,
            acknowledgement=f"Backfill approval request {payload['request_id']} recorded.",
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
    Persist one approval decision without executing remediation.

    Args:
        interaction: Discord interaction context.
        request_id: Human-facing APR identifier.
        decision: Approve or reject.
        comment: Human-readable review rationale.

    Returns:
        None.
    """
    await interaction.response.defer(thinking=True, ephemeral=True)

    try:
        payload = await asyncio.to_thread(
            decide_discord_approval_request,
            request_id.strip(),
            decision,
            operator_identity(interaction),
            comment.strip(),
        )
        message = format_approval_recorded(
            request_id=str(payload["request_id"]),
            approved_by=str(payload["decided_by"]),
            status=str(payload["status"]),
            decision=decision,
        )
        await publish_result(
            interaction=interaction,
            channel_id=OPS_CHANNEL_ID or TRIAGE_CHANNEL_ID,
            message=message,
            acknowledgement=(
                "Durable approval recorded. No Airflow DAG was triggered."
                if decision == "approve"
                else "Durable rejection recorded."
            ),
        )

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
