####
## Discord Bot Delivery Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import requests

from agent.tools.audit_log import AGENT_AUDIT_LOG_COLUMNS
from apps.discord_bot.contracts import DiscordIncidentMessageState
from apps.discord_bot.formatters import build_discord_incident_payload, render_incident_embed
from apps.discord_bot.registry import (
    DISCORD_INCIDENT_MESSAGE_COLUMNS,
    load_incident_message_state,
)
from apps.discord_bot.delivery import (
    DISCORD_DELIVERY_AUDIT_ACTION,
    DiscordBotDeliveryError,
    DiscordBotDeliverySettings,
    build_bot_headers,
    build_notification_lookup_sql,
    build_message_payload,
    channel_messages_url,
    deliver_alert,
    load_persisted_incident_narrative,
    report_json_uri,
    resolve_run_dates,
    run_discord_bot_notifications,
    validate_bot_message_response,
)


# --- Defining Test Doubles
class FakeQueryResult:
    """
    Minimal clickhouse-connect query result used by delivery tests.

    Attributes:
        result_rows: Query rows returned to scalar helpers.
        column_names: Optional column names associated with the rows.
    """

    def __init__(self, result_rows: list[tuple[Any, ...]], column_names: list[str] | None = None) -> None:
        """
        Initialize a fake query result.

        Args:
            result_rows: Rows returned by the fake query.
            column_names: Optional query column names.

        Returns:
            None.
        """
        self.result_rows = result_rows
        self.column_names = column_names or []


class FakeClickHouseClient:
    """
    Capture bot-delivery registry queries and agent audit inserts.

    Attributes:
    state: Optional durable Discord message state returned by registry queries.
        queries: SQL statements executed by the delivery flow.
        inserts: Insert calls written through the shared audit tool.
    """

    def __init__(self, state: DiscordIncidentMessageState | None = None) -> None:
        """
        Initialize a fake ClickHouse client.

        Args:
            state: Optional prior Discord message state.

        Returns:
            None.
        """
        self.state = state
        self.queries: list[str] = []
        self.inserts: list[dict[str, Any]] = []

    def query(self, sql: str) -> FakeQueryResult:
        """
        Return a scalar idempotency result for the supplied SQL.

        Args:
            sql: ClickHouse query text.

        Returns:
            Fake scalar query result.
        """
        self.queries.append(sql)

        if "discord_incident_messages" in sql and self.state is not None:
            row = tuple(getattr(self.state, name if name != "alert_display_id" else "alert_ref") for name in DISCORD_INCIDENT_MESSAGE_COLUMNS)
            return FakeQueryResult(result_rows=[row], column_names=DISCORD_INCIDENT_MESSAGE_COLUMNS)

        return FakeQueryResult(result_rows=[], column_names=[])

    def insert(self, table: str, data: list[list[Any]], column_names: list[str]) -> None:
        """
        Capture one ClickHouse insert call.

        Args:
            table: Target ClickHouse table.
            data: Insert rows.
            column_names: Insert column order.

        Returns:
            None.
        """
        self.inserts.append(
            {
                "table": table,
                "data": data,
                "column_names": column_names,
            }
        )


class FakeResponse:
    """
    Minimal requests response with configurable HTTP behavior.

    Attributes:
        status_code: HTTP status returned by Discord.
        failure: Whether raise_for_status should raise an HTTP error.
    """

    def __init__(
        self,
        status_code: int = 200,
        failure: bool = False,
        payload: Any | None = None,
    ) -> None:
        """
        Initialize a fake response.

        Args:
            status_code: HTTP response status.
            failure: Whether the response should raise an error.

        Returns:
            None.
        """
        self.status_code = status_code
        self.failure     = failure
        self.payload = payload if payload is not None else {
            "id": "message-123",
            "channel_id": "456",
            "author": {"id": "bot-123", "bot": True},
            "content": "",
            "embeds": [{"title": "Incident"}],
        }

    def raise_for_status(self) -> None:
        """
        Raise a requests HTTP error when failure mode is enabled.

        Returns:
            None.

        Raises:
            requests.HTTPError: When failure mode is enabled.
        """
        if self.failure:
            error          = requests.HTTPError("Discord request failed")
            error.response = self

            raise error

    def json(self) -> Any:
        """Return the bounded Discord message response."""
        if isinstance(self.payload, dict):
            return dict(self.payload)
        if isinstance(self.payload, list):
            return [dict(item) if isinstance(item, dict) else item for item in self.payload]
        return self.payload


