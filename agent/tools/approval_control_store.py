####
## Transactional Approval Control Store for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterator, Protocol


# --- Defining Constants
TABLE = "dq_control.approval_requests"
MAX_SCOPE_BYTES = 10_000
SENSITIVE_KEY_PARTS = ("api_key", "credential", "password", "secret", "token")
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_.:+-]{1,200}$")
SUPPORTED_ACTION_TYPES = frozenset({"backfill", "rerun_dbt", "create_ticket", "post_notification", "recover_partition"})
LEGACY_IMPORTS_TABLE = "dq_control.approval_legacy_imports"
PROJECTION_OUTBOX_TABLE = "dq_control.approval_projection_outbox"
RECOVERY_REQUESTER = "datasentry_recovery"
COLUMNS = (
    "request_id", "idempotency_hash", "request_generation", "action_type",
    "scope_json", "requested_by", "reason", "status", "version", "created_at",
    "updated_at", "decided_by", "decided_at", "decision_comment", "cancelled_by",
    "cancelled_at", "execution_run_id", "execution_status", "execution_error",
)
SELECT_COLUMNS = ", ".join(COLUMNS)


# --- Defining Database Contracts
class Cursor(Protocol):
    def execute(self, statement: str, parameters: tuple[Any, ...]) -> Any: ...
    def fetchone(self) -> tuple[Any, ...] | None: ...
    def fetchall(self) -> list[tuple[Any, ...]]: ...
    def close(self) -> None: ...


class Connection(Protocol):
    def cursor(self) -> Cursor: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def close(self) -> None: ...


class ApprovalControlConflict(ValueError):
    """A stale version or invalid lifecycle transition was rejected."""


# --- Defining State Models
@dataclass(frozen=True)
class ApprovalControlRecord:
    request_id: str
    idempotency_hash: str
    request_generation: int
    action_type: str
    scope: dict[str, Any]
    requested_by: str
    reason: str
    status: str
    version: int
    created_at: datetime
    updated_at: datetime
    decided_by: str | None
    decided_at: datetime | None
    decision_comment: str
    cancelled_by: str | None
    cancelled_at: datetime | None
    execution_run_id: str | None
    execution_status: str
    execution_error: str


