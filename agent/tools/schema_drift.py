####
## Schema Drift Evidence Tool for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Read exact, bounded schema contract evidence for one triage alert."""

# --- Importing Libraries
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[2]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.state import Alert, EvidenceItem, EvidenceType
from agent.tools.alerts import load_alert
from agent.tools.audit_log import write_agent_audit_event
from agent.tools.clickhouse_sql import rows_to_dicts
from pipelines.common.clickhouse import (
    build_clickhouse_client,
    quote_sql_literal,
    validate_qualified_table_name,
)
from pipelines.common.logging import logger
from pipelines.schema_drift.detector import validate_schema_run_id
from pipelines.schema_drift.storage import (
    SCHEMA_DRIFT_RESULTS_TABLE,
    SCHEMA_SNAPSHOTS_TABLE,
    clickhouse_text,
)


# --- Defining Constants
TOOL_NAME                = "schema_drift"
DEFAULT_FINDING_LIMIT    = 50
MAX_FINDING_LIMIT        = 100
MAX_EVIDENCE_TEXT_LENGTH = 500

SNAPSHOT_COLUMNS = (
    "run_id",
    "observed_at",
    "contract_name",
    "contract_version",
    "contract_sha256",
    "qualified_name",
    "schema_sha256",
    "status",
    "highest_severity",
    "comparison_count",
    "finding_count",
)

FINDING_COLUMNS = (
    "column_name",
    "check_type",
    "status",
    "severity",
    "expected_value",
    "actual_value",
    "details_json",
    "total_finding_count",
)


# --- Defining Validation Helpers
def resolve_schema_alert_source(alert: Alert) -> tuple[str, str]:
    """
    Resolve the exact persisted detector run and table referenced by an alert.

    Args:
        alert: Loaded alert expected to represent deterministic schema drift.

    Returns:
        Tuple containing validated schema detector run ID and qualified table name.

    Raises:
        ValueError: If the alert is not schema drift or lacks valid source correlation.
    """
    if not alert.is_schema_drift:
        raise ValueError("Schema drift evidence may only be collected for schema drift alerts.")

    source_run_id = str(alert.details.get("source_schema_run_id") or "").strip()

    if not source_run_id:
        raise ValueError("Schema drift alert is missing details.source_schema_run_id.")

    run_id         = validate_schema_run_id(source_run_id)
    qualified_name = validate_qualified_table_name(alert.table_name)

    return run_id, qualified_name


def bounded_text(value: Any, limit: int = MAX_EVIDENCE_TEXT_LENGTH) -> str:
    """
    Normalize and bound a persisted schema comparison value.

    Args:
        value: Raw ClickHouse value.
        limit: Maximum retained character count.

    Returns:
        Normalized string with an explicit truncation suffix when required.
    """
    normalized = clickhouse_text(value)

    if len(normalized) <= limit:
        return normalized

    return f"{normalized[: limit - 14]}...[truncated]"


def parse_details_json(value: Any) -> dict[str, Any]:
    """
    Parse bounded finding details without exposing malformed payloads.

    Args:
        value: Raw details_json value from ClickHouse.

    Returns:
        Parsed JSON object, or an empty dictionary when input is invalid.
    """
    normalized = bounded_text(value)

    if not normalized:
        return {}

    try:
        parsed = json.loads(normalized)

    except json.JSONDecodeError:
        logger.warning("Ignoring malformed schema evidence details JSON")

        return {}

    return parsed if isinstance(parsed, dict) else {}


# --- Building Read-Only Queries
def build_schema_snapshot_sql(run_id: str, qualified_name: str) -> str:
    """
    Build an exact-run, exact-table query for one persisted schema snapshot.

    Args:
        run_id: Airflow detector run identifier.
        qualified_name: Fully qualified affected table.

    Returns:
        Bounded read-only ClickHouse SQL.
    """
    validated_run_id = validate_schema_run_id(run_id)
    validated_table  = validate_qualified_table_name(qualified_name)

    return f"""
        SELECT
            run_id,
            observed_at,
            contract_name,
            contract_version,
            contract_sha256,
            qualified_name,
            schema_sha256,
            status,
            highest_severity,
            comparison_count,
            finding_count
        FROM {SCHEMA_SNAPSHOTS_TABLE} FINAL
        WHERE run_id = {quote_sql_literal(validated_run_id)}
          AND qualified_name = {quote_sql_literal(validated_table)}
        ORDER BY observed_at DESC
        LIMIT 2
    """


