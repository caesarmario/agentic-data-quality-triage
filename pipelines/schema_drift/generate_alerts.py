####
## Schema Drift Alert Generator for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Convert persisted schema drift evidence into grouped, idempotent alerts."""

# --- Importing Libraries
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Sequence


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[2]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipelines.common.alert_identity import build_alert_ref
from pipelines.common.alerts import ALERTS_TABLE, AlertCandidate, insert_alert_rows
from pipelines.common.clickhouse import build_clickhouse_client, quote_sql_literal, scalar
from pipelines.common.logging import logger
from pipelines.schema_drift.config import SCHEMA_CONTRACT_NAMES, SchemaContractConfig, load_named_schema_contract
from pipelines.schema_drift.detector import validate_schema_run_id
from pipelines.schema_drift.models import SEVERITY_RANK
from pipelines.schema_drift.storage import (
    MAX_SCHEMA_RESULTS,
    MAX_SCHEMA_TABLES,
    SCHEMA_DRIFT_RESULTS_TABLE,
    SCHEMA_SNAPSHOTS_TABLE,
    clickhouse_text,
)
from pipelines.seeding.helpers import iter_dates, parse_date


# --- Defining Constants
SCHEMA_ALERT_TYPE   = "schema_drift"
SCHEMA_ALERT_METRIC = "schema_contract_drift"
OPEN_ALERT_STATUS   = "open"

MAX_ALERT_FINDINGS_PER_TABLE = 25
MAX_EVIDENCE_VALUE_LENGTH     = 500


# --- Defining Data Models
@dataclass(frozen=True)
class SchemaDriftFinding:
    """
    Represent one warning or failure loaded from persisted schema evidence.

    Attributes:
        qualified_name: Fully qualified affected ClickHouse table.
        column_name: Affected column, or an empty string for table-level drift.
        check_type: Deterministic schema comparison category.
        status: Persisted warn or fail result.
        severity: Operational finding severity.
        expected_value: Bounded expected contract value.
        actual_value: Bounded observed value.
        details: Parsed deterministic comparison details.
    """

    qualified_name: str
    column_name: str
    check_type: str
    status: str
    severity: str
    expected_value: str
    actual_value: str
    details: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        """
        Serialize this finding for alert evidence.

        Returns:
            JSON-safe finding dictionary.
        """
        return {
            "column_name": self.column_name,
            "check_type": self.check_type,
            "status": self.status,
            "severity": self.severity,
            "expected_value": self.expected_value,
            "actual_value": self.actual_value,
            "details": self.details,
        }


@dataclass(frozen=True)
class SchemaDriftTableEvidence:
    """
    Group persisted schema findings for one affected table.

    Attributes:
        contract_name: Stable schema contract name.
        contract_version: Contract version used by the detector.
        contract_sha256: Exact validated contract hash.
        qualified_name: Fully qualified affected table.
        schema_sha256: Hash of the observed physical schema.
        snapshot_status: Aggregate pass, warn, or fail snapshot status.
        highest_severity: Highest persisted finding severity.
        finding_count: Finding count recorded by the snapshot.
        findings: Warning and failure details for this table.
    """

    contract_name: str
    contract_version: int
    contract_sha256: str
    qualified_name: str
    schema_sha256: str
    snapshot_status: str
    highest_severity: str
    finding_count: int
    findings: tuple[SchemaDriftFinding, ...]


# --- Defining Parsing Helpers
def bounded_text(value: Any, limit: int = MAX_EVIDENCE_VALUE_LENGTH) -> str:
    """
    Normalize a ClickHouse value into bounded user-safe evidence text.

    Args:
        value: Raw ClickHouse value.
        limit: Maximum retained character count.

    Returns:
        Normalized text, truncated with an explicit suffix when necessary.
    """
    normalized = clickhouse_text(value)

    if len(normalized) <= limit:
        return normalized

    return f"{normalized[: limit - 14]}...[truncated]"


def parse_details_json(value: Any) -> dict[str, Any]:
    """
    Parse persisted comparison details without failing alert generation.

    Args:
        value: Raw details_json value from ClickHouse.

    Returns:
        Parsed dictionary, or an empty dictionary for malformed/non-object JSON.
    """
    normalized = clickhouse_text(value)

    if not normalized:
        return {}

    try:
        parsed = json.loads(normalized)
    except json.JSONDecodeError:
        logger.warning("Ignoring malformed schema drift details JSON | value=%s", normalized[:200])

        return {}

    return parsed if isinstance(parsed, dict) else {}