class FakeSession:
    """
    Capture outgoing bot API requests without network access.

    Attributes:
        response: Fake response returned by post.
        posts: Captured post arguments.
        patches: Captured patch arguments.
        closed: Whether close was called.
    """

    def __init__(
        self,
        response: FakeResponse | None = None,
        patch_response: FakeResponse | None = None,
        get_responses: list[FakeResponse] | None = None,
    ) -> None:
        """
        Initialize a fake HTTP session.

        Args:
            response: Optional fake response override.

        Returns:
            None.
        """
        self.response = response or FakeResponse()
        self.patch_response = patch_response or FakeResponse()
        self.get_responses = list(get_responses or [])
        self.posts: list[dict[str, Any]] = []
        self.patches: list[dict[str, Any]] = []
        self.gets: list[dict[str, Any]] = []
        self.closed = False

    def post(
        self,
        url: str,
        headers: dict[str, str],
        json: dict[str, Any],
        timeout: float,
    ) -> FakeResponse:
        """
        Capture a Discord bot post call.

        Args:
            url: Fixed official Discord API URL.
            json: JSON payload sent to Discord.
            timeout: Request timeout in seconds.

        Returns:
            Configured fake response.
        """
        self.posts.append({"url": url, "headers": headers, "json": json, "timeout": timeout})

        return self.response

    def patch(
        self,
        url: str,
        headers: dict[str, str],
        json: dict[str, Any],
        timeout: float,
    ) -> FakeResponse:
        """Capture a Discord bot message update."""
        self.patches.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return self.patch_response

    def get(
        self,
        url: str,
        headers: dict[str, str],
        timeout: float,
    ) -> FakeResponse:
        """Capture a Discord bot message read or recent-message reconciliation."""
        self.gets.append({"url": url, "headers": headers, "timeout": timeout})

        if self.get_responses:
            return self.get_responses.pop(0)

        return FakeResponse()

    def close(self) -> None:
        """
        Record session closure.

        Returns:
            None.
        """
        self.closed = True


# --- Defining Fixtures
def sample_alert() -> dict[str, Any]:
    """
    Build a representative ClickHouse alert row.

    Returns:
        Alert dictionary accepted by the Discord formatter and bot sender.
    """
    return {
        "alert_id": str(uuid4()),
        "alert_key": "orders|dq_failure|2026-05-04|dq.raw_orders|row_count_positive|table",
        "alert_display_id": "DQ-20260504-A1B2C3",
        "severity": "critical",
        "status": "open",
        "table_name": "dq.raw_orders",
        "metric": "row_count_positive",
        "dt": "2026-05-04",
        "observed_value": 0,
        "expected_value": 1,
        "threshold_value": 1,
        "details_json": "{}",
    }


def sample_message_state(content_hash: str = "0" * 64) -> DiscordIncidentMessageState:
    """Build a prior durable message revision for update tests."""
    now = datetime.now(timezone.utc)

    return DiscordIncidentMessageState(
        alert_key=sample_alert()["alert_key"],
        alert_ref="DQ-20260504-A1B2C3",
        channel_id="456",
        message_id="message-old",
        rendered_lifecycle="OPEN",
        content_hash=content_hash,
        narrative_source="deterministic_fallback",
        created_at=now,
        updated_at=now,
    )


