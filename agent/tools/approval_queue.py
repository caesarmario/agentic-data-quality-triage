####
## Human Approval Queue Tool for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent.tools.audit_log import write_agent_audit_event
from pipelines.common.clickhouse import build_clickhouse_client, quote_sql_literal
from pipelines.common.logging import logger


# --- Defining Constants
APPROVAL_REQUESTS_TABLE    = "dq.approval_requests"
BACKFILL_DISPATCHER_DAG_ID = "90_dag_dq_platform_backfill_dispatcher"
MAX_BACKFILL_DAYS          = 31
MAX_PARAMETERS_JSON_BYTES  = 10_000

ALLOWED_BACKFILL_TARGET_DAG_IDS = {
    "00_dag_dq_platform_daily_orchestrator",
    "10_dag_dq_orders_landing_orchestrator",
    "11_dag_dq_orders_seed_to_s3",
    "12_dag_dq_orders_load_raw_clickhouse",
    "20_dag_dq_orders_dbt_transform",
    "30_dag_dq_orders_quality_alerts",
    "40_dag_dq_orders_triage_agent",
}

SENSITIVE_PARAMETER_FRAGMENTS = {
    "api_key",
    "credential",
    "password",
    "secret",
    "token",
}

APPROVAL_REQUEST_COLUMNS = [
    "request_id",
    "created_at",
    "updated_at",
    "alert_id",
    "alert_key",
    "agent_run_id",
    "action_type",
    "risk_level",
    "status",
    "requested_by",
    "reason",
    "dispatcher_dag_id",
    "target_dag_id",
    "start_date",
    "end_date",
    "parameters_json",
    "dry_run",
    "idempotency_key",
    "decided_by",
    "decided_at",
    "decision_comment",
    "execution_dag_run_id",
    "execution_status",
    "execution_error",
]


# --- Defining Enumerations
class ApprovalRequestStatus(str, Enum):
    """
    Lifecycle states accepted by the human approval queue.

    Values:
        PENDING: Request is waiting for an explicit human decision.
        APPROVED: Request is authorized for the exact stored action scope.
        REJECTED: Request was explicitly denied.
    """

    PENDING  = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ApprovalDecision(str, Enum):
    """
    Human decisions supported by the approval API.

    Values:
        APPROVE: Authorize the exact bounded request.
        REJECT: Deny the request without executing it.
    """

    APPROVE = "approve"
    REJECT  = "reject"


class ApprovalRiskLevel(str, Enum):
    """
    Risk labels shown to operators before a decision.

    Values:
        LOW: Read-only or easily reversible action.
        MEDIUM: Bounded operational action with limited blast radius.
        HIGH: Mutation or orchestration action requiring explicit approval.
    """

    LOW    = "low"
    MEDIUM = "medium"
    HIGH   = "high"


class ApprovalExecutionStatus(str, Enum):
    """
    Operational execution states for one approved request.

    Values:
        NOT_STARTED: No dispatcher run has claimed the request.
        DISPATCHING: The approved request is claimed by one dispatcher DagRun.
        DISPATCHED: Child DagRuns were created without synchronous completion waiting.
        SUCCEEDED: All synchronously monitored child DagRuns completed successfully.
        FAILED: Dispatch or monitored child execution failed.
    """

    NOT_STARTED = "not_started"
    DISPATCHING = "dispatching"
    DISPATCHED  = "dispatched"
    SUCCEEDED   = "succeeded"
    FAILED      = "failed"


ALLOWED_EXECUTION_TRANSITIONS = {
    ApprovalExecutionStatus.NOT_STARTED.value: {
        ApprovalExecutionStatus.DISPATCHING.value,
    },
    ApprovalExecutionStatus.DISPATCHING.value: {
        ApprovalExecutionStatus.DISPATCHED.value,
        ApprovalExecutionStatus.SUCCEEDED.value,
        ApprovalExecutionStatus.FAILED.value,
    },
    ApprovalExecutionStatus.DISPATCHED.value: {
        ApprovalExecutionStatus.SUCCEEDED.value,
        ApprovalExecutionStatus.FAILED.value,
    },
    ApprovalExecutionStatus.SUCCEEDED.value: set(),
    ApprovalExecutionStatus.FAILED.value: set(),
}


