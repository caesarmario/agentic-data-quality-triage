####
## Discord Bot Delivery for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from agent.tools.alerts import query_alert_rows
from agent.tools.audit_log import write_agent_audit_event
from apps.discord_bot.contracts import DiscordIncidentMessageState, DiscordRenderResult
from apps.discord_bot.formatters import (
    build_discord_incident_payload,
    render_incident_embed,
    trim_message,
)
from apps.discord_bot.registry import (
    build_failed_message_state,
    load_incident_message_state,
    write_incident_message_state,
)
from agent.tools.s3 import parse_s3_uri
from pipelines.common.clickhouse import build_clickhouse_client, quote_sql_literal, scalar
from pipelines.common.logging import logger
from pipelines.seeding.helpers import iter_dates, parse_date
from pipelines.seeding.upload_to_s3 import build_s3_client


# --- Defining Constants
DISCORD_DELIVERY_AUDIT_ACTION = "discord_incident_message_synced"
DISCORD_DELIVERY_TOOL_NAME    = "discord_bot_api"
DISCORD_MESSAGE_LIMIT         = 1900
DISCORD_API_ROOT              = "https://discord.com/api/v10"

DEFAULT_DELIVERY_TIMEOUT_SECONDS = 10.0
DEFAULT_DELIVERY_MAX_ALERTS      = 20

RETRYABLE_STATUS_CODES = (429, 500, 502, 503, 504)


# --- Defining Exceptions
class DiscordBotDeliveryError(RuntimeError):
    """
    Represent a sanitized Discord bot delivery failure.

    The exception deliberately excludes the bot token because the message may be
    rendered in Airflow task logs.
    """


# --- Defining Data Models
@dataclass(frozen=True)
class DiscordBotDeliverySettings:
    """
    Runtime settings for bounded Discord bot delivery.

    Attributes:
        bot_token: Secret token for the existing DataSentry bot application.
        alerts_channel_id: Discord channel that receives DQ incident cards.
        timeout_seconds: HTTP request timeout in seconds.
        max_alerts_per_date: Maximum open alerts delivered for each business date.
    """

    bot_token: str
    alerts_channel_id: str
    timeout_seconds: float
    max_alerts_per_date: int


# --- Loading Configuration
def load_delivery_settings() -> DiscordBotDeliverySettings:
    """
    Load Discord bot delivery settings from environment variables.

    Returns:
        Validated runtime settings. A blank token or channel disables delivery.

    Raises:
        ValueError: If numeric settings are outside their safe bounds.
    """
    timeout_seconds     = float(os.getenv("DISCORD_DELIVERY_TIMEOUT_SECONDS", DEFAULT_DELIVERY_TIMEOUT_SECONDS))
    max_alerts_per_date = int(os.getenv("DISCORD_DELIVERY_MAX_ALERTS", DEFAULT_DELIVERY_MAX_ALERTS))

    if not 1.0 <= timeout_seconds <= 60.0:
        raise ValueError("DISCORD_DELIVERY_TIMEOUT_SECONDS must be between 1 and 60 seconds.")

    if not 1 <= max_alerts_per_date <= 100:
        raise ValueError("DISCORD_DELIVERY_MAX_ALERTS must be between 1 and 100.")

    return DiscordBotDeliverySettings(
        bot_token=os.getenv("DISCORD_BOT_TOKEN", "").strip(),
        alerts_channel_id=os.getenv("DISCORD_ALERTS_CHANNEL_ID", "").strip(),
        timeout_seconds=timeout_seconds,
        max_alerts_per_date=max_alerts_per_date,
    )


