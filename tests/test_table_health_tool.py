####
## Table Health Tool Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Validate bounded table-health evidence, trust states, and audit behavior."""

# --- Importing Libraries
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any
from uuid import uuid4

import pytest

from agent.tools import metadata_catalog, table_health


# --- Defining Test Doubles
class FakeQueryResult:
    """
    Represent one clickhouse-connect query result.

    Attributes:
        column_names: Ordered result column names.
        result_rows: Result tuples aligned to the columns.
    """

    def __init__(self, columns: list[str], rows: list[tuple[Any, ...]]) -> None:
        """
        Initialize one query result fixture.

        Args:
            columns: Ordered result column names.
            rows: Result tuples aligned to the columns.

        Returns:
            None.
        """
        self.column_names = columns
        self.result_rows  = rows


class FakeClickHouseClient:
    """
    Return ordered query results without connecting to ClickHouse.

    Attributes:
        results: Metadata, profile, and DQ query results in call order.
        queries: Captured read-only SQL statements.
    """

    def __init__(self, results: list[FakeQueryResult | Exception]) -> None:
        """
        Initialize the ordered result queue.

        Args:
            results: Query results or exceptions returned in call order.

        Returns:
            None.
        """
        self.results = list(results)
        self.queries: list[str] = []

    def query(self, sql: str) -> FakeQueryResult:
        """
        Capture SQL and return the next configured result.

        Args:
            sql: Read-only query issued by the table-health tool.

        Returns:
            Next configured query result.

        Raises:
            Exception: Configured query failure.
        """
        self.queries.append(sql)
        result = self.results.pop(0)

        if isinstance(result, Exception):
            raise result

        return result


# --- Defining Test Fixtures
PROFILE_COLUMNS = [
    "profile_run_id",
    "run_at",
    "dt",
    "table_name",
    "column_name",
    "metric_name",
    "metric_value",
    "metric_unit",
    "details_json",
]

DQ_COLUMNS = [
    "check_run_id",
    "run_at",
    "dt",
    "table_name",
    "check_name",
    "check_type",
    "status",
    "severity",
    "observed_value",
    "expected_value",
    "threshold_value",
    "details_json",
    "evidence_s3_uri",
]


def metadata_row(qualified_name: str = "dq.fct_orders_daily") -> tuple[Any, ...]:
    """
    Build one representative public metadata row.

    Args:
        qualified_name: Fully qualified warehouse asset identity.

    Returns:
        Tuple aligned to the public metadata catalog columns.
    """
    database_name, table_name = qualified_name.split(".", maxsplit=1)
    values = {
        "qualified_name": qualified_name,
        "database_name": database_name,
        "table_name": table_name,
        "display_name": "Daily Orders Fact",
        "description": "Curated daily order metrics.",
        "dataset": "orders",
        "domain": "commerce",
        "data_layer": "mart",
        "technical_owner": "Analytics Engineering",
        "business_owner": "Commerce Analytics",
        "grain": "One row per business date, country, and channel.",
        "refresh_frequency": "daily",
        "sla_time": "01:15",
        "sla_timezone": "Asia/Bangkok",
        "criticality": "critical",
        "sensitivity": "internal",
        "contains_pii": 0,
        "certification_status": "certified",
        "lifecycle_status": "active",
        "tags": ["orders", "analytics-ready"],
        "synced_at": datetime(2026, 8, 30, 0, 0, tzinfo=timezone.utc),
    }

    return tuple(values[column] for column in metadata_catalog.PUBLIC_METADATA_COLUMNS)


def profile_row(
    dt: date = date(2026, 6, 10),
    metric_name: str = "row_count",
    details_json: str = '{"source": "profiling"}',
) -> tuple[Any, ...]:
    """
    Build one profiling evidence row.

    Args:
        dt: Business date represented by the metric.
        metric_name: Stable profile metric name.
        details_json: Serialized profile details.

    Returns:
        Tuple aligned to PROFILE_COLUMNS.
    """
    return (
        uuid4(),
        datetime(2026, 6, 10, 1, 0, tzinfo=timezone.utc),
        dt,
        "dq.fct_orders_daily",
        "",
        metric_name,
        128.0,
        "rows",
        details_json,
    )


