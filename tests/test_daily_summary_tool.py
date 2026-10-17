####
## Daily Data Quality Summary Tool Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Focused tests for deterministic daily-summary SQL, aggregation, and audit behavior."""

# --- Importing Libraries
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from pydantic import ValidationError

from agent.tools import daily_summary
from apps.api.schemas import DailySummaryResponse


# --- Defining Test Fakes
@dataclass
class FakeQueryResult:
    """
    Minimal clickhouse-connect result used by daily-summary tests.

    Attributes:
        column_names: Ordered query-result column names.
        result_rows: Tuple rows returned by the fake query.
    """

    column_names: list[str]
    result_rows: list[tuple[Any, ...]]


class FakeClickHouseClient:
    """
    Capture the daily-summary SQL and return deterministic aggregate rows.

    Attributes:
        queries: SQL statements executed by the tool.
    """

    def __init__(self) -> None:
        """Initialize an empty query history."""
        self.queries: list[str] = []

    def query(self, sql: str) -> FakeQueryResult:
        """
        Capture SQL and return check plus alert count rows.

        Args:
            sql: Daily-summary aggregation SQL.

        Returns:
            Deterministic category, label, and count rows.
        """
        self.queries.append(sql)

        return FakeQueryResult(
            column_names=["category", "label", "count"],
            result_rows=[
                ("alert", "critical", 1),
                ("alert", "warning", 2),
                ("check", "fail", 3),
                ("check", "pass", 9),
            ],
        )


class FakeLatestDateClickHouseClient(FakeClickHouseClient):
    """Resolve fixture dates using the latest-date query's alert filter."""

    def __init__(
        self,
        check_dates: tuple[str, ...] = ("2026-06-10",),
        alerts: tuple[tuple[str, str], ...] = (),
    ) -> None:
        """Store check dates and (date, alert_type) detection-history rows."""
        super().__init__()
        self.check_dates = check_dates
        self.alerts = alerts

    def query(self, sql: str) -> FakeQueryResult:
        if "SELECT maxOrNull(dt)" in sql:
            self.queries.append(sql)
            dates = list(self.check_dates)
            dates.extend(
                dt for dt, alert_type in self.alerts
                if "AND alert_type = 'dq_failure'" not in sql or alert_type == "dq_failure"
            )

            return FakeQueryResult(
                column_names=["maxOrNull(dt)"],
                result_rows=[(max(dates) if dates else None,)],
            )

        return super().query(sql)


# --- Defining SQL Tests
def test_daily_summary_sql_is_date_filtered_open_only_and_bounded() -> None:
    """
    Ensure both large tables use the exact date and the aggregate result is bounded.

    Returns:
        None.
    """
    sql = daily_summary.build_daily_summary_sql("2026-06-10")

    assert sql.count("toDate('2026-06-10')") == 2
    assert "FROM dq.dq_check_results" in sql
    assert "FROM dq.alerts FINAL" in sql
    assert sql.index("FROM dq.alerts FINAL") < sql.index("AND status = 'open'")
    assert "AND status = 'open'" in sql
    assert "GROUP BY severity" in sql
    assert "LIMIT 100" in sql
    assert "alert_type" not in sql


def test_latest_summary_date_sql_excludes_schema_dates_and_preserves_empty_data() -> None:
    """Only DQ alert business dates can augment check dates; empty input stays null."""
    sql = daily_summary.LATEST_SUMMARY_DATE_SQL
    check_sql, alert_sql = sql.split("UNION ALL")

    assert "SELECT maxOrNull(dt)" in check_sql
    assert "SELECT max(dt)" not in sql
    assert "FROM dq.dq_check_results" in check_sql
    assert "alert_type" not in check_sql
    assert "FROM dq.alerts FINAL" in alert_sql
    assert "AND alert_type = 'dq_failure'" in alert_sql
    assert sql.count("WHERE dt IS NOT NULL") == 2
    assert "status" not in sql


# --- Defining Aggregation Tests
def test_daily_summary_payload_rejects_unknown_categories_and_invalid_counts() -> None:
    """
    Ensure malformed warehouse aggregates fail before reaching operator interfaces.

    Returns:
        None.
    """
    with pytest.raises(ValueError, match="unknown category"):
        daily_summary.build_daily_summary_payload(
            dt="2026-06-10",
            rows=[{"category": "other", "label": "value", "count": 1}],
        )

    with pytest.raises(ValueError, match="invalid count"):
        daily_summary.build_daily_summary_payload(
            dt="2026-06-10",
            rows=[{"category": "check", "label": "pass", "count": True}],
        )


