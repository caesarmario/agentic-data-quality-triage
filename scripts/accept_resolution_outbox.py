"""Manual Airflow acceptance using one owned fixture and live platform stores.

Only prepare/restore alter the reserved fixture column. All checks use DAG 90_03;
closure uses the unchanged evidence gate. A synthetic application delivery fault
is deliberately injected before Discord; this is not an external Discord outage.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from datetime import date
from uuid import UUID

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.resolution_contracts import IncidentResolve
from agent.tools.incident_resolution import confirm_resolution, synchronize_incident, _source
from agent.tools.incident_resolution_store import control_transaction, read_incident
from agent.tools.audit_log import write_agent_audit_event, build_audit_idempotency_key
from scripts.resolution_outbox_fixture import (
    ACTOR, REASON, FixtureDeliveryFault, InjectedFixtureDeliveryFault,
    read_recovery_state, require_owned_fixture, require_fixture_incident,
)
from apps.common.airflow_api import AirflowRevalidationClient
from apps.common.control_plane import ControlPlaneClient
from apps.discord_bot.registry import load_incident_message_state
from pipelines.common.clickhouse import build_clickhouse_client, quote_sql_literal
from pipelines.common.logging import logger
from pipelines.schema_drift.config import load_named_schema_contract
from pipelines.schema_drift.generate_alerts import (
    fetch_schema_drift_evidence, build_schema_alert_candidates, insert_new_schema_alerts,
)
from pipelines.schema_drift.run_schema_drift import run_schema_drift_command
from scripts.prepare_resolution_schema_fixture import TABLE


STAGES = ("prepare", "capture", "restore", "request", "wait", "close_fault", "replay", "verify")
STATE_ROOT = PROJECT_ROOT / "data" / "acceptance" / "resolution_outbox"


def identity(run_id: str, business_date: str) -> tuple[str, date]:
    """Accept UUID and canonical date, never paths, SQL, tables, or commands."""
    identifier = str(UUID(run_id))
    day = date.fromisoformat(business_date)
    if day.isoformat() != business_date:
        raise ValueError("A canonical YYYY-MM-DD business date is required.")
    return identifier, day


def state_path(run_id: str) -> Path:
    return STATE_ROOT / (str(UUID(run_id)) + ".json")


def save(state: dict) -> None:
    """Retain only acceptance metadata in an ignored, atomic local manifest."""
    path = state_path(state["run_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, sort_keys=True, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def load(run_id: str, day: date) -> dict:
    path = state_path(run_id)
    if path.stat().st_size > 100_000:
        raise ValueError("Acceptance manifest exceeds its metadata limit.")
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("run_id") != run_id or state.get("business_date") != day.isoformat():
        raise ValueError("Acceptance identity has changed; do not replay it.")
    return state


def api_client():
    base = os.getenv("CONTROL_PLANE_API_URL", "").strip() or "http://api:8000"
    client = ControlPlaneClient(base_url=base, timeout_seconds=15)
    client.approval_headers()
    return client


def record_hash(cursor, key: str) -> str:
    """Hash all control/decision/outbox contents; never persist their raw reasons."""
    records = []
    for table, columns, ordering in (
        ("incident_controls", "alert_ref,revision,episode,status,snapshot,updated_at", "alert_key"),
        ("incident_resolution_decisions", "idempotency_hash,request_id,record,created_at", "idempotency_hash"),
        ("incident_outbox", "event_id,revision,event_type,payload,projection_done,audit_done,delivery_done,error_code,created_at", "event_id"),
    ):
        cursor.execute(f"SELECT {columns} FROM dq_control.{table} WHERE alert_key=%s ORDER BY {ordering} LIMIT 1001", (key,))
        rows = cursor.fetchall()
        if len(rows) > 1000:
            raise ValueError("Fixture preservation exceeds its evidence bound.")
        records.append(rows)
    return hashlib.sha256(json.dumps(records, sort_keys=True, default=str).encode()).hexdigest()


def preserved_decisions() -> list[dict]:
    """Capture every older fixture record within an explicit fail-closed bound."""
    with control_transaction() as cursor:
        cursor.execute("SELECT c.alert_key,c.status,count(d.idempotency_hash) "
                       "FROM dq_control.incident_controls c LEFT JOIN dq_control.incident_resolution_decisions d USING(alert_key) "
                       "WHERE c.snapshot->>'table_name'=%s GROUP BY c.alert_key,c.status ORDER BY c.alert_key LIMIT 101", (TABLE,))
        rows = cursor.fetchall()
        if len(rows) > 100:
            raise ValueError("Fixture preservation exceeds its incident bound.")
        if any(status != "resolved" for _, status, _ in rows):
            raise ValueError("Another fixture incident is active; leave its data and decisions unchanged.")
        return [{"alert_key": key, "status": status, "decision_count": count,
                 "record_hash": record_hash(cursor, key)} for key, status, count in rows]


def column_owner(run_id: str) -> str:
    return "resolution_outbox_acceptance:" + str(UUID(run_id))


def prepare_fixture(run_id: str, day: date, client) -> dict:
    require_owned_fixture(client)
    if state_path(run_id).exists():
        state = load(run_id, day)
        if not state.get("warning_created"):
            raise RuntimeError("Fixture preparation outcome is uncertain; do not repeat DDL.")
        return state
    columns = client.query("SELECT name,type FROM system.columns WHERE database='dq_resolution_acceptance' "
                           "AND table='schema_fixture' ORDER BY position").result_rows
    if columns != [("dt", "Date"), ("id", "UInt64")]:
        raise ValueError("The fixture must start clean; existing changes will not be removed.")
    api_client().health()
    state = {"run_id": run_id, "business_date": day.isoformat(),
             "schema_run_id": "resolution_outbox_" + UUID(run_id).hex,
             "preserved": preserved_decisions(), "prepare_claimed": True}
    save(state)
    client.command(f"ALTER TABLE {TABLE} ADD COLUMN acceptance_extra Nullable(String) "
                   "COMMENT " + quote_sql_literal(column_owner(run_id)))
    state["warning_created"] = True
    save(state)
    return state


def capture_alert(state: dict, client) -> None:
    if state.get("capture_complete"):
        _source(client, state["alert_key"])
        return
    if not state.get("detection_claimed"):
        state["detection_claimed"] = True
        save(state)
        payload, succeeded = run_schema_drift_command("resolution_acceptance", "detect", state["schema_run_id"], "observe")
        if not succeeded or payload["evaluation"]["status"] != "warn" or payload["evaluation"]["finding_count"] != 1:
            raise ValueError("Exactly one real fixture schema warning is required.")
    contract, _ = load_named_schema_contract("resolution_acceptance")
    evidence = fetch_schema_drift_evidence(client, state["schema_run_id"])
    candidates = build_schema_alert_candidates(contract, evidence, date.fromisoformat(state["business_date"]), state["schema_run_id"])
    if len(candidates) != 1 or candidates[0].table_name != TABLE:
        raise ValueError("The detector must produce exactly one owned fixture incident.")
    candidate = candidates[0]
    if not state.get("alert_key"):
        with control_transaction() as cursor:
            if read_incident(cursor, candidate.alert_key):
                raise ValueError("Use a new fixture business date; do not reuse a completed incident.")
        state.update(alert_key=candidate.alert_key, alert_ref=candidate.alert_display_id)
        save(state)
    elif state["alert_key"] != candidate.alert_key:
        raise ValueError("The captured incident identity changed.")
    insert_new_schema_alerts(client, [candidate])
    if not synchronize_incident(state["alert_key"], client=client):
        raise RuntimeError("Initial incident delivery is pending; stop before closure.")
    message = load_incident_message_state(client, state["alert_key"])
    if message is None or message.transport != "discord_bot_api" or message.rendered_lifecycle != "OPEN":
        raise ValueError("A bot-authored OPEN card is required before recovery acceptance.")
    state.update(message_id=message.message_id, author_id=message.author_id, capture_complete=True)
    save(state)


def restore_fixture(state: dict, client) -> None:
    require_owned_fixture(client)
    if not state.get("prepare_claimed"):
        raise ValueError("Fixture preparation is not confirmed; there is no owned cleanup intent.")
    columns = client.query("SELECT name,type FROM system.columns WHERE database='dq_resolution_acceptance' "
                           "AND table='schema_fixture' ORDER BY position").result_rows
    clean = [("dt", "Date"), ("id", "UInt64")]
    if columns != clean:
        if state.get("fixture_restored") or columns != clean + [("acceptance_extra", "Nullable(String)")]:
            raise ValueError("The fixture changed outside this preparation; do not overwrite it.")
        owner = client.query("SELECT comment FROM system.columns WHERE database='dq_resolution_acceptance' "
                             "AND table='schema_fixture' AND name='acceptance_extra'").result_rows
        if owner != [(column_owner(state["run_id"]),)]:
            raise ValueError("The warning column belongs to another run; no cleanup was performed.")
        client.command(f"ALTER TABLE {TABLE} DROP COLUMN acceptance_extra")
    state["fixture_restored"] = True
    save(state)


def fixture_request(state: dict, client) -> dict:
    request = client.get_incident_revalidation(state["request_id"])
    scope = request.get("scope") or {}
    if (request.get("alert_ref") != state["alert_ref"] or scope.get("table_name") != TABLE
            or scope.get("kind") != "schema_drift" or scope.get("dt") != state["business_date"]):
        raise ValueError("Revalidation must match the authoritative fixture incident.")
    return request


def request_check(state: dict) -> None:
    if not state.get("capture_complete") or not state.get("fixture_restored"):
        raise ValueError("Capture and restore must finish before requesting the check.")
    response = api_client().create_incident_revalidation(state["alert_ref"], actor=ACTOR,
                        idempotency_key="resolution_outbox_" + state["run_id"])
    state["request_id"] = str(UUID(response["request_id"]))
    state["validation_dag_run_id"] = response["dag_run_id"]
    save(state)


def wait_for_pass(state: dict) -> None:
    client = api_client()
    deadline = time.monotonic() + 850
    airflow = AirflowRevalidationClient()
    while time.monotonic() < deadline:
        request = fixture_request(state, client)
        dag = airflow.trigger(state["request_id"], reconcile_only=True)
        if request["status"] in {"blocked", "expired", "error"} or dag["state"] == "failed":
            raise ValueError("The fresh check did not pass; do not close the fixture incident.")
        if request["status"] == "pass" and dag["state"] == "success":
            result = request.get("result") or {}
            checks = result.get("checks") or []
            metrics = {item.get("metric") for item in checks if isinstance(item, dict)}
            if (result.get("status") != "pass" or not result.get("evidence_references")
                    or not {"table_presence", "unexpected_columns"}.issubset(metrics)
                    or any(not isinstance(item, dict) or item.get("status") != "pass"
                           or item.get("table_name") != TABLE for item in checks)):
                raise ValueError("A clean schema with zero findings is required.")
            state["pass_recorded"] = True
            save(state)
            return
        time.sleep(2)
    raise TimeoutError("The existing revalidation did not finish; no replacement run was triggered.")


def confirm_with_fault(state: dict, client) -> None:
    require_owned_fixture(client)
    request = fixture_request(state, api_client())
    if request["status"] != "pass" or not state.get("pass_recorded"):
        raise ValueError("Fresh Airflow PASS is required before delegated closure.")
    with control_transaction() as cursor:
        incident = read_incident(cursor, state["alert_key"])
        require_fixture_incident(incident or {}, state["alert_key"])
    fault = FixtureDeliveryFault(client, state["alert_key"], state["request_id"])
    decision = confirm_resolution(state["alert_ref"], IncidentResolve(
        actor=ACTOR, reason=REASON, idempotency_key="outbox_closure_" + state["run_id"],
        resolution_request_id=state["request_id"], expected_incident_revision=request["incident_revision"],
    ), before_delivery=fault)
    durable = read_recovery_state(state["alert_key"], state["request_id"])
    if (decision["synchronization_pending"] and not durable["delivery_done"]
            and not durable["error_code"] and not fault.used):
        # A worker may have stopped after commit but before entering delivery.
        # Reconcile that same event; never create a replacement closure decision.
        synchronize_incident(state["alert_key"], client=client, before_delivery=fault)
        durable = read_recovery_state(state["alert_key"], state["request_id"])
    if (not decision["synchronization_pending"] or durable["status"] != "resolved"
            or durable["decision_count"] != 1 or not durable["projection_done"] or not durable["audit_done"]
            or durable["delivery_done"] or durable["error_code"] != InjectedFixtureDeliveryFault.__name__):
        raise ValueError("The controlled application fault was not retained as one pending closure event.")
    message = load_incident_message_state(client, state["alert_key"])
    if message is None or message.message_id != state["message_id"] or message.rendered_lifecycle != "OPEN":
        raise ValueError("The first delivery must be blocked before the Discord card update.")
    state.update(fault_event_id=durable["event_id"], fault_recorded=True, closed_revision=durable["revision"])
    write_agent_audit_event(
        client=client, action="fixture_delivery_fault_observed", status="success",
        agent_run_id=durable["event_id"], alert_key=state["alert_key"], actor=ACTOR,
        tool_name="resolution_outbox_acceptance", input_payload={"request_id": state["request_id"]},
        output_payload={**durable, "message_id": state["message_id"], "external_llm_calls": 0},
        idempotency_key=build_audit_idempotency_key("resolution_outbox_fault", state["alert_key"], durable["event_id"]),
    )
    save(state)


def replay(state: dict, client) -> None:
    require_owned_fixture(client)
    if not state.get("fault_recorded"):
        raise ValueError("A recorded fault is required; do not reset completed outbox flags.")
    if not synchronize_incident(state["alert_key"], client=client):
        raise RuntimeError("Outbox replay remains pending; no new closure was attempted.")
    state["replay_completed"] = True
    save(state)


def verify(state: dict, client) -> dict:
    require_owned_fixture(client)
    durable = read_recovery_state(state["alert_key"], state["request_id"])
    message = load_incident_message_state(client, state["alert_key"])
    if (durable["status"] != "resolved" or durable["decision_count"] != 1
            or durable["event_id"] != state["fault_event_id"] or durable["revision"] != state["closed_revision"]
            or not all(durable[key] for key in ("projection_done", "audit_done", "delivery_done"))
            or durable["error_code"] or message is None or message.message_id != state["message_id"]
            or message.author_id != state["author_id"] or message.transport != "discord_bot_api"
            or message.rendered_lifecycle != "RESOLVED"):
        raise ValueError("Recovery must retain one decision, the same event and the same bot card.")
    observed_fault = client.query(
        "SELECT output_json FROM dq.agent_audit_log WHERE agent_run_id=toUUID(" +
        quote_sql_literal(str(UUID(state["fault_event_id"]))) + ") AND action='fixture_delivery_fault_observed' "
        "AND tool_name='resolution_outbox_acceptance' AND status='success' ORDER BY ts DESC LIMIT 1"
    ).result_rows
    if not observed_fault:
        raise ValueError("Durable audit proof of the controlled fault is required.")
    fault = json.loads(observed_fault[0][0])
    if (fault.get("event_id") != durable["event_id"] or fault.get("decision_count") != 1
            or fault.get("delivery_done") is not False or fault.get("message_id") != state["message_id"]
            or fault.get("error_code") != InjectedFixtureDeliveryFault.__name__):
        raise ValueError("The fault audit does not prove this event was previously pending.")
    with control_transaction() as cursor:
        for original in state["preserved"]:
            cursor.execute("SELECT c.status,count(d.idempotency_hash) FROM dq_control.incident_controls c "
                           "LEFT JOIN dq_control.incident_resolution_decisions d USING(alert_key) "
                           "WHERE c.alert_key=%s GROUP BY c.status", (original["alert_key"],))
            if cursor.fetchone() != (original["status"], original["decision_count"]):
                raise ValueError("A pre-existing fixture decision changed during acceptance.")
            if record_hash(cursor, original["alert_key"]) != original["record_hash"]:
                raise ValueError("Older fixture control, decision or outbox contents changed.")
    preview = api_client().resolution_preview(state["alert_ref"])
    if preview["status"] != "resolved" or preview["synchronization_pending"]:
        raise ValueError("The shared API must expose completed synchronization.")
    return {**durable, "status": "success", "incident_status": durable["status"],
            "alert_ref": state["alert_ref"], "request_id": state["request_id"],
            "validation_dag_run_id": state["validation_dag_run_id"], "message_id": state["message_id"],
            "bot_author_id": state["author_id"],
            "fault_type": "synthetic_application_fault_before_discord", "discord_outage_tested": False,
            "external_llm_calls": 0, "orders_mutations": 0, "previous_fixture_records_preserved": True}


def run_stage(run_id: str, business_date: str, stage: str, *, confirm_demo_closure: bool) -> dict:
    run_id, day = identity(run_id, business_date)
    if stage not in STAGES or confirm_demo_closure is not True:
        raise ValueError("An explicit delegated fixture-closure confirmation and named stage are required.")
    if os.getenv("EXTERNAL_LLM_ENABLED", "false").lower() != "false":
        raise ValueError("This acceptance must run without external LLMs.")
    client = build_clickhouse_client()
    try:
        state = prepare_fixture(run_id, day, client) if stage == "prepare" else load(run_id, day)
        if stage == "capture": capture_alert(state, client)
        elif stage == "restore": restore_fixture(state, client)
        elif stage == "request": request_check(state)
        elif stage == "wait": wait_for_pass(state)
        elif stage == "close_fault": confirm_with_fault(state, client)
        elif stage == "replay": replay(state, client)
        elif stage == "verify": return verify(state, client)
        return {"run_id": run_id, "stage": stage, "status": "success", "alert_ref": state.get("alert_ref", ""),
                "request_id": state.get("request_id", ""), "fault_recorded": state.get("fault_recorded", False),
                "external_llm_calls": 0}
    finally:
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--dt", required=True)
    parser.add_argument("--stage", required=True, choices=STAGES)
    parser.add_argument("--confirm-demo-closure", action="store_true")
    args = parser.parse_args()
    try:
        result = run_stage(args.run_id, args.dt, args.stage, confirm_demo_closure=args.confirm_demo_closure)
    except Exception as exc:
        logger.error("Resolution outbox acceptance failed safely | stage=%s error_type=%s", args.stage, type(exc).__name__)
        raise SystemExit(1) from None
    print(json.dumps(result, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
