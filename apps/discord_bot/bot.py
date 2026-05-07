####
## Discord Bot for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import os

import discord
from discord import app_commands

from pipelines.common.logging import logger


# --- Defining Constants
TOKEN     = os.environ["DISCORD_BOT_TOKEN"]
GUILD_ID  = int(os.getenv("DISCORD_GUILD_ID", "0") or "0")
ALERTS_CH = int(os.environ["DISCORD_ALERTS_CHANNEL_ID"])
TRIAGE_CH = int(os.environ["DISCORD_TRIAGE_CHANNEL_ID"])

intents = discord.Intents.default()
client  = discord.Client(intents=intents)
tree    = app_commands.CommandTree(client)


# --- Defining Functions
async def send_text(channel_id: int, content: str) -> None:
    """
    Send a Discord message to a configured channel.

    Args:
        channel_id: Discord channel id.
        content: Message body to send.

    Returns:
        None.
    """
    logger.info("Sending Discord message | channel_id=%s content_head=%s", channel_id, content[:80])

    channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
    await channel.send(content)


@client.event
async def on_ready() -> None:
    """
    Sync slash commands and announce that the bot is online.

    Returns:
        None.
    """
    logger.info("Discord bot ready | user=%s guild_id=%s", client.user, GUILD_ID)

    # Guild-scoped sync is faster during local development than global command sync.
    if GUILD_ID:
        await tree.sync(guild=discord.Object(id=GUILD_ID))
    else:
        await tree.sync()

    await send_text(ALERTS_CH, "DQ bot is online.")


@tree.command(
    name="triage",
    description="Run triage for an alert_id",
    guild=discord.Object(id=GUILD_ID) if GUILD_ID else None,
)
@app_commands.describe(alert_id="Alert ID from ClickHouse alerts table")
async def triage(interaction: discord.Interaction, alert_id: int) -> None:
    """
    Handle the /triage slash command.

    Args:
        interaction: Discord interaction context.
        alert_id: Alert id selected by the user.

    Returns:
        None.
    """
    logger.info("Received Discord triage command | alert_id=%s user=%s", alert_id, interaction.user)

    await interaction.response.defer(thinking=True)

    # Integration point: wire agent/graph.py here once the core triage runner lands.
    summary = f"Triage started for alert_id={alert_id}. (mock)"

    await send_text(TRIAGE_CH, summary)
    await interaction.followup.send(f"Done. Posted to <#{TRIAGE_CH}>")


logger.info("Starting Discord bot")
client.run(TOKEN)
