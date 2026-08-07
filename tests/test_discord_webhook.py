####
## Discord Webhook Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import requests

from agent.tools.audit_log import AGENT_AUDIT_LOG_COLUMNS
from apps.discord_bot.webhook import (
    DISCORD_WEBHOOK_AUDIT_ACTION,
    DiscordWebhookDeliveryError,
    DiscordWebhookSettings,
    build_notification_lookup_sql,
    build_webhook_payload,
    deliver_alert,
    resolve_run_dates,
    run_discord_webhook_notifications,
    validate_discord_webhook_url,
)


# --- Defining Test Doubles
class FakeQueryResult:
    """
    Minimal clickhouse-connect query result used by webhook tests.

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
    Capture webhook idempotency queries and agent audit inserts.

    Attributes:
        delivered_count: Prior successful delivery count returned by scalar queries.
        queries: SQL statements executed by the webhook flow.
        inserts: Insert calls written through the shared audit tool.
    """

    def __init__(self, delivered_count: int = 0) -> None:
        """
        Initialize a fake ClickHouse client.

        Args:
            delivered_count: Count returned for prior successful webhook delivery.

        Returns:
            None.
        """
        self.delivered_count = delivered_count
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

        return FakeQueryResult(result_rows=[(self.delivered_count,)], column_names=["count()"])

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

    def __init__(self, status_code: int = 204, failure: bool = False) -> None:
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

    def raise_for_status(self) -> None:
        """
        Raise a requests HTTP error when failure mode is enabled.

        Returns:
            None.

        Raises:
            requests.HTTPError: When failure mode is enabled.
        """
        if self.failure:
            error          = requests.HTTPError("request failed for a secret webhook URL")
            error.response = self

            raise error


class FakeSession:
    """
    Capture outgoing webhook requests without network access.

    Attributes:
        response: Fake response returned by post.
        posts: Captured post arguments.
        closed: Whether close was called.
    """

    def __init__(self, response: FakeResponse | None = None) -> None:
        """
        Initialize a fake HTTP session.

        Args:
            response: Optional fake response override.

        Returns:
            None.
        """
        self.response = response or FakeResponse()
        self.posts: list[dict[str, Any]] = []
        self.closed = False

    def post(self, url: str, json: dict[str, Any], timeout: float) -> FakeResponse:
        """
        Capture a Discord webhook post call.

        Args:
            url: Secret Discord webhook URL.
            json: JSON payload sent to Discord.
            timeout: Request timeout in seconds.

        Returns:
            Configured fake response.
        """
        self.posts.append({"url": url, "json": json, "timeout": timeout})

        return self.response

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
        Alert dictionary accepted by the Discord formatter and webhook sender.
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


def webhook_settings() -> DiscordWebhookSettings:
    """
    Build valid test settings without using a real Discord credential.

    Returns:
        Discord webhook settings with a syntactically valid fake endpoint.
    """
    return DiscordWebhookSettings(
        webhook_url="https://discord.com/api/webhooks/123456/test-token",
        username="DataSentry Test",
        timeout_seconds=5.0,
        max_alerts_per_date=20,
    )


# --- Testing URL And Payload Guardrails
def test_validate_discord_webhook_url_accepts_official_https_endpoint() -> None:
    """
    Verify official Discord HTTPS webhook endpoints are accepted.

    Returns:
        None.
    """
    webhook_url = "https://discord.com/api/webhooks/123456/test-token"

    assert validate_discord_webhook_url(webhook_url) == webhook_url


@pytest.mark.parametrize(
    "webhook_url",
    [
        "http://discord.com/api/webhooks/123456/test-token",
        "https://example.com/api/webhooks/123456/test-token",
        "https://discord.com/not-a-webhook/123456/test-token",
    ],
)
def test_validate_discord_webhook_url_rejects_unsafe_endpoints(webhook_url: str) -> None:
    """
    Verify non-HTTPS, non-Discord, and invalid webhook paths are rejected.

    Args:
        webhook_url: Invalid webhook endpoint under test.

    Returns:
        None.
    """
    with pytest.raises(ValueError):
        validate_discord_webhook_url(webhook_url)


def test_build_webhook_payload_disables_mentions_and_bounds_content() -> None:
    """
    Verify webhook payloads cannot mention users or exceed the soft limit.

    Returns:
        None.
    """
    payload = build_webhook_payload("@everyone " + ("x" * 2500), username="DataSentry")

    assert len(payload["content"]) <= 1900
    assert payload["allowed_mentions"] == {"parse": []}
    assert payload["username"] == "DataSentry"


