####
## Discord Alert Webhook for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import urlparse
from uuid import UUID, uuid4

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from agent.tools.alerts import query_alert_rows
from agent.tools.audit_log import write_agent_audit_event
from apps.discord_bot.formatters import format_alert_summary, trim_message
from pipelines.common.clickhouse import build_clickhouse_client, quote_sql_literal, scalar
from pipelines.common.logging import logger
from pipelines.seeding.helpers import iter_dates, parse_date


# --- Defining Constants
DISCORD_WEBHOOK_AUDIT_ACTION = "discord_alert_webhook_sent"
DISCORD_WEBHOOK_TOOL_NAME    = "discord_webhook"
DISCORD_WEBHOOK_MESSAGE_LIMIT = 1900

DEFAULT_WEBHOOK_USERNAME        = "DataSentry"
DEFAULT_WEBHOOK_TIMEOUT_SECONDS = 10.0
DEFAULT_WEBHOOK_MAX_ALERTS      = 20

RETRYABLE_STATUS_CODES = (429, 500, 502, 503, 504)
ALLOWED_WEBHOOK_DOMAINS = ("discord.com", "discordapp.com")


# --- Defining Exceptions
class DiscordWebhookDeliveryError(RuntimeError):
    """
    Represent a sanitized Discord webhook delivery failure.

    The exception deliberately excludes the webhook URL because the URL contains
    a credential-like token and may be rendered in Airflow task logs.
    """


# --- Defining Data Models
@dataclass(frozen=True)
class DiscordWebhookSettings:
    """
    Runtime settings for bounded Discord alert delivery.

    Attributes:
        webhook_url: Secret Discord webhook URL. An empty value disables delivery.
        username: Display name used by the Discord webhook message.
        timeout_seconds: HTTP request timeout in seconds.
        max_alerts_per_date: Maximum open alerts delivered for each business date.
    """

    webhook_url: str
    username: str
    timeout_seconds: float
    max_alerts_per_date: int


# --- Loading Configuration
def load_webhook_settings() -> DiscordWebhookSettings:
    """
    Load Discord webhook settings from environment variables.

    Returns:
        Validated runtime settings. A blank webhook URL means delivery is disabled.

    Raises:
        ValueError: If numeric settings are outside their safe bounds.
    """
    timeout_seconds     = float(os.getenv("DISCORD_WEBHOOK_TIMEOUT_SECONDS", DEFAULT_WEBHOOK_TIMEOUT_SECONDS))
    max_alerts_per_date = int(os.getenv("DISCORD_WEBHOOK_MAX_ALERTS", DEFAULT_WEBHOOK_MAX_ALERTS))

    if not 1.0 <= timeout_seconds <= 60.0:
        raise ValueError("DISCORD_WEBHOOK_TIMEOUT_SECONDS must be between 1 and 60 seconds.")

    if not 1 <= max_alerts_per_date <= 100:
        raise ValueError("DISCORD_WEBHOOK_MAX_ALERTS must be between 1 and 100.")

    return DiscordWebhookSettings(
        webhook_url=os.getenv("DISCORD_ALERT_WEBHOOK_URL", "").strip(),
        username=os.getenv("DISCORD_WEBHOOK_USERNAME", DEFAULT_WEBHOOK_USERNAME).strip()
        or DEFAULT_WEBHOOK_USERNAME,
        timeout_seconds=timeout_seconds,
        max_alerts_per_date=max_alerts_per_date,
    )


