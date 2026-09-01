####
## Agent Context Persistence Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Validate bounded run context, durable incident memory, and ClickHouse storage."""

# --- Importing Libraries
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from agent.context.models import (
    IncidentMemoryType,
    RunContextPhase,
    build_incident_memory_record,
    build_run_context_event,
)
from agent.context.store import (
    INCIDENT_MEMORY_COLUMNS,
    INCIDENT_MEMORY_TABLE,
    RUN_CONTEXT_COLUMNS,
    RUN_CONTEXT_TABLE,
    build_incident_memory_query,
    ensure_agent_context_tables,
    fetch_incident_memory,
    persist_incident_memory,
    persist_run_context_event,
    read_agent_context_ddl_statements,
)
from agent.specialists.contracts import (
    AgentApprovalState,
    AgentTaskStatus,
    ContextReference,
    ContextReferenceType,
    EvidenceReference,
)
from scripts.verify_control_plane_supervisor import (
    verify_incident_memory_evidence,
    verify_run_context_evidence,
)


# --- Defining Test Doubles
class FakeClickHouseClient:
    """Capture DDL and insert calls without requiring live ClickHouse."""

    def __init__(self) -> None:
        """Initialize empty command and insert histories."""
        self.commands: list[str] = []
        self.inserts: list[dict[str, Any]] = []

    def command(self, sql: str) -> None:
        """
        Capture one ClickHouse command.

        Args:
            sql: DDL statement executed by the context schema ensurer.

        Returns:
            None.
        """
        self.commands.append(sql)

    def insert(
        self,
        table: str,
        data: list[list[Any]],
        column_names: tuple[str, ...],
    ) -> None:
        """
        Capture one clickhouse-connect insert call.

        Args:
            table: Fully qualified ClickHouse table.
            data: Row-oriented insert values.
            column_names: Explicit insert-column order.

        Returns:
            None.
        """
        self.inserts.append(
            {
                "table": table,
                "data": data,
                "column_names": column_names,
            }
        )


class FakeQueryResult:
    """Provide clickhouse-connect-compatible query metadata and rows."""

    def __init__(self, columns: tuple[str, ...], rows: list[tuple[Any, ...]]) -> None:
        """
        Initialize one fixed query result.

        Args:
            columns: Result column names.
            rows: Result tuples in matching order.

        Returns:
            None.
        """
        self.column_names = list(columns)
        self.result_rows  = rows


class FakeQueryClient:
    """Return one fixed query result while retaining generated SQL."""

    def __init__(self, result: FakeQueryResult) -> None:
        """
        Initialize the query client.

        Args:
            result: Fixed result returned for every query.

        Returns:
            None.
        """
        self.result = result
        self.sql    = ""

    def query(self, sql: str) -> FakeQueryResult:
        """
        Capture a bounded read query and return fixed data.

        Args:
            sql: Generated incident-memory query.

        Returns:
            Preconfigured FakeQueryResult.
        """
        self.sql = sql

        return self.result


# --- Defining Fixtures
def sample_evidence_reference() -> EvidenceReference:
    """
    Build one durable evidence pointer without copying raw query output.

    Returns:
        EvidenceReference pointing to a persisted report artifact.
    """
    return EvidenceReference(
        evidence_type="report_artifact",
        source_tool="s3_artifact_store",
        reference="s3://dq-artifacts/agent-reports/report.json",
        summary="Structured triage evidence persisted for operator review.",
    )


def sample_context_reference() -> ContextReference:
    """
    Build one explicit run-context reference used by a specialist handoff.

    Returns:
        ContextReference containing only a stable persistence identifier.
    """
    return ContextReference(
        reference_type=ContextReferenceType.RUN_CONTEXT,
        reference="run-context:123e4567-e89b-12d3-a456-426614174000",
        description="Persisted bounded context for this investigation.",
    )


