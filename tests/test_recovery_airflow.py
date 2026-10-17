"""Public Airflow API identity and exactly-once trigger behavior."""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from datetime import date
from uuid import uuid4
from unittest.mock import Mock

import pytest
import requests

from agent.recovery.contracts import RECOVERY_DAG_ID
from agent.recovery.reconciliation import inspect_dispatched_backfill
from agent.tools.approval_queue import BACKFILL_DISPATCHER_DAG_ID, normalize_backfill_parameters
from agent.tools.incident_resolution_store import ResolutionConflict, ResolutionUnavailable
from apps.common.recovery_airflow import OBSERVABLE_DAGS, RecoveryAirflowClient
from dags.dq_platform.backfill_dispatcher import ALLOWED_TARGET_DAG_IDS, build_child_run_id


def test_incident_outbox_replay_uses_existing_intents_and_second_pass_is_empty(monkeypatch):
    from agent.recovery import reconciliation
    from agent.tools import incident_resolution
    cursor = Mock()
    cursor.fetchall.side_effect = [[('owned-one',), ('owned-two',)], []]
    cursor.fetchone.return_value = (0,)
    @contextmanager
    def transaction():
        yield cursor
    sync = Mock(return_value=True)
    monkeypatch.setattr(reconciliation, 'control_transaction', transaction)
    monkeypatch.setattr(incident_resolution, 'synchronize_incident', sync)
    assert reconciliation.flush_incident_outbox(2) == 0
    assert reconciliation.flush_incident_outbox(2) == 0
    assert [call.args[0] for call in sync.call_args_list] == ['owned-one', 'owned-two']
    assert all(call.args[0].startswith('SELECT ') for call in cursor.execute.call_args_list)
    assert cursor.execute.call_args_list[0].args[1] == (2,)


@pytest.mark.parametrize('limit', [0, 51, -1, True, '10'])
def test_incident_outbox_replay_rejects_unbounded_scope_before_store(monkeypatch, limit):
    from agent.recovery import reconciliation
    transaction = Mock(side_effect=AssertionError('Invalid scope reached store'))
    monkeypatch.setattr(reconciliation, 'control_transaction', transaction)
    with pytest.raises(ValueError):
        reconciliation.flush_incident_outbox(limit)
    transaction.assert_not_called()


class Response:
    def __init__(self, status_code, payload=None):
        self.status_code, self.payload = status_code, payload or {}

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, *, gets=(), trigger_posts=()):
        self.gets = deque(gets)
        self.trigger_posts = deque(trigger_posts)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if url.endswith("/auth/token"):
            return Response(200, {"access_token": "test-access"})
        response = self.trigger_posts.popleft()
        if isinstance(response, BaseException):
            raise response
        return response

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        response = self.gets.popleft()
        if isinstance(response, BaseException):
            raise response
        return response


def client(monkeypatch, session):
    monkeypatch.setenv("AIRFLOW_API_BASE_URL", "https://airflow.example.test")
    monkeypatch.setenv("AIRFLOW_API_USERNAME", "test-user")
    monkeypatch.setenv("AIRFLOW_API_PASSWORD", "test-password")
    return RecoveryAirflowClient(session=session)


def mutation_calls(session):
    return [call for call in session.calls if call[0] == "POST" and call[1].endswith("/dagRuns")]


def test_trigger_posts_once_with_deterministic_run_and_execution_identity(monkeypatch):
    execution_id = str(uuid4())
    session = FakeSession(gets=[Response(404)], trigger_posts=[Response(201, {
        "dag_run_id": f"manual__incident_recovery_{execution_id}",
        "conf": {"execution_id": execution_id}, "state": "queued",
    })])

    result = client(monkeypatch, session).trigger(execution_id)

    assert result["conf"]["execution_id"] == execution_id
    calls = mutation_calls(session)
    assert len(calls) == 1
    assert calls[0][1].endswith(f"/dags/{RECOVERY_DAG_ID}/dagRuns")
    assert calls[0][2]["json"]["dag_run_id"] == f"manual__incident_recovery_{execution_id}"
    assert calls[0][2]["json"]["conf"] == {"execution_id": execution_id}
    assert calls[0][2]["allow_redirects"] is False


def test_ambiguous_trigger_returns_unknown_and_reconcile_never_posts_again(monkeypatch):
    execution_id = str(uuid4())
    session = FakeSession(gets=[Response(404), Response(404), Response(404)],
                          trigger_posts=[requests.Timeout("response lost")])
    airflow = client(monkeypatch, session)

    uncertain = airflow.trigger(execution_id)
    reconciled = airflow.trigger(execution_id, reconcile_only=True)

    assert uncertain["state"] == reconciled["state"] == "unknown"
    assert len(mutation_calls(session)) == 1


