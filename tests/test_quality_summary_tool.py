####
## Bounded Quality Summary Tool Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Focused tests for weekly, table, and warehouse quality summaries."""

# --- Importing Libraries
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from agent.tools import quality_summary


# --- Defining Test Fakes
@dataclass
class FakeQueryResult:
    """Minimal clickhouse-connect result used by quality-summary tests."""

    column_names: list[str]
    result_rows: list[tuple[Any, ...]]


class FakeClickHouseClient:
    """Capture quality-summary SQL and return deterministic aggregates."""

    def __init__(self) -> None:
        """Initialize an empty query history."""
        self.queries: list[str] = []

    def query(self, sql: str) -> FakeQueryResult:
        """Capture SQL and return a complete bounded aggregate fixture."""
        self.queries.append(sql)

        return FakeQueryResult(
            column_names=["category", "label", "entity", "bucket_date", "count"],
            result_rows=[
                ("check", "fail", "", None, 2),
                ("check", "pass", "", None, 8),
                ("alert", "critical", "", None, 1),
                ("daily_check", "fail", "", "2026-06-10", 2),
                ("daily_check", "pass", "", "2026-06-10", 8),
                ("daily_alert", "critical", "", "2026-06-10", 1),
                ("table_check", "fail", "dq.fct_orders_daily", None, 2),
                ("table_check", "pass", "dq.fct_orders_daily", None, 8),
                ("table_alert", "critical", "dq.fct_orders_daily", None, 1),
                ("metadata", "certified", "", None, 3),
            ],
        )


def normalized_fixture_rows() -> list[dict[str, Any]]:
    """Return the fake query rows in the normalized tool-adapter shape."""
    result = FakeClickHouseClient().query("SELECT fixture")

    return [dict(zip(result.column_names, row)) for row in result.result_rows]


# --- Defining Validation Tests
def test_quality_summary_request_rejects_unbounded_or_invalid_scope() -> None:
    """Ensure callers cannot create arbitrary or unbounded summary queries."""
    with pytest.raises(ValueError, match="scope"):
        quality_summary.normalize_quality_summary_request(
            scope="arbitrary",
            start_date="2026-06-01",
            end_date="2026-06-10",
        )

    with pytest.raises(ValueError, match="cannot exceed"):
        quality_summary.normalize_quality_summary_request(
            scope="database",
            start_date="2026-01-01",
            end_date="2026-06-10",
        )

    with pytest.raises(ValueError, match="qualified"):
        quality_summary.normalize_quality_summary_request(
            scope="table",
            start_date="2026-06-01",
            end_date="2026-06-10",
            table_name="dq.fct_orders_daily; DROP TABLE dq.alerts",
        )


def test_quality_summary_sql_is_bounded_and_table_filtered() -> None:
    """Ensure per-table SQL uses literals, exact dates, FINAL alerts, and a hard limit."""
    sql = quality_summary.build_quality_summary_sql(
        scope="table",
        start_date="2026-06-04",
        end_date="2026-06-10",
        table_name="dq.fct_orders_daily",
    )

    assert sql.count("toDate('2026-06-04')") == 6
    assert sql.count("toDate('2026-06-10')") == 6
    assert sql.count("table_name = 'dq.fct_orders_daily'") == 6
    assert "qualified_name = 'dq.fct_orders_daily'" in sql
    assert "FROM dq.alerts FINAL" in sql
    assert "LIMIT 1000" in sql


# --- Defining Aggregation Tests
def test_quality_summary_payload_builds_health_and_advisory_rerun() -> None:
    """Ensure observed failures produce a bounded advisory, never an execution claim."""
    payload = quality_summary.build_quality_summary_payload(
        scope="database",
        start_date="2026-06-04",
        end_date="2026-06-10",
        rows=normalized_fixture_rows(),
    )

    assert payload["total_checks"] == 10
    assert payload["total_open_alerts"] == 1
    assert payload["registered_asset_count"] == 3
    assert payload["daily_counts"] == [
        {
            "dt": "2026-06-10",
            "total_checks": 10,
            "failed_checks": 2,
            "warning_checks": 0,
            "open_alerts": 1,
            "critical_alerts": 1,
        }
    ]
    assert payload["rerun_suggestions"] == [
        {
            "target_dag_id": "20_dag_dq_orders_dbt_transform",
            "affected_tables": ["dq.fct_orders_daily"],
            "start_date": "2026-06-04",
            "end_date": "2026-06-10",
            "mode": "advisory_only",
            "requires_approval": True,
            "reason": "Observed failed checks or critical alerts require evidence review before a bounded rerun.",
        }
    ]


def test_quality_summary_payload_rejects_malformed_rows() -> None:
    """Ensure malformed warehouse aggregates fail closed."""
    with pytest.raises(ValueError, match="invalid count"):
        quality_summary.build_quality_summary_payload(
            scope="weekly",
            start_date="2026-06-04",
            end_date="2026-06-10",
            rows=[
                {
                    "category": "check",
                    "label": "pass",
                    "entity": "",
                    "bucket_date": None,
                    "count": True,
                }
            ],
        )


def test_fetch_quality_summary_audits_public_totals(monkeypatch) -> None:
    """Ensure the public tool records one audited bounded query."""
    client         = FakeClickHouseClient()
    captured_audit: dict[str, Any] = {}

    monkeypatch.setattr(
        quality_summary,
        "write_agent_audit_event",
        lambda **kwargs: captured_audit.update(kwargs),
    )

    payload = quality_summary.fetch_weekly_quality_summary(
        end_date="2026-06-10",
        client=client,
        agent_run_id="11111111-1111-1111-1111-111111111111",
    )

    assert payload["scope"] == "weekly"
    assert payload["start_date"] == "2026-06-04"
    assert payload["end_date"] == "2026-06-10"
    assert len(client.queries) == 1
    assert captured_audit["action"] == "fetch_quality_summary"
    assert captured_audit["tool_name"] == "quality_summary"
    assert captured_audit["status"] == "success"
    assert captured_audit["row_count"] == 10
