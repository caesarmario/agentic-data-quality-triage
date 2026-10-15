####
## Evidence-Gated Discord Resolution for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Human closure controls; the shared API owns validation and lifecycle decisions."""

from __future__ import annotations

import asyncio
import hashlib
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator

import discord

from apps.common.control_plane import ControlPlaneClientError
from apps.common.i18n import current_language, language_context, translate as tr
from apps.discord_bot import interactions
from apps.discord_bot.contracts import DiscordInteractionResult, DiscordRenderResult
from apps.discord_bot.preferences import get_user_language
from apps.discord_bot.formatters import (
    render_resolution_confirmation_embed,
    render_resolution_evidence_embed,
    render_resolution_preview_embed,
    sanitize_discord_text,
)
from apps.discord_bot.service import (
    create_discord_revalidation,
    fetch_discord_resolution_preview,
    fetch_discord_revalidation,
    resolve_discord_incident,
)
from pipelines.common.logging import logger


_LANGUAGE_LOOKUP_TIMEOUT = 0.75


@asynccontextmanager
async def _resolution_language(interaction: discord.Interaction) -> AsyncIterator[None]:
    """Bound preference reads, including before a modal's acknowledgement deadline."""
    language = "en"
    guild_id = getattr(interaction, "guild_id", None)
    user_id = getattr(getattr(interaction, "user", None), "id", None)
    if guild_id and user_id:
        try:
            selected = await asyncio.wait_for(
                asyncio.to_thread(get_user_language, guild_id, user_id), timeout=_LANGUAGE_LOOKUP_TIMEOUT,
            )
            if selected in {"en", "id"}:
                language = selected
        except Exception as exc:
            logger.warning("Discord resolution language lookup failed safely | error_type=%s", type(exc).__name__)
    with language_context(language):
        yield


def _utc_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (TypeError, ValueError):
        return None


def request_proof_is_fresh(request: dict[str, Any], now: datetime | None = None) -> bool:
    """UI gating only: the API must enforce scope, TTL, and race safety again."""
    now = now or datetime.now(timezone.utc)
    finished = _utc_time(request.get("finished_at"))
    expires = _utc_time(request.get("expires_at"))
    result = request.get("result")
    refs = result.get("evidence_references") if isinstance(result, dict) else None
    return bool(
        request.get("status") == "pass" and finished and expires
        and 0 <= (now - finished).total_seconds() <= 600 and now < expires
        and isinstance(result, dict) and result.get("status") == "pass" and str(result.get("summary") or "").strip()
        and isinstance(refs, list) and refs and all(isinstance(ref, str) and ref.strip() for ref in refs)
    )


def closure_blocker(preview: dict[str, Any], request: dict[str, Any]) -> str:
    """A rendered PASS does not override a stale episode or backend blocker."""
    if str(preview.get("status") or "").lower() == "resolved":
        return tr('This incident is already closed. Open Resolve again to view its history.')
    if preview.get("available") is not True:
        return tr('The check is unavailable. The incident cannot be closed yet.')
    if preview.get("blockers"):
        return tr('Work or conditions still block closure. Open Workspace to review them.')
    if (preview.get("alert_ref") != request.get("alert_ref")
            or preview.get("incident_revision") != request.get("incident_revision")
            or preview.get("scope") != request.get("scope")):
        return tr('The incident has changed. Check again before closing it.')
    latest = preview.get("request") or {}
    if latest.get("request_id") != request.get("request_id"):
        return tr('This is not the latest closure check request. Open Resolve again.')
    if request.get("status") != "pass":
        return tr('The check has not passed. The incident cannot be closed yet.')
    if not request_proof_is_fresh(request):
        return tr('Evidence is incomplete or expired. Check again before closing the incident.')
    return ""


def _operator_ui_url() -> str:
    return os.getenv("DISCORD_OPERATOR_UI_URL", "").strip()