def build_schema_findings_sql(
    run_id: str,
    qualified_name: str,
    limit: int = DEFAULT_FINDING_LIMIT,
) -> str:
    """
    Build a bounded query for persisted warning and failure findings.

    Args:
        run_id: Airflow detector run identifier.
        qualified_name: Fully qualified affected table.
        limit: Maximum findings retained by the caller.

    Returns:
        Read-only ClickHouse SQL with one overflow row for truncation detection.
    """
    validated_run_id = validate_schema_run_id(run_id)
    validated_table  = validate_qualified_table_name(qualified_name)
    safe_limit       = max(1, min(int(limit), MAX_FINDING_LIMIT))

    return f"""
        SELECT
            column_name,
            check_type,
            status,
            severity,
            expected_value,
            actual_value,
            details_json,
            count() OVER () AS total_finding_count
        FROM {SCHEMA_DRIFT_RESULTS_TABLE} FINAL
        WHERE run_id = {quote_sql_literal(validated_run_id)}
          AND qualified_name = {quote_sql_literal(validated_table)}
          AND status IN ('warn', 'fail')
        ORDER BY
            multiIf(severity = 'critical', 3, severity = 'warning', 2, 1) DESC,
            check_type,
            column_name
        LIMIT {safe_limit + 1}
    """


# --- Normalizing Persisted Evidence
def normalize_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    """
    Normalize one persisted snapshot into a JSON-safe evidence dictionary.

    Args:
        row: Snapshot row keyed by SNAPSHOT_COLUMNS.

    Returns:
        Normalized snapshot metadata.
    """
    return {
        "run_id": clickhouse_text(row.get("run_id")),
        "observed_at": row.get("observed_at"),
        "contract_name": clickhouse_text(row.get("contract_name")),
        "contract_version": int(row.get("contract_version") or 0),
        "contract_sha256": clickhouse_text(row.get("contract_sha256")),
        "qualified_name": clickhouse_text(row.get("qualified_name")),
        "schema_sha256": clickhouse_text(row.get("schema_sha256")),
        "snapshot_status": clickhouse_text(row.get("status")),
        "highest_severity": clickhouse_text(row.get("highest_severity")),
        "comparison_count": int(row.get("comparison_count") or 0),
        "finding_count": int(row.get("finding_count") or 0),
    }


