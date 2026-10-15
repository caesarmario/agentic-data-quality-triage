"""PostgreSQL locks and durable records shared by every lifecycle writer."""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from typing import Any

# A platform-wide lock also serializes approval creation with closure. This is
# deliberately conservative for the local POC, not a high-throughput design.
LIFECYCLE_LOCK = 6811392374251903


def encoded(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True, allow_nan=False)


def decoded(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


class ResolutionConflict(ValueError):
    """A stale or unsafe lifecycle transition (HTTP 409)."""


class ResolutionUnavailable(RuntimeError):
    """A required dependency is unavailable (HTTP 503)."""


@contextmanager
def control_transaction():
    url = os.getenv("DQ_CONTROL_DATABASE_URL", "").strip()
    if not url:
        raise ResolutionUnavailable("Control store belum tersedia.")
    try:
        import psycopg
        connect = psycopg.connect
    except ImportError:
        import psycopg2
        connect = psycopg2.connect
    connection = connect(url, connect_timeout=5)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout = '10s'")
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (LIFECYCLE_LOCK,))
            yield cursor
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def read_incident(cursor, alert_key: str) -> dict | None:
    cursor.execute(
        "SELECT alert_ref,revision,episode,status,snapshot FROM dq_control.incident_controls "
        "WHERE alert_key=%s FOR UPDATE", (alert_key,),
    )
    row = cursor.fetchone()
    if not row:
        return None
    return dict(alert_key=alert_key, alert_ref=row[0], revision=row[1], episode=row[2],
                status=row[3], snapshot=decoded(row[4]))


def save_incident(cursor, incident: dict) -> None:
    cursor.execute(
        "INSERT INTO dq_control.incident_controls(alert_key,alert_ref,revision,episode,status,snapshot) "
        "VALUES (%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT(alert_key) DO UPDATE SET "
        "revision=EXCLUDED.revision,episode=EXCLUDED.episode,status=EXCLUDED.status,"
        "snapshot=EXCLUDED.snapshot,updated_at=now()",
        (incident["alert_key"], incident["alert_ref"], incident["revision"], incident["episode"],
         incident["status"], encoded(incident["snapshot"])),
    )


def read_request(cursor, request_id: str) -> dict:
    cursor.execute("SELECT record FROM dq_control.incident_revalidations WHERE request_id=%s FOR UPDATE",
                   (request_id,))
    row = cursor.fetchone()
    if not row:
        raise LookupError("Pemeriksaan tidak ditemukan.")
    return decoded(row[0])


def save_request(cursor, request: dict, alert_key: str, idempotency_hash: str = "") -> None:
    cursor.execute(
        "INSERT INTO dq_control.incident_revalidations(request_id,alert_key,incident_revision,"
        "idempotency_hash,status,record) VALUES (%s,%s,%s,%s,%s,%s::jsonb) "
        "ON CONFLICT(request_id) DO UPDATE SET status=EXCLUDED.status,record=EXCLUDED.record",
        (request["request_id"], alert_key, request["incident_revision"], idempotency_hash,
         request["status"], encoded(request)),
    )