def render_revalidation_result(request: dict[str, Any]) -> DiscordRenderResult:
    """Serializable ops notification for the Airflow runner; never invokes an LLM.

    The durable request should include its authoritative ``scope`` for table/date
    display. Every rendered button reloads the request and preview when clicked.
    """
    preview = {
        "alert_ref": request["alert_ref"],
        "incident_revision": request["incident_revision"],
        "status": "open", "scope": dict(request.get("scope") or {}),
        "available": True, "blockers": list(request.get("blockers") or []),
        "request": request,
    }
    nonce = hashlib.sha256(str(request["request_id"]).encode()).hexdigest()[:32]
    with language_context("en"):
        return render_resolution_preview_embed(
            preview, request, can_close=not closure_blocker(preview, request),
            operator_ui_url=_operator_ui_url(), check_nonce=nonce, show_cancel=False,
        )


def _allowed_channels() -> set[int]:
    return {value for value in {
        interactions.ALERTS_CHANNEL_ID, interactions.TRIAGE_CHANNEL_ID, interactions.OPS_CHANNEL_ID,
    } if value}


def _message_custom_ids(message: discord.Message) -> set[str]:
    ids: set[str] = set()
    for row in getattr(message, "components", []):
        for button in getattr(row, "children", []):
            custom_id = getattr(button, "custom_id", None)
            if custom_id:
                ids.add(str(custom_id))
    return ids


def _verify_incident_message(interaction: discord.Interaction, alert_ref: str, custom_id: str) -> None:
    """Check the actual bot-authored component and displayed target, not client state."""
    message = interaction.message
    if not getattr(message, "id", None) or getattr(getattr(message, "channel", None), "id", None) != interaction.channel_id:
        raise PermissionError(tr('The incident message cannot be verified.'))
    if (interaction.data or {}).get("custom_id") != custom_id or custom_id not in _message_custom_ids(message):
        raise PermissionError(tr('The button does not match the DataSentry message.'))
    refs = set()
    for embed in getattr(message, "embeds", []):
        fields = embed.to_dict().get("fields", [])
        refs.update(str(field.get("value") or "").strip("` ") for field in fields if field.get("name") == tr('Alert Ref'))
    if alert_ref not in refs:
        raise PermissionError(tr("The button's incident does not match the message."))


async def require_resolution_operator_access(interaction: discord.Interaction) -> None:
    """Final decisions require current ops-channel visibility and optional approver role."""
    if not interactions.OPS_CHANNEL_ID or not interaction.guild_id:
        raise PermissionError(tr('dq-ops-private access is not configured.'))
    channel = interaction.client.get_channel(interactions.OPS_CHANNEL_ID)
    if channel is None:
        channel = await interaction.client.fetch_channel(interactions.OPS_CHANNEL_ID)
    if (getattr(getattr(channel, "guild", None), "id", None) != interaction.guild_id
            or not channel.permissions_for(interaction.user).view_channel):
        raise PermissionError(tr('You need dq-ops-private access to close an incident.'))
    role_ids = interactions.configured_approver_role_ids()
    if role_ids and not role_ids.intersection({int(role.id) for role in getattr(interaction.user, "roles", [])}):
        raise PermissionError(tr('You do not have an authorized approver role.'))


@dataclass(frozen=True)
class ResolutionOrigin:
    """Server-held correlation for a modal and its explicit confirmation."""

    alert_ref: str
    request_id: str
    requester_id: int
    guild_id: int
    channel_id: int
    message_id: int
    author_id: int

    def validate(self, interaction: discord.Interaction) -> None:
        if (interaction.user.id != self.requester_id or interaction.guild_id != self.guild_id
                or interaction.channel_id != self.channel_id
                or getattr(interaction.client.user, "id", None) != self.author_id or not self.message_id):
            raise PermissionError(tr('Only the operator who opened this panel can confirm closure.'))


