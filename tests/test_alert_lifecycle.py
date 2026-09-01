####
## Alert Lifecycle Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

from datetime import date, datetime, timezone
from uuid import uuid4

from agent.state import Alert
from agent.tools.alert_lifecycle import (
    ALERT_VERSION_COLUMNS,
    build_alert_lifecycle_source_sql,
    build_alert_lifecycle_version_row,
    mark_alert_triaged,
)


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
    Minimal ClickHouse client that captures version writes and verification queries.

    Attributes:
        queries: SQL queries executed by the lifecycle tool.
        inserts: Typed rows inserted by the lifecycle tool.
        should_fail: Whether insert execution should raise an exception.
        retain_stale_readback: Whether readback should ignore the inserted row.
    """

    def __init__(
        self,
        should_fail: bool = False,
        report_s3_uri: str = "s3://dq-artifacts/old-report.md",
        retain_stale_readback: bool = False,
    ) -> None:
        """
        Initialize fake client state.

        Args:
            should_fail: Whether insert execution should raise an exception.
            report_s3_uri: Report URI present on the initial alert version.
            retain_stale_readback: Whether readback should ignore inserted values.

        Returns:
            None.
        """
        self.queries: list[str]          = []
        self.inserts: list[dict]         = []
        self.should_fail                 = should_fail
        self.retain_stale_readback       = retain_stale_readback
        self.current_row                 = {
            "alert_id": uuid4(),
            "alert_key": "orders|dq_failure|2026-05-04|dq.raw_orders|row_count_positive|table",
            "alert_display_id": "DQ-20260504-TEST01",
            "created_at": datetime(2026, 5, 4, tzinfo=timezone.utc),
            "updated_at": datetime(2026, 5, 4, tzinfo=timezone.utc),
            "status": "triaged",
            "alert_type": "dq_failure",
            "severity": "critical",
            "table_name": "dq.raw_orders",
            "metric": "row_count_positive",
            "dt": date(2026, 5, 4),
            "dimension": "table",
            "observed_value": 0.0,
            "expected_value": 1.0,
            "threshold_value": 1.0,
            "source_check_run_id": uuid4(),
            "details_json": "{}",
            "report_s3_uri": report_s3_uri,
            "acknowledged_by": "",
            "resolved_at": None,
        }

    def insert(
        self,
        table: str,
        data: list[list[object]],
        column_names: tuple[str, ...],
    ) -> None:
        """
        Capture a typed lifecycle insert and optionally raise an exception.

        Args:
            table: Fully qualified ClickHouse target table.
            data: Row-oriented values sent to clickhouse-connect.
            column_names: Ordered ClickHouse insert columns.

        Returns:
            None.

        Raises:
            RuntimeError: When should_fail is enabled.
        """
        self.inserts.append(
            {
                "table": table,
                "data": data,
                "column_names": column_names,
            }
        )

        if self.should_fail:
            raise RuntimeError("lifecycle version write failed")

        if not self.retain_stale_readback:
            self.current_row = dict(zip(column_names, data[0], strict=True))

    def query(self, sql: str) -> FakeQueryResponse:
        """
        Capture lifecycle verification query and return a triaged row.

        Args:
            sql: SQL query sent to ClickHouse.

        Returns:
            Fake query response with one lifecycle row.
        """
        self.queries.append(sql)

        if "alert_id" in sql:
            columns = list(ALERT_VERSION_COLUMNS)
        else:
            columns = ["alert_key", "status", "report_s3_uri", "updated_at"]

        return FakeQueryResponse(
            column_names=columns,
            result_rows=[tuple(self.current_row[column] for column in columns)],
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
def test_build_alert_lifecycle_source_sql_reads_one_final_version() -> None:
    """
    Validate lifecycle source selection is bounded to one FINAL alert version.

    Returns:
        None.
    """
    sql = build_alert_lifecycle_source_sql(
        alert_key="orders|dq_failure|2026-05-04|dq.raw_orders|row_count_positive|table"
    )

    assert "SELECT" in sql
    assert "FROM dq.alerts FINAL" in sql
    assert "ORDER BY updated_at DESC" in sql
    assert "LIMIT 1" in sql


def test_build_alert_lifecycle_version_row_preserves_source_fields() -> None:
    """
    Validate a copied version changes only timestamp, status, and report URI.

    Returns:
        None.
    """
    client      = FakeClickHouseClient()
    source_row = dict(client.current_row)
    observed_at = datetime(2026, 5, 5, tzinfo=timezone.utc)
    values      = build_alert_lifecycle_version_row(
        source_row=source_row,
        report_s3_uri="s3://dq-artifacts/report.md",
        observed_at=observed_at,
    )
    version_row = dict(zip(ALERT_VERSION_COLUMNS, values, strict=True))

    assert version_row["alert_id"] == source_row["alert_id"]
    assert version_row["created_at"] == source_row["created_at"]
    assert version_row["status"] == "triaged"
    assert version_row["report_s3_uri"] == "s3://dq-artifacts/report.md"
    assert version_row["updated_at"] == observed_at


def test_mark_alert_triaged_writes_version_and_audit(monkeypatch) -> None:
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
    assert result["version_written"] is True
    assert len(client.inserts) == 1
    assert len(client.queries) == 2
    assert client.inserts[0]["table"] == "dq.alerts"
    assert client.inserts[0]["column_names"] == ALERT_VERSION_COLUMNS
    assert audit_events[0]["action"] == "mark_alert_triaged"
    assert audit_events[0]["status"] == "success"
    assert audit_events[0]["row_count"] == 1


def test_mark_alert_triaged_skips_identical_report_version(monkeypatch) -> None:
    """
    Validate repeated lifecycle writes for the same report remain idempotent.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    client       = FakeClickHouseClient(report_s3_uri="s3://dq-artifacts/report.md")
    audit_events = []

    def fake_build_clickhouse_client(host=None, port=None):
        """
        Return the already-current fake ClickHouse client.

        Args:
            host: Optional ClickHouse host override.
            port: Optional ClickHouse port override.

        Returns:
            Fake ClickHouse client.
        """
        return client

    def fake_write_agent_audit_event(**kwargs):
        """
        Capture the idempotent lifecycle audit.

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
    assert result["version_written"] is False
    assert client.inserts == []
    assert audit_events[0]["row_count"] == 0


def test_mark_alert_triaged_rejects_stale_report_readback(monkeypatch) -> None:
    """
    Reject a lifecycle write whose readback still points to an older report.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    client       = FakeClickHouseClient(retain_stale_readback=True)
    audit_events = []

    def fake_build_clickhouse_client(host=None, port=None):
        """
        Return the stale-readback ClickHouse client.

        Args:
            host: Optional ClickHouse host override.
            port: Optional ClickHouse port override.

        Returns:
            Fake ClickHouse client.
        """
        return client

    def fake_write_agent_audit_event(**kwargs):
        """
        Capture the expected failed postcondition audit.

        Args:
            kwargs: Audit payload keyword arguments.

        Returns:
            Agent run UUID.
        """
        audit_events.append(kwargs)

        return kwargs["agent_run_id"]

    monkeypatch.setattr(
        "agent.tools.alert_lifecycle.build_clickhouse_client",
        fake_build_clickhouse_client,
    )
    monkeypatch.setattr(
        "agent.tools.alert_lifecycle.write_agent_audit_event",
        fake_write_agent_audit_event,
    )

    result = mark_alert_triaged(
        alert=build_alert(),
        report_s3_uri="s3://dq-artifacts/report.md",
    )

    assert result["status"] == "failed"
    assert result["error_type"] == "RuntimeError"
    assert "latest report URI" in result["error_message"]
    assert audit_events[0]["status"] == "failed"


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
    assert len(client.inserts) == 1
    assert audit_events[0]["status"] == "failed"
