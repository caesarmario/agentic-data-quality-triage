####
## Transactional Approval Action CLI Tests
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import argparse
from datetime import datetime, timezone

import pytest

from agent.tools.approval_control_store import ApprovalControlRecord
from scripts import manage_approved_action


# --- Defining Test Doubles
def _record() -> ApprovalControlRecord:
    now = datetime.now(timezone.utc)
    return ApprovalControlRecord(
        request_id="APR-ABCDEF0123456789ABCD",
        idempotency_hash="a" * 64,
        request_generation=1,
        action_type="rerun_dbt",
        scope={"target_dag_id": "20_dag_dq_orders_dbt_transform", "dt": "2026-09-28", "run_tests": True, "full_refresh": False},
        requested_by="operator",
        reason="Rerun the approved partition transform",
        status="pending",
        version=1,
        created_at=now,
        updated_at=now,
        decided_by=None,
        decided_at=None,
        decision_comment="",
        cancelled_by=None,
        cancelled_at=None,
        execution_run_id=None,
        execution_status="not_started",
        execution_error="",
    )


class StubStore:
    def __init__(self) -> None:
        self.record = _record()

    def decide(self, request_id, **kwargs):
        self.record = self.record.__class__(**{
            **self.record.__dict__,
            "status": "approved" if kwargs["decision"] == "approve" else "rejected",
            "version": kwargs["expected_version"] + 1,
            "decided_by": kwargs["decided_by"],
            "decided_at": datetime.now(timezone.utc),
        })
        return self.record

    def cancel(self, request_id, **kwargs):
        self.record = self.record.__class__(**{
            **self.record.__dict__,
            "status": "cancelled",
            "version": kwargs["expected_version"] + 1,
            "cancelled_by": kwargs["cancelled_by"],
            "cancelled_at": datetime.now(timezone.utc),
        })
        return self.record

    def read_latest(self, request_id):
        return self.record if request_id == self.record.request_id else None


# --- Testing Parser And Commands
def test_scope_parser_requires_json_object() -> None:
    assert manage_approved_action.parse_scope_json('{"dt":"2026-09-28"}') == {"dt": "2026-09-28"}
    with pytest.raises(argparse.ArgumentTypeError):
        manage_approved_action.parse_scope_json("[]")
    with pytest.raises(argparse.ArgumentTypeError):
        manage_approved_action.parse_scope_json("not-json")


def test_decide_and_get_output_are_sanitized(monkeypatch) -> None:
    store = StubStore()
    monkeypatch.setattr(manage_approved_action, "build_approval_control_store", lambda: store)
    decided = manage_approved_action.run(
        argparse.Namespace(
            command="decide",
            request_id=store.record.request_id,
            decision="approve",
            decided_by="reviewer",
            expected_version=1,
            comment="approved",
        )
    )
    monkeypatch.setattr(
        manage_approved_action,
        "action_readiness",
        lambda record: {"provider_ready": True, "side_effects_executed": False},
    )
    loaded = manage_approved_action.run(
        argparse.Namespace(command="get", request_id=store.record.request_id)
    )

    assert decided["request"]["status"] == "approved"
    assert loaded["readiness"]["side_effects_executed"] is False
    assert "idempotency_hash" not in loaded["request"]
    assert loaded["request"]["request_generation"] == 1


def test_create_parser_defaults_generation_and_accepts_explicit_repeat() -> None:
    parser = manage_approved_action.build_parser()
    default_args = parser.parse_args(
        [
            "create", "--action", "rerun_dbt", "--scope-json", '{"dt":"2026-09-28"}',
            "--requested-by", "operator", "--reason", "Rerun the approved partition",
        ]
    )
    repeat_args = parser.parse_args(
        [
            "create", "--action", "rerun_dbt", "--scope-json", '{"dt":"2026-09-28"}',
            "--requested-by", "operator", "--reason", "Rerun the approved partition",
            "--generation", "2",
        ]
    )

    assert default_args.generation == 1
    assert repeat_args.generation == 2


def test_unknown_request_fails_closed(monkeypatch) -> None:
    store = StubStore()
    monkeypatch.setattr(manage_approved_action, "build_approval_control_store", lambda: store)

    with pytest.raises(LookupError):
        manage_approved_action.run(
            argparse.Namespace(command="get", request_id="APR-00000000000000000000")
        )
