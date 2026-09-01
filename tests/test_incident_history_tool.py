####
## Incident History Tool Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Validate bounded, sanitized, and audited prior-investigation evidence."""

# --- Importing Libraries
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any
from uuid import uuid4

import pytest

from agent.context.models import build_incident_memory_record
from agent.specialists.contracts import (
    AgentApprovalState,
    AgentTaskStatus,
    EvidenceReference,
)
from agent.state import Alert, EvidenceType
from agent.tools import incident_history
from scripts.verify_control_plane_supervisor import verify_incident_history_read


# --- Defining Test Fixtures
def build_memory_record(
    category: str = "missing_partition",
    confidence: float = 0.88,
    summary: str = "The previous investigation found a missing raw partition.",
) -> Any:
    """
    Build one strict durable-memory record for tool tests.

    Args:
        category: Prior policy-owned likely-cause category.
        confidence: Prior deterministic confidence score.
        summary: Operator-facing prior investigation summary.

    Returns:
        Validated IncidentMemoryRecord.
    """
    return build_incident_memory_record(
        parent_run_id=uuid4(),
        outcome_status=AgentTaskStatus.SUCCESS,
        specialist_name="incident_triage_agent",
        task_type="triage_alert",
        summary=summary,
        alert_key="orders|dq_failure|2026-05-13|dq.raw_orders|row_count|table",
        alert_display_id="DQ-20260513-764959",
        evidence_references=[
            EvidenceReference(
                evidence_type="dq_history",
                source_tool="dq_history",
                reference="audit:prior-dq-history",
                summary="Prior deterministic DQ history evidence.",
            ),
            EvidenceReference(
                evidence_type="report_artifact",
                source_tool="s3_artifacts",
                reference="s3://dq-artifacts/agent-reports/prior/report.json",
                summary="Prior structured triage report.",
            ),
        ],
        decision_facts={
            "confidence": confidence,
            "top_hypothesis_category": category,
            "report_id": "RPT-PRIOR01",
        },
        report_s3_uri="s3://dq-artifacts/agent-reports/prior/report.md",
        approval_state=AgentApprovalState.REQUIRED,
        recorded_at=datetime(2026, 8, 19, 10, 0, tzinfo=timezone.utc),
    )


def build_alert() -> Alert:
    """
    Build one current alert sharing the prior memory's canonical identity.

    Returns:
        Validated Alert used by the collector boundary test.
    """
    return Alert(
        alert_key="orders|dq_failure|2026-05-13|dq.raw_orders|row_count|table",
        alert_display_id="DQ-20260513-764959",
        alert_type="dq_failure",
        severity="critical",
        table_name="dq.raw_orders",
        metric="row_count",
        dt=date(2026, 5, 13),
    )


# --- Testing Sanitized Evidence Rows
def test_incident_memory_row_exposes_only_bounded_operator_safe_fields() -> None:
    """Raw decision payloads, keys, hashes, and evidence references must not leak."""
    record = build_memory_record(summary="Prior finding " + ("x" * 900))
    row    = incident_history.incident_memory_to_evidence_row(record)

    assert set(row) == {
        "memory_id",
        "parent_run_id",
        "recorded_at",
        "memory_type",
        "alert_display_id",
        "outcome_status",
        "specialist_name",
        "task_type",
        "summary",
        "confidence",
        "top_hypothesis_category",
        "report_id",
        "evidence_reference_count",
        "evidence_types",
        "report_s3_uri",
        "approval_state",
        "resolution_reference",
    }
    assert len(row["summary"]) == incident_history.MAX_EVIDENCE_SUMMARY_CHARS
    assert row["evidence_reference_count"] == 2
    assert row["evidence_types"] == ["dq_history", "report_artifact"]
    assert "decision_facts" not in row
    assert "memory_key" not in row
    assert "content_sha256" not in row
    assert "alert_key" not in row


# --- Testing Audited Read Behavior
def test_fetch_incident_history_audits_only_summary_metadata(monkeypatch) -> None:
    """One exact bounded read must create one safe successful audit event."""
    client       = object()
    record       = build_memory_record()
    audit_events: list[dict[str, Any]] = []

    monkeypatch.setattr(
        incident_history,
        "build_clickhouse_client",
        lambda **_: client,
    )
    monkeypatch.setattr(
        incident_history,
        "fetch_incident_memory",
        lambda **_: [record],
    )
    monkeypatch.setattr(
        incident_history,
        "write_agent_audit_event",
        lambda **kwargs: audit_events.append(kwargs),
    )

    result = incident_history.fetch_incident_history(
        alert_reference=record.alert_key,
        lookback_days=30,
        limit=5,
        agent_run_id=uuid4(),
        alert_key=record.alert_key,
    )

    assert result["row_count"] == 1
    assert result["recurrence_counts"] == {"missing_partition": 1}
    assert len(audit_events) == 1
    assert audit_events[0]["action"] == "fetch_incident_history"
    assert audit_events[0]["status"] == "success"
    assert audit_events[0]["tool_name"] == "incident_history"
    assert audit_events[0]["input_payload"] == {
        "identity_type": "exact_alert_reference",
        "lookback_days": 30,
        "limit": 5,
    }
    assert audit_events[0]["output_payload"]["row_count"] == 1
    assert "rows" not in audit_events[0]["output_payload"]
    assert "decision_facts" not in json.dumps(audit_events[0]["output_payload"])
    assert "SELECT" in audit_events[0]["sql"]


