####
## Airflow Backfill Approval Gate Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from dags.dq_platform import backfill_dispatcher
from agent.tools.approval_queue import ApprovalRequest, normalize_backfill_parameters


# --- Defining Constants
DAG_PATH = Path("dags/90_dag_dq_platform_backfill_dispatcher.py")


# --- Defining Test Helpers
def build_approved_request() -> ApprovalRequest:
    """
    Build one exact-scope approved backfill request for dispatcher tests.

    Returns:
        Approved ApprovalRequest model.
    """
    now = datetime(2026, 6, 23, 10, 0, tzinfo=timezone.utc)

    return ApprovalRequest(
        request_id="APR-20260610-A1B2C3D4",
        created_at=now,
        updated_at=now,
        alert_key="orders|dq_failure|2026-06-10",
        action_type="backfill",
        risk_level="high",
        status="approved",
        requested_by="mario",
        reason="Backfill the missing orders partition after triage review.",
        dispatcher_dag_id="90_dag_dq_platform_backfill_dispatcher",
        target_dag_id="00_dag_dq_platform_daily_orchestrator",
        start_date=date(2026, 6, 10),
        end_date=date(2026, 6, 10),
        parameters=normalize_backfill_parameters(),
        dry_run=False,
        idempotency_key="a1b2c3d4",
        decided_by="mario",
        decided_at=now,
    )


# --- Defining Tests
def test_dry_run_bypasses_durable_approval_lookup(monkeypatch) -> None:
    """
    Ensure preview-only dispatcher runs remain available without approval state.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    monkeypatch.setattr(
        backfill_dispatcher,
        "require_approved_backfill_request",
        lambda **kwargs: pytest.fail("Dry-run must not query approval state."),
    )

    approval = backfill_dispatcher.validate_execution_approval(
        parent_conf={},
        dry_run=True,
        target_dag_id=backfill_dispatcher.DEFAULT_TARGET_DAG_ID,
        start_date=date(2026, 6, 10),
        end_date=date(2026, 6, 10),
    )

    assert approval is None


def test_real_dispatch_requires_approval_request_id() -> None:
    """
    Ensure non-dry-run execution cannot proceed without a durable approval reference.

    Returns:
        None.
    """
    with pytest.raises(ValueError, match="approval_request_id is required"):
        backfill_dispatcher.validate_execution_approval(
            parent_conf={},
            dry_run=False,
            target_dag_id=backfill_dispatcher.DEFAULT_TARGET_DAG_ID,
            start_date=date(2026, 6, 10),
            end_date=date(2026, 6, 10),
        )


def test_real_dispatch_passes_exact_scope_to_approval_gate(monkeypatch) -> None:
    """
    Ensure dispatcher sends canonical dates, target, and flags to the durable gate.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    approved = build_approved_request()
    captured: dict[str, object] = {}

    def fake_require(**kwargs):
        """
        Capture exact authorization inputs and return a matching approval.

        Args:
            **kwargs: Approval gate keyword arguments.

        Returns:
            Approved request model.
        """
        captured.update(kwargs)
        return approved

    monkeypatch.setattr(backfill_dispatcher, "require_approved_backfill_request", fake_require)

    result = backfill_dispatcher.validate_execution_approval(
        parent_conf={
            "approval_request_id": approved.request_id,
            "run_triage": False,
            "max_alerts": 5,
        },
        dry_run=False,
        target_dag_id=approved.target_dag_id,
        start_date=approved.start_date,
        end_date=approved.end_date,
    )

    assert result == approved
    assert captured["request_id"] == approved.request_id
    assert captured["target_dag_id"] == approved.target_dag_id
    assert captured["parameters"] == approved.parameters


def test_dag_exposes_approval_request_id_and_exact_scope_guidance() -> None:
    """
    Ensure the manual Airflow form documents the durable execution gate.

    Returns:
        None.
    """
    content = DAG_PATH.read_text(encoding="utf-8")

    assert '"approval_request_id": Param(' in content
    assert "required when dry_run=false" in content
    assert "must match exactly" in content

def test_dispatcher_claims_and_completes_non_waiting_execution(monkeypatch) -> None:
    """
    Ensure a real dispatcher run transitions approved state through dispatching to dispatched.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    approved    = build_approved_request()
    transitions: list[str] = []

    class FakeDagRun:
        """Minimal dispatcher DagRun context."""

        run_id = "manual__dispatcher_01"
        conf   = {
            "start_date": "2026-06-10",
            "end_date": "2026-06-10",
            "target_dag_id": "00_dag_dq_platform_daily_orchestrator",
            "requested_by": "mario",
            "reason": "Approved backfill",
            "approval_request_id": approved.request_id,
            "dry_run": False,
            "wait_for_completion": False,
        }

    def fake_transition(**kwargs):
        """
        Capture execution transitions and return the updated model.

        Args:
            **kwargs: Transition keyword arguments.

        Returns:
            Updated approval model and changed flag.
        """
        target = getattr(kwargs["execution_status"], "value", kwargs["execution_status"])
        transitions.append(str(target))

        return (
            approved.model_copy(
                update={
                    "execution_status": str(target),
                    "execution_dag_run_id": kwargs["execution_dag_run_id"],
                }
            ),
            True,
        )

    monkeypatch.setattr(backfill_dispatcher, "validate_execution_approval", lambda **kwargs: approved)
    monkeypatch.setattr(backfill_dispatcher, "transition_approval_execution", fake_transition)
    monkeypatch.setattr(
        backfill_dispatcher,
        "trigger_child_dag",
        lambda **kwargs: {
            "status": "triggered",
            "target_dag_id": kwargs["target_dag_id"],
            "run_id": kwargs["run_id"],
            "dt": kwargs["run_dt"].isoformat(),
        },
    )

    summary = backfill_dispatcher.run_backfill_dispatcher(dag_run=FakeDagRun())

    assert transitions == ["dispatching", "dispatched"]
    assert summary["execution_status"] == "dispatched"
    assert summary["approval_request_id"] == approved.request_id
    assert summary["results"][0]["status"] == "triggered"


def test_dispatcher_marks_claimed_approval_failed_when_trigger_errors(monkeypatch) -> None:
    """
    Ensure a child trigger failure becomes a durable failed execution state.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    approved    = build_approved_request()
    transitions: list[str] = []

    class FakeDagRun:
        """Minimal failing dispatcher DagRun context."""

        run_id = "manual__dispatcher_failure"
        conf   = {
            "start_date": "2026-06-10",
            "end_date": "2026-06-10",
            "target_dag_id": "00_dag_dq_platform_daily_orchestrator",
            "requested_by": "mario",
            "reason": "Approved backfill",
            "approval_request_id": approved.request_id,
            "dry_run": False,
        }

    def fake_transition(**kwargs):
        """Capture dispatching and failed transitions."""
        target = getattr(kwargs["execution_status"], "value", kwargs["execution_status"])
        transitions.append(str(target))
        return approved.model_copy(update={"execution_status": str(target)}), True

    def raise_trigger_error(**kwargs):
        """
        Simulate an Airflow child trigger failure.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError("child trigger unavailable")

    monkeypatch.setattr(backfill_dispatcher, "validate_execution_approval", lambda **kwargs: approved)
    monkeypatch.setattr(backfill_dispatcher, "transition_approval_execution", fake_transition)
    monkeypatch.setattr(backfill_dispatcher, "trigger_child_dag", raise_trigger_error)

    with pytest.raises(RuntimeError, match="child trigger unavailable"):
        backfill_dispatcher.run_backfill_dispatcher(dag_run=FakeDagRun())

    assert transitions == ["dispatching", "failed"]
