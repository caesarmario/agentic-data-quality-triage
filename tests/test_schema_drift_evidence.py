####
## Schema Drift Agent Evidence Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Validate guarded schema evidence reads, auditability, and triage hypotheses."""

# --- Importing Libraries
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest

from agent.graph import build_hypotheses_for_state
from agent.state import Alert, EvidenceItem, EvidenceType, TriageState
from agent.tools import schema_drift
from agent.tools.audit_log import AGENT_AUDIT_LOG_COLUMNS
from agent.tools.schema_drift import (
    build_schema_findings_sql,
    build_schema_snapshot_sql,
    collect_schema_drift_evidence,
    fetch_schema_drift_run_context,
)
from pipelines.schema_drift.storage import SCHEMA_DRIFT_RESULTS_TABLE, SCHEMA_SNAPSHOTS_TABLE


# --- Defining Test Fakes
class FakeQueryResult:
    """
    Represent the subset of clickhouse-connect query output used by the tool.

    Attributes:
        result_rows: Rows returned by the fake query.
        column_names: Column names matching the query projection.
    """

    def __init__(self, result_rows: list[tuple[Any, ...]], column_names: tuple[str, ...]) -> None:
        """
        Store deterministic query rows and columns.

        Args:
            result_rows: Rows returned to production code.
            column_names: Ordered projection names.

        Returns:
            None.
        """
        self.result_rows = result_rows
        self.column_names = column_names


class FakeSchemaEvidenceClient:
    """
    Return schema evidence and capture agent audit inserts.

    Attributes:
        snapshot_rows: Exact parent snapshot rows.
        finding_rows: Exact warning/failure rows.
        queries: SQL statements issued by the tool.
        inserts: Audit rows written after the tool call.
    """

    def __init__(
        self,
        snapshot_rows: list[tuple[Any, ...]],
        finding_rows: list[tuple[Any, ...]],
        query_error: Exception | None = None,
    ) -> None:
        """
        Initialize deterministic ClickHouse behavior.

        Args:
            snapshot_rows: Rows returned for dq.schema_snapshots.
            finding_rows: Rows returned for dq.schema_drift_results.
            query_error: Optional exception raised before any evidence row is returned.

        Returns:
            None.
        """
        self.snapshot_rows = snapshot_rows
        self.finding_rows  = finding_rows
        self.query_error   = query_error
        self.queries: list[str]           = []
        self.inserts: list[dict[str, Any]] = []

    def query(self, query: str) -> FakeQueryResult:
        """
        Route a bounded SQL statement to deterministic evidence rows.

        Args:
            query: SQL emitted by the schema evidence tool.

        Returns:
            Fake query result with matching columns.

        Raises:
            Exception: Configured query_error for failure-path tests.
            AssertionError: If the tool issues an unexpected query.
        """
        self.queries.append(query)

        if self.query_error:
            raise self.query_error

        if f"FROM {SCHEMA_SNAPSHOTS_TABLE}" in query:
            return FakeQueryResult(self.snapshot_rows, schema_drift.SNAPSHOT_COLUMNS)

        if f"FROM {SCHEMA_DRIFT_RESULTS_TABLE}" in query:
            return FakeQueryResult(self.finding_rows, schema_drift.FINDING_COLUMNS)

        raise AssertionError(f"Unexpected schema evidence query: {query}")

    def insert(self, table: str, data: list[list[Any]], column_names: list[str]) -> None:
        """
        Capture an explicit ClickHouse audit insert.

        Args:
            table: Target ClickHouse table.
            data: Rows submitted for insertion.
            column_names: Explicit audit column order.

        Returns:
            None.
        """
        self.inserts.append({"table": table, "data": data, "column_names": column_names})


# --- Defining Test Data
def build_schema_alert(**details_overrides: Any) -> Alert:
    """
    Build a representative schema drift alert with exact source correlation.

    Args:
        details_overrides: Optional schema alert detail fields to replace.

    Returns:
        Validated schema drift Alert.
    """
    details = {
        "source_schema_run_id": "manual__schema_evidence_test",
        "contract_sha256": "a" * 64,
        "schema_sha256": "b" * 64,
        "finding_count": 2,
    }
    details.update(details_overrides)

    return Alert(
        alert_id=uuid4(),
        alert_key="orders|schema_drift|2026-08-08|dq.raw_orders|schema_contract_drift|fingerprint",
        alert_type="schema_drift",
        severity="critical",
        table_name="dq.raw_orders",
        metric="schema_contract_drift",
        dt="2026-08-08",
        observed_value=2,
        expected_value=0,
        details=details,
    )