def resolve_alert_date(
    dt: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> date:
    """
    Resolve one alert correlation date from daily or backfill arguments.

    Args:
        dt: Optional single business date in YYYY-MM-DD format.
        start: Optional inclusive backfill start date.
        end: Optional inclusive backfill end date.

    Returns:
        Single date used for alert lookup and operator correlation. For a backfill,
        this is the inclusive end date because one schema evaluation covers the run.

    Raises:
        ValueError: If date arguments are incomplete or mutually exclusive.
    """
    if dt and (start or end):
        raise ValueError("Use either --dt or --start/--end, not both.")

    if dt:
        return parse_date(dt)

    if not start or not end:
        raise ValueError("Provide --dt or both --start and --end.")

    dates = iter_dates(start_date=parse_date(start), end_date=parse_date(end))

    return dates[-1]


# --- Loading Persisted Evidence
def fetch_schema_drift_evidence(client: Any, run_id: str) -> list[SchemaDriftTableEvidence]:
    """
    Load bounded schema snapshots and findings for one detector run.

    Args:
        client: clickhouse-connect client instance.
        run_id: Airflow run identifier used by schema detection.

    Returns:
        Grouped evidence for tables with at least one finding.

    Raises:
        RuntimeError: If evidence exceeds bounds or findings have no parent snapshot.
    """
    validated_run_id = validate_schema_run_id(run_id)
    run_literal      = quote_sql_literal(validated_run_id)

    snapshot_rows = client.query(
        f"""
        SELECT
            contract_name,
            contract_version,
            contract_sha256,
            qualified_name,
            schema_sha256,
            status,
            highest_severity,
            finding_count
        FROM {SCHEMA_SNAPSHOTS_TABLE} FINAL
        WHERE run_id = {run_literal}
          AND finding_count > 0
        ORDER BY qualified_name
        LIMIT {MAX_SCHEMA_TABLES + 1}
        """
    ).result_rows

    if len(snapshot_rows) > MAX_SCHEMA_TABLES:
        raise RuntimeError(f"Schema drift snapshot evidence exceeds limit {MAX_SCHEMA_TABLES}.")

    finding_rows = client.query(
        f"""
        SELECT
            qualified_name,
            column_name,
            check_type,
            status,
            severity,
            expected_value,
            actual_value,
            details_json
        FROM {SCHEMA_DRIFT_RESULTS_TABLE} FINAL
        WHERE run_id = {run_literal}
          AND status IN ('warn', 'fail')
        ORDER BY qualified_name, check_type, column_name
        LIMIT {MAX_SCHEMA_RESULTS + 1}
        """
    ).result_rows

    if len(finding_rows) > MAX_SCHEMA_RESULTS:
        raise RuntimeError(f"Schema drift finding evidence exceeds limit {MAX_SCHEMA_RESULTS}.")

    findings_by_table: dict[str, list[SchemaDriftFinding]] = {}

    for row in finding_rows:
        qualified_name = clickhouse_text(row[0])
        finding = SchemaDriftFinding(
            qualified_name=qualified_name,
            column_name=clickhouse_text(row[1]),
            check_type=clickhouse_text(row[2]),
            status=clickhouse_text(row[3]),
            severity=clickhouse_text(row[4]),
            expected_value=bounded_text(row[5]),
            actual_value=bounded_text(row[6]),
            details=parse_details_json(row[7]),
        )
        findings_by_table.setdefault(qualified_name, []).append(finding)

    evidence: list[SchemaDriftTableEvidence] = []

    for row in snapshot_rows:
        qualified_name = clickhouse_text(row[3])
        findings      = tuple(findings_by_table.pop(qualified_name, []))

        if not findings:
            raise RuntimeError(f"Schema snapshot has finding_count but no persisted findings: {qualified_name}")

        if len(findings) != int(row[7]):
            raise RuntimeError(
                f"Schema snapshot finding_count mismatch: {qualified_name}; "
                f"snapshot={int(row[7])}; persisted={len(findings)}"
            )

        evidence.append(
            SchemaDriftTableEvidence(
                contract_name=clickhouse_text(row[0]),
                contract_version=int(row[1]),
                contract_sha256=clickhouse_text(row[2]),
                qualified_name=qualified_name,
                schema_sha256=clickhouse_text(row[4]),
                snapshot_status=clickhouse_text(row[5]),
                highest_severity=clickhouse_text(row[6]),
                finding_count=int(row[7]),
                findings=findings,
            )
        )

    if findings_by_table:
        orphan_tables = ", ".join(sorted(findings_by_table))
        raise RuntimeError(f"Schema drift findings are missing parent snapshots: {orphan_tables}")

    logger.info(
        "Fetched grouped schema drift evidence | run_id=%s tables=%d findings=%d",
        validated_run_id,
        len(evidence),
        len(finding_rows),
    )

    return evidence


# --- Building Alert Candidates
def build_schema_fingerprint(evidence: SchemaDriftTableEvidence) -> str:
    """
    Build a stable fingerprint for one contract and observed table schema.

    Args:
        evidence: Grouped table evidence.

    Returns:
        SHA-256 fingerprint used to deduplicate an unresolved drift episode.
    """
    identity = {
        "qualified_name": evidence.qualified_name,
        "contract_sha256": evidence.contract_sha256,
        "schema_sha256": evidence.schema_sha256,
    }
    encoded = json.dumps(identity, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")

    return hashlib.sha256(encoded).hexdigest()


def build_schema_alert_key(
    dataset: str,
    alert_dt: date,
    qualified_name: str,
    schema_fingerprint: str,
) -> str:
    """
    Build a stable daily alert key while retaining a cross-day drift fingerprint.

    Args:
        dataset: Data product name from the schema contract.
        alert_dt: Business date correlated with this detection run.
        qualified_name: Fully qualified affected table.
        schema_fingerprint: Full deterministic schema incident fingerprint.

    Returns:
        Stable system alert key for same-day Airflow retries.
    """
    return "|".join(
        (
            dataset,
            SCHEMA_ALERT_TYPE,
            alert_dt.isoformat(),
            qualified_name,
            SCHEMA_ALERT_METRIC,
            schema_fingerprint[:16],
        )
    )


def build_schema_alert_candidates(
    contract: SchemaContractConfig,
    evidence_rows: list[SchemaDriftTableEvidence],
    alert_dt: date,
    run_id: str,
) -> list[AlertCandidate]:
    """
    Convert grouped schema findings into one alert candidate per affected table.

    Args:
        contract: Validated schema contract.
        evidence_rows: Grouped persisted findings.
        alert_dt: Correlation date shown to operators.
        run_id: Source Airflow run identifier.

    Returns:
        Grouped schema drift alert candidates.
    """
    candidates: list[AlertCandidate] = []

    for evidence in evidence_rows:
        fingerprint = build_schema_fingerprint(evidence)
        alert_key   = build_schema_alert_key(
            dataset=contract.dataset,
            alert_dt=alert_dt,
            qualified_name=evidence.qualified_name,
            schema_fingerprint=fingerprint,
        )
        highest_severity = max(
            (finding.severity for finding in evidence.findings),
            key=lambda severity: SEVERITY_RANK.get(severity, -1),
        )
        visible_findings = evidence.findings[:MAX_ALERT_FINDINGS_PER_TABLE]
        details = {
            "summary": (
                f"Detected {evidence.finding_count} schema contract finding(s) "
                f"for {evidence.qualified_name}."
            ),
            "schema_fingerprint": fingerprint,
            "source_schema_run_id": run_id,
            "contract_name": evidence.contract_name,
            "contract_version": evidence.contract_version,
            "contract_sha256": evidence.contract_sha256,
            "schema_sha256": evidence.schema_sha256,
            "snapshot_status": evidence.snapshot_status,
            "highest_severity": highest_severity,
            "finding_count": evidence.finding_count,
            "finding_types": sorted({finding.check_type for finding in evidence.findings}),
            "findings": [finding.as_dict() for finding in visible_findings],
            "findings_truncated": max(0, len(evidence.findings) - len(visible_findings)),
        }

        candidates.append(
            AlertCandidate(
                alert_key=alert_key,
                alert_display_id=build_alert_ref(alert_key=alert_key, dt=alert_dt),
                status=OPEN_ALERT_STATUS,
                alert_type=SCHEMA_ALERT_TYPE,
                severity=highest_severity,
                table_name=evidence.qualified_name,
                metric=SCHEMA_ALERT_METRIC,
                dt=alert_dt,
                dimension="",
                observed_value=float(evidence.finding_count),
                expected_value=0.0,
                threshold_value=0.0,
                source_check_run_id=None,
                details=details,
            )
        )

    logger.info(
        "Built grouped schema drift alert candidates | run_id=%s dt=%s candidates=%d",
        run_id,
        alert_dt,
        len(candidates),
    )

    return candidates


# --- Applying Alert Idempotency
def open_schema_alert_exists(client: Any, candidate: AlertCandidate) -> bool:
    """
    Check whether the same unresolved schema fingerprint is already open.

    Args:
        client: clickhouse-connect client instance.
        candidate: Schema drift alert candidate containing fingerprint evidence.

    Returns:
        True when an open alert already tracks this exact contract/schema state.
    """
    fingerprint = str(candidate.details.get("schema_fingerprint") or "")

    if not fingerprint:
        raise ValueError("Schema drift alert candidate is missing schema_fingerprint.")

    existing_count = scalar(
        client=client,
        query=f"""
            SELECT count()
            FROM {ALERTS_TABLE}
            WHERE status = {quote_sql_literal(OPEN_ALERT_STATUS)}
              AND alert_type = {quote_sql_literal(SCHEMA_ALERT_TYPE)}
              AND table_name = {quote_sql_literal(candidate.table_name)}
              AND JSONExtractString(details_json, 'schema_fingerprint') = {quote_sql_literal(fingerprint)}
        """,
        default=0,
    )

    return int(existing_count or 0) > 0


def insert_new_schema_alerts(client: Any, candidates: list[AlertCandidate]) -> dict[str, Any]:
    """
    Insert only schema candidates that do not match an unresolved fingerprint.

    Args:
        client: clickhouse-connect client instance.
        candidates: Grouped table-level schema alert candidates.

    Returns:
        Insert and deduplication counts with skipped human alert references.
    """
    new_candidates: list[AlertCandidate] = []
    skipped_refs: list[str]              = []

    for candidate in candidates:
        if open_schema_alert_exists(client=client, candidate=candidate):
            logger.info(
                "Skipping existing unresolved schema drift alert | alert_ref=%s table=%s",
                candidate.alert_display_id,
                candidate.table_name,
            )
            skipped_refs.append(candidate.alert_display_id)
            continue

        new_candidates.append(candidate)

    inserted = insert_alert_rows(client=client, candidates=new_candidates)

    return {
        "inserted": inserted,
        "skipped_existing": len(skipped_refs),
        "skipped_alert_refs": skipped_refs,
    }


# --- Running Alert Generation
def run_schema_alert_generation(
    contract_name: str,
    run_id: str,
    alert_dt: date,
    clickhouse_host: str | None = None,
    clickhouse_port: int | None = None,
) -> dict[str, Any]:
    """
    Generate grouped schema drift alerts from one persisted detection run.

    Args:
        contract_name: Allowlisted schema contract alias.
        run_id: Source Airflow run identifier.
        alert_dt: Business date used for operator correlation.
        clickhouse_host: Optional ClickHouse host override.
        clickhouse_port: Optional ClickHouse HTTP port override.

    Returns:
        JSON-safe alert generation summary.
    """
    validated_run_id = validate_schema_run_id(run_id)
    contract, _      = load_named_schema_contract(contract_name)
    client           = build_clickhouse_client(host=clickhouse_host, port=clickhouse_port)
    started          = time.monotonic()

    logger.info(
        "Starting schema drift alert generation | contract=%s run_id=%s dt=%s",
        contract_name,
        validated_run_id,
        alert_dt,
    )

    try:
        evidence   = fetch_schema_drift_evidence(client=client, run_id=validated_run_id)
        candidates = build_schema_alert_candidates(
            contract=contract,
            evidence_rows=evidence,
            alert_dt=alert_dt,
            run_id=validated_run_id,
        )
        writes = insert_new_schema_alerts(client=client, candidates=candidates)
    finally:
        close = getattr(client, "close", None)

        if callable(close):
            close()

    summary = {
        "status": "success",
        "contract_alias": contract_name,
        "run_id": validated_run_id,
        "alert_dt": alert_dt.isoformat(),
        "affected_tables": len(evidence),
        "candidates": len(candidates),
        "inserted": writes["inserted"],
        "skipped_existing": writes["skipped_existing"],
        "duration_ms": int((time.monotonic() - started) * 1000),
    }

    logger.info("Schema drift alert generation completed | summary=%s", summary)

    return summary


# --- Defining CLI Functions
def build_parser() -> argparse.ArgumentParser:
    """
    Build the bounded schema drift alert command parser.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(description="Generate grouped alerts from persisted schema drift evidence.")
    parser.add_argument("--contract", default="orders", choices=SCHEMA_CONTRACT_NAMES)
    parser.add_argument("--run-id", required=True, help="Schema detector Airflow run identifier.")
    parser.add_argument("--dt", default=None, help="Single correlation date in YYYY-MM-DD format.")
    parser.add_argument("--start", default=None, help="Inclusive backfill start date.")
    parser.add_argument("--end", default=None, help="Inclusive backfill end date.")
    parser.add_argument("--clickhouse-host", default=None, help="Optional ClickHouse host override.")
    parser.add_argument("--clickhouse-port", type=int, default=None, help="Optional ClickHouse HTTP port override.")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """
    Parse CLI arguments and emit an Airflow-friendly JSON summary.

    Args:
        argv: Optional argument sequence used by tests.

    Returns:
        Zero when evidence was processed successfully.
    """
    parser = build_parser()
    args   = parser.parse_args(argv)

    try:
        alert_dt = resolve_alert_date(dt=args.dt, start=args.start, end=args.end)
    except ValueError as exc:
        parser.error(str(exc))

    summary = run_schema_alert_generation(
        contract_name=args.contract,
        run_id=args.run_id,
        alert_dt=alert_dt,
        clickhouse_host=args.clickhouse_host,
        clickhouse_port=args.clickhouse_port,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))

    return 0


# --- Running CLI Entrypoint
if __name__ == "__main__":
    raise SystemExit(main())
