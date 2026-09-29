####
## Airflow Approved Action Dispatcher Runtime Tests
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agent.tools.approval_control_store import ApprovalControlRecord
from agent.tools.approved_actions import (
    ActionExecutionResult,
    ApprovedActionDeliveryUnknown,
)
from dags.dq_platform import approved_actions


# --- Defining Test Doubles
class StubStore:
    def __init__(self, record: ApprovalControlRecord | None) -> None:
        self.record = record

    def read_latest(self, request_id: str):
        if self.record is not None and request_id == self.record.request_id:
            return self.record
        return None


def _record() -> ApprovalControlRecord:
    now = datetime.now(timezone.utc)
    return ApprovalControlRecord(
        request_id="APR-ABCDEF0123456789ABCD",
        idempotency_hash="a" * 64,
        request_generation=1,
        action_type="rerun_dbt",
        scope={
            "target_dag_id": "20_dag_dq_orders_dbt_transform",
            "dt": "2026-09-28",
            "run_tests": True,
            "full_refresh": False,
        },
        requested_by="operator",
        reason="Rerun the approved partition transform",
        status="approved",
        version=2,
        created_at=now,
        updated_at=now,
        decided_by="reviewer",
        decided_at=now,
        decision_comment="approved",
        cancelled_by=None,
        cancelled_at=None,
        execution_run_id=None,
        execution_status="not_started",
        execution_error="",
    )


def _context(
    *,
    dry_run=True,
    request_id="APR-ABCDEF0123456789ABCD",
    triggering_user_name=None,
):
    return {
        "dag_run": SimpleNamespace(
            run_id="manual__approved_action_test",
            conf={"approval_request_id": request_id, "dry_run": dry_run},
            triggering_user_name=triggering_user_name,
        )
    }


# --- Testing Preview And Execution Boundaries
def test_preview_reads_state_without_executing(monkeypatch) -> None:
    record = _record()
    audits: list[dict] = []
    monkeypatch.setattr(approved_actions, "build_approval_control_store", lambda: StubStore(record))
    monkeypatch.setattr(approved_actions, "build_clickhouse_client", lambda: object())
    monkeypatch.setattr(approved_actions, "write_agent_audit_event", lambda **kwargs: audits.append(kwargs))
    monkeypatch.setattr(
        approved_actions,
        "action_readiness",
        lambda value: {
            "request_id": value.request_id,
            "action_type": value.action_type,
            "provider_ready": True,
            "side_effects_executed": False,
        },
    )
    monkeypatch.setattr(
        approved_actions,
        "execute_approved_action",
        lambda **kwargs: pytest.fail("preview must not execute the action"),
    )

    result = approved_actions.run_approved_action_dispatcher(**_context())

    assert result["side_effects_executed"] is False
    assert audits[0]["action"] == "approved_action_previewed"


def test_execute_uses_only_durable_request_id_and_airflow_run_id(monkeypatch) -> None:
    record = _record()
    captured: dict = {}
    audits: list[dict] = []
    store = StubStore(record)
    monkeypatch.setattr(approved_actions, "build_approval_control_store", lambda: store)
    monkeypatch.setattr(approved_actions, "build_clickhouse_client", lambda: object())
    monkeypatch.setattr(approved_actions, "write_agent_audit_event", lambda **kwargs: audits.append(kwargs))

    def execute(**kwargs):
        captured.update(kwargs)
        return ActionExecutionResult(
            request_id=record.request_id,
            action_type="rerun_dbt",
            status="dispatched",
            execution_run_id="manual__approved_action_test",
            execution_reference="approved_rerun_dbt__test",
            provider="airflow",
            side_effects_executed=True,
        )

    monkeypatch.setattr(approved_actions, "execute_approved_action", execute)

    result = approved_actions.run_approved_action_dispatcher(**_context(dry_run=False))

    assert captured == {
        "request_id": record.request_id,
        "execution_run_id": "manual__approved_action_test",
        "store": store,
    }
    assert result["status"] == "dispatched"
    assert audits[-1]["action"] == "approved_action_executed"


