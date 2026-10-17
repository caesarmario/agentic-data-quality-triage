"""Preflight and idempotency tests for the legacy approval cutover."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json

import pytest

from agent.tools import approval_queue
from agent.tools.approval_control_store import (
    COLUMNS,
    ApprovalControlConflict,
    ApprovalControlStore,
)
from scripts import migrate_legacy_approvals as migration


NOW = datetime(2026, 10, 15, 8, 0, tzinfo=timezone.utc)


def snapshot(*, request_id="APR-LEGACY-001", idempotency_key=None, **overrides):
    idempotency_key = idempotency_key or "a" * 64
    payload = {
        "request_id": request_id,
        "created_at": NOW - timedelta(hours=1),
        "updated_at": NOW,
        "alert_key": "orders|dq_failure|2026-10-14",
        "action_type": "backfill",
        "risk_level": "high",
        "status": "pending",
        "requested_by": "mario",
        "reason": "Replay the approved missing business date.",
        "dispatcher_dag_id": approval_queue.BACKFILL_DISPATCHER_DAG_ID,
        "target_dag_id": "00_dag_dq_platform_daily_orchestrator",
        "start_date": date(2026, 10, 14),
        "end_date": date(2026, 10, 14),
        "parameters": {},
        "dry_run": False,
        "idempotency_key": idempotency_key,
        "execution_status": "not_started",
    }
    payload.update(overrides)
    return approval_queue.ApprovalRequest.model_validate(payload)


class SourceClient:
    def __init__(self, requests):
        self.rows = [approval_queue.approval_to_row(item) for item in requests]

    def query(self, _sql):
        class Result:
            result_rows = self.rows
        return Result()


class IdempotentStore:
    def __init__(self):
        self.receipts = {}
        self.preflights = []

    def import_legacy_request(self, record, *, history, source_hash, dry_run=True):
        assert record.action_type == "backfill"
        assert record.scope["execution_eligibility"] == "legacy_review_required"
        assert record.scope.get("approved") is not True
        assert record.execution_status == "not_started"
        if dry_run:
            self.preflights.append(record.request_id)
            return True
        prior = self.receipts.get(record.request_id)
        if prior is not None:
            if prior != (source_hash, history):
                raise ApprovalControlConflict("Imported legacy history changed")
            return False
        if any(existing_hash == record.idempotency_hash for existing_hash, _ in self.receipts.values()):
            raise ApprovalControlConflict("idempotency hash collision")
        self.receipts[record.request_id] = (source_hash, history)
        return True


class ImportDatabase:
    """Small DB-API double that exercises the real PostgreSQL import method."""
    def __init__(self):
        self.requests = {}
        self.imports = {}
        self.outbox = {}

    def connect(self):
        return ImportConnection(self)


class ImportConnection:
    def __init__(self, database):
        self.database = database

    def cursor(self):
        return ImportCursor(self.database)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class ImportCursor:
    def __init__(self, database):
        self.database = database
        self.rows = []
        self.row = None

    def execute(self, statement, parameters):
        sql = " ".join(statement.split())
        self.rows, self.row = [], None
        if "pg_advisory_xact_lock" in sql:
            return
        if sql.startswith("SELECT") and "OR idempotency_hash" in sql:
            request_id, idem_hash = parameters
            found = [item for item in self.database.requests.values()
                     if item["request_id"] == request_id or item["idempotency_hash"] == idem_hash]
            self.rows = [tuple(item[column] for column in COLUMNS) for item in found]
        elif sql.startswith("SELECT") and "approval_legacy_imports" in sql:
            self.row = self.database.imports.get(parameters[0])
        elif sql.startswith("INSERT") and "approval_requests" in sql:
            item = dict(zip(COLUMNS, parameters, strict=True))
            self.database.requests[item["request_id"]] = item
        elif sql.startswith("INSERT") and "approval_legacy_imports" in sql:
            self.database.imports[parameters[0]] = (parameters[1], json.loads(parameters[2]))
        elif sql.startswith("INSERT") and "approval_projection_outbox" in sql:
            self.database.outbox[(parameters[0], parameters[1])] = parameters[2]
        else:
            raise AssertionError(f"Unexpected importer SQL: {sql}")

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows

    def close(self):
        pass


def test_legacy_history_import_is_idempotent_for_identical_source():
    item = migration.prepare_legacy_import([snapshot()])
    store = IdempotentStore()

    assert store.import_legacy_request(item.record, history=item.history, source_hash=item.source_hash, dry_run=False)
    assert not store.import_legacy_request(item.record, history=item.history, source_hash=item.source_hash, dry_run=False)
    assert len(store.receipts) == 1


def test_idempotency_hash_collision_between_different_legacy_ids_blocks_entire_apply():
    client = SourceClient([
        snapshot(request_id="APR-LEGACY-001"),
        snapshot(request_id="APR-LEGACY-002"),
    ])
    store = IdempotentStore()

    report = migration.migrate_legacy_approvals(client=client, store=store, apply=True, writers_quiesced=True)

    assert report["imported"] == 0
    assert report["ready"] == 1
    assert report["blockers"]
    assert "idempotency hash" in report["blockers"][0]["reason"]
    assert store.receipts == {}


def test_unknown_execution_state_is_reported_as_blocker_and_never_imported():
    client = SourceClient([snapshot(status="approved", decided_by="mario", decided_at=NOW,
                                    execution_status="unknown", execution_dag_run_id="run-uncertain")])
    store = IdempotentStore()

    report = migration.migrate_legacy_approvals(client=client, store=store, apply=True, writers_quiesced=True)

    assert report["imported"] == 0
    assert report["ready"] == 0
    assert report["blockers"]
    assert "unknown" in report["blockers"][0]["reason"].lower()
    assert store.receipts == {}


def test_apply_requires_explicit_quiescence_and_does_not_consume_source_on_failure():
    client = SourceClient([snapshot()])
    store = IdempotentStore()

    with pytest.raises(ValueError, match="writers-quiesced"):
        migration.migrate_legacy_approvals(client=client, store=store, apply=True, writers_quiesced=False)

    assert store.receipts == {}
    assert store.preflights == []


def test_cancelled_legacy_request_remains_cancelled_and_unclaimed_in_pg_projection():
    cancelled = snapshot(status="cancelled", decided_by="mario", decided_at=NOW,
                         decision_comment="No longer required.")
    item = migration.prepare_legacy_import([cancelled])
    db = ImportDatabase()
    store = ApprovalControlStore(db.connect)

    assert item.record.status == "cancelled"
    assert item.record.execution_status == "not_started"
    assert not item.record.execution_run_id
    assert item.record.scope["execution_eligibility"] == "legacy_review_required"
    assert store.import_legacy_request(item.record, history=item.history,
                                       source_hash=item.source_hash, dry_run=False)
    persisted = db.requests[item.record.request_id]
    persisted_scope = json.loads(persisted["scope_json"])
    assert persisted["status"] == "cancelled"
    assert persisted["execution_status"] == "not_started"
    assert persisted["execution_run_id"] is None
    assert persisted_scope["execution_eligibility"] == "legacy_review_required"
    assert "cancelled" in db.outbox[(item.record.request_id, item.record.version)]
    assert not store.import_legacy_request(item.record, history=item.history,
                                           source_hash=item.source_hash, dry_run=False)
