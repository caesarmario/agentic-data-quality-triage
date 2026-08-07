####
## Human Approval Queue Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from agent.tools import approval_queue
from agent.tools.approval_queue import (
    ApprovalRequest,
    ApprovalRequestCreate,
)


# --- Defining Test Helpers
def build_create_request(**overrides) -> ApprovalRequestCreate:
    """
    Build a valid bounded backfill approval proposal for tests.

    Args:
        **overrides: Field values replacing the deterministic defaults.

    Returns:
        Validated ApprovalRequestCreate model.
    """
    payload = {
        "alert_key": "orders|dq_failure|2026-06-10",
        "requested_by": "mario",
        "reason": "Backfill the missing orders partition after triage review.",
        "target_dag_id": "00_dag_dq_platform_daily_orchestrator",
        "start_date": date(2026, 6, 10),
        "end_date": date(2026, 6, 10),
        "parameters": {},
    }
    payload.update(overrides)

    return ApprovalRequestCreate.model_validate(payload)


def build_approval(status: str = "pending", **overrides) -> ApprovalRequest:
    """
    Build one latest-state approval model for lifecycle tests.

    Args:
        status: Approval lifecycle status.
        **overrides: Field values replacing the deterministic defaults.

    Returns:
        ApprovalRequest model.
    """
    now     = datetime(2026, 6, 23, 10, 0, tzinfo=timezone.utc)
    request = build_create_request()
    key     = approval_queue.build_idempotency_key(request)
    payload = {
        "request_id": approval_queue.build_request_id(request, key),
        "created_at": now,
        "updated_at": now,
        "alert_key": request.alert_key,
        "action_type": "backfill",
        "risk_level": "high",
        "status": status,
        "requested_by": request.requested_by,
        "reason": request.reason,
        "dispatcher_dag_id": approval_queue.BACKFILL_DISPATCHER_DAG_ID,
        "target_dag_id": request.target_dag_id,
        "start_date": request.start_date,
        "end_date": request.end_date,
        "parameters": request.parameters,
        "dry_run": False,
        "idempotency_key": key,
    }
    payload.update(overrides)

    return ApprovalRequest.model_validate(payload)


# --- Defining Tests
def test_create_request_normalizes_complete_execution_parameters() -> None:
    """
    Ensure omitted execution flags receive deterministic approval-bound defaults.

    Returns:
        None.
    """
    request = build_create_request(parameters={"run_triage": True, "max_alerts": 2})

    assert request.parameters["run_triage"] is True
    assert request.parameters["max_alerts"] == 2
    assert request.parameters["run_seed"] is True
    assert request.parameters["run_load"] is True
    assert request.parameters["run_dbt"] is True
    assert request.parameters["run_dq"] is True
    assert request.parameters["run_mode"] == "backfill"
    assert len(request.parameters) == 14


def test_idempotency_key_is_stable_and_changes_with_execution_scope() -> None:
    """
    Ensure exact duplicate proposals collapse while changed execution flags do not.

    Returns:
        None.
    """
    first     = build_create_request()
    duplicate = build_create_request(reason="A differently worded human explanation.")
    changed   = build_create_request(parameters={"run_triage": True})

    first_key = approval_queue.build_idempotency_key(first)

    assert approval_queue.build_idempotency_key(duplicate) == first_key
    assert approval_queue.build_idempotency_key(changed) != first_key
    assert approval_queue.build_request_id(first, first_key).startswith("APR-20260610-")


def test_create_request_rejects_sensitive_or_unknown_parameters() -> None:
    """
    Ensure credentials and unrecognized execution flags never enter the approval table.

    Returns:
        None.
    """
    with pytest.raises(ValidationError, match="Sensitive approval parameters"):
        build_create_request(parameters={"api_token": "not-a-real-token"})

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        build_create_request(parameters={"unsafe_override": True})