# --- Defining Data Models
class BackfillExecutionParameters(BaseModel):
    """
    Canonical Airflow parameters bound to a backfill approval decision.

    Attributes:
        incident_scenario: Synthetic incident scenario passed to child DAGs.
        run_mode: Logical child run mode. Backfills must use backfill.
        run_seed: Whether child DAGs generate landing data.
        run_load: Whether child DAGs load ClickHouse raw data.
        run_dbt: Whether child DAGs run dbt transformations.
        run_dq: Whether child DAGs execute DQ checks and alerts.
        run_triage: Whether child DAGs run bounded agent triage.
        max_alerts: Maximum alerts triaged per child run.
        reset_dag_run: Whether duplicate child runs may be reset.
        wait_for_completion: Whether dispatcher waits for child completion.
        fail_fast: Whether dispatcher stops after a failed child run.
        max_dates: Dispatcher safety cap for date count.
        poll_interval_sec: Child state polling interval.
        timeout_sec: Per-child completion timeout.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    incident_scenario: str      = Field(default="baseline", min_length=1, max_length=100)
    run_mode: str               = Field(default="backfill", pattern="^backfill$")
    run_seed: bool              = True
    run_load: bool              = True
    run_dbt: bool               = True
    run_dq: bool                = True
    run_triage: bool            = False
    max_alerts: int             = Field(default=5, ge=1, le=20)
    reset_dag_run: bool         = False
    wait_for_completion: bool   = False
    fail_fast: bool             = True
    max_dates: int              = Field(default=14, ge=1, le=90)
    poll_interval_sec: int      = Field(default=15, ge=5, le=300)
    timeout_sec: int            = Field(default=3600, ge=60, le=86400)


class ApprovalRequestCreate(BaseModel):
    """
    Validated proposal used to create one durable approval request.

    Attributes:
        action_type: Bounded action type. The first implementation supports backfill only.
        alert_id: Optional source alert UUID.
        alert_key: Stable source alert key.
        agent_run_id: Optional triage run UUID that proposed the action.
        requested_by: Human or system identity creating the request.
        reason: Human-readable reason shown to the approver.
        dispatcher_dag_id: Airflow dispatcher allowed to execute the action.
        target_dag_id: Operational DAG triggered once per business date.
        start_date: Inclusive backfill start date.
        end_date: Inclusive backfill end date.
        parameters: Non-sensitive pass-through parameters bound to the approval.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    action_type: str                   = "backfill"
    alert_id: UUID | None              = None
    alert_key: str                     = ""
    agent_run_id: UUID | None          = None
    requested_by: str                  = Field(min_length=1, max_length=200)
    reason: str                        = Field(min_length=5, max_length=2000)
    dispatcher_dag_id: str             = BACKFILL_DISPATCHER_DAG_ID
    target_dag_id: str
    start_date: date
    end_date: date
    parameters: dict[str, Any]         = Field(default_factory=dict)

    @field_validator("parameters")
    @classmethod
    def reject_sensitive_parameters(cls, value: dict[str, Any]) -> dict[str, Any]:
        """
        Reject credentials and oversized payloads from durable approval metadata.

        Args:
            value: Candidate action parameters.

        Returns:
            Original parameters when they are safe to persist.

        Raises:
            ValueError: If a sensitive key or oversized JSON payload is detected.
        """
        sensitive_paths = find_sensitive_parameter_paths(value)

        if sensitive_paths:
            raise ValueError(f"Sensitive approval parameters are not allowed: {', '.join(sensitive_paths)}")

        serialized = canonical_json(value)

        if len(serialized.encode("utf-8")) > MAX_PARAMETERS_JSON_BYTES:
            raise ValueError(f"Approval parameters exceed {MAX_PARAMETERS_JSON_BYTES} bytes.")

        return value

    @model_validator(mode="after")
    def validate_backfill_scope(self) -> "ApprovalRequestCreate":
        """
        Enforce the bounded first-version backfill approval contract.

        Returns:
            Current request when action, target, and date scope are safe.

        Raises:
            ValueError: If the action is unsupported or exceeds policy boundaries.
        """
        if self.action_type != "backfill":
            raise ValueError("Only backfill approval requests are supported in the current implementation.")

        if self.dispatcher_dag_id != BACKFILL_DISPATCHER_DAG_ID:
            raise ValueError(f"dispatcher_dag_id must be {BACKFILL_DISPATCHER_DAG_ID}.")

        if self.target_dag_id not in ALLOWED_BACKFILL_TARGET_DAG_IDS:
            raise ValueError(f"Backfill target DAG is not allowed: {self.target_dag_id}")

        if self.end_date < self.start_date:
            raise ValueError("end_date must be greater than or equal to start_date.")

        date_count = (self.end_date - self.start_date).days + 1

        if date_count > MAX_BACKFILL_DAYS:
            raise ValueError(f"Backfill approval range exceeds {MAX_BACKFILL_DAYS} dates.")

        # Persist a complete parameter contract so execution flags cannot change after approval.
        self.parameters = normalize_backfill_parameters(self.parameters)

        return self