def test_notification_lookup_sql_escapes_alert_key_and_uses_success_state() -> None:
    """
    Verify deduplication SQL safely quotes alert keys and only trusts successful sends.

    Returns:
        None.
    """
    sql = build_notification_lookup_sql("orders|metric|'quoted'")

    assert "discord_alert_webhook_sent" in sql
    assert "status = 'success'" in sql
    assert "\\'quoted\\'" in sql


# --- Testing Delivery And Auditability
def test_deliver_alert_sends_once_and_writes_secret_safe_audit_event() -> None:
    """
    Verify a new alert is posted once and audited without the webhook secret.

    Returns:
        None.
    """
    client   = FakeClickHouseClient(delivered_count=0)
    session  = FakeSession()
    settings = webhook_settings()
    alert    = sample_alert()

    result = deliver_alert(
        client=client,
        session=session,
        settings=settings,
        alert=alert,
        agent_run_id=uuid4(),
    )

    assert result["status"] == "sent"
    assert result["http_status"] == 204
    assert len(session.posts) == 1
    assert session.posts[0]["json"]["allowed_mentions"] == {"parse": []}
    assert len(client.inserts) == 1

    insert_call = client.inserts[0]
    row         = dict(zip(AGENT_AUDIT_LOG_COLUMNS, insert_call["data"][0]))
    audit_text  = json.dumps(row, default=str)

    assert row["action"] == DISCORD_WEBHOOK_AUDIT_ACTION
    assert row["status"] == "success"
    assert row["actor"] == "airflow"
    assert "test-token" not in audit_text
    assert "discord.com" in audit_text


def test_deliver_alert_skips_previously_successful_delivery() -> None:
    """
    Verify Airflow retries do not resend alerts with successful audit evidence.

    Returns:
        None.
    """
    client  = FakeClickHouseClient(delivered_count=1)
    session = FakeSession()

    result = deliver_alert(
        client=client,
        session=session,
        settings=webhook_settings(),
        alert=sample_alert(),
        agent_run_id=uuid4(),
    )

    assert result["status"] == "skipped_existing"
    assert session.posts == []
    assert client.inserts == []


def test_delivery_failure_raises_sanitized_error_and_writes_failed_audit() -> None:
    """
    Verify failed HTTP delivery remains retryable without leaking the webhook URL.

    Returns:
        None.
    """
    client   = FakeClickHouseClient(delivered_count=0)
    session  = FakeSession(response=FakeResponse(status_code=500, failure=True))
    settings = webhook_settings()

    with pytest.raises(DiscordWebhookDeliveryError) as exc_info:
        deliver_alert(
            client=client,
            session=session,
            settings=settings,
            alert=sample_alert(),
            agent_run_id=uuid4(),
        )

    insert_call = client.inserts[0]
    row         = dict(zip(AGENT_AUDIT_LOG_COLUMNS, insert_call["data"][0]))
    audit_text  = json.dumps(row, default=str)

    assert row["status"] == "failed"
    assert row["error_message"] == "HTTPError; http_status=500"
    assert "test-token" not in str(exc_info.value)
    assert "test-token" not in audit_text


def test_disabled_webhook_returns_explicit_skip_without_clickhouse_or_http() -> None:
    """
    Verify missing optional webhook configuration produces a safe no-op result.

    Returns:
        None.
    """
    settings = DiscordWebhookSettings(
        webhook_url="",
        username="DataSentry",
        timeout_seconds=10.0,
        max_alerts_per_date=20,
    )

    result = run_discord_webhook_notifications(
        dates=[date(2026, 5, 4)],
        settings=settings,
    )

    assert result == {
        "status": "skipped",
        "reason": "webhook_url_not_configured",
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


def test_quality_alert_dag_runs_discord_webhook_after_alert_generation() -> None:
    """
    Verify Airflow owns scheduled webhook invocation after alert generation.

    Returns:
        None.
    """
    dag_source = Path("dags/30_dag_dq_orders_quality_alerts.py").read_text(encoding="utf-8")

    assert 'task_id="t40_push_discord_alerts"' in dag_source
    assert "python -m apps.discord_bot.webhook $DATE_ARGS" in dag_source
    assert "t30_generate_orders_alerts\n        >> t40_push_discord_alerts\n        >> t90_finish" in dag_source