# --- Testing Run-Scoped Context
def test_run_context_event_is_deterministic_and_ttl_bounded() -> None:
    """The same parent and phase must produce one stable idempotent event."""
    parent_run_id = uuid4()
    occurred_at   = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
    event_one     = build_run_context_event(
        parent_run_id=parent_run_id,
        external_run_id="manual__context_test",
        phase=RunContextPhase.ROUTED,
        requester="airflow",
        status=AgentTaskStatus.RUNNING,
        selected_specialist="metadata_lineage_agent",
        task_type="asset_context",
        context_references=[sample_context_reference()],
        decision_facts={"resolved_intent": "asset_context"},
        retention_days=30,
        occurred_at=occurred_at,
    )
    event_two = build_run_context_event(
        parent_run_id=parent_run_id,
        external_run_id="manual__context_test",
        phase=RunContextPhase.ROUTED,
        requester="airflow",
        status=AgentTaskStatus.RUNNING,
        selected_specialist="metadata_lineage_agent",
        task_type="asset_context",
        context_references=[sample_context_reference()],
        decision_facts={"resolved_intent": "asset_context"},
        retention_days=30,
        occurred_at=occurred_at,
    )

    assert event_one.context_event_id == event_two.context_event_id
    assert event_one.content_sha256 == event_two.content_sha256
    assert event_one.event_sequence == 20
    assert (event_one.expires_at - event_one.occurred_at).days == 30


@pytest.mark.parametrize(
    "decision_facts",
    [
        {"prompt": "hidden system instruction"},
        {"nested": {"conversation_history": ["raw message"]}},
        {"raw_sql": "SELECT * FROM dq.raw_orders"},
        {"raw_tool_output": [{"order_id": "sensitive-row"}]},
    ],
)
def test_run_context_rejects_hidden_or_raw_payloads(
    decision_facts: dict[str, Any],
) -> None:
    """
    Hidden prompts, conversations, raw SQL, and raw tool rows must fail pre-insert.

    Args:
        decision_facts: Unsafe persisted context payload under test.

    Returns:
        None.
    """
    with pytest.raises(ValueError, match="forbidden hidden-context key"):
        build_run_context_event(
            parent_run_id=uuid4(),
            external_run_id="manual__unsafe_context",
            phase=RunContextPhase.STARTED,
            requester="airflow",
            status=AgentTaskStatus.RUNNING,
            decision_facts=decision_facts,
        )


def test_run_context_rejects_retention_above_policy() -> None:
    """Temporary shared context may not become accidental permanent memory."""
    with pytest.raises(ValueError, match="retention_days must be between"):
        build_run_context_event(
            parent_run_id=uuid4(),
            external_run_id="manual__excessive_retention",
            phase=RunContextPhase.STARTED,
            requester="airflow",
            status=AgentTaskStatus.RUNNING,
            retention_days=91,
        )


# --- Testing Durable Incident Memory
def test_incident_memory_is_deterministic_and_evidence_driven() -> None:
    """One investigation outcome must retain stable identity and evidence pointers."""
    parent_run_id = uuid4()
    recorded_at   = datetime(2026, 8, 20, 11, 0, tzinfo=timezone.utc)
    kwargs        = {
        "parent_run_id": parent_run_id,
        "outcome_status": AgentTaskStatus.SUCCESS,
        "specialist_name": "incident_triage_agent",
        "task_type": "triage_alert",
        "summary": "  Missing orders partition   confirmed.  ",
        "alert_key": (
            "orders|dq_failure|2026-08-20|dq.raw_orders|row_count_positive|table"
        ),
        "alert_display_id": "DQ-20260820-A1B2C3",
        "evidence_references": [sample_evidence_reference()],
        "decision_facts": {"top_hypothesis_category": "missing_partition"},
        "report_s3_uri": "s3://dq-artifacts/agent-reports/report.md",
        "approval_state": AgentApprovalState.PENDING,
        "recorded_at": recorded_at,
    }
    record_one = build_incident_memory_record(**kwargs)
    record_two = build_incident_memory_record(**kwargs)

    assert record_one.memory_id == record_two.memory_id
    assert record_one.memory_key == record_two.memory_key
    assert record_one.content_sha256 == record_two.content_sha256
    assert record_one.memory_type == IncidentMemoryType.INVESTIGATION_OUTCOME
    assert record_one.summary == "Missing orders partition confirmed."
    assert record_one.approval_state == AgentApprovalState.PENDING