class ApprovalRequest(BaseModel):
    """
    Latest durable state for one human approval request.

    Attributes:
        request_id: Human-facing deterministic approval reference.
        created_at: UTC timestamp when the request was first created.
        updated_at: UTC timestamp for the latest lifecycle version.
        alert_id: Optional source alert UUID.
        alert_key: Stable source alert key.
        agent_run_id: Optional source triage run UUID.
        action_type: Bounded action type.
        risk_level: Operator-facing action risk.
        status: Current approval lifecycle status.
        requested_by: Requesting human or system identity.
        reason: Human-readable action reason.
        dispatcher_dag_id: Airflow dispatcher allowed to execute the action.
        target_dag_id: Operational DAG bound to the approval.
        start_date: Inclusive backfill start date.
        end_date: Inclusive backfill end date.
        parameters: Bounded non-sensitive action parameters.
        dry_run: Whether the approved action is preview-only.
        idempotency_key: Stable hash used to collapse duplicate requests.
        decided_by: Human identity that made the terminal decision.
        decided_at: UTC decision timestamp.
        decision_comment: Optional decision explanation.
        execution_dag_run_id: Dispatcher DagRun correlation identifier.
        execution_status: Current single-use execution lifecycle state.
        execution_error: Bounded execution failure detail.
    """

    model_config = ConfigDict(use_enum_values=True)

    request_id: str
    created_at: datetime
    updated_at: datetime
    alert_id: UUID | None              = None
    alert_key: str                     = ""
    agent_run_id: UUID | None          = None
    action_type: str
    risk_level: ApprovalRiskLevel | str
    status: ApprovalRequestStatus | str
    requested_by: str
    reason: str
    dispatcher_dag_id: str
    target_dag_id: str
    start_date: date | None            = None
    end_date: date | None              = None
    parameters: dict[str, Any]         = Field(default_factory=dict)
    dry_run: bool                      = False
    idempotency_key: str
    decided_by: str                    = ""
    decided_at: datetime | None        = None
    decision_comment: str              = ""
    execution_dag_run_id: str          = ""
    execution_status: ApprovalExecutionStatus | str = ApprovalExecutionStatus.NOT_STARTED
    execution_error: str               = ""