async def _resolution_error(interaction: discord.Interaction, action: str, alert_ref: str, exc: Exception) -> None:
    """Never display raw API errors, URLs, authorization headers, or credentials."""
    if isinstance(exc, PermissionError):
        text = tr('You cannot use this control. Verify that the message is from DataSentry and that you have the required access.')
    elif isinstance(exc, ValueError):
        text = sanitize_discord_text(tr(str(exc)), 400)
    elif isinstance(exc, ControlPlaneClientError):
        text = tr('The system has not confirmed this request. Check Resolve or Workspace; do not assume the incident is closed.')
    else:
        text = tr('The check is unavailable. Review Workspace or contact an operator.')
    logger.warning("Discord resolution failed safely | action=%s error_type=%s", action, type(exc).__name__)
    outcome = DiscordInteractionResult("rejected" if isinstance(exc, (PermissionError, ValueError)) else "failed", action, alert_ref, text)
    sender = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
    await sender(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
    await asyncio.to_thread(interactions.write_interaction_audit, interactions.interaction_context(interaction, action, alert_ref), outcome, type(exc).__name__)


async def _load_current_request(alert_ref: str, request_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    preview = await asyncio.to_thread(fetch_discord_resolution_preview, alert_ref)
    request = await asyncio.to_thread(fetch_discord_revalidation, request_id)
    if request.get("alert_ref") != alert_ref:
        raise ValueError(tr('The check does not match the selected incident.'))
    return preview, request


class ResolutionReasonModal(discord.ui.Modal):
    """Collect a reason; submission itself must never close an incident."""

    def __init__(self, origin: ResolutionOrigin) -> None:
        super().__init__(title=tr('Incident closure reason'), timeout=300)
        self.origin = origin
        self.reason = discord.ui.TextInput(
            label=tr("Why can this incident be closed?"), min_length=5, max_length=1000,
            style=discord.TextStyle.paragraph,
            placeholder=tr("Example: missing segments reloaded; the latest check passed."),
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        async with _resolution_language(interaction):
            await self._submit(interaction)

    async def _submit(self, interaction: discord.Interaction) -> None:
        """Validate the original owner and current proof before offering confirmation."""
        try:
            interactions.validate_interaction_scope(interaction, _allowed_channels(), require_bot_authored_message=False)
            self.origin.validate(interaction)
            await require_resolution_operator_access(interaction)
            reason = str(self.reason.value).strip()
            if not 5 <= len(reason) <= 1000:
                raise ValueError(tr('A reason must contain 5-1,000 characters after trimming whitespace.'))
            preview, request = await _load_current_request(self.origin.alert_ref, self.origin.request_id)
            blocker = closure_blocker(preview, request)
            if blocker:
                raise ValueError(blocker)
            render = render_resolution_confirmation_embed(preview, request, reason)
            embed = discord.Embed.from_dict(render.payload["embeds"][0])
            view = ResolutionConfirmationView(self.origin, request["incident_revision"], reason)
            view.confirmation_message = await interaction.followup.send(
                embed=embed, view=view, ephemeral=True, wait=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            outcome = DiscordInteractionResult("success", "resolve_reason", self.origin.alert_ref, tr('Reason ready for confirmation; no incident has been closed.'))
            await asyncio.to_thread(interactions.write_interaction_audit, interactions.interaction_context(interaction, "resolve_reason", self.origin.alert_ref), outcome)
        except Exception as exc:
            await _resolution_error(interaction, "resolve_reason", self.origin.alert_ref, exc)


class ResolutionConfirmationView(discord.ui.View):
    """Bound one final decision to the original operator, request, and revision."""

    def __init__(self, origin: ResolutionOrigin, incident_revision: int, reason: str) -> None:
        super().__init__(timeout=120)
        self.origin = origin
        self.incident_revision = incident_revision
        self.reason = reason
        self._lock = asyncio.Lock()
        self._decided = False
        self._expired = False
        self.language = current_language()
        self.confirmation_message: discord.WebhookMessage | None = None
        self.confirm.label = tr("Confirm Close")
        self.cancel.label = tr("Cancel")
        self.idempotency_key = hashlib.sha256(
            f"discord-resolve:{origin.guild_id}:{origin.requester_id}:{origin.request_id}:{incident_revision}".encode()
        ).hexdigest()

    async def _retire_confirmation(self, content: str, interaction: discord.Interaction | None = None) -> None:
        """Retire local intent first; a failed UI edit must not alter the decision."""
        for control in self.children:
            control.disabled = True
        self.stop()
        try:
            # Leave the original embed (including the operator reason) intact.
            if self.confirmation_message is not None:
                await self.confirmation_message.edit(
                    content=content, view=self, allowed_mentions=discord.AllowedMentions.none(),
                )
            elif interaction is not None:
                await interaction.edit_original_response(
                    content=content, view=self, allowed_mentions=discord.AllowedMentions.none(),
                )
        except Exception as exc:
            logger.warning("Discord confirmation edit failed safely | error_type=%s", type(exc).__name__)

    async def on_timeout(self) -> None:
        """Expiration is UI-only and cannot race a committed or cancelled decision."""
        async with self._lock:
            if self._decided or self._expired:
                return
            self._expired = True
            with language_context(self.language):
                await self._retire_confirmation(tr(
                    'Confirmation expired. Open Resolve again to review the latest status before trying to close this incident.'
                ))

    @discord.ui.button(label="Confirm Close", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        async with _resolution_language(interaction):
            await self._confirm(interaction)

    async def _confirm(self, interaction: discord.Interaction) -> None:
        """Re-check authority and PASS before one idempotent closure decision."""
        async with self._lock:
            try:
                interactions.validate_interaction_scope(interaction, _allowed_channels())
                self.origin.validate(interaction)
                await require_resolution_operator_access(interaction)
                if self._expired:
                    await interaction.followup.send(tr('Confirmation expired. Open Resolve again to review the latest status before trying to close this incident.'), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
                    return
                if self._decided:
                    await interaction.followup.send(tr('This confirmation is already complete. No new decision was made.'), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
                    return
                preview, request = await _load_current_request(self.origin.alert_ref, self.origin.request_id)
                blocker = closure_blocker(preview, request)
                if request.get("incident_revision") != self.incident_revision or blocker:
                    raise ValueError(blocker or tr('The incident revision has changed. Check again.'))
                payload = await asyncio.to_thread(
                    resolve_discord_incident, self.origin.alert_ref, self.origin.request_id,
                    self.incident_revision, self.idempotency_key, interactions.operator_identity(interaction), self.reason,
                )
                self._decided = True
                await self._retire_confirmation(tr('Closure saved through the shared API.'), interaction)
                resolution = dict(payload.get("resolution") or payload)
                resolution.setdefault("result", request.get("result") or {})
                resolution.setdefault("resolution_request_id", self.origin.request_id)
                resolved_preview = {**preview, "status": "resolved", "resolution": resolution,
                                    "synchronization_pending": bool(payload.get("synchronization_pending"))}
                await interactions.respond_with_render(
                    interaction, render_resolution_preview_embed(resolved_preview, operator_ui_url=_operator_ui_url()), ephemeral=True,
                )
                outcome = DiscordInteractionResult(
                    "success", "resolve_confirm", self.origin.alert_ref, tr('Closure saved through the shared API.'),
                    details={"resolution_request_id": self.origin.request_id, "synchronization_pending": resolved_preview["synchronization_pending"]},
                )
                await asyncio.to_thread(interactions.write_interaction_audit, interactions.interaction_context(interaction, "resolve_confirm", self.origin.alert_ref), outcome)
            except Exception as exc:
                await _resolution_error(interaction, "resolve_confirm", self.origin.alert_ref, exc)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer()
        async with _resolution_language(interaction):
            await self._cancel(interaction)

    async def _cancel(self, interaction: discord.Interaction) -> None:
        """Cancel only this owner's confirmation, not the incident or validation."""
        try:
            interactions.validate_interaction_scope(interaction, _allowed_channels())
            self.origin.validate(interaction)
            async with self._lock:
                if self._expired:
                    await interaction.followup.send(tr('Confirmation expired. Open Resolve again to review the latest status before trying to close this incident.'), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
                    return
                if self._decided:
                    await interaction.followup.send(tr('This panel is complete. Check Resolve for the latest status.'), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
                    return
                self._decided = True
                await self._retire_confirmation(tr('Confirmation cancelled. No incident closure or data repairs.'), interaction)
                outcome = DiscordInteractionResult("cancelled", "resolve_cancel", self.origin.alert_ref, tr('Confirmation cancelled; no closure decision was made.'))
                await asyncio.to_thread(interactions.write_interaction_audit, interactions.interaction_context(interaction, "resolve_cancel", self.origin.alert_ref), outcome)
        except Exception as exc:
            await _resolution_error(interaction, "resolve_cancel", self.origin.alert_ref, exc)


async def handle_resolution_action(
    interaction: discord.Interaction, action: str, alert_ref: str, request_id: str = "",
) -> None:
    """Acknowledge promptly; never wait for Airflow completion in the gateway."""
    if action != "resolve_close":
        if action == "resolve_cancel" and getattr(getattr(interaction.message, "flags", None), "ephemeral", False):
            await interaction.response.defer()
        else:
            await interaction.response.defer(thinking=True, ephemeral=True)
    async with _resolution_language(interaction):
        await _handle_resolution_action(interaction, action, alert_ref, request_id)


async def _handle_resolution_action(
    interaction: discord.Interaction, action: str, alert_ref: str, request_id: str,
) -> None:
    """Keep the original guild, component, owner, scope and lifecycle checks intact."""
    try:
        interactions.validate_interaction_scope(interaction, _allowed_channels())
        custom_id = f"dq:v1:{action}:{alert_ref}" + (f":{request_id}" if request_id else "")
        _verify_incident_message(interaction, alert_ref, custom_id)
        if action == "resolve_close":
            # A modal must be opened before Discord's response deadline. Re-load
            # authoritative state and permissions on submit, before confirmation.
            origin = ResolutionOrigin(alert_ref, request_id, interaction.user.id, interaction.guild_id,
                                      interaction.channel_id, interaction.message.id, interaction.message.author.id)
            await interaction.response.send_modal(ResolutionReasonModal(origin))
            outcome = DiscordInteractionResult("success", action, alert_ref, tr('Reason modal displayed; no closure decision was made.'))
            await asyncio.to_thread(interactions.write_interaction_audit, interactions.interaction_context(interaction, action, alert_ref), outcome)
            return
        if action == "resolve_cancel":
            if getattr(getattr(interaction.message, "flags", None), "ephemeral", False):
                await interaction.edit_original_response(
                    content=tr('Panel closed. The incident and check request are unchanged.'), embeds=[], view=None,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                await interaction.followup.send(
                    tr('The incident and request are unchanged. The status message remains stored.'),
                    ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
                )
            outcome = DiscordInteractionResult("cancelled", action, alert_ref, tr('Panel closed; no lifecycle or request changes.'))
            await asyncio.to_thread(interactions.write_interaction_audit, interactions.interaction_context(interaction, action, alert_ref), outcome)
            return
        preview = await asyncio.to_thread(fetch_discord_resolution_preview, alert_ref)
        request = preview.get("request")
        if action == "resolve_check":
            if (preview.get("available") is not True or preview.get("blockers")
                    or str(preview.get("status") or "").lower() == "resolved"):
                raise ValueError(tr('The check cannot start yet. Open Resolve again to review its status and blockers.'))
            key = hashlib.sha256(
                f"discord-check:{interaction.guild_id}:{interaction.user.id}:{interaction.message.id}:{request_id}".encode()
            ).hexdigest()
            request = await asyncio.to_thread(create_discord_revalidation, alert_ref, interactions.operator_identity(interaction), key)
            preview = {**preview, "request": request}
        elif action in {"resolve_status", "resolve_evidence"}:
            request = await asyncio.to_thread(fetch_discord_revalidation, request_id)
            if request.get("alert_ref") != alert_ref:
                raise ValueError(tr('The check does not match the selected incident.'))
            if action == "resolve_evidence":
                render = render_resolution_evidence_embed(preview, request)
                await interactions.respond_with_render(interaction, render, ephemeral=True)
                outcome = DiscordInteractionResult("success", action, alert_ref, tr('Evidence displayed without changing the incident.'), details={"resolution_request_id": request_id})
                await asyncio.to_thread(interactions.write_interaction_audit, interactions.interaction_context(interaction, action, alert_ref), outcome)
                return
        elif action != "resolve":
            raise ValueError(tr('Unknown closure control.'))
        blocker = closure_blocker(preview, request) if request else ""
        render = render_resolution_preview_embed(
            preview, request, can_close=bool(request and not blocker),
            blocked_message=blocker if request and request.get("status") == "pass" else "",
            operator_ui_url=_operator_ui_url(),
        )
        await interactions.respond_with_render(interaction, render, ephemeral=True)
        outcome = DiscordInteractionResult("success", action, alert_ref, tr('Closure status displayed without data repairs.'),
                                           details={"resolution_request_id": str((request or {}).get("request_id") or "")})
        await asyncio.to_thread(interactions.write_interaction_audit, interactions.interaction_context(interaction, action, alert_ref), outcome)
    except Exception as exc:
        await _resolution_error(interaction, action, alert_ref, exc)
