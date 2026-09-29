####
## Transactional Approval Control Store Tests
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, Thread

import pytest

from agent.tools.approval_control_store import (
    COLUMNS,
    ApprovalControlConflict,
    ApprovalControlStore,
)


# --- Defining Fake Transaction Adapter
class FakeDatabase:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.lock = Lock()
        self.fail_commit = False
        self.connections = 0

    def connect(self) -> "FakeConnection":
        self.lock.acquire()
        self.connections += 1
        return FakeConnection(self)


class FakeConnection:
    def __init__(self, database: FakeDatabase) -> None:
        self.database = database
        self.snapshot = deepcopy(database.rows)
        self.closed = False

    def cursor(self) -> "FakeCursor":
        return FakeCursor(self.database)

    def commit(self) -> None:
        if self.database.fail_commit:
            raise RuntimeError("commit failed")

    def rollback(self) -> None:
        self.database.rows = self.snapshot

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.database.lock.release()


class FakeCursor:
    def __init__(self, database: FakeDatabase) -> None:
        self.database = database
        self.result: tuple | None = None

    def execute(self, statement: str, parameters: tuple) -> None:
        self.result = None
        sql = " ".join(statement.split())
        assert "%s" in sql

        if sql.startswith("INSERT"):
            assert "ON CONFLICT (idempotency_hash) DO NOTHING" in sql
            request_id, full_hash, generation, action, scope_json, actor, reason = parameters
            if not any(row["idempotency_hash"] == full_hash for row in self.database.rows.values()):
                assert request_id not in self.database.rows
                now = datetime.now(timezone.utc)
                self.database.rows[request_id] = {
                    "request_id": request_id,
                    "idempotency_hash": full_hash,
                    "request_generation": generation,
                    "action_type": action,
                    "scope_json": scope_json,
                    "requested_by": actor,
                    "reason": reason,
                    "status": "pending",
                    "version": 1,
                    "created_at": now,
                    "updated_at": now,
                    "decided_by": None,
                    "decided_at": None,
                    "decision_comment": "",
                    "cancelled_by": None,
                    "cancelled_at": None,
                    "execution_run_id": None,
                    "execution_status": "not_started",
                    "execution_error": "",
                }
                self.result = self._row(self.database.rows[request_id])
            return

        if sql.startswith("SELECT"):
            key = parameters[0]
            row = next(
                (item for item in self.database.rows.values() if item["idempotency_hash"] == key),
                None,
            ) if "WHERE idempotency_hash" in sql else self.database.rows.get(key)
            self.result = self._row(row) if row else None
            return

        assert sql.startswith("UPDATE") and "AND version = %s" in sql
        changed = False
        if "SET status = %s" in sql:
            status, actor, comment, request_id, version = parameters
            row = self.database.rows.get(request_id)
            if row and row["status"] == "pending" and row["version"] == version:
                row.update(status=status, decided_by=actor, decided_at=datetime.now(timezone.utc), decision_comment=comment)
                changed = True
        elif "SET status = 'cancelled'" in sql:
            actor, request_id, version = parameters
            row = self.database.rows.get(request_id)
            if row and row["status"] in ("pending", "approved") and row["execution_status"] == "not_started" and row["version"] == version:
                row.update(status="cancelled", cancelled_by=actor, cancelled_at=datetime.now(timezone.utc))
                changed = True
        elif "SET execution_run_id = %s" in sql:
            run_id, request_id, version = parameters
            row = self.database.rows.get(request_id)
            if row and row["status"] == "approved" and row["execution_status"] == "not_started" and row["version"] == version:
                row.update(execution_run_id=run_id, execution_status="dispatching")
                changed = True
        else:
            assert "execution_status = ANY(%s)" in sql
            outcome, error, request_id, run_id, previous, version = parameters
            row = self.database.rows.get(request_id)
            if row and row["status"] == "approved" and row["execution_run_id"] == run_id and row["execution_status"] in previous and row["version"] == version:
                row.update(execution_status=outcome, execution_error=error)
                changed = True

        if changed:
            row["version"] += 1
            row["updated_at"] = datetime.now(timezone.utc)
            self.result = self._row(row)

    @staticmethod
    def _row(row: dict) -> tuple:
        return tuple(row[column] for column in COLUMNS)

    def fetchone(self) -> tuple | None:
        return self.result

    def close(self) -> None:
        pass


# --- Defining Test Data
def create(store: ApprovalControlStore, **overrides):
    payload = {
        "action_type": "backfill",
        "scope": {"target_dag_id": "20_dag_dq_orders_dbt_transform", "date": "2026-09-28"},
        "requested_by": "operator",
        "reason": "Restore the approved missing partition",
    }
    payload.update(overrides)
    return store.create_request(**payload)


