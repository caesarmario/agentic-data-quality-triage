"""Explicit, fail-closed CH -> PG approval cutover. Dry-run is the default.

Quiesce ALL old CH approval writers before --apply --writers-quiesced. This tool
never applies SQL, deletes CH rows, reconciles uncertain runs, or changes scope.
It retains every available physical snapshot (not FINAL); snapshots already
removed by historical ReplacingMergeTree merges cannot be reconstructed here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.tools.approval_control_store import ApprovalControlConflict, ApprovalControlRecord
from agent.tools.approval_queue import (
    APPROVAL_REQUEST_COLUMNS, APPROVAL_REQUESTS_TABLE, ApprovalRequest,
    approval_from_row, canonical_json, queue_control_scope,
)


@dataclass(frozen=True)
class LegacyImport:
    """Verified source history plus the exact non-executable PG snapshot."""

    record: ApprovalControlRecord
    history: list[dict[str, Any]]
    source_hash: str


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def prepare_legacy_import(snapshots: list[ApprovalRequest]) -> LegacyImport:
    """Reject ambiguous histories instead of choosing a winner or rewriting flags."""
    if not snapshots:
        raise ValueError("Empty legacy history")
    ordered = sorted(snapshots, key=lambda item: (_utc(item.updated_at), canonical_json(item.model_dump(mode="json"))))
    immutable_fields = ("request_id", "created_at", "alert_id", "alert_key", "agent_run_id", "action_type",
                        "risk_level", "requested_by", "reason", "dispatcher_dag_id", "target_dag_id",
                        "start_date", "end_date", "parameters", "dry_run", "idempotency_key")
    first = ordered[0]
    history = []
    previous = None
    for snapshot in ordered:
        if any(getattr(snapshot, key) != getattr(first, key) for key in immutable_fields):
            raise ApprovalControlConflict("Legacy immutable scope/parameters changed")
        if snapshot.action_type != "backfill":
            raise ApprovalControlConflict("Legacy import cannot grant typed recovery or other action rights")
        if not re.fullmatch(r"[0-9a-f]{64}", snapshot.idempotency_key):
            raise ApprovalControlConflict("Legacy idempotency key is not a full SHA-256 hash")
        if not re.fullmatch(r"[A-Za-z0-9_.:+-]{1,200}", snapshot.request_id):
            raise ApprovalControlConflict("Invalid legacy request ID")
        if not 5 <= len(snapshot.reason) <= 2000 or not snapshot.requested_by.strip():
            raise ApprovalControlConflict("Legacy requester/reason cannot satisfy PG constraints")
        if _utc(snapshot.updated_at) < _utc(snapshot.created_at):
            raise ApprovalControlConflict("Legacy timestamps are inconsistent")
        if snapshot.status not in {"pending", "approved", "rejected", "cancelled"}:
            raise ApprovalControlConflict("Unknown legacy approval status")
        execution = str(snapshot.execution_status)
        if execution not in {"not_started", "dispatching", "dispatched", "succeeded", "failed"}:
            raise ApprovalControlConflict("Legacy execution is unknown; manual reconciliation required")
        if execution != "not_started" and (snapshot.status != "approved" or not snapshot.execution_dag_run_id):
            raise ApprovalControlConflict("Legacy execution ownership is inconsistent")
        if execution == "not_started" and snapshot.execution_dag_run_id:
            raise ApprovalControlConflict("Legacy run ownership is set without a known claim")
        if snapshot.status in {"approved", "rejected", "cancelled"} and (not snapshot.decided_by or not snapshot.decided_at):
            raise ApprovalControlConflict("Legacy terminal decision identity/time is missing")
        if snapshot.status == "pending" and snapshot.decided_at:
            raise ApprovalControlConflict("Legacy pending request already has a decision")
        payload = snapshot.model_dump(mode="json", exclude={"version", "execution_eligibility"})
        payload["created_at"] = _utc(snapshot.created_at).isoformat()
        payload["updated_at"] = _utc(snapshot.updated_at).isoformat()
        if snapshot.decided_at:
            payload["decided_at"] = _utc(snapshot.decided_at).isoformat()
        if previous is not None:
            if _utc(previous.updated_at) == _utc(snapshot.updated_at) and history[-1] != payload:
                raise ApprovalControlConflict("Tied legacy timestamps have conflicting states")
            allowed = {"pending": {"pending", "approved", "rejected", "cancelled"},
                       "approved": {"approved", "cancelled"}, "rejected": {"rejected"}, "cancelled": {"cancelled"}}
            if snapshot.status not in allowed[str(previous.status)]:
                raise ApprovalControlConflict("Legacy decision history regressed or conflicts")
            if previous.execution_dag_run_id and previous.execution_dag_run_id != snapshot.execution_dag_run_id:
                raise ApprovalControlConflict("Legacy execution history has multiple owners")
            rank = {"not_started": 0, "dispatching": 1, "dispatched": 2, "succeeded": 3, "failed": 3}
            if (rank[execution] < rank[str(previous.execution_status)]
                    or (str(previous.execution_status) in {"succeeded", "failed"} and execution != str(previous.execution_status))):
                raise ApprovalControlConflict("Legacy execution history regressed or conflicts")
        history.append(payload)
        previous = snapshot
    latest = ordered[-1]
    if str(latest.execution_status) in {"dispatching", "dispatched"}:
        raise ApprovalControlConflict("Legacy execution is in-flight; reconcile existing runs, never rerun")
    record = ApprovalControlRecord(
        request_id=latest.request_id, idempotency_hash=latest.idempotency_key, request_generation=1,
        action_type=latest.action_type, scope=queue_control_scope(latest, legacy=True),
        requested_by=latest.requested_by, reason=latest.reason, status=str(latest.status),
        version=len({canonical_json(item) for item in history}),
        created_at=_utc(latest.created_at), updated_at=_utc(latest.updated_at),
        decided_by=latest.decided_by or None, decided_at=_utc(latest.decided_at) if latest.decided_at else None,
        decision_comment=latest.decision_comment,
        cancelled_by=latest.decided_by if latest.status == "cancelled" else None,
        cancelled_at=_utc(latest.decided_at) if latest.status == "cancelled" and latest.decided_at else None,
        execution_run_id=latest.execution_dag_run_id or None,
        execution_status=str(latest.execution_status), execution_error=latest.execution_error,
    )
    encoded = json.dumps(history, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return LegacyImport(record, history, hashlib.sha256(encoded.encode("utf-8")).hexdigest())


def migrate_legacy_approvals(*, client: Any, store: Any, apply: bool = False, writers_quiesced: bool = False) -> dict[str, Any]:
    """Preflight the entire available source before importing; report every blocker."""
    if apply and not writers_quiesced:
        raise ValueError("--apply requires --writers-quiesced; old CH writers do not share PG locks")
    rows = client.query(
        f"SELECT {', '.join(APPROVAL_REQUEST_COLUMNS)} FROM {APPROVAL_REQUESTS_TABLE} "
        "ORDER BY request_id, updated_at"
    ).result_rows
    grouped: dict[str, list[ApprovalRequest]] = defaultdict(list)
    blockers = []
    for row in rows:
        try:
            snapshot = approval_from_row(row)
            grouped[snapshot.request_id].append(snapshot)
        except (ValueError, TypeError):
            blockers.append({"request_id": str(row[0]) if row else "", "reason": "Malformed legacy snapshot"})
    prepared = []
    hashes: dict[str, str] = {}
    for request_id, snapshots in grouped.items():
        try:
            item = prepare_legacy_import(snapshots)
            other = hashes.setdefault(item.record.idempotency_hash, request_id)
            if other != request_id:
                raise ApprovalControlConflict("Multiple legacy IDs share one idempotency hash")
            store.import_legacy_request(item.record, history=item.history, source_hash=item.source_hash, dry_run=True)
            prepared.append(item)
        except (ValueError, LookupError) as exc:
            blockers.append({"request_id": request_id, "reason": str(exc)})
    report = {"mode": "apply" if apply else "dry_run", "source_rows": len(rows), "requests": len(grouped),
              "imported": 0, "reused": 0, "ready": len(prepared), "blockers": blockers,
              "history_coverage": "All available physical CH snapshots; historical merges may already have removed versions"}
    if blockers or not apply:
        return report
    for item in prepared:
        try:
            created = store.import_legacy_request(item.record, history=item.history, source_hash=item.source_hash, dry_run=False)
            report["imported" if created else "reused"] += 1
        except (ValueError, LookupError) as exc:
            report["blockers"].append({"request_id": item.record.request_id, "reason": str(exc)})
            break
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Import after a successful full preflight (never applies SQL)")
    parser.add_argument("--writers-quiesced", action="store_true", help="Attest ALL old CH approval writers have stopped")
    args = parser.parse_args(argv)
    from agent.tools.approved_actions import build_approval_control_store
    from pipelines.common.clickhouse import build_clickhouse_client
    try:
        report = migrate_legacy_approvals(client=build_clickhouse_client(), store=build_approval_control_store(),
                                          apply=args.apply, writers_quiesced=args.writers_quiesced)
    except Exception:
        # Driver errors can contain DSNs, credentials or SQL. Do not print them.
        print(json.dumps({"blockers": [{"reason": "Migration unavailable or unsafe; check schema and quiescence before retry"}]}))
        return 2
    print(json.dumps(report, sort_keys=True))
    return 2 if report["blockers"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