# --- Defining Serialization Helpers
def canonical_json(payload: Any) -> str:
    """
    Serialize a JSON-like payload deterministically for hashing and persistence.

    Args:
        payload: JSON-like object to serialize.

    Returns:
        Stable compact JSON string with sorted keys.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def enum_value(value: Enum | str) -> str:
    """
    Return the serialized value for a string enum or plain string.

    Args:
        value: Enum member or already-normalized string.

    Returns:
        String value suitable for persistence and comparison.
    """
    return str(value.value) if isinstance(value, Enum) else str(value)


def normalize_backfill_parameters(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Build the complete validated parameter contract for one backfill action.

    Args:
        payload: Partial or complete caller-supplied backfill parameters.

    Returns:
        Canonical dictionary containing every approved execution parameter.

    Raises:
        pydantic.ValidationError: If unknown or invalid parameters are supplied.
    """
    return BackfillExecutionParameters.model_validate(payload or {}).model_dump(mode="json")


def backfill_parameters_from_conf(conf: dict[str, Any]) -> dict[str, Any]:
    """
    Extract and normalize approval-bound parameters from Airflow dag_run.conf.

    Args:
        conf: Raw Airflow dispatcher configuration.

    Returns:
        Complete canonical parameter dictionary with policy defaults.
    """
    allowed_keys = set(BackfillExecutionParameters.model_fields)
    payload      = {key: conf[key] for key in allowed_keys if key in conf}

    return normalize_backfill_parameters(payload)


def find_sensitive_parameter_paths(payload: Any, prefix: str = "") -> list[str]:
    """
    Recursively find parameter keys that look like credentials or secrets.

    Args:
        payload: JSON-like parameter payload.
        prefix: Parent key path used during recursion.

    Returns:
        Sorted list of sensitive key paths.
    """
    paths: list[str] = []

    if isinstance(payload, dict):
        for key, value in payload.items():
            key_text = str(key)
            path     = f"{prefix}.{key_text}" if prefix else key_text
            lowered  = key_text.lower()

            if any(fragment in lowered for fragment in SENSITIVE_PARAMETER_FRAGMENTS):
                paths.append(path)

            paths.extend(find_sensitive_parameter_paths(value, prefix=path))

    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            path = f"{prefix}[{index}]"
            paths.extend(find_sensitive_parameter_paths(value, prefix=path))

    return sorted(set(paths))


def build_idempotency_key(request: ApprovalRequestCreate) -> str:
    """
    Build a stable hash for the exact backfill action scope.

    Args:
        request: Validated approval proposal.

    Returns:
        Hex-encoded SHA-256 idempotency key.
    """
    payload = {
        "action_type": request.action_type,
        "alert_key": request.alert_key,
        "dispatcher_dag_id": request.dispatcher_dag_id,
        "target_dag_id": request.target_dag_id,
        "start_date": request.start_date.isoformat(),
        "end_date": request.end_date.isoformat(),
        "parameters": request.parameters,
    }

    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def build_request_id(request: ApprovalRequestCreate, idempotency_key: str) -> str:
    """
    Build a readable deterministic approval reference.

    Args:
        request: Validated approval proposal.
        idempotency_key: Stable action-scope hash.

    Returns:
        Human-facing identifier such as APR-20260610-A1B2C3D4.
    """
    return f"APR-{request.start_date.strftime('%Y%m%d')}-{idempotency_key[:8].upper()}"


def parse_parameters_json(value: str | None) -> dict[str, Any]:
    """
    Parse persisted approval parameters into a dictionary.

    Args:
        value: JSON text stored in ClickHouse.

    Returns:
        Parsed dictionary, or an empty dictionary for blank values.

    Raises:
        ValueError: If the JSON is not an object.
    """
    payload = json.loads(value or "{}")

    if not isinstance(payload, dict):
        raise ValueError("Approval parameters_json must contain a JSON object.")

    return payload


def approval_from_row(row: tuple[Any, ...]) -> ApprovalRequest:
    """
    Convert one ClickHouse result tuple into an ApprovalRequest model.

    Args:
        row: Result tuple ordered according to APPROVAL_REQUEST_COLUMNS.

    Returns:
        Validated approval request model.
    """
    payload                    = dict(zip(APPROVAL_REQUEST_COLUMNS, row, strict=True))
    payload["parameters"]      = parse_parameters_json(payload.pop("parameters_json"))
    payload["dry_run"]         = bool(payload["dry_run"])

    return ApprovalRequest.model_validate(payload)


