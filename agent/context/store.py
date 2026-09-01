####
## Agent Context ClickHouse Store for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Persist and retrieve bounded run context and durable incident memory."""

# --- Importing Libraries
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

from agent.context.models import IncidentMemoryRecord, RunContextEvent, canonical_json
from agent.specialists.contracts import EvidenceReference
from pipelines.common.clickhouse import quote_sql_literal
from pipelines.common.logging import logger


# --- Defining Constants
PROJECT_ROOT = Path(__file__).resolve().parents[2]

AGENT_CONTEXT_DDL_PATH   = PROJECT_ROOT / "infra" / "init" / "clickhouse" / "07_agent_context_tables.sql"
RUN_CONTEXT_TABLE        = "dq.agent_run_context_events"
INCIDENT_MEMORY_TABLE    = "dq.incident_memory"
DEFAULT_MEMORY_LOOKBACK  = 90
MAX_MEMORY_LOOKBACK      = 365
DEFAULT_MEMORY_LIMIT     = 20
MAX_MEMORY_LIMIT         = 100

RUN_CONTEXT_COLUMNS = (
    "context_event_id",
    "parent_run_id",
    "external_run_id",
    "event_sequence",
    "phase",
    "occurred_at",
    "expires_at",
    "requester",
    "status",
    "selected_specialist",
    "task_type",
    "task_id",
    "alert_id",
    "alert_key",
    "alert_display_id",
    "context_references_json",
    "evidence_references_json",
    "decision_json",
    "report_s3_uri",
    "approval_state",
    "content_sha256",
)

INCIDENT_MEMORY_COLUMNS = (
    "memory_id",
    "memory_key",
    "parent_run_id",
    "recorded_at",
    "memory_type",
    "alert_id",
    "alert_key",
    "alert_display_id",
    "outcome_status",
    "specialist_name",
    "task_type",
    "summary",
    "evidence_references_json",
    "decision_json",
    "report_s3_uri",
    "approval_state",
    "resolution_reference",
    "content_sha256",
)


# --- Defining Text And JSON Helpers
def clickhouse_text(value: Any) -> str:
    """
    Normalize ClickHouse String and FixedString values into plain text.

    Args:
        value: Raw scalar returned by clickhouse-connect.

    Returns:
        Text without FixedString null padding.
    """
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").rstrip("\x00")

    return str(value or "").rstrip("\x00")


