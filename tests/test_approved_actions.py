####
## Approval-Gated Action Executor Tests
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

from datetime import datetime, timezone
from subprocess import CompletedProcess

import pytest
import requests

from agent.tools.approval_control_store import ApprovalControlRecord
from agent.tools.approved_actions import (
    DBT_DAG_ID,
    ApprovedActionDeliveryUnknown,
    ApprovedActionType,
    action_readiness,
    create_action_request,
    execute_approved_action,
    normalize_action_scope,
)


# --- Defining Test Doubles
class StubStore:
    def __init__(self, record: ApprovalControlRecord) -> None:
        self.record = record
        self.claims: list[tuple[str, str, int]] = []
        self.outcomes: list[tuple[str, str, str, int, str]] = []
        self.created: list[dict] = []

    def create_request(self, **payload):
        self.created.append(payload)
        return self.record, True

    def read_latest(self, request_id: str):
        return self.record if request_id == self.record.request_id else None

    def claim(self, request_id: str, *, execution_run_id: str, expected_version: int):
        self.claims.append((request_id, execution_run_id, expected_version))
        self.record = _record(
            action_type=self.record.action_type,
            scope=self.record.scope,
            version=expected_version + 1,
            execution_status="dispatching",
            execution_run_id=execution_run_id,
        )
        return self.record

    def record_execution_outcome(
        self,
        request_id: str,
        *,
        execution_run_id: str,
        outcome: str,
        expected_version: int,
        error: str = "",
    ):
        self.outcomes.append((request_id, execution_run_id, outcome, expected_version, error))
        return self.record


class StubResponse:
    def __init__(self, status_code: int, payload=None) -> None:
        self.status_code = status_code
        self.payload = payload if payload is not None else {}

    def json(self):
        return self.payload


# --- Building Test Records
def _record(
    *,
    action_type: str,
    scope: dict,
    status: str = "approved",
    version: int = 2,
    execution_status: str = "not_started",
    execution_run_id: str | None = None,
) -> ApprovalControlRecord:
    now = datetime.now(timezone.utc)
    return ApprovalControlRecord(
        request_id="APR-ABCDEF0123456789ABCD",
        idempotency_hash="a" * 64,
        request_generation=1,
        action_type=action_type,
        scope=scope,
        requested_by="operator",
        reason="Restore the affected data product",
        status=status,
        version=version,
        created_at=now,
        updated_at=now,
        decided_by="reviewer",
        decided_at=now,
        decision_comment="approved for bounded execution",
        cancelled_by=None,
        cancelled_at=None,
        execution_run_id=execution_run_id,
        execution_status=execution_status,
        execution_error="",
    )


# --- Testing Scope Contracts
def test_rerun_scope_is_fixed_to_dag_20_and_rejects_full_refresh() -> None:
    scope = normalize_action_scope(
        ApprovedActionType.RERUN_DBT,
        {"dt": "2026-09-28", "run_tests": True},
    )

    assert scope == {
        "target_dag_id": DBT_DAG_ID,
        "dt": "2026-09-28",
        "run_tests": True,
        "full_refresh": False,
    }
    with pytest.raises(ValueError, match="full_refresh"):
        normalize_action_scope("rerun_dbt", {"dt": "2026-09-28", "full_refresh": True})
    with pytest.raises(ValueError):
        normalize_action_scope("rerun_dbt", {"dt": "2026-09-28", "target_dag_id": "arbitrary"})


def test_ticket_and_notification_scope_cannot_select_destination_or_credentials() -> None:
    with pytest.raises(ValueError):
        normalize_action_scope(
            "create_ticket",
            {
                "dt": "2026-09-28",
                "alert_ref": "ALT-123",
                "title": "Missing daily partition",
                "summary": "The expected partition is missing.",
                "repository": "caller/selected",
            },
        )
    with pytest.raises(ValueError):
        normalize_action_scope(
            "post_notification",
            {
                "dt": "2026-09-28",
                "alert_ref": "ALT-123",
                "title": "Missing daily partition",
                "summary": "The expected partition is missing.",
                "webhook_url": "https://example.invalid",
            },
        )


def test_create_request_persists_only_validated_scope() -> None:
    record = _record(action_type="rerun_dbt", scope={"dt": "2026-09-28"})
    store = StubStore(record)

    returned, created = create_action_request(
        action_type="rerun_dbt",
        scope={"dt": "2026-09-28"},
        requested_by="operator",
        reason="Rerun the approved business-date transform",
        store=store,
    )

    assert created is True and returned == record
    assert store.created[0]["scope"]["target_dag_id"] == DBT_DAG_ID


def test_create_request_passes_explicit_generation_to_the_store() -> None:
    record = _record(action_type="rerun_dbt", scope={"dt": "2026-09-28"})
    store = StubStore(record)

    create_action_request(
        action_type="rerun_dbt",
        scope={"dt": "2026-09-28"},
        requested_by="operator",
        reason="Repeat the approved business-date transform",
        request_generation=2,
        store=store,
    )

    assert store.created[0]["request_generation"] == 2


# --- Testing Readiness And Exact Execution
def test_disabled_outbound_provider_fails_before_atomic_claim() -> None:
    scope = {
        "dt": "2026-09-28",
        "alert_ref": "ALT-123",
        "title": "Create a data quality ticket",
        "summary": "The partition is incomplete.",
        "labels": ["data-quality"],
    }
    store = StubStore(_record(action_type="create_ticket", scope=scope))

    with pytest.raises(RuntimeError, match="disabled or incomplete"):
        execute_approved_action(
            request_id=store.record.request_id,
            execution_run_id="manual__ticket",
            store=store,
            env={"APPROVED_ACTION_GITHUB_ENABLED": "false"},
        )

    assert store.claims == []


