####
## Schema Drift Alert Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Validate grouped schema alert evidence, identity, and deduplication behavior."""

# --- Importing Libraries
from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest

from agent.display import build_alert_one_liner, build_alert_title
from pipelines.schema_drift.config import load_named_schema_contract
from pipelines.schema_drift.generate_alerts import (
    SCHEMA_ALERT_METRIC,
    build_schema_alert_candidates,
    fetch_schema_drift_evidence,
    insert_new_schema_alerts,
    resolve_alert_date,
)
from pipelines.schema_drift.storage import SCHEMA_DRIFT_RESULTS_TABLE, SCHEMA_SNAPSHOTS_TABLE


# --- Defining Test Fakes
class FakeQueryResult:
    """
    Minimal clickhouse-connect query response.

    Attributes:
        result_rows: Rows returned by a fake query.
    """

    def __init__(self, result_rows: list[tuple[Any, ...]]) -> None:
        """
        Store deterministic query rows.

        Args:
            result_rows: Rows exposed to the production function.

        Returns:
            None.
        """
        self.result_rows = result_rows


class FakeSchemaAlertClient:
    """
    Return persisted schema evidence and capture alert inserts.

    Attributes:
        snapshot_rows: Persisted affected-table snapshot rows.
        finding_rows: Persisted warning/failure rows.
        existing_count: Matching unresolved alerts returned by deduplication lookup.
        queries: SQL issued by the alert generator.
        inserts: Explicit ClickHouse insert calls.
    """

    def __init__(
        self,
        snapshot_rows: list[tuple[Any, ...]],
        finding_rows: list[tuple[Any, ...]],
        existing_count: int = 0,
    ) -> None:
        """
        Initialize deterministic schema alert test state.

        Args:
            snapshot_rows: Rows returned for schema snapshots.
            finding_rows: Rows returned for schema comparison findings.
            existing_count: Existing open alert count for each candidate.

        Returns:
            None.
        """
        self.snapshot_rows  = snapshot_rows
        self.finding_rows   = finding_rows
        self.existing_count = existing_count
        self.queries: list[str] = []
        self.inserts: list[dict[str, Any]] = []

    def query(self, query: str) -> FakeQueryResult:
        """
        Return rows based on the bounded table referenced by SQL.

        Args:
            query: SQL emitted by the schema alert generator.

        Returns:
            Matching fake query result.
        """
        self.queries.append(query)

        if f"FROM {SCHEMA_SNAPSHOTS_TABLE}" in query:
            return FakeQueryResult(self.snapshot_rows)
        if f"FROM {SCHEMA_DRIFT_RESULTS_TABLE}" in query:
            return FakeQueryResult(self.finding_rows)
        if "FROM dq.alerts" in query:
            return FakeQueryResult([(self.existing_count,)])

        raise AssertionError(f"Unexpected schema alert query: {query}")

    def insert(self, table: str, data: list[tuple[Any, ...]], column_names: tuple[str, ...]) -> None:
        """
        Capture one explicit ClickHouse alert insert.

        Args:
            table: Target ClickHouse table.
            data: Alert rows submitted for insertion.
            column_names: Explicit alert column order.

        Returns:
            None.
        """
        self.inserts.append({"table": table, "data": data, "column_names": column_names})


# --- Defining Test Data
def schema_evidence_rows() -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    """
    Build one grouped table snapshot with two deterministic findings.

    Returns:
        Snapshot and finding rows matching ClickHouse query order.
    """
    contract_hash = "a" * 64
    schema_hash   = "b" * 64
    snapshots     = [
        (
            "orders_warehouse_schema",
            1,
            contract_hash,
            "dq.raw_orders",
            schema_hash,
            "fail",
            "critical",
            2,
        )
    ]
    findings = [
        (
            "dq.raw_orders",
            "order_id",
            "column_type",
            "fail",
            "critical",
            "String",
            "UInt64",
            json.dumps({"position": 2}),
        ),
        (
            "dq.raw_orders",
            "experimental_column",
            "unexpected_columns",
            "warn",
            "warning",
            "absent",
            "present",
            "{}",
        ),
    ]

    return snapshots, findings


# --- Defining Tests
def test_schema_drift_findings_are_grouped_into_one_table_alert() -> None:
    """
    Ensure multiple findings become one operator alert instead of alert fatigue.

    Returns:
        None.
    """
    snapshots, findings = schema_evidence_rows()
    client              = FakeSchemaAlertClient(snapshots, findings)
    contract, _         = load_named_schema_contract("orders")
    evidence            = fetch_schema_drift_evidence(client=client, run_id="manual__schema_alert_test")
    candidates          = build_schema_alert_candidates(
        contract=contract,
        evidence_rows=evidence,
        alert_dt=date(2026, 8, 7),
        run_id="manual__schema_alert_test",
    )

    assert len(evidence) == 1
    assert len(candidates) == 1
    assert candidates[0].severity == "critical"
    assert candidates[0].metric == SCHEMA_ALERT_METRIC
    assert candidates[0].observed_value == 2.0
    assert candidates[0].expected_value == 0.0
    assert candidates[0].source_check_run_id is None
    assert candidates[0].alert_key.startswith("orders|schema_drift|2026-08-07|dq.raw_orders|")
    assert candidates[0].details["finding_types"] == ["column_type", "unexpected_columns"]
    assert len(candidates[0].details["schema_fingerprint"]) == 64


