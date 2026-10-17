"""PostgreSQL persistence for immutable sources, plans, and single-use executions."""
from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from agent.recovery.contracts import RecoveryExecution, RecoveryPlan
from agent.tools.incident_resolution_store import (
    ResolutionConflict, control_transaction, decoded, encoded,
)


def register_source_snapshot(manifest: dict[str, Any]) -> None:
    """Register a verified immutable object; conflicting reuse is never overwritten."""
    from pipelines.seeding.snapshots import SourceSnapshotManifest
    manifest = SourceSnapshotManifest.model_validate(manifest).model_dump(mode="json")
    with control_transaction() as cursor:
        cursor.execute(
            "INSERT INTO dq_control.source_snapshots(snapshot_id,dataset,dt,kind,checksum,manifest) "
            "VALUES (%s,'orders',%s,%s,%s,%s::jsonb) ON CONFLICT(snapshot_id) DO NOTHING",
            (manifest["snapshot_id"], manifest["dt"], manifest["kind"], manifest["sha256"], encoded(manifest)),
        )
        cursor.execute("SELECT manifest FROM dq_control.source_snapshots WHERE snapshot_id=%s", (manifest["snapshot_id"],))
        existing = decoded(cursor.fetchone()[0])
        if existing != manifest:
            raise ResolutionConflict("An immutable source version already has different metadata.")


def latest_source_snapshot(dt: str, kind: str = "upstream") -> dict[str, Any] | None:
    """Discover a version only when planning. Execution always uses its pinned ID."""
    if kind not in {"upstream", "landing"}:
        raise ValueError("Unsupported source snapshot kind.")
    with control_transaction() as cursor:
        cursor.execute("SELECT manifest FROM dq_control.source_snapshots WHERE dataset='orders' "
                       "AND dt=%s AND kind=%s ORDER BY created_at DESC,snapshot_id DESC LIMIT 1", (dt, kind))
        row = cursor.fetchone()
        return decoded(row[0]) if row else None


def read_plan(cursor: Any, plan_id: str) -> RecoveryPlan:
    """Read a plan; no update path exists for its approved scope."""
    cursor.execute("SELECT record FROM dq_control.recovery_plans WHERE plan_id=%s", (str(UUID(plan_id)),))
    row = cursor.fetchone()
    if row is None:
        raise LookupError("Recovery plan was not found.")
    return RecoveryPlan.model_validate(decoded(row[0]))


def read_execution(cursor: Any, execution_id: str) -> RecoveryExecution:
    """Lock one execution before a durable progress transition."""
    cursor.execute("SELECT record FROM dq_control.recovery_executions WHERE execution_id=%s FOR UPDATE",
                   (str(UUID(execution_id)),))
    row = cursor.fetchone()
    if row is None:
        raise LookupError("Recovery execution was not found.")
    return RecoveryExecution.model_validate(decoded(row[0]))


def save_execution(cursor: Any, execution: RecoveryExecution) -> None:
    """Persist progress without changing immutable plan ownership."""
    cursor.execute("INSERT INTO dq_control.recovery_executions(execution_id,plan_id,status,record) "
                   "VALUES (%s,%s,%s,%s::jsonb) ON CONFLICT(execution_id) DO UPDATE SET "
                   "status=EXCLUDED.status,record=EXCLUDED.record,updated_at=now()",
                   (str(execution.execution_id), str(execution.plan_id), execution.status,
                    execution.model_dump_json()))


def outbox_event(cursor: Any, aggregate_id: str, event_type: str, payload: dict[str, Any]) -> None:
    """Keep audit/notification failure independent from the committed decision."""
    cursor.execute("INSERT INTO dq_control.recovery_outbox(event_id,aggregate_id,event_type,payload) "
                   "VALUES (%s,%s,%s,%s::jsonb) ON CONFLICT(aggregate_id,event_type) DO NOTHING",
                   (str(uuid4()), aggregate_id, event_type, encoded(payload)))
