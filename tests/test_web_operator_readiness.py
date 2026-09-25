"""Offline acceptance tests for one exact synthetic approval interaction."""

# --- Importing Libraries
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import smoke_readiness, trigger_airflow_validation
from scripts.run_validation_suite import resolve_test_paths
from scripts.web_operator_readiness import (
    build_approval_audit_query,
    check_web_operator_approval,
    validate_web_operator_request_id,
)


# --- Defining Constants
REQUEST_ID = "APR-20260923-50763ABF"
COMPLETE_AUDIT = [
    ("approval_requested", "pending", 1),
    ("approval_decision", "approved", 1),
    ("approval_cancelled", "cancelled", 1),
]


# --- Defining Test Fixtures
class FakeClient:
    """Return exact latest state and grouped audit without network access."""

    def __init__(self, audit=COMPLETE_AUDIT, **overrides):
        self.audit = audit
        self.approval = SimpleNamespace(
            request_id=REQUEST_ID,
            requested_by="web-acceptance-20260925-v2",
            status="cancelled",
            execution_status="not_started",
            execution_dag_run_id="",
        )
        self.approval.__dict__.update(overrides)
        self.queries = []

    def query(self, sql):
        self.queries.append(sql)
        if "dq.agent_audit_log" in sql:
            return SimpleNamespace(result_rows=self.audit)
        return SimpleNamespace(result_rows=[self.approval])


# --- Verifying Approval Evidence
def test_complete_cancelled_synthetic_approval_passes():
    """Require complete lifecycle audit and no dispatch evidence."""
    client = FakeClient()
    result = check_web_operator_approval(REQUEST_ID, client_factory=lambda: client, approval_reader=lambda _client, _id: client.approval)
    assert result.status == "pass"
    assert result.details["request_id"] == REQUEST_ID
    assert result.details["execution_dag_run_id_empty"] is True
    assert result.details["audit_actions"] == ["approval_cancelled", "approval_decision", "approval_requested"]
    assert "JSONExtractString(input_json, 'request_id')" in client.queries[0]


@pytest.mark.parametrize("overrides,audit", [
    ({"status": "approved"}, COMPLETE_AUDIT),
    ({"execution_status": "running"}, COMPLETE_AUDIT),
    ({"execution_dag_run_id": "manual__dispatch"}, COMPLETE_AUDIT),
    ({"requested_by": "real-operator"}, COMPLETE_AUDIT),
    ({}, COMPLETE_AUDIT[:2]),
    ({}, COMPLETE_AUDIT + [("approval_execution_transition", "running", 1)]),
])
def test_incomplete_or_dispatched_approval_fails(overrides, audit):
    """A cancelled row alone cannot establish safe browser interaction."""
    client = FakeClient(audit=audit, **overrides)
    assert check_web_operator_approval(REQUEST_ID, client_factory=lambda: client, approval_reader=lambda _client, _id: client.approval).status == "fail"


def test_missing_request_fails():
    """Never treat absent fixture data as skipped acceptance."""
    assert check_web_operator_approval(REQUEST_ID, client_factory=FakeClient, approval_reader=lambda _client, _id: None).status == "fail"


@pytest.mark.parametrize("bad", ["APR-20260923-50763ABF'; DROP TABLE dq.alerts;--", "APR-20260923-50763abf", "APR-20260923-50763ABF\n", "APR-20260923-50763ABG"])
def test_injection_is_rejected_before_query_or_dag_conf(bad):
    """Keep SQL and shell interpolation restricted to canonical IDs."""
    with pytest.raises(ValueError):
        validate_web_operator_request_id(bad)
    with pytest.raises(ValueError):
        build_approval_audit_query(bad)
    with pytest.raises(ValueError):
        trigger_airflow_validation.build_trigger_command("ui", "manual__x", web_operator_request_id=bad)
    assert check_web_operator_approval(bad, client_factory=lambda: pytest.fail("must not query")).status == "fail"


def test_optional_id_is_propagated_and_implies_web_readiness():
    """CLI, JSON conf, and DAG command preserve the exact ID without raw conf rendering."""
    assert smoke_readiness.build_parser().parse_args([]).web_operator_request_id == ""
    assert smoke_readiness.build_parser().parse_args(["--web-operator-request-id", REQUEST_ID]).web_operator_request_id == REQUEST_ID
    command = trigger_airflow_validation.build_trigger_command("ui", "manual__x", web_operator_request_id=REQUEST_ID)
    assert json.loads(command[command.index("-c") + 1]) == {
        "validation_suite": "ui", "web_operator_request_id": REQUEST_ID, "require_web": True,
    }
    dag = (Path(__file__).resolve().parents[1] / "dags/91_dag_dq_platform_validation.py").read_text()
    assert '"web_operator_request_id": Param(' in dag
    assert "--web-operator-request-id '{{ params.web_operator_request_id }}'" in dag
    assert "tests/test_web_operator_readiness.py" in resolve_test_paths("ui")