def sample_pending_state(content_hash: str) -> DiscordIncidentMessageState:
    """Build an uncertain create revision that must be reconciled before retry."""
    previous = sample_message_state(content_hash=content_hash)

    return DiscordIncidentMessageState(
        **{
            **previous.__dict__,
            "message_id": "",
            "transport": "discord_bot_api_pending",
            "author_id": "",
            "last_http_status": None,
            "last_error_state": "pending_create",
        }
    )


def delivery_settings() -> DiscordBotDeliverySettings:
    """
    Build valid test settings without using a real Discord credential.

    Returns:
        Discord bot settings with a fake token and numeric channel ID.
    """
    return DiscordBotDeliverySettings(
        bot_token="test-only-token",
        alerts_channel_id="456",
        timeout_seconds=5.0,
        max_alerts_per_date=20,
    )


# --- Testing URL And Payload Guardrails
def test_bot_api_endpoint_is_fixed_and_requires_numeric_channel_id() -> None:
    assert channel_messages_url("456") == "https://discord.com/api/v10/channels/456/messages"
    with pytest.raises(ValueError):
        channel_messages_url("https://example.invalid")


def test_bot_headers_use_discord_bot_authentication() -> None:
    headers = build_bot_headers("test-only-token")
    assert headers["Authorization"] == "Bot test-only-token"
    assert headers["Content-Type"] == "application/json"


def test_build_message_payload_disables_mentions_and_bounds_content() -> None:
    """
    Verify bot payloads cannot mention users or override the bot identity.

    Returns:
        None.
    """
    payload = build_message_payload("@everyone " + ("x" * 2500))

    assert len(payload["content"]) <= 1900
    assert payload["allowed_mentions"] == {"parse": []}
    assert "username" not in payload
    assert "avatar_url" not in payload


def test_bot_message_response_rejects_webhook_or_non_bot_authors() -> None:
    valid = {
        "id": "message-123",
        "channel_id": "456",
        "author": {"id": "bot-123", "bot": True},
    }
    assert validate_bot_message_response(valid, "456") == ("message-123", "456", "bot-123")
    with pytest.raises(ValueError):
        validate_bot_message_response({**valid, "webhook_id": "legacy"}, "456")
    with pytest.raises(ValueError):
        validate_bot_message_response({**valid, "author": {"id": "user", "bot": False}}, "456")


def test_notification_lookup_sql_escapes_alert_key_and_uses_success_state() -> None:
    """
    Verify deduplication SQL safely quotes alert keys and only trusts successful sends.

    Returns:
        None.
    """
    sql = build_notification_lookup_sql("orders|metric|'quoted'")

    assert "discord_incident_message_synced" in sql
    assert "status = 'success'" in sql
    assert "\\'quoted\\'" in sql


def test_persisted_report_narrative_is_identity_bound(monkeypatch) -> None:
    """Load only the allowlisted narrative for the exact incident identity."""
    class Body:
        def read(self) -> bytes:
            return json.dumps(
                {
                    "alert": {"alert_key": sample_alert()["alert_key"]},
                    "ai_investigation": {
                        "what_happened": "The latest partition is incomplete.",
                        "source": "external_model",
                    },
                    "llm_runtime": {
                        "runtime_mode": "external_model",
                        "provider": "gemini",
                        "model": "gemini-3.5-flash-lite",
                    },
                }
            ).encode("utf-8")

    class S3Client:
        def get_object(self, **kwargs: Any) -> dict[str, Any]:
            assert kwargs == {"Bucket": "dq-artifacts", "Key": "reports/report.json"}
            return {"Body": Body()}

    monkeypatch.setattr("apps.discord_bot.delivery.build_s3_client", lambda: S3Client())
    alert = {**sample_alert(), "report_s3_uri": "s3://dq-artifacts/reports/report.md"}

    narrative, runtime, source = load_persisted_incident_narrative(alert)

    assert report_json_uri(alert["report_s3_uri"]) == "s3://dq-artifacts/reports/report.json"
    assert narrative == "The latest partition is incomplete."
    assert runtime["provider"] == "gemini"
    assert source == "external_model"