def test_existing_run_after_trigger_conflict_is_read_back_not_reposted(monkeypatch):
    execution_id = str(uuid4())
    existing = {"dag_run_id": f"manual__incident_recovery_{execution_id}",
                "conf": {"execution_id": execution_id}, "state": "queued"}
    session = FakeSession(gets=[Response(404), Response(200, existing)], trigger_posts=[Response(409)])

    result = client(monkeypatch, session).trigger(execution_id)

    assert result == existing
    assert len(mutation_calls(session)) == 1
    assert sum(method == "GET" for method, _, _ in session.calls) == 2


def test_existing_run_must_echo_exact_execution_identity(monkeypatch):
    execution_id = str(uuid4())
    session = FakeSession(gets=[Response(200, {"conf": {"execution_id": str(uuid4())}})])

    with pytest.raises(ResolutionConflict, match="does not match"):
        client(monkeypatch, session).trigger(execution_id)
    assert not mutation_calls(session)


@pytest.mark.parametrize("dag_id,run_id", [
    ("arbitrary_dag", "manual__safe"), (RECOVERY_DAG_ID, "run/with/path"),
    (RECOVERY_DAG_ID, "x" * 251),
])
def test_inspection_rejects_unallowlisted_or_malformed_execution_identity(monkeypatch, dag_id, run_id):
    session = FakeSession()
    with pytest.raises(ValueError, match="not allowlisted"):
        client(monkeypatch, session).inspect(dag_id, run_id)
    assert not session.calls


def test_auth_failure_and_unavailable_inspection_fail_closed(monkeypatch):
    session = FakeSession(gets=[Response(503)])
    airflow = client(monkeypatch, session)
    with pytest.raises(ResolutionUnavailable, match="could not be inspected"):
        airflow.inspect(RECOVERY_DAG_ID, "manual__safe")
    assert not mutation_calls(session)


def test_missing_airflow_credentials_reject_client_construction(monkeypatch):
    monkeypatch.setenv("AIRFLOW_API_BASE_URL", "https://airflow.example.test")
    monkeypatch.delenv("AIRFLOW_API_USERNAME", raising=False)
    monkeypatch.delenv("AIRFLOW_API_PASSWORD", raising=False)
    with pytest.raises(ResolutionUnavailable, match="not configured"):
        RecoveryAirflowClient(session=FakeSession())


def test_all_dispatcher_child_dags_are_explicitly_observable():
    assert ALLOWED_TARGET_DAG_IDS <= OBSERVABLE_DAGS


class ChildRunAirflow:
    """Read-only fake for bounded child-run inspection."""

    def __init__(self, runs):
        self.runs = runs
        self.inspected = []

    def inspect(self, dag_id, run_id):
        self.inspected.append((dag_id, run_id))
        return self.runs.get((dag_id, run_id))


def backfill_parent(*, wait=False, state="success"):
    parent_id = "manual__approved_backfill_01"
    approval_id = "APR-20261016-RECOVERY"
    target = "00_dag_dq_platform_daily_orchestrator"
    parameters = normalize_backfill_parameters({"wait_for_completion": wait, "max_dates": 3})
    scope = {"_approval_request": {
        "dispatcher_dag_id": BACKFILL_DISPATCHER_DAG_ID,
        "target_dag_id": target,
        "start_date": "2026-10-01",
        "end_date": "2026-10-03",
        "dry_run": False,
        "parameters": parameters,
    }}
    run = {
        "dag_run_id": parent_id,
        "state": state,
        "conf": {
            "approval_request_id": approval_id,
            "target_dag_id": target,
            "start_date": "2026-10-01",
            "end_date": "2026-10-03",
            "dry_run": False,
            **parameters,
        },
    }
    return parent_id, approval_id, target, scope, run


def make_child(target, dt, parent_id, approval_id, state="success"):
    run_id = build_child_run_id(target, date.fromisoformat(dt), parent_id)
    return (target, run_id), {
        "dag_run_id": run_id,
        "state": state,
        "conf": {
            "approval_request_id": approval_id,
            "target_dag_id": target,
            "dt": dt,
            "backfill_dispatcher": BACKFILL_DISPATCHER_DAG_ID,
        },
    }


