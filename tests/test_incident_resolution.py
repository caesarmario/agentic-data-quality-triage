"""Unit proof for the pure gate; operational proof belongs to Airflow."""
from datetime import datetime, timedelta, timezone
from copy import deepcopy

import pytest
from pydantic import ValidationError

from agent.resolution_contracts import IncidentResolve, RevalidationCreate
from agent.tools.incident_resolution import validate_closure, scope_for
from agent.tools.incident_resolution_store import ResolutionConflict


def fixture():
    time = datetime.now(timezone.utc)
    incident = {"alert_key": "orders|failure", "revision": 3, "status": "triaged", "snapshot": {
        "alert_type": "dq_failure", "table_name": "dq.fct_orders_daily", "dt": "2026-10-05",
        "metric": "segment_coverage__country_channel", "dimension": "country_channel"}}
    request = {"alert_key": incident["alert_key"], "incident_revision": 3, "scope": scope_for(incident["snapshot"]),
               "status": "pass", "created_at": (time - timedelta(minutes=1)).isoformat(),
               "finished_at": time.isoformat(), "expires_at": (time + timedelta(minutes=10)).isoformat(),
               "contract_hash": "a", "result": {"status": "pass", "evidence_references": ["dq:uuid"],
                "contract_hash": "a", "data_watermark": "w", "schema_hash": "s", "observed_at": time.isoformat(), "checks": [{
                    "table_name": "dq.fct_orders_daily", "metric": "segment_coverage__country_channel",
                    "dimension": "country_channel", "dt": "2026-10-05", "status": "pass", "run_at": time.isoformat()}]}}
    return incident, request, time


def call(incident, request, time, **kwargs):
    values = dict(expected_revision=3, current_time=time, contract_hash="a", schema_hash="s", data_watermark="w", blockers=[])
    values.update(kwargs)
    return validate_closure(incident, request, **values)


def test_fresh_pass_allows_only_this_incident():
    assert call(*fixture()) is None


@pytest.mark.parametrize("field,value", [("status", "blocked"), ("status", "error"), ("status", "running"),
                                        ("alert_key", "another"), ("incident_revision", 2), ("contract_hash", "changed")])
def test_invalid_request_never_resolves(field, value):
    incident, request, time = fixture()
    request[field] = value
    with pytest.raises(ResolutionConflict):
        call(incident, request, time)


@pytest.mark.parametrize("field", ["dt", "table_name", "metric", "dimension", "kind"])
def test_wrong_scope_rejected(field):
    incident, request, time = fixture()
    request["scope"][field] = "other"
    with pytest.raises(ResolutionConflict):
        call(incident, request, time)


@pytest.mark.parametrize("result", [{}, {"status": "warn"}, {"status": "pass", "evidence_references": []}])
def test_incomplete_evidence(result):
    incident, request, time = fixture()
    request["result"] = result
    with pytest.raises(ResolutionConflict):
        call(incident, request, time)


@pytest.mark.parametrize("argument,value", [("contract_hash", "new"), ("schema_hash", "new"),
                                           ("data_watermark", "new"), ("blockers", ["Remediation running"]),
                                           ("expected_revision", 2)])
def test_concurrent_changes_and_remediation_block(argument, value):
    with pytest.raises(ResolutionConflict):
        call(*fixture(), **{argument: value})


def test_expired_pass_and_already_closed_rejected():
    incident, request, time = fixture()
    with pytest.raises(ResolutionConflict):
        call(incident, request, time + timedelta(minutes=11))
    incident["status"] = "resolved"
    with pytest.raises(ResolutionConflict):
        call(incident, request, time)


@pytest.mark.parametrize("reason", ["", "   abc    ", "x" * 1001])
def test_reason_required(reason):
    with pytest.raises(ValidationError):
        IncidentResolve(actor="operator", idempotency_key="once", resolution_request_id="00000000-0000-0000-0000-000000000001",
                        expected_incident_revision=1, reason=reason)


def test_client_cannot_submit_status_or_scope():
    with pytest.raises(ValidationError):
        RevalidationCreate(actor="operator", idempotency_key="once", status="pass", scope={})


def test_schema_warn_is_not_pass():
    incident, request, time = fixture()
    incident["snapshot"]["alert_type"] = "schema_drift"
    request["scope"] = scope_for(incident["snapshot"])
    request["status"] = "blocked"
    request["result"]["status"] = "blocked"
    with pytest.raises(ResolutionConflict):
        call(incident, request, time)
