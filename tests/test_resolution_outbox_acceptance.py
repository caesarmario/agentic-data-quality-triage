"""Developer feedback for fixture guards; not a substitute for live DAG acceptance."""
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agent.resolution_contracts import IncidentResolve
from agent.tools import incident_resolution as resolution
from scripts import resolution_outbox_fixture as policy
from scripts import accept_resolution_outbox as runner


RUN = "12345678-1234-1234-1234-123456789012"
DAY = "2026-10-13"
KEY = "resolution_acceptance|schema_drift|2026-10-13|owned-fixture"
REF = "DQ-20261013-123ABC"


@pytest.fixture(autouse=True)
def isolated_manifest(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(runner, "api_client", Mock(return_value=Mock()))


def warehouse(columns=None, marker=policy.MARKER, column_marker=None):
    client = Mock()
    def query(sql):
        if "system.tables" in sql:
            return SimpleNamespace(result_rows=[(marker,)])
        if "SELECT comment FROM system.columns" in sql:
            return SimpleNamespace(result_rows=[(column_marker or runner.column_owner(RUN),)])
        return SimpleNamespace(result_rows=columns or [("dt", "Date"), ("id", "UInt64")])
    client.query.side_effect = query
    return client


def incident(**changes):
    value = {"alert_key": KEY, "snapshot": {"table_name": policy.TABLE,
             "alert_type": "schema_drift", "metric": "schema_contract_drift"}}
    value.update(changes)
    return value


@pytest.mark.parametrize("identifier,day", [("../../private", DAY), ("", DAY), (RUN, "2026-1-13"),
    (RUN, "2026-02-31"), (RUN, "2026-10-13';DROP TABLE"), (RUN, "2026-10-13T00:00:00")])
def test_bad_identity_cannot_reach_store_or_warehouse(monkeypatch, identifier, day):
    build = Mock()
    monkeypatch.setattr(runner, "build_clickhouse_client", build)
    with pytest.raises(ValueError):
        runner.run_stage(identifier, day, "prepare", confirm_demo_closure=True)
    build.assert_not_called()


@pytest.mark.parametrize("stage,confirmation,external", [("prepare", False, "false"),
    ("arbitrary", True, "false"), ("prepare", True, "true")])
def test_default_or_llm_enabled_acceptance_is_rejected_before_any_effect(monkeypatch, stage, confirmation, external):
    monkeypatch.setenv("EXTERNAL_LLM_ENABLED", external)
    build = Mock()
    monkeypatch.setattr(runner, "build_clickhouse_client", build)
    with pytest.raises(ValueError):
        runner.run_stage(RUN, DAY, stage, confirm_demo_closure=confirmation)
    build.assert_not_called()


def test_fault_is_once_before_transport_and_bound_to_fixture_request():
    client = warehouse()
    fault = policy.FixtureDeliveryFault(client, KEY, RUN)
    eid = str(uuid4())
    payload = {"resolution_request_id": RUN, "resolved_by": policy.ACTOR, "resolution_reason": policy.REASON}
    fault(incident(), eid, "incident_detected", payload)
    assert not fault.used
    with pytest.raises(policy.InjectedFixtureDeliveryFault):
        fault(incident(), eid, "incident_resolved", payload)
    assert fault.used and fault.event_id == eid
    fault(incident(), eid, "incident_resolved", payload)
    client.command.assert_not_called()


@pytest.mark.parametrize("change", [{"resolution_request_id": str(uuid4())},
    {"resolved_by": "another_operator"}, {"resolution_reason": "another reason"}])
def test_fault_rejects_a_different_decision(change):
    fault = policy.FixtureDeliveryFault(warehouse(), KEY, RUN)
    payload = {"resolution_request_id": RUN, "resolved_by": policy.ACTOR, "resolution_reason": policy.REASON, **change}
    fault(incident(), str(uuid4()), "incident_resolved", payload)
    assert not fault.used


@pytest.mark.parametrize("change", [{"alert_key": "other"}, {"snapshot": {"table_name": "dq.fct_orders_daily"}},
    {"snapshot": {"table_name": policy.TABLE, "alert_type": "dq_failure"}}])
def test_fault_cannot_target_production_or_foreign_incident(change):
    fault = policy.FixtureDeliveryFault(warehouse(), KEY, RUN)
    fault(incident(**change), str(uuid4()), "incident_resolved",
          {"resolution_request_id": RUN, "resolved_by": policy.ACTOR, "resolution_reason": policy.REASON})
    assert not fault.used


def test_unowned_fixture_cannot_arm_fault_or_prepare(monkeypatch):
    client = warehouse(marker="another_owner")
    with pytest.raises(ValueError):
        policy.FixtureDeliveryFault(client, KEY, RUN)
    with pytest.raises(ValueError):
        runner.prepare_fixture(RUN, runner.date.fromisoformat(DAY), client)
    client.command.assert_not_called()


@pytest.mark.parametrize("columns", [[("dt", "Date"), ("id", "UInt64"), ("acceptance_extra", "Nullable(String)")],
    [("dt", "String"), ("id", "UInt64")]])
def test_preexisting_fixture_changes_are_not_overwritten(monkeypatch, columns):
    client = warehouse(columns)
    with pytest.raises(ValueError):
        runner.prepare_fixture(RUN, runner.date.fromisoformat(DAY), client)
    client.command.assert_not_called()
    assert not runner.state_path(RUN).exists()


def test_restore_refuses_unknown_preparation_and_later_foreign_change(monkeypatch):
    client = warehouse()
    with pytest.raises(ValueError, match="not confirmed"):
        runner.restore_fixture({}, client)
    with pytest.raises(ValueError, match="outside"):
        runner.restore_fixture({"prepare_claimed": True, "warning_created": True, "fixture_restored": True},
                               warehouse([("dt", "Date"), ("id", "UInt64"), ("acceptance_extra", "Nullable(String)")]))
    client.command.assert_not_called()


def test_restore_reconciles_owned_completed_ddl_without_warning_manifest():
    client = warehouse([("dt", "Date"), ("id", "UInt64"), ("acceptance_extra", "Nullable(String)")])
    state = {"run_id": RUN, "business_date": DAY, "prepare_claimed": True}
    runner.restore_fixture(state, client)
    assert state["fixture_restored"] is True
    client.command.assert_called_once_with(f"ALTER TABLE {policy.TABLE} DROP COLUMN acceptance_extra")


def test_restore_refuses_another_runs_reserved_column():
    client = warehouse([("dt", "Date"), ("id", "UInt64"), ("acceptance_extra", "Nullable(String)")], column_marker="another_run")
    with pytest.raises(ValueError, match="another run"):
        runner.restore_fixture({"run_id": RUN, "business_date": DAY, "prepare_claimed": True}, client)
    client.command.assert_not_called()


def test_manifest_is_atomic_bounded_and_not_an_arbitrary_path():
    state = {"run_id": RUN, "business_date": DAY, "request_id": str(uuid4())}
    runner.save(state)
    assert runner.load(RUN, runner.date.fromisoformat(DAY)) == state
    assert not runner.state_path(RUN).with_suffix(".tmp").exists()
    with pytest.raises(ValueError):
        runner.state_path("../secret")
    with pytest.raises(ValueError, match="identity"):
        runner.load(RUN, runner.date.fromisoformat("2026-10-14"))
    runner.state_path(RUN).write_text("x" * 100001, encoding="utf-8")
    with pytest.raises(ValueError, match="limit"):
        runner.load(RUN, runner.date.fromisoformat(DAY))


def test_api_contract_does_not_accept_python_delivery_hook():
    with pytest.raises(ValidationError):
        IncidentResolve(actor="operator", reason="Fresh check passed", idempotency_key="one",
            expected_incident_revision=1, resolution_request_id=RUN, before_delivery="inject")


def test_replay_cannot_reset_completed_outbox_or_create_another_decision(monkeypatch):
    sync = Mock(return_value=True)
    monkeypatch.setattr(runner, "synchronize_incident", sync)
    with pytest.raises(ValueError, match="recorded fault"):
        runner.replay({"alert_key": KEY}, warehouse())
    sync.assert_not_called()


def test_internal_fault_is_retained_after_projection_audit_before_discord(monkeypatch):
    client = Mock()
    cursor = Mock()
    eid = uuid4()
    cursor.fetchall.return_value = [(eid, "incident_resolved", {}, False, False, False)]
    cursor.fetchone.return_value = (1,)
    @contextmanager
    def transaction():
        yield cursor
    monkeypatch.setattr(resolution, "control_transaction", transaction)
    monkeypatch.setattr(resolution, "read_incident", lambda *args: {"snapshot": {}})
    from agent.tools import audit_log
    audit = Mock()
    monkeypatch.setattr(audit_log, "write_agent_audit_event", audit)
    def guard(*args):
        raise policy.InjectedFixtureDeliveryFault()
    assert resolution.synchronize_incident(KEY, client=client, before_delivery=guard) is False
    client.insert.assert_called_once()
    audit.assert_called_once()
    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert any("projection_done=true" in sql for sql in statements)
    assert any("audit_done=true" in sql for sql in statements)
    assert not any("delivery_done=true" in sql for sql in statements)
    assert any(call.args[1] == ("InjectedFixtureDeliveryFault", eid) for call in cursor.execute.call_args_list)
    assert not any("DELETE" in sql or "=false" in sql for sql in statements)


def test_committed_pending_closure_is_reconciled_without_second_decision(monkeypatch):
    client = warehouse()
    eid = str(uuid4())
    pending = {"status": "resolved", "revision": 2, "decision_count": 1, "event_id": eid,
               "projection_done": False, "audit_done": False, "delivery_done": False, "error_code": ""}
    faulted = {**pending, "projection_done": True, "audit_done": True,
               "error_code": policy.InjectedFixtureDeliveryFault.__name__}
    monkeypatch.setattr(runner, "fixture_request", lambda *args: {"status": "pass", "incident_revision": 1})
    @contextmanager
    def transaction():
        yield Mock()
    monkeypatch.setattr(runner, "control_transaction", transaction)
    monkeypatch.setattr(runner, "read_incident", lambda *args: incident())
    confirm = Mock(return_value={"synchronization_pending": True})
    monkeypatch.setattr(runner, "confirm_resolution", confirm)
    monkeypatch.setattr(runner, "read_recovery_state", Mock(side_effect=[pending, faulted]))
    def sync(key, **kwargs):
        with pytest.raises(policy.InjectedFixtureDeliveryFault):
            kwargs["before_delivery"](incident(), eid, "incident_resolved", {
                "resolution_request_id": RUN, "resolved_by": policy.ACTOR, "resolution_reason": policy.REASON,
            })
        return False
    synchronize = Mock(side_effect=sync)
    monkeypatch.setattr(runner, "synchronize_incident", synchronize)
    monkeypatch.setattr(runner, "load_incident_message_state", lambda *args: SimpleNamespace(message_id="1001", rendered_lifecycle="OPEN"))
    observe = Mock()
    monkeypatch.setattr(runner, "write_agent_audit_event", observe)
    state = {"run_id": RUN, "business_date": DAY, "alert_key": KEY, "alert_ref": REF,
             "request_id": RUN, "pass_recorded": True, "message_id": "1001"}
    runner.confirm_with_fault(state, client)
    confirm.assert_called_once()
    synchronize.assert_called_once()
    assert state["fault_recorded"] and state["closed_revision"] == 2
    assert observe.call_args.kwargs["output_payload"]["delivery_done"] is False
    from agent.tools.audit_log import validate_audit_idempotency_key
    validate_audit_idempotency_key(observe.call_args.kwargs["idempotency_key"])


def test_preservation_hash_detects_content_changes_even_with_identical_counts():
    def digest(decision, outbox):
        cursor = Mock()
        cursor.fetchall.side_effect = [[("ref", 2, 1, "resolved", {"status": "resolved"})], [decision], [outbox]]
        return runner.record_hash(cursor, KEY)
    baseline = digest({"reason": "Original operator reason"}, {"delivery_done": True})
    assert baseline != digest({"reason": "Changed operator reason"}, {"delivery_done": True})
    assert baseline != digest({"reason": "Original operator reason"}, {"delivery_done": False})


def test_preservation_refuses_truncated_evidence():
    cursor = Mock()
    cursor.fetchall.return_value = [()] * 1001
    with pytest.raises(ValueError, match="bound"):
        runner.record_hash(cursor, KEY)


@pytest.mark.parametrize("observed", [0, "0 schema findings"])
def test_wait_uses_structured_pass_not_human_display_string(monkeypatch, observed):
    state = {"run_id": RUN, "business_date": DAY, "request_id": RUN}
    result = {"status": "pass", "evidence_references": ["clickhouse://fixture/one"], "observed": observed,
              "checks": [{"metric": metric, "status": "pass", "table_name": policy.TABLE}
                         for metric in ["table_presence", "unexpected_columns"]]}
    monkeypatch.setattr(runner, "fixture_request", lambda *args: {"status": "pass", "result": result})
    airflow = Mock()
    airflow.trigger.return_value = {"state": "success"}
    monkeypatch.setattr(runner, "AirflowRevalidationClient", lambda: airflow)
    runner.wait_for_pass(state)
    assert state["pass_recorded"] is True
    airflow.trigger.assert_called_once_with(RUN, reconcile_only=True)


def test_new_dag_has_allowlisted_retained_log_reader():
    from scripts.read_airflow_validation_logs import airflow_log_directory
    path = airflow_log_directory("90_04_dag_dq_resolution_outbox_acceptance", "manual__safe_acceptance")
    assert path.name == "run_id=manual__safe_acceptance"