def test_approved_dbt_rerun_uses_fixed_command_and_atomic_claim() -> None:
    scope = normalize_action_scope("rerun_dbt", {"dt": "2026-09-28"})
    store = StubStore(_record(action_type="rerun_dbt", scope=scope))
    commands: list[list[str]] = []

    def runner(command, **kwargs):
        commands.append(command)
        return CompletedProcess(command, 0, stdout="created", stderr="")

    result = execute_approved_action(
        request_id=store.record.request_id,
        execution_run_id="manual__approved_rerun",
        store=store,
        env={},
        command_runner=runner,
    )

    assert store.claims == [("APR-ABCDEF0123456789ABCD", "manual__approved_rerun", 2)]
    assert store.outcomes[0][2] == "dispatched"
    assert result.status == "dispatched" and result.provider == "airflow"
    assert commands[0][:7] == [
        "/usr/bin/docker",
        "exec",
        "dq_airflow_api_server",
        "airflow",
        "dags",
        "trigger",
        DBT_DAG_ID,
    ]
    conf = commands[0][commands[0].index("--conf") + 1]
    assert '"dt": "2026-09-28"' in conf
    assert '"full_refresh": false' in conf


def test_ticket_uses_environment_repository_and_returns_only_reference() -> None:
    scope = normalize_action_scope(
        "create_ticket",
        {
            "dt": "2026-09-28",
            "alert_ref": "ALT-123",
            "title": "Missing daily partition",
            "summary": "The expected partition is missing.",
        },
    )
    store = StubStore(_record(action_type="create_ticket", scope=scope))
    calls: list[dict] = []

    def post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return StubResponse(201, {"number": 42})

    result = execute_approved_action(
        request_id=store.record.request_id,
        execution_run_id="manual__ticket",
        store=store,
        env={
            "APPROVED_ACTION_GITHUB_ENABLED": "true",
            "APPROVED_ACTION_GITHUB_REPOSITORY": "owner/repository",
            "APPROVED_ACTION_GITHUB_TOKEN": "synthetic-test-token",
            "APPROVED_ACTION_HTTP_TIMEOUT_SECONDS": "5",
        },
        http_post=post,
    )

    assert calls[0]["url"] == "https://api.github.com/repos/owner/repository/issues"
    assert calls[0]["headers"]["Authorization"] == "Bearer synthetic-test-token"
    assert result.execution_reference == "github_issue:42"
    assert "token" not in result.model_dump_json().lower()
    assert store.outcomes[0][2] == "succeeded"


def test_notification_timeout_is_unknown_and_never_automatically_retried() -> None:
    scope = normalize_action_scope(
        "post_notification",
        {
            "dt": "2026-09-28",
            "alert_ref": "ALT-123",
            "title": "Missing daily partition",
            "summary": "The expected partition is missing.",
        },
    )
    store = StubStore(_record(action_type="post_notification", scope=scope))
    calls = 0

    def post(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise requests.Timeout("synthetic timeout")

    with pytest.raises(ApprovedActionDeliveryUnknown):
        execute_approved_action(
            request_id=store.record.request_id,
            execution_run_id="manual__notification",
            store=store,
            env={
                "APPROVED_ACTION_NOTIFICATION_ENABLED": "true",
                "APPROVED_ACTION_DISCORD_WEBHOOK_URL": (
                    "https://discord.com/api/webhooks/123456789012345678/"
                    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
                ),
            },
            http_post=post,
        )

    assert calls == 1
    assert store.outcomes[0][2] == "unknown"
    assert "manual reconciliation" in store.outcomes[0][4]


def test_readiness_never_exposes_destination_or_secret() -> None:
    scope = normalize_action_scope(
        "create_ticket",
        {
            "dt": "2026-09-28",
            "alert_ref": "ALT-123",
            "title": "Missing daily partition",
            "summary": "The expected partition is missing.",
        },
    )
    record = _record(action_type="create_ticket", scope=scope)
    result = action_readiness(
        record,
        {
            "APPROVED_ACTION_GITHUB_ENABLED": "true",
            "APPROVED_ACTION_GITHUB_REPOSITORY": "owner/repository",
            "APPROVED_ACTION_GITHUB_TOKEN": "synthetic-test-token",
        },
    )

    serialized = str(result)
    assert result["provider_ready"] is True
    assert "owner/repository" not in serialized
    assert "synthetic-test-token" not in serialized


def test_explicit_empty_environment_does_not_inherit_ambient_credentials(monkeypatch) -> None:
    scope = normalize_action_scope(
        "create_ticket",
        {
            "dt": "2026-09-28",
            "alert_ref": "ALT-123",
            "title": "Missing daily partition",
            "summary": "The expected partition is missing.",
        },
    )
    record = _record(action_type="create_ticket", scope=scope)
    monkeypatch.setenv("APPROVED_ACTION_GITHUB_ENABLED", "true")
    monkeypatch.setenv("APPROVED_ACTION_GITHUB_REPOSITORY", "owner/repository")
    monkeypatch.setenv("APPROVED_ACTION_GITHUB_TOKEN", "ambient-token")

    readiness = action_readiness(record, env={})

    assert readiness["provider_ready"] is False
    store = StubStore(record)
    with pytest.raises(RuntimeError, match="disabled or incomplete"):
        execute_approved_action(
            request_id=record.request_id,
            execution_run_id="manual__isolated",
            store=store,
            env={},
        )
    assert store.claims == []