def test_execution_audit_uses_airflow_triggering_principal(monkeypatch) -> None:
    record = _record()
    audits: list[dict] = []
    monkeypatch.setattr(
        approved_actions,
        "build_approval_control_store",
        lambda: StubStore(record),
    )
    monkeypatch.setattr(approved_actions, "build_clickhouse_client", lambda: object())
    monkeypatch.setattr(
        approved_actions,
        "write_agent_audit_event",
        lambda **kwargs: audits.append(kwargs),
    )
    monkeypatch.setattr(
        approved_actions,
        "execute_approved_action",
        lambda **kwargs: ActionExecutionResult(
            request_id=record.request_id,
            action_type=record.action_type,
            status="dispatched",
            execution_run_id="manual__approved_action_test",
            execution_reference="approved_rerun_dbt__test",
            provider="airflow",
            side_effects_executed=True,
        ),
    )

    approved_actions.run_approved_action_dispatcher(
        **_context(dry_run=False, triggering_user_name="airflow-operator")
    )

    assert audits[-1]["actor"] == "airflow:airflow-operator"
    assert audits[-1]["actor"] not in {record.requested_by, record.decided_by}
    assert audits[-1]["input_payload"]["requested_by"] == record.requested_by
    assert audits[-1]["input_payload"]["approved_by"] == record.decided_by


def test_execution_audit_falls_back_when_trigger_identity_is_unavailable(monkeypatch) -> None:
    record = _record()
    audits: list[dict] = []
    monkeypatch.setattr(
        approved_actions,
        "build_approval_control_store",
        lambda: StubStore(record),
    )
    monkeypatch.setattr(approved_actions, "build_clickhouse_client", lambda: object())
    monkeypatch.setattr(
        approved_actions,
        "write_agent_audit_event",
        lambda **kwargs: audits.append(kwargs),
    )
    monkeypatch.setattr(
        approved_actions,
        "execute_approved_action",
        lambda **kwargs: ActionExecutionResult(
            request_id=record.request_id,
            action_type=record.action_type,
            status="dispatched",
            execution_run_id="manual__approved_action_test",
            provider="airflow",
        ),
    )

    approved_actions.run_approved_action_dispatcher(**_context(dry_run=False))

    assert audits[-1]["actor"] == "airflow"


@pytest.mark.parametrize(
    ("context", "error"),
    [
        ({"dag_run": SimpleNamespace(run_id="manual__x", conf={})}, ValueError),
        (_context(request_id="APR-00000000000000000000"), LookupError),
    ],
)
def test_missing_or_unknown_request_fails_closed(monkeypatch, context, error) -> None:
    monkeypatch.setattr(approved_actions, "build_approval_control_store", lambda: StubStore(None))

    with pytest.raises(error):
        approved_actions.run_approved_action_dispatcher(**context)


def test_execution_failure_is_sanitized_into_audit(monkeypatch) -> None:
    record = _record()
    audits: list[dict] = []
    monkeypatch.setattr(approved_actions, "build_approval_control_store", lambda: StubStore(record))
    monkeypatch.setattr(approved_actions, "build_clickhouse_client", lambda: object())
    monkeypatch.setattr(approved_actions, "write_agent_audit_event", lambda **kwargs: audits.append(kwargs))
    monkeypatch.setattr(
        approved_actions,
        "execute_approved_action",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("provider disabled")),
    )

    with pytest.raises(RuntimeError, match="provider disabled"):
        approved_actions.run_approved_action_dispatcher(**_context(dry_run=False))

    assert audits[-1]["action"] == "approved_action_execution_failed"
    assert audits[-1]["output_payload"] == {
        "error_type": "RuntimeError",
        "message": "provider disabled",
    }


def test_unknown_delivery_is_audited_as_unknown_before_rethrow(monkeypatch) -> None:
    record = _record()
    audits: list[dict] = []
    monkeypatch.setattr(
        approved_actions,
        "build_approval_control_store",
        lambda: StubStore(record),
    )
    monkeypatch.setattr(approved_actions, "build_clickhouse_client", lambda: object())
    monkeypatch.setattr(
        approved_actions,
        "write_agent_audit_event",
        lambda **kwargs: audits.append(kwargs),
    )
    monkeypatch.setattr(
        approved_actions,
        "execute_approved_action",
        lambda **kwargs: (_ for _ in ()).throw(
            ApprovedActionDeliveryUnknown("unsafe provider detail")
        ),
    )

    with pytest.raises(ApprovedActionDeliveryUnknown, match="unsafe provider detail"):
        approved_actions.run_approved_action_dispatcher(
            **_context(dry_run=False, triggering_user_name="airflow-operator")
        )

    assert audits[-1]["action"] == "approved_action_execution_unknown"
    assert audits[-1]["status"] == "unknown"
    assert audits[-1]["actor"] == "airflow:airflow-operator"
    assert audits[-1]["output_payload"] == {
        "error_type": "ApprovedActionDeliveryUnknown",
        "message": (
            "Delivery outcome is unknown; inspect the target system before "
            "considering a retry."
        ),
    }
    assert "unsafe provider detail" not in str(audits[-1])