def test_create_approval_request_inserts_and_audits_once(monkeypatch) -> None:
    """
    Ensure first creation persists one pending state and one audit event.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    inserted: list[ApprovalRequest] = []
    audited: list[dict]             = []

    monkeypatch.setattr(approval_queue, "get_approval_request_by_idempotency_key", lambda client, key: None)
    monkeypatch.setattr(approval_queue, "insert_approval_request", lambda client, request: inserted.append(request))
    monkeypatch.setattr(approval_queue, "write_agent_audit_event", lambda **kwargs: audited.append(kwargs))

    approval, created_new = approval_queue.create_approval_request(build_create_request(), client=object())

    assert created_new is True
    assert approval.status == "pending"
    assert approval.risk_level == "high"
    assert inserted == [approval]
    assert audited[0]["action"] == "approval_requested"
    assert audited[0]["input_payload"]["request_id"] == approval.request_id


def test_create_approval_request_reuses_existing_idempotent_request(monkeypatch) -> None:
    """
    Ensure a repeated proposal returns existing state without another insert or audit event.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    existing = build_approval()
    inserted: list[ApprovalRequest] = []

    monkeypatch.setattr(
        approval_queue,
        "get_approval_request_by_idempotency_key",
        lambda client, key: existing,
    )
    monkeypatch.setattr(approval_queue, "insert_approval_request", lambda client, request: inserted.append(request))

    approval, created_new = approval_queue.create_approval_request(build_create_request(), client=object())

    assert created_new is False
    assert approval == existing
    assert inserted == []


def test_decision_is_terminal_and_idempotent(monkeypatch) -> None:
    """
    Ensure same decisions are repeatable while opposite terminal decisions are blocked.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    pending  = build_approval(status="pending")
    inserted: list[ApprovalRequest] = []

    monkeypatch.setattr(approval_queue, "get_approval_request", lambda client, request_id: pending)
    monkeypatch.setattr(approval_queue, "insert_approval_request", lambda client, request: inserted.append(request))
    monkeypatch.setattr(approval_queue, "write_agent_audit_event", lambda **kwargs: None)

    approved, changed = approval_queue.decide_approval_request(
        request_id=pending.request_id,
        decision="approve",
        decided_by="mario",
        comment="Evidence and date scope reviewed.",
        client=object(),
    )

    assert changed is True
    assert approved.status == "approved"
    assert approved.decided_by == "mario"
    assert inserted == [approved]

    monkeypatch.setattr(approval_queue, "get_approval_request", lambda client, request_id: approved)

    repeated, changed_again = approval_queue.decide_approval_request(
        request_id=approved.request_id,
        decision="approve",
        decided_by="mario",
        client=object(),
    )

    assert repeated == approved
    assert changed_again is False

    with pytest.raises(ValueError, match="already approved"):
        approval_queue.decide_approval_request(
            request_id=approved.request_id,
            decision="reject",
            decided_by="reviewer",
            client=object(),
        )


def test_approved_request_must_exactly_match_execution_parameters(monkeypatch) -> None:
    """
    Ensure post-approval execution flag changes are rejected by the gate.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    approved = build_approval(status="approved", decided_by="mario")

    monkeypatch.setattr(approval_queue, "get_approval_request", lambda client, request_id: approved)

    matched = approval_queue.require_approved_backfill_request(
        request_id=approved.request_id,
        target_dag_id=approved.target_dag_id,
        start_date=approved.start_date,
        end_date=approved.end_date,
        parameters=approved.parameters,
        client=object(),
    )

    assert matched == approved

    changed_parameters = dict(approved.parameters)
    changed_parameters["run_triage"] = True

    with pytest.raises(ValueError, match="parameters"):
        approval_queue.require_approved_backfill_request(
            request_id=approved.request_id,
            target_dag_id=approved.target_dag_id,
            start_date=approved.start_date,
            end_date=approved.end_date,
            parameters=changed_parameters,
            client=object(),
        )

