####
## Agent Audit Idempotency Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Contract tests for append-only and explicitly replay-safe agent audit events."""

# --- Importing Libraries
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import pytest

from agent.tools.audit_log import (
    AGENT_AUDIT_LOG_COLUMNS,
    build_audit_event_id,
    build_audit_idempotency_key,
    write_agent_audit_event,
)


# --- Defining Test Doubles
class FakeQueryResult:
    """
    Minimal ClickHouse query response.

    Attributes:
        result_rows: Scalar rows returned to audit existence checks.
    """

    def __init__(self, count: int) -> None:
        """
        Store one scalar count response.

        Args:
            count: Existing event count.

        Returns:
            None.
        """
        self.result_rows = [(count,)]


class FakeAuditClient:
    """
    Capture audit existence queries and typed inserts.

    Attributes:
        rows_by_id: Persisted rows keyed by audit UUID.
        queries: Query parameters received by the fake client.
        inserts: Typed insert calls received by the fake client.
    """

    def __init__(self) -> None:
        """
        Initialize empty in-memory audit storage.

        Returns:
            None.
        """
        self.rows_by_id: dict[UUID, dict[str, Any]] = {}
        self.queries: list[dict[str, Any]]           = []
        self.inserts: list[dict[str, Any]]           = []

    def query(self, sql: str, parameters: dict[str, Any]) -> FakeQueryResult:
        """
        Return whether one deterministic audit UUID already exists.

        Args:
            sql: Bounded ClickHouse existence query.
            parameters: Query parameters containing audit_id.

        Returns:
            Scalar query result with zero or one.
        """
        self.queries.append({"sql": sql, "parameters": parameters})
        audit_id = UUID(str(parameters["audit_id"]))

        return FakeQueryResult(int(audit_id in self.rows_by_id))

    def insert(
        self,
        table: str,
        data: list[list[Any]],
        column_names: list[str],
    ) -> None:
        """
        Capture one typed ClickHouse audit insert.

        Args:
            table: Fully qualified ClickHouse table.
            data: Row-oriented audit values.
            column_names: Explicit audit column order.

        Returns:
            None.
        """
        row = dict(zip(column_names, data[0], strict=True))
        self.inserts.append({"table": table, "data": data, "column_names": column_names})
        self.rows_by_id[row["audit_id"]] = row


# --- Defining Tests
def test_audit_idempotency_key_and_event_id_are_stable() -> None:
    """
    Ensure stable logical values produce the same audit key and UUID.

    Returns:
        None.
    """
    key_one = build_audit_idempotency_key("store_triage_report", "run-1", "report.md")
    key_two = build_audit_idempotency_key("store_triage_report", "run-1", "report.md")

    assert key_one == key_two
    assert len(key_one) == 64
    assert build_audit_event_id(key_one) == build_audit_event_id(key_two)


def test_append_only_audit_events_remain_distinct_without_idempotency_key() -> None:
    """
    Ensure ordinary attempts remain independently observable.

    Returns:
        None.
    """
    client = FakeAuditClient()

    write_agent_audit_event(client=client, action="tool_attempt", status="success")
    write_agent_audit_event(client=client, action="tool_attempt", status="success")

    assert len(client.inserts) == 2
    assert len(client.rows_by_id) == 2
    assert client.queries == []


def test_replay_safe_audit_event_is_inserted_once() -> None:
    """
    Ensure the same replay-safe logical event reuses one deterministic audit row.

    Returns:
        None.
    """
    client = FakeAuditClient()
    key    = build_audit_idempotency_key("triage_completed", "run-1", "RPT-ONE")

    for _ in range(2):
        write_agent_audit_event(
            client=client,
            action="triage_completed",
            status="success",
            agent_run_id="11111111-1111-4111-8111-111111111111",
            input_payload={"report_id": "RPT-ONE"},
            idempotency_key=key,
        )

    assert len(client.inserts) == 1
    assert len(client.queries) == 2

    row           = dict(zip(AGENT_AUDIT_LOG_COLUMNS, client.inserts[0]["data"][0], strict=True))
    input_payload = json.loads(row["input_json"])

    assert row["audit_id"] == build_audit_event_id(key)
    assert input_payload["_audit_idempotency_key"] == key


def test_replay_safe_audit_rejects_invalid_key() -> None:
    """
    Ensure arbitrary caller text cannot control deterministic event identity.

    Returns:
        None.
    """
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        write_agent_audit_event(
            client=FakeAuditClient(),
            action="triage_completed",
            status="success",
            idempotency_key="not-a-sha256-key",
        )


def test_replay_safe_audit_requires_dictionary_input() -> None:
    """
    Ensure the event key can be retained in a structured audit payload.

    Returns:
        None.
    """
    key = build_audit_idempotency_key("triage_completed", "run-1")

    with pytest.raises(ValueError, match="dictionary input payload"):
        write_agent_audit_event(
            client=FakeAuditClient(),
            action="triage_completed",
            status="success",
            input_payload="raw-input",
            idempotency_key=key,
        )