# --- Validating Immutable Request Scope
def _actor(value: str, field: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 200:
        raise ValueError(f"{field} must contain 1 to 200 characters")
    return normalized


def _identifier(value: str, field: str) -> str:
    normalized = value.strip()
    if not IDENTIFIER_PATTERN.fullmatch(normalized):
        raise ValueError(f"{field} has an invalid format")
    return normalized


def _generation(value: int) -> int:
    """Validate an explicit repeat generation before opening a database connection."""
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1_000_000:
        raise ValueError("request_generation must be an integer between 1 and 1000000")
    return value


def _version(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("expected_version must be a positive integer")
    return value


def _check_scope(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("scope keys must be strings")
            if any(part in key.lower() for part in SENSITIVE_KEY_PARTS):
                raise ValueError("scope cannot contain credential-like keys")
            _check_scope(item)
    elif isinstance(value, list):
        for item in value:
            _check_scope(item)
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise ValueError("scope must contain only JSON values")


def _canonical_scope(action_type: str, scope: dict[str, Any]) -> tuple[str, str]:
    if action_type not in SUPPORTED_ACTION_TYPES:
        raise ValueError(f"Unsupported approval action type: {action_type}")
    if not isinstance(scope, dict) or not scope:
        raise ValueError("scope must be a non-empty object")
    _check_scope(scope)
    try:
        scope_json = json.dumps(scope, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("scope must contain only JSON values") from exc
    if len(scope_json.encode("utf-8")) > MAX_SCOPE_BYTES:
        raise ValueError("scope is too large")
    payload = json.dumps({"action_type": action_type, "scope": scope}, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return scope_json, hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _generation_hash(base_hash: str, request_generation: int) -> str:
    """Preserve generation-one identities while deriving explicit repeat identities."""
    if request_generation == 1:
        return base_hash
    payload = f"{base_hash}:generation:{request_generation}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _record(row: tuple[Any, ...]) -> ApprovalControlRecord:
    if len(row) != len(COLUMNS):
        raise ValueError("Invalid approval control row shape")
    values = dict(zip(COLUMNS, row, strict=True))
    scope = values.pop("scope_json")
    if isinstance(scope, str):
        scope = json.loads(scope)
    if not isinstance(scope, dict):
        raise ValueError("Stored approval scope must be an object")
    values["scope"] = scope
    return ApprovalControlRecord(**values)


# --- Managing Transaction Boundaries
class ApprovalControlStore:
    """Single-row PostgreSQL transitions; callers supply a DB-API connection factory."""

    def __init__(self, connection_factory: Callable[[], Connection]) -> None:
        self._connection_factory = connection_factory

    @contextmanager
    def _transaction(self) -> Iterator[Cursor]:
        connection = self._connection_factory()
        try:
            cursor = connection.cursor()
            try:
                from agent.tools.incident_resolution_store import LIFECYCLE_LOCK
                cursor.execute("SELECT pg_advisory_xact_lock(%s)", (LIFECYCLE_LOCK,))
                yield cursor
                connection.commit()
            finally:
                cursor.close()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _one(cursor: Cursor) -> ApprovalControlRecord | None:
        row = cursor.fetchone()
        return _record(row) if row is not None else None

    def _latest(self, cursor: Cursor, request_id: str) -> ApprovalControlRecord | None:
        cursor.execute(
            f"SELECT {SELECT_COLUMNS} FROM {TABLE} WHERE request_id = %s",
            (request_id,),
        )
        return self._one(cursor)

    def _require_transition(self, cursor: Cursor, request_id: str) -> ApprovalControlRecord:
        current = self._latest(cursor, request_id)
        if current is None:
            raise LookupError("Approval control request not found")
        raise ApprovalControlConflict(
            f"Approval control transition rejected; current status={current.status}, "
            f"execution_status={current.execution_status}, version={current.version}"
        )

    @staticmethod
    def _enqueue(cursor: Cursor, record: ApprovalControlRecord, event: str, actor: str) -> None:
        """Persist the CH projection and audit intent in the SAME state transaction."""
        from agent.tools.approval_queue import approval_from_control_record
        payload = {
            "approval": approval_from_control_record(record).model_dump(mode="json"),
            "event": event,
            "actor": actor,
        }
        cursor.execute(
            f"INSERT INTO {PROJECTION_OUTBOX_TABLE} (request_id, version, payload_json) "
            "VALUES (%s, %s, %s::jsonb) ON CONFLICT (request_id, version) DO NOTHING",
            (record.request_id, record.version, json.dumps(payload, sort_keys=True, allow_nan=False)),
        )

    def _changed(self, cursor: Cursor, request_id: str, event: str, actor: str) -> ApprovalControlRecord:
        record = self._one(cursor) or self._require_transition(cursor, request_id)
        self._enqueue(cursor, record, event, actor)
        return record

    @staticmethod
    def _incident_open(cursor: Cursor, scope: dict[str, Any]) -> None:
        """Creation shares the closure lock; never acquire it in a nested transaction."""
        key = scope.get("alert_key") or scope.get("_approval_request", {}).get("alert_key")
        if key:
            cursor.execute(
                "SELECT status FROM dq_control.incident_controls WHERE alert_key=%s FOR UPDATE", (key,),
            )
            row = cursor.fetchone()
            if row and row[0] == "resolved":
                raise ApprovalControlConflict("Incident is resolved; new remediation proposals are blocked")

    # --- Creating And Reading Requests
    def create_request(
        self, *, action_type: str, scope: dict[str, Any], requested_by: str, reason: str,
        request_generation: int = 1,
    ) -> tuple[ApprovalControlRecord, bool]:
        scope_json, base_hash = _canonical_scope(action_type, scope)
        generation = _generation(request_generation)
        full_hash = _generation_hash(base_hash, generation)
        normalized_scope = json.loads(scope_json)
        actor = _actor(requested_by, "requested_by")
        if action_type == "recover_partition":
            from agent.tools.approved_actions import normalize_action_scope
            if actor != RECOVERY_REQUESTER:
                raise ValueError("Recovery approvals require the server-system requester datasentry_recovery")
            if normalize_action_scope(action_type, normalized_scope) != normalized_scope:
                raise ValueError("Recovery scope must already match the exact typed plan scope")
        normalized_reason = reason.strip()
        if not 5 <= len(normalized_reason) <= 2000:
            raise ValueError("reason must contain 5 to 2000 characters")
        request_id = f"APR-{full_hash[:20].upper()}"
        with self._transaction() as cursor:
            self._incident_open(cursor, normalized_scope)
            cursor.execute(
                f"INSERT INTO {TABLE} (request_id, idempotency_hash, request_generation, action_type, "
                f"scope_json, requested_by, reason) VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s) "
                f"ON CONFLICT (idempotency_hash) DO NOTHING RETURNING {SELECT_COLUMNS}",
                (request_id, full_hash, generation, action_type, scope_json, actor, normalized_reason),
            )
            created = self._one(cursor)
            if created is not None:
                self._enqueue(cursor, created, "approval_requested", actor)
                return created, True
            cursor.execute(
                f"SELECT {SELECT_COLUMNS} FROM {TABLE} WHERE idempotency_hash = %s",
                (full_hash,),
            )
            existing = self._one(cursor)
            if (
                existing is None
                or existing.action_type != action_type
                or existing.scope != normalized_scope
                or existing.request_generation != generation
            ):
                raise ApprovalControlConflict("Idempotency conflict could not be verified")
            return existing, False

    def read_latest(self, request_id: str) -> ApprovalControlRecord | None:
        normalized_id = _identifier(request_id, "request_id")
        with self._transaction() as cursor:
            return self._latest(cursor, normalized_id)

    def read_by_idempotency_hash(self, idempotency_hash: str) -> ApprovalControlRecord | None:
        with self._transaction() as cursor:
            cursor.execute(f"SELECT {SELECT_COLUMNS} FROM {TABLE} WHERE idempotency_hash = %s", (idempotency_hash,))
            return self._one(cursor)

    def list_requests(self, *, status: str | None = None, limit: int = 50) -> list[ApprovalControlRecord]:
        """Read only the authoritative PG queue, including recovery and imported records."""
        if status is not None and status not in {"pending", "approved", "rejected", "cancelled"}:
            raise ValueError("Invalid approval status")
        with self._transaction() as cursor:
            cursor.execute(
                f"SELECT {SELECT_COLUMNS} FROM {TABLE} WHERE (%s::text IS NULL OR status = %s) "
                "ORDER BY updated_at DESC, request_id ASC LIMIT %s",
                (status, status, max(1, min(int(limit), 100))),
            )
            return [_record(row) for row in cursor.fetchall()]

    def create_queue_request(self, approval: Any) -> tuple[ApprovalControlRecord, bool]:
        """Keep the original backfill ID, hash and immutable public parameters in PG."""
        from agent.tools.approval_queue import queue_control_scope
        scope = queue_control_scope(approval)
        scope_json, _ = _canonical_scope(approval.action_type, scope)
        with self._transaction() as cursor:
            self._incident_open(cursor, scope)
            cursor.execute(
                f"SELECT {SELECT_COLUMNS} FROM {TABLE} WHERE request_id = %s OR idempotency_hash = %s FOR UPDATE",
                (approval.request_id, approval.idempotency_key),
            )
            rows = cursor.fetchall()
            if rows:
                if len(rows) != 1:
                    raise ApprovalControlConflict("Conflicting approval ID and idempotency hash")
                existing = _record(rows[0])
                if (existing.request_id != approval.request_id or existing.idempotency_hash != approval.idempotency_key
                        or existing.action_type != approval.action_type
                        or existing.scope.get("_approval_request") != scope["_approval_request"]):
                    raise ApprovalControlConflict("Existing approval has a different immutable scope; migration/review required")
                return existing, False
            cursor.execute(
                f"INSERT INTO {TABLE} (request_id, idempotency_hash, request_generation, action_type, "
                f"scope_json, requested_by, reason) VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s) "
                f"RETURNING {SELECT_COLUMNS}",
                (approval.request_id, approval.idempotency_key, 1, approval.action_type,
                 scope_json, approval.requested_by, approval.reason),
            )
            created = self._one(cursor)
            if created is None:
                raise ApprovalControlConflict("Approval insertion did not return a record")
            self._enqueue(cursor, created, "approval_requested", approval.requested_by)
            return created, True

    # --- Importing Legacy State Without Reinterpreting Its Authority
    def import_legacy_request(
        self, record: ApprovalControlRecord, *, history: list[dict[str, Any]], source_hash: str,
        dry_run: bool = True,
    ) -> bool:
        """Import one verified legacy snapshot and its available history atomically.

        Returns True for a new import (or a prospective dry-run import), False for
        the identical previously imported source. PG never loses to a CH snapshot.
        """
        if record.action_type == "recover_partition" or record.execution_status in {"dispatching", "dispatched", "unknown"}:
            raise ApprovalControlConflict("Legacy execution is in-flight/unknown or attempts to grant recovery rights")
        if record.scope.get("execution_eligibility") != "legacy_review_required":
            raise ApprovalControlConflict("Legacy imports must require review before execution")
        history_json = json.dumps(history, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if hashlib.sha256(history_json.encode("utf-8")).hexdigest() != source_hash:
            raise ValueError("Legacy source hash does not match history")
        scope_json, _ = _canonical_scope(record.action_type, record.scope)
        with self._transaction() as cursor:
            cursor.execute(
                f"SELECT {SELECT_COLUMNS} FROM {TABLE} WHERE request_id = %s OR idempotency_hash = %s FOR UPDATE",
                (record.request_id, record.idempotency_hash),
            )
            existing = [_record(row) for row in cursor.fetchall()]
            cursor.execute(f"SELECT source_hash, history_json FROM {LEGACY_IMPORTS_TABLE} WHERE request_id = %s", (record.request_id,))
            imported = cursor.fetchone()
            if imported:
                saved_history = json.loads(imported[1]) if isinstance(imported[1], str) else imported[1]
                if imported[0] != source_hash or saved_history != history:
                    raise ApprovalControlConflict("Imported legacy history changed; manual reconciliation required")
                if len(existing) != 1:
                    raise ApprovalControlConflict("Imported PG request is missing or has a hash collision")
                current = existing[0]
                immutable = ("request_id", "idempotency_hash", "action_type", "scope", "requested_by", "reason", "created_at")
                if any(getattr(current, key) != getattr(record, key) for key in immutable):
                    raise ApprovalControlConflict("PG and legacy immutable scope conflict")
                if current.execution_status in {"dispatching", "dispatched", "unknown"}:
                    raise ApprovalControlConflict("PG execution is in-flight/unknown; reconcile before migration")
                return False
            if existing:
                raise ApprovalControlConflict("PG versus CH conflict: existing request has no matching legacy import receipt")
            if dry_run:
                return True
            values = [getattr(record, name) if name != "scope_json" else scope_json for name in COLUMNS]
            placeholders = ", ".join("%s::jsonb" if name == "scope_json" else "%s" for name in COLUMNS)
            cursor.execute(f"INSERT INTO {TABLE} ({SELECT_COLUMNS}) VALUES ({placeholders})", tuple(values))
            cursor.execute(
                f"INSERT INTO {LEGACY_IMPORTS_TABLE} (request_id, source_hash, history_json) VALUES (%s, %s, %s::jsonb)",
                (record.request_id, source_hash, history_json),
            )
            self._enqueue(cursor, record, "approval_legacy_imported", "legacy_approval_migration")
            return True

    def flush_projection_outbox(self, deliver: Callable[..., None], *, limit: int = 100) -> dict[str, int]:
        """Retry each CH append independently; failed deliveries remain durably pending.

        CH acknowledgements and PG cannot be atomic. Consumers must tolerate an
        at-least-once append after an acknowledgement/commit loss. The audit key
        and projection snapshot are deterministic for (request_id, version).
        """
        completed = failed = 0
        with self._transaction() as cursor:
            cursor.execute(
                f"SELECT request_id, version, payload_json, audit_done, projection_done FROM {PROJECTION_OUTBOX_TABLE} "
                "WHERE NOT (audit_done AND projection_done) ORDER BY request_id, version "
                "LIMIT %s FOR UPDATE SKIP LOCKED", (max(1, min(int(limit), 1000)),),
            )
            entries = cursor.fetchall()
            for request_id, version, raw, audit_done, projection_done in entries:
                payload = json.loads(raw) if isinstance(raw, str) else raw
                for phase, done in (("audit", audit_done), ("projection", projection_done)):
                    if done:
                        continue
                    try:
                        deliver(phase, payload, request_id, version)
                    except Exception:
                        failed += 1
                        break
                    # phase comes from the two literal values above, never from callers.
                    cursor.execute(
                        f"UPDATE {PROJECTION_OUTBOX_TABLE} SET {phase}_done = true WHERE request_id = %s AND version = %s",
                        (request_id, version),
                    )
                else:
                    completed += 1
        return {"completed": completed, "failed": failed, "selected": len(entries)}

    # --- Deciding And Cancelling Requests
    def decide(
        self, request_id: str, *, decision: str, decided_by: str,
        expected_version: int, comment: str = "",
    ) -> ApprovalControlRecord:
        normalized_id = _identifier(request_id, "request_id")
        actor = _actor(decided_by, "decided_by")
        if decision not in ("approve", "reject"):
            raise ValueError("decision must be approve or reject")
        _version(expected_version)
        if len(comment) > 2000:
            raise ValueError("comment is too long")
        status = "approved" if decision == "approve" else "rejected"
        with self._transaction() as cursor:
            cursor.execute(
                f"SELECT {SELECT_COLUMNS} FROM {TABLE} WHERE request_id = %s FOR UPDATE",
                (normalized_id,),
            )
            current = self._one(cursor)
            if current is None:
                raise LookupError("Approval control request not found")
            if decision == "approve" and current.requested_by.casefold() == actor.casefold():
                raise ApprovalControlConflict("The requester cannot approve the same action request")
            cursor.execute(
                f"UPDATE {TABLE} SET status = %s, decided_by = %s, decided_at = now(), "
                f"decision_comment = %s, updated_at = now(), version = version + 1 "
                f"WHERE request_id = %s AND status = 'pending' AND version = %s "
                f"RETURNING {SELECT_COLUMNS}",
                (status, actor, comment.strip(), normalized_id, expected_version),
            )
            return self._changed(cursor, normalized_id, "approval_decision", actor)

    def cancel(
        self, request_id: str, *, cancelled_by: str, expected_version: int, comment: str = "",
    ) -> ApprovalControlRecord:
        normalized_id = _identifier(request_id, "request_id")
        actor = _actor(cancelled_by, "cancelled_by")
        _version(expected_version)
        if len(comment) > 2000:
            raise ValueError("comment is too long")
        with self._transaction() as cursor:
            cursor.execute(
                f"UPDATE {TABLE} SET status = 'cancelled', cancelled_by = %s, "
                f"cancelled_at = now(), decision_comment = %s, updated_at = now(), version = version + 1 "
                f"WHERE request_id = %s AND status IN ('pending', 'approved') "
                f"AND execution_status = 'not_started' AND execution_run_id IS NULL AND version = %s "
                f"RETURNING {SELECT_COLUMNS}",
                (actor, comment.strip(), normalized_id, expected_version),
            )
            return self._changed(cursor, normalized_id, "approval_cancelled", actor)

    # --- Claiming And Recording Execution
    def claim(
        self, request_id: str, *, execution_run_id: str, expected_version: int,
    ) -> ApprovalControlRecord:
        normalized_id = _identifier(request_id, "request_id")
        run_id = _identifier(execution_run_id, "execution_run_id")
        _version(expected_version)
        with self._transaction() as cursor:
            current = self._latest(cursor, normalized_id)
            if current is not None and current.scope.get("execution_eligibility") == "legacy_review_required":
                raise ApprovalControlConflict("legacy_review_required: legacy scope cannot authorize execution")
            cursor.execute(
                f"UPDATE {TABLE} SET execution_run_id = %s, execution_status = 'dispatching', "
                f"updated_at = now(), version = version + 1 "
                f"WHERE request_id = %s AND status = 'approved' "
                f"AND execution_status = 'not_started' AND execution_run_id IS NULL AND version = %s "
                f"RETURNING {SELECT_COLUMNS}",
                (run_id, normalized_id, expected_version),
            )
            return self._changed(cursor, normalized_id, "approval_execution_transition", "airflow")

    def record_execution_outcome(
        self, request_id: str, *, execution_run_id: str,
        outcome: str, expected_version: int, error: str = "",
    ) -> ApprovalControlRecord:
        normalized_id = _identifier(request_id, "request_id")
        run_id = _identifier(execution_run_id, "execution_run_id")
        if outcome not in ("dispatched", "succeeded", "failed", "unknown"):
            raise ValueError("outcome must be dispatched, succeeded, failed, or unknown")
        _version(expected_version)
        if len(error) > 2000 or (outcome not in {"failed", "unknown"} and error):
            raise ValueError("error is only allowed for a failed or unknown outcome, up to 2000 characters")
        allowed_previous = ("dispatching",) if outcome == "dispatched" else ("dispatching", "dispatched")
        with self._transaction() as cursor:
            cursor.execute(
                f"UPDATE {TABLE} SET execution_status = %s, execution_error = %s, "
                f"updated_at = now(), version = version + 1 "
                f"WHERE request_id = %s AND status = 'approved' AND execution_run_id = %s "
                f"AND execution_status = ANY(%s) AND version = %s "
                f"RETURNING {SELECT_COLUMNS}",
                (outcome, error.strip(), normalized_id, run_id, list(allowed_previous), expected_version),
            )
            return self._changed(cursor, normalized_id, "approval_execution_transition", "airflow")