def test_successful_incident_memory_requires_identity_and_evidence() -> None:
    """Successful investigation memory must not become an unsupported narrative."""
    with pytest.raises(ValueError, match="requires an alert identity"):
        build_incident_memory_record(
            parent_run_id=uuid4(),
            outcome_status=AgentTaskStatus.SUCCESS,
            specialist_name="incident_triage_agent",
            task_type="triage_alert",
            summary="Missing identity.",
            evidence_references=[sample_evidence_reference()],
        )

    with pytest.raises(ValueError, match="requires a summary and evidence references"):
        build_incident_memory_record(
            parent_run_id=uuid4(),
            outcome_status=AgentTaskStatus.SUCCESS,
            specialist_name="incident_triage_agent",
            task_type="triage_alert",
            summary="Unsupported conclusion.",
            alert_display_id="DQ-20260820-NOEV01",
        )


# --- Testing ClickHouse DDL And Persistence
def test_agent_context_ddl_defines_temporary_and_durable_storage() -> None:
    """Modular bootstrap must define exactly the two purpose-specific tables."""
    statements = read_agent_context_ddl_statements()
    ddl        = "\n".join(statements)

    assert len(statements) == 2
    assert "dq.agent_run_context_events" in ddl
    assert "dq.incident_memory" in ddl
    assert "TTL expires_at DELETE" in ddl
    assert ddl.count("ReplacingMergeTree") == 2


def test_context_schema_and_records_use_explicit_clickhouse_contracts() -> None:
    """DDL and inserts must use fixed tables, columns, and canonical JSON payloads."""
    client        = FakeClickHouseClient()
    parent_run_id = uuid4()
    context_event = build_run_context_event(
        parent_run_id=parent_run_id,
        external_run_id="manual__context_persistence",
        phase=RunContextPhase.ROUTED,
        requester="airflow",
        status=AgentTaskStatus.RUNNING,
        selected_specialist="incident_triage_agent",
        task_type="triage_alert",
        context_references=[sample_context_reference()],
        decision_facts={"resolved_intent": "triage_alert"},
    )
    memory_record = build_incident_memory_record(
        parent_run_id=parent_run_id,
        outcome_status=AgentTaskStatus.SUCCESS,
        specialist_name="incident_triage_agent",
        task_type="triage_alert",
        summary="Evidence supports a missing partition.",
        alert_display_id="DQ-20260820-PERS01",
        evidence_references=[sample_evidence_reference()],
        decision_facts={"top_hypothesis_category": "missing_partition"},
    )

    ensure_agent_context_tables(client)
    context_id = persist_run_context_event(client, context_event)
    memory_id  = persist_incident_memory(client, memory_record)

    assert len(client.commands) == 2
    assert context_id == context_event.context_event_id
    assert memory_id == memory_record.memory_id
    assert client.inserts[0]["table"] == RUN_CONTEXT_TABLE
    assert client.inserts[0]["column_names"] == RUN_CONTEXT_COLUMNS
    assert client.inserts[1]["table"] == INCIDENT_MEMORY_TABLE
    assert client.inserts[1]["column_names"] == INCIDENT_MEMORY_COLUMNS

    context_row = dict(zip(RUN_CONTEXT_COLUMNS, client.inserts[0]["data"][0], strict=True))
    memory_row  = dict(zip(INCIDENT_MEMORY_COLUMNS, client.inserts[1]["data"][0], strict=True))

    assert json.loads(context_row["decision_json"]) == {
        "resolved_intent": "triage_alert"
    }
    assert json.loads(memory_row["evidence_references_json"])[0]["reference"].startswith(
        "s3://dq-artifacts/"
    )