def test_fetch_daily_quality_summary_aggregates_and_audits(monkeypatch) -> None:
    """
    Ensure the public tool returns consistent totals and records one audited tool call.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    client         = FakeClickHouseClient()
    captured_audit: dict[str, Any] = {}

    monkeypatch.setattr(
        daily_summary,
        "write_agent_audit_event",
        lambda **kwargs: captured_audit.update(kwargs),
    )

    payload = daily_summary.fetch_daily_quality_summary(
        dt="2026-06-10",
        client=client,
        agent_run_id="11111111-1111-1111-1111-111111111111",
    )

    assert payload["dt"] == "2026-06-10"
    assert payload["availability"] == "available"
    assert payload["check_counts"] == [
        {"status": "fail", "count": 3},
        {"status": "pass", "count": 9},
    ]
    assert payload["alert_counts"] == [
        {"severity": "critical", "count": 1},
        {"severity": "warning", "count": 2},
    ]
    assert payload["total_checks"] == 12
    assert payload["total_open_alerts"] == 3
    assert payload["total_open_alerts"] == sum(item["count"] for item in payload["alert_counts"])
    assert len(client.queries) == 1
    assert captured_audit["action"] == "fetch_daily_quality_summary"
    assert captured_audit["tool_name"] == "daily_summary"
    assert captured_audit["status"] == "success"
    assert captured_audit["row_count"] == 4
    assert captured_audit["sql"] == payload["sql"]


def test_fetch_daily_quality_summary_resolves_latest_available_date(monkeypatch) -> None:
    """An omitted date should resolve deterministically before aggregation."""
    client = FakeLatestDateClickHouseClient()
    monkeypatch.setattr(daily_summary, "write_agent_audit_event", lambda **kwargs: None)

    payload = daily_summary.fetch_daily_quality_summary(
        client=client,
        agent_run_id="11111111-1111-1111-1111-111111111111",
    )

    assert payload["dt"] == "2026-06-10"
    assert len(client.queries) == 2
    assert "SELECT maxOrNull(dt)" in client.queries[0]
    assert "toDate('2026-06-10')" in client.queries[1]


@pytest.mark.parametrize("requested_dt", [None, "", "   "])
def test_latest_summary_ignores_newer_schema_detection_date(monkeypatch, requested_dt) -> None:
    """The October 13 schema fixture must not replace October 11 DQ evidence."""
    client = FakeLatestDateClickHouseClient(
        check_dates=("2026-10-11",),
        alerts=(("2026-10-13", "schema_drift"), ("2026-10-10", "dq_failure")),
    )
    audits = []
    monkeypatch.setattr(daily_summary, "write_agent_audit_event", lambda **kwargs: audits.append(kwargs))

    payload = daily_summary.fetch_daily_quality_summary(dt=requested_dt, client=client)

    assert payload["dt"] == "2026-10-11"
    assert len(client.queries) == 2
    assert client.queries[0] == daily_summary.LATEST_SUMMARY_DATE_SQL
    assert payload["sql"] == client.queries[1]
    assert payload["sql"].count("toDate('2026-10-11')") == 2
    assert len(audits) == 1
    assert audits[0]["status"] == "success"
    assert audits[0]["input_payload"]["requested_dt"] == requested_dt
    assert audits[0]["input_payload"]["resolved_dt"] == "2026-10-11"


@pytest.mark.parametrize("check_dates", [(), ("2026-10-10",)])
def test_latest_summary_uses_exact_dq_failure_alert_date(check_dates) -> None:
    """Exact DQ failures provide a latest date even without any check results."""
    client = FakeLatestDateClickHouseClient(
        check_dates=check_dates,
        alerts=(
            ("2026-10-11", "dq_failure"),
            ("2026-10-13", "schema_drift"),
            ("2026-10-14", "dq_failure_extra"),
        ),
    )

    assert daily_summary.resolve_daily_summary_date(client) == "2026-10-11"
    assert client.queries == [daily_summary.LATEST_SUMMARY_DATE_SQL]


def test_explicit_schema_date_bypasses_latest_lookup_and_keeps_alert_scope(monkeypatch) -> None:
    """An explicit detection date still counts all open alert types for that date."""
    client = FakeLatestDateClickHouseClient(check_dates=("2026-10-11",))
    audits = []
    monkeypatch.setattr(daily_summary, "write_agent_audit_event", lambda **kwargs: audits.append(kwargs))

    payload = daily_summary.fetch_daily_quality_summary(dt="2026-10-13", client=client)

    assert payload["dt"] == "2026-10-13"
    assert len(client.queries) == 1
    assert payload["sql"] == client.queries[0]
    assert payload["sql"].count("toDate('2026-10-13')") == 2
    assert "alert_type" not in payload["sql"]
    assert payload["total_open_alerts"] == 3
    assert audits[0]["input_payload"]["resolved_dt"] == "2026-10-13"


@pytest.mark.parametrize("requested_dt", [None, "", "   "])
@pytest.mark.parametrize("alerts", [(), (("2026-10-13", "schema_drift"),)])
def test_latest_summary_without_dq_data_returns_empty_and_audits_success(monkeypatch, alerts, requested_dt) -> None:
    """Empty and schema-only datasets return no date without fabricating evidence."""
    client = FakeLatestDateClickHouseClient(check_dates=(), alerts=alerts)
    audits = []
    monkeypatch.setattr(daily_summary, "write_agent_audit_event", lambda **kwargs: audits.append(kwargs))

    payload = daily_summary.fetch_daily_quality_summary(dt=requested_dt, client=client)

    assert payload["status"] == "success"
    assert payload["availability"] == "empty"
    assert payload["dt"] is None
    assert payload["check_counts"] == payload["alert_counts"] == []
    assert payload["total_checks"] == payload["total_open_alerts"] == 0
    assert payload["summary"] == "No quality data yet"
    public = DailySummaryResponse.model_validate(payload).model_dump(mode="json")
    assert public["dt"] is None
    assert public["availability"] == "empty"
    assert "sql" not in public
    assert client.queries == [daily_summary.LATEST_SUMMARY_DATE_SQL]
    assert len(audits) == 1
    assert audits[0]["status"] == "success"
    assert audits[0]["input_payload"]["requested_dt"] == requested_dt
    assert audits[0]["input_payload"]["resolved_dt"] is None
    assert audits[0]["output_payload"] == {
        "availability": "empty", "total_checks": 0, "total_open_alerts": 0,
    }
    assert audits[0]["sql"] == daily_summary.LATEST_SUMMARY_DATE_SQL
    assert audits[0]["row_count"] == 0


def test_latest_summary_with_no_scalar_rows_has_no_date() -> None:
    """A missing scalar result follows the same no-data path as a null maximum."""
    class EmptyClient:
        def query(self, sql: str) -> FakeQueryResult:
            return FakeQueryResult(column_names=["maxOrNull(dt)"], result_rows=[])

    assert daily_summary.resolve_daily_summary_date(EmptyClient()) is None


def test_explicit_date_without_rows_returns_zero_totals_and_audits(monkeypatch) -> None:
    """Explicit dates remain valid even when neither table has rows on that date."""
    client = FakeClickHouseClient()
    audits = []

    def empty_query(sql: str) -> FakeQueryResult:
        client.queries.append(sql)
        return FakeQueryResult(column_names=["category", "label", "count"], result_rows=[])

    monkeypatch.setattr(client, "query", empty_query)
    monkeypatch.setattr(daily_summary, "write_agent_audit_event", lambda **kwargs: audits.append(kwargs))

    payload = daily_summary.fetch_daily_quality_summary(dt="2026-10-13", client=client)

    assert payload["status"] == "success"
    assert payload["availability"] == "empty"
    assert payload["dt"] == "2026-10-13"
    assert payload["check_counts"] == payload["alert_counts"] == []
    assert payload["total_checks"] == payload["total_open_alerts"] == 0
    assert len(client.queries) == 1
    assert audits[0]["status"] == "success"
    assert audits[0]["row_count"] == 0
    assert DailySummaryResponse.model_validate(payload).model_dump(mode="json")["dt"] == "2026-10-13"


@pytest.mark.parametrize("fail_latest", [True, False])
def test_summary_query_errors_propagate_and_audit_failure(monkeypatch, fail_latest) -> None:
    """Latest-date and aggregation query errors retain the existing failure audit."""
    client = FakeLatestDateClickHouseClient()
    audits = []
    original_query = client.query
    error = RuntimeError("ClickHouse query failed")

    def failing_query(sql: str) -> FakeQueryResult:
        if (sql == daily_summary.LATEST_SUMMARY_DATE_SQL) == fail_latest:
            client.queries.append(sql)
            raise error
        return original_query(sql)

    monkeypatch.setattr(client, "query", failing_query)
    monkeypatch.setattr(daily_summary, "write_agent_audit_event", lambda **kwargs: audits.append(kwargs))

    with pytest.raises(RuntimeError, match="ClickHouse query failed") as raised:
        daily_summary.fetch_daily_quality_summary(client=client)

    assert raised.value is error
    assert len(client.queries) == (1 if fail_latest else 2)
    assert len(audits) == 1
    assert audits[0]["status"] == "failed"
    assert audits[0]["input_payload"]["resolved_dt"] == ("" if fail_latest else "2026-06-10")
    assert audits[0]["output_payload"] == {"error_type": "RuntimeError"}
    assert audits[0]["error_message"] == str(error)


@pytest.mark.parametrize("requested_dt", ["not-a-date", "2026-02-30", "2026-13-01"])
def test_invalid_summary_date_still_raises_and_audits(monkeypatch, requested_dt) -> None:
    """An invalid request must not be converted into a successful empty response."""
    client = FakeClickHouseClient()
    audits = []
    monkeypatch.setattr(daily_summary, "write_agent_audit_event", lambda **kwargs: audits.append(kwargs))

    with pytest.raises(ValueError):
        daily_summary.fetch_daily_quality_summary(dt=requested_dt, client=client)

    assert client.queries == []
    assert len(audits) == 1
    assert audits[0]["status"] == "failed"
    assert audits[0]["output_payload"] == {"error_type": "ValueError"}


def test_empty_summary_audit_dependency_error_still_propagates(monkeypatch) -> None:
    """A failed audit write cannot be disguised as a successful empty summary."""
    client = FakeLatestDateClickHouseClient(check_dates=())

    def fail_audit(**kwargs):
        raise RuntimeError("Audit persistence unavailable")

    monkeypatch.setattr(daily_summary, "write_agent_audit_event", fail_audit)

    with pytest.raises(RuntimeError, match="Audit persistence unavailable"):
        daily_summary.fetch_daily_quality_summary(client=client)


@pytest.mark.parametrize("category,label", [("check", "pass"), ("alert", "warning")])
def test_summary_with_only_alerts_or_checks_is_available(category, label) -> None:
    """Alert-only summaries are available but are not evidence of healthy checks."""
    payload = daily_summary.build_daily_summary_payload(
        "2026-10-16", [{"category": category, "label": label, "count": 1}],
    )

    assert payload["availability"] == "available"
    assert payload["dt"] == "2026-10-16"
    assert DailySummaryResponse.model_validate(payload).availability == "available"


def test_summary_payload_cannot_attach_evidence_to_a_null_date() -> None:
    with pytest.raises(ValueError, match="business date"):
        daily_summary.build_daily_summary_payload(None, [{"category": "check", "label": "pass", "count": 1}])


def test_zero_count_rows_normalize_to_empty_collections() -> None:
    payload = daily_summary.build_daily_summary_payload(
        "2026-10-16", [{"category": "check", "label": "pass", "count": 0}],
    )

    assert payload["availability"] == "empty"
    assert payload["check_counts"] == payload["alert_counts"] == []
    assert payload["summary"] == "No quality data yet"


@pytest.mark.parametrize("updates", [
    {"availability": "unknown"},
    {"availability": "available"},
    {"status": "failed"},
    {"dt": "invalid"},
    {"total_checks": 1},
    {"check_counts": [{"status": "pass", "count": 0}]},
    {"alert_counts": [{"severity": "warning", "count": 0}]},
    {"check_counts": [{"status": "pass", "count": 1}], "total_checks": 1},
    {"alert_counts": [{"severity": "critical", "count": 1}], "total_open_alerts": 1},
])
def test_daily_response_rejects_invalid_empty_contract(updates) -> None:
    payload = daily_summary.build_daily_summary_payload(None, [])

    with pytest.raises(ValidationError):
        DailySummaryResponse.model_validate({**payload, **updates})


@pytest.mark.parametrize("updates", [{"dt": None}, {"availability": "empty"}])
def test_daily_response_requires_date_for_available_data(updates) -> None:
    payload = daily_summary.build_daily_summary_payload(
        "2026-10-16", [{"category": "check", "label": "pass", "count": 1}],
    )

    with pytest.raises(ValidationError):
        DailySummaryResponse.model_validate({**payload, **updates})


def test_daily_response_rejects_dated_available_summary_without_evidence() -> None:
    payload = daily_summary.build_daily_summary_payload("2026-10-16", [])

    with pytest.raises(ValidationError, match="nonzero totals"):
        DailySummaryResponse.model_validate({**payload, "availability": "available"})