# --- Testing Delivery And Auditability
def test_deliver_alert_sends_once_and_writes_secret_safe_audit_event() -> None:
    """
    Verify a new alert is posted once and audited without the bot token.

    Returns:
        None.
    """
    client   = FakeClickHouseClient()
    session  = FakeSession()
    settings = delivery_settings()
    alert    = sample_alert()

    result = deliver_alert(
        client=client,
        session=session,
        settings=settings,
        alert=alert,
        agent_run_id=uuid4(),
    )

    assert result["status"] == "created"
    assert result["http_status"] == 200
    assert result["message_id"] == "message-123"
    assert len(session.posts) == 1
    assert session.posts[0]["url"] == "https://discord.com/api/v10/channels/456/messages"
    assert session.posts[0]["headers"]["Authorization"] == "Bot test-only-token"
    assert session.posts[0]["json"]["allowed_mentions"] == {"parse": []}
    assert session.posts[0]["json"]["embeds"]
    assert session.posts[0]["json"]["enforce_nonce"] is True
    assert len(session.posts[0]["json"]["nonce"]) == 25
    assert "username" not in session.posts[0]["json"]
    assert len(client.inserts) == 3
    registry_inserts = [item for item in client.inserts if item["table"] == "dq.discord_incident_messages"]
    assert len(registry_inserts) == 2
    pending_row = dict(zip(DISCORD_INCIDENT_MESSAGE_COLUMNS, registry_inserts[0]["data"][0]))
    final_row = dict(zip(DISCORD_INCIDENT_MESSAGE_COLUMNS, registry_inserts[1]["data"][0]))
    assert pending_row["transport"] == "discord_bot_api_pending"
    assert pending_row["last_error_state"] == "pending_create"
    assert final_row["transport"] == "discord_bot_api"
    assert final_row["message_id"] == "message-123"

    insert_call = next(item for item in client.inserts if item["table"] == "dq.agent_audit_log")
    row         = dict(zip(AGENT_AUDIT_LOG_COLUMNS, insert_call["data"][0]))
    audit_text  = json.dumps(row, default=str)

    assert row["action"] == DISCORD_DELIVERY_AUDIT_ACTION
    assert row["status"] == "success"
    assert row["actor"] == "airflow"
    assert "test-only-token" not in audit_text
    assert "discord.com" in audit_text


def test_deliver_alert_skips_unchanged_content_hash(monkeypatch) -> None:
    """
    Verify Airflow retries do not resend alerts with successful audit evidence.

    Returns:
        None.
    """
    monkeypatch.delenv("DISCORD_OPERATOR_UI_URL", raising=False)
    alert   = sample_alert()
    render  = render_incident_embed(build_discord_incident_payload(alert))
    now     = datetime.now(timezone.utc)
    state   = DiscordIncidentMessageState(
        alert_key=alert["alert_key"],
        alert_ref=alert["alert_display_id"],
        channel_id="456",
        message_id="message-123",
        rendered_lifecycle="OPEN",
        content_hash=render.content_hash,
        narrative_source=render.narrative_source,
        created_at=now,
        updated_at=now,
    )
    client  = FakeClickHouseClient(state=state)
    session = FakeSession(
        get_responses=[
            FakeResponse(
                payload={
                    "id": "message-123",
                    "channel_id": "456",
                    "author": {"id": "bot-123", "bot": True},
                    "content": "",
                    "embeds": [{"title": "Incident"}],
                }
            )
        ]
    )

    result = deliver_alert(
        client=client,
        session=session,
        settings=delivery_settings(),
        alert=alert,
        agent_run_id=uuid4(),
    )

    assert result["status"] == "skipped_unchanged"
    assert result["http_status"] == 200
    assert len(session.gets) == 1
    assert session.gets[0]["url"].endswith("/channels/456/messages/message-123")
    assert session.posts == []
    assert session.patches == []
    audit_insert = next(item for item in client.inserts if item["table"] == "dq.agent_audit_log")
    audit_row = dict(zip(AGENT_AUDIT_LOG_COLUMNS, audit_insert["data"][0]))
    assert audit_row["status"] == "skipped"
    assert "test-only-token" not in json.dumps(audit_row, default=str)