# --- Testing Bounded Memory Reads
def test_incident_memory_query_enforces_lookback_limit_and_exact_identity() -> None:
    """Memory lookup must remain read-only, time-bounded, escaped, and hard-limited."""
    sql = build_incident_memory_query(
        alert_reference="DQ-'20260820",
        lookback_days=30,
        limit=7,
    )

    assert f"FROM {INCIDENT_MEMORY_TABLE} FINAL" in sql
    assert "recorded_at >= now64(3) - INTERVAL 30 DAY" in sql
    assert "alert_display_id = 'DQ-\\'20260820'" in sql
    assert "LIMIT 7" in sql

    with pytest.raises(ValueError, match="single-line"):
        build_incident_memory_query("DQ-TEST\nDROP TABLE")

    with pytest.raises(ValueError, match="lookback_days"):
        build_incident_memory_query("DQ-TEST", lookback_days=0)

    with pytest.raises(ValueError, match="limit"):
        build_incident_memory_query("DQ-TEST", limit=101)


def test_fetch_incident_memory_returns_typed_records() -> None:
    """Persisted JSON must be parsed back into the strict durable-memory contract."""
    record = build_incident_memory_record(
        parent_run_id=uuid4(),
        outcome_status=AgentTaskStatus.SUCCESS,
        specialist_name="incident_triage_agent",
        task_type="triage_alert",
        summary="Typed memory lookup succeeded.",
        alert_display_id="DQ-20260820-FETCH1",
        evidence_references=[sample_evidence_reference()],
        decision_facts={"confidence": 0.91},
        report_s3_uri="s3://dq-artifacts/agent-reports/report.md",
    )
    insert_client = FakeClickHouseClient()

    persist_incident_memory(insert_client, record)
    persisted_row = insert_client.inserts[0]["data"][0]
    query_client   = FakeQueryClient(
        FakeQueryResult(
            columns=INCIDENT_MEMORY_COLUMNS,
            rows=[tuple(persisted_row)],
        )
    )

    records = fetch_incident_memory(
        client=query_client,
        alert_reference=record.alert_display_id,
        lookback_days=30,
        limit=5,
    )

    assert len(records) == 1
    assert isinstance(records[0].memory_id, UUID)
    assert records[0].memory_id == record.memory_id
    assert records[0].evidence_references == record.evidence_references
    assert records[0].decision_facts == {"confidence": 0.91}
    assert "LIMIT 5" in query_client.sql


def test_fetch_incident_memory_rejects_malformed_persisted_json() -> None:
    """Corrupt durable JSON must fail explicitly instead of returning partial memory."""
    record = build_incident_memory_record(
        parent_run_id=uuid4(),
        outcome_status=AgentTaskStatus.SUCCESS,
        specialist_name="incident_triage_agent",
        task_type="triage_alert",
        summary="Malformed JSON boundary test.",
        alert_display_id="DQ-20260820-BADJS1",
        evidence_references=[sample_evidence_reference()],
    )
    insert_client = FakeClickHouseClient()

    persist_incident_memory(insert_client, record)
    persisted_row = list(insert_client.inserts[0]["data"][0])
    evidence_index = INCIDENT_MEMORY_COLUMNS.index("evidence_references_json")
    persisted_row[evidence_index] = "{malformed"
    query_client = FakeQueryClient(
        FakeQueryResult(
            columns=INCIDENT_MEMORY_COLUMNS,
            rows=[tuple(persisted_row)],
        )
    )

    with pytest.raises(ValueError, match="Malformed persisted JSON"):
        fetch_incident_memory(query_client, record.alert_display_id)


