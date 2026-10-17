"""Explicit operator recovery controls. No provider call or local mutation fallback."""
from __future__ import annotations

import asyncio
import logging
import re
from functools import wraps

import discord
logger = logging.getLogger(__name__)

from apps.common.i18n import tr
from apps.common.recovery import RecoveryClient, can_execute, recovery_error_message
from apps.discord_bot import interactions
from apps.discord_bot.formatters import sanitize_discord_text
from apps.discord_bot.service import build_control_plane_client

PATTERN = re.compile(r"^dqrec:v1:(?P<action>review|propose|approve|reject|run|track):(?P<target>DQ-\d{8}-[A-F0-9]{6}|[a-f0-9-]{36})$")


def personal_recovery(callback):
    """Bound locale lookup without acknowledging a Discord interaction twice."""
    @wraps(callback)
    async def localized(self, interaction, *args, **kwargs):
        from apps.common.i18n import language_context
        language = "en"
        if getattr(interaction, "guild_id", None):
            from apps.discord_bot.preferences import get_user_language
            try:
                language = await asyncio.wait_for(asyncio.to_thread(get_user_language,
                    interaction.guild_id, interaction.user.id), timeout=0.75)
            except Exception:
                pass
        with language_context(language if language in {"en", "id"} else "en"):
            return await callback(self, interaction, *args, **kwargs)
    return localized


def recovery_client() -> RecoveryClient:
    return RecoveryClient(build_control_plane_client())


def plan_embed(bundle: dict) -> discord.Embed:
    """Facts come from the authoritative plan; no system keys on the primary card."""
    plan, approval = bundle["plan"], bundle["approval"]
    embed = discord.Embed(title=tr("Recovery plan") + " · " + plan["alert_ref"],
                          description=sanitize_discord_text(plan["reason"], 1000), color=0x13946B)
    for name, value in [("Approval", approval["status"].upper()), ("Runbook", plan["runbook"]),
                        ("Business date", plan["dt"]), ("Affected tables", "\n".join(plan["targets"])),
                        ("Validation and rollback", "\n".join(plan["validation"] + plan["rollback"]))]:
        embed.add_field(name=tr(name), value=sanitize_discord_text(value, 1024), inline=name in {"Approval", "Business date"})
    execution = bundle.get("execution")
    if execution:
        embed.add_field(name=tr("Execution"), value=execution["status"].upper())
        embed.add_field(name=tr("Data verification"), value=execution["validation_status"].upper())
        if execution["synchronization_pending"]:
            embed.add_field(name=tr("Synchronization"), value=tr("Decision stored; audit or Discord delivery is still pending."), inline=False)
    embed.set_footer(text=tr("Approval does not execute. Recovery does not close the incident. No model call."))
    return embed


def plan_components(bundle: dict) -> list[dict]:
    """The REST sender and persistent gateway use identical bounded action IDs."""
    actions = []
    if bundle["approval"]["status"] == "pending":
        actions += [("approve", "Approve Plan"), ("reject", "Reject")]
    if can_execute(bundle):
        actions += [("run", "Run Approved Fix")]
    actions += [("track", "Track Execution")]
    return [{"type": 1, "components": [{"type": 2, "style": 1 if action == "run" else 2,
        "label": label, "custom_id": f"dqrec:v1:{action}:{bundle['plan']['plan_id']}"} for action, label in actions]}]


async def require_operator(interaction: discord.Interaction) -> None:
    from apps.discord_bot.resolution import require_resolution_operator_access
    interactions.validate_interaction_scope(interaction, {value for value in {
        interactions.ALERTS_CHANNEL_ID, interactions.TRIAGE_CHANNEL_ID, interactions.OPS_CHANNEL_ID} if value})
    if getattr(interaction.user, "bot", False):
        raise PermissionError("An operator is required.")
    await require_resolution_operator_access(interaction)


def actor(interaction: discord.Interaction) -> str:
    return "discord:" + str(interaction.user.id)


async def audit_interaction(interaction, action: str, target: str, result: str) -> None:
    """Record correlation only; reasons and free-form questions stay out of this event."""
    from apps.discord_bot.contracts import DiscordInteractionContext, DiscordInteractionResult
    try:
        context = DiscordInteractionContext(action="recovery_" + action, target_ref=target, actor=actor(interaction),
            guild_id=str(getattr(interaction, "guild_id", "") or ""), channel_id=str(getattr(interaction, "channel_id", "") or ""),
            message_id=str(getattr(getattr(interaction, "message", None), "id", "")))
        outcome = DiscordInteractionResult(status="success", action=action, target_ref=target, message=result)
        await asyncio.to_thread(interactions.write_interaction_audit, context, outcome)
    except Exception as exc:
        logger.warning("Discord recovery interaction audit pending | error_code=%s", type(exc).__name__)


class RecoveryView(discord.ui.View):
    def __init__(self, bundle: dict):
        super().__init__(timeout=None)
        plan_id = bundle["plan"]["plan_id"]
        status = bundle["approval"]["status"]
        if status == "pending":
            self.add_item(RecoveryButton("approve", plan_id))
            self.add_item(RecoveryButton("reject", plan_id))
        if can_execute(bundle):
            self.add_item(RecoveryButton("run", plan_id))
        self.add_item(RecoveryButton("track", plan_id))