def build_schema_rows(
    finding_count: int = 2,
) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    """
    Build one schema snapshot and bounded finding rows.

    Args:
        finding_count: Complete finding count reported by snapshot and window query.

    Returns:
        Snapshot rows and finding rows matching production projections.
    """
    observed_at = datetime(2026, 8, 8, 0, 5, tzinfo=timezone.utc)
    snapshots    = [
        (
            "manual__schema_evidence_test",
            observed_at,
            "orders_warehouse_schema",
            1,
            "a" * 64,
            "dq.raw_orders",
            "b" * 64,
            "fail",
            "critical",
            20,
            finding_count,
        )
    ]
    base_findings = [
        (
            "order_id",
            "column_type",
            "fail",
            "critical",
            "String",
            "UInt64",
            json.dumps({"position": 2}),
            finding_count,
        ),
        (
            "experimental_column",
            "unexpected_columns",
            "warn",
            "warning",
            "absent",
            "present",
            "{}",
            finding_count,
        ),
        (
            "customer_id",
            "column_default",
            "warn",
            "warning",
            "empty",
            "DEFAULT ''",
            "{}",
            finding_count,
        ),
    ]

    return snapshots, base_findings[:finding_count]


def audit_row(client: FakeSchemaEvidenceClient) -> dict[str, Any]:
    """
    Convert the latest captured insert into a named audit dictionary.

    Args:
        client: Fake client containing at least one audit insert.

    Returns:
        Audit row keyed by dq.agent_audit_log column names.
    """
    return dict(zip(AGENT_AUDIT_LOG_COLUMNS, client.inserts[-1]["data"][0], strict=True))


# --- Defining Guardrail Tests
def test_schema_evidence_sql_is_exact_bounded_and_read_only() -> None:
    """
    Ensure schema queries cannot expand beyond the alert run and table.

    Returns:
        None.
    """
    snapshot_sql = build_schema_snapshot_sql(
        run_id="manual__schema_evidence_test",
        qualified_name="dq.raw_orders",
    )
    findings_sql = build_schema_findings_sql(
        run_id="manual__schema_evidence_test",
        qualified_name="dq.raw_orders",
        limit=25,
    )

    for query in (snapshot_sql, findings_sql):
        normalized = " ".join(query.lower().split())

        assert normalized.startswith("select")
        assert "where run_id = 'manual__schema_evidence_test'" in normalized
        assert "qualified_name = 'dq.raw_orders'" in normalized
        assert all(keyword not in normalized for keyword in (" insert ", " alter ", " drop ", " delete "))

    assert "LIMIT 2" in snapshot_sql
    assert "LIMIT 26" in findings_sql
    assert "multiIf(severity = 'critical', 3" in findings_sql

    with pytest.raises(ValueError, match="unsupported characters"):
        build_schema_snapshot_sql("manual__run;drop table dq.raw_orders", "dq.raw_orders")

    with pytest.raises(ValueError, match="database.table"):
        build_schema_findings_sql("manual__safe", "raw_orders")


def test_schema_evidence_tool_returns_exact_context_and_success_audit(monkeypatch) -> None:
    """
    Ensure one tool call returns exact evidence and persists a secret-safe SQL hash.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    snapshots, findings = build_schema_rows()
    client              = FakeSchemaEvidenceClient(snapshots, findings)
    monkeypatch.setattr(schema_drift, "build_clickhouse_client", lambda **kwargs: client)

    evidence = collect_schema_drift_evidence(
        alert=build_schema_alert(),
        agent_run_id=uuid4(),
    )
    row = audit_row(client)

    assert evidence.evidence_type == "schema_drift"
    assert evidence.tool_name == "schema_drift"
    assert evidence.row_count == 2
    assert "confirms 2 finding(s)" in evidence.summary
    assert evidence.rows[0]["contract_name"] == "orders_warehouse_schema"
    assert evidence.rows[0]["expected_value"] == "String"
    assert len(client.queries) == 2
    assert client.inserts[-1]["table"] == "dq.agent_audit_log"
    assert row["action"] == "fetch_schema_drift_context"
    assert row["status"] == "success"
    assert row["tool_name"] == "schema_drift"
    assert len(row["sql_hash"]) == 64
    assert "SELECT" not in row["input_json"]
    assert "SELECT" not in row["output_json"]


def test_schema_run_context_accepts_clean_snapshot_and_audits_zero_findings(monkeypatch) -> None:
    """
    Ensure the specialist can assess an exact clean detector run without a synthetic alert.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    observed_at = datetime(2026, 8, 8, 0, 5, tzinfo=timezone.utc)
    snapshots = [
        (
            "manual__schema_evidence_clean",
            observed_at,
            "orders_warehouse_schema",
            1,
            "a" * 64,
            "dq.raw_orders",
            "b" * 64,
            "pass",
            "info",
            20,
            0,
        )
    ]
    client = FakeSchemaEvidenceClient(snapshots, [])
    monkeypatch.setattr(schema_drift, "build_clickhouse_client", lambda **kwargs: client)

    context = fetch_schema_drift_run_context(
        source_run_id="manual__schema_evidence_clean",
        qualified_name="dq.raw_orders",
        agent_run_id=uuid4(),
    )
    row = audit_row(client)

    assert context["finding_count"] == 0
    assert context["findings"] == []
    assert context["snapshot"]["snapshot_status"] == "pass"
    assert "confirms no drift findings" in context["summary"]
    assert len(client.queries) == 2
    assert row["action"] == "fetch_schema_drift_run_context"
    assert row["status"] == "success"
    assert row["row_count"] == 0


