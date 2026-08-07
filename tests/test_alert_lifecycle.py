####
## Alert Lifecycle Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

from datetime import date
from uuid import uuid4

from agent.state import Alert
from agent.tools.alert_lifecycle import build_mark_alert_triaged_sql, mark_alert_triaged


# --- Defining Test Helpers
class FakeQueryResponse:
    """
    Minimal ClickHouse query response used by lifecycle tests.

    Attributes:
        column_names: Ordered response column names.
        result_rows: Query result rows.
    """

    def __init__(self, column_names: list[str], result_rows: list[tuple[object, ...]]) -> None:
        """
        Store fake query response data.

        Args:
            column_names: Ordered response column names.
            result_rows: Query result rows.

        Returns:
            None.
        """
        self.column_names = column_names
        self.result_rows  = result_rows


class FakeClickHouseClient:
    """
    Minimal ClickHouse client that captures mutation SQL and lifecycle verification queries.

    Attributes:
        commands: SQL commands executed by the lifecycle tool.
        queries: SQL queries executed by the lifecycle tool.
        should_fail: Whether command execution should raise an exception.
    """

    def __init__(self, should_fail: bool = False) -> None:
        """
        Initialize fake client state.

        Args:
            should_fail: Whether command execution should raise an exception.

        Returns:
            None.
        """
        self.commands: list[str] = []
        self.queries: list[str]  = []
        self.should_fail         = should_fail

    def command(self, sql: str) -> None:
        """
        Capture a ClickHouse command and optionally raise an exception.

        Args:
            sql: SQL command sent to ClickHouse.

        Returns:
            None.

        Raises:
            RuntimeError: When should_fail is enabled.
        """
        self.commands.append(sql)

        if self.should_fail:
            raise RuntimeError("mutation failed")

    def query(self, sql: str) -> FakeQueryResponse:
        """
        Capture lifecycle verification query and return a triaged row.

        Args:
            sql: SQL query sent to ClickHouse.

        Returns:
            Fake query response with one lifecycle row.
        """
        self.queries.append(sql)

        return FakeQueryResponse(
            column_names=["alert_key", "status", "report_s3_uri", "updated_at"],
            result_rows=[("orders|dq_failure|2026-05-04|dq.raw_orders|row_count_positive|table", "triaged", "s3://dq-artifacts/report.md", None)],
        )


def build_alert() -> Alert:
    """
    Build one alert model for lifecycle tests.

    Returns:
        Alert model matching dq.alerts fields.
    """
    return Alert(
        alert_id=uuid4(),
        alert_key="orders|dq_failure|2026-05-04|dq.raw_orders|row_count_positive|table",
        status="open",
        alert_type="dq_failure",
        severity="critical",
        table_name="dq.raw_orders",
        metric="row_count_positive",
        dt=date(2026, 5, 4),
    )


# --- Defining Tests
def test_build_mark_alert_triaged_sql_is_bounded_to_open_alert() -> None:
    """
    Validate that lifecycle mutation is scoped to one open alert key.

    Returns:
        None.
    """
    sql = build_mark_alert_triaged_sql(
        alert_key="orders|dq_failure|2026-05-04|dq.raw_orders|row_count_positive|table",
        report_s3_uri="s3://dq-artifacts/report.md",
    )

    assert "ALTER TABLE dq.alerts" in sql
    assert "status = 'triaged'" in sql
    assert "report_s3_uri = 's3://dq-artifacts/report.md'" in sql
    assert "AND status = 'open'" in sql
    assert "SETTINGS mutations_sync = 1" in sql
    assert "updated_at" not in sql


def test_mark_alert_triaged_writes_mutation_and_audit(monkeypatch) -> None:
    """
    Validate successful lifecycle update behavior.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    client       = FakeClickHouseClient()
    audit_events = []

    def fake_build_clickhouse_client(host=None, port=None):
        """
        Return the fake ClickHouse client.

        Args:
            host: Optional ClickHouse host override.
            port: Optional ClickHouse port override.

        Returns:
            Fake ClickHouse client.
        """
        return client

    def fake_write_agent_audit_event(**kwargs):
        """
        Capture audit writes without touching ClickHouse.

        Args:
            kwargs: Audit payload keyword arguments.

        Returns:
            Agent run UUID.
        """
        audit_events.append(kwargs)

        return kwargs["agent_run_id"]

    monkeypatch.setattr("agent.tools.alert_lifecycle.build_clickhouse_client", fake_build_clickhouse_client)
    monkeypatch.setattr("agent.tools.alert_lifecycle.write_agent_audit_event", fake_write_agent_audit_event)

    result = mark_alert_triaged(alert=build_alert(), report_s3_uri="s3://dq-artifacts/report.md")

    assert result["status"] == "success"
    assert result["alert_status"] == "triaged"
    assert len(client.commands) == 1
    assert len(client.queries) == 1
    assert audit_events[0]["action"] == "mark_alert_triaged"
    assert audit_events[0]["status"] == "success"


def test_mark_alert_triaged_returns_failed_result_without_raising(monkeypatch) -> None:
    """
    Validate lifecycle failures are returned as failed results instead of crashing triage.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    client       = FakeClickHouseClient(should_fail=True)
    audit_events = []

    def fake_build_clickhouse_client(host=None, port=None):
        """
        Return the failing fake ClickHouse client.

        Args:
            host: Optional ClickHouse host override.
            port: Optional ClickHouse port override.

        Returns:
            Fake ClickHouse client.
        """
        return client

    def fake_write_agent_audit_event(**kwargs):
        """
        Capture failed audit writes without touching ClickHouse.

        Args:
            kwargs: Audit payload keyword arguments.

        Returns:
            Agent run UUID.
        """
        audit_events.append(kwargs)

        return kwargs["agent_run_id"]

    monkeypatch.setattr("agent.tools.alert_lifecycle.build_clickhouse_client", fake_build_clickhouse_client)
    monkeypatch.setattr("agent.tools.alert_lifecycle.write_agent_audit_event", fake_write_agent_audit_event)

    result = mark_alert_triaged(alert=build_alert(), report_s3_uri="s3://dq-artifacts/report.md")

    assert result["status"] == "failed"
    assert result["error_type"] == "RuntimeError"
    assert len(client.commands) == 1
    assert audit_events[0]["status"] == "failed"