def parse_json_list(value: Any, field_name: str) -> list[dict[str, Any]]:
    """
    Parse a persisted JSON list while preserving an explicit failure boundary.

    Args:
        value: Raw JSON text returned by ClickHouse.
        field_name: Field name used in parsing errors.

    Returns:
        List of JSON object dictionaries.

    Raises:
        ValueError: If the value is not a JSON list of objects.
    """
    try:
        payload = json.loads(clickhouse_text(value) or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError(f"Malformed persisted JSON in {field_name}.") from exc

    if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
        raise ValueError(f"Persisted {field_name} must contain a JSON object list.")

    return payload


def parse_json_object(value: Any, field_name: str) -> dict[str, Any]:
    """
    Parse one persisted JSON object with strict type validation.

    Args:
        value: Raw JSON text returned by ClickHouse.
        field_name: Field name used in parsing errors.

    Returns:
        JSON object dictionary.

    Raises:
        ValueError: If the value is malformed or not an object.
    """
    try:
        payload = json.loads(clickhouse_text(value) or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"Malformed persisted JSON in {field_name}.") from exc

    if not isinstance(payload, dict):
        raise ValueError(f"Persisted {field_name} must contain a JSON object.")

    return payload


def rows_to_dicts(columns: list[str], rows: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
    """
    Map clickhouse-connect result tuples into dictionaries.

    Args:
        columns: Query result column names.
        rows: Query result row tuples.

    Returns:
        Row dictionaries in source order.
    """
    return [dict(zip(columns, row, strict=False)) for row in rows]


# --- Defining DDL Functions
def read_agent_context_ddl_statements(
    path: Path = AGENT_CONTEXT_DDL_PATH,
) -> tuple[str, ...]:
    """
    Read the modular agent-context DDL into exactly two table statements.

    Args:
        path: DDL file defining temporary context and durable memory tables.

    Returns:
        Tuple containing two CREATE TABLE statements.

    Raises:
        FileNotFoundError: If the modular DDL file is missing.
        ValueError: If the file does not define exactly two tables.
    """
    if not path.is_file():
        raise FileNotFoundError(f"Agent context DDL not found: {path}")

    ddl        = path.read_text(encoding="utf-8-sig")
    statements = tuple(statement.strip() for statement in ddl.split(";") if statement.strip())

    if len(statements) != 2 or sum("CREATE TABLE" in item.upper() for item in statements) != 2:
        raise ValueError("Agent context DDL must contain exactly two CREATE TABLE statements.")

    return statements


def ensure_agent_context_tables(
    client: Any,
    path: Path = AGENT_CONTEXT_DDL_PATH,
) -> None:
    """
    Apply idempotent context and memory DDL for existing ClickHouse volumes.

    Args:
        client: clickhouse-connect client with command support.
        path: Modular context DDL path.

    Returns:
        None.
    """
    statements = read_agent_context_ddl_statements(path)
    logger.info("Ensuring agent context tables | ddl=%s statements=%d", path, len(statements))

    for statement in statements:
        client.command(statement)


# --- Defining Persistence Functions
def persist_run_context_event(client: Any, event: RunContextEvent) -> UUID:
    """
    Persist one idempotently identified temporary context event.

    Args:
        client: clickhouse-connect client.
        event: Validated run context event.

    Returns:
        Persisted context event UUID.
    """
    row = [
        event.context_event_id,
        event.parent_run_id,
        event.external_run_id,
        event.event_sequence,
        event.phase.value,
        event.occurred_at,
        event.expires_at,
        event.requester,
        event.status.value,
        event.selected_specialist,
        event.task_type,
        event.task_id,
        event.alert_id,
        event.alert_key,
        event.alert_display_id,
        canonical_json([item.model_dump(mode="json") for item in event.context_references]),
        canonical_json([item.model_dump(mode="json") for item in event.evidence_references]),
        canonical_json(event.decision_facts),
        event.report_s3_uri,
        event.approval_state.value,
        event.content_sha256,
    ]

    logger.info(
        "Persisting run context event | parent_run_id=%s phase=%s event_id=%s",
        event.parent_run_id,
        event.phase.value,
        event.context_event_id,
    )
    client.insert(
        table=RUN_CONTEXT_TABLE,
        data=[row],
        column_names=RUN_CONTEXT_COLUMNS,
    )

    return event.context_event_id


def persist_incident_memory(client: Any, record: IncidentMemoryRecord) -> UUID:
    """
    Persist one idempotently identified durable incident-memory record.

    Args:
        client: clickhouse-connect client.
        record: Validated durable incident memory.

    Returns:
        Persisted incident-memory UUID.
    """
    row = [
        record.memory_id,
        record.memory_key,
        record.parent_run_id,
        record.recorded_at,
        record.memory_type.value,
        record.alert_id,
        record.alert_key,
        record.alert_display_id,
        record.outcome_status.value,
        record.specialist_name,
        record.task_type,
        record.summary,
        canonical_json([item.model_dump(mode="json") for item in record.evidence_references]),
        canonical_json(record.decision_facts),
        record.report_s3_uri,
        record.approval_state.value,
        record.resolution_reference,
        record.content_sha256,
    ]

    logger.info(
        "Persisting incident memory | parent_run_id=%s memory_id=%s alert_ref=%s",
        record.parent_run_id,
        record.memory_id,
        record.alert_display_id or record.alert_key or record.alert_id,
    )
    client.insert(
        table=INCIDENT_MEMORY_TABLE,
        data=[row],
        column_names=INCIDENT_MEMORY_COLUMNS,
    )

    return record.memory_id


# --- Defining Read Functions
def build_incident_memory_query(
    alert_reference: str,
    lookback_days: int = DEFAULT_MEMORY_LOOKBACK,
    limit: int = DEFAULT_MEMORY_LIMIT,
) -> str:
    """
    Build a bounded read-only query for recent durable incident memory.

    Args:
        alert_reference: Canonical system key, human Alert Ref, or alert UUID.
        lookback_days: Mandatory recent timestamp window.
        limit: Hard result-row limit.

    Returns:
        Read-only ClickHouse query using exact alert identity predicates.

    Raises:
        ValueError: If the alert identity, lookback, or limit is invalid.
    """
    normalized_reference = alert_reference.strip()

    if not normalized_reference or "\n" in normalized_reference or "\r" in normalized_reference:
        raise ValueError("Incident memory lookup requires a single-line alert reference.")

    if not 1 <= lookback_days <= MAX_MEMORY_LOOKBACK:
        raise ValueError(f"lookback_days must be between 1 and {MAX_MEMORY_LOOKBACK}.")

    if not 1 <= limit <= MAX_MEMORY_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_MEMORY_LIMIT}.")

    reference_literal = quote_sql_literal(normalized_reference)

    return f"""
        SELECT
            memory_id,
            memory_key,
            parent_run_id,
            recorded_at,
            memory_type,
            alert_id,
            alert_key,
            alert_display_id,
            outcome_status,
            specialist_name,
            task_type,
            summary,
            evidence_references_json,
            decision_json,
            report_s3_uri,
            approval_state,
            resolution_reference,
            content_sha256
        FROM {INCIDENT_MEMORY_TABLE} FINAL
        WHERE recorded_at >= now64(3) - INTERVAL {lookback_days} DAY
          AND (
                alert_key = {reference_literal}
             OR alert_display_id = {reference_literal}
             OR toString(alert_id) = {reference_literal}
          )
        ORDER BY recorded_at DESC
        LIMIT {limit}
    """


def fetch_incident_memory(
    client: Any,
    alert_reference: str,
    lookback_days: int = DEFAULT_MEMORY_LOOKBACK,
    limit: int = DEFAULT_MEMORY_LIMIT,
) -> list[IncidentMemoryRecord]:
    """
    Fetch recent typed incident memory without raw audit or conversation payloads.

    Args:
        client: clickhouse-connect client.
        alert_reference: Canonical system key, human Alert Ref, or alert UUID.
        lookback_days: Mandatory recent timestamp window.
        limit: Hard result-row limit.

    Returns:
        Typed durable incident-memory records ordered newest first.

    Raises:
        ValueError: If persisted JSON or required record fields are malformed.
    """
    result = client.query(
        build_incident_memory_query(
            alert_reference=alert_reference,
            lookback_days=lookback_days,
            limit=limit,
        )
    )
    rows = rows_to_dicts(
        columns=list(result.column_names or []),
        rows=list(result.result_rows or []),
    )
    records: list[IncidentMemoryRecord] = []

    for row in rows:
        context_payload = parse_json_list(
            row.get("evidence_references_json"),
            "evidence_references_json",
        )
        records.append(
            IncidentMemoryRecord(
                memory_id=row.get("memory_id"),
                memory_key=clickhouse_text(row.get("memory_key")),
                parent_run_id=row.get("parent_run_id"),
                recorded_at=row.get("recorded_at"),
                memory_type=clickhouse_text(row.get("memory_type")),
                alert_id=row.get("alert_id"),
                alert_key=clickhouse_text(row.get("alert_key")),
                alert_display_id=clickhouse_text(row.get("alert_display_id")),
                outcome_status=clickhouse_text(row.get("outcome_status")),
                specialist_name=clickhouse_text(row.get("specialist_name")),
                task_type=clickhouse_text(row.get("task_type")),
                summary=clickhouse_text(row.get("summary")),
                evidence_references=[
                    EvidenceReference.model_validate(item)
                    for item in context_payload
                ],
                decision_facts=parse_json_object(row.get("decision_json"), "decision_json"),
                report_s3_uri=clickhouse_text(row.get("report_s3_uri")),
                approval_state=clickhouse_text(row.get("approval_state")),
                resolution_reference=clickhouse_text(row.get("resolution_reference")),
                content_sha256=clickhouse_text(row.get("content_sha256")),
            )
        )

    logger.info(
        "Fetched incident memory | alert_reference=%s lookback_days=%d rows=%d",
        alert_reference,
        lookback_days,
        len(records),
    )

    return records