def validate_discord_webhook_url(webhook_url: str) -> str:
    """
    Validate that a webhook URL targets an official Discord HTTPS endpoint.

    Args:
        webhook_url: Discord webhook URL loaded from a secret environment value.

    Returns:
        Normalized webhook URL.

    Raises:
        ValueError: If the URL is not an official Discord webhook endpoint.
    """
    normalized = webhook_url.strip()
    parsed     = urlparse(normalized)
    hostname   = (parsed.hostname or "").lower()

    official_domain = any(
        hostname == domain or hostname.endswith(f".{domain}")
        for domain in ALLOWED_WEBHOOK_DOMAINS
    )

    if parsed.scheme != "https" or not official_domain:
        raise ValueError("Discord webhook must use HTTPS on an official Discord domain.")

    if not parsed.path.startswith("/api/webhooks/"):
        raise ValueError("Discord webhook URL must use the /api/webhooks/ endpoint path.")

    if parsed.username or parsed.password:
        raise ValueError("Discord webhook URL must not contain URL user credentials.")

    return normalized


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
        allowed_methods=frozenset({"POST"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )

    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()

    session.mount("https://", adapter)

    return session


def build_webhook_payload(message: str, username: str) -> dict[str, Any]:
    """
    Build a safe Discord webhook payload.

    Args:
        message: Human-readable Discord Markdown content.
        username: Display name shown for the webhook sender.

    Returns:
        JSON payload with all Discord mentions explicitly disabled.
    """
    return {
        "content": trim_message(message, limit=DISCORD_WEBHOOK_MESSAGE_LIMIT),
        "username": username,
        "allowed_mentions": {"parse": []},
    }


def post_webhook_message(
    session: requests.Session,
    webhook_url: str,
    payload: dict[str, Any],
    timeout_seconds: float,
) -> int:
    """
    Post one alert message to Discord.

    Args:
        session: Retry-enabled requests session.
        webhook_url: Validated Discord webhook URL.
        payload: JSON message payload.
        timeout_seconds: HTTP request timeout in seconds.

    Returns:
        Successful Discord HTTP status code.

    Raises:
        requests.RequestException: If Discord rejects the request after retries.
    """
    response = session.post(
        webhook_url,
        json=payload,
        timeout=timeout_seconds,
    )

    # Raise only after bounded retry and rate-limit handling have completed.
    response.raise_for_status()

    return int(response.status_code)


# --- Building Idempotency Guardrails
def build_notification_lookup_sql(alert_key: str) -> str:
    """
    Build a bounded query that checks whether an alert was already delivered.

    Args:
        alert_key: Stable system alert key used as the delivery idempotency key.

    Returns:
        ClickHouse scalar query for a successful prior webhook audit event.
    """
    return f"""
        SELECT count()
        FROM dq.agent_audit_log
        WHERE action = {quote_sql_literal(DISCORD_WEBHOOK_AUDIT_ACTION)}
          AND status = 'success'
          AND alert_key = {quote_sql_literal(alert_key)}
    """


def was_alert_notified(client: Any, alert_key: str) -> bool:
    """
    Check whether a successful Discord webhook event already exists.

    Args:
        client: clickhouse-connect client instance.
        alert_key: Stable alert key being evaluated.

    Returns:
        True when the alert was previously delivered successfully.
    """
    delivered_count = int(scalar(client, build_notification_lookup_sql(alert_key), default=0) or 0)

    return delivered_count > 0


# --- Delivering Alerts
def deliver_alert(
    client: Any,
    session: requests.Session,
    settings: DiscordWebhookSettings,
    alert: dict[str, Any],
    agent_run_id: UUID,
) -> dict[str, Any]:
    """
    Deliver one alert once and persist the result in the agent audit log.

    Args:
        client: clickhouse-connect client used for deduplication and audit writes.
        session: Retry-enabled requests session.
        settings: Validated Discord webhook settings.
        alert: Alert row loaded from ClickHouse.
        agent_run_id: Batch run identifier shared by all delivery audit events.

    Returns:
        Delivery result containing status, alert key, alert reference, and HTTP code.

    Raises:
        DiscordWebhookDeliveryError: If delivery fails after bounded HTTP retries.
    """
    alert_key = str(alert.get("alert_key") or "").strip()
    alert_ref = str(alert.get("alert_display_id") or alert_key)

    if not alert_key:
        raise ValueError("Discord webhook delivery requires a non-empty alert_key.")

    if was_alert_notified(client=client, alert_key=alert_key):
        logger.info("Skipping previously delivered Discord alert | alert_ref=%s", alert_ref)

        return {
            "status": "skipped_existing",
            "alert_key": alert_key,
            "alert_ref": alert_ref,
            "http_status": None,
        }

    payload       = build_webhook_payload(format_alert_summary(alert), settings.username)
    endpoint_host = urlparse(settings.webhook_url).hostname or "discord.com"
    started_at    = time.monotonic()

    logger.info("Sending Discord alert webhook | alert_ref=%s endpoint_host=%s", alert_ref, endpoint_host)

    try:
        http_status = post_webhook_message(
            session=session,
            webhook_url=settings.webhook_url,
            payload=payload,
            timeout_seconds=settings.timeout_seconds,
        )

    except Exception as exc:
        duration_ms = int((time.monotonic() - started_at) * 1000)
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        error_type  = type(exc).__name__

        # Never log or persist the webhook URL because its path contains a secret token.
        logger.error(
            "Discord webhook delivery failed | alert_ref=%s error_type=%s status_code=%s",
            alert_ref,
            error_type,
            status_code,
        )

        write_agent_audit_event(
            client=client,
            action=DISCORD_WEBHOOK_AUDIT_ACTION,
            status="failed",
            agent_run_id=agent_run_id,
            alert_id=alert.get("alert_id"),
            alert_key=alert_key,
            actor="airflow",
            tool_name=DISCORD_WEBHOOK_TOOL_NAME,
            duration_ms=duration_ms,
            input_payload={"alert_ref": alert_ref, "endpoint_host": endpoint_host},
            output_payload={"error_type": error_type, "http_status": status_code},
            error_message=f"{error_type}; http_status={status_code}",
        )

        raise DiscordWebhookDeliveryError(
            f"Discord webhook delivery failed for {alert_ref}; error_type={error_type}; http_status={status_code}."
        ) from None

    duration_ms = int((time.monotonic() - started_at) * 1000)

    write_agent_audit_event(
        client=client,
        action=DISCORD_WEBHOOK_AUDIT_ACTION,
        status="success",
        agent_run_id=agent_run_id,
        alert_id=alert.get("alert_id"),
        alert_key=alert_key,
        actor="airflow",
        tool_name=DISCORD_WEBHOOK_TOOL_NAME,
        duration_ms=duration_ms,
        input_payload={"alert_ref": alert_ref, "endpoint_host": endpoint_host},
        output_payload={"http_status": http_status},
        row_count=1,
    )

    logger.info(
        "Discord alert webhook sent | alert_ref=%s http_status=%s duration_ms=%d",
        alert_ref,
        http_status,
        duration_ms,
    )

    return {
        "status": "sent",
        "alert_key": alert_key,
        "alert_ref": alert_ref,
        "http_status": http_status,
    }


def run_discord_webhook_notifications(
    dates: list[date],
    settings: DiscordWebhookSettings,
    client: Any | None = None,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """
    Deliver open alerts for one or more dates through an idempotent webhook flow.

    Args:
        dates: Business dates whose open alerts should be delivered.
        settings: Discord webhook runtime settings.
        client: Optional ClickHouse client override used by tests.
        session: Optional requests session override used by tests.

    Returns:
        Batch summary with sent, skipped, and discovered alert counts.

    Raises:
        ValueError: If a configured webhook URL is invalid.
        DiscordWebhookDeliveryError: If a configured delivery fails after retries.
    """
    if not settings.webhook_url:
        logger.info("Discord alert webhook skipped | reason=webhook_url_not_configured")

        return {
            "status": "skipped",
            "reason": "webhook_url_not_configured",
            "dates": [item.isoformat() for item in dates],
            "alerts_discovered": 0,
            "sent": 0,
            "skipped_existing": 0,
        }

    validated_url   = validate_discord_webhook_url(settings.webhook_url)
    runtime_settings = DiscordWebhookSettings(
        webhook_url=validated_url,
        username=settings.username,
        timeout_seconds=settings.timeout_seconds,
        max_alerts_per_date=settings.max_alerts_per_date,
    )
    runtime_client  = client or build_clickhouse_client()
    runtime_session = session or build_retry_session()
    owns_session    = session is None
    agent_run_id    = uuid4()
    results         = []
    seen_alert_keys = set()
    discovered      = 0

    logger.info(
        "Starting Discord webhook notification batch | dates=%s max_alerts_per_date=%d agent_run_id=%s",
        [item.isoformat() for item in dates],
        settings.max_alerts_per_date,
        agent_run_id,
    )

    try:
        for run_dt in dates:
            _, alerts = query_alert_rows(
                client=runtime_client,
                status="open",
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

    sent             = sum(item["status"] == "sent" for item in results)
    skipped_existing = sum(item["status"] == "skipped_existing" for item in results)
    summary          = {
        "status": "success",
        "agent_run_id": str(agent_run_id),
        "dates": [item.isoformat() for item in dates],
        "alerts_discovered": discovered,
        "unique_alerts": len(seen_alert_keys),
        "sent": sent,
        "skipped_existing": skipped_existing,
    }

    logger.info("Discord webhook notification batch completed | summary=%s", summary)

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
    Build the command-line parser for Discord webhook delivery.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(description="Push open DQ alerts to a configured Discord webhook.")

    parser.add_argument("--dt", default=None, help="Single business date in YYYY-MM-DD format.")
    parser.add_argument("--start", default=None, help="Inclusive start date for backfill notification checks.")
    parser.add_argument("--end", default=None, help="Inclusive end date for backfill notification checks.")

    return parser


def main() -> None:
    """
    Parse CLI arguments and execute Discord webhook delivery.

    Returns:
        None.
    """
    parser = build_parser()
    args   = parser.parse_args()

    try:
        dates    = resolve_run_dates(dt=args.dt, start=args.start, end=args.end)
        settings = load_webhook_settings()
        summary  = run_discord_webhook_notifications(dates=dates, settings=settings)

    except ValueError as exc:
        parser.error(str(exc))

    print(json.dumps(summary, indent=2, default=str))


# --- Running CLI Entrypoint
if __name__ == "__main__":
    main()