def approval_to_row(request: ApprovalRequest) -> list[Any]:
    """
    Convert an ApprovalRequest model into ClickHouse insert column order.

    Args:
        request: Approval request state to persist.

    Returns:
        Row values aligned to APPROVAL_REQUEST_COLUMNS.
    """
    return [
        request.request_id,
        request.created_at,
        request.updated_at,
        request.alert_id,
        request.alert_key,
        request.agent_run_id,
        request.action_type,
        enum_value(request.risk_level),
        enum_value(request.status),
        request.requested_by,
        request.reason,
        request.dispatcher_dag_id,
        request.target_dag_id,
        request.start_date,
        request.end_date,
        canonical_json(request.parameters),
        int(request.dry_run),
        request.idempotency_key,
        request.decided_by,
        request.decided_at,
        request.decision_comment,
        request.execution_dag_run_id,
        request.execution_status,
        request.execution_error,
    ]


# --- Defining ClickHouse Helpers
def approval_select_sql(where_sql: str, limit: int = 1) -> str:
    """
    Build a bounded query for latest approval states.

    Args:
        where_sql: Trusted internal predicate assembled from quoted literals.
        limit: Maximum rows returned.

    Returns:
        ClickHouse SELECT statement using ReplacingMergeTree FINAL semantics.
    """
    selected_columns = ",\n            ".join(APPROVAL_REQUEST_COLUMNS)

    return f"""
        SELECT
            {selected_columns}
        FROM {APPROVAL_REQUESTS_TABLE} FINAL
        WHERE {where_sql}
        ORDER BY updated_at DESC, request_id ASC
        LIMIT {max(1, min(int(limit), 100))}
    """


def get_approval_request(client: Any, request_id: str) -> ApprovalRequest | None:
    """
    Load the latest state for one approval request identifier.

    Args:
        client: clickhouse-connect client instance.
        request_id: Human-facing approval request ID.

    Returns:
        ApprovalRequest when found, otherwise None.
    """
    rows = client.query(
        approval_select_sql(
            where_sql=f"request_id = {quote_sql_literal(request_id.strip())}",
            limit=1,
        )
    ).result_rows

    return approval_from_row(rows[0]) if rows else None


def get_approval_request_by_idempotency_key(client: Any, idempotency_key: str) -> ApprovalRequest | None:
    """
    Load an existing request for the same deterministic action scope.

    Args:
        client: clickhouse-connect client instance.
        idempotency_key: Stable action-scope hash.

    Returns:
        Existing ApprovalRequest when found, otherwise None.
    """
    rows = client.query(
        approval_select_sql(
            where_sql=f"idempotency_key = {quote_sql_literal(idempotency_key)}",
            limit=1,
        )
    ).result_rows

    return approval_from_row(rows[0]) if rows else None


def list_approval_requests(
    client: Any,
    status: ApprovalRequestStatus | str | None = None,
    limit: int = 50,
) -> list[ApprovalRequest]:
    """
    List latest approval request states with an optional status filter.

    Args:
        client: clickhouse-connect client instance.
        status: Optional pending, approved, or rejected status.
        limit: Maximum number of requests returned.

    Returns:
        Approval requests ordered by latest update time.
    """
    where_sql = "1 = 1"

    if status is not None:
        normalized_status = ApprovalRequestStatus(enum_value(status)).value
        where_sql         = f"status = {quote_sql_literal(normalized_status)}"

    rows = client.query(approval_select_sql(where_sql=where_sql, limit=limit)).result_rows

    return [approval_from_row(row) for row in rows]