def test_schema_alert_insert_is_idempotent_across_daily_run_dates() -> None:
    """
    Ensure an unresolved schema fingerprint is skipped even when its daily key changes.

    Returns:
        None.
    """
    snapshots, findings = schema_evidence_rows()
    client              = FakeSchemaAlertClient(snapshots, findings, existing_count=1)
    contract, _         = load_named_schema_contract("orders")
    evidence            = fetch_schema_drift_evidence(client=client, run_id="manual__schema_alert_retry")
    candidates          = build_schema_alert_candidates(
        contract=contract,
        evidence_rows=evidence,
        alert_dt=date(2026, 8, 8),
        run_id="manual__schema_alert_retry",
    )

    summary = insert_new_schema_alerts(client=client, candidates=candidates)
    lookup  = next(query for query in client.queries if "FROM dq.alerts" in query)

    assert summary["inserted"] == 0
    assert summary["skipped_existing"] == 1
    assert client.inserts == []
    assert "JSONExtractString(details_json, 'schema_fingerprint')" in lookup
    assert candidates[0].details["schema_fingerprint"] in lookup


def test_new_schema_fingerprint_uses_shared_alert_column_contract() -> None:
    """
    Ensure new schema incidents insert one row with structured evidence.

    Returns:
        None.
    """
    snapshots, findings = schema_evidence_rows()
    client              = FakeSchemaAlertClient(snapshots, findings, existing_count=0)
    contract, _         = load_named_schema_contract("orders")
    evidence            = fetch_schema_drift_evidence(client=client, run_id="manual__schema_alert_insert")
    candidates          = build_schema_alert_candidates(
        contract=contract,
        evidence_rows=evidence,
        alert_dt=date(2026, 8, 7),
        run_id="manual__schema_alert_insert",
    )

    summary = insert_new_schema_alerts(client=client, candidates=candidates)
    row     = client.inserts[0]["data"][0]
    details = json.loads(row[-1])

    assert summary == {"inserted": 1, "skipped_existing": 0, "skipped_alert_refs": []}
    assert client.inserts[0]["table"] == "dq.alerts"
    assert len(client.inserts[0]["column_names"]) == 14
    assert details["source_schema_run_id"] == "manual__schema_alert_insert"
    assert details["finding_count"] == 2


def test_clean_schema_run_creates_no_candidates_or_inserts() -> None:
    """
    Ensure clean persisted evidence is a no-op for alert generation.

    Returns:
        None.
    """
    client      = FakeSchemaAlertClient(snapshot_rows=[], finding_rows=[])
    contract, _ = load_named_schema_contract("orders")
    evidence    = fetch_schema_drift_evidence(client=client, run_id="manual__schema_alert_clean")
    candidates  = build_schema_alert_candidates(
        contract=contract,
        evidence_rows=evidence,
        alert_dt=date(2026, 8, 7),
        run_id="manual__schema_alert_clean",
    )
    summary = insert_new_schema_alerts(client=client, candidates=candidates)

    assert evidence == []
    assert candidates == []
    assert summary["inserted"] == 0
    assert client.inserts == []


def test_incomplete_schema_finding_evidence_fails_closed() -> None:
    """
    Ensure a partial persistence read cannot produce a misleading grouped alert.

    Returns:
        None.
    """
    snapshots, findings = schema_evidence_rows()
    client              = FakeSchemaAlertClient(snapshots, findings[:1])

    with pytest.raises(RuntimeError, match="finding_count mismatch"):
        fetch_schema_drift_evidence(client=client, run_id="manual__schema_alert_incomplete")


def test_schema_alert_date_resolution_uses_backfill_end_date() -> None:
    """
    Ensure one backfill-wide schema evaluation gets one clear operator date.

    Returns:
        None.
    """
    assert resolve_alert_date(dt="2026-08-07") == date(2026, 8, 7)
    assert resolve_alert_date(start="2026-08-01", end="2026-08-07") == date(2026, 8, 7)

    with pytest.raises(ValueError, match="either --dt"):
        resolve_alert_date(dt="2026-08-07", start="2026-08-01", end="2026-08-07")


def test_schema_alert_wording_is_human_readable() -> None:
    """
    Ensure Discord and UI helpers avoid exposing a raw metric as the headline.

    Returns:
        None.
    """
    alert = {
        "metric": SCHEMA_ALERT_METRIC,
        "table_name": "dq.raw_orders",
        "dt": "2026-08-07",
        "observed_value": 2,
        "expected_value": 0,
    }

    assert "schema contract change" in build_alert_title(alert).lower()
    assert "schema contract change" in build_alert_one_liner(alert).lower()
