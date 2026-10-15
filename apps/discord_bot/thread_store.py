"""Transactional canonical workspaces and fail-closed investigation claims.

Session advisory locks serialize Discord creation across processes. Each external
write intent is committed separately, so a crash never erases an uncertain write.
No questions, answers, report bodies, or credentials are stored here.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
import os
from typing import Any, Callable, Iterator


TABLE = "dq_control.discord_incident_workspaces"
ATTEMPTS = "dq_control.discord_investigation_claims"
AUDIT = "dq_control.discord_workspace_events"


class WorkspaceBusy(RuntimeError):
    """Another process owns creation; do not race its Discord writes."""


class WorkspaceUncertain(RuntimeError):
    """An external write may have completed; reconcile instead of replaying."""


def starter_nonce(guild_id: str, alert_key: str, generation: int = 0) -> str:
    """Return a stable, bounded Discord nonce for one workspace generation."""
    return sha256(f"{guild_id}\0{alert_key}\0{generation}".encode()).hexdigest()[:24]


def lock_key(guild_id: str, alert_key: str) -> int:
    """Derive a signed 64-bit PostgreSQL advisory key without exposing content."""
    return int.from_bytes(sha256(f"{guild_id}\0{alert_key}".encode()).digest()[:8], "big", signed=True)


@dataclass(frozen=True)
class IncidentWorkspaceState:
    """Metadata for one authoritative incident in a configured guild/channel."""

    guild_id: str
    alert_key: str
    alert_ref: str
    channel_id: str
    generation: int = 0
    starter_nonce: str = ""
    starter_message_id: str = ""
    thread_id: str = ""
    starter_attempted: bool = False
    thread_attempted: bool = False


COLUMNS = tuple(IncidentWorkspaceState.__dataclass_fields__)


class WorkspaceLease:
    """A session-locked row; save commits intent before the next HTTP write."""

    def __init__(self, connection: Any, state: IncidentWorkspaceState) -> None:
        self.connection = connection
        self.state = state

    def save(self, **changes: Any) -> IncidentWorkspaceState:
        """Persist row metadata while retaining the session advisory lock."""
        state = replace(self.state, **changes)
        mutable = COLUMNS[2:]
        values = asdict(state)
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {TABLE} SET " + ", ".join(f"{name} = %s" for name in mutable)
                + " , updated_at = now() WHERE guild_id = %s AND alert_key = %s",
                tuple(values[name] for name in mutable) + (state.guild_id, state.alert_key),
            )
        self.connection.commit()
        self.state = state
        return state

    def replace_missing(self) -> IncidentWorkspaceState:
        """Audit an authoritative thread 404 and atomically start a new generation."""
        old = self.state
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"INSERT INTO {AUDIT} (guild_id, alert_key, event, generation, thread_id) "
                "VALUES (%s, %s, 'missing_404_replacement', %s, %s)",
                (old.guild_id, old.alert_key, old.generation, old.thread_id or old.starter_message_id),
            )
        return self.save(
            generation=old.generation + 1,
            starter_nonce=starter_nonce(old.guild_id, old.alert_key, old.generation + 1),
            starter_message_id="", thread_id="", starter_attempted=False, thread_attempted=False,
        )


class IncidentThreadStore:
    """PostgreSQL registry; missing control storage always fails closed."""

    def __init__(self, connection_factory: Callable | None = None) -> None:
        self.connection_factory = connection_factory

    @contextmanager
    def connection(self) -> Iterator[Any]:
        if self.connection_factory is not None:
            with self.connection_factory() as connection:
                yield connection
            return
        import psycopg
        url = os.getenv("DQ_CONTROL_DATABASE_URL", "").strip()
        if not url:
            raise RuntimeError("Discord incident workspaces require the control database.")
        with psycopg.connect(url, connect_timeout=5) as connection:
            yield connection

    @contextmanager
    def locked(self, guild_id: str, alert_key: str, alert_ref: str,
               channel_id: str) -> Iterator[WorkspaceLease]:
        """Claim the incident before any Discord HTTP; commit each creation checkpoint."""
        if not all((guild_id, alert_key, alert_ref, channel_id)):
            raise ValueError("Canonical workspace identifiers must be authoritative and nonempty.")
        key = lock_key(guild_id, alert_key)
        with self.connection() as connection:
            acquired = False
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_try_advisory_lock(%s)", (key,))
                    acquired = bool(cursor.fetchone()[0])
                    if not acquired:
                        raise WorkspaceBusy("Incident workspace creation is already in progress.")
                    initial = IncidentWorkspaceState(guild_id, alert_key, alert_ref, channel_id,
                                                     starter_nonce=starter_nonce(guild_id, alert_key))
                    cursor.execute(
                        f"INSERT INTO {TABLE} ({', '.join(COLUMNS)}) "
                        f"VALUES ({', '.join(['%s'] * len(COLUMNS))}) "
                        "ON CONFLICT (guild_id, alert_key) DO NOTHING",
                        tuple(asdict(initial).values()),
                    )
                    cursor.execute(f"SELECT {', '.join(COLUMNS)} FROM {TABLE} "
                                   "WHERE guild_id = %s AND alert_key = %s", (guild_id, alert_key))
                    state = IncidentWorkspaceState(**dict(zip(COLUMNS, cursor.fetchone())))
                connection.commit()
                if state.channel_id != channel_id or state.alert_ref != alert_ref:
                    raise PermissionError("Canonical workspace authority/configuration does not match.")
                yield WorkspaceLease(connection, state)
            finally:
                connection.rollback()
                if acquired:
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT pg_advisory_unlock(%s)", (key,))
                    connection.commit()

    def find_thread(self, guild_id: str, thread_id: str) -> IncidentWorkspaceState | None:
        """Ground a canonical follow-up without depending on mention history."""
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(f"SELECT {', '.join(COLUMNS)} FROM {TABLE} "
                           "WHERE guild_id = %s AND thread_id = %s", (guild_id, thread_id))
            row = cursor.fetchone()
            return IncidentWorkspaceState(**dict(zip(COLUMNS, row))) if row else None

    def claim_investigation(self, guild_id: str, alert_key: str, request_id: str,
                            reinvestigate: bool = False) -> bool:
        """Persist a single attempt; uncertain/active calls have no TTL or replay path."""
        if not request_id:
            raise ValueError("Investigation requires a stable interaction/request identifier.")
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key(guild_id, alert_key),))
            cursor.execute(
                f"SELECT request_id, status FROM {ATTEMPTS} "
                "WHERE guild_id = %s AND alert_key = %s ORDER BY created_at DESC",
                (guild_id, alert_key),
            )
            previous = cursor.fetchall()
            if (any(row[0] == request_id or row[1] in {"claimed", "uncertain"} for row in previous)
                    or (any(row[1] != "cancelled_before_start" for row in previous) and not reinvestigate)):
                return False
            cursor.execute(
                f"INSERT INTO {ATTEMPTS} (guild_id, alert_key, request_id, status) "
                "VALUES (%s, %s, %s, 'claimed') ON CONFLICT DO NOTHING RETURNING request_id",
                (guild_id, alert_key, request_id),
            )
            return cursor.fetchone() is not None

    def cancel_unstarted_investigation(self, guild_id: str, alert_key: str, request_id: str) -> None:
        """Retire only this claim when the caller proves triage never started."""
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {ATTEMPTS} SET status = 'cancelled_before_start', updated_at = now() "
                "WHERE guild_id = %s AND alert_key = %s AND request_id = %s AND status = 'claimed'",
                (guild_id, alert_key, request_id),
            )

    def finish_investigation(self, guild_id: str, alert_key: str, request_id: str,
                             *, completed: bool, report_id: str = "") -> None:
        """Record completion or uncertainty; failures never grant a retry automatically."""
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {ATTEMPTS} SET status = %s, report_id = %s, updated_at = now() "
                "WHERE guild_id = %s AND alert_key = %s AND request_id = %s AND status = 'claimed'",
                ("complete" if completed else "uncertain", report_id, guild_id, alert_key, request_id),
            )