@pytest.mark.parametrize(
    "child_states,expected",
    [
        (["success", "success", "success"], "succeeded"),
        (["success", "failed", "success"], "failed"),
        (["success", "running", "success"], None),
    ],
)
def test_async_backfill_inspects_every_approved_child_before_terminal_status(child_states, expected):
    parent_id, approval_id, target, scope, parent = backfill_parent()
    runs = {}
    expected_ids = []
    for dt, state in zip(("2026-10-01", "2026-10-02", "2026-10-03"), child_states, strict=True):
        identity, run = make_child(target, dt, parent_id, approval_id, state)
        runs[identity] = run
        expected_ids.append(identity)
    airflow = ChildRunAirflow(runs)

    outcome = inspect_dispatched_backfill(
        airflow=airflow, approval_id=approval_id, parent_run_id=parent_id,
        parent_run=parent, approved_scope=scope,
    )

    assert (outcome["status"] if outcome else None) == expected
    assert airflow.inspected == expected_ids
    assert outcome is None or outcome["child_runs_inspected"] == 3


def test_async_success_with_missing_child_stays_unresolved():
    parent_id, approval_id, target, scope, parent = backfill_parent()
    runs = dict(make_child(target, dt, parent_id, approval_id) for dt in ("2026-10-01", "2026-10-02"))
    airflow = ChildRunAirflow(runs)

    outcome = inspect_dispatched_backfill(
        airflow=airflow, approval_id=approval_id, parent_run_id=parent_id,
        parent_run=parent, approved_scope=scope,
    )

    assert outcome is None
    assert airflow.inspected[-1] == (
        target, build_child_run_id(target, date(2026, 10, 3), parent_id)
    )


def test_async_inspection_normalizes_string_false_like_dispatcher():
    parent_id, approval_id, target, scope, parent = backfill_parent()
    parent["conf"]["wait_for_completion"] = "false"
    runs = dict(make_child(target, dt, parent_id, approval_id) for dt in ("2026-10-01", "2026-10-02", "2026-10-03"))
    airflow = ChildRunAirflow(runs)

    outcome = inspect_dispatched_backfill(
        airflow=airflow, approval_id=approval_id, parent_run_id=parent_id,
        parent_run=parent, approved_scope=scope,
    )

    assert outcome["status"] == "succeeded"
    assert outcome["wait_for_completion"] is False
    assert len(airflow.inspected) == 3


def test_async_child_identity_mismatch_stays_unresolved():
    parent_id, approval_id, target, scope, parent = backfill_parent()
    identity, child = make_child(target, "2026-10-01", parent_id, approval_id)
    child["conf"]["approval_request_id"] = "APR-OTHER"
    airflow = ChildRunAirflow({identity: child})

    assert inspect_dispatched_backfill(
        airflow=airflow, approval_id=approval_id, parent_run_id=parent_id,
        parent_run=parent, approved_scope=scope,
    ) is None


def test_failed_parent_waits_for_any_started_children_then_remains_failed():
    parent_id, approval_id, target, scope, parent = backfill_parent(state="failed")
    first_identity, first = make_child(target, "2026-10-01", parent_id, approval_id, "success")
    second_identity, second = make_child(target, "2026-10-02", parent_id, approval_id, "running")
    airflow = ChildRunAirflow({first_identity: first, second_identity: second})

    assert inspect_dispatched_backfill(
        airflow=airflow, approval_id=approval_id, parent_run_id=parent_id,
        parent_run=parent, approved_scope=scope,
    ) is None

    second["state"] = "success"
    outcome = inspect_dispatched_backfill(
        airflow=airflow, approval_id=approval_id, parent_run_id=parent_id,
        parent_run=parent, approved_scope=scope,
    )
    assert outcome["status"] == "failed"
    assert outcome["child_runs_missing"] == 1


def test_waiting_dispatch_uses_parent_terminal_state_without_child_reads():
    parent_id, approval_id, _target, scope, parent = backfill_parent(wait=True)
    airflow = ChildRunAirflow({})

    outcome = inspect_dispatched_backfill(
        airflow=airflow, approval_id=approval_id, parent_run_id=parent_id,
        parent_run=parent, approved_scope=scope,
    )

    assert outcome["status"] == "succeeded"
    assert outcome["wait_for_completion"] is True
    assert airflow.inspected == []


