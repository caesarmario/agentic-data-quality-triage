####
## Discord Interactions for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Persistent, evidence-aware Discord button and modal handlers."""

# --- Importing Libraries
from __future__ import annotations

import asyncio
import os
import re
from dataclasses import replace
from functools import wraps
from typing import Any

import discord
from apps.common.i18n import tr, current_language, language_context

from agent.tools.audit_log import write_agent_audit_event
from apps.discord_bot.contracts import (
    DiscordActionSpec,
    DiscordActionRow,
    DiscordInteractionContext,
    DiscordInteractionResult,
    DiscordRenderResult,
)
from apps.discord_bot.delivery import synchronize_incident_message
from apps.discord_bot.formatters import (
    render_approval_embed,
    render_approval_plan_embed,
    render_evidence_embed,
    render_operator_answer_embed,
    render_report_reference_embed,
    render_triage_embed,
    sanitize_discord_text,
)
from apps.discord_bot.mention_store import MentionRequestState
from apps.discord_bot.workspace import get_incident_workspace as get_workspace_runtime, operator_language, report_attachments
from apps.discord_bot import workspace as workspace_module
from apps.discord_bot.service import (
    answer_discord_question,
    build_discord_triage_note,
    create_discord_backfill_approval_request,
    decide_discord_approval_request,
    fetch_discord_approval_request,
    fetch_discord_debug_context,
    fetch_discord_evidence_context,
    run_discord_triage,
)
from pipelines.common.clickhouse import build_clickhouse_client
from pipelines.common.logging import logger


# --- Defining Constants
DEFAULT_TARGET_DAG_ID = "00_dag_dq_platform_daily_orchestrator"

GUILD_ID          = int(os.getenv("DISCORD_GUILD_ID", "0") or "0")
ALERTS_CHANNEL_ID = int(os.getenv("DISCORD_ALERTS_CHANNEL_ID", "0") or "0")
TRIAGE_CHANNEL_ID = int(os.getenv("DISCORD_TRIAGE_CHANNEL_ID", "0") or "0")
OPS_CHANNEL_ID    = int(os.getenv("DISCORD_OPS_CHANNEL_ID", "0") or "0")

INCIDENT_ACTION_PATTERN = re.compile(
    r"^dq:v1:(?P<action>investigate|reinvestigate|ask|evidence|report|draft|resolve|resolve_check|resolve_status|resolve_close|resolve_evidence|resolve_cancel)"
    r":(?P<target>DQ-[A-Za-z0-9-]{3,80})(?::(?P<request_id>[A-Za-z0-9-]{1,50}))?$"
)
APPROVAL_ACTION_PATTERN = re.compile(
    r"^dq:v1:(?P<action>view_plan|approve|reject):(?P<target>APR-[A-Za-z0-9-]{3,80})$"
)

BUTTON_STYLES = {
    "primary": discord.ButtonStyle.primary,
    "secondary": discord.ButtonStyle.secondary,
    "success": discord.ButtonStyle.success,
    "danger": discord.ButtonStyle.danger,
}


# --- Defining Interaction Helpers
def personal_copy(callback: Any) -> Any:
    """ACK before preference I/O and scope fixed personal copy to the invoking user."""
    @wraps(callback)
    async def localized(*args: Any, **kwargs: Any) -> Any:
        interaction = kwargs.get("interaction") or (args[0] if hasattr(args[0], "response") else args[1])
        if not interaction.response.is_done():
            await interaction.response.defer(thinking=True, ephemeral=True)
        async with operator_language(interaction.guild_id, interaction.user.id):
            return await callback(*args, **kwargs)
    return localized


def operator_identity(interaction: discord.Interaction) -> str:
    """Build a stable operator identity without retaining message content."""
    return f"discord:{interaction.user.id}:{interaction.user.display_name}"


def interaction_context(
    interaction: discord.Interaction,
    action: str,
    target_ref: str,
) -> DiscordInteractionContext:
    """Capture sanitized interaction correlation fields for audit."""
    return DiscordInteractionContext(
        action=action,
        target_ref=target_ref,
        actor=operator_identity(interaction),
        guild_id=str(interaction.guild_id or ""),
        channel_id=str(interaction.channel_id or ""),
        message_id=str(getattr(interaction.message, "id", "") or ""),
    )


def configured_approver_role_ids() -> set[int]:
    """Read optional role hardening while allowing private-channel access by default."""
    role_ids = set()

    for value in os.getenv("DISCORD_APPROVER_ROLE_IDS", "").split(","):
        normalized = value.strip()
        if normalized.isdigit():
            role_ids.add(int(normalized))

    return role_ids