def normalize_finding(row: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    """
    Normalize one schema finding and attach its contract correlation metadata.

    Args:
        row: Finding row keyed by FINDING_COLUMNS.
        snapshot: Normalized parent snapshot metadata.

    Returns:
        JSON-safe finding evidence row.
    """
    return {
        "contract_name": snapshot["contract_name"],
        "contract_version": snapshot["contract_version"],
        "contract_sha256": snapshot["contract_sha256"],
        "schema_sha256": snapshot["schema_sha256"],
        "qualified_name": snapshot["qualified_name"],
        "column_name": clickhouse_text(row.get("column_name")),
        "check_type": clickhouse_text(row.get("check_type")),
        "status": clickhouse_text(row.get("status")),
        "severity": clickhouse_text(row.get("severity")),
        "expected_value": bounded_text(row.get("expected_value")),
        "actual_value": bounded_text(row.get("actual_value")),
        "details": parse_details_json(row.get("details_json")),
    }


def validate_snapshot_integrity(
    snapshot: dict[str, Any],
    expected_contract_sha256: str = "",
    expected_schema_sha256: str = "",
    expected_finding_count: int | None = None,
) -> None:
    """
    Verify optional correlation fields against one persisted schema snapshot.

    Args:
        snapshot: Normalized persisted parent snapshot.
        expected_contract_sha256: Optional expected validated-contract hash.
        expected_schema_sha256: Optional expected observed-schema hash.
        expected_finding_count: Optional expected complete finding count.

    Returns:
        None.

    Raises:
        RuntimeError: If any supplied correlation value disagrees with persistence.
    """
    expected_pairs = {
        "contract_sha256": expected_contract_sha256.strip(),
        "schema_sha256": expected_schema_sha256.strip(),
    }

    for field_name, expected_value in expected_pairs.items():
        if expected_value and expected_value != str(snapshot[field_name]):
            raise RuntimeError(f"Schema {field_name} does not match persisted evidence.")

    if (
        expected_finding_count is not None
        and int(expected_finding_count) != int(snapshot["finding_count"])
    ):
        raise RuntimeError("Schema finding_count does not match persisted evidence.")


def build_schema_evidence_summary(
    qualified_name: str,
    finding_count: int,
    findings: list[dict[str, Any]],
    findings_truncated: int,
) -> str:
    """
    Build an operator-readable summary from deterministic schema findings.

    Args:
        qualified_name: Fully qualified affected table.
        finding_count: Complete persisted finding count.
        findings: Bounded visible finding rows.
        findings_truncated: Findings omitted from the bounded response.

    Returns:
        Concise summary describing severity, types, and affected columns.
    """
    if finding_count == 0:
        return (
            f"Schema contract evidence confirms no drift findings for {qualified_name}. "
            "The persisted snapshot matches the configured contract."
        )

    severity_counts = Counter(str(row.get("severity") or "unknown") for row in findings)
    finding_types   = sorted({str(row.get("check_type") or "unknown") for row in findings})
    changed_columns = sorted({str(row.get("column_name") or "<table>") for row in findings})
    truncation_text = f" {findings_truncated} additional finding(s) were omitted." if findings_truncated else ""

    return (
        f"Schema contract evidence confirms {finding_count} finding(s) for {qualified_name}. "
        f"Visible severity counts: {dict(severity_counts)}. "
        f"Finding types: {finding_types}. Affected columns: {changed_columns}."
        f"{truncation_text}"
    )


def query_schema_drift_run_context(
    client: Any,
    source_run_id: str,
    qualified_name: str,
    finding_limit: int = DEFAULT_FINDING_LIMIT,
    expected_contract_sha256: str = "",
    expected_schema_sha256: str = "",
    expected_finding_count: int | None = None,
    require_findings: bool = False,
) -> dict[str, Any]:
    """
    Read and validate one exact persisted schema run without writing audit state.

    Args:
        client: clickhouse-connect compatible client.
        source_run_id: Exact detector run identifier.
        qualified_name: Exact database.table snapshot identity.
        finding_limit: Maximum warning or failure rows returned.
        expected_contract_sha256: Optional expected contract hash.
        expected_schema_sha256: Optional expected observed-schema hash.
        expected_finding_count: Optional expected complete finding count.
        require_findings: Whether a zero-finding snapshot must fail closed.

    Returns:
        Bounded snapshot, findings, counts, summary, and fixed read-only SQL.

    Raises:
        RuntimeError: If persistence is missing, duplicated, or inconsistent.
    """
    run_id         = validate_schema_run_id(source_run_id)
    validated_name = validate_qualified_table_name(qualified_name)
    safe_limit     = max(1, min(int(finding_limit), MAX_FINDING_LIMIT))
    snapshot_sql   = build_schema_snapshot_sql(
        run_id=run_id,
        qualified_name=validated_name,
    )
    findings_sql   = build_schema_findings_sql(
        run_id=run_id,
        qualified_name=validated_name,
        limit=safe_limit,
    )

    snapshot_result = client.query(snapshot_sql)
    snapshot_rows   = rows_to_dicts(
        columns=list(snapshot_result.column_names or SNAPSHOT_COLUMNS),
        rows=snapshot_result.result_rows,
    )

    if len(snapshot_rows) != 1:
        raise RuntimeError(
            f"Expected one schema snapshot for run/table correlation; found {len(snapshot_rows)}."
        )

    snapshot = normalize_snapshot(snapshot_rows[0])
    validate_snapshot_integrity(
        snapshot=snapshot,
        expected_contract_sha256=expected_contract_sha256,
        expected_schema_sha256=expected_schema_sha256,
        expected_finding_count=expected_finding_count,
    )

    finding_result = client.query(findings_sql)
    finding_rows   = rows_to_dicts(
        columns=list(finding_result.column_names or FINDING_COLUMNS),
        rows=finding_result.result_rows,
    )
    persisted_count = int(snapshot["finding_count"])

    if persisted_count > 0 and not finding_rows:
        raise RuntimeError("Schema snapshot reports findings but no persisted findings were found.")

    if persisted_count == 0 and finding_rows:
        raise RuntimeError("Schema snapshot reports no findings but persisted finding rows exist.")

    if finding_rows:
        window_count = int(finding_rows[0].get("total_finding_count") or 0)

        if window_count != persisted_count:
            raise RuntimeError("Schema snapshot finding_count does not match persisted findings.")

    if require_findings and persisted_count == 0:
        raise RuntimeError("Schema drift alert has no persisted warning or failure findings.")

    visible_rows       = finding_rows[:safe_limit]
    findings           = [normalize_finding(row=row, snapshot=snapshot) for row in visible_rows]
    findings_truncated = max(0, persisted_count - len(findings))
    summary            = build_schema_evidence_summary(
        qualified_name=validated_name,
        finding_count=persisted_count,
        findings=findings,
        findings_truncated=findings_truncated,
    )
    combined_sql = f"{snapshot_sql.strip()}\n\n{findings_sql.strip()}"

    return {
        "status": "success",
        "run_id": run_id,
        "table_name": validated_name,
        "snapshot": snapshot,
        "findings": findings,
        "finding_count": persisted_count,
        "visible_finding_count": len(findings),
        "findings_truncated": findings_truncated,
        "summary": summary,
        "query": combined_sql,
    }


def fetch_schema_drift_run_context(
    source_run_id: str,
    qualified_name: str,
    finding_limit: int = DEFAULT_FINDING_LIMIT,
    agent_run_id: UUID | str | None = None,
    alert_key: str = "",
    clickhouse_host: str | None = None,
    clickhouse_port: int | None = None,
) -> dict[str, Any]:
    """
    Fetch and audit one exact schema detector run for specialist assessment.

    Args:
        source_run_id: Exact detector run identifier retained in ClickHouse.
        qualified_name: Exact database.table snapshot identity.
        finding_limit: Maximum warning or failure rows returned.
        agent_run_id: Optional parent agent run UUID for audit correlation.
        alert_key: Optional related alert reference.
        clickhouse_host: Optional ClickHouse host override.
        clickhouse_port: Optional ClickHouse HTTP port override.

    Returns:
        Bounded clean or drifted schema context from persisted detector evidence.

    Raises:
        ValueError: If run or table identifiers are unsafe.
        RuntimeError: If persisted evidence is missing or inconsistent.
    """
    client                = build_clickhouse_client(host=clickhouse_host, port=clickhouse_port)
    resolved_agent_run_id = UUID(str(agent_run_id)) if agent_run_id else uuid4()
    started_monotonic     = time.monotonic()
    safe_limit            = max(1, min(int(finding_limit), MAX_FINDING_LIMIT))
    snapshot_sql          = ""
    findings_sql          = ""
    input_payload         = {
        "source_schema_run_id": source_run_id,
        "qualified_name": qualified_name,
        "finding_limit": safe_limit,
    }

    try:
        run_id         = validate_schema_run_id(source_run_id)
        validated_name = validate_qualified_table_name(qualified_name)
        snapshot_sql   = build_schema_snapshot_sql(run_id=run_id, qualified_name=validated_name)
        findings_sql   = build_schema_findings_sql(
            run_id=run_id,
            qualified_name=validated_name,
            limit=safe_limit,
        )
        result = query_schema_drift_run_context(
            client=client,
            source_run_id=run_id,
            qualified_name=validated_name,
            finding_limit=safe_limit,
        )
        duration_ms = int((time.monotonic() - started_monotonic) * 1_000)

        write_agent_audit_event(
            client=client,
            action="fetch_schema_drift_run_context",
            status="success",
            agent_run_id=resolved_agent_run_id,
            alert_key=alert_key,
            tool_name=TOOL_NAME,
            duration_ms=duration_ms,
            input_payload=input_payload,
            output_payload={
                "contract_name": result["snapshot"]["contract_name"],
                "contract_version": result["snapshot"]["contract_version"],
                "snapshot_status": result["snapshot"]["snapshot_status"],
                "highest_severity": result["snapshot"]["highest_severity"],
                "finding_count": result["finding_count"],
                "visible_finding_count": result["visible_finding_count"],
                "findings_truncated": result["findings_truncated"],
            },
            sql=result["query"],
            row_count=result["visible_finding_count"],
        )

        logger.info(
            "Fetched schema detector run context | run_id=%s table=%s findings=%d",
            run_id,
            validated_name,
            result["finding_count"],
        )

        return result

    except Exception as exc:
        duration_ms = int((time.monotonic() - started_monotonic) * 1_000)
        audit_status = "blocked" if isinstance(exc, ValueError) else "failed"
        combined_sql = "\n\n".join(sql.strip() for sql in (snapshot_sql, findings_sql) if sql)

        logger.exception(
            "Failed to fetch schema detector run context | run_id=%s table=%s error_type=%s",
            source_run_id,
            qualified_name,
            type(exc).__name__,
        )
        write_agent_audit_event(
            client=client,
            action="fetch_schema_drift_run_context",
            status=audit_status,
            agent_run_id=resolved_agent_run_id,
            alert_key=alert_key,
            tool_name=TOOL_NAME,
            duration_ms=duration_ms,
            input_payload=input_payload,
            output_payload={"error_type": type(exc).__name__},
            error_message=str(exc),
            sql=combined_sql,
        )

        raise


# --- Querying Schema Drift Evidence
def fetch_schema_drift_context(
    alert: Alert,
    finding_limit: int = DEFAULT_FINDING_LIMIT,
    agent_run_id: UUID | str | None = None,
    clickhouse_host: str | None = None,
    clickhouse_port: int | None = None,
) -> dict[str, Any]:
    """
    Fetch exact persisted schema evidence and write one audit event.

    Args:
        alert: Loaded schema drift alert.
        finding_limit: Maximum findings returned to the triage state.
        agent_run_id: Optional agent run UUID for audit correlation.
        clickhouse_host: Optional ClickHouse host override.
        clickhouse_port: Optional ClickHouse HTTP port override.

    Returns:
        Snapshot metadata, bounded findings, truncation state, and query hashes via audit.

    Raises:
        ValueError: If alert source correlation is missing or unsafe.
        RuntimeError: If persisted evidence is missing, duplicated, or inconsistent.
    """
    client                = build_clickhouse_client(host=clickhouse_host, port=clickhouse_port)
    resolved_agent_run_id = UUID(str(agent_run_id)) if agent_run_id else uuid4()
    started_monotonic     = time.monotonic()
    safe_limit            = max(1, min(int(finding_limit), MAX_FINDING_LIMIT))
    snapshot_sql          = ""
    findings_sql          = ""
    input_payload         = {
        "alert_type": alert.alert_type,
        "table_name": alert.table_name,
        "source_schema_run_id": str(alert.details.get("source_schema_run_id") or ""),
        "finding_limit": safe_limit,
    }

    try:
        run_id, qualified_name = resolve_schema_alert_source(alert)
        snapshot_sql           = build_schema_snapshot_sql(run_id=run_id, qualified_name=qualified_name)
        findings_sql           = build_schema_findings_sql(
            run_id=run_id,
            qualified_name=qualified_name,
            limit=safe_limit,
        )

        result = query_schema_drift_run_context(
            client=client,
            source_run_id=run_id,
            qualified_name=qualified_name,
            finding_limit=safe_limit,
            expected_contract_sha256=str(alert.details.get("contract_sha256") or ""),
            expected_schema_sha256=str(alert.details.get("schema_sha256") or ""),
            expected_finding_count=alert.details.get("finding_count"),
            require_findings=True,
        )
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)

        write_agent_audit_event(
            client=client,
            action="fetch_schema_drift_context",
            status="success",
            agent_run_id=resolved_agent_run_id,
            alert_id=alert.alert_id,
            alert_key=alert.alert_key,
            tool_name=TOOL_NAME,
            duration_ms=duration_ms,
            input_payload=input_payload,
            output_payload={
                "contract_name": result["snapshot"]["contract_name"],
                "contract_version": result["snapshot"]["contract_version"],
                "snapshot_status": result["snapshot"]["snapshot_status"],
                "highest_severity": result["snapshot"]["highest_severity"],
                "finding_count": result["finding_count"],
                "visible_finding_count": result["visible_finding_count"],
                "findings_truncated": result["findings_truncated"],
            },
            sql=result["query"],
            row_count=result["visible_finding_count"],
        )

        logger.info(
            "Fetched schema drift context | run_id=%s table=%s findings=%d visible=%d truncated=%d",
            run_id,
            qualified_name,
            result["finding_count"],
            result["visible_finding_count"],
            result["findings_truncated"],
        )

        return result

    except Exception as exc:
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)
        audit_status = "blocked" if isinstance(exc, ValueError) else "failed"
        combined_sql = "\n\n".join(sql.strip() for sql in (snapshot_sql, findings_sql) if sql)

        logger.exception(
            "Failed to fetch schema drift context | table=%s error_type=%s",
            alert.table_name,
            type(exc).__name__,
        )

        write_agent_audit_event(
            client=client,
            action="fetch_schema_drift_context",
            status=audit_status,
            agent_run_id=resolved_agent_run_id,
            alert_id=alert.alert_id,
            alert_key=alert.alert_key,
            tool_name=TOOL_NAME,
            duration_ms=duration_ms,
            input_payload=input_payload,
            output_payload={"error_type": type(exc).__name__},
            error_message=str(exc),
            sql=combined_sql,
        )

        raise