def test_deliver_alert_recreates_unchanged_registry_message_when_get_returns_404(monkeypatch) -> None:
    """A matching content hash cannot skip delivery when the Discord message was deleted."""
    monkeypatch.delenv("DISCORD_OPERATOR_UI_URL", raising=False)
    alert = sample_alert()
    render = render_incident_embed(build_discord_incident_payload(alert))
    state = sample_message_state(content_hash=render.content_hash)
    state = DiscordIncidentMessageState(
        **{
            **state.__dict__,
            "message_id": "message-deleted",
            "author_id": "bot-123",
        }
    )
    session = FakeSession(
        response=FakeResponse(
            payload={
                "id": "message-replacement",
                "channel_id": "456",
                "author": {"id": "bot-123", "bot": True},
                "content": "",
                "embeds": [{"title": "Incident"}],
            }
        ),
        get_responses=[FakeResponse(status_code=404, failure=True)],
    )
    client = FakeClickHouseClient(state=state)

    result = deliver_alert(
        client=client,
        session=session,
        settings=delivery_settings(),
        alert=alert,
        agent_run_id=uuid4(),
    )

    assert result["status"] == "recreated_missing"
    assert result["message_id"] == "message-replacement"
    assert len(session.gets) == 1
    assert len(session.posts) == 1
    assert session.patches == []


def test_deliver_alert_recovers_pending_create_from_recent_bot_message(monkeypatch) -> None:
    """An uncertain POST is reconciled from recent bot messages without creating a duplicate."""
    monkeypatch.delenv("DISCORD_OPERATOR_UI_URL", raising=False)
    alert = sample_alert()
    render = render_incident_embed(build_discord_incident_payload(alert))
    recovered_message = {
        "id": "message-recovered",
        "channel_id": "456",
        "author": {"id": "bot-123", "bot": True},
        "content": "",
        "embeds": [
            {
                "title": "Incident",
                "fields": [
                    {"name": "Alert Ref", "value": f"`{alert['alert_display_id']}`"},
                ],
            }
        ],
    }
    session = FakeSession(get_responses=[FakeResponse(payload=[recovered_message])])
    client = FakeClickHouseClient(state=sample_pending_state(render.content_hash))

    result = deliver_alert(
        client=client,
        session=session,
        settings=delivery_settings(),
        alert=alert,
        agent_run_id=uuid4(),
    )

    assert result["status"] == "recovered_pending_create"
    assert result["message_id"] == "message-recovered"
    assert len(session.gets) == 1
    assert session.gets[0]["url"].endswith("/channels/456/messages?limit=100")
    assert session.posts == []
    assert session.patches == []
    registry_insert = next(
        item
        for item in reversed(client.inserts)
        if item["table"] == "dq.discord_incident_messages"
    )
    row = dict(zip(DISCORD_INCIDENT_MESSAGE_COLUMNS, registry_insert["data"][0]))
    assert row["transport"] == "discord_bot_api"
    assert row["message_id"] == "message-recovered"


def test_deliver_alert_updates_recovered_pending_message_when_lifecycle_changed(monkeypatch) -> None:
    """A recovered uncertain create is patched before a newer lifecycle is recorded."""
    monkeypatch.delenv("DISCORD_OPERATOR_UI_URL", raising=False)
    alert = sample_alert()
    recovered_message = {
        "id": "message-recovered",
        "channel_id": "456",
        "author": {"id": "bot-123", "bot": True},
        "content": "",
        "embeds": [
            {
                "title": "Incident",
                "fields": [
                    {"name": "Alert Ref", "value": f"`{alert['alert_display_id']}`"},
                ],
            }
        ],
    }
    session = FakeSession(
        patch_response=FakeResponse(
            payload={
                **recovered_message,
                "embeds": [{"title": "Triaged incident"}],
            }
        ),
        get_responses=[FakeResponse(payload=[recovered_message])],
    )
    pending = sample_pending_state("0" * 64)
    client = FakeClickHouseClient(state=pending)

    result = deliver_alert(
        client=client,
        session=session,
        settings=delivery_settings(),
        alert={**alert, "status": "triaged"},
        agent_run_id=uuid4(),
    )

    assert result["status"] == "recovered_pending_create_updated"
    assert result["message_id"] == "message-recovered"
    assert session.posts == []
    assert len(session.patches) == 1
    assert session.patches[0]["url"].endswith("/messages/message-recovered")
    registry_insert = next(
        item
        for item in reversed(client.inserts)
        if item["table"] == "dq.discord_incident_messages"
    )
    row = dict(zip(DISCORD_INCIDENT_MESSAGE_COLUMNS, registry_insert["data"][0]))
    assert row["rendered_lifecycle"] == "TRIAGED"
    assert row["transport"] == "discord_bot_api"