def test_async_inspection_rejects_parent_scope_not_matching_approval():
    parent_id, approval_id, _target, scope, parent = backfill_parent()
    parent["conf"]["end_date"] = "2026-10-04"
    airflow = ChildRunAirflow({})

    assert inspect_dispatched_backfill(
        airflow=airflow, approval_id=approval_id, parent_run_id=parent_id,
        parent_run=parent, approved_scope=scope,
    ) is None
    assert airflow.inspected == []


def test_reconcile_finalizes_async_approval_only_after_child_runs_are_terminal(monkeypatch):
    from agent.recovery import reconciliation
    from agent.tools import approval_queue
    from agent.recovery import outbox

    parent_id, approval_id, target, scope, parent = backfill_parent()
    children = dict(make_child(target, dt, parent_id, approval_id)
                    for dt in ("2026-10-01", "2026-10-02", "2026-10-03"))
    runs = {("90_dag_dq_platform_backfill_dispatcher", parent_id): parent, **children}
    airflow = ChildRunAirflow(runs)
    updates = []
    events = []

    class Cursor:
        def __init__(self):
            self.rows = []
            self.update_result = None

        def execute(self, sql, params=()):
            if "SELECT execution_id,record FROM dq_control.recovery_executions" in sql:
                self.rows = []
            elif "SELECT request_id,action_type,execution_run_id,scope_json" in sql:
                self.rows = [(approval_id, "backfill", parent_id, scope)]
            elif "UPDATE dq_control.approval_requests SET execution_status=%s" in sql:
                updates.append(params[0])
                self.update_result = (approval_id,)

        def fetchall(self):
            return self.rows

        def fetchone(self):
            result, self.update_result = self.update_result, None
            return result

    cursor = Cursor()

    @contextmanager
    def transaction():
        yield cursor

    monkeypatch.setattr(reconciliation, "control_transaction", transaction)
    monkeypatch.setattr(reconciliation, "decoded", lambda value: value)
    monkeypatch.setattr(reconciliation, "outbox_event", lambda *args: events.append(args))
    monkeypatch.setattr(approval_queue, "flush_approval_projections", lambda **kwargs: 0)
    monkeypatch.setattr(outbox, "flush", lambda limit: {"pending": 0})

    monkeypatch.setattr(reconciliation, 'flush_incident_outbox', lambda **kwargs: 0)
    result = reconciliation.reconcile(airflow=airflow)

    assert updates == ["succeeded"]
    assert result["reconciled"] == 1 and result["unknown"] == 0
    assert len(airflow.inspected) == 4
    assert events[0][2] == "approved_execution_reconciled"
    assert events[0][3]["child_run_count"] == 3
    assert events[0][3]["business_validation"] == "not_implied"


def test_reconcile_leaves_async_approval_unresolved_while_any_child_runs(monkeypatch):
    from agent.recovery import reconciliation
    from agent.tools import approval_queue
    from agent.recovery import outbox

    parent_id, approval_id, target, scope, parent = backfill_parent()
    child_runs = [make_child(target, dt, parent_id, approval_id, state)[1]
                  for dt, state in zip(("2026-10-01", "2026-10-02", "2026-10-03"),
                                       ("success", "running", "success"), strict=True)]
    runs = {("90_dag_dq_platform_backfill_dispatcher", parent_id): parent}
    runs.update({(target, child["dag_run_id"]): child for child in child_runs})
    airflow = ChildRunAirflow(runs)
    updates = []

    class Cursor:
        def __init__(self):
            self.rows = []

        def execute(self, sql, params=()):
            if "SELECT execution_id,record FROM dq_control.recovery_executions" in sql:
                self.rows = []
            elif "SELECT request_id,action_type,execution_run_id,scope_json" in sql:
                self.rows = [(approval_id, "backfill", parent_id, scope)]
            elif "UPDATE dq_control.approval_requests SET execution_status=%s" in sql:
                updates.append(params[0])

        def fetchall(self):
            return self.rows

        def fetchone(self):
            return None

    cursor = Cursor()

    @contextmanager
    def transaction():
        yield cursor

    monkeypatch.setattr(reconciliation, "control_transaction", transaction)
    monkeypatch.setattr(reconciliation, "decoded", lambda value: value)
    monkeypatch.setattr(approval_queue, "flush_approval_projections", lambda **kwargs: 0)
    monkeypatch.setattr(outbox, "flush", lambda limit: {"pending": 0})

    monkeypatch.setattr(reconciliation, 'flush_incident_outbox', lambda **kwargs: 0)
    result = reconciliation.reconcile(airflow=airflow)

    assert updates == []
    assert result["reconciled"] == 0 and result["unknown"] == 1
    assert len(airflow.inspected) == 4
