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

from agent.tools import daily_summary


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
    assert "FROM dq.alerts" in sql
    assert "AND status = 'open'" in sql
    assert "LIMIT 100" in sql


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
    assert len(client.queries) == 1
    assert captured_audit["action"] == "fetch_daily_quality_summary"
    assert captured_audit["tool_name"] == "daily_summary"
    assert captured_audit["status"] == "success"
    assert captured_audit["row_count"] == 4
    assert captured_audit["sql"] == payload["sql"]
