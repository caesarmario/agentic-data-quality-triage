"""Pure/mocked review feedback only; no real store, Airflow, or provider calls."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from agent.resolution_contracts import IncidentResolve, RevalidationCreate
from agent.tools import incident_resolution as resolution
from agent.tools.incident_resolution import scope_for, timestamp, validate_closure
from agent.tools.incident_resolution_store import ResolutionConflict, ResolutionUnavailable
from apps.common.airflow_api import AirflowRevalidationClient


REQUEST_ID = "00000000-0000-0000-0000-000000000001"
CHECK_ID = "00000000-0000-0000-0000-000000000002"


@pytest.fixture(autouse=True)
def no_live_dependencies(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("Review tests must not access a live dependency")

    monkeypatch.setattr(requests.sessions.Session, "request", unexpected)
    monkeypatch.setattr(resolution, "build_clickhouse_client", unexpected)
    monkeypatch.setattr(resolution, "control_transaction", unexpected)


@pytest.mark.parametrize(
    "value",
    [
        "2026-10-11T10:00:00+07:00",
        "2026-10-10T22:00:00-05:00",
        "2026-10-11T03:00:00+00:00",
        "2026-10-11T03:00:00Z",
    ],
)
def test_timestamp_string_and_datetime_preserve_the_same_instant(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    expected = datetime(2026, 10, 11, 3, tzinfo=timezone.utc)
    assert timestamp(value) == timestamp(parsed) == expected


def _proof(finished):
    incident = {
        "alert_key": "orders|resolution-review-fixture",
        "alert_ref": "DQ-20261005-ABCDEF",
        "revision": 1,
        "episode": 1,
        "status": "triaged",
        "snapshot": {
            "alert_type": "dq_failure",
            "table_name": "dq.fct_orders_daily",
            "dt": "2026-10-05",
            "metric": "segment_coverage__country_channel",
            "dimension": "country_channel",
            "updated_at": (finished - timedelta(minutes=2)).isoformat(),
        },
    }
    observed = finished - timedelta(seconds=30)
    request = {
        "request_id": REQUEST_ID,
        "alert_ref": incident["alert_ref"],
        "alert_key": incident["alert_key"],
        "incident_revision": 1,
        "scope": scope_for(incident["snapshot"]),
        "status": "pass",
        "created_at": (finished - timedelta(minutes=1)).isoformat(),
        "deadline_at": (finished + timedelta(minutes=14)).isoformat(),
        "finished_at": finished.isoformat(),
        "expires_at": (finished + timedelta(minutes=10)).isoformat(),
        "contract_hash": "fixture-contract",
        "actor": "fixture-operator",
        "dag_run_id": f"manual__incident_revalidation_{REQUEST_ID}",
        "check_claimed": True,
        "check_started_at": observed.isoformat(),
        "trigger_state": "confirmed",
        "result": {
            "status": "pass",
            "summary": "Fixture verification passed.",
            "contract_hash": "fixture-contract",
            "schema_hash": "fixture-schema",
            "data_watermark": "fixture-watermark",
            "observed_at": observed.isoformat(),
            "evidence_references": [f"clickhouse://dq.dq_check_results/{CHECK_ID}"],
            "checks": [{
                "check_run_id": CHECK_ID,
                "run_at": observed.isoformat(),
                "table_name": incident["snapshot"]["table_name"],
                "dt": incident["snapshot"]["dt"],
                "metric": incident["snapshot"]["metric"],
                "dimension": incident["snapshot"]["dimension"],
                "status": "pass",
                "expected": 25,
                "observed": 25,
            }],
        },
    }
    return incident, request


def _validate(incident, request, current):
    return validate_closure(
        incident, request, expected_revision=1, current_time=current,
        contract_hash="fixture-contract", schema_hash="fixture-schema",
        data_watermark="fixture-watermark", blockers=[],
    )


def test_offset_cannot_extend_expired_pass_evidence():
    # 10:00 +07:00 is 03:00 UTC, not 10:00 UTC.
    finished = datetime.fromisoformat("2026-10-11T10:00:00+07:00")
    incident, request = _proof(finished)
    with pytest.raises(ResolutionConflict):
        _validate(incident, request, datetime(2026, 10, 11, 10, 5, tzinfo=timezone.utc))


def test_negative_offset_does_not_reject_fresh_evidence():
    finished = datetime.fromisoformat("2026-10-10T22:00:00-05:00")
    incident, request = _proof(finished)
    assert _validate(
        incident, request, datetime(2026, 10, 11, 3, 5, tzinfo=timezone.utc)
    ) is None


def test_expiry_cannot_exceed_ten_minutes_even_when_current_time_is_valid():
    finished = datetime(2026, 10, 11, 3, tzinfo=timezone.utc)
    incident, request = _proof(finished)
    request["expires_at"] = (finished + timedelta(minutes=20)).isoformat()
    with pytest.raises(ResolutionConflict):
        _validate(incident, request, finished + timedelta(minutes=5))


@pytest.mark.parametrize("checks", [None, [], [{}]])
def test_aggregate_pass_with_missing_checks_is_rejected(checks):
    finished = datetime(2026, 10, 11, 3, tzinfo=timezone.utc)
    incident, request = _proof(finished)
    request["result"]["checks"] = checks
    with pytest.raises(ResolutionConflict):
        _validate(incident, request, finished + timedelta(minutes=1))


@pytest.mark.parametrize("status", ["warn", "fail", "skip", "error", None])
def test_aggregate_pass_does_not_override_a_nonpassing_prerequisite(status):
    finished = datetime(2026, 10, 11, 3, tzinfo=timezone.utc)
    incident, request = _proof(finished)
    prerequisite = deepcopy(request["result"]["checks"][0])
    prerequisite.update(metric="row_count_positive", dimension="", status=status)
    request["result"]["checks"].append(prerequisite)
    with pytest.raises(ResolutionConflict):
        _validate(incident, request, finished + timedelta(minutes=1))


@pytest.mark.parametrize("field,value", [
    ("table_name", "dq.another_table"),
    ("dt", "2026-10-06"),
    ("metric", "row_count_positive"),
    ("dimension", "another_dimension"),
])
def test_pass_check_for_another_scope_cannot_close_this_incident(field, value):
    finished = datetime(2026, 10, 11, 3, tzinfo=timezone.utc)
    incident, request = _proof(finished)
    request["result"]["checks"][0][field] = value
    with pytest.raises(ResolutionConflict):
        _validate(incident, request, finished + timedelta(minutes=1))


def test_proof_is_expired_at_the_exact_expiry_instant():
    finished = datetime(2026, 10, 11, 3, tzinfo=timezone.utc)
    incident, request = _proof(finished)
    with pytest.raises(ResolutionConflict):
        _validate(incident, request, finished + timedelta(minutes=10))


@pytest.mark.parametrize("offset", [timedelta(minutes=-2), timedelta(seconds=1)])
def test_observation_must_belong_to_this_request_capture_window(offset):
    finished = datetime(2026, 10, 11, 3, tzinfo=timezone.utc)
    incident, request = _proof(finished)
    invalid_observation = (finished + offset).isoformat()
    request["result"]["observed_at"] = invalid_observation
    request["result"]["checks"][0]["run_at"] = invalid_observation
    with pytest.raises(ResolutionConflict):
        _validate(incident, request, finished + timedelta(seconds=2))


def test_fresh_aggregate_timestamp_cannot_relabel_an_old_target_check():
    finished = datetime(2026, 10, 11, 3, tzinfo=timezone.utc)
    incident, request = _proof(finished)
    request["result"]["checks"][0]["run_at"] = (finished - timedelta(days=1)).isoformat()
    with pytest.raises(ResolutionConflict):
        _validate(incident, request, finished + timedelta(seconds=1))


def test_prerequisite_pass_for_another_date_cannot_support_exact_target_pass():
    finished = datetime(2026, 10, 11, 3, tzinfo=timezone.utc)
    incident, request = _proof(finished)
    prerequisite = deepcopy(request["result"]["checks"][0])
    prerequisite.update(metric="row_count_positive", dimension="", dt="2026-10-06")
    request["result"]["checks"].append(prerequisite)
    with pytest.raises(ResolutionConflict):
        _validate(incident, request, finished + timedelta(seconds=1))


class _Cursor:
    def __init__(self):
        self.statements = []

    def execute(self, statement, args=None):
        self.statements.append((statement, args))

    def fetchone(self):
        return None


def _mock_confirmation(monkeypatch, incident, request, client, current):
    cursor = _Cursor()

    @contextmanager
    def transaction():
        yield cursor

    monkeypatch.setattr(resolution, "control_transaction", transaction)
    monkeypatch.setattr(resolution, "build_clickhouse_client", lambda: client)
    monkeypatch.setattr(resolution, "_source", lambda *args: incident["snapshot"])
    monkeypatch.setattr(resolution, "initialize", lambda *args: deepcopy(incident))
    monkeypatch.setattr(resolution, "read_request", lambda *args: deepcopy(request))
    monkeypatch.setattr(resolution, "_latest_request", lambda *args: deepcopy(request))
    monkeypatch.setattr(resolution, "now", lambda: current)
    monkeypatch.setattr(resolution, "save_incident", Mock())
    monkeypatch.setattr(resolution, "event", Mock(return_value="fixture-event"))
    monkeypatch.setattr(resolution, "synchronize_incident", Mock(return_value=True))
    import pipelines.dq.incident_revalidation as adapter
    monkeypatch.setattr(adapter, "current_contract_hash", lambda *args: "fixture-contract")
    monkeypatch.setattr(adapter, "current_schema_hash", lambda *args: "fixture-schema")
    monkeypatch.setattr(adapter, "current_data_watermark", lambda *args: "fixture-watermark")
    return cursor


@pytest.mark.parametrize("kind", ["dq_failure", "schema_drift"])
def test_new_failure_between_observation_and_finish_blocks_confirmation(monkeypatch, kind):
    finished = datetime(2026, 10, 11, 3, tzinfo=timezone.utc)
    incident, request = _proof(finished)
    incident["snapshot"]["alert_type"] = kind
    request["scope"] = scope_for(incident["snapshot"])
    observed_at = request["result"]["observed_at"]
    queries = []

    def query(sql):
        queries.append(sql)
        # A recorded failure is newer than observation but older than finish.
        return SimpleNamespace(result_rows=[[1 if observed_at in sql else 0]])

    client = SimpleNamespace(query=query)
    _mock_confirmation(monkeypatch, incident, request, client, finished + timedelta(seconds=1))
    blocker = Mock(return_value=[])
    monkeypatch.setattr(resolution, "remediation_blockers", blocker)
    body = IncidentResolve(
        actor="fixture-operator", idempotency_key="confirmation-once",
        resolution_request_id=REQUEST_ID, expected_incident_revision=1,
        reason="Fixture data has been checked.",
    )
    with pytest.raises(ResolutionConflict):
        resolution.confirm_resolution(incident["alert_ref"], body)
    assert queries and observed_at in queries[0]
    assert blocker.call_args.args[3] == timestamp(observed_at)
    resolution.save_incident.assert_not_called()
    resolution.event.assert_not_called()


class _Response:
    def __init__(self, status, body):
        self.status_code = status
        self.body = body

    def json(self):
        return self.body


class _TimeoutSession:
    def __init__(self, recovery):
        self.recovery = recovery
        self.dag_posts = []
        self.run_gets = []

    def post(self, url, **kwargs):
        assert kwargs["allow_redirects"] is False
        if url.endswith("/auth/token"):
            return _Response(200, {"access_token": "synthetic-token"})
        self.dag_posts.append(kwargs["json"])
        raise requests.Timeout()

    def get(self, url, **kwargs):
        assert kwargs["allow_redirects"] is False
        self.run_gets.append(url)
        if len(self.run_gets) == 1:
            return _Response(404, {})
        if isinstance(self.recovery, Exception):
            raise self.recovery
        return self.recovery


def _airflow_client(monkeypatch, session):
    monkeypatch.setenv("AIRFLOW_API_BASE_URL", "http://fixture.invalid")
    monkeypatch.setenv("AIRFLOW_API_USERNAME", "fixture-operator")
    monkeypatch.setenv("AIRFLOW_API_PASSWORD", "synthetic-password")
    return AirflowRevalidationClient(session=session)


@pytest.mark.parametrize("recovery", [
    requests.Timeout(), requests.ConnectionError(),
    _Response(404, {}), _Response(503, {}),
])
def test_uncertain_post_and_inconclusive_lookup_remain_unknown(monkeypatch, recovery):
    session = _TimeoutSession(recovery)
    client = _airflow_client(monkeypatch, session)
    result = client.trigger(REQUEST_ID)
    assert result["state"] == "unknown"
    assert result["dag_run_id"] == f"manual__incident_revalidation_{REQUEST_ID}"
    assert len(session.dag_posts) == 1
    assert len(session.run_gets) == 2
    assert session.dag_posts[0]["conf"] == {"resolution_request_id": REQUEST_ID}


def test_reconcile_only_does_not_post_when_run_is_not_found(monkeypatch):
    session = _TimeoutSession(_Response(404, {}))
    result = _airflow_client(monkeypatch, session).trigger(REQUEST_ID, reconcile_only=True)
    assert result["state"] == "not_found"
    assert not session.dag_posts
    assert len(session.run_gets) == 1


def _mock_request_store(monkeypatch, request):
    stored = deepcopy(request)

    @contextmanager
    def transaction():
        yield _Cursor()

    def read(*args):
        return deepcopy(stored)

    def save(cursor, value, alert_key, *args):
        assert alert_key == stored["alert_key"]
        stored.clear()
        stored.update(deepcopy(value))

    monkeypatch.setattr(resolution, "control_transaction", transaction)
    monkeypatch.setattr(resolution, "read_request", read)
    monkeypatch.setattr(resolution, "save_request", save)
    return stored


def test_dispatch_unknown_replay_reconciles_same_request_instead_of_reposting(monkeypatch):
    finished = datetime(2026, 10, 11, 3, tzinfo=timezone.utc)
    _, request = _proof(finished)
    request.update(status="queued", trigger_state="pending", result=None, check_claimed=False)
    stored = _mock_request_store(monkeypatch, request)
    trigger = Mock(side_effect=[{"state": "unknown"}, {"state": "queued"}])
    airflow = SimpleNamespace(trigger=trigger)
    first = resolution.dispatch_revalidation(REQUEST_ID, airflow)
    assert first["status"] == "queued" and first["trigger_state"] == "unknown"
    replay = resolution.dispatch_revalidation(REQUEST_ID, airflow)
    assert replay["status"] == "queued" and replay["trigger_state"] == "confirmed"
    assert [call.kwargs["reconcile_only"] for call in trigger.call_args_list] == [False, True]
    assert all(call.args == (REQUEST_ID,) for call in trigger.call_args_list)
    assert stored["request_id"] == REQUEST_ID


def test_reconciliation_failure_does_not_terminally_reject_unknown_dispatch(monkeypatch):
    finished = datetime(2026, 10, 11, 3, tzinfo=timezone.utc)
    _, request = _proof(finished)
    request.update(status="queued", trigger_state="unknown", result=None, check_claimed=False)
    stored = _mock_request_store(monkeypatch, request)
    trigger = Mock(side_effect=ResolutionUnavailable("Synthetic lookup failure"))
    replay = resolution.dispatch_revalidation(REQUEST_ID, SimpleNamespace(trigger=trigger))
    assert replay["status"] == stored["status"] == "queued"
    assert replay["trigger_state"] == "unknown"
    trigger.assert_called_once_with(REQUEST_ID, reconcile_only=True)


@pytest.mark.parametrize("trigger_state", ["claimed", "unknown"])
def test_started_check_cannot_remain_running_after_its_deadline(monkeypatch, trigger_state):
    current = datetime(2026, 10, 11, 3, tzinfo=timezone.utc)
    _, request = _proof(current)
    request.update(
        status="running", trigger_state=trigger_state, result=None,
        deadline_at=(current - timedelta(seconds=1)).isoformat(),
    )
    stored = _mock_request_store(monkeypatch, request)
    monkeypatch.setattr(resolution, "now", lambda: current)
    response = resolution.get_revalidation(REQUEST_ID)
    assert response["status"] in {"expired", "error"}
    assert stored["status"] == response["status"]


def test_late_reconciliation_failure_cannot_downgrade_confirmed_dispatch(monkeypatch):
    current = datetime(2026, 10, 11, 3, tzinfo=timezone.utc)
    _, request = _proof(current)
    request.update(status="queued", trigger_state="unknown", result=None, check_claimed=False)
    stored = _mock_request_store(monkeypatch, request)

    def trigger(*args, **kwargs):
        # Another in-flight dispatcher commits its successful acknowledgement.
        stored.update(trigger_state="confirmed", error_code="")
        raise ResolutionUnavailable("Synthetic late reconciliation failure")

    response = resolution.dispatch_revalidation(REQUEST_ID, SimpleNamespace(trigger=trigger))
    assert response["trigger_state"] == stored["trigger_state"] == "confirmed"
    assert response.get("error_code", "") == ""


@pytest.mark.parametrize("after_resolution", [False, True])
def test_existing_revalidation_key_replays_before_new_state_blockers(monkeypatch, after_resolution):
    current = datetime(2026, 10, 11, 3, tzinfo=timezone.utc)
    incident, request = _proof(current)
    incident["status"] = "resolved" if after_resolution else "triaged"
    cursor = _Cursor()
    cursor.fetchone = Mock(return_value=(deepcopy(request), "fixture-operator"))

    @contextmanager
    def transaction():
        yield cursor

    monkeypatch.setattr(resolution, "control_transaction", transaction)
    monkeypatch.setattr(resolution, "build_clickhouse_client", lambda: object())
    monkeypatch.setattr(resolution, "_source", lambda *args: incident["snapshot"])
    monkeypatch.setattr(resolution, "initialize", lambda *args: deepcopy(incident))
    blockers = Mock(return_value=["A new unrelated remediation blocks new requests."])
    monkeypatch.setattr(resolution, "remediation_blockers", blockers)
    dispatch = Mock(return_value=deepcopy(request))
    monkeypatch.setattr(resolution, "dispatch_revalidation", dispatch)
    import apps.common.airflow_api as airflow_module
    monkeypatch.setattr(airflow_module, "AirflowRevalidationClient", lambda: SimpleNamespace())
    response = resolution.create_revalidation(
        incident["alert_ref"], RevalidationCreate(actor="fixture-operator", idempotency_key="previous-key"),
    )
    assert response["request_id"] == REQUEST_ID
    blockers.assert_not_called()