def test_execution_transition_is_single_use_and_idempotent(monkeypatch) -> None:
    """
    Ensure one approved request is owned by one DagRun and follows allowed transitions.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    current  = build_approval(status="approved", decided_by="mario")
    inserted: list[ApprovalRequest] = []
    audited: list[dict]             = []

    def fake_get(client, request_id: str) -> ApprovalRequest:
        """
        Return the latest in-memory approval state.

        Args:
            client: Ignored fake ClickHouse client.
            request_id: Approval request identifier.

        Returns:
            Current approval state.
        """
        return current

    def fake_insert(client, request: ApprovalRequest) -> None:
        """
        Capture and promote one append-versioned state.

        Args:
            client: Ignored fake ClickHouse client.
            request: New approval state.

        Returns:
            None.
        """
        nonlocal current
        current = request
        inserted.append(request)

    monkeypatch.setattr(approval_queue, "get_approval_request", fake_get)
    monkeypatch.setattr(approval_queue, "insert_approval_request", fake_insert)
    monkeypatch.setattr(approval_queue, "write_agent_audit_event", lambda **kwargs: audited.append(kwargs))

    dispatching, changed = approval_queue.transition_approval_execution(
        request_id=current.request_id,
        execution_status="dispatching",
        execution_dag_run_id="manual__dispatcher_01",
        client=object(),
    )

    assert changed is True
    assert dispatching.execution_status == "dispatching"
    assert dispatching.execution_dag_run_id == "manual__dispatcher_01"

    dispatched, changed = approval_queue.transition_approval_execution(
        request_id=current.request_id,
        execution_status="dispatched",
        execution_dag_run_id="manual__dispatcher_01",
        client=object(),
    )

    assert changed is True
    assert dispatched.execution_status == "dispatched"
    assert len(inserted) == 2
    assert len(audited) == 2

    repeated, changed = approval_queue.transition_approval_execution(
        request_id=current.request_id,
        execution_status="dispatched",
        execution_dag_run_id="manual__dispatcher_01",
        client=object(),
    )

    assert repeated == dispatched
    assert changed is False

    succeeded, changed = approval_queue.transition_approval_execution(
        request_id=current.request_id,
        execution_status="succeeded",
        execution_dag_run_id="manual__dispatcher_01",
        client=object(),
    )

    assert succeeded.execution_status == "succeeded"
    assert changed is True

    with pytest.raises(ValueError, match="already claimed"):
        approval_queue.transition_approval_execution(
            request_id=current.request_id,
            execution_status="failed",
            execution_dag_run_id="manual__dispatcher_02",
            client=object(),
        )

    with pytest.raises(ValueError, match="Invalid approval execution transition"):
        approval_queue.transition_approval_execution(
            request_id=current.request_id,
            execution_status="failed",
            execution_dag_run_id="manual__dispatcher_01",
            client=object(),
        )


def test_execution_transition_requires_approved_request(monkeypatch) -> None:
    """
    Ensure pending or rejected requests cannot be claimed by a dispatcher.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    pending = build_approval(status="pending")

    monkeypatch.setattr(approval_queue, "get_approval_request", lambda client, request_id: pending)

    with pytest.raises(ValueError, match="execution requires approved status"):
        approval_queue.transition_approval_execution(
            request_id=pending.request_id,
            execution_status="dispatching",
            execution_dag_run_id="manual__dispatcher_01",
            client=object(),
        )


def test_approved_request_gate_rejects_replayed_execution(monkeypatch) -> None:
    """
    Ensure exact-scope validation rejects requests already claimed or dispatched.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    replayed = build_approval(
        status="approved",
        decided_by="mario",
        execution_status="dispatched",
        execution_dag_run_id="manual__dispatcher_01",
    )

    monkeypatch.setattr(approval_queue, "get_approval_request", lambda client, request_id: replayed)

    with pytest.raises(ValueError, match="execution_status"):
        approval_queue.require_approved_backfill_request(
            request_id=replayed.request_id,
            target_dag_id=replayed.target_dag_id,
            start_date=replayed.start_date,
            end_date=replayed.end_date,
            parameters=replayed.parameters,
            client=object(),
        )
