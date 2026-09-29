####
## Approval-Gated Action Executor for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping
from datetime import date
from enum import Enum
from typing import Any, Protocol

import requests
from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent.tools.approval_control_store import ApprovalControlRecord, ApprovalControlStore


# --- Defining Constants
DBT_DAG_ID = "20_dag_dq_orders_dbt_transform"
APPROVED_ACTION_DAG_ID = "90_02_dag_dq_platform_approved_actions"
AIRFLOW_CLI_CONTAINER = "dq_airflow_api_server"
GITHUB_API_ROOT = "https://api.github.com"
GITHUB_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
DISCORD_WEBHOOK_PATTERN = re.compile(
    r"^https://(?:discord(?:app)?\.com)/api/webhooks/[0-9]{10,30}/[A-Za-z0-9._-]{20,250}$"
)


# --- Defining Action Contracts
class ApprovedActionType(str, Enum):
    """Approval-gated action types executed by the bounded dispatcher."""

    RERUN_DBT = "rerun_dbt"
    CREATE_TICKET = "create_ticket"
    POST_NOTIFICATION = "post_notification"


class RerunDbtScope(BaseModel):
    """Exact, one-date dbt rerun scope."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    target_dag_id: str = Field(default=DBT_DAG_ID, pattern=f"^{DBT_DAG_ID}$")
    dt: date
    run_tests: bool = True
    full_refresh: bool = False

    @model_validator(mode="after")
    def reject_full_refresh(self) -> "RerunDbtScope":
        """Keep this action partition-bounded; full refresh needs a separate policy."""
        if self.full_refresh:
            raise ValueError("Approval-gated dbt reruns do not allow full_refresh.")
        return self


class CreateTicketScope(BaseModel):
    """Bounded GitHub issue content without credentials or repository routing."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    dt: date
    alert_ref: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=5, max_length=160)
    summary: str = Field(min_length=5, max_length=2000)
    labels: list[str] = Field(default_factory=lambda: ["data-quality"], max_length=5)


class PostNotificationScope(BaseModel):
    """Bounded Discord notification content without a caller-selected destination."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    dt: date
    alert_ref: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=5, max_length=160)
    summary: str = Field(min_length=5, max_length=1500)


class ActionExecutionResult(BaseModel):
    """Sanitized result returned to Airflow and audit callers."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    request_id: str
    action_type: str
    status: str
    execution_run_id: str
    execution_reference: str = ""
    provider: str = ""
    side_effects_executed: bool = False
    detail: str = ""


class HttpResponse(Protocol):
    """Small response contract used to keep HTTP tests deterministic."""

    status_code: int

    def json(self) -> Any: ...


class ApprovedActionDeliveryUnknown(RuntimeError):
    """An outbound request may have arrived, so automatic retry is unsafe."""


# --- Building The Transactional Store
def build_approval_control_store(database_url: str | None = None) -> ApprovalControlStore:
    """Build the PostgreSQL-backed control store without importing psycopg in tests."""
    resolved_url = (database_url or os.getenv("DQ_CONTROL_DATABASE_URL", "")).strip()
    if not resolved_url:
        raise RuntimeError("DQ_CONTROL_DATABASE_URL is required for approval-gated actions.")

    try:
        import psycopg

        return ApprovalControlStore(lambda: psycopg.connect(resolved_url))
    except ImportError:
        try:
            import psycopg2
        except ImportError as exc:  # pragma: no cover - runtime dependency check
            raise RuntimeError("psycopg or psycopg2 is required for the approval control store.") from exc

        return ApprovalControlStore(lambda: psycopg2.connect(resolved_url))


# --- Validating And Creating Requests
def normalize_action_scope(action_type: ApprovedActionType | str, scope: Mapping[str, Any]) -> dict[str, Any]:
    """Validate an immutable action scope and return JSON-safe values."""
    normalized_type = (
        action_type
        if isinstance(action_type, ApprovedActionType)
        else ApprovedActionType(str(action_type))
    )
    model_by_type: dict[ApprovedActionType, type[BaseModel]] = {
        ApprovedActionType.RERUN_DBT: RerunDbtScope,
        ApprovedActionType.CREATE_TICKET: CreateTicketScope,
        ApprovedActionType.POST_NOTIFICATION: PostNotificationScope,
    }
    validated = model_by_type[normalized_type].model_validate(dict(scope))
    return validated.model_dump(mode="json")


def create_action_request(
    *,
    action_type: ApprovedActionType | str,
    scope: Mapping[str, Any],
    requested_by: str,
    reason: str,
    store: ApprovalControlStore,
    request_generation: int = 1,
) -> tuple[ApprovalControlRecord, bool]:
    """Create or reuse one exact-scope transactional approval request."""
    normalized_type = (
        action_type.value
        if isinstance(action_type, ApprovedActionType)
        else ApprovedActionType(str(action_type)).value
    )
    normalized_scope = normalize_action_scope(normalized_type, scope)
    return store.create_request(
        action_type=normalized_type,
        scope=normalized_scope,
        requested_by=requested_by,
        reason=reason,
        request_generation=request_generation,
    )