# --- Testing Idempotency And Fail-Closed Input
def test_create_uses_full_unique_hash_and_reuses_exact_scope() -> None:
    database = FakeDatabase()
    store = ApprovalControlStore(database.connect)
    original, created = create(store)
    repeated, reused = create(store, requested_by="another-operator", reason="A different explanation")
    different, added = create(store, scope={"target_dag_id": "20_dag_dq_orders_dbt_transform", "date": "2026-09-29"})

    assert created and added and not reused
    assert repeated == original
    assert len(original.idempotency_hash) == 64
    assert different.idempotency_hash != original.idempotency_hash
    assert len(database.rows) == 2
    assert store.read_latest(original.request_id) == original


def test_explicit_generation_allows_an_intentional_repeat_without_breaking_reuse() -> None:
    database = FakeDatabase()
    store = ApprovalControlStore(database.connect)
    original, original_created = create(store)
    repeated, repeated_created = create(store, request_generation=2)
    reused, reused_created = create(store, request_generation=2, requested_by="another-operator")

    assert original_created and repeated_created and not reused_created
    assert original.request_generation == 1
    assert repeated.request_generation == 2
    assert repeated.request_id != original.request_id
    assert repeated.idempotency_hash != original.idempotency_hash
    assert reused == repeated
    assert len(database.rows) == 2


def test_invalid_scope_or_identity_never_opens_a_connection() -> None:
    database = FakeDatabase()
    store = ApprovalControlStore(database.connect)
    invalid = (
        {"action_type": "delete", "scope": {"date": "2026-09-28"}},
        {"scope": {"api_token": "synthetic"}},
        {"scope": {"nested": [{"password": "synthetic"}]}},
        {"scope": {"date": float("nan")}},
        {"scope": {"date": (1, 2)}},
        {"requested_by": " "},
        {"reason": "x"},
        {"request_generation": 0},
        {"request_generation": True},
    )
    for override in invalid:
        with pytest.raises(ValueError):
            create(store, **override)
    assert database.connections == 0


@pytest.mark.parametrize("action_type", ["backfill", "rerun_dbt", "create_ticket", "post_notification"])
def test_supported_action_types_use_the_same_transactional_contract(action_type: str) -> None:
    database = FakeDatabase()
    store = ApprovalControlStore(database.connect)

    created, is_new = create(store, action_type=action_type)

    assert is_new is True
    assert created.action_type == action_type


# --- Testing Compare-And-Set Lifecycle
def test_decision_requires_pending_state_and_exact_version() -> None:
    database = FakeDatabase()
    store = ApprovalControlStore(database.connect)
    pending, _ = create(store)
    with pytest.raises(ApprovalControlConflict):
        store.decide(pending.request_id, decision="approve", decided_by="reviewer", expected_version=2)
    approved = store.decide(pending.request_id, decision="approve", decided_by="reviewer", expected_version=1)
    assert approved.status == "approved" and approved.version == 2
    assert approved.decided_by == "reviewer"
    with pytest.raises(ApprovalControlConflict):
        store.decide(pending.request_id, decision="reject", decided_by="other", expected_version=1)
    assert store.read_latest(pending.request_id) == approved


def test_requester_cannot_approve_their_own_request() -> None:
    database = FakeDatabase()
    store = ApprovalControlStore(database.connect)
    pending, _ = create(store, requested_by="Operator.One")

    with pytest.raises(ApprovalControlConflict, match="requester cannot approve"):
        store.decide(
            pending.request_id,
            decision="approve",
            decided_by="operator.one",
            expected_version=1,
        )

    rejected = store.decide(
        pending.request_id,
        decision="reject",
        decided_by="Operator.One",
        expected_version=1,
    )
    assert rejected.status == "rejected"


def test_rejection_and_cancellation_never_grant_execution() -> None:
    database = FakeDatabase()
    store = ApprovalControlStore(database.connect)
    pending, _ = create(store)
    rejected = store.decide(pending.request_id, decision="reject", decided_by="reviewer", expected_version=1)
    with pytest.raises(ApprovalControlConflict):
        store.claim(rejected.request_id, execution_run_id="manual__rejected", expected_version=2)
    with pytest.raises(ApprovalControlConflict):
        store.cancel(rejected.request_id, cancelled_by="operator", expected_version=2)

    next_request, _ = create(store, scope={"date": "2026-09-29"})
    cancelled = store.cancel(next_request.request_id, cancelled_by="operator", expected_version=1)
    assert cancelled.status == "cancelled" and cancelled.cancelled_by == "operator"
    with pytest.raises(ApprovalControlConflict):
        store.decide(cancelled.request_id, decision="approve", decided_by="reviewer", expected_version=2)