def test_fetch_incident_history_audits_failed_reads(monkeypatch) -> None:
    """A failed memory read must remain visible without persisting raw errors as output."""
    client       = object()
    audit_events: list[dict[str, Any]] = []

    monkeypatch.setattr(
        incident_history,
        "build_clickhouse_client",
        lambda **_: client,
    )

    def fail_fetch(**_: Any) -> list[Any]:
        """Raise one deterministic storage error for failure-path coverage."""
        raise ValueError("Malformed persisted incident memory")

    monkeypatch.setattr(incident_history, "fetch_incident_memory", fail_fetch)
    monkeypatch.setattr(
        incident_history,
        "write_agent_audit_event",
        lambda **kwargs: audit_events.append(kwargs),
    )

    with pytest.raises(ValueError, match="Malformed persisted incident memory"):
        incident_history.fetch_incident_history(
            alert_reference="DQ-20260513-764959",
            lookback_days=30,
            limit=5,
            agent_run_id=uuid4(),
        )

    assert len(audit_events) == 1
    assert audit_events[0]["status"] == "failed"
    assert audit_events[0]["output_payload"] == {"error_type": "ValueError"}


# --- Testing Triage Evidence Conversion
def test_collect_incident_history_evidence_marks_prior_outcomes_as_context(monkeypatch) -> None:
    """Prior outcomes must be visible but explicitly non-authoritative for current RCA."""
    record = build_memory_record()
    row    = incident_history.incident_memory_to_evidence_row(record)

    monkeypatch.setattr(
        incident_history,
        "fetch_incident_history",
        lambda **_: {
            "status": "success",
            "rows": [row, dict(row)],
            "row_count": 2,
            "recurrence_counts": {"missing_partition": 2},
            "sql": "SELECT bounded incident memory LIMIT 10",
        },
    )

    evidence = incident_history.collect_incident_history_evidence(
        alert=build_alert(),
        agent_run_id=uuid4(),
    )

    assert evidence.evidence_type == EvidenceType.INCIDENT_HISTORY
    assert evidence.tool_name == "incident_history"
    assert evidence.row_count == 2
    assert "comparison context" in evidence.summary
    assert "not proof of the current root cause" in evidence.summary
    assert evidence.s3_uri == record.report_s3_uri


# --- Testing Operational Audit Verification
def test_supervisor_verifier_accepts_one_bounded_incident_history_read() -> None:
    """DAG 98 verification must enforce the exact-match, limit, and safe audit contract."""
    row_count = verify_incident_history_read(
        [
            {
                "action": "fetch_incident_history",
                "tool_name": "incident_history",
                "status": "success",
                "input_json": json.dumps(
                    {
                        "identity_type": "exact_alert_reference",
                        "lookback_days": 90,
                        "limit": 10,
                    }
                ),
                "output_json": json.dumps(
                    {
                        "row_count": 2,
                        "recurrence_counts": {"missing_partition": 2},
                        "report_count": 2,
                        "latest_recorded_at": "2026-08-19T10:00:00+00:00",
                    }
                ),
                "sql_hash": "a" * 64,
                "row_count": 2,
            }
        ]
    )

    assert row_count == 2


def test_supervisor_verifier_rejects_raw_incident_history_rows() -> None:
    """Operational verification must fail if audit output stores raw memory rows."""
    with pytest.raises(RuntimeError, match="must not persist raw memory rows"):
        verify_incident_history_read(
            [
                {
                    "action": "fetch_incident_history",
                    "tool_name": "incident_history",
                    "status": "success",
                    "input_json": json.dumps(
                        {
                            "identity_type": "exact_alert_reference",
                            "lookback_days": 90,
                            "limit": 10,
                        }
                    ),
                    "output_json": json.dumps({"row_count": 1, "rows": [{"raw": True}]}),
                    "sql_hash": "b" * 64,
                    "row_count": 1,
                }
            ]
        )
