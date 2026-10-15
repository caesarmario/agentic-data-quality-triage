"""Real transactional locking, using new synthetic control rows only.

Skipped outside the configured local platform. No warehouse data/schema changes.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import os

import pytest

from agent.resolution_contracts import IncidentResolve, RevalidationCreate
from agent.tools import incident_resolution as service
from agent.tools.incident_resolution_store import control_transaction, read_incident, save_request, encoded, ResolutionConflict
from pipelines.dq import incident_revalidation as adapter


pytestmark = pytest.mark.skipif(not os.getenv("DQ_CONTROL_DATABASE_URL"), reason="Local control store unavailable")
CREATED_KEYS = []


@pytest.fixture(autouse=True)
def isolate_control_records():
    yield
    with control_transaction() as cursor:
        for key in CREATED_KEYS:
            assert key.startswith("resolution_fixture_")
            cursor.execute("DELETE FROM dq_control.incident_revalidation_keys WHERE request_id IN "
                           "(SELECT request_id FROM dq_control.incident_revalidations WHERE alert_key=%s)", (key,))
            for table in ("incident_outbox", "incident_resolution_decisions", "incident_revalidations", "incident_controls"):
                cursor.execute(f"DELETE FROM dq_control.{table} WHERE alert_key=%s", (key,))
        CREATED_KEYS.clear()


class Warehouse:
    def query(self, sql):
        class Result:
            result_rows = [(0,)] if "SELECT count()" in sql else []
        return Result()


def prepare(monkeypatch):
    now = datetime.now(timezone.utc)
    key = "resolution_fixture_" + uuid4().hex
    CREATED_KEYS.append(key)
    ref = "DQ-20261011-" + uuid4().hex[:6].upper()
    snapshot = dict(alert_key=key, alert_display_id=ref, alert_type="dq_failure", status="triaged",
                    created_at=now.isoformat(), updated_at=now.isoformat(), table_name="dq.raw_orders",
                    dt="2026-10-11", metric="row_count_positive", dimension="", details_json="{}")
    request_id = str(uuid4())
    request = dict(request_id=request_id, alert_ref=ref, alert_key=key, incident_revision=1, status="pass",
                   scope=service.scope_for(snapshot), created_at=now.isoformat(), finished_at=now.isoformat(),
                   expires_at=(now + timedelta(minutes=10)).isoformat(), deadline_at=(now+timedelta(minutes=15)).isoformat(),
                   dag_run_id="fixture_read_only_" + request_id, contract_hash="a", result=dict(status="pass",
                   expected=1, observed=25, summary="Pemeriksaan fixture lolos.", evidence_references=["fixture:"+request_id],
                   contract_hash="a", data_watermark="w", schema_hash="s", observed_at=now.isoformat(), checks=[dict(table_name="dq.raw_orders",
                   dt="2026-10-11", metric="row_count_positive", dimension="", status="pass", run_at=now.isoformat())]))
    with control_transaction() as cursor:
        service.initialize(cursor, snapshot)
        save_request(cursor, request, key, uuid4().hex + uuid4().hex)
    monkeypatch.setattr(service, "build_clickhouse_client", Warehouse)
    monkeypatch.setattr(service, "_source", lambda client, reference: snapshot)
    monkeypatch.setattr(service, "synchronize_incident", lambda *a, **k: True)
    monkeypatch.setattr(adapter, "current_contract_hash", lambda scope: "a")
    monkeypatch.setattr(adapter, "current_schema_hash", lambda c, scope: "s")
    monkeypatch.setattr(adapter, "current_data_watermark", lambda c, scope: "w")
    return key, ref, request_id


def test_two_operator_confirmations_have_one_durable_decision(monkeypatch):
    key, ref, request_id = prepare(monkeypatch)
    def close(actor):
        try:
            return service.confirm_resolution(ref, IncidentResolve(actor=actor, reason="Fixture pass verified.",
                idempotency_key=actor, resolution_request_id=request_id, expected_incident_revision=1))["status"]
        except ResolutionConflict:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(close, ["discord-fixture", "streamlit-fixture"]))
    assert sorted(results) == ["conflict", "resolved"]
    with control_transaction() as cursor:
        incident = read_incident(cursor, key)
        assert incident["revision"] == 2 and incident["status"] == "resolved"
        cursor.execute("SELECT count(*) FROM dq_control.incident_resolution_decisions WHERE alert_key=%s", (key,))
        assert cursor.fetchone()[0] == 1


def test_double_click_is_idempotent_and_different_payload_conflicts(monkeypatch):
    key, ref, request_id = prepare(monkeypatch)
    body = IncidentResolve(actor="fixture", reason="Fixture pass verified.", idempotency_key="same-click",
                           resolution_request_id=request_id, expected_incident_revision=1)
    first = service.confirm_resolution(ref, body)
    assert service.confirm_resolution(ref, body)["resolution_request_id"] == first["resolution_request_id"]
    with pytest.raises(ResolutionConflict):
        service.confirm_resolution(ref, body.model_copy(update={"reason": "A different reason."}))


def test_reused_active_request_retains_each_idempotency_key_after_completion(monkeypatch):
    from apps.common import airflow_api
    key, ref, request_id = prepare(monkeypatch)
    calls = []
    class Airflow:
        def trigger(self, request_id, **kwargs):
            calls.append(kwargs)
            return {"state": "queued"}
    monkeypatch.setattr(airflow_api, "AirflowRevalidationClient", Airflow)
    with control_transaction() as cursor:
        request = service.read_request(cursor, request_id)
        request.update(status="queued", trigger_state="pending")
        save_request(cursor, request, key)
    first = RevalidationCreate(actor="fixture-one", idempotency_key="first-key")
    second = RevalidationCreate(actor="fixture-two", idempotency_key="second-key")
    assert service.create_revalidation(ref, first)["request_id"] == request_id
    assert service.create_revalidation(ref, second)["request_id"] == request_id
    with control_transaction() as cursor:
        request = service.read_request(cursor, request_id)
        request["status"] = "pass"
        save_request(cursor, request, key)
    body = IncidentResolve(actor="fixture-one", reason="Fixture pass verified.", idempotency_key="close-once",
                           resolution_request_id=request_id, expected_incident_revision=1)
    service.confirm_resolution(ref, body)
    assert service.create_revalidation(ref, second)["request_id"] == request_id
    assert len(calls) == 1
    with pytest.raises(ResolutionConflict):
        service.create_revalidation(ref, second.model_copy(update={"actor": "another-user"}))


def test_uncertain_dispatch_replay_reconciles_without_reposting(monkeypatch):
    key, ref, request_id = prepare(monkeypatch)
    with control_transaction() as cursor:
        request = service.read_request(cursor, request_id)
        request.update(status="queued", trigger_state="claimed")
        save_request(cursor, request, key)
    class Airflow:
        def trigger(self, rid, **kwargs):
            assert rid == request_id and kwargs["reconcile_only"] is True
            return {"state": "not_found"}
    request = service.dispatch_revalidation(request_id, Airflow())
    assert request["status"] == "queued" and request["trigger_state"] == "unknown"


def test_old_failure_stays_closed_but_new_failure_opens_preserved_episode(monkeypatch):
    from pipelines.common.alerts import AlertCandidate
    from datetime import date
    key, ref, request_id = prepare(monkeypatch)
    service.confirm_resolution(ref, IncidentResolve(actor="fixture", reason="Fixture pass verified.", idempotency_key="close",
        resolution_request_id=request_id, expected_incident_revision=1))
    with control_transaction() as cursor:
        original = read_incident(cursor, key)
    closed_at = service.timestamp(service._details(original["snapshot"])["resolution"]["resolved_at"])
    def candidate(observed_at):
        return AlertCandidate(alert_key=key, alert_display_id="DQ-DO-NOT-CHANGE", status="open", alert_type="dq_failure",
            severity="critical", table_name="dq.raw_orders", metric="row_count_positive", dt=date(2026,10,11),
            dimension="", observed_value=0, expected_value=1, threshold_value=1, source_check_run_id=uuid4(),
            details={"source_run_at": observed_at.isoformat()})
    assert service.publish_candidates(Warehouse(), [candidate(closed_at-timedelta(seconds=1))]) == 0
    assert service.publish_candidates(Warehouse(), [candidate(closed_at+timedelta(seconds=1))]) == 1
    with control_transaction() as cursor:
        reopened = read_incident(cursor, key)
    assert reopened["status"] == "open" and reopened["episode"] == 2
    assert reopened["alert_ref"] == reopened["snapshot"]["alert_display_id"] == ref
    assert reopened["snapshot"]["created_at"] == original["snapshot"]["created_at"]
    assert len(service._details(reopened["snapshot"])["resolution_history"]) == 1


def test_late_triage_result_never_reopens_closed_control_incident(monkeypatch):
    key, ref, request_id = prepare(monkeypatch)
    service.confirm_resolution(ref, IncidentResolve(actor="fixture", reason="Fixture pass verified.", idempotency_key="close",
        resolution_request_id=request_id, expected_incident_revision=1))
    with control_transaction() as cursor:
        incident = read_incident(cursor, key)
    outcome = service.transition_triaged(Warehouse(), incident["snapshot"], "s3://fixture/late-report.md")
    assert outcome["alert_status"] == "resolved"
    with control_transaction() as cursor:
        incident = read_incident(cursor, key)
    assert incident["status"] == "resolved" and incident["snapshot"]["report_s3_uri"] == "s3://fixture/late-report.md"