def test_schema_evidence_tool_reports_bounded_truncation(monkeypatch) -> None:
    """
    Ensure large findings remain bounded while preserving the complete persisted count.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    snapshots, findings = build_schema_rows(finding_count=3)
    client              = FakeSchemaEvidenceClient(snapshots, findings)
    monkeypatch.setattr(schema_drift, "build_clickhouse_client", lambda **kwargs: client)

    alert    = build_schema_alert(finding_count=3)
    evidence = collect_schema_drift_evidence(alert=alert, finding_limit=2, agent_run_id=uuid4())
    output   = json.loads(audit_row(client)["output_json"])

    assert evidence.row_count == 2
    assert "1 additional finding(s) were omitted" in evidence.summary
    assert output["finding_count"] == 3
    assert output["visible_finding_count"] == 2
    assert output["findings_truncated"] == 1


def test_schema_evidence_tool_fails_closed_and_audits_mismatched_snapshot(monkeypatch) -> None:
    """
    Ensure stale alert hashes cannot silently attach evidence from another snapshot.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    snapshots, findings = build_schema_rows()
    client              = FakeSchemaEvidenceClient(snapshots, findings)
    monkeypatch.setattr(schema_drift, "build_clickhouse_client", lambda **kwargs: client)

    with pytest.raises(RuntimeError, match="schema_sha256"):
        collect_schema_drift_evidence(
            alert=build_schema_alert(schema_sha256="c" * 64),
            agent_run_id=uuid4(),
        )

    row = audit_row(client)

    assert row["status"] == "failed"
    assert json.loads(row["output_json"])["error_type"] == "RuntimeError"
    assert len(client.queries) == 1


def test_schema_evidence_tool_blocks_non_schema_alert_and_writes_audit(monkeypatch) -> None:
    """
    Ensure a generic DQ alert cannot invoke the schema-specific collector.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    client = FakeSchemaEvidenceClient([], [])
    monkeypatch.setattr(schema_drift, "build_clickhouse_client", lambda **kwargs: client)
    alert = build_schema_alert().model_copy(
        update={"alert_type": "dq_failure", "metric": "row_count_positive"}
    )

    with pytest.raises(ValueError, match="only be collected"):
        collect_schema_drift_evidence(alert=alert, agent_run_id=uuid4())

    assert client.queries == []
    assert audit_row(client)["status"] == "blocked"


# --- Defining Triage Policy Tests
def test_schema_drift_hypothesis_uses_exact_evidence_and_blocks_auto_alter() -> None:
    """
    Ensure schema alerts use a specific high-confidence, human-reviewed hypothesis.

    Returns:
        None.
    """
    state = TriageState(
        alert=build_schema_alert(),
        evidence=[
            EvidenceItem(
                evidence_type=EvidenceType.SCHEMA_DRIFT,
                tool_name="schema_drift",
                description="Exact persisted schema findings.",
                rows=[
                    {
                        "column_name": "order_id",
                        "check_type": "column_type",
                        "status": "fail",
                        "severity": "critical",
                    }
                ],
                summary="One breaking schema contract finding.",
            )
        ],
    )

    hypotheses = build_hypotheses_for_state(state)
    state.hypotheses = hypotheses

    assert hypotheses[0].root_cause_category == "breaking_schema_change"
    assert hypotheses[0].confidence == 0.94
    assert "Do not alter" in hypotheses[0].recommended_action
    assert state.should_collect_more_evidence is False


def test_schema_drift_hypothesis_requests_more_evidence_when_source_read_failed() -> None:
    """
    Ensure missing exact evidence remains uncertain and enters the bounded retry loop.

    Returns:
        None.
    """
    state            = TriageState(alert=build_schema_alert())
    state.hypotheses = build_hypotheses_for_state(state)

    assert state.hypotheses[0].root_cause_category == "schema_evidence_unavailable"
    assert state.hypotheses[0].confidence == 0.45
    assert state.should_collect_more_evidence is True