def insert_approval_request(client: Any, request: ApprovalRequest) -> None:
    """
    Persist one append-versioned approval request state.

    Args:
        client: clickhouse-connect client instance.
        request: Approval state to append.

    Returns:
        None.
    """
    client.insert(
        table=APPROVAL_REQUESTS_TABLE,
        data=[approval_to_row(request)],
        column_names=APPROVAL_REQUEST_COLUMNS,
    )


# --- Defining Approval Lifecycle
def create_approval_request(
    request: ApprovalRequestCreate,
    client: Any | None = None,
) -> tuple[ApprovalRequest, bool]:
    """
    Create one idempotent approval request and audit the request boundary.

    Args:
        request: Validated approval proposal.
        client: Optional ClickHouse client override for tests or shared callers.

    Returns:
        Tuple containing the latest request and a created-new flag.
    """
    resolved_client = client or build_clickhouse_client()
    idempotency_key = build_idempotency_key(request)
    existing        = get_approval_request_by_idempotency_key(resolved_client, idempotency_key)

    if existing is not None:
        logger.info(
            "Reusing idempotent approval request | request_id=%s status=%s",
            existing.request_id,
            existing.status,
        )

        return existing, False

    now = datetime.now(timezone.utc)

    approval = ApprovalRequest(
        request_id=build_request_id(request, idempotency_key),
        created_at=now,
        updated_at=now,
        alert_id=request.alert_id,
        alert_key=request.alert_key,
        agent_run_id=request.agent_run_id,
        action_type=request.action_type,
        risk_level=ApprovalRiskLevel.HIGH,
        status=ApprovalRequestStatus.PENDING,
        requested_by=request.requested_by,
        reason=request.reason,
        dispatcher_dag_id=request.dispatcher_dag_id,
        target_dag_id=request.target_dag_id,
        start_date=request.start_date,
        end_date=request.end_date,
        parameters=request.parameters,
        dry_run=False,
        idempotency_key=idempotency_key,
    )

    insert_approval_request(resolved_client, approval)
    write_agent_audit_event(
        client=resolved_client,
        action="approval_requested",
        status="pending",
        agent_run_id=approval.agent_run_id,
        alert_id=approval.alert_id,
        alert_key=approval.alert_key,
        actor=approval.requested_by,
        tool_name="approval_queue",
        input_payload={
            "request_id": approval.request_id,
            "action_type": approval.action_type,
            "risk_level": approval.risk_level,
            "dispatcher_dag_id": approval.dispatcher_dag_id,
            "target_dag_id": approval.target_dag_id,
            "start_date": approval.start_date,
            "end_date": approval.end_date,
            "parameters": approval.parameters,
        },
        output_payload={"request_id": approval.request_id, "status": approval.status},
    )

    logger.info(
        "Created approval request | request_id=%s action=%s target=%s dates=%s..%s",
        approval.request_id,
        approval.action_type,
        approval.target_dag_id,
        approval.start_date,
        approval.end_date,
    )

    return approval, True