# --- Testing Operational Evidence Verification
def test_supervisor_verifier_accepts_exact_context_and_incident_memory() -> None:
    """Operational acceptance must bind lifecycle context and memory to one parent run."""
    parent_run_id = uuid4()
    external_run  = "manual__context_verifier"
    occurred_at   = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    evidence      = sample_evidence_reference()
    context_events = [
        build_run_context_event(
            parent_run_id=parent_run_id,
            external_run_id=external_run,
            phase=RunContextPhase.STARTED,
            requester="airflow",
            status=AgentTaskStatus.RUNNING,
            decision_facts={"requested_intent": "triage_alert"},
            occurred_at=occurred_at,
        ),
        build_run_context_event(
            parent_run_id=parent_run_id,
            external_run_id=external_run,
            phase=RunContextPhase.ROUTED,
            requester="airflow",
            status=AgentTaskStatus.RUNNING,
            selected_specialist="incident_triage_agent",
            task_type="triage_alert",
            context_references=[sample_context_reference()],
            decision_facts={"resolved_intent": "triage_alert"},
            occurred_at=occurred_at,
        ),
        build_run_context_event(
            parent_run_id=parent_run_id,
            external_run_id=external_run,
            phase=RunContextPhase.COMPLETED,
            requester="airflow",
            status=AgentTaskStatus.SUCCESS,
            selected_specialist="incident_triage_agent",
            task_type="triage_alert",
            evidence_references=[evidence],
            decision_facts={"confidence": 0.91},
            report_s3_uri="s3://dq-artifacts/agent-reports/report.md",
            occurred_at=occurred_at,
        ),
    ]
    context_client = FakeClickHouseClient()

    for event in context_events:
        persist_run_context_event(context_client, event)

    context_rows = [
        dict(zip(RUN_CONTEXT_COLUMNS, insert["data"][0], strict=True))
        for insert in context_client.inserts
    ]
    phases = verify_run_context_evidence(
        rows=context_rows,
        external_run_id=external_run,
        parent_run_id=str(parent_run_id),
        expected_specialist="incident_triage_agent",
        expected_task_type="triage_alert",
        expected_terminal_status="success",
    )
    memory = build_incident_memory_record(
        parent_run_id=parent_run_id,
        outcome_status=AgentTaskStatus.SUCCESS,
        specialist_name="incident_triage_agent",
        task_type="triage_alert",
        summary="Verifier retained the evidence-driven outcome.",
        alert_display_id="DQ-20260820-VERIFY",
        evidence_references=[evidence],
        decision_facts={"confidence": 0.91},
        report_s3_uri="s3://dq-artifacts/agent-reports/report.md",
    )
    memory_client = FakeClickHouseClient()

    persist_incident_memory(memory_client, memory)
    memory_row = dict(
        zip(
            INCIDENT_MEMORY_COLUMNS,
            memory_client.inserts[0]["data"][0],
            strict=True,
        )
    )
    memory_id = verify_incident_memory_evidence(
        rows=[memory_row],
        parent_run_id=str(parent_run_id),
        expected_specialist="incident_triage_agent",
        expected_task_type="triage_alert",
        report_s3_uri="s3://dq-artifacts/agent-reports/report.md",
        expected_terminal_status="success",
        required=True,
    )

    assert tuple(phases) == ("started", "routed", "completed")
    assert memory_id == str(memory.memory_id)


def test_supervisor_verifier_rejects_missing_lifecycle_phase() -> None:
    """Incomplete persisted lifecycle evidence must fail operational acceptance."""
    parent_run_id = uuid4()
    event         = build_run_context_event(
        parent_run_id=parent_run_id,
        external_run_id="manual__incomplete_context",
        phase=RunContextPhase.STARTED,
        requester="airflow",
        status=AgentTaskStatus.RUNNING,
    )
    client = FakeClickHouseClient()

    persist_run_context_event(client, event)
    row = dict(zip(RUN_CONTEXT_COLUMNS, client.inserts[0]["data"][0], strict=True))

    with pytest.raises(RuntimeError, match="exactly started, routed, and completed"):
        verify_run_context_evidence(
            rows=[row],
            external_run_id="manual__incomplete_context",
            parent_run_id=str(parent_run_id),
            expected_specialist="metadata_lineage_agent",
            expected_task_type="asset_context",
            expected_terminal_status="success",
        )
