####
## Discord Incident Message Registry for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Durable ClickHouse registry for idempotent Discord incident messages."""

# --- Importing Libraries
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from apps.discord_bot.contracts import DiscordIncidentMessageState
from pipelines.common.clickhouse import quote_sql_literal


# --- Defining Constants
DISCORD_INCIDENT_MESSAGES_TABLE = "dq.discord_incident_messages"

DISCORD_INCIDENT_MESSAGE_COLUMNS = [
    "alert_key",
    "alert_display_id",
    "channel_id",
    "message_id",
    "rendered_lifecycle",
    "content_hash",
    "narrative_source",
    "transport",
    "author_id",
    "last_http_status",
    "last_error_state",
    "created_at",
    "updated_at",
]


def _decode_clickhouse_text(value: Any) -> str:
    """Normalize String and FixedString values returned by clickhouse-connect."""
    if isinstance(value, bytes):
        return value.rstrip(b"\x00").decode("utf-8")

    return str(value or "")


# --- Loading Registry State
def build_incident_message_lookup_sql(alert_key: str) -> str:
    """Build a bounded lookup for the latest message state of one alert."""
    return f"""
        SELECT
            alert_key,
            alert_display_id,
            channel_id,
            message_id,
            rendered_lifecycle,
            content_hash,
            narrative_source,
            transport,
            author_id,
            last_http_status,
            last_error_state,
            created_at,
            updated_at
        FROM {DISCORD_INCIDENT_MESSAGES_TABLE} FINAL
        WHERE alert_key = {quote_sql_literal(alert_key)}
        ORDER BY updated_at DESC
        LIMIT 1
    """


def load_incident_message_state(
    client: Any,
    alert_key: str,
) -> DiscordIncidentMessageState | None:
    """Load the latest durable Discord state for one stable alert key."""
    result = client.query(build_incident_message_lookup_sql(alert_key))

    if not result.result_rows:
        return None

    row = dict(zip(result.column_names, result.result_rows[0]))

    return DiscordIncidentMessageState(
        alert_key=str(row.get("alert_key") or ""),
        alert_ref=str(row.get("alert_display_id") or ""),
        channel_id=str(row.get("channel_id") or ""),
        message_id=str(row.get("message_id") or ""),
        rendered_lifecycle=str(row.get("rendered_lifecycle") or "OPEN"),
        content_hash=_decode_clickhouse_text(row.get("content_hash")),
        narrative_source=str(row.get("narrative_source") or "deterministic_fallback"),
        transport=str(row.get("transport") or "legacy_webhook"),
        author_id=str(row.get("author_id") or ""),
        last_http_status=(
            int(row["last_http_status"])
            if row.get("last_http_status") is not None
            else None
        ),
        last_error_state=str(row.get("last_error_state") or ""),
        created_at=row.get("created_at") or datetime.now(timezone.utc),
        updated_at=row.get("updated_at") or datetime.now(timezone.utc),
    )


# --- Persisting Registry State
def write_incident_message_state(
    client: Any,
    state: DiscordIncidentMessageState,
) -> None:
    """Append one replay-safe replacement row for a Discord incident message."""
    client.insert(
        DISCORD_INCIDENT_MESSAGES_TABLE,
        [[
            state.alert_key,
            state.alert_ref,
            state.channel_id,
            state.message_id,
            state.rendered_lifecycle,
            state.content_hash,
            state.narrative_source,
            state.transport,
            state.author_id,
            state.last_http_status,
            state.last_error_state,
            state.created_at,
            state.updated_at,
        ]],
        column_names=DISCORD_INCIDENT_MESSAGE_COLUMNS,
    )


def build_failed_message_state(
    previous: DiscordIncidentMessageState,
    error_type: str,
    http_status: int | None,
) -> DiscordIncidentMessageState:
    """Create a sanitized failure revision without persisting raw response text."""
    return DiscordIncidentMessageState(
        alert_key=previous.alert_key,
        alert_ref=previous.alert_ref,
        channel_id=previous.channel_id,
        message_id=previous.message_id,
        rendered_lifecycle=previous.rendered_lifecycle,
        content_hash=previous.content_hash,
        narrative_source=previous.narrative_source,
        transport=previous.transport,
        author_id=previous.author_id,
        last_http_status=http_status,
        last_error_state=error_type,
        created_at=previous.created_at,
        updated_at=datetime.now(timezone.utc),
    )