def decide_approval_request(
    request_id: str,
    decision: ApprovalDecision | str,
    decided_by: str,
    comment: str = "",
    client: Any | None = None,
) -> tuple[ApprovalRequest, bool]:
    """
    Apply an idempotent terminal human decision to one approval request.

    Args:
        request_id: Human-facing approval request ID.
        decision: Approve or reject decision.
        decided_by: Human identity making the decision.
        comment: Optional bounded decision comment.
        client: Optional ClickHouse client override for tests or shared callers.

    Returns:
        Tuple containing latest state and a changed-state flag.

    Raises:
        LookupError: If the request does not exist.
        ValueError: If identity is blank or a conflicting terminal decision is attempted.
    """
    resolved_client   = client or build_clickhouse_client()
    normalized_id     = request_id.strip()
    normalized_actor  = decided_by.strip()
    normalized_comment = comment.strip()[:2000]
    normalized_decision = ApprovalDecision(enum_value(decision))
    target_status       = (
        ApprovalRequestStatus.APPROVED
        if normalized_decision == ApprovalDecision.APPROVE
        else ApprovalRequestStatus.REJECTED
    )

    if not normalized_actor:
        raise ValueError("decided_by cannot be blank.")

    current = get_approval_request(resolved_client, normalized_id)

    if current is None:
        raise LookupError(f"Approval request was not found: {normalized_id}")

    if current.status == target_status.value:
        logger.info(
            "Approval decision already applied | request_id=%s status=%s",
            current.request_id,
            current.status,
        )

        return current, False

    if current.status != ApprovalRequestStatus.PENDING.value:
        raise ValueError(
            f"Approval request {current.request_id} is already {current.status}; "
            f"cannot change it to {target_status.value}."
        )

    decided_at = datetime.now(timezone.utc)
    updated    = current.model_copy(
        update={
            "updated_at": decided_at,
            "status": target_status.value,
            "decided_by": normalized_actor,
            "decided_at": decided_at,
            "decision_comment": normalized_comment,
        }
    )

    insert_approval_request(resolved_client, updated)
    write_agent_audit_event(
        client=resolved_client,
        action="approval_decision",
        status=target_status.value,
        agent_run_id=updated.agent_run_id,
        alert_id=updated.alert_id,
        alert_key=updated.alert_key,
        actor=normalized_actor,
        tool_name="approval_queue",
        input_payload={
            "request_id": updated.request_id,
            "decision": normalized_decision.value,
            "comment": normalized_comment,
        },
        output_payload={"request_id": updated.request_id, "status": updated.status},
    )

    logger.info(
        "Applied approval decision | request_id=%s status=%s decided_by=%s",
        updated.request_id,
        updated.status,
        updated.decided_by,
    )

    return updated, True


def transition_approval_execution(
    request_id: str,
    execution_status: ApprovalExecutionStatus | str,
    execution_dag_run_id: str,
    error_message: str = "",
    actor: str = "airflow",
    client: Any | None = None,
) -> tuple[ApprovalRequest, bool]:
    """
    Append one validated single-use approval execution transition.

    Args:
        request_id: Human-facing APR request identifier.
        execution_status: Target dispatching, dispatched, succeeded, or failed state.
        execution_dag_run_id: Parent Airflow dispatcher DagRun identifier.
        error_message: Optional bounded failure detail.
        actor: System identity responsible for the transition.
        client: Optional ClickHouse client override for tests or shared callers.

    Returns:
        Tuple containing latest state and a changed-state flag.

    Raises:
        LookupError: If the approval request does not exist.
        ValueError: If status, approval, ownership, or transition policy is invalid.
    """
    normalized_id     = request_id.strip()
    normalized_run_id = execution_dag_run_id.strip()
    normalized_actor  = actor.strip() or "airflow"
    target_status     = ApprovalExecutionStatus(enum_value(execution_status)).value
    resolved_client   = client or build_clickhouse_client()
    current           = get_approval_request(resolved_client, normalized_id)

    if not normalized_run_id:
        raise ValueError("execution_dag_run_id cannot be blank.")

    if target_status == ApprovalExecutionStatus.NOT_STARTED.value:
        raise ValueError("Execution lifecycle cannot transition back to not_started.")

    if current is None:
        raise LookupError(f"Approval request was not found: {normalized_id}")

    if current.status != ApprovalRequestStatus.APPROVED.value:
        raise ValueError(
            f"Approval request {current.request_id} is {current.status}; execution requires approved status."
        )

    if current.execution_dag_run_id and current.execution_dag_run_id != normalized_run_id:
        raise ValueError(
            f"Approval request {current.request_id} is already claimed by "
            f"{current.execution_dag_run_id}."
        )

    current_status = enum_value(current.execution_status)

    if current_status == target_status:
        logger.info(
            "Approval execution transition already applied | request_id=%s status=%s run_id=%s",
            current.request_id,
            target_status,
            normalized_run_id,
        )

        return current, False

    allowed_targets = ALLOWED_EXECUTION_TRANSITIONS.get(current_status)

    if allowed_targets is None or target_status not in allowed_targets:
        raise ValueError(
            f"Invalid approval execution transition for {current.request_id}: "
            f"{current_status} -> {target_status}."
        )

    execution_error = (
        error_message.strip()[:2000]
        if target_status == ApprovalExecutionStatus.FAILED.value
        else ""
    )
    updated_at = datetime.now(timezone.utc)
    updated    = current.model_copy(
        update={
            "updated_at": updated_at,
            "execution_dag_run_id": normalized_run_id,
            "execution_status": target_status,
            "execution_error": execution_error,
        }
    )

    insert_approval_request(resolved_client, updated)
    write_agent_audit_event(
        client=resolved_client,
        action="approval_execution_transition",
        status=target_status,
        agent_run_id=updated.agent_run_id,
        alert_id=updated.alert_id,
        alert_key=updated.alert_key,
        actor=normalized_actor,
        tool_name="approval_queue",
        input_payload={
            "request_id": updated.request_id,
            "from_status": current_status,
            "to_status": target_status,
            "execution_dag_run_id": normalized_run_id,
        },
        output_payload={
            "request_id": updated.request_id,
            "approval_status": updated.status,
            "execution_status": target_status,
            "execution_error": execution_error,
        },
    )

    logger.info(
        "Applied approval execution transition | request_id=%s from=%s to=%s run_id=%s",
        updated.request_id,
        current_status,
        target_status,
        normalized_run_id,
    )

    return updated, True