class ConfirmRecovery(discord.ui.View):
    def __init__(self, bundle: dict, action: str, reason: str, owner: int):
        super().__init__(timeout=300)
        self.bundle, self.action, self.reason, self.owner = bundle, action, reason, owner

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    @personal_recovery
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner:
            await interaction.response.send_message(tr("Only the operator who opened this confirmation can submit it."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await require_operator(interaction)
            client = recovery_client()
            if self.action == "approve":
                bundle = await asyncio.to_thread(client.decide, self.bundle, actor=actor(interaction), decision="approve", comment=self.reason)
            else:
                execution = await asyncio.to_thread(client.execute, self.bundle, actor=actor(interaction), confirm_scope=True)
                bundle = await asyncio.to_thread(client.plan, self.bundle["plan"]["plan_id"])
                bundle["execution"] = execution
            await interaction.edit_original_response(embed=plan_embed(bundle), view=RecoveryView(bundle))
            await audit_interaction(interaction, self.action, bundle["plan"]["plan_id"], "accepted")
            self.stop()
        except Exception as exc:
            await interaction.followup.send(tr(recovery_error_message(exc)), ephemeral=True)


class RecoveryReason(discord.ui.Modal):
    reason = discord.ui.TextInput(label="Reason", min_length=5, max_length=1000, style=discord.TextStyle.paragraph)

    def __init__(self, action: str, target: str, *, bundle: dict | None = None, revision: int | None = None):
        super().__init__(title=tr("Review recovery scope"), timeout=300)
        self.action, self.target, self.bundle, self.revision = action, target, bundle, revision
        self.reason.label = tr("Decision reason")

    @personal_recovery
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            await require_operator(interaction)
            client = recovery_client()
            if self.action == "propose":
                bundle = await asyncio.to_thread(client.propose, self.target, reason=str(self.reason.value),
                                                expected_incident_revision=self.revision)
                await interaction.followup.send(embed=plan_embed(bundle), view=RecoveryView(bundle), ephemeral=True)
            elif self.action == "reject":
                bundle = await asyncio.to_thread(client.decide, self.bundle, actor=actor(interaction), decision="reject", comment=str(self.reason.value))
                await interaction.followup.send(embed=plan_embed(bundle), view=RecoveryView(bundle), ephemeral=True)
            else:
                await interaction.followup.send(embed=plan_embed(self.bundle),
                    content=tr("Confirm the reviewed recovery scope"),
                    view=ConfirmRecovery(self.bundle, "approve", str(self.reason.value), interaction.user.id), ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(tr(recovery_error_message(exc)), ephemeral=True)


class RecoveryButton(discord.ui.DynamicItem[discord.ui.Button], template=PATTERN):
    def __init__(self, action: str, target: str):
        labels = {"review": "Review Fix", "propose": "Create recovery plan", "approve": "Approve Plan",
                  "reject": "Reject", "run": "Run Approved Fix", "track": "Track Execution"}
        style = discord.ButtonStyle.primary if action == "run" else discord.ButtonStyle.secondary
        super().__init__(discord.ui.Button(label=tr(labels[action]), style=style, custom_id=f"dqrec:v1:{action}:{target}"))
        self.action, self.target = action, target

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"], match["target"])

    @personal_recovery
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            await require_operator(interaction)
            await audit_interaction(interaction, self.action, self.target, "opened")
            # Modals require the initial response; all expensive reads happen after acknowledgement.
            if self.action == "propose":
                preview = await asyncio.to_thread(recovery_client().preview, self.target)
                view = interactions.ModalLaunchView(RecoveryReason("propose", self.target, revision=preview["incident_revision"]), interaction.user.id, tr("Create recovery plan"))
                await interaction.followup.send(tr("Review the scope, then explain why it should be recovered."), view=view, ephemeral=True)
                return
            client = recovery_client()
            if self.action == "review":
                preview = await asyncio.to_thread(client.preview, self.target)
                if preview.get("latest_plan_id"):
                    bundle = await asyncio.to_thread(client.plan, preview["latest_plan_id"])
                    await interaction.followup.send(embed=plan_embed(bundle), view=RecoveryView(bundle), ephemeral=True)
                else:
                    embed = discord.Embed(title=tr("Review Fix") + " · " + self.target,
                        description=tr("Recovery replays saved input or rebuilds derived data. It does not generate a clean baseline or close the incident."), color=0x13946B)
                    embed.add_field(name=tr("Runbook"), value=preview.get("runbook") or tr("Unavailable"))
                    if preview["blockers"]:
                        embed.add_field(name=tr("Recovery blockers"), value=sanitize_discord_text("\n".join(preview["blockers"]), 1024), inline=False)
                    view = discord.ui.View(timeout=None)
                    if preview["eligible"]:
                        view.add_item(RecoveryButton("propose", self.target))
                    await interaction.followup.send(embed=embed, view=view, ephemeral=True)
                return
            bundle = await asyncio.to_thread(client.plan, self.target)
            if self.action in {"approve", "reject"}:
                modal = RecoveryReason(self.action, self.target, bundle=bundle)
                view = interactions.ModalLaunchView(modal, interaction.user.id, tr("Continue"))
                await interaction.followup.send(embed=plan_embed(bundle), view=view, ephemeral=True)
            elif self.action == "run":
                await interaction.followup.send(embed=plan_embed(bundle), content=tr("Confirm the reviewed recovery scope"),
                    view=ConfirmRecovery(bundle, "run", "", interaction.user.id), ephemeral=True)
            else:
                await interaction.followup.send(embed=plan_embed(bundle), view=RecoveryView(bundle), ephemeral=True)
        except Exception as exc:
            message = tr(recovery_error_message(exc))
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