def collect_schema_drift_evidence(
    alert: Alert,
    finding_limit: int = DEFAULT_FINDING_LIMIT,
    agent_run_id: UUID | str | None = None,
    clickhouse_host: str | None = None,
    clickhouse_port: int | None = None,
) -> EvidenceItem:
    """
    Convert persisted schema context into one typed triage evidence item.

    Args:
        alert: Loaded schema drift alert.
        finding_limit: Maximum finding rows attached to the report.
        agent_run_id: Optional agent run UUID for audit correlation.
        clickhouse_host: Optional ClickHouse host override.
        clickhouse_port: Optional ClickHouse HTTP port override.

    Returns:
        EvidenceItem grounded in exact detector run and affected table evidence.
    """
    result = fetch_schema_drift_context(
        alert=alert,
        finding_limit=finding_limit,
        agent_run_id=agent_run_id,
        clickhouse_host=clickhouse_host,
        clickhouse_port=clickhouse_port,
    )

    return EvidenceItem(
        evidence_type=EvidenceType.SCHEMA_DRIFT,
        tool_name=TOOL_NAME,
        description="Exact persisted schema snapshot and contract comparison findings for this alert.",
        query=result["query"],
        rows=result["findings"],
        row_count=result["visible_finding_count"],
        summary=result["summary"],
    )