def require_approved_backfill_request(
    request_id: str,
    target_dag_id: str,
    start_date: date,
    end_date: date,
    parameters: dict[str, Any] | None = None,
    client: Any | None = None,
) -> ApprovalRequest:
    """
    Validate that an approved request exactly matches an execution proposal.

    Args:
        request_id: Approval reference supplied to the Airflow dispatcher.
        target_dag_id: Target DAG proposed by the dispatcher run.
        start_date: Inclusive proposed start date.
        end_date: Inclusive proposed end date.
        parameters: Complete or partial dispatcher parameter proposal.
        client: Optional ClickHouse client override for tests or shared callers.

    Returns:
        Matching approved request.

    Raises:
        ValueError: If the request is absent, unapproved, replayed, or scope-mismatched.
    """
    normalized_id = request_id.strip()

    if not normalized_id:
        raise ValueError("approval_request_id is required when dry_run=false.")

    resolved_client = client or build_clickhouse_client()
    approval        = get_approval_request(resolved_client, normalized_id)
    execution_parameters = normalize_backfill_parameters(parameters)

    if approval is None:
        raise ValueError(f"Approval request was not found: {normalized_id}")

    expected = {
        "status": ApprovalRequestStatus.APPROVED.value,
        "action_type": "backfill",
        "dispatcher_dag_id": BACKFILL_DISPATCHER_DAG_ID,
        "target_dag_id": target_dag_id,
        "start_date": start_date,
        "end_date": end_date,
        "parameters": execution_parameters,
        "dry_run": False,
        "execution_status": ApprovalExecutionStatus.NOT_STARTED.value,
    }
    actual = {
        "status": approval.status,
        "action_type": approval.action_type,
        "dispatcher_dag_id": approval.dispatcher_dag_id,
        "target_dag_id": approval.target_dag_id,
        "start_date": approval.start_date,
        "end_date": approval.end_date,
        "parameters": approval.parameters,
        "dry_run": approval.dry_run,
        "execution_status": enum_value(approval.execution_status),
    }

    mismatches = [key for key, expected_value in expected.items() if actual[key] != expected_value]

    if mismatches:
        mismatch_text = ", ".join(
            f"{key}: expected={expected[key]!r} actual={actual[key]!r}"
            for key in mismatches
        )
        raise ValueError(f"Approval request does not authorize this dispatcher run: {mismatch_text}")

    logger.info(
        "Validated approved backfill request | request_id=%s target=%s dates=%s..%s",
        approval.request_id,
        target_dag_id,
        start_date,
        end_date,
    )

    return approval