def test_deliver_alert_retries_pending_create_when_recent_message_is_absent(monkeypatch) -> None:
    """A pending create is posted again only after bounded recent-message reconciliation misses."""
    monkeypatch.delenv("DISCORD_OPERATOR_UI_URL", raising=False)
    alert = sample_alert()
    render = render_incident_embed(build_discord_incident_payload(alert))
    session = FakeSession(
        response=FakeResponse(
            payload={
                "id": "message-retried",
                "channel_id": "456",
                "author": {"id": "bot-123", "bot": True},
                "content": "",
                "embeds": [{"title": "Incident"}],
            }
        ),
        get_responses=[FakeResponse(payload=[])],
    )
    client = FakeClickHouseClient(state=sample_pending_state(render.content_hash))

    result = deliver_alert(
        client=client,
        session=session,
        settings=delivery_settings(),
        alert=alert,
        agent_run_id=uuid4(),
    )

    assert result["status"] == "retried_pending_create"
    assert result["message_id"] == "message-retried"
    assert len(session.gets) == 1
    assert len(session.posts) == 1
    assert session.patches == []


def test_deliver_alert_updates_existing_message_and_registry() -> None:
    """Verify changed content PATCHes the stable Discord message id."""
    client  = FakeClickHouseClient(state=sample_message_state())
    session = FakeSession(
        patch_response=FakeResponse(
            payload={
                "id": "message-old",
                "channel_id": "456",
                "author": {"id": "bot-123", "bot": True},
                "content": "",
                "embeds": [{"title": "Incident"}],
            }
        )
    )

    result = deliver_alert(
        client=client,
        session=session,
        settings=delivery_settings(),
        alert=sample_alert(),
        agent_run_id=uuid4(),
    )

    assert result["status"] == "updated"
    assert result["message_id"] == "message-old"
    assert session.posts == []
    assert session.patches[0]["url"].endswith("/messages/message-old")
    assert session.patches[0]["json"]["allowed_mentions"] == {"parse": []}
    assert any(item["table"] == "dq.discord_incident_messages" for item in client.inserts)


def test_deliver_alert_replaces_legacy_webhook_message_without_patching_it() -> None:
    """A bot cannot edit a webhook-authored message, so cutover creates a replacement."""
    legacy = sample_message_state()
    legacy = DiscordIncidentMessageState(
        **{
            **legacy.__dict__,
            "transport": "legacy_webhook",
            "author_id": "legacy-webhook-id",
        }
    )
    client = FakeClickHouseClient(state=legacy)
    session = FakeSession()

    result = deliver_alert(
        client=client,
        session=session,
        settings=delivery_settings(),
        alert=sample_alert(),
        agent_run_id=uuid4(),
    )

    assert result["status"] == "replaced_legacy_webhook"
    assert result["replaced_message_id"] == "message-old"
    assert result["author_id"] == "bot-123"
    assert len(session.posts) == 1
    assert session.patches == []