# --- Validating Runtime Configuration
def _enabled(env: Mapping[str, str], name: str) -> bool:
    """Parse one explicit opt-in boolean from runtime configuration."""
    return str(env.get(name, "false")).strip().lower() in {"1", "true", "yes", "on"}


def _timeout_seconds(env: Mapping[str, str]) -> float:
    """Return a bounded outbound timeout without permitting hidden retries."""
    try:
        value = float(env.get("APPROVED_ACTION_HTTP_TIMEOUT_SECONDS", "10"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("APPROVED_ACTION_HTTP_TIMEOUT_SECONDS must be numeric.") from exc
    if not 1 <= value <= 30:
        raise RuntimeError("APPROVED_ACTION_HTTP_TIMEOUT_SECONDS must be between 1 and 30.")
    return value


def action_readiness(record: ApprovalControlRecord, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Validate persisted scope and report destination readiness without secrets."""
    runtime_env = os.environ if env is None else env
    action_type = ApprovedActionType(record.action_type)
    scope = normalize_action_scope(action_type, record.scope)

    if action_type == ApprovedActionType.RERUN_DBT:
        provider = "airflow"
        ready = True
        reason = "Airflow DAG 20 is the fixed dbt target."
    elif action_type == ApprovedActionType.CREATE_TICKET:
        repository = runtime_env.get("APPROVED_ACTION_GITHUB_REPOSITORY", "").strip()
        enabled = _enabled(runtime_env, "APPROVED_ACTION_GITHUB_ENABLED")
        token_present = bool(runtime_env.get("APPROVED_ACTION_GITHUB_TOKEN", "").strip())
        repository_valid = bool(GITHUB_REPOSITORY_PATTERN.fullmatch(repository))
        provider = "github_issues"
        ready = enabled and token_present and repository_valid
        reason = "GitHub issue provider is configured." if ready else "GitHub issue provider is disabled or incomplete."
    else:
        webhook = runtime_env.get("APPROVED_ACTION_DISCORD_WEBHOOK_URL", "").strip()
        enabled = _enabled(runtime_env, "APPROVED_ACTION_NOTIFICATION_ENABLED")
        provider = "discord_webhook"
        ready = enabled and bool(DISCORD_WEBHOOK_PATTERN.fullmatch(webhook))
        reason = "Discord notification provider is configured." if ready else "Discord notification provider is disabled or incomplete."

    return {
        "request_id": record.request_id,
        "action_type": action_type.value,
        "approval_status": record.status,
        "execution_status": record.execution_status,
        "provider": provider,
        "provider_ready": ready,
        "scope": scope,
        "reason": reason,
        "side_effects_executed": False,
    }


# --- Executing Approved Actions
def _run_dbt_rerun(
    record: ApprovalControlRecord,
    execution_run_id: str,
    command_runner: Callable[..., subprocess.CompletedProcess[str]],
) -> ActionExecutionResult:
    """Trigger the fixed dbt DAG once for the approved date."""
    scope = RerunDbtScope.model_validate(record.scope)
    child_run_id = f"approved_rerun_dbt__{record.request_id.lower()}"
    conf = {
        "dt": scope.dt.isoformat(),
        "run_mode": "approved_rerun",
        "run_tests": scope.run_tests,
        "full_refresh": False,
        "approval_request_id": record.request_id,
    }
    command = [
        "/usr/bin/docker",
        "exec",
        AIRFLOW_CLI_CONTAINER,
        "airflow",
        "dags",
        "trigger",
        DBT_DAG_ID,
        "--run-id",
        child_run_id,
        "--conf",
        json.dumps(conf, sort_keys=True),
    ]
    completed = command_runner(command, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError("Airflow rejected the approved dbt rerun trigger.")
    return ActionExecutionResult(
        request_id=record.request_id,
        action_type=record.action_type,
        status="dispatched",
        execution_run_id=execution_run_id,
        execution_reference=child_run_id,
        provider="airflow",
        side_effects_executed=True,
        detail="The approved dbt DagRun was created; completion remains observable in Airflow.",
    )


def _create_github_ticket(
    record: ApprovalControlRecord,
    execution_run_id: str,
    env: Mapping[str, str],
    http_post: Callable[..., HttpResponse],
) -> ActionExecutionResult:
    """Create one bounded GitHub issue using an environment-owned repository."""
    scope = CreateTicketScope.model_validate(record.scope)
    repository = env["APPROVED_ACTION_GITHUB_REPOSITORY"].strip()
    token = env["APPROVED_ACTION_GITHUB_TOKEN"].strip()
    marker = f"<!-- approval-request:{record.request_id} -->"
    body = (
        f"{scope.summary}\n\n"
        f"Alert Ref: `{scope.alert_ref}`\n"
        f"Business date: `{scope.dt.isoformat()}`\n"
        f"Approval request: `{record.request_id}`\n\n{marker}"
    )
    response = http_post(
        f"{GITHUB_API_ROOT}/repos/{repository}/issues",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={"title": scope.title, "body": body, "labels": scope.labels},
        timeout=_timeout_seconds(env),
    )
    if response.status_code >= 500:
        raise ApprovedActionDeliveryUnknown(
            f"GitHub issue creation returned HTTP {response.status_code}; delivery is uncertain."
        )
    if response.status_code != 201:
        raise RuntimeError(f"GitHub issue creation returned HTTP {response.status_code}.")
    try:
        payload = response.json()
    except ValueError as exc:
        raise ApprovedActionDeliveryUnknown(
            "GitHub accepted the request but returned an unreadable response."
        ) from exc
    issue_number = str(payload.get("number", "")) if isinstance(payload, dict) else ""
    if not issue_number.isdigit():
        raise ApprovedActionDeliveryUnknown(
            "GitHub accepted the request without a usable issue reference."
        )
    return ActionExecutionResult(
        request_id=record.request_id,
        action_type=record.action_type,
        status="succeeded",
        execution_run_id=execution_run_id,
        execution_reference=f"github_issue:{issue_number}",
        provider="github_issues",
        side_effects_executed=True,
        detail="The approved issue was created in the configured repository.",
    )


def _post_discord_notification(
    record: ApprovalControlRecord,
    execution_run_id: str,
    env: Mapping[str, str],
    http_post: Callable[..., HttpResponse],
) -> ActionExecutionResult:
    """Post one bounded Discord notification to the configured ops webhook."""
    scope = PostNotificationScope.model_validate(record.scope)
    content = (
        f"## Approved data quality notification\n"
        f"**{scope.title}**\n\n{scope.summary}\n\n"
        f"Alert Ref: `{scope.alert_ref}`\n"
        f"Date: `{scope.dt.isoformat()}`\n"
        f"Approval: `{record.request_id}`"
    )
    response = http_post(
        env["APPROVED_ACTION_DISCORD_WEBHOOK_URL"].strip(),
        json={"username": "DataSentry", "content": content[:1900]},
        timeout=_timeout_seconds(env),
    )
    if response.status_code >= 500:
        raise ApprovedActionDeliveryUnknown(
            f"Discord notification returned HTTP {response.status_code}; delivery is uncertain."
        )
    if response.status_code not in {200, 204}:
        raise RuntimeError(f"Discord notification returned HTTP {response.status_code}.")
    return ActionExecutionResult(
        request_id=record.request_id,
        action_type=record.action_type,
        status="succeeded",
        execution_run_id=execution_run_id,
        execution_reference=f"discord_notification:{record.request_id}",
        provider="discord_webhook",
        side_effects_executed=True,
        detail="The approved notification was accepted by the configured Discord webhook.",
    )


def execute_approved_action(
    *,
    request_id: str,
    execution_run_id: str,
    store: ApprovalControlStore,
    env: Mapping[str, str] | None = None,
    command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    http_post: Callable[..., HttpResponse] = requests.post,
) -> ActionExecutionResult:
    """Atomically claim and execute one approved exact-scope action."""
    runtime_env = os.environ if env is None else env
    current = store.read_latest(request_id)
    if current is None:
        raise LookupError(f"Approval request was not found: {request_id}")
    if current.status != "approved":
        raise ValueError(f"Approval request {request_id} is {current.status}; approved status is required.")
    readiness = action_readiness(current, runtime_env)
    if not readiness["provider_ready"]:
        raise RuntimeError(str(readiness["reason"]))

    claimed = store.claim(
        request_id,
        execution_run_id=execution_run_id,
        expected_version=current.version,
    )
    action_type = ApprovedActionType(claimed.action_type)

    try:
        if action_type == ApprovedActionType.RERUN_DBT:
            result = _run_dbt_rerun(claimed, execution_run_id, command_runner)
        elif action_type == ApprovedActionType.CREATE_TICKET:
            result = _create_github_ticket(claimed, execution_run_id, runtime_env, http_post)
        else:
            result = _post_discord_notification(claimed, execution_run_id, runtime_env, http_post)
    except (requests.Timeout, requests.ConnectionError, ApprovedActionDeliveryUnknown) as exc:
        store.record_execution_outcome(
            request_id,
            execution_run_id=execution_run_id,
            outcome="unknown",
            expected_version=claimed.version,
            error="Outbound delivery outcome could not be confirmed; manual reconciliation is required.",
        )
        if isinstance(exc, ApprovedActionDeliveryUnknown):
            raise
        raise ApprovedActionDeliveryUnknown(
            "Outbound delivery outcome is unknown; automatic retry is disabled."
        ) from exc
    except Exception as exc:
        store.record_execution_outcome(
            request_id,
            execution_run_id=execution_run_id,
            outcome="failed",
            expected_version=claimed.version,
            error=str(exc)[:2000],
        )
        raise

    store.record_execution_outcome(
        request_id,
        execution_run_id=execution_run_id,
        outcome=result.status,
        expected_version=claimed.version,
    )
    return result