# --- Building HTTP Transport
def build_retry_session() -> requests.Session:
    """
    Build a requests session with bounded retry and backoff behavior.

    Returns:
        Requests session configured to retry Discord rate limits and transient errors.
    """
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=0.5,
        status_forcelist=RETRYABLE_STATUS_CODES,
        # A POST can create a message even when the response is lost. Retrying it
        # inside urllib3 would bypass the durable pending-create reconciliation
        # below and could create a duplicate incident card.
        allowed_methods=frozenset({"GET", "PATCH"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )

    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()

    session.mount("https://", adapter)

    return session


def build_message_payload(message: str) -> dict[str, Any]:
    """
    Build a safe Discord bot message payload.

    Args:
        message: Human-readable Discord Markdown content.
    Returns:
        JSON payload with all Discord mentions explicitly disabled.
    """
    return {
        "content": trim_message(message, limit=DISCORD_MESSAGE_LIMIT),
        "allowed_mentions": {"parse": []},
    }


def build_embed_message_payload(render_result: DiscordRenderResult) -> dict[str, Any]:
    """Build the primary bot payload from a bounded render result."""
    payload = dict(render_result.payload)
    payload["allowed_mentions"] = {"parse": []}

    return payload


def _response_json(response: requests.Response) -> dict[str, Any]:
    """Read a Discord response object without retaining raw response content."""
    payload = response.json()

    if not isinstance(payload, dict):
        raise ValueError("Discord returned an invalid message response.")

    return payload


def build_bot_headers(bot_token: str) -> dict[str, str]:
    """Build Discord bot authentication headers without logging the token."""
    return {
        "Authorization": f"Bot {bot_token}",
        "Content-Type": "application/json",
        "User-Agent": "DataSentry (https://github.com/caesarmario/agentic-data-quality-triage, 1.0)",
    }


def channel_messages_url(channel_id: str) -> str:
    """Return the fixed official Discord message endpoint for one channel."""
    normalized = channel_id.strip()
    if not normalized.isdigit():
        raise ValueError("Discord channel id must contain digits only.")
    return f"{DISCORD_API_ROOT}/channels/{normalized}/messages"


def post_bot_message(
    session: requests.Session,
    bot_token: str,
    channel_id: str,
    payload: dict[str, Any],
    timeout_seconds: float,
) -> tuple[int, dict[str, Any]]:
    """
    Post one alert message to Discord.

    Args:
        session: Retry-enabled requests session.
        bot_token: Existing DataSentry bot token.
        channel_id: Destination Discord channel ID.
        payload: JSON message payload.
        timeout_seconds: HTTP request timeout in seconds.

    Returns:
        Successful Discord HTTP status code.

    Raises:
        requests.RequestException: If Discord rejects the request after retries.
    """
    canonical_payload = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    request_payload = dict(payload)
    request_payload["nonce"] = hashlib.sha256(
        canonical_payload.encode("utf-8")
    ).hexdigest()[:25]
    request_payload["enforce_nonce"] = True

    response = session.post(
        channel_messages_url(channel_id),
        headers=build_bot_headers(bot_token),
        json=request_payload,
        timeout=timeout_seconds,
    )

    # Raise only after bounded retry and rate-limit handling have completed.
    response.raise_for_status()

    return int(response.status_code), _response_json(response)


def patch_bot_message(
    session: requests.Session,
    bot_token: str,
    channel_id: str,
    message_id: str,
    payload: dict[str, Any],
    timeout_seconds: float,
) -> tuple[int, dict[str, Any]]:
    """Update one message authored by the existing DataSentry bot."""
    message_url = f"{channel_messages_url(channel_id)}/{message_id}"
    response    = session.patch(
        message_url,
        headers=build_bot_headers(bot_token),
        json=payload,
        timeout=timeout_seconds,
    )

    response.raise_for_status()

    return int(response.status_code), _response_json(response)


def get_bot_message(
    session: requests.Session,
    bot_token: str,
    channel_id: str,
    message_id: str,
    timeout_seconds: float,
) -> tuple[int, dict[str, Any]]:
    """Read one known Discord message so a content-hash skip cannot hide deletion."""
    response = session.get(
        f"{channel_messages_url(channel_id)}/{message_id}",
        headers=build_bot_headers(bot_token),
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    return int(response.status_code), _response_json(response)


def find_recent_incident_message(
    session: requests.Session,
    bot_token: str,
    channel_id: str,
    alert_ref: str,
    timeout_seconds: float,
) -> dict[str, Any] | None:
    """Reconcile an uncertain POST by finding a recent bot card for one Alert Ref."""
    response = session.get(
        f"{channel_messages_url(channel_id)}?limit=100",
        headers=build_bot_headers(bot_token),
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    payload = response.json()

    if not isinstance(payload, list):
        raise ValueError("Discord returned an invalid channel message list.")

    for message in payload:
        if not isinstance(message, dict) or message.get("webhook_id"):
            continue

        author = message.get("author") or {}
        if not isinstance(author, dict) or not author.get("bot"):
            continue

        for embed in message.get("embeds") or []:
            if not isinstance(embed, dict):
                continue

            for field in embed.get("fields") or []:
                if not isinstance(field, dict):
                    continue

                if str(field.get("name") or "").strip() == "Alert Ref" and alert_ref in str(
                    field.get("value") or ""
                ):
                    return message

    return None


def validate_bot_message_response(
    payload: dict[str, Any],
    expected_channel_id: str,
) -> tuple[str, str, str]:
    """Validate that Discord returned a message authored by a bot, not a webhook."""
    if not isinstance(payload, dict):
        raise ValueError("Discord returned an invalid message response.")

    message_id = str(payload.get("id") or "").strip()
    channel_id = str(payload.get("channel_id") or "").strip()
    author     = payload.get("author") or {}
    author_id  = str(author.get("id") or "").strip() if isinstance(author, dict) else ""
    author_bot = bool(author.get("bot")) if isinstance(author, dict) else False

    if not message_id:
        raise ValueError("Discord did not return a message id.")

    if channel_id != expected_channel_id:
        raise ValueError("Discord returned a message for an unexpected channel.")

    if payload.get("webhook_id"):
        raise ValueError("Discord returned a webhook-authored message for bot delivery.")

    if not author_id or not author_bot:
        raise ValueError("Discord did not confirm a bot-authored message.")

    return message_id, channel_id, author_id


# --- Building Idempotency Guardrails
def build_notification_lookup_sql(alert_key: str) -> str:
    """
    Build a bounded query that checks whether an alert was already delivered.

    Args:
        alert_key: Stable system alert key used as the delivery idempotency key.

    Returns:
        ClickHouse scalar query for a successful prior bot delivery audit event.
    """
    return f"""
        SELECT count()
        FROM dq.agent_audit_log
        WHERE action = {quote_sql_literal(DISCORD_DELIVERY_AUDIT_ACTION)}
          AND status = 'success'
          AND alert_key = {quote_sql_literal(alert_key)}
    """


def was_alert_notified(client: Any, alert_key: str) -> bool:
    """
    Check whether a successful Discord bot delivery event already exists.

    Args:
        client: clickhouse-connect client instance.
        alert_key: Stable alert key being evaluated.

    Returns:
        True when the alert was previously delivered successfully.
    """
    delivered_count = int(scalar(client, build_notification_lookup_sql(alert_key), default=0) or 0)

    return delivered_count > 0


def report_json_uri(report_s3_uri: str) -> str:
    """Resolve a persisted triage Markdown or JSON URI to its JSON companion."""
    normalized = report_s3_uri.strip()

    if normalized.endswith("/report.md"):
        return normalized[:-len("report.md")] + "report.json"

    return normalized if normalized.endswith("/report.json") else ""


def load_persisted_incident_narrative(alert: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
    """Load allowlisted narrative fields from the alert's persisted report artifact."""
    report_uri = report_json_uri(str(alert.get("report_s3_uri") or ""))

    if not report_uri:
        return "", {}, ""

    try:
        bucket, key = parse_s3_uri(report_uri)
        response    = build_s3_client().get_object(Bucket=bucket, Key=key)
        report      = json.loads(response["Body"].read())

        if str((report.get("alert") or {}).get("alert_key") or "") != str(alert.get("alert_key") or ""):
            raise ValueError("Persisted report alert identity does not match the incident.")

        investigation = report.get("ai_investigation") or {}
        runtime       = report.get("llm_runtime") or {}

        if not isinstance(investigation, dict) or not isinstance(runtime, dict):
            raise ValueError("Persisted report narrative metadata is malformed.")

        runtime = dict(runtime)
        if not str(runtime.get("provider") or "").strip():
            runtime["provider"] = str(investigation.get("provider") or "")
        if not str(runtime.get("model") or "").strip():
            runtime["model"] = str(investigation.get("model") or "")

        return (
            str(investigation.get("what_happened") or "").strip(),
            runtime,
            str(investigation.get("source") or "").strip(),
        )

    except Exception as exc:
        logger.warning(
            "Persisted Discord narrative unavailable; using deterministic fallback | "
            "alert_ref=%s error_type=%s",
            alert.get("alert_display_id") or "unknown",
            type(exc).__name__,
        )
        return "", {}, ""


# --- Delivering Alerts
def deliver_alert(
    client: Any,
    session: requests.Session,
    settings: DiscordBotDeliverySettings,
    alert: dict[str, Any],
    agent_run_id: UUID,
    narrative: str = "",
    llm_runtime: dict[str, Any] | None = None,
    narrative_source: str = "",
    approval_state: str = "",
) -> dict[str, Any]:
    """
    Create, update, skip, or recreate one durable Discord incident message.

    Args:
        client: clickhouse-connect client used for deduplication and audit writes.
        session: Retry-enabled requests session.
        settings: Validated Discord bot delivery settings.
        alert: Alert row loaded from ClickHouse.
        agent_run_id: Batch run identifier shared by all delivery audit events.
        narrative: Optional grounded Copilot narrative from an existing triage run.
        llm_runtime: Sanitized runtime proof associated with the narrative.
        narrative_source: Explicit validated source such as external_model.

    Returns:
        Delivery result containing status, alert key, alert reference, and HTTP code.

    Raises:
        DiscordBotDeliveryError: If delivery fails after bounded HTTP retries.
    """
    alert_key = str(alert.get("alert_key") or "").strip()
    alert_ref = str(alert.get("alert_display_id") or alert_key)

    if not alert_key:
        raise ValueError("Discord bot delivery requires a non-empty alert_key.")

    if not narrative:
        persisted_narrative, persisted_runtime, persisted_source = load_persisted_incident_narrative(alert)
        narrative        = persisted_narrative
        llm_runtime      = persisted_runtime or llm_runtime
        narrative_source = persisted_source or narrative_source

    previous = load_incident_message_state(client=client, alert_key=alert_key)
    effective_approval_state = approval_state.strip()
    if not effective_approval_state and previous is not None:
        effective_approval_state = {
            "WAITING APPROVAL": "pending",
            "APPROVED": "approved",
            "REJECTED": "rejected",
        }.get(previous.rendered_lifecycle, "")

    from apps.common.i18n import language_context
    with language_context("en"):
        incident = build_discord_incident_payload(
            alert=alert,
            narrative=narrative,
            llm_runtime=llm_runtime,
            explicit_source=narrative_source,
            approval_state=effective_approval_state,
        )
        render_result = render_incident_embed(
            incident,
            operator_ui_url=os.getenv("DISCORD_OPERATOR_UI_URL", "").strip(),
        )
    payload       = build_embed_message_payload(render_result)
    endpoint_host = "discord.com"
    started_at    = time.monotonic()
    operation     = "created"
    pending_state: DiscordIncidentMessageState | None = None
    bot_owned_previous = bool(
        previous is not None
        and previous.transport == "discord_bot_api"
        and previous.channel_id == settings.alerts_channel_id
    )

    if (
        bot_owned_previous
        and previous is not None
        and previous.content_hash == render_result.content_hash
        and previous.rendered_lifecycle == render_result.lifecycle
    ):
        try:
            _, existing_payload = get_bot_message(
                session=session,
                bot_token=settings.bot_token,
                channel_id=settings.alerts_channel_id,
                message_id=previous.message_id,
                timeout_seconds=settings.timeout_seconds,
            )
            existing_message_id, _, existing_author_id = validate_bot_message_response(
                payload=existing_payload,
                expected_channel_id=settings.alerts_channel_id,
            )

            if existing_message_id != previous.message_id:
                raise ValueError("Discord returned an unexpected message during skip verification.")

            if previous.author_id and existing_author_id != previous.author_id:
                raise ValueError("Discord message author changed during skip verification.")

        except requests.HTTPError as exc:
            if getattr(exc.response, "status_code", None) != 404:
                raise DiscordBotDeliveryError(
                    f"Discord message verification failed for {alert_ref}."
                ) from None

            bot_owned_previous = False
            operation = "recreated_missing"

        if bot_owned_previous:
            duration_ms = int((time.monotonic() - started_at) * 1000)
            write_agent_audit_event(
                client=client,
                action=DISCORD_DELIVERY_AUDIT_ACTION,
                status="skipped",
                agent_run_id=agent_run_id,
                alert_id=alert.get("alert_id"),
                alert_key=alert_key,
                actor="airflow",
                tool_name=DISCORD_DELIVERY_TOOL_NAME,
                duration_ms=duration_ms,
                input_payload={"alert_ref": alert_ref, "endpoint_host": endpoint_host},
                output_payload={
                    "operation": "content_hash_skip_verified",
                    "message_id": previous.message_id,
                    "lifecycle": previous.rendered_lifecycle,
                },
            )

            logger.info(
                "Skipping verified unchanged Discord incident | alert_ref=%s lifecycle=%s",
                alert_ref,
                previous.rendered_lifecycle,
            )

            return {
                "status": "skipped_unchanged",
                "alert_key": alert_key,
                "alert_ref": alert_ref,
                "message_id": previous.message_id,
                "http_status": 200,
            }

    logger.info(
        "Synchronizing Discord incident | alert_ref=%s lifecycle=%s endpoint_host=%s",
        alert_ref,
        render_result.lifecycle,
        endpoint_host,
    )

    try:
        response_payload: dict[str, Any]

        if previous is not None and previous.transport == "discord_bot_api_pending":
            recovered = find_recent_incident_message(
                session=session,
                bot_token=settings.bot_token,
                channel_id=settings.alerts_channel_id,
                alert_ref=alert_ref,
                timeout_seconds=settings.timeout_seconds,
            )

            if recovered is not None:
                recovered_message_id, _, _ = validate_bot_message_response(
                    payload=recovered,
                    expected_channel_id=settings.alerts_channel_id,
                )

                if (
                    previous.content_hash == render_result.content_hash
                    and previous.rendered_lifecycle == render_result.lifecycle
                ):
                    operation        = "recovered_pending_create"
                    http_status      = 200
                    response_payload = recovered
                else:
                    operation = "recovered_pending_create_updated"
                    http_status, response_payload = patch_bot_message(
                        session=session,
                        bot_token=settings.bot_token,
                        channel_id=settings.alerts_channel_id,
                        message_id=recovered_message_id,
                        payload=payload,
                        timeout_seconds=settings.timeout_seconds,
                    )
            else:
                operation = "retried_pending_create"
                pending_state = previous
                http_status, response_payload = post_bot_message(
                    session=session,
                    bot_token=settings.bot_token,
                    channel_id=settings.alerts_channel_id,
                    payload=payload,
                    timeout_seconds=settings.timeout_seconds,
                )

        elif bot_owned_previous and previous is not None and previous.message_id:
            operation = "updated"

            try:
                http_status, response_payload = patch_bot_message(
                    session=session,
                    bot_token=settings.bot_token,
                    channel_id=settings.alerts_channel_id,
                    message_id=previous.message_id,
                    payload=payload,
                    timeout_seconds=settings.timeout_seconds,
                )

            except requests.HTTPError as exc:
                status_code = getattr(exc.response, "status_code", None)

                if status_code != 404:
                    raise

                operation = "recreated"
                pending_now   = datetime.now(timezone.utc)
                pending_state = DiscordIncidentMessageState(
                    alert_key=alert_key,
                    alert_ref=alert_ref,
                    channel_id=settings.alerts_channel_id,
                    message_id="",
                    rendered_lifecycle=render_result.lifecycle or "OPEN",
                    content_hash=render_result.content_hash,
                    narrative_source=render_result.narrative_source,
                    transport="discord_bot_api_pending",
                    author_id="",
                    last_http_status=None,
                    last_error_state="pending_create",
                    created_at=previous.created_at,
                    updated_at=pending_now,
                )
                write_incident_message_state(client=client, state=pending_state)
                http_status, response_payload = post_bot_message(
                    session=session,
                    bot_token=settings.bot_token,
                    channel_id=settings.alerts_channel_id,
                    payload=payload,
                    timeout_seconds=settings.timeout_seconds,
                )

        else:
            if previous is not None and operation == "created":
                operation = "replaced_legacy_webhook"

            pending_now   = datetime.now(timezone.utc)
            pending_state = DiscordIncidentMessageState(
                alert_key=alert_key,
                alert_ref=alert_ref,
                channel_id=settings.alerts_channel_id,
                message_id="",
                rendered_lifecycle=render_result.lifecycle or "OPEN",
                content_hash=render_result.content_hash,
                narrative_source=render_result.narrative_source,
                transport="discord_bot_api_pending",
                author_id="",
                last_http_status=None,
                last_error_state="pending_create",
                created_at=previous.created_at if previous else pending_now,
                updated_at=pending_now,
            )
            write_incident_message_state(client=client, state=pending_state)

            http_status, response_payload = post_bot_message(
                session=session,
                bot_token=settings.bot_token,
                channel_id=settings.alerts_channel_id,
                payload=payload,
                timeout_seconds=settings.timeout_seconds,
            )

        message_id, channel_id, author_id = validate_bot_message_response(
            payload=response_payload,
            expected_channel_id=settings.alerts_channel_id,
        )
        render_transport = "embed"

        # Discord can accept an embed payload while suppressing the embed in the
        # stored message because of channel permissions. Never treat that empty
        # message as a successful operator notification.
        if payload.get("embeds") and not response_payload.get("embeds"):
            fallback_payload = build_message_payload(render_result.fallback_markdown)
            fallback_status, fallback_response = patch_bot_message(
                session=session,
                bot_token=settings.bot_token,
                channel_id=settings.alerts_channel_id,
                message_id=message_id,
                payload=fallback_payload,
                timeout_seconds=settings.timeout_seconds,
            )
            fallback_message_id, _, fallback_author_id = validate_bot_message_response(
                payload=fallback_response,
                expected_channel_id=settings.alerts_channel_id,
            )

            if fallback_message_id and fallback_message_id != message_id:
                raise ValueError("Discord plain fallback changed the message identity.")

            if not str(fallback_response.get("content") or "").strip():
                raise ValueError("Discord removed both embed and plain fallback content.")

            if fallback_author_id != author_id:
                raise ValueError("Discord changed the bot author during plain fallback.")

            http_status      = fallback_status
            response_payload = fallback_response
            operation        = f"{operation}_plain_fallback"
            render_transport = "plain_markdown"

    except Exception as exc:
        duration_ms = int((time.monotonic() - started_at) * 1000)
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        error_type  = type(exc).__name__

        # Never log or persist the bot token or outbound authorization header.
        logger.error(
            "Discord bot delivery failed | alert_ref=%s error_type=%s status_code=%s",
            alert_ref,
            error_type,
            status_code,
        )

        write_agent_audit_event(
            client=client,
            action=DISCORD_DELIVERY_AUDIT_ACTION,
            status="failed",
            agent_run_id=agent_run_id,
            alert_id=alert.get("alert_id"),
            alert_key=alert_key,
            actor="airflow",
            tool_name=DISCORD_DELIVERY_TOOL_NAME,
            duration_ms=duration_ms,
            input_payload={"alert_ref": alert_ref, "endpoint_host": endpoint_host},
            output_payload={
                "operation": operation,
                "error_type": error_type,
                "http_status": status_code,
            },
            error_message=f"{error_type}; http_status={status_code}",
        )

        failure_base = pending_state or previous
        if failure_base is not None:
            write_incident_message_state(
                client=client,
                state=build_failed_message_state(
                    previous=failure_base,
                    error_type=error_type,
                    http_status=status_code,
                ),
            )

        raise DiscordBotDeliveryError(
            f"Discord bot delivery failed for {alert_ref}; error_type={error_type}; http_status={status_code}."
        ) from None

    duration_ms = int((time.monotonic() - started_at) * 1000)

    now = datetime.now(timezone.utc)
    state = DiscordIncidentMessageState(
        alert_key=alert_key,
        alert_ref=alert_ref,
        channel_id=channel_id,
        message_id=message_id,
        rendered_lifecycle=render_result.lifecycle or "OPEN",
        content_hash=render_result.content_hash,
        narrative_source=render_result.narrative_source,
        transport="discord_bot_api",
        author_id=author_id,
        last_http_status=http_status,
        last_error_state="",
        created_at=previous.created_at if previous else now,
        updated_at=now,
    )
    write_incident_message_state(client=client, state=state)

    write_agent_audit_event(
        client=client,
        action=DISCORD_DELIVERY_AUDIT_ACTION,
        status="success",
        agent_run_id=agent_run_id,
        alert_id=alert.get("alert_id"),
        alert_key=alert_key,
        actor="airflow",
        tool_name=DISCORD_DELIVERY_TOOL_NAME,
        duration_ms=duration_ms,
        input_payload={"alert_ref": alert_ref, "endpoint_host": endpoint_host},
        output_payload={
            "operation": operation,
            "http_status": http_status,
            "message_id": message_id,
            "channel_id": channel_id,
            "lifecycle": render_result.lifecycle,
            "narrative_source": render_result.narrative_source,
            "content_hash": render_result.content_hash,
            "transport": "discord_bot_api",
            "render_transport": render_transport,
            "author_id": author_id,
            "replaced_message_id": (
                previous.message_id
                if previous is not None and operation == "replaced_legacy_webhook"
                else ""
            ),
        },
        row_count=1,
    )

    logger.info(
        "Discord incident synchronized | alert_ref=%s operation=%s http_status=%s duration_ms=%d",
        alert_ref,
        operation,
        http_status,
        duration_ms,
    )

    return {
        "status": operation,
        "alert_key": alert_key,
        "alert_ref": alert_ref,
        "message_id": message_id,
        "channel_id": channel_id,
        "lifecycle": render_result.lifecycle,
        "http_status": http_status,
        "transport": "discord_bot_api",
        "render_transport": render_transport,
        "author_id": author_id,
        "replaced_message_id": (
            previous.message_id
            if previous is not None and operation == "replaced_legacy_webhook"
            else ""
        ),
    }


def synchronize_incident_message(
    alert_key: str,
    narrative: str = "",
    llm_runtime: dict[str, Any] | None = None,
    narrative_source: str = "",
    approval_state: str = "",
    settings: DiscordBotDeliverySettings | None = None,
    client: Any | None = None,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Synchronize one alert after triage or another lifecycle transition."""
    runtime_settings = settings or load_delivery_settings()

    missing = []
    if not runtime_settings.bot_token:
        missing.append("DISCORD_BOT_TOKEN")
    if not runtime_settings.alerts_channel_id:
        missing.append("DISCORD_ALERTS_CHANNEL_ID")
    if missing:
        return {"status": "skipped", "reason": "bot_delivery_not_configured", "missing": missing}
    runtime_client  = client or build_clickhouse_client()
    runtime_session = session or build_retry_session()
    owns_session    = session is None

    try:
        _, alerts = query_alert_rows(
            client=runtime_client,
            alert_key=alert_key,
            limit=1,
        )

        if not alerts:
            return {"status": "skipped", "reason": "alert_not_found"}

        return deliver_alert(
            client=runtime_client,
            session=runtime_session,
            settings=runtime_settings,
            alert=alerts[0],
            agent_run_id=uuid4(),
            narrative=narrative,
            llm_runtime=llm_runtime,
            narrative_source=narrative_source,
            approval_state=approval_state,
        )

    finally:
        if owns_session:
            runtime_session.close()


def run_discord_bot_notifications(
    dates: list[date],
    settings: DiscordBotDeliverySettings,
    client: Any | None = None,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """
    Synchronize incident lifecycle messages for one or more business dates.

    Args:
        dates: Business dates whose open alerts should be delivered.
        settings: Discord bot runtime settings.
        client: Optional ClickHouse client override used by tests.
        session: Optional requests session override used by tests.

    Returns:
        Batch summary with sent, skipped, and discovered alert counts.

    Raises:
        DiscordBotDeliveryError: If a configured delivery fails after retries.
    """
    missing = []
    if not settings.bot_token:
        missing.append("DISCORD_BOT_TOKEN")
    if not settings.alerts_channel_id:
        missing.append("DISCORD_ALERTS_CHANNEL_ID")
    if missing:
        logger.info("Discord bot delivery skipped | reason=bot_delivery_not_configured missing=%s", missing)

        return {
            "status": "skipped",
            "reason": "bot_delivery_not_configured",
            "missing": missing,
            "dates": [item.isoformat() for item in dates],
            "alerts_discovered": 0,
            "sent": 0,
            "skipped_existing": 0,
        }

    runtime_settings = settings
    runtime_client  = client or build_clickhouse_client()
    runtime_session = session or build_retry_session()
    owns_session    = session is None
    agent_run_id    = uuid4()
    results         = []
    seen_alert_keys = set()
    discovered      = 0

    logger.info(
        "Starting Discord bot notification batch | dates=%s max_alerts_per_date=%d agent_run_id=%s",
        [item.isoformat() for item in dates],
        settings.max_alerts_per_date,
        agent_run_id,
    )

    try:
        for run_dt in dates:
            _, alerts = query_alert_rows(
                client=runtime_client,
                status=None,
                dt=run_dt.isoformat(),
                limit=settings.max_alerts_per_date,
            )
            discovered += len(alerts)

            for alert in alerts:
                alert_key = str(alert.get("alert_key") or "")

                # ReplacingMergeTree lifecycle rows can be duplicated before background merges.
                if not alert_key or alert_key in seen_alert_keys:
                    continue

                seen_alert_keys.add(alert_key)
                results.append(
                    deliver_alert(
                        client=runtime_client,
                        session=runtime_session,
                        settings=runtime_settings,
                        alert=alert,
                        agent_run_id=agent_run_id,
                    )
                )

    finally:
        if owns_session:
            runtime_session.close()

    created           = sum(item["status"] == "created" for item in results)
    updated           = sum(item["status"] == "updated" for item in results)
    recreated         = sum(item["status"] == "recreated" for item in results)
    replaced_legacy   = sum(item["status"] == "replaced_legacy_webhook" for item in results)
    skipped_unchanged = sum(item["status"] == "skipped_unchanged" for item in results)
    summary          = {
        "status": "success",
        "agent_run_id": str(agent_run_id),
        "dates": [item.isoformat() for item in dates],
        "alerts_discovered": discovered,
        "unique_alerts": len(seen_alert_keys),
        "sent": created + recreated + replaced_legacy,
        "created": created,
        "updated": updated,
        "recreated": recreated,
        "replaced_legacy": replaced_legacy,
        "skipped_existing": skipped_unchanged,
        "skipped_unchanged": skipped_unchanged,
    }

    logger.info("Discord bot notification batch completed | summary=%s", summary)

    return summary


# --- Resolving CLI Dates
def resolve_run_dates(dt: str | None, start: str | None, end: str | None) -> list[date]:
    """
    Resolve one date or an inclusive date range for notification delivery.

    Args:
        dt: Optional single business date in YYYY-MM-DD format.
        start: Optional inclusive start date.
        end: Optional inclusive end date.

    Returns:
        Ordered business dates to process.

    Raises:
        ValueError: If date arguments are missing or incomplete.
    """
    if dt:
        return [parse_date(dt)]

    if not start or not end:
        raise ValueError("Provide --dt or both --start and --end.")

    return iter_dates(start_date=parse_date(start), end_date=parse_date(end))


# --- Building CLI
def build_parser() -> argparse.ArgumentParser:
    """
    Build the command-line parser for Discord bot delivery.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(description="Push open DQ alerts through the DataSentry bot.")

    parser.add_argument("--dt", default=None, help="Single business date in YYYY-MM-DD format.")
    parser.add_argument("--start", default=None, help="Inclusive start date for backfill notification checks.")
    parser.add_argument("--end", default=None, help="Inclusive end date for backfill notification checks.")
    parser.add_argument(
        "--alert-key",
        default=None,
        help="Exact Alert Ref or system alert key to synchronize after triage.",
    )

    return parser


def main() -> None:
    """
    Parse CLI arguments and execute Discord bot delivery.

    Returns:
        None.
    """
    parser = build_parser()
    args   = parser.parse_args()

    try:
        settings = load_delivery_settings()

        if args.alert_key:
            summary = synchronize_incident_message(
                alert_key=args.alert_key,
                settings=settings,
            )
        else:
            dates   = resolve_run_dates(dt=args.dt, start=args.start, end=args.end)
            summary = run_discord_bot_notifications(dates=dates, settings=settings)

    except ValueError as exc:
        parser.error(str(exc))

    print(json.dumps(summary, indent=2, default=str))


# --- Running CLI Entrypoint
if __name__ == "__main__":
    main()