def dq_row(
    status: str = "pass",
    dt: date = date(2026, 6, 10),
    details_json: str = '{"rule": "required"}',
) -> tuple[Any, ...]:
    """
    Build one deterministic DQ result row.

    Args:
        status: DQ result status.
        dt: Business date checked by the rule.
        details_json: Serialized check details.

    Returns:
        Tuple aligned to DQ_COLUMNS.
    """
    return (
        uuid4(),
        datetime(2026, 6, 10, 1, 5, tzinfo=timezone.utc),
        dt,
        "dq.fct_orders_daily",
        "row_count_positive",
        "volume",
        status,
        "critical",
        128.0,
        1.0,
        0.0,
        details_json,
        "",
    )


def public_profile_row(dt: str = "2026-06-10") -> dict[str, Any]:
    """
    Build one normalized profile row for payload classification tests.

    Args:
        dt: ISO business date.

    Returns:
        Public profile row with parsed details.
    """
    return {
        "dt": dt,
        "table_name": "dq.fct_orders_daily",
        "metric_name": "row_count",
        "details": {},
    }


def public_dq_row(status: str = "pass", dt: str = "2026-06-10") -> dict[str, Any]:
    """
    Build one normalized DQ row for trust-state tests.

    Args:
        status: DQ outcome status.
        dt: ISO business date.

    Returns:
        Public DQ row with parsed details.
    """
    return {
        "dt": dt,
        "table_name": "dq.fct_orders_daily",
        "check_name": "row_count_positive",
        "status": status,
        "details": {},
    }


# --- Testing SQL Guardrails
def test_table_health_sql_is_read_only_date_filtered_and_bounded() -> None:
    """
    Ensure profile and DQ queries preserve exact table/date bounds and hard limits.

    Returns:
        None.
    """
    target_dt  = date(2026, 6, 10)
    profile_sql = table_health.build_table_health_profile_sql(
        table_name="dq.fct_orders_daily",
        dt=target_dt,
        lookback_days=14,
    )
    dq_sql = table_health.build_table_health_dq_sql(
        table_name="dq.fct_orders_daily",
        dt=target_dt,
        lookback_days=14,
    )

    for sql, hard_limit in (
        (profile_sql, table_health.MAX_PROFILE_ROWS),
        (dq_sql, table_health.MAX_DQ_ROWS),
    ):
        normalized_sql = " ".join(sql.lower().split())

        assert "select" in normalized_sql
        assert "table_name = 'dq.fct_orders_daily'" in normalized_sql
        assert "dt >= todate('2026-06-10') - interval 14 day" in normalized_sql
        assert "dt <= todate('2026-06-10')" in normalized_sql
        assert f"limit {hard_limit}" in normalized_sql
        assert all(
            keyword not in normalized_sql
            for keyword in (" insert ", " update ", " delete ", " drop ", " alter ")
        )


@pytest.mark.parametrize(
    ("table_name", "lookback_days", "error_pattern"),
    [
        ("dq.raw_orders; DROP TABLE dq.alerts", 14, "database.table format"),
        ("dq.raw_orders", -1, "between 0 and 30"),
        ("dq.raw_orders", 31, "between 0 and 30"),
    ],
)
def test_table_health_sql_rejects_unsafe_or_unbounded_inputs(
    table_name: str,
    lookback_days: int,
    error_pattern: str,
) -> None:
    """
    Ensure callers cannot inject SQL or request an unbounded history scan.

    Args:
        table_name: Candidate warehouse table identity.
        lookback_days: Candidate historical window.
        error_pattern: Expected validation error fragment.

    Returns:
        None.
    """
    with pytest.raises(ValueError, match=error_pattern):
        table_health.build_table_health_dq_sql(
            table_name=table_name,
            dt=date(2026, 6, 10),
            lookback_days=lookback_days,
        )


