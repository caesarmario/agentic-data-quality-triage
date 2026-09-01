####
## Incident History Tool for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Read bounded prior investigation outcomes as auditable triage evidence."""

# --- Importing Libraries
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Add repo root before importing project packages when this file is executed by path.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.context.models import IncidentMemoryRecord
from agent.context.store import (
    DEFAULT_MEMORY_LOOKBACK,
    build_incident_memory_query,
    fetch_incident_memory,
)
from agent.state import Alert, EvidenceItem, EvidenceType
from agent.tools.alerts import load_alert
from agent.tools.audit_log import write_agent_audit_event
from pipelines.common.clickhouse import build_clickhouse_client
from pipelines.common.logging import logger


# --- Defining Constants
TOOL_NAME                    = "incident_history"
DEFAULT_INCIDENT_HISTORY_LIMIT = 10
MAX_EVIDENCE_SUMMARY_CHARS   = 600
MAX_EVIDENCE_TYPE_LABELS     = 8


# --- Defining Normalization Helpers
def enum_value(value: Any) -> str:
    """
    Normalize enum-like values into stable JSON-safe text.

    Args:
        value: Enum, string, or nullable scalar.

    Returns:
        Stable string value without exposing object representations.
    """
    if isinstance(value, Enum):
        return str(value.value)

    return str(value or "")


def bounded_text(value: Any, max_length: int) -> str:
    """
    Collapse whitespace and cap one operator-facing text field.

    Args:
        value: Raw text-like value.
        max_length: Maximum number of characters retained.

    Returns:
        Single-line bounded text.
    """
    return " ".join(str(value or "").split())[:max_length]


