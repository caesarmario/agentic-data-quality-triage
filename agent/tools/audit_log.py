####
## Agent Audit Log Tool for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Add repo root before importing project packages when this file is executed by path.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipelines.common.clickhouse import build_clickhouse_client
from pipelines.common.logging import logger


# --- Defining Constants
AGENT_AUDIT_LOG_TABLE = "dq.agent_audit_log"
AUDIT_IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[0-9a-f]{64}$")

AGENT_AUDIT_LOG_COLUMNS = [
    "audit_id",
    "alert_id",
    "alert_key",
    "agent_run_id",
    "actor",
    "action",
    "tool_name",
    "status",
    "duration_ms",
    "input_json",
    "output_json",
    "error_message",
    "sql_hash",
    "row_count",
    "report_s3_uri",
]


# --- Defining Functions
def hash_sql(sql: str) -> str:
    """
    Build a stable SHA-256 hash for an executed SQL statement.

    Args:
        sql: SQL statement to hash.

    Returns:
        Hex-encoded SHA-256 hash.
    """
    normalized = " ".join(sql.strip().split())

    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def build_audit_idempotency_key(scope: str, *values: Any) -> str:
    """
    Build a stable SHA-256 key for one explicitly replay-safe audit event.

    Args:
        scope: Bounded event scope such as store_triage_report.
        values: Stable correlation values that define the logical event.

    Returns:
        Lowercase SHA-256 idempotency key.

    Raises:
        ValueError: If scope is blank or a correlation value is None.
    """
    normalized_scope = scope.strip()

    if not normalized_scope:
        raise ValueError("Audit idempotency scope must not be blank.")

    if any(value is None for value in values):
        raise ValueError("Audit idempotency values must not contain None.")

    canonical_payload = json.dumps(
        {
            "scope": normalized_scope,
            "values": [str(value) for value in values],
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )

    return hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()


def validate_audit_idempotency_key(value: str) -> str:
    """
    Validate one caller-supplied audit idempotency key.

    Args:
        value: Candidate lowercase SHA-256 key.

    Returns:
        Validated key.

    Raises:
        ValueError: If the key is not a lowercase SHA-256 digest.
    """
    normalized = value.strip()

    if not AUDIT_IDEMPOTENCY_KEY_PATTERN.fullmatch(normalized):
        raise ValueError("Audit idempotency key must be a lowercase SHA-256 digest.")

    return normalized


def build_audit_event_id(idempotency_key: str = "") -> UUID:
    """
    Build a deterministic event UUID for replay-safe events or a random UUID otherwise.

    Args:
        idempotency_key: Optional validated SHA-256 event key.

    Returns:
        Deterministic UUID5 when a key is supplied, otherwise UUID4.
    """
    if not idempotency_key:
        return uuid4()

    validated_key = validate_audit_idempotency_key(idempotency_key)

    return uuid5(NAMESPACE_URL, f"agent-audit:{validated_key}")


def audit_event_exists(client: Any, audit_id: UUID) -> bool:
    """
    Check whether a deterministic audit event has already been persisted.

    Args:
        client: clickhouse-connect client instance.
        audit_id: Deterministic audit event UUID.

    Returns:
        True when an event with the same UUID already exists.
    """
    result = client.query(
        f"""
            SELECT count()
            FROM {AGENT_AUDIT_LOG_TABLE}
            WHERE audit_id = {{audit_id:UUID}}
        """,
        parameters={"audit_id": str(audit_id)},
    )

    return bool(result.result_rows and int(result.result_rows[0][0]) > 0)


def json_dumps_safe(payload: dict[str, Any] | list[Any] | str | None) -> str:
    """
    Serialize an audit payload into JSON text.

    Args:
        payload: JSON-like object, string, or None.

    Returns:
        JSON string safe for ClickHouse insertion.
    """
    if payload is None:
        return "{}"

    if isinstance(payload, str):
        return payload

    return json.dumps(payload, default=json_default, ensure_ascii=True)


def json_default(value: Any) -> Any:
    """
    Convert common non-JSON Python values into serializable values.

    Args:
        value: Python value being serialized by json.dumps.

    Returns:
        JSON-serializable representation.

    Raises:
        TypeError: If the value type is not supported.
    """
    if isinstance(value, (datetime,)):
        return value.isoformat()

    if isinstance(value, UUID):
        return str(value)

    if isinstance(value, Decimal):
        return float(value)

    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def normalize_uuid(value: UUID | str | None) -> UUID | None:
    """
    Normalize a UUID-like value for ClickHouse insertion.

    Args:
        value: UUID, UUID string, or None.

    Returns:
        UUID instance or None.
    """
    if value is None or isinstance(value, UUID):
        return value

    return UUID(str(value))


def write_agent_audit_event(
    client: Any,
    action: str,
    status: str,
    agent_run_id: UUID | str | None = None,
    alert_id: UUID | str | None = None,
    alert_key: str = "",
    actor: str = "agent",
    tool_name: str = "",
    duration_ms: int | None = None,
    input_payload: dict[str, Any] | list[Any] | str | None = None,
    output_payload: dict[str, Any] | list[Any] | str | None = None,
    error_message: str = "",
    sql: str = "",
    row_count: int | None = None,
    report_s3_uri: str = "",
    idempotency_key: str = "",
) -> UUID:
    """
    Write one agent tool/action audit event to ClickHouse.

    Args:
        client: clickhouse-connect client instance.
        action: Logical action name.
        status: Tool/action status such as success, failed, or blocked.
        agent_run_id: Optional agent run UUID. A new UUID is generated when omitted.
        alert_id: Optional alert UUID.
        alert_key: Stable alert key related to the action.
        actor: Actor name, usually agent, user, or system.
        tool_name: Tool name that performed the action.
        duration_ms: Optional execution duration in milliseconds.
        input_payload: Tool input payload stored as JSON.
        output_payload: Tool output payload stored as JSON.
        error_message: Optional error message.
        sql: Optional SQL statement. Only a hash is persisted.
        row_count: Optional row count produced by the tool.
        report_s3_uri: Optional report artifact URI.
        idempotency_key: Optional SHA-256 key for an explicitly replay-safe event.
            The pre-insert check protects sequential replay, not concurrent distributed writes.

    Returns:
        Agent run UUID associated with the event.
    """
    resolved_agent_run_id = normalize_uuid(agent_run_id) or uuid4()
    resolved_alert_id     = normalize_uuid(alert_id)
    resolved_event_key    = validate_audit_idempotency_key(idempotency_key) if idempotency_key else ""
    audit_id              = build_audit_event_id(resolved_event_key)
    sql_hash              = hash_sql(sql) if sql else ""
    resolved_input        = input_payload

    if resolved_event_key:
        if resolved_input is not None and not isinstance(resolved_input, dict):
            raise ValueError("Replay-safe audit events require a dictionary input payload.")

        resolved_input = dict(resolved_input or {})
        resolved_input["_audit_idempotency_key"] = resolved_event_key

        if audit_event_exists(client=client, audit_id=audit_id):
            logger.info(
                "Reusing replay-safe audit event | audit_id=%s agent_run_id=%s action=%s",
                audit_id,
                resolved_agent_run_id,
                action,
            )

            return resolved_agent_run_id

    row = [
        audit_id,
        resolved_alert_id,
        alert_key,
        resolved_agent_run_id,
        actor,
        action,
        tool_name,
        status,
        duration_ms,
        json_dumps_safe(resolved_input),
        json_dumps_safe(output_payload),
        error_message[:2000],
        sql_hash,
        row_count,
        report_s3_uri,
    ]

    logger.info(
        "Writing agent audit event | audit_id=%s agent_run_id=%s alert_key=%s tool=%s action=%s status=%s row_count=%s",
        audit_id,
        resolved_agent_run_id,
        alert_key,
        tool_name,
        action,
        status,
        row_count,
    )

    client.insert(
        table=AGENT_AUDIT_LOG_TABLE,
        data=[row],
        column_names=AGENT_AUDIT_LOG_COLUMNS,
    )

    logger.info("Agent audit event written | agent_run_id=%s action=%s", resolved_agent_run_id, action)

    return resolved_agent_run_id


def build_llm_route_audit_payload(response: Any) -> dict[str, Any]:
    """
    Build a non-sensitive audit payload from a normalized LLM response.

    Args:
        response: LlmResponse-like object returned by the provider router.

    Returns:
        Dictionary containing provider, model, route, token, cost, fallback,
        duration, structured-output status, and sanitized provider failure metadata.
    """
    metadata = dict(getattr(response, "metadata", {}) or {})

    return {
        "requested_route": metadata.get("requested_route", getattr(response, "route_name", "")),
        "executed_route": metadata.get("executed_route", getattr(response, "route_name", "")),
        "attempted_routes": metadata.get("attempted_routes", []),
        "provider": getattr(response, "provider", ""),
        "model": getattr(response, "model", ""),
        "input_tokens": int(getattr(response, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(response, "output_tokens", 0) or 0),
        "estimated_cost_usd": float(getattr(response, "estimated_cost_usd", 0.0) or 0.0),
        "used_heuristic": bool(getattr(response, "used_heuristic", False)),
        "fallback_reason": str(getattr(response, "fallback_reason", "") or ""),
        "duration_ms": int(getattr(response, "duration_ms", 0) or 0),
        "finish_reason": str(metadata.get("finish_reason", "") or ""),
        "reasoning_tokens": int(metadata.get("reasoning_tokens", 0) or 0),
        "force_heuristic": bool(metadata.get("force_heuristic", False)),
        "structured_output_requested": bool(metadata.get("structured_output_requested", False)),
        "structured_output_mode": str(metadata.get("structured_output_mode", "") or ""),
        "structured_output_status": str(metadata.get("structured_output_status", "") or ""),
        "structured_output_validation_errors": metadata.get("structured_output_validation_errors", []),
        "structured_output_provider_fallback": bool(
            metadata.get("structured_output_provider_fallback", False)
        ),
        "provider_failures": metadata.get("provider_failures", []),
    }


def write_llm_route_audit_event(
    client: Any,
    response: Any,
    agent_run_id: UUID | str,
    alert_id: UUID | str | None,
    alert_key: str,
) -> UUID:
    """
    Persist one completed LLM route decision to ClickHouse audit storage.

    Args:
        client: clickhouse-connect client instance.
        response: LlmResponse-like object returned by the provider router.
        agent_run_id: Agent run UUID used for correlation.
        alert_id: Optional alert UUID related to the route call.
        alert_key: Stable alert key related to the route call.

    Returns:
        Agent run UUID associated with the audit event.
    """
    output_payload = build_llm_route_audit_payload(response=response)
    input_payload  = {
        "requested_route": output_payload["requested_route"],
        "force_heuristic": output_payload["force_heuristic"],
    }

    logger.info(
        "Writing LLM route audit event | agent_run_id=%s alert_key=%s route=%s provider=%s model=%s heuristic=%s",
        agent_run_id,
        alert_key,
        output_payload["executed_route"],
        output_payload["provider"],
        output_payload["model"],
        output_payload["used_heuristic"],
    )

    return write_agent_audit_event(
        client=client,
        action="llm_route_completed",
        status="success",
        agent_run_id=agent_run_id,
        alert_id=alert_id,
        alert_key=alert_key,
        tool_name="llm_router",
        duration_ms=output_payload["duration_ms"],
        input_payload=input_payload,
        output_payload=output_payload,
    )


def build_parser() -> argparse.ArgumentParser:
    """
    Build the command-line parser for audit log smoke testing.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(description="Write one agent audit event to ClickHouse.")

    parser.add_argument("--action", default="manual_smoke_test", help="Audit action name.")
    parser.add_argument("--status", default="success", help="Audit status.")
    parser.add_argument("--tool-name", default="audit_log", help="Tool name.")
    parser.add_argument("--alert-key", default="", help="Optional alert key.")
    parser.add_argument("--clickhouse-host", default=None, help="Optional ClickHouse host override.")
    parser.add_argument("--clickhouse-port", type=int, default=None, help="Optional ClickHouse HTTP port override.")

    return parser


def main() -> None:
    """
    Parse CLI arguments and write a smoke-test audit event.

    Returns:
        None.
    """
    parser = build_parser()
    args   = parser.parse_args()
    client = build_clickhouse_client(host=args.clickhouse_host, port=args.clickhouse_port)

    agent_run_id = write_agent_audit_event(
        client=client,
        action=args.action,
        status=args.status,
        alert_key=args.alert_key,
        tool_name=args.tool_name,
        input_payload={"source": "audit_log_cli"},
        output_payload={"written_at": datetime.now(timezone.utc)},
    )

    print(json.dumps({"status": "success", "agent_run_id": str(agent_run_id)}, indent=2))


# --- Running CLI Entrypoint
if __name__ == "__main__":
    main()
