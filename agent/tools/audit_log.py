####
## Agent Audit Log Tool for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Add repo root before importing project packages when this file is executed by path.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipelines.common.clickhouse import build_clickhouse_client
from pipelines.common.logging import logger


# --- Defining Constants
AGENT_AUDIT_LOG_TABLE = "dq.agent_audit_log"

AGENT_AUDIT_LOG_COLUMNS = [
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

    Returns:
        Agent run UUID associated with the event.
    """
    resolved_agent_run_id = normalize_uuid(agent_run_id) or uuid4()
    resolved_alert_id     = normalize_uuid(alert_id)
    sql_hash              = hash_sql(sql) if sql else ""

    row = [
        resolved_alert_id,
        alert_key,
        resolved_agent_run_id,
        actor,
        action,
        tool_name,
        status,
        duration_ms,
        json_dumps_safe(input_payload),
        json_dumps_safe(output_payload),
        error_message[:2000],
        sql_hash,
        row_count,
        report_s3_uri,
    ]

    logger.info(
        "Writing agent audit event | agent_run_id=%s alert_key=%s tool=%s action=%s status=%s row_count=%s",
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