# --- Defining CLI Helpers
def build_parser() -> argparse.ArgumentParser:
    """
    Build the command-line parser for schema evidence inspection.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(description="Fetch audited schema drift evidence for one alert.")

    parser.add_argument("--alert-key", required=True, help="Schema drift alert key to investigate.")
    parser.add_argument("--finding-limit", type=int, default=DEFAULT_FINDING_LIMIT, help="Maximum findings returned.")
    parser.add_argument("--agent-run-id", default=None, help="Optional agent run UUID.")
    parser.add_argument("--clickhouse-host", default=None, help="Optional ClickHouse host override.")
    parser.add_argument("--clickhouse-port", type=int, default=None, help="Optional ClickHouse HTTP port override.")

    return parser


def main() -> None:
    """
    Load one alert and print its typed schema drift evidence.

    Returns:
        None.
    """
    parser                = build_parser()
    args                  = parser.parse_args()
    resolved_agent_run_id = args.agent_run_id or str(uuid4())
    alert                 = load_alert(
        alert_key=args.alert_key,
        agent_run_id=resolved_agent_run_id,
        clickhouse_host=args.clickhouse_host,
        clickhouse_port=args.clickhouse_port,
    )
    evidence = collect_schema_drift_evidence(
        alert=alert,
        finding_limit=args.finding_limit,
        agent_run_id=resolved_agent_run_id,
        clickhouse_host=args.clickhouse_host,
        clickhouse_port=args.clickhouse_port,
    )

    print(evidence.model_dump_json(indent=2))


# --- Running CLI Entrypoint
if __name__ == "__main__":
    main()