def normalize_confidence(value: Any) -> float | None:
    """
    Accept only a numeric confidence inside the supported zero-to-one range.

    Args:
        value: Candidate confidence from bounded decision facts.

    Returns:
        Rounded confidence, or None when the value is absent or invalid.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None

    confidence = float(value)

    if not 0.0 <= confidence <= 1.0:
        return None

    return round(confidence, 4)


def resolve_alert_reference(alert: Alert) -> str:
    """
    Select the strongest exact alert identity for incident-memory lookup.

    Args:
        alert: Loaded alert being investigated.

    Returns:
        Canonical alert key, then Alert Ref, then alert UUID as a fallback.

    Raises:
        ValueError: If no exact alert identity is available.
    """
    candidates = (
        alert.alert_key,
        alert.alert_display_id,
        str(alert.alert_id or ""),
    )

    for candidate in candidates:
        normalized = candidate.strip()

        if normalized:
            return normalized

    raise ValueError("Alert identity is required to collect incident history evidence.")


def incident_memory_to_evidence_row(record: IncidentMemoryRecord) -> dict[str, Any]:
    """
    Convert durable memory into a bounded evidence row without raw decisions.

    Args:
        record: Validated durable incident-memory record.

    Returns:
        Operator-safe evidence facts suitable for reports and LLM context.
    """
    decision_facts = record.decision_facts
    evidence_types = sorted(
        {
            bounded_text(reference.evidence_type, 80)
            for reference in record.evidence_references
            if bounded_text(reference.evidence_type, 80)
        }
    )[:MAX_EVIDENCE_TYPE_LABELS]

    return {
        "memory_id": str(record.memory_id),
        "parent_run_id": str(record.parent_run_id),
        "recorded_at": record.recorded_at.isoformat(),
        "memory_type": enum_value(record.memory_type),
        "alert_display_id": bounded_text(record.alert_display_id, 40),
        "outcome_status": enum_value(record.outcome_status),
        "specialist_name": bounded_text(record.specialist_name, 80),
        "task_type": bounded_text(record.task_type, 80),
        "summary": bounded_text(record.summary, MAX_EVIDENCE_SUMMARY_CHARS),
        "confidence": normalize_confidence(decision_facts.get("confidence")),
        "top_hypothesis_category": bounded_text(
            decision_facts.get("top_hypothesis_category"),
            80,
        ),
        "report_id": bounded_text(decision_facts.get("report_id"), 40),
        "evidence_reference_count": len(record.evidence_references),
        "evidence_types": evidence_types,
        "report_s3_uri": record.report_s3_uri,
        "approval_state": enum_value(record.approval_state),
        "resolution_reference": bounded_text(record.resolution_reference, 500),
    }


def summarize_recurrence(rows: list[dict[str, Any]]) -> dict[str, int]:
    """
    Count prior likely-cause categories without treating them as current truth.

    Args:
        rows: Sanitized prior investigation rows.

    Returns:
        Deterministically ordered category-to-count mapping.
    """
    counts = Counter(
        str(row.get("top_hypothesis_category") or "")
        for row in rows
        if row.get("top_hypothesis_category")
    )

    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


# --- Defining Read And Audit Functions
def fetch_incident_history(
    alert_reference: str,
    lookback_days: int = DEFAULT_MEMORY_LOOKBACK,
    limit: int = DEFAULT_INCIDENT_HISTORY_LIMIT,
    agent_run_id: UUID | str | None = None,
    alert_id: UUID | str | None = None,
    alert_key: str = "",
    clickhouse_host: str | None = None,
    clickhouse_port: int | None = None,
) -> dict[str, Any]:
    """
    Fetch exact-match prior investigations and audit the bounded read.

    Args:
        alert_reference: Canonical alert key, human Alert Ref, or alert UUID.
        lookback_days: Mandatory recent timestamp window.
        limit: Maximum durable-memory rows returned.
        agent_run_id: Optional triage run UUID for audit correlation.
        alert_id: Optional alert UUID for audit correlation.
        alert_key: Canonical system alert key for audit correlation.
        clickhouse_host: Optional ClickHouse host override.
        clickhouse_port: Optional ClickHouse HTTP port override.

    Returns:
        Sanitized prior outcomes, recurrence counts, and bounded query metadata.
    """
    client                = build_clickhouse_client(host=clickhouse_host, port=clickhouse_port)
    resolved_agent_run_id = UUID(str(agent_run_id)) if agent_run_id else uuid4()
    started_monotonic     = time.monotonic()
    sql                   = ""

    try:
        sql     = build_incident_memory_query(
            alert_reference=alert_reference,
            lookback_days=lookback_days,
            limit=limit,
        )
        records = fetch_incident_memory(
            client=client,
            alert_reference=alert_reference,
            lookback_days=lookback_days,
            limit=limit,
        )
        rows              = [incident_memory_to_evidence_row(record) for record in records]
        recurrence_counts = summarize_recurrence(rows)
        duration_ms       = int((time.monotonic() - started_monotonic) * 1000)
        report_count      = sum(bool(row.get("report_s3_uri")) for row in rows)
        latest_recorded_at = str(rows[0].get("recorded_at", "")) if rows else ""

        write_agent_audit_event(
            client=client,
            action="fetch_incident_history",
            status="success",
            agent_run_id=resolved_agent_run_id,
            alert_id=alert_id,
            alert_key=alert_key,
            tool_name=TOOL_NAME,
            duration_ms=duration_ms,
            input_payload={
                "identity_type": "exact_alert_reference",
                "lookback_days": lookback_days,
                "limit": limit,
            },
            output_payload={
                "row_count": len(rows),
                "recurrence_counts": recurrence_counts,
                "report_count": report_count,
                "latest_recorded_at": latest_recorded_at,
            },
            sql=sql,
            row_count=len(rows),
        )

        logger.info(
            "Fetched incident history | alert_ref=%s lookback_days=%d rows=%d recurrence=%s",
            alert_reference,
            lookback_days,
            len(rows),
            recurrence_counts,
        )

        return {
            "status": "success",
            "alert_reference": alert_reference,
            "lookback_days": lookback_days,
            "limit": limit,
            "rows": rows,
            "row_count": len(rows),
            "recurrence_counts": recurrence_counts,
            "report_count": report_count,
            "latest_recorded_at": latest_recorded_at,
            "sql": sql,
        }

    except Exception as exc:
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)
        logger.exception(
            "Failed to fetch incident history | alert_ref=%s lookback_days=%d",
            alert_reference,
            lookback_days,
        )

        write_agent_audit_event(
            client=client,
            action="fetch_incident_history",
            status="failed",
            agent_run_id=resolved_agent_run_id,
            alert_id=alert_id,
            alert_key=alert_key,
            tool_name=TOOL_NAME,
            duration_ms=duration_ms,
            input_payload={
                "identity_type": "exact_alert_reference",
                "lookback_days": lookback_days,
                "limit": limit,
            },
            output_payload={"error_type": type(exc).__name__},
            error_message=str(exc),
            sql=sql,
        )

        raise


def build_incident_history_summary(result: dict[str, Any]) -> str:
    """
    Build a cautious human-readable comparison summary from prior outcomes.

    Args:
        result: Sanitized incident-history result.

    Returns:
        Summary that describes prior patterns without declaring current causality.
    """
    rows = list(result.get("rows") or [])

    if not rows:
        return "No previous durable investigation was found for this exact alert identity."

    latest           = rows[0]
    latest_category  = str(latest.get("top_hypothesis_category") or "not recorded")
    latest_confidence = latest.get("confidence")
    confidence_text  = (
        f" at {float(latest_confidence):.0%} confidence"
        if isinstance(latest_confidence, (int, float))
        else ""
    )
    recurrence_counts = dict(result.get("recurrence_counts") or {})
    recurrence_text   = ", ".join(
        f"{category} ({count})"
        for category, count in recurrence_counts.items()
    ) or "none recorded"

    return (
        f"Found {len(rows)} previous investigation(s) for this exact alert identity. "
        f"The latest prior likely-cause category was {latest_category}{confidence_text}. "
        f"Prior category counts: {recurrence_text}. These outcomes are comparison context, "
        "not proof of the current root cause."
    )


def collect_incident_history_evidence(
    alert: Alert,
    lookback_days: int = DEFAULT_MEMORY_LOOKBACK,
    limit: int = DEFAULT_INCIDENT_HISTORY_LIMIT,
    agent_run_id: UUID | str | None = None,
    clickhouse_host: str | None = None,
    clickhouse_port: int | None = None,
) -> EvidenceItem:
    """
    Build one bounded prior-investigation evidence item for the loaded alert.

    Args:
        alert: Alert being investigated.
        lookback_days: Mandatory recent timestamp window.
        limit: Maximum prior outcomes retained in the evidence item.
        agent_run_id: Optional triage run UUID for audit correlation.
        clickhouse_host: Optional ClickHouse host override.
        clickhouse_port: Optional ClickHouse HTTP port override.

    Returns:
        EvidenceItem containing sanitized prior outcome facts and report links.
    """
    alert_reference = resolve_alert_reference(alert)
    result          = fetch_incident_history(
        alert_reference=alert_reference,
        lookback_days=lookback_days,
        limit=limit,
        agent_run_id=agent_run_id,
        alert_id=alert.alert_id,
        alert_key=alert.alert_key,
        clickhouse_host=clickhouse_host,
        clickhouse_port=clickhouse_port,
    )
    latest_report_uri = next(
        (
            str(row.get("report_s3_uri"))
            for row in result["rows"]
            if row.get("report_s3_uri")
        ),
        "",
    )

    return EvidenceItem(
        evidence_type=EvidenceType.INCIDENT_HISTORY,
        tool_name=TOOL_NAME,
        description=(
            "Exact-match prior investigation outcomes used only as bounded comparison context."
        ),
        query=result["sql"],
        rows=result["rows"],
        row_count=result["row_count"],
        summary=build_incident_history_summary(result),
        s3_uri=latest_report_uri,
    )


# --- Defining CLI
def build_parser() -> argparse.ArgumentParser:
    """
    Build the command-line parser for audited incident-history lookup.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(
        description="Collect bounded prior investigation evidence for one alert."
    )

    parser.add_argument("--alert-key", required=True, help="System alert key or Alert Ref.")
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=DEFAULT_MEMORY_LOOKBACK,
        help="Mandatory historical lookback window.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_INCIDENT_HISTORY_LIMIT,
        help="Maximum prior outcomes returned.",
    )
    parser.add_argument("--agent-run-id", default=None, help="Optional agent run UUID.")
    parser.add_argument("--clickhouse-host", default=None, help="Optional ClickHouse host override.")
    parser.add_argument("--clickhouse-port", type=int, default=None, help="Optional ClickHouse HTTP port override.")

    return parser


def main() -> None:
    """
    Load one alert and print its sanitized prior-investigation evidence.

    Returns:
        None.
    """
    args                  = build_parser().parse_args()
    resolved_agent_run_id = args.agent_run_id or str(uuid4())
    alert                 = load_alert(
        alert_key=args.alert_key,
        agent_run_id=resolved_agent_run_id,
        clickhouse_host=args.clickhouse_host,
        clickhouse_port=args.clickhouse_port,
    )
    evidence = collect_incident_history_evidence(
        alert=alert,
        lookback_days=args.lookback_days,
        limit=args.limit,
        agent_run_id=resolved_agent_run_id,
        clickhouse_host=args.clickhouse_host,
        clickhouse_port=args.clickhouse_port,
    )

    print(json.dumps(evidence.model_dump(mode="json"), indent=2, sort_keys=True))


# --- Running CLI Entrypoint
if __name__ == "__main__":
    main()