# --- Testing Trust Classification
@pytest.mark.parametrize(
    ("dq_rows", "profile_rows", "expected_state"),
    [
        ([], [public_profile_row()], "unverified"),
        ([public_dq_row("fail")], [public_profile_row()], "critical"),
        ([public_dq_row("warn")], [public_profile_row()], "warning"),
        ([public_dq_row("skip")], [public_profile_row()], "warning"),
        ([public_dq_row("unknown")], [public_profile_row()], "unverified"),
        ([public_dq_row("pass")], [], "warning"),
        ([public_dq_row("pass")], [public_profile_row()], "healthy"),
    ],
)
def test_classify_table_trust_state_is_conservative(
    dq_rows: list[dict[str, Any]],
    profile_rows: list[dict[str, Any]],
    expected_state: str,
) -> None:
    """
    Ensure trust never becomes healthy without passing DQ and profiling evidence.

    Args:
        dq_rows: Current deterministic DQ rows.
        profile_rows: Current profiling rows.
        expected_state: Expected conservative trust classification.

    Returns:
        None.
    """
    state, reason = table_health.classify_table_trust_state(dq_rows, profile_rows)

    assert state == expected_state
    assert reason


def test_build_table_health_payload_uses_only_current_date_for_trust() -> None:
    """
    Ensure historical failures do not replace exact target-date trust evidence.

    Returns:
        None.
    """
    payload = table_health.build_table_health_payload(
        table_name="dq.fct_orders_daily",
        dt=date(2026, 6, 10),
        lookback_days=14,
        metadata_asset={"qualified_name": "dq.fct_orders_daily"},
        profile_rows=[public_profile_row(), public_profile_row("2026-06-09")],
        dq_rows=[public_dq_row("pass"), public_dq_row("fail", "2026-06-09")],
    )

    assert payload["trust_state"] == "healthy"
    assert payload["current_check_count"] == 1
    assert payload["current_status_counts"] == {"pass": 1}
    assert payload["profile_metric_count"] == 2
    assert payload["dq_result_count"] == 2


# --- Testing Tool Results And Audit
def test_fetch_table_health_returns_public_payload_and_audits(monkeypatch) -> None:
    """
    Verify the tool joins deterministic evidence and records one safe audit event.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    client = FakeClickHouseClient(
        [
            FakeQueryResult(list(metadata_catalog.PUBLIC_METADATA_COLUMNS), [metadata_row()]),
            FakeQueryResult(PROFILE_COLUMNS, [profile_row()]),
            FakeQueryResult(DQ_COLUMNS, [dq_row()]),
        ]
    )
    audit_events: list[dict[str, Any]] = []

    monkeypatch.setattr(
        table_health,
        "write_agent_audit_event",
        lambda **kwargs: audit_events.append(kwargs),
    )

    payload = table_health.fetch_table_health(
        table_name="dq.fct_orders_daily",
        dt="2026-06-10",
        lookback_days=14,
        client=client,
    )

    assert payload["trust_state"] == "healthy"
    assert payload["metadata_registered"] is True
    assert payload["metadata_asset"]["technical_owner"] == "Analytics Engineering"
    assert payload["profile_metrics"][0]["details"] == {"source": "profiling"}
    assert payload["dq_results"][0]["details"] == {"rule": "required"}
    assert "details_json" not in payload["profile_metrics"][0]
    assert "details_json" not in payload["dq_results"][0]
    assert "sql" not in payload
    assert len(client.queries) == 3
    assert audit_events[0]["action"] == "fetch_table_health"
    assert audit_events[0]["status"] == "success"
    assert audit_events[0]["row_count"] == 3
    assert audit_events[0]["output_payload"]["trust_state"] == "healthy"


def test_fetch_table_health_rejects_malformed_evidence_and_audits_failure(monkeypatch) -> None:
    """
    Ensure malformed persisted JSON fails closed and remains auditable.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    client = FakeClickHouseClient(
        [
            FakeQueryResult(list(metadata_catalog.PUBLIC_METADATA_COLUMNS), []),
            FakeQueryResult(PROFILE_COLUMNS, [profile_row(details_json="{broken")]),
            FakeQueryResult(DQ_COLUMNS, [dq_row()]),
        ]
    )
    audit_events: list[dict[str, Any]] = []

    monkeypatch.setattr(
        table_health,
        "write_agent_audit_event",
        lambda **kwargs: audit_events.append(kwargs),
    )

    with pytest.raises(ValueError, match="details_json contains malformed JSON"):
        table_health.fetch_table_health(
            table_name="dq.fct_orders_daily",
            dt="2026-06-10",
            client=client,
        )

    assert audit_events[0]["action"] == "fetch_table_health"
    assert audit_events[0]["status"] == "failed"
    assert audit_events[0]["output_payload"] == {"error_type": "ValueError"}