def test_deliver_alert_preserves_durable_approval_lifecycle_when_state_is_omitted() -> None:
    """A later sync must not regress an approved incident back to triaged."""
    previous = sample_message_state()
    previous = DiscordIncidentMessageState(
        **{
            **previous.__dict__,
            "rendered_lifecycle": "APPROVED",
        }
    )
    client = FakeClickHouseClient(state=previous)
    session = FakeSession(
        patch_response=FakeResponse(
            payload={
                "id": "message-old",
                "channel_id": "456",
                "author": {"id": "bot-123", "bot": True},
                "content": "",
                "embeds": [{"title": "Incident"}],
            }
        )
    )

    result = deliver_alert(
        client=client,
        session=session,
        settings=delivery_settings(),
        alert={**sample_alert(), "status": "triaged"},
        agent_run_id=uuid4(),
    )

    assert result["status"] == "updated"
    assert result["lifecycle"] == "APPROVED"


def test_deliver_alert_recreates_missing_discord_message_after_404() -> None:
    """Verify a deleted Discord message is recreated and re-registered."""
    session = FakeSession(
        response=FakeResponse(
            payload={
                "id": "message-new",
                "channel_id": "456",
                "author": {"id": "bot-123", "bot": True},
                "content": "",
                "embeds": [{"title": "Incident"}],
            }
        ),
        patch_response=FakeResponse(status_code=404, failure=True),
    )
    client = FakeClickHouseClient(state=sample_message_state())

    result = deliver_alert(
        client=client,
        session=session,
        settings=delivery_settings(),
        alert=sample_alert(),
        agent_run_id=uuid4(),
    )

    assert result["status"] == "recreated"
    assert result["message_id"] == "message-new"
    assert len(session.patches) == 1
    assert len(session.posts) == 1
    assert session.posts[0]["url"] == "https://discord.com/api/v10/channels/456/messages"


def test_deliver_alert_falls_back_to_compact_plain_text_when_embed_is_removed() -> None:
    """Treat a suppressed Discord embed as a transport fallback, not silent success."""
    client = FakeClickHouseClient()
    session = FakeSession(
        response=FakeResponse(
            payload={
                "id": "message-123",
                "channel_id": "456",
                "author": {"id": "bot-123", "bot": True},
                "content": "",
                "embeds": [],
            }
        ),
        patch_response=FakeResponse(
            payload={
                "id": "message-123",
                "channel_id": "456",
                "author": {"id": "bot-123", "bot": True},
                "content": "**Orders row count is empty** readable fallback",
                "embeds": [],
            }
        ),
    )

    result = deliver_alert(
        client=client,
        session=session,
        settings=delivery_settings(),
        alert=sample_alert(),
        agent_run_id=uuid4(),
    )

    assert result["status"] == "created_plain_fallback"
    assert result["transport"] == "discord_bot_api"
    assert result["render_transport"] == "plain_markdown"
    assert result["message_id"] == "message-123"
    assert len(session.posts) == 1
    assert len(session.patches) == 1
    fallback_payload = session.patches[0]["json"]
    assert fallback_payload["content"].startswith("**")
    assert "# " not in fallback_payload["content"]
    assert fallback_payload["allowed_mentions"] == {"parse": []}


def test_delivery_failure_raises_sanitized_error_and_writes_failed_audit() -> None:
    """
    Verify failed HTTP delivery remains retryable without leaking the bot token.

    Returns:
        None.
    """
    client   = FakeClickHouseClient()
    session  = FakeSession(response=FakeResponse(status_code=500, failure=True))
    settings = delivery_settings()

    with pytest.raises(DiscordBotDeliveryError) as exc_info:
        deliver_alert(
            client=client,
            session=session,
            settings=settings,
            alert=sample_alert(),
            agent_run_id=uuid4(),
        )

    insert_call = next(item for item in client.inserts if item["table"] == "dq.agent_audit_log")
    row         = dict(zip(AGENT_AUDIT_LOG_COLUMNS, insert_call["data"][0]))
    audit_text  = json.dumps(row, default=str)

    assert row["status"] == "failed"
    assert row["error_message"] == "HTTPError; http_status=500"
    assert "test-only-token" not in str(exc_info.value)
    assert "test-only-token" not in audit_text


