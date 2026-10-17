"""Evidence-gated incident closure; no model, warehouse repair, or client SQL."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from uuid import UUID, uuid4

from agent.resolution_contracts import IncidentResolve, RevalidationCreate, RevalidationResult
from agent.tools.incident_resolution_store import (
    ResolutionConflict, ResolutionUnavailable, control_transaction, decoded, encoded,
    read_incident, read_request, save_incident, save_request,
)
from pipelines.common.clickhouse import build_clickhouse_client, quote_sql_literal


def now() -> datetime:
    return datetime.now(timezone.utc)


def timestamp(value) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def scope_for(snapshot: dict) -> dict:
    return {"kind": snapshot["alert_type"], "table_name": snapshot["table_name"],
            "dt": str(snapshot.get("dt") or ""), "metric": snapshot["metric"],
            "dimension": snapshot.get("dimension", "")}


def operation_hash(action: str, ref: str, key: str) -> str:
    return hashlib.sha256(encoded([action, ref, key]).encode()).hexdigest()


def _source(client, reference: str) -> dict:
    from agent.tools.alert_lifecycle import query_alert_lifecycle_source_row
    result = client.query("SELECT alert_key FROM dq.alerts FINAL WHERE alert_key=" +
                          quote_sql_literal(reference) + " OR alert_display_id=" +
                          quote_sql_literal(reference) + " ORDER BY updated_at DESC LIMIT 1")
    if not result.result_rows:
        raise LookupError("Incident tidak ditemukan.")
    return query_alert_lifecycle_source_row(client, result.result_rows[0][0])


def initialize(cursor, snapshot: dict) -> dict:
    incident = read_incident(cursor, snapshot["alert_key"])
    if incident:
        return incident
    snapshot = decoded(encoded(snapshot))
    incident = dict(alert_key=snapshot["alert_key"], alert_ref=snapshot["alert_display_id"],
                    revision=1, episode=1, status=snapshot["status"], snapshot=snapshot)
    save_incident(cursor, incident)
    return incident


def event(cursor, incident: dict, kind: str, payload: dict) -> str:
    event_id = str(uuid4())
    cursor.execute("INSERT INTO dq_control.incident_outbox(event_id,alert_key,revision,event_type,payload) "
                   "VALUES (%s,%s,%s,%s,%s::jsonb) ON CONFLICT(alert_key,revision,event_type) DO NOTHING",
                   (event_id, incident["alert_key"], incident["revision"], kind, encoded(payload)))
    return event_id


def _details(snapshot: dict) -> dict:
    value = snapshot.get("details_json", {})
    return decoded(value) if value else {}


def _next_version(incident: dict, status: str) -> None:
    incident["revision"] += 1
    incident["status"] = status
    snapshot = incident["snapshot"]
    snapshot["status"] = status
    snapshot["updated_at"] = max(now(), timestamp(snapshot["updated_at"]) + timedelta(milliseconds=1)).isoformat()


def publish_candidates(client, candidates: list) -> int:
    """A new failure may reopen an episode, but historical failure evidence cannot."""
    count = 0
    for candidate in candidates:
        with control_transaction() as cursor:
            incident = read_incident(cursor, candidate.alert_key)
            if incident is None:
                try:
                    previous = _source(client, candidate.alert_key)
                except LookupError:
                    previous = None
                if previous:
                    incident = initialize(cursor, previous)
            if incident:
                if incident["status"] != "resolved":
                    continue
                closure = _details(incident["snapshot"]).get("resolution", {})
                detected = candidate.details.get("source_run_at") or candidate.details.get("detected_at")
                if not detected or timestamp(detected) <= timestamp(closure.get("resolved_at", incident["snapshot"]["updated_at"])):
                    continue
                if str(candidate.source_check_run_id or "") == str(incident["snapshot"].get("source_check_run_id") or "") and candidate.alert_type != "schema_drift":
                    continue
                prior_details = _details(incident["snapshot"])
                history = prior_details.get("resolution_history", []) + [closure]
                incident["episode"] += 1
                _next_version(incident, "open")
                for column, value in zip(__import__("pipelines.common.alerts", fromlist=["ALERT_COLUMNS"]).ALERT_COLUMNS, candidate.as_insert_row()):
                    if column in {"alert_key", "alert_display_id"}:
                        continue
                    incident["snapshot"][column] = str(value) if isinstance(value, (UUID, date)) else value
                detail = dict(candidate.details, resolution_history=history)
                incident["snapshot"]["details_json"] = encoded(detail)
                incident["snapshot"]["resolved_at"] = None
            else:
                from pipelines.common.alerts import ALERT_COLUMNS
                snapshot = dict(zip(ALERT_COLUMNS, candidate.as_insert_row()))
                snapshot.update(alert_id=str(uuid4()), created_at=now().isoformat(), updated_at=now().isoformat(),
                                report_s3_uri="", acknowledged_by="", resolved_at=None)
                incident = initialize(cursor, snapshot)
            save_incident(cursor, incident)
            event(cursor, incident, "incident_detected", {"actor": "alert_producer"})
            count += 1
        synchronize_incident(incident["alert_key"], client=client, deliver=False)
    return count


def transition_triaged(client, snapshot: dict, report_uri: str) -> dict:
    with control_transaction() as cursor:
        incident = initialize(cursor, snapshot)
        # A late report remains accessible without reviving the incident.
        status = "resolved" if incident["status"] == "resolved" else "triaged"
        if incident["snapshot"].get("report_s3_uri") != report_uri or incident["status"] != status:
            _next_version(incident, status)
            incident["snapshot"]["report_s3_uri"] = report_uri
            save_incident(cursor, incident)
            event(cursor, incident, "incident_triaged", {"actor": "triage_workflow"})
    synchronize_incident(incident["alert_key"], client=client, deliver=False)
    return {"status": "success", "alert_key": incident["alert_key"], "alert_status": status,
            "report_s3_uri": report_uri, "version_written": True}


def synchronize_incident(alert_key: str, *, client=None, deliver: bool = True,
                         before_delivery: Callable[[dict, str, str, dict], None] | None = None) -> bool:
    """Replay latest state and retain failed sinks.

    The trusted Python delivery seam is internal, not an API/client field. Its
    exception is retained before transport, just like an application failure.
    """
    client = client or build_clickhouse_client()
    with control_transaction() as cursor:
        incident = read_incident(cursor, alert_key)
        if not incident:
            raise LookupError("Incident tidak ditemukan.")
        cursor.execute("SELECT event_id,event_type,payload,projection_done,audit_done,delivery_done "
                       "FROM dq_control.incident_outbox WHERE alert_key=%s AND "
                       "NOT (projection_done AND audit_done AND delivery_done) ORDER BY created_at", (alert_key,))
        events = cursor.fetchall()
        for eid, kind, payload, projected, audited, delivered in events:
            try:
                if not projected:
                    from agent.tools.alert_lifecycle import ALERT_VERSION_COLUMNS
                    snapshot = dict(incident["snapshot"])
                    for key in ("created_at", "updated_at", "resolved_at"):
                        if snapshot.get(key):
                            snapshot[key] = timestamp(snapshot[key])
                    if snapshot.get("dt"):
                        snapshot["dt"] = date.fromisoformat(str(snapshot["dt"]))
                    for key in ("alert_id", "source_check_run_id"):
                        if snapshot.get(key):
                            snapshot[key] = UUID(str(snapshot[key]))
                    client.insert("dq.alerts", [[snapshot.get(k) for k in ALERT_VERSION_COLUMNS]], column_names=ALERT_VERSION_COLUMNS)
                    cursor.execute("UPDATE dq_control.incident_outbox SET projection_done=true WHERE event_id=%s", (eid,))
                if not audited:
                    from agent.tools.audit_log import write_agent_audit_event
                    write_agent_audit_event(client=client, action=kind, status="success", agent_run_id=eid,
                                           alert_key=alert_key, actor=decoded(payload).get("actor", "system"),
                                           tool_name="incident_resolution", output_payload=decoded(payload),
                                           idempotency_key=operation_hash("audit", alert_key, str(eid)))
                    cursor.execute("UPDATE dq_control.incident_outbox SET audit_done=true WHERE event_id=%s", (eid,))
                if deliver and not delivered:
                    if before_delivery is not None:
                        before_delivery(decoded(encoded(incident)), str(eid), kind, decoded(payload))
                    if kind.startswith("incident_revalidation_completed_"):
                        _notify_request(read_request(cursor, decoded(payload)["request_id"]))
                    elif not kind.startswith("incident_revalidation_requested_"):
                        from apps.discord_bot.delivery import synchronize_incident_message
                        # Reuses persisted narrative only; no provider is called.
                        outcome = synchronize_incident_message(alert_key=alert_key, client=client)
                        if isinstance(outcome, dict) and not (outcome.get("message_id") and outcome.get("status") != "failed"):
                            raise RuntimeError("NotificationPending")
                    cursor.execute("UPDATE dq_control.incident_outbox SET delivery_done=true,error_code='' WHERE event_id=%s", (eid,))
            except Exception as exc:
                cursor.execute("UPDATE dq_control.incident_outbox SET error_code=%s WHERE event_id=%s",
                               (type(exc).__name__, eid))
                break
        cursor.execute("SELECT count(*) FROM dq_control.incident_outbox WHERE alert_key=%s "
                       "AND NOT (projection_done AND audit_done AND delivery_done)", (alert_key,))
        return cursor.fetchone()[0] == 0


def remediation_blockers(cursor, client, incident: dict, finished_at: datetime | None = None) -> list[str]:
    """Lock authoritative controls and inspect legacy backfill approvals too."""
    scope = scope_for(incident["snapshot"])
    cursor.execute("SELECT request_id,action_type,scope_json,status,execution_status,updated_at "
                   "FROM dq_control.approval_requests WHERE action_type IN ('backfill','rerun_dbt','recover_partition') FOR UPDATE")
    records = cursor.fetchall()
    blockers = []
    for rid, kind, payload, status, execution, updated in records:
        payload = decoded(payload)
        payload = {**payload.get("_approval_request", {}), **payload}
        linked = payload.get("alert_ref") == incident["alert_ref"] or payload.get("alert_key") == incident["alert_key"]
        affected_date = payload.get("dt")
        linked = linked or (affected_date and str(affected_date) == scope["dt"])
        start, end = payload.get("start_date"), payload.get("end_date")
        linked = linked or (start and end and scope["dt"] and str(start) <= scope["dt"] <= str(end))
        if not linked:
            continue
        if execution in {"dispatching", "dispatched", "unknown"} or status in {"pending", "approved"} and execution == "not_started":
            blockers.append(f"Remediation {rid} belum selesai atau belum dibatalkan.")
        elif finished_at and timestamp(updated) > finished_at and execution in {"succeeded", "failed"}:
            blockers.append("Periksa lagi setelah pekerjaan remediation selesai.")
    # Legacy history is imported transactionally; a stale CH projection cannot
    # override a committed cancellation or terminal execution in PostgreSQL.
    return sorted(set(blockers))


def _latest_request(cursor, incident: dict) -> dict | None:
    cursor.execute("SELECT record FROM dq_control.incident_revalidations WHERE alert_key=%s ORDER BY created_at DESC LIMIT 1",
                   (incident["alert_key"],))
    row = cursor.fetchone()
    if not row:
        return None
    request = decoded(row[0])
    if (request["status"] == "running" or request["status"] == "queued" and request.get("trigger_state") not in {"claimed", "unknown"}) and now() >= timestamp(request["deadline_at"]):
        request.update(status="expired", error_code="RequestDeadlineExceeded")
        save_request(cursor, request, incident["alert_key"])
    return request


def resolution_preview(reference: str) -> dict:
    from apps.common.airflow_api import configured
    client = build_clickhouse_client()
    with control_transaction() as cursor:
        incident = initialize(cursor, _source(client, reference))
        blockers = remediation_blockers(cursor, client, incident)
        if not configured():
            blockers.append("Pemeriksaan belum tersedia: koneksi Airflow belum diatur.")
        return _preview(cursor, incident, blockers, configured())


def _preview(cursor, incident, blockers, available):
    request = _latest_request(cursor, incident)
    cursor.execute("SELECT count(*) FROM dq_control.incident_outbox WHERE alert_key=%s "
                   "AND NOT (projection_done AND audit_done AND delivery_done)", (incident["alert_key"],))
    return dict(alert_ref=incident["alert_ref"], incident_revision=incident["revision"], status=incident["status"],
                scope=scope_for(incident["snapshot"]), blockers=blockers, available=available, request=request,
                resolution=_details(incident["snapshot"]).get("resolution"), synchronization_pending=cursor.fetchone()[0] > 0)


def create_revalidation(reference: str, body: RevalidationCreate) -> dict:
    from apps.common.airflow_api import AirflowRevalidationClient
    airflow = AirflowRevalidationClient()
    client = build_clickhouse_client()
    with control_transaction() as cursor:
        incident = initialize(cursor, _source(client, reference))
        digest = operation_hash("revalidate", incident["alert_ref"], body.idempotency_key)
        cursor.execute("SELECT r.record,k.actor FROM dq_control.incident_revalidation_keys k "
                       "JOIN dq_control.incident_revalidations r USING(request_id) WHERE k.idempotency_hash=%s", (digest,))
        replay = cursor.fetchone()
        request = decoded(replay[0]) if replay else _latest_request(cursor, incident)
        if replay and replay[1] != body.actor:
            raise ResolutionConflict("Idempotency key sudah digunakan operator lain.")
        if not replay:
            if incident["status"] == "resolved":
                raise ResolutionConflict("Incident sudah ditutup.")
            blockers = remediation_blockers(cursor, client, incident)
            if blockers:
                raise ResolutionConflict(" ".join(blockers))
        if not replay and not (request and request["status"] in {"queued", "running"}):
            from pipelines.dq.incident_revalidation import current_contract_hash
            created = now()
            request_id = str(uuid4())
            request = dict(request_id=request_id, alert_ref=incident["alert_ref"], alert_key=incident["alert_key"],
                           incident_revision=incident["revision"], status="queued", created_at=created.isoformat(),
                           deadline_at=(created + timedelta(minutes=15)).isoformat(), finished_at=None, expires_at=None,
                           dag_run_id=f"manual__incident_revalidation_{request_id}", scope=scope_for(incident["snapshot"]),
                           contract_hash=current_contract_hash(scope_for(incident["snapshot"])), actor=body.actor,
                           result=None, error_code="", check_claimed=False, trigger_state="pending")
            save_request(cursor, request, incident["alert_key"], digest)
            event(cursor, incident, "incident_revalidation_requested_" + request_id, {"actor": body.actor, "request_id": request_id})
        cursor.execute("INSERT INTO dq_control.incident_revalidation_keys(idempotency_hash,request_id,actor) "
                       "VALUES(%s,%s,%s) ON CONFLICT DO NOTHING", (digest, request["request_id"], body.actor))
    return dispatch_revalidation(request["request_id"], airflow)


def dispatch_revalidation(request_id: str, airflow=None) -> dict:
    """Claim the durable dispatch intent once; replay reconciles instead of POSTing."""
    from apps.common.airflow_api import AirflowRevalidationClient
    airflow = airflow or AirflowRevalidationClient()
    with control_transaction() as cursor:
        request = read_request(cursor, request_id)
        if request["status"] != "queued" or request.get("trigger_state") == "confirmed":
            return request
        previous = request.get("trigger_state", "pending")
        request["trigger_state"] = "claimed"
        save_request(cursor, request, request["alert_key"])
    try:
        trigger = airflow.trigger(request_id, reconcile_only=previous in {"claimed", "unknown"})
    except ResolutionUnavailable:
        with control_transaction() as cursor:
            request = read_request(cursor, request_id)
            if request.get("trigger_state") == "confirmed":
                return request
            if previous in {"claimed", "unknown"}:
                request.update(trigger_state="unknown", error_code="TriggerDeliveryUnknown")
            else:
                request.update(status="error", error_code="AirflowTriggerUnavailable", trigger_state="rejected")
            save_request(cursor, request, request["alert_key"])
        if previous not in {"claimed", "unknown"}:
            raise
        return request
    with control_transaction() as cursor:
        request = read_request(cursor, request_id)
        state = trigger["state"]
        if request.get("trigger_state") == "confirmed" and state in {"unknown", "not_found"}:
            return request
        if state in {"unknown", "not_found"}:
            request.update(trigger_state="unknown", error_code="TriggerDeliveryUnknown")
            if state == "not_found" and now() > timestamp(request["deadline_at"]):
                request.update(status="expired", trigger_state="absent", error_code="RequestDeadlineExceeded")
        else:
            request.update(trigger_state="confirmed", error_code="")
        save_request(cursor, request, request["alert_key"])
    return request


def get_revalidation(request_id: str) -> dict:
    request_id = str(UUID(request_id))
    with control_transaction() as cursor:
        request = read_request(cursor, request_id)
        if (request["status"] == "running" or request["status"] == "queued" and request.get("trigger_state") not in {"claimed", "unknown"}) and now() >= timestamp(request["deadline_at"]):
            request.update(status="expired", error_code="RequestDeadlineExceeded")
            save_request(cursor, request, request["alert_key"])
    if request["status"] == "queued" and request.get("trigger_state") in {"claimed", "unknown"}:
        return dispatch_revalidation(request_id)
    return request


def validate_closure(incident: dict, request: dict, expected_revision: int, current_time: datetime,
                     contract_hash: str, schema_hash: str, data_watermark: str, blockers: list[str]) -> None:
    """Pure gate, separately tested; never accepts a client PASS or stale evidence."""
    result = request.get("result") or {}
    if incident["status"] == "resolved":
        raise ResolutionConflict("Incident sudah ditutup.")
    if incident["revision"] != expected_revision or request["incident_revision"] != incident["revision"]:
        raise ResolutionConflict("Incident berubah. Periksa lagi sebelum menutup.")
    if request.get("alert_key") != incident["alert_key"] or request["scope"] != scope_for(incident["snapshot"]):
        raise ResolutionConflict("Bukti bukan untuk incident ini.")
    refs = result.get("evidence_references")
    checks = result.get("checks")
    if (request["status"] != "pass" or result.get("status") != "pass"
            or not isinstance(refs, list) or not refs or not all(isinstance(x, str) and x.strip() for x in refs)
            or not isinstance(checks, list) or not checks
            or not all(isinstance(x, dict) and x.get("status") == "pass" and x.get("table_name") == request["scope"]["table_name"] for x in checks)):
        raise ResolutionConflict("Belum bisa ditutup: pemeriksaan belum lolos.")
    if request["scope"]["kind"] == "dq_failure" and not all(x.get("dt") == request["scope"]["dt"] for x in checks):
        raise ResolutionConflict("Bukti prasyarat bukan untuk tanggal incident ini.")
    if request["scope"]["kind"] == "dq_failure" and not any(
        x.get("metric") == request["scope"]["metric"] and x.get("dimension", "") == request["scope"]["dimension"]
        and x.get("dt") == request["scope"]["dt"] for x in checks
    ):
        raise ResolutionConflict("Bukti rule atau tanggal tidak sesuai incident.")
    if not request.get("finished_at") or not request.get("expires_at") or current_time >= timestamp(request["expires_at"]):
        raise ResolutionConflict("Bukti kedaluwarsa. Periksa lagi.")
    if timestamp(request["expires_at"]) > timestamp(request["finished_at"]) + timedelta(minutes=10):
        raise ResolutionConflict("Masa berlaku bukti tidak valid.")
    if timestamp(request["finished_at"]) < timestamp(request["created_at"]) or current_time < timestamp(request["finished_at"]):
        raise ResolutionConflict("Waktu bukti tidak valid.")
    try:
        capture_start = max(timestamp(request["created_at"]), timestamp(request.get("check_started_at", request["created_at"])))
        capture_end = timestamp(request["finished_at"])
        observed = timestamp(result["observed_at"])
        evidence_times = [timestamp(x.get("run_at") or x.get("observed_at")) for x in checks]
        if (capture_end > timestamp(request.get("deadline_at", capture_start + timedelta(minutes=15)))
                or not capture_start <= observed <= capture_end
                or not all(capture_start <= x <= capture_end for x in evidence_times)):
            raise ValueError("Invalid evidence capture window")
    except (KeyError, ValueError, TypeError):
        raise ResolutionConflict("Waktu evidence tidak cocok dengan request pemeriksaan ini. Periksa lagi.") from None
    if contract_hash != request["contract_hash"] or contract_hash != result.get("contract_hash"):
        raise ResolutionConflict("Kontrak berubah. Periksa lagi.")
    if schema_hash != result.get("schema_hash") or data_watermark != result.get("data_watermark"):
        raise ResolutionConflict("Data atau schema berubah. Periksa lagi.")
    if blockers:
        raise ResolutionConflict(" ".join(blockers))


def confirm_resolution(reference: str, body: IncidentResolve, *,
                       before_delivery: Callable[[dict, str, str, dict], None] | None = None) -> dict:
    """Commit one evidence-gated decision before best-effort sink delivery."""
    client = build_clickhouse_client()
    with control_transaction() as cursor:
        incident = initialize(cursor, _source(client, reference))
        digest = operation_hash("resolve", incident["alert_ref"], body.idempotency_key)
        cursor.execute("SELECT record FROM dq_control.incident_resolution_decisions WHERE idempotency_hash=%s", (digest,))
        replay = cursor.fetchone()
        if replay:
            decision = decoded(replay[0])
            if decision["resolution_request_id"] != str(body.resolution_request_id) or decision["resolved_by"] != body.actor or decision["resolution_reason"] != body.reason:
                raise ResolutionConflict("Idempotency key sudah dipakai untuk keputusan berbeda.")
            cursor.execute("SELECT count(*) FROM dq_control.incident_outbox WHERE alert_key=%s "
                           "AND NOT (projection_done AND audit_done AND delivery_done)", (incident["alert_key"],))
            decision["synchronization_pending"] = cursor.fetchone()[0] > 0
            return decision
        request = read_request(cursor, str(body.resolution_request_id))
        latest = _latest_request(cursor, incident)
        if latest and latest["request_id"] != request["request_id"]:
            raise ResolutionConflict("Gunakan hasil pemeriksaan penutupan yang paling baru.")
        from pipelines.dq.incident_revalidation import current_contract_hash, current_schema_hash, current_data_watermark
        observed_at = (request.get("result") or {}).get("observed_at")
        observed_at = observed_at or min((x.get("run_at") or x.get("observed_at") for x in (request.get("result") or {}).get("checks", []) if x.get("run_at") or x.get("observed_at")), default=request.get("finished_at"))
        blockers = remediation_blockers(cursor, client, incident, timestamp(observed_at) if observed_at else None)
        if request["scope"]["kind"] == "dq_failure" and request.get("finished_at"):
            newer = client.query("SELECT count() FROM dq.dq_check_results WHERE table_name=" + quote_sql_literal(request["scope"]["table_name"]) +
                " AND check_name=" + quote_sql_literal(request["scope"]["metric"]) + " AND dt=toDate(" + quote_sql_literal(request["scope"]["dt"]) +
                ") AND run_at >= parseDateTime64BestEffort(" + quote_sql_literal(observed_at) + ") AND status != 'pass'").result_rows
            if newer and newer[0][0]:
                blockers.append("Ada hasil pemeriksaan baru yang belum lolos. Periksa lagi.")
        elif request["scope"]["kind"] == "schema_drift" and request.get("finished_at"):
            newer = client.query("SELECT count() FROM dq.schema_snapshots FINAL WHERE qualified_name=" +
                quote_sql_literal(request["scope"]["table_name"]) + " AND observed_at >= parseDateTime64BestEffort(" +
                quote_sql_literal(observed_at) + ") AND status != 'pass'").result_rows
            if newer and newer[0][0]:
                blockers.append("Ada pemeriksaan schema baru yang belum lolos. Periksa lagi.")
        validate_closure(incident, request, body.expected_incident_revision, now(),
                         current_contract_hash(request["scope"]), current_schema_hash(client, request["scope"]),
                         current_data_watermark(client, request["scope"]), blockers)
        _next_version(incident, "resolved")
        decision = dict(alert_ref=incident["alert_ref"], status="resolved", resolved_by=body.actor,
                        resolved_at=now().isoformat(), resolution_reason=body.reason,
                        resolution_request_id=request["request_id"], validation_dag_run_id=request["dag_run_id"],
                        evidence_references=request["result"]["evidence_references"], contract_hash=request["contract_hash"],
                        incident_revision=incident["revision"], expected=request["result"].get("expected"),
                        observed=request["result"].get("observed"), summary=request["result"]["summary"],
                        synchronization_pending=True)
        details = _details(incident["snapshot"])
        details["resolution"] = decision
        incident["snapshot"].update(details_json=encoded(details), resolved_at=decision["resolved_at"])
        save_incident(cursor, incident)
        cursor.execute("INSERT INTO dq_control.incident_resolution_decisions(idempotency_hash,alert_key,request_id,record) "
                       "VALUES(%s,%s,%s,%s::jsonb)", (digest, incident["alert_key"], request["request_id"], encoded(decision)))
        event(cursor, incident, "incident_resolved", decision | {"actor": body.actor})
    delivery_options = {"before_delivery": before_delivery} if before_delivery is not None else {}
    decision["synchronization_pending"] = not synchronize_incident(incident["alert_key"], client=client, **delivery_options)
    return decision


def run_request_stage(request_id: str, stage: str) -> dict:
    request_id = str(UUID(request_id))
    request = get_revalidation(request_id)
    if stage == "load":
        if request["status"] == "expired":
            raise ResolutionConflict("Request melewati batas waktu antre.")
        return {"request_id": request_id, "status": request["status"], "scope": request["scope"]}
    if stage == "checks":
        with control_transaction() as cursor:
            request = read_request(cursor, request_id)
            if request["status"] in {"pass", "blocked"} or request.get("result"):
                return {"request_id": request_id, "status": request["status"]}
            if request["check_claimed"] or request["status"] != "queued":
                raise ResolutionConflict("Pemeriksaan sudah dimulai; tidak diulang otomatis.")
            incident = read_incident(cursor, request["alert_key"])
            if incident["revision"] != request["incident_revision"] or now() > timestamp(request["deadline_at"]):
                request.update(status="expired", error_code="IncidentChangedOrExpired")
                save_request(cursor, request, request["alert_key"])
                return {"request_id": request_id, "status": "expired"}
            request.update(status="running", check_claimed=True, check_started_at=now().isoformat())
            save_request(cursor, request, request["alert_key"])
        try:
            from pipelines.dq.incident_revalidation import capture_revalidation
            result = RevalidationResult.model_validate(capture_revalidation(build_clickhouse_client(), request["scope"])).model_dump(mode="json")
            finished = now()
            with control_transaction() as cursor:
                request = read_request(cursor, request_id)
                request.update(result=result, finished_at=finished.isoformat(),
                               expires_at=(finished + timedelta(minutes=10)).isoformat())
                if finished > timestamp(request["deadline_at"]):
                    request.update(status="expired", error_code="RequestDeadlineExceeded")
                save_request(cursor, request, request["alert_key"])
        except Exception as exc:
            with control_transaction() as cursor:
                request = read_request(cursor, request_id)
                request.update(status="error", error_code=type(exc).__name__, finished_at=now().isoformat())
                save_request(cursor, request, request["alert_key"])
                incident = read_incident(cursor, request["alert_key"])
                event(cursor, incident, "incident_revalidation_completed_" + request_id,
                      {"actor": request["actor"], "request_id": request_id, "status": "error", "evidence_references": []})
            raise RuntimeError("Pemeriksaan mengalami error; incident tetap aktif.") from None
    elif stage == "persist":
        with control_transaction() as cursor:
            request = read_request(cursor, request_id)
            if request["status"] == "running" and request.get("result"):
                request["status"] = request["result"]["status"]
                save_request(cursor, request, request["alert_key"])
            incident = read_incident(cursor, request["alert_key"])
            event(cursor, incident, "incident_revalidation_completed_" + request_id,
                  {"actor": request["actor"], "request_id": request_id, "status": request["status"],
                   "evidence_references": (request.get("result") or {}).get("evidence_references", [])})
    elif stage == "notify":
        with control_transaction() as cursor:
            request = read_request(cursor, request_id)
            if request["status"] in {"running", "queued"} and not request.get("result"):
                request.update(status="error", error_code="WorkflowInterrupted", finished_at=now().isoformat())
                save_request(cursor, request, request["alert_key"])
            incident = read_incident(cursor, request["alert_key"])
            event(cursor, incident, "incident_revalidation_completed_" + request_id,
                  {"actor": request["actor"], "request_id": request_id, "status": request["status"],
                   "evidence_references": (request.get("result") or {}).get("evidence_references", [])})
        if not synchronize_incident(request["alert_key"]):
            return {"request_id": request_id, "status": request["status"], "notification": "pending"}
    else:
        raise ValueError("Unsupported request stage")
    return {"request_id": request_id, "status": get_revalidation(request_id)["status"]}


def _notify_request(request: dict) -> None:
    """An outbox-backed result notification; never alters the validation verdict."""
    from apps.discord_bot.delivery import post_bot_message, build_retry_session, load_delivery_settings, build_embed_message_payload
    from apps.discord_bot.resolution import render_revalidation_result
    channel = os.getenv("DISCORD_OPS_CHANNEL_ID", "")
    settings = load_delivery_settings()
    if not channel or not settings.bot_token:
        raise ResolutionUnavailable("NotificationPending")
    with build_retry_session() as session:
        post_bot_message(session, settings.bot_token, channel,
                         build_embed_message_payload(render_revalidation_result(request)), settings.timeout_seconds)


def main():
    parser = argparse.ArgumentParser(description="Run one read-only incident revalidation stage.")
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--stage", required=True, choices=("load", "checks", "persist", "notify"))
    args = parser.parse_args()
    print(encoded(run_request_stage(args.request_id, args.stage)))


if __name__ == "__main__":
    main()