def test_approved_request_can_cancel_only_before_claim() -> None:
    database = FakeDatabase()
    store = ApprovalControlStore(database.connect)
    pending, _ = create(store)
    approved = store.decide(pending.request_id, decision="approve", decided_by="reviewer", expected_version=1)
    cancelled = store.cancel(approved.request_id, cancelled_by="operator", expected_version=2)
    assert cancelled.status == "cancelled" and cancelled.decided_by == "reviewer"
    with pytest.raises(ApprovalControlConflict):
        store.claim(cancelled.request_id, execution_run_id="manual__late", expected_version=3)


def test_claim_is_atomic_under_two_concurrent_callers() -> None:
    database = FakeDatabase()
    store = ApprovalControlStore(database.connect)
    pending, _ = create(store)
    approved = store.decide(pending.request_id, decision="approve", decided_by="reviewer", expected_version=1)
    results: list[str] = []

    def contender(run_id: str) -> None:
        try:
            store.claim(approved.request_id, execution_run_id=run_id, expected_version=2)
            results.append("claimed")
        except ApprovalControlConflict:
            results.append("rejected")

    threads = [Thread(target=contender, args=(f"manual__run_{index}",)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == ["claimed", "rejected"]
    assert store.read_latest(approved.request_id).execution_status == "dispatching"


def test_outcome_requires_claimant_and_cannot_overwrite_terminal_state() -> None:
    database = FakeDatabase()
    store = ApprovalControlStore(database.connect)
    pending, _ = create(store)
    approved = store.decide(pending.request_id, decision="approve", decided_by="reviewer", expected_version=1)
    claimed = store.claim(approved.request_id, execution_run_id="manual__2026-09-28T00:00:00+00:00", expected_version=2)
    with pytest.raises(ApprovalControlConflict):
        store.cancel(claimed.request_id, cancelled_by="operator", expected_version=3)
    with pytest.raises(ApprovalControlConflict):
        store.record_execution_outcome(claimed.request_id, execution_run_id="manual__other", outcome="succeeded", expected_version=3)
    dispatched = store.record_execution_outcome(claimed.request_id, execution_run_id=claimed.execution_run_id, outcome="dispatched", expected_version=3)
    failed = store.record_execution_outcome(dispatched.request_id, execution_run_id=claimed.execution_run_id, outcome="failed", expected_version=4, error="Child run failed")
    assert failed.execution_status == "failed" and failed.execution_error == "Child run failed"
    with pytest.raises(ApprovalControlConflict):
        store.record_execution_outcome(failed.request_id, execution_run_id=claimed.execution_run_id, outcome="succeeded", expected_version=5)


def test_ambiguous_outbound_outcome_is_terminal_and_auditable() -> None:
    database = FakeDatabase()
    store = ApprovalControlStore(database.connect)
    pending, _ = create(store, action_type="post_notification")
    approved = store.decide(pending.request_id, decision="approve", decided_by="reviewer", expected_version=1)
    claimed = store.claim(approved.request_id, execution_run_id="manual__notification", expected_version=2)

    unknown = store.record_execution_outcome(
        claimed.request_id,
        execution_run_id="manual__notification",
        outcome="unknown",
        expected_version=3,
        error="Delivery outcome could not be confirmed",
    )

    assert unknown.execution_status == "unknown"
    with pytest.raises(ApprovalControlConflict):
        store.record_execution_outcome(
            unknown.request_id,
            execution_run_id="manual__notification",
            outcome="succeeded",
            expected_version=4,
        )


def test_commit_failure_rolls_back_and_missing_request_fails_closed() -> None:
    database = FakeDatabase()
    store = ApprovalControlStore(database.connect)
    database.fail_commit = True
    with pytest.raises(RuntimeError, match="commit failed"):
        create(store)
    assert database.rows == {}
    database.fail_commit = False
    assert store.read_latest("APR-MISSING") is None
    with pytest.raises(LookupError):
        store.decide("APR-MISSING", decision="approve", decided_by="reviewer", expected_version=1)


def test_migration_has_unique_full_hash_and_state_constraints() -> None:
    migration = Path(__file__).resolve().parents[1] / "infra/init/postgres/01_approval_control_store.sql"
    sql = migration.read_text(encoding="utf-8")
    assert "idempotency_hash CHAR(64) NOT NULL UNIQUE" in sql
    assert "request_generation INTEGER NOT NULL DEFAULT 1" in sql
    assert "approval_request_generation_positive" in sql
    assert "CREATE SCHEMA IF NOT EXISTS dq_control" in sql
    assert "approval_claim_fields" in sql
    assert "'rerun_dbt', 'create_ticket', 'post_notification'" in sql
    assert "'unknown'" in sql
