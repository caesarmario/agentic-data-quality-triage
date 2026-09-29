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
SUPPORTED_ACTION_TYPES = frozenset({"backfill", "rerun_dbt", "create_ticket", "post_notification"})
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
        normalized_reason = reason.strip()
        if not 5 <= len(normalized_reason) <= 2000:
            raise ValueError("reason must contain 5 to 2000 characters")
        request_id = f"APR-{full_hash[:20].upper()}"
        with self._transaction() as cursor:
            cursor.execute(
                f"INSERT INTO {TABLE} (request_id, idempotency_hash, request_generation, action_type, "
                f"scope_json, requested_by, reason) VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s) "
                f"ON CONFLICT (idempotency_hash) DO NOTHING RETURNING {SELECT_COLUMNS}",
                (request_id, full_hash, generation, action_type, scope_json, actor, normalized_reason),
            )
            created = self._one(cursor)
            if created is not None:
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

    # --- Deciding And Cancelling Requests
    def decide(
        self, request_id: str, *, decision: str, decided_by: str,
        expected_version: int, comment: str = "",
    ) -> ApprovalControlRecord:
        normalized_id = _identifier(request_id, "request_id")
        actor = _actor(decided_by, "decided_by")
        if decision not in ("approve", "reject"):
            raise ValueError("decision must be approve or reject")
        if not 0 < expected_version:
            raise ValueError("expected_version must be positive")
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
            return self._one(cursor) or self._require_transition(cursor, normalized_id)

    def cancel(
        self, request_id: str, *, cancelled_by: str, expected_version: int,
    ) -> ApprovalControlRecord:
        normalized_id = _identifier(request_id, "request_id")
        actor = _actor(cancelled_by, "cancelled_by")
        if not 0 < expected_version:
            raise ValueError("expected_version must be positive")
        with self._transaction() as cursor:
            cursor.execute(
                f"UPDATE {TABLE} SET status = 'cancelled', cancelled_by = %s, "
                f"cancelled_at = now(), updated_at = now(), version = version + 1 "
                f"WHERE request_id = %s AND status IN ('pending', 'approved') "
                f"AND execution_status = 'not_started' AND version = %s "
                f"RETURNING {SELECT_COLUMNS}",
                (actor, normalized_id, expected_version),
            )
            return self._one(cursor) or self._require_transition(cursor, normalized_id)

    # --- Claiming And Recording Execution
    def claim(
        self, request_id: str, *, execution_run_id: str, expected_version: int,
    ) -> ApprovalControlRecord:
        normalized_id = _identifier(request_id, "request_id")
        run_id = _identifier(execution_run_id, "execution_run_id")
        if not 0 < expected_version:
            raise ValueError("expected_version must be positive")
        with self._transaction() as cursor:
            cursor.execute(
                f"UPDATE {TABLE} SET execution_run_id = %s, execution_status = 'dispatching', "
                f"updated_at = now(), version = version + 1 "
                f"WHERE request_id = %s AND status = 'approved' "
                f"AND execution_status = 'not_started' AND version = %s "
                f"RETURNING {SELECT_COLUMNS}",
                (run_id, normalized_id, expected_version),
            )
            return self._one(cursor) or self._require_transition(cursor, normalized_id)

    def record_execution_outcome(
        self, request_id: str, *, execution_run_id: str,
        outcome: str, expected_version: int, error: str = "",
    ) -> ApprovalControlRecord:
        normalized_id = _identifier(request_id, "request_id")
        run_id = _identifier(execution_run_id, "execution_run_id")
        if outcome not in ("dispatched", "succeeded", "failed", "unknown"):
            raise ValueError("outcome must be dispatched, succeeded, failed, or unknown")
        if not 0 < expected_version:
            raise ValueError("expected_version must be positive")
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
            return self._one(cursor) or self._require_transition(cursor, normalized_id)