def validate_interaction_scope(
    interaction: discord.Interaction,
    allowed_channels: set[int],
    require_approver: bool = False,
    require_bot_authored_message: bool = True,
) -> None:
    """Fail closed when a component is invoked outside its bounded guild/channel scope."""
    if interaction.user.bot:
        raise PermissionError("Bot users cannot operate incident controls.")

    if not GUILD_ID:
        raise PermissionError("The Discord guild boundary is not configured.")

    if interaction.guild_id != GUILD_ID:
        raise PermissionError("This control belongs to a different Discord server.")

    if not allowed_channels:
        raise PermissionError("This Discord control has no configured channel boundary.")

    channel = getattr(interaction, "channel", None)
    triage_thread = (
        isinstance(channel, discord.Thread)
        and channel.type == discord.ChannelType.public_thread
        and channel.parent_id == TRIAGE_CHANNEL_ID
        and TRIAGE_CHANNEL_ID in allowed_channels
        and not require_approver
    )
    if interaction.channel_id not in allowed_channels and not triage_thread:
        raise PermissionError("This control is not allowed in the current channel.")

    message_author_id = str(getattr(getattr(interaction.message, "author", None), "id", "") or "")
    bot_user_id       = str(getattr(interaction.client.user, "id", "") or "")
    if require_bot_authored_message:
        if not message_author_id or not bot_user_id:
            raise PermissionError("The DataSentry message author could not be verified.")
        if message_author_id != bot_user_id:
            raise PermissionError("Only DataSentry-authored controls are accepted.")

    role_ids = configured_approver_role_ids()
    if require_approver and role_ids:
        user_role_ids = {int(role.id) for role in getattr(interaction.user, "roles", [])}
        if not role_ids.intersection(user_role_ids):
            raise PermissionError("You do not have an allowlisted DQ approver role.")


def write_interaction_audit(
    context: DiscordInteractionContext,
    result: DiscordInteractionResult,
    error_type: str = "",
) -> None:
    """Persist metadata-only interaction evidence without raw questions or answers."""
    try:
        client = build_clickhouse_client()
        write_agent_audit_event(
            client=client,
            action=f"discord_interaction_{context.action}",
            status=result.status,
            alert_key=result.alert_key,
            actor=context.actor,
            tool_name="discord_interactions",
            input_payload={
                "target_ref": context.target_ref,
                "guild_id": context.guild_id,
                "channel_id": context.channel_id,
                "message_id": context.message_id,
            },
            output_payload={
                "result_status": result.status,
                "detail_keys": sorted(result.details),
                "error_type": error_type,
            },
            error_message=error_type,
        )
    except Exception as exc:
        logger.warning(
            "Discord interaction audit failed safely | action=%s target=%s error_type=%s",
            context.action,
            context.target_ref,
            type(exc).__name__,
        )


async def send_render_to_channel(
    interaction: discord.Interaction,
    channel_id: int,
    result: DiscordRenderResult,
) -> discord.Message:
    """Send one rendered result using the same persistent controls as REST delivery."""
    source_channel = getattr(interaction, "channel", None)
    if (channel_id == TRIAGE_CHANNEL_ID and isinstance(source_channel, discord.Thread)
            and source_channel.type == discord.ChannelType.public_thread
            and source_channel.parent_id == TRIAGE_CHANNEL_ID):
        channel = source_channel
    else:
        channel = interaction.client.get_channel(channel_id) or await interaction.client.fetch_channel(channel_id)
    embeds  = [discord.Embed.from_dict(item) for item in result.payload.get("embeds", [])]
    view    = build_discord_view(result)

    send_kwargs = {
        "content": str(result.payload.get("content") or "") or None,
        "embeds": embeds,
        "allowed_mentions": discord.AllowedMentions.none(),
    }
    if view is not None:
        send_kwargs["view"] = view

    return await channel.send(**send_kwargs)


async def respond_with_render(
    interaction: discord.Interaction,
    result: DiscordRenderResult,
    ephemeral: bool = True,
) -> None:
    """Return one rendered result from a component or modal interaction."""
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


async def send_render_to_thread(thread: Any, result: DiscordRenderResult,
                                files: list[discord.File] | None = None) -> Any:
    """Post shared render output only to a verified canonical public thread."""
    kwargs: dict[str, Any] = {
        "content": str(result.payload.get("content") or "") or None,
        "embeds": [discord.Embed.from_dict(item) for item in result.payload.get("embeds", [])],
        "allowed_mentions": discord.AllowedMentions.none(),
    }
    view = build_discord_view(result)
    if view is not None:
        kwargs["view"] = view
    if files:
        kwargs["files"] = files
    try:
        return await thread.send(**kwargs)
    finally:
        for file in files or []:
            file.close()


def with_reinvestigate_control(render: DiscordRenderResult, alert_ref: str) -> DiscordRenderResult:
    """Add an explicit persistent model rerun without changing shared formatters."""
    if render.lifecycle == "RESOLVED":
        return render
    if any(action.custom_id == f"dq:v1:reinvestigate:{alert_ref}"
           for row in render.action_rows for action in row.actions):
        return render
    if len(render.action_rows) >= 5:
        return render
    row = DiscordActionRow(actions=(DiscordActionSpec(
        "reinvestigate", tr("Run New Investigation"), "secondary", custom_id=f"dq:v1:reinvestigate:{alert_ref}",
    ),))
    return replace(render, action_rows=render.action_rows + (row,))


async def get_incident_workspace(interaction: discord.Interaction, alert_ref: str) -> discord.Thread:
    """Parent slash adapter: ACK, ensure canonical thread, send an ephemeral link.

    The returned thread is public, in configured dq-triage, and not locked.
    Callers must not send the shared render to the parent channel afterwards.
    """
    if not interaction.response.is_done():
        await interaction.response.defer(thinking=True, ephemeral=True)
    validate_interaction_scope(interaction, {value for value in {ALERTS_CHANNEL_ID, TRIAGE_CHANNEL_ID} if value},
                               require_bot_authored_message=False)
    runtime = get_workspace_runtime(interaction.client, GUILD_ID, TRIAGE_CHANNEL_ID)
    await runtime.bind_thread(getattr(interaction, "channel", None), alert_ref)
    workspace = await runtime.ensure(alert_ref)
    async with operator_language(interaction.guild_id, interaction.user.id):
        await interaction.followup.send(tr("Incident workspace: {url}", url=workspace.url), ephemeral=True,
                                        allowed_mentions=discord.AllowedMentions.none())
    return workspace.thread


