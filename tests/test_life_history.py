####
## LIFE Evaluation History Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import pytest

from agent.tools.life_history import (
    build_life_history_sql,
    life_history_record_from_row,
    list_life_evaluation_history,
    normalize_evaluation_status,
    normalize_scenario_id,
)


# --- Defining Test Fakes
class FakeQueryResult:
    """
    Minimal ClickHouse query result for LIFE history tests.

    Attributes:
        column_names: Ordered query result columns.
        result_rows: Ordered query result tuples.
    """

    def __init__(self, result_rows: list[tuple[Any, ...]]) -> None:
        """
        Initialize one deterministic ClickHouse result.

        Args:
            result_rows: Row tuples returned by the fake query.

        Returns:
            None.
        """
        self.column_names = [
            "audit_id",
            "evaluated_at",
            "alert_id",
            "alert_key",
            "agent_run_id",
            "audit_status",
            "report_s3_uri",
            "output_json",
        ]
        self.result_rows = result_rows


class FakeClickHouseClient:
    """
    Capture the read-only LIFE history query and return deterministic rows.

    Attributes:
        result: Query result returned to the tool.
        queries: SQL statements executed by the tool.
    """

    def __init__(self, result_rows: list[tuple[Any, ...]]) -> None:
        """
        Initialize the fake client.

        Args:
            result_rows: Row tuples returned by every query.

        Returns:
            None.
        """
        self.result  = FakeQueryResult(result_rows)
        self.queries: list[str] = []

    def query(self, sql: str) -> FakeQueryResult:
        """
        Capture one SQL query and return the configured result.

        Args:
            sql: Read-only ClickHouse query.

        Returns:
            Configured fake query result.
        """
        self.queries.append(sql)

        return self.result


# --- Defining Test Data Helpers
def valid_life_output() -> dict[str, Any]:
    """
    Build one valid LIFE audit output payload.

    Returns:
        JSON-serializable LIFE evaluation summary.
    """
    return {
        "run_id": "life-eval-20260807T010203",
        "scenario_id": "missing_latest_day",
        "eval_status": "review",
        "failed_checks": ["confidence"],
        "failure_category": "low_confidence",
        "failure_categories": ["low_confidence"],
        "life_stage": "find_faults",
        "suggested_change_type": "evidence_plan_review",
        "suggested_change_summary": "Review evidence coverage before changing runtime behavior.",
        "requires_human_approval": True,
        "summary": "The report is evidence-backed but confidence remains below target.",
        "source_report_sha256": "a" * 64,
        "created_at": "2026-08-07T01:02:03+00:00",
        "json_report_s3_uri": "s3://dq-artifacts/agent-life/run_id=life-eval/report.json",
        "markdown_report_s3_uri": "s3://dq-artifacts/agent-life/run_id=life-eval/report.md",
    }


def valid_audit_row(output_json: str | None = None) -> dict[str, Any]:
    """
    Build one raw ClickHouse audit row.

    Args:
        output_json: Optional output payload override.

    Returns:
        Audit row consumed by the LIFE history normalizer.
    """
    return {
        "audit_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "evaluated_at": "2026-08-07T01:02:04+00:00",
        "alert_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "alert_key": "orders|dq_failure|2026-08-06|dq.raw_orders|row_count_positive|table",
        "agent_run_id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
        "audit_status": "success",
        "report_s3_uri": "s3://dq-artifacts/agent-life/run_id=life-eval/report.json",
        "output_json": output_json if output_json is not None else json.dumps(valid_life_output()),
    }


# --- Defining Tests
def test_life_history_sql_enforces_time_window_limit_and_allowlisted_filters() -> None:
    """
    Ensure LIFE history queries are read-only, date-bounded, and hard-limited.

    Returns:
        None.
    """
    sql = build_life_history_sql(
        eval_status="review",
        scenario_id="missing_latest_day",
        lookback_days=999,
        limit=999,
    )
    normalized = " ".join(sql.split())

    assert normalized.startswith("SELECT")
    assert "FROM dq.agent_audit_log" in normalized
    assert "action = 'life_evaluation_completed'" in normalized
    assert "ts >= now() - INTERVAL 365 DAY" in normalized
    assert "JSONExtractString(output_json, 'eval_status') = 'review'" in normalized
    assert "JSONExtractString(output_json, 'scenario_id') = 'missing_latest_day'" in normalized
    assert normalized.endswith("LIMIT 100")
    assert all(keyword not in normalized.upper() for keyword in ("INSERT ", "ALTER ", "DELETE ", "DROP "))


def test_life_history_filters_reject_unknown_or_unsafe_values() -> None:
    """
    Ensure status and scenario filters cannot introduce arbitrary SQL fragments.

    Returns:
        None.
    """
    with pytest.raises(ValueError, match="Unsupported LIFE evaluation status"):
        normalize_evaluation_status("succeeded")

    with pytest.raises(ValueError, match="scenario_id"):
        normalize_scenario_id("missing_latest_day' OR 1=1")


def test_life_history_record_hides_raw_payload_and_exposes_review_metadata() -> None:
    """
    Ensure the public record contains allowlisted summary fields only.

    Returns:
        None.
    """
    raw_payload = valid_life_output()
    raw_payload["private_debug"] = "do-not-expose"
    record = life_history_record_from_row(valid_audit_row(json.dumps(raw_payload)))
    public = record.model_dump(mode="json")

    assert public["eval_status"] == "review"
    assert public["scenario_id"] == "missing_latest_day"
    assert public["requires_human_approval"] is True
    assert public["payload_valid"] is True
    assert "output_json" not in public
    assert "input_json" not in public
    assert "private_debug" not in json.dumps(public)


def test_life_history_record_surfaces_malformed_payload_without_raw_content() -> None:
    """
    Ensure one malformed audit event remains visible without leaking its payload.

    Returns:
        None.
    """
    record = life_history_record_from_row(
        valid_audit_row('{"secret":"should-not-leak"')
    )
    public = record.model_dump(mode="json")

    assert public["eval_status"] == "unknown"
    assert public["payload_valid"] is False
    assert public["payload_error"] == "invalid_json"
    assert "should-not-leak" not in json.dumps(public)


def test_list_life_evaluation_history_returns_sanitized_bounded_result() -> None:
    """
    Ensure the public tool normalizes ClickHouse rows into the shared contract.

    Returns:
        None.
    """
    row = valid_audit_row()
    client = FakeClickHouseClient(
        result_rows=[
            (
                UUID(row["audit_id"]),
                datetime(2026, 8, 7, 1, 2, 4, tzinfo=timezone.utc),
                UUID(row["alert_id"]),
                row["alert_key"],
                UUID(row["agent_run_id"]),
                row["audit_status"],
                row["report_s3_uri"],
                row["output_json"],
            )
        ]
    )

    result = list_life_evaluation_history(
        client=client,
        eval_status="review",
        scenario_id="missing_latest_day",
        lookback_days=30,
        limit=10,
    )

    assert result.status == "success"
    assert result.row_count == 1
    assert result.rows[0].run_id == "life-eval-20260807T010203"
    assert result.rows[0].evaluated_at == "2026-08-07T01:02:04+00:00"
    assert result.rows[0].payload_valid is True
    assert len(client.queries) == 1
    assert "INTERVAL 30 DAY" in client.queries[0]
    assert "LIMIT 10" in client.queries[0]