def test_disabled_bot_delivery_returns_explicit_skip_without_clickhouse_or_http() -> None:
    """
    Verify missing optional bot configuration produces a safe no-op result.

    Returns:
        None.
    """
    settings = DiscordBotDeliverySettings(
        bot_token="",
        alerts_channel_id="",
        timeout_seconds=10.0,
        max_alerts_per_date=20,
    )

    result = run_discord_bot_notifications(
        dates=[date(2026, 5, 4)],
        settings=settings,
    )

    assert result == {
        "status": "skipped",
        "reason": "bot_delivery_not_configured",
        "missing": ["DISCORD_BOT_TOKEN", "DISCORD_ALERTS_CHANNEL_ID"],
        "dates": ["2026-05-04"],
        "alerts_discovered": 0,
        "sent": 0,
        "skipped_existing": 0,
    }


# --- Testing CLI And Airflow Integration
def test_resolve_run_dates_supports_single_date_and_inclusive_range() -> None:
    """
    Verify notification CLI dates align with existing operational DAG date arguments.

    Returns:
        None.
    """
    assert resolve_run_dates(dt="2026-05-04", start=None, end=None) == [date(2026, 5, 4)]
    assert resolve_run_dates(dt=None, start="2026-05-04", end="2026-05-06") == [
        date(2026, 5, 4),
        date(2026, 5, 5),
        date(2026, 5, 6),
    ]


def test_quality_alert_dag_runs_discord_bot_delivery_after_each_alert_stage() -> None:
    """
    Verify Airflow delivers schema alerts early and DQ alerts after checks.

    Returns:
        None.
    """
    dag_source = Path("dags/30_dag_dq_orders_quality_alerts.py").read_text(encoding="utf-8")

    assert 'task_id="t30_push_schema_drift_alerts"' in dag_source
    assert 'task_id="t70_push_discord_alerts"' in dag_source
    assert dag_source.count("python -m apps.discord_bot.delivery $DATE_ARGS") == 2
    assert (
        "t20_generate_schema_drift_alerts\n"
        "        >> t30_push_schema_drift_alerts\n"
        "        >> t40_profile_orders_tables"
    ) in dag_source
    assert "t60_generate_orders_alerts\n        >> t70_push_discord_alerts\n        >> t90_finish" in dag_source


def test_clickhouse_init_defines_durable_discord_message_registry() -> None:
    """Ensure fresh environments create the lifecycle message registry."""
    sql = Path("infra/init/clickhouse/08_discord_incident_messages.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS dq.discord_incident_messages" in sql
    assert "ReplacingMergeTree(updated_at)" in sql
    assert "content_hash" in sql
    assert "message_id" in sql
    assert "last_error_state" in sql
    assert "transport" in sql
    assert "author_id" in sql

    migration = Path("infra/init/clickhouse/09_discord_bot_delivery_migration.sql").read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS transport" in migration
    assert "ADD COLUMN IF NOT EXISTS author_id" in migration


def test_registry_decodes_clickhouse_fixed_string_content_hash() -> None:
    """Ensure ClickHouse FixedString bytes preserve content-hash idempotency."""
    expected_hash = "a" * 64
    row = (
        "alert-key",
        "DQ-20261007-ABC123",
        "channel-id",
        "message-id",
        "OPEN",
        expected_hash.encode("utf-8"),
        "deterministic_fallback",
        "discord_bot_api",
        "author-id",
        200,
        "",
        datetime(2026, 10, 7, tzinfo=timezone.utc),
        datetime(2026, 10, 7, tzinfo=timezone.utc),
    )
    client = FakeClickHouseClient()
    client.query = lambda sql: FakeQueryResult(  # type: ignore[method-assign]
        [row],
        column_names=DISCORD_INCIDENT_MESSAGE_COLUMNS,
    )

    state = load_incident_message_state(client, "alert-key")

    assert state is not None
    assert state.content_hash == expected_hash