async def run_workspace_investigation(interaction: discord.Interaction, alert_ref: str,
                                      rerun: bool = False) -> Any:
    """Parent slash adapter: ACK, reuse/claim triage, post in thread, return report.

    ``rerun=True`` is an explicit reinvestigation, not a timeout retry. Durable
    unresolved claims block both slash and persistent-component provider replay.
    Exceptions propagate to the parent's sanitized error boundary.
    """
    if not interaction.response.is_done():
        await interaction.response.defer(thinking=True, ephemeral=True)
    validate_interaction_scope(interaction, {value for value in {ALERTS_CHANNEL_ID, TRIAGE_CHANNEL_ID} if value},
                               require_bot_authored_message=False)
    runtime = get_workspace_runtime(interaction.client, GUILD_ID, TRIAGE_CHANNEL_ID)
    await runtime.bind_thread(getattr(interaction, "channel", None), alert_ref)
    workspace = await runtime.ensure(alert_ref)
    handler = getattr(interaction.client, "mention_ask_handler", None)
    if handler is None:
        from apps.discord_bot.mentions import MentionAskHandler
        handler = MentionAskHandler(interaction.client, GUILD_ID, TRIAGE_CHANNEL_ID, workspace=runtime)
    actor, thread_key = interaction.user.id, workspace.thread.id
    if not handler.admit(actor, thread_key):
        raise ValueError("A question/investigation is active or the 10-second cooldown has not finished.")
    handler.last_request[actor] = handler.clock()
    investigation_task = None
    try:
        async with operator_language(interaction.guild_id, actor):
            investigation_task = asyncio.create_task(runtime.investigate(workspace, str(interaction.id), reinvestigate=rerun))
            report, transport = await asyncio.wait_for(asyncio.shield(investigation_task), handler.request_timeout_seconds)
            note = str(getattr(report.ai_investigation, "what_happened", "") or getattr(report, "summary", "") or "")
            render = with_reinvestigate_control(render_triage_embed(
                report=report, assistant_note=note, execution_transport=transport, narrative_transport="stored_report",
            ), workspace.state.alert_ref)
        await send_render_to_thread(workspace.thread, render)
        await asyncio.to_thread(synchronize_incident_message, str(report.alert.alert_key), note,
                                report.llm_runtime.model_dump(mode="json"),
                                str(getattr(report.ai_investigation, "source", "") or ""))
        context = interaction_context(interaction, "reinvestigate" if rerun else "investigate", alert_ref)
        async with operator_language(interaction.guild_id, actor):
            outcome = DiscordInteractionResult(
                status="success", action=context.action, target_ref=alert_ref, alert_key=workspace.state.alert_key,
                message=tr("Investigation available in the incident thread: {url}", url=workspace.url),
                details={"narrative_source": render.narrative_source, "transport": transport},
            )
            await asyncio.to_thread(write_interaction_audit, context, outcome)
            await interaction.followup.send(outcome.message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        return report
    finally:
        if investigation_task is not None and not investigation_task.done():
            investigation_task.add_done_callback(lambda task: handler.release_after_completion(task, actor, thread_key))
        else:
            handler.release(actor, thread_key)


async def safe_component_error(
    interaction: discord.Interaction,
    context: DiscordInteractionContext,
    exc: Exception,
) -> None:
    """Return sanitized component failure text and write an auditable failure event."""
    logger.warning(
        "Discord interaction failed | action=%s target=%s error_type=%s",
        context.action,
        context.target_ref,
        type(exc).__name__,
    )
    if not interaction.response.is_done():
        await interaction.response.defer(thinking=True, ephemeral=True)
    async with operator_language(interaction.guild_id, interaction.user.id):
        result = DiscordInteractionResult(
            status="failed", action=context.action, target_ref=context.target_ref,
            message=tr("The request failed safely. Error type: {error_type}.", error_type=type(exc).__name__),
        )
        await asyncio.to_thread(write_interaction_audit, context, result, type(exc).__name__)
        await interaction.followup.send(result.message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


# --- Defining Modal Workflows
class ModalLaunchView(discord.ui.View):
    """Open a personalized modal after the initial component ACK and preference read.

    Discord cannot send a modal after deferring the same interaction. A private
    launch control keeps DB I/O out of that three-second acknowledgement window.
    """

    def __init__(self, modal: discord.ui.Modal, requester_id: int, label: str) -> None:
        super().__init__(timeout=300)
        self.modal = modal
        self.requester_id = requester_id
        self.language = current_language()
        button = discord.ui.Button(label=label, style=discord.ButtonStyle.primary)
        button.callback = self.open
        self.add_item(button)

    async def open(self, interaction: discord.Interaction) -> None:
        with language_context(self.language):
            if interaction.user.bot or interaction.user.id != self.requester_id:
                await interaction.response.send_message(tr("Only the requesting operator can open this form."), ephemeral=True)
                return
            await interaction.response.send_modal(self.modal)


class AskIncidentModal(discord.ui.Modal):
    """Collect one bounded question grounded to an existing Alert Ref."""

    question = discord.ui.TextInput(
        label="Question about this incident",
        placeholder="What is the strongest evidence for the likely root cause?",
        min_length=3,
        max_length=1000,
        style=discord.TextStyle.paragraph,
    )

    def __init__(self, alert_ref: str, origin_message: discord.Message | None = None,
                 requester_id: int = 0) -> None:
        super().__init__(title=tr("Ask DataSentry"), timeout=300)
        self.question.label = tr("Question about this incident")
        self.question.placeholder = tr("What is the strongest evidence for the likely root cause?")
        self.alert_ref = alert_ref
        self.origin_message = origin_message
        self.requester_id = requester_id

    @personal_copy
    async def on_submit(self, interaction: discord.Interaction) -> None:
        context = interaction_context(interaction, "ask", self.alert_ref)
        handler = getattr(interaction.client, "mention_ask_handler", None)
        state = None
        admitted = False
        claimed = False
        answer_task = None
        thread_key = 0
        try:
            validate_interaction_scope(
                interaction, {value for value in {ALERTS_CHANNEL_ID, TRIAGE_CHANNEL_ID} if value},
                require_bot_authored_message=False,
            )
            if (self.origin_message is None
                    or self.origin_message.author.id != interaction.client.user.id
                    or self.origin_message.channel.id != interaction.channel_id
                    or self.requester_id != interaction.user.id):
                raise PermissionError("The follow-up origin could not be verified.")
            question = str(self.question.value).strip()
            if not 3 <= len(question) <= 1000:
                raise ValueError("Questions must contain 3-1,000 characters.")
            refs = {ref.upper() for ref in workspace_module.ALERT_REF.findall(question)}
            if refs and refs != {self.alert_ref.upper()}:
                raise PermissionError("Open the other incident separately; this question cannot switch incident context.")
            runtime = get_workspace_runtime(interaction.client, GUILD_ID, TRIAGE_CHANNEL_ID)
            origin_channel = getattr(interaction, "channel", None)
            canonical = await runtime.bind_thread(origin_channel, self.alert_ref)
            authority = canonical
            if handler is None:
                raise RuntimeError("Thread Copilot admission is unavailable.")
            if isinstance(origin_channel, discord.Thread) and canonical is None:
                root = await asyncio.to_thread(handler.store.find_thread, str(origin_channel.id))
                if (root is None or root.guild_id != str(interaction.guild_id)
                        or root.channel_id != str(TRIAGE_CHANNEL_ID) or origin_channel.locked):
                    raise PermissionError("This thread is not a registered DataSentry conversation.")
                thread, root_id = origin_channel, root.root_message_id
            else:
                workspace = await runtime.ensure(self.alert_ref)
                authority = workspace.state
                thread, root_id = workspace.thread, workspace.state.starter_message_id
            thread_key = thread.id
            admitted = handler.admit(interaction.user.id, thread_key)
            if not admitted:
                raise ValueError("A question is active or the 10-second cooldown has not finished.")
            state = MentionRequestState(
                source_message_id=str(interaction.id), root_message_id=root_id,
                guild_id=str(interaction.guild_id), channel_id=str(TRIAGE_CHANNEL_ID),
                actor_id=str(interaction.user.id), thread_id=str(thread_key),
                alert_ref=self.alert_ref, source_kind="modal",
            )
            claimed = await asyncio.to_thread(handler.store.claim, state)
            if not claimed:
                await interaction.followup.send(tr("This follow-up was already submitted; no model call was repeated."), ephemeral=True)
                return
            handler.last_request[interaction.user.id] = handler.clock()
            history = await asyncio.to_thread(handler.store.history, str(thread_key), str(interaction.user.id),
                                              self.alert_ref, str(interaction.guild_id), str(TRIAGE_CHANNEL_ID))
            turns = await handler.conversation(thread, history, self.alert_ref, str(interaction.user.id))
            async with operator_language(interaction.guild_id, interaction.user.id):
                answer_task = asyncio.create_task(asyncio.to_thread(
                    answer_discord_question, question, self.alert_ref, conversation_context=turns, mention_mode=True,
                ))
                result = await asyncio.wait_for(asyncio.shield(answer_task), handler.request_timeout_seconds)
            if not isinstance(result, dict) or not str(result.get("answer") or "").strip():
                raise ValueError("Invalid Copilot answer")
            if str(result.get("alert_ref") or self.alert_ref) != self.alert_ref:
                raise ValueError("Copilot returned a different incident reference.")
            returned_key = str(result.get("alert_key") or "")
            if authority is not None and returned_key and returned_key not in {authority.alert_key, authority.alert_ref}:
                raise ValueError("Copilot returned a different authoritative incident key.")
            async with operator_language(interaction.guild_id, interaction.user.id):
                render = render_operator_answer_embed(
                    answer=result["answer"], alert_key=self.alert_ref,
                    transport=result.get("transport", ""), agent_run_id=result.get("agent_run_id", ""),
                    llm_runtime=result.get("llm_runtime"),
                )
            render.payload["embeds"][0].setdefault("fields", []).append({
                "name": tr("Follow-up question"), "value": sanitize_discord_text(question, 1000), "inline": False,
            })
            answer_message = await send_render_to_thread(thread, render)
            if state is not None:
                state = replace(state, answer_message_id=str(answer_message.id), status="answered",
                                agent_run_id=str(result.get("agent_run_id") or ""))
                await asyncio.to_thread(handler.store.save, state)
            outcome = DiscordInteractionResult(
                status="success",
                action="ask",
                target_ref=self.alert_ref,
                alert_key=str(result.get("alert_key") or ""),
                message=tr("Answer posted in the incident thread: {url}", url=f"https://discord.com/channels/{interaction.guild_id}/{thread_key}"),
                details={"narrative_source": render.narrative_source, "conversation_turns": len(turns)},
            )
            await asyncio.to_thread(write_interaction_audit, context, outcome)
            await interaction.followup.send(outcome.message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        except Exception as exc:
            if state is not None and claimed:
                try:
                    await asyncio.to_thread(handler.store.save, replace(state, status="failed", error_type=type(exc).__name__))
                except Exception:
                    logger.warning("Follow-up claim retained; no automatic replay")
            await safe_component_error(interaction, context, exc)
        finally:
            if admitted:
                if answer_task is not None and not answer_task.done():
                    actor_id = interaction.user.id
                    answer_task.add_done_callback(lambda task: handler.release_after_completion(task, actor_id, thread_key))
                else:
                    handler.release(interaction.user.id, thread_key)


class DraftActionModal(discord.ui.Modal):
    """Create an approval request preview for one incident without execution."""

    reason = discord.ui.TextInput(
        label="Why should this action be reviewed?",
        min_length=8,
        max_length=500,
        style=discord.TextStyle.paragraph,
    )

    def __init__(self, alert_ref: str) -> None:
        super().__init__(title=tr("Draft approval-gated action"), timeout=300)
        self.reason.label = tr("Why should this action be reviewed?")
        self.alert_ref = alert_ref
        self.reason.default = tr("Review a bounded backfill for incident {alert_ref}.", alert_ref=alert_ref)

    @personal_copy
    async def on_submit(self, interaction: discord.Interaction) -> None:
        context = interaction_context(interaction, "draft", self.alert_ref)

        try:
            validate_interaction_scope(interaction, {value for value in {ALERTS_CHANNEL_ID, TRIAGE_CHANNEL_ID} if value},
                                       require_bot_authored_message=False)
            if not OPS_CHANNEL_ID:
                raise PermissionError("The private approval channel is not configured.")
            evidence = await asyncio.to_thread(fetch_discord_evidence_context, self.alert_ref)
            if str(evidence.get("status") or "").strip().lower() == "resolved":
                raise ValueError("Resolved incidents cannot create a new remediation draft.")
            affected_date = str(evidence.get("affected_date") or "")
            if not affected_date:
                raise ValueError("The incident does not have an affected business date.")

            payload = await asyncio.to_thread(
                create_discord_backfill_approval_request,
                affected_date,
                affected_date,
                DEFAULT_TARGET_DAG_ID,
                str(self.reason.value).strip(),
                operator_identity(interaction),
                str(evidence["alert_key"]),
            )
            render = render_approval_embed(
                request_id=str(payload["request_id"]),
                status=str(payload["status"]),
                summary=(
                    tr("Review a bounded backfill proposal for `{table}` on `{date}`. No execution has occurred.",
                       table=evidence['table_name'], date=affected_date)
                ),
                next_step=tr("Review the exact scope, then approve or reject this request."),
                target_dag_id=str(payload.get("target_dag_id") or DEFAULT_TARGET_DAG_ID),
                start_date=str(payload.get("start_date") or affected_date),
                end_date=str(payload.get("end_date") or affected_date),
                risk_level=str(payload.get("risk_level") or "HIGH"),
            )
            await send_render_to_channel(interaction, OPS_CHANNEL_ID, render)
            await asyncio.to_thread(
                synchronize_incident_message,
                str(evidence["alert_key"]),
                "",
                None,
                "",
                str(payload.get("status") or "pending"),
            )
            outcome = DiscordInteractionResult(
                status="success",
                action="draft",
                target_ref=self.alert_ref,
                alert_key=str(evidence["alert_key"]),
                message=tr("Approval request {request_id} posted to dq-ops-private. No remediation ran.", request_id=payload['request_id']),
                details={"request_id": str(payload["request_id"])},
            )
            await asyncio.to_thread(write_interaction_audit, context, outcome)
            await interaction.followup.send(outcome.message, ephemeral=True)
        except Exception as exc:
            await safe_component_error(interaction, context, exc)


class RejectReasonModal(discord.ui.Modal):
    """Require a human rationale before rejecting an approval request."""

    reason = discord.ui.TextInput(
        label="Reason for rejection",
        min_length=5,
        max_length=500,
        style=discord.TextStyle.paragraph,
    )

    def __init__(
        self,
        request_id: str,
        origin_message: discord.Message | None,
        default_reason: str = "",
    ) -> None:
        super().__init__(title=tr("Reject approval request"), timeout=300)
        self.reason.label = tr("Reason for rejection")
        self.request_id    = request_id
        self.origin_message = origin_message
        if default_reason.strip():
            self.reason.default = default_reason.strip()[:500]

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await perform_approval_decision(
            interaction=interaction,
            request_id=self.request_id,
            decision="reject",
            comment=str(self.reason.value).strip(),
            origin_message=self.origin_message,
        )


# --- Defining Approval Confirmation
class ApprovalConfirmationView(discord.ui.View):
    """Short-lived confirmation step before an approval decision is persisted."""

    def __init__(self, request_id: str, origin_message: discord.Message | None) -> None:
        super().__init__(timeout=120)
        self.request_id     = request_id
        self.origin_message = origin_message
        self.confirm.label = tr("Confirm approval")
        self.cancel.label = tr("Cancel")

    @discord.ui.button(label="Confirm approval", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await perform_approval_decision(
            interaction=interaction,
            request_id=self.request_id,
            decision="approve",
            comment="Confirmed through the DataSentry approval control.",
            origin_message=self.origin_message,
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    @personal_copy
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        context = interaction_context(interaction, "approve", self.request_id)
        outcome = DiscordInteractionResult(
            status="cancelled",
            action="approve",
            target_ref=self.request_id,
            message=tr("Approval confirmation cancelled. No state changed."),
        )
        await asyncio.to_thread(write_interaction_audit, context, outcome)
        await interaction.edit_original_response(content=outcome.message, embed=None, view=None)
        self.stop()


@personal_copy
async def perform_approval_decision(
    interaction: discord.Interaction,
    request_id: str,
    decision: str,
    comment: str,
    origin_message: discord.Message | None,
) -> None:
    """Persist approval state, refresh the card, and never execute remediation."""
    context = interaction_context(interaction, decision, request_id)
    try:
        validate_interaction_scope(
            interaction,
            allowed_channels={OPS_CHANNEL_ID} if OPS_CHANNEL_ID else set(),
            require_approver=True,
        )

        payload = await asyncio.to_thread(
            decide_discord_approval_request,
            request_id,
            decision,
            operator_identity(interaction),
            comment,
        )
        status = str(payload["status"])
        render = render_approval_embed(
            request_id=str(payload["request_id"]),
            status=status,
            summary=(
                    tr("{decision} decision recorded by `{actor}`. The decision is durable, but no remediation was executed.",
                       decision=tr(decision.title()), actor=payload['decided_by'])
            ),
            next_step=(
                tr("An operator may separately review the Airflow dispatcher boundary.")
                if status == "approved"
                else tr("This rejected request cannot be executed.")
            ),
            target_dag_id=str(payload.get("target_dag_id") or ""),
            start_date=str(payload.get("start_date") or ""),
            end_date=str(payload.get("end_date") or ""),
            risk_level=str(payload.get("risk_level") or "HIGH"),
        )

        if origin_message is not None:
            await origin_message.edit(
                content=None,
                embeds=[discord.Embed.from_dict(item) for item in render.payload.get("embeds", [])],
                view=build_discord_view(render),
                allowed_mentions=discord.AllowedMentions.none(),
            )

        approval_parameters = payload.get("parameters") or {}
        approval_alert_key  = str(
            payload.get("alert_key")
            or approval_parameters.get("alert_key")
            or ""
        )
        if approval_alert_key:
            await asyncio.to_thread(
                synchronize_incident_message,
                approval_alert_key,
                "",
                None,
                "",
                status,
            )

        outcome = DiscordInteractionResult(
            status="success",
            action=decision,
            target_ref=request_id,
            alert_key=approval_alert_key,
            message=tr("{decision} recorded for {request_id}. No Airflow DAG was triggered.", decision=tr(decision.title()), request_id=request_id),
            details={"approval_status": status},
        )
        await asyncio.to_thread(write_interaction_audit, context, outcome)
        await interaction.followup.send(outcome.message, ephemeral=True)
    except PermissionError as exc:
        outcome = DiscordInteractionResult(
            status="rejected",
            action=decision,
            target_ref=request_id,
            message=str(exc),
        )
        await asyncio.to_thread(write_interaction_audit, context, outcome, type(exc).__name__)
        if interaction.response.is_done():
            await interaction.followup.send(outcome.message, ephemeral=True)
        else:
            await interaction.response.send_message(outcome.message, ephemeral=True)
    except Exception as exc:
        await safe_component_error(interaction, context, exc)


# --- Defining Persistent Dynamic Buttons
class IncidentActionButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=INCIDENT_ACTION_PATTERN,
):
    """Persistent incident action reconstructed from a bounded custom ID."""

    def __init__(self, action: str, target: str, request_id: str = "") -> None:
        labels = {
            "investigate": tr("Investigate"),
            "reinvestigate": tr("Run New Investigation"),
            "ask": tr("Ask AI"),
            "evidence": tr("Evidence"),
            "report": tr("View Report"),
            "draft": tr("Draft Action"),
            "resolve": tr("Resolve"),
            "resolve_check": tr("Check Again"),
            "resolve_status": tr("Check Status"),
            "resolve_close": tr("Close Incident"),
            "resolve_evidence": tr("View Evidence"),
            "resolve_cancel": tr("Cancel"),
        }
        styles = {
            "investigate": discord.ButtonStyle.primary,
            "reinvestigate": discord.ButtonStyle.secondary,
            "ask": discord.ButtonStyle.secondary,
            "evidence": discord.ButtonStyle.secondary,
            "report": discord.ButtonStyle.secondary,
            "draft": discord.ButtonStyle.secondary,
            "resolve": discord.ButtonStyle.secondary,
            "resolve_check": discord.ButtonStyle.primary,
            "resolve_status": discord.ButtonStyle.secondary,
            "resolve_close": discord.ButtonStyle.success,
            "resolve_evidence": discord.ButtonStyle.secondary,
            "resolve_cancel": discord.ButtonStyle.secondary,
        }
        if request_id and action not in {"resolve_check", "resolve_status", "resolve_close", "resolve_evidence"}:
            raise ValueError("This incident action does not accept a request identifier.")
        if action in {"resolve_check", "resolve_status", "resolve_close", "resolve_evidence"} and not request_id:
            raise ValueError("Resolution controls require a request identifier or check nonce.")
        custom_id = f"dq:v1:{action}:{target}" + (f":{request_id}" if request_id else "")
        if len(custom_id) > 100 or not INCIDENT_ACTION_PATTERN.fullmatch(custom_id):
            raise ValueError("Invalid or oversized Discord action identifier.")
        super().__init__(
            discord.ui.Button(
                label=labels[action],
                style=styles[action],
                custom_id=custom_id,
            )
        )
        self.action = action
        self.target = target
        self.request_id = request_id

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Item[Any],
        match: re.Match[str],
    ) -> "IncidentActionButton":
        return cls(action=match["action"], target=match["target"], request_id=match["request_id"] or "")

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.action.startswith("resolve"):
            from apps.discord_bot.resolution import handle_resolution_action

            await handle_resolution_action(interaction, self.action, self.target, self.request_id)
            return
        context = interaction_context(interaction, self.action, self.target)

        try:
            validate_interaction_scope(
                interaction,
                allowed_channels={value for value in {ALERTS_CHANNEL_ID, TRIAGE_CHANNEL_ID} if value},
            )

            if self.action == "ask":
                await interaction.response.defer(thinking=True, ephemeral=True)
                runtime = get_workspace_runtime(interaction.client, GUILD_ID, TRIAGE_CHANNEL_ID)
                canonical = await runtime.bind_thread(getattr(interaction, "channel", None), self.target)
                workspace_link = ""
                if canonical or not isinstance(getattr(interaction, "channel", None), discord.Thread):
                    workspace_link = (await runtime.ensure(self.target)).url
                async with operator_language(interaction.guild_id, interaction.user.id):
                    modal = AskIncidentModal(self.target, interaction.message, interaction.user.id)
                    view = ModalLaunchView(modal, interaction.user.id, tr("Ask AI"))
                    await interaction.followup.send(tr("Open the question form for this incident.") + (f"\n{workspace_link}" if workspace_link else ""), view=view, ephemeral=True,
                                                    allowed_mentions=discord.AllowedMentions.none())
                return

            if self.action == "draft":
                await interaction.response.defer(thinking=True, ephemeral=True)
                async with operator_language(interaction.guild_id, interaction.user.id):
                    view = ModalLaunchView(DraftActionModal(self.target), interaction.user.id, tr("Draft Action"))
                    await interaction.followup.send(tr("Open the approval proposal form. No remediation will run."),
                                                    view=view, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
                return

            await interaction.response.defer(thinking=True, ephemeral=True)

            if self.action in {"investigate", "reinvestigate"}:
                await run_workspace_investigation(interaction, self.target, rerun=self.action == "reinvestigate")
                return

            runtime = get_workspace_runtime(interaction.client, GUILD_ID, TRIAGE_CHANNEL_ID)
            await runtime.bind_thread(getattr(interaction, "channel", None), self.target)
            workspace = await runtime.ensure(self.target)
            files = []
            if self.action == "evidence":
                evidence = await asyncio.to_thread(fetch_discord_evidence_context, self.target)
                if evidence["alert_key"] != workspace.state.alert_key:
                    raise ValueError("Evidence returned a different incident.")
                async with operator_language(interaction.guild_id, interaction.user.id):
                    render = render_evidence_embed(evidence)
                outcome = DiscordInteractionResult(
                    status="success",
                    action=self.action,
                    target_ref=self.target,
                    alert_key=str(evidence["alert_key"]),
                    message=tr("Evidence posted in the incident thread: {url}", url=workspace.url),
                )

            elif self.action == "report":
                report_context = await asyncio.to_thread(fetch_discord_debug_context, self.target)
                if report_context["alert_key"] != workspace.state.alert_key:
                    raise ValueError("Report context returned a different incident.")
                async with operator_language(interaction.guild_id, interaction.user.id):
                    render = render_report_reference_embed(report_context)
                file_items = await asyncio.to_thread(workspace_module.fetch_discord_report_files, workspace.state.alert_ref)
                files = report_attachments(file_items)
                outcome = DiscordInteractionResult(
                    status="success",
                    action=self.action,
                    target_ref=self.target,
                    alert_key=str(report_context["alert_key"]),
                    message=tr("Report posted in the incident thread: {url}", url=workspace.url),
                )

            else:
                raise ValueError("Unsupported incident action.")

            await send_render_to_thread(workspace.thread, render, files=files)
            await asyncio.to_thread(write_interaction_audit, context, outcome)
            await interaction.followup.send(outcome.message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        except Exception as exc:
            await safe_component_error(interaction, context, exc)


class ApprovalActionButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=APPROVAL_ACTION_PATTERN,
):
    """Persistent approval control reconstructed from a bounded request ID."""

    def __init__(self, action: str, target: str) -> None:
        labels = {"view_plan": tr("View Plan"), "approve": tr("Approve"), "reject": tr("Reject")}
        styles = {
            "view_plan": discord.ButtonStyle.secondary,
            "approve": discord.ButtonStyle.success,
            "reject": discord.ButtonStyle.danger,
        }
        super().__init__(
            discord.ui.Button(
                label=labels[action],
                style=styles[action],
                custom_id=f"dq:v1:{action}:{target}",
            )
        )
        self.action = action
        self.target = target

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Item[Any],
        match: re.Match[str],
    ) -> "ApprovalActionButton":
        return cls(action=match["action"], target=match["target"])

    async def callback(self, interaction: discord.Interaction) -> None:
        context = interaction_context(interaction, self.action, self.target)

        try:
            validate_interaction_scope(
                interaction,
                allowed_channels={OPS_CHANNEL_ID} if OPS_CHANNEL_ID else set(),
                require_approver=self.action in {"approve", "reject"},
            )

            if self.action == "approve":
                await interaction.response.defer(thinking=True, ephemeral=True)
                async with operator_language(interaction.guild_id, interaction.user.id):
                    await interaction.followup.send(
                        tr("Confirm this approval decision. This records approval state only and does not execute remediation."),
                        view=ApprovalConfirmationView(self.target, interaction.message),
                        ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
                    )
                return

            if self.action == "reject":
                await interaction.response.defer(thinking=True, ephemeral=True)
                async with operator_language(interaction.guild_id, interaction.user.id):
                    view = ModalLaunchView(RejectReasonModal(self.target, interaction.message), interaction.user.id, tr("Reject"))
                    await interaction.followup.send(tr("Open the rejection reason form."), view=view, ephemeral=True,
                                                    allowed_mentions=discord.AllowedMentions.none())
                return

            await interaction.response.defer(thinking=True, ephemeral=True)
            payload = await asyncio.to_thread(fetch_discord_approval_request, self.target)
            async with operator_language(interaction.guild_id, interaction.user.id):
                await respond_with_render(interaction, render_approval_plan_embed(payload), ephemeral=True)
            outcome = DiscordInteractionResult(
                status="success",
                action=self.action,
                target_ref=self.target,
                alert_key=str(payload.get("alert_key") or ""),
                message=tr("Approval plan displayed ephemerally."),
            )
            await asyncio.to_thread(write_interaction_audit, context, outcome)
        except Exception as exc:
            await safe_component_error(interaction, context, exc)


# --- Building Gateway Views
def action_to_item(action: DiscordActionSpec) -> discord.ui.Item[Any]:
    """Build one gateway item from the shared typed action contract."""
    if action.style == "link":
        return discord.ui.Button(
            label=action.label,
            style=discord.ButtonStyle.link,
            url=action.url,
            disabled=action.disabled,
            emoji=action.emoji or None,
        )

    if action.custom_id.startswith("dqrec:"):
        from apps.discord_bot.recovery import RecoveryButton, PATTERN
        match = PATTERN.fullmatch(action.custom_id)
        if match is None:
            raise ValueError("Unknown recovery action")
        return RecoveryButton(match["action"], match["target"])
    if action.custom_id.startswith("dqhelp:"):
        from apps.discord_bot.help import HelpActionButton, PATTERN
        match = PATTERN.fullmatch(action.custom_id)
        if match is None:
            raise ValueError("Unknown help action")
        item = HelpActionButton(match["topic"], int(match["page"]), match["nav"] or "")
        item.item.label = action.label
        item.item.style = BUTTON_STYLES[action.style]
        item.item.disabled = action.disabled
        return item

    incident_match = INCIDENT_ACTION_PATTERN.match(action.custom_id)
    if incident_match:
        item = IncidentActionButton(
            incident_match["action"], incident_match["target"], incident_match["request_id"] or "",
        )
        item.item.disabled = action.disabled
        item.item.label    = action.label
        item.item.style    = BUTTON_STYLES[action.style]
        item.item.emoji    = action.emoji or None
        return item

    approval_match = APPROVAL_ACTION_PATTERN.match(action.custom_id)
    if approval_match:
        item = ApprovalActionButton(approval_match["action"], approval_match["target"])
        item.item.disabled = action.disabled
        item.item.label    = action.label
        item.item.style    = BUTTON_STYLES[action.style]
        item.item.emoji    = action.emoji or None
        return item

    raise ValueError(f"Unsupported Discord action id: {action.action_id}")


def build_discord_view(result: DiscordRenderResult) -> discord.ui.View | None:
    """Build a persistent gateway view matching the REST component payload."""
    if not result.action_rows:
        return None

    view = discord.ui.View(timeout=None)
    for row_index, row in enumerate(result.action_rows[:5]):
        for action in row.actions[:5]:
            item     = action_to_item(action)
            item.row = row_index
            view.add_item(item)

    return view


def register_persistent_interactions(client: discord.Client) -> None:
    """Register dynamic handlers before the gateway starts receiving interactions."""
    from apps.discord_bot.help import HelpActionButton
    from apps.discord_bot.recovery import RecoveryButton
    client.add_dynamic_items(IncidentActionButton, ApprovalActionButton, HelpActionButton, RecoveryButton)
