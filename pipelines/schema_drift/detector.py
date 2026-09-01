####
## Deterministic Schema Drift Detector for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Capture ClickHouse schemas and compare them with validated contracts."""

# --- Importing Libraries
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable
from uuid import UUID, uuid5

from pipelines.common.clickhouse import quote_sql_literal, split_table_name
from pipelines.common.logging import logger
from pipelines.schema_drift.config import (
    SchemaColumnConfig,
    SchemaContractConfig,
    SchemaTableConfig,
)
from pipelines.schema_drift.models import (
    SEVERITY_RANK,
    ObservedColumn,
    SchemaComparisonResult,
    SchemaDriftEvaluation,
    TableSchemaSnapshot,
)


# --- Defining Constants
SCHEMA_DRIFT_UUID_NAMESPACE = UUID("05af5f45-7be9-4a72-a027-50a1f4b6a9c2")
SAFE_RUN_ID_PATTERN         = re.compile(r"^[A-Za-z0-9_.:+-]{1,250}$")
MAX_COLUMNS_PER_TABLE       = 1000


# --- Defining Hash Helpers
def schema_contract_sha256(contract: SchemaContractConfig) -> str:
    """
    Hash normalized validated contract content.

    Args:
        contract: Validated schema contract.

    Returns:
        Lower-case SHA-256 digest.
    """
    payload = contract.model_dump(mode="json")
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")

    return hashlib.sha256(encoded).hexdigest()


def observed_schema_sha256(columns: Iterable[ObservedColumn]) -> str:
    """
    Hash one normalized observed schema.

    Args:
        columns: Ordered ClickHouse column observations.

    Returns:
        Lower-case SHA-256 digest.
    """
    payload = [column.as_dict() for column in columns]
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")

    return hashlib.sha256(encoded).hexdigest()


# --- Defining Validation Helpers
def validate_schema_run_id(run_id: str) -> str:
    """
    Validate an Airflow/CLI correlation identifier before persistence.

    Args:
        run_id: Candidate run identifier.

    Returns:
        Original validated run identifier.

    Raises:
        ValueError: If the identifier contains unsupported characters.
    """
    normalized = run_id.strip()

    if not SAFE_RUN_ID_PATTERN.fullmatch(normalized):
        raise ValueError("Schema drift run_id contains unsupported characters or length.")

    return normalized


def finding_status(severity: str) -> str:
    """
    Convert deterministic severity into a comparison status.

    Args:
        severity: info, warning, or critical severity.

    Returns:
        fail for critical findings, otherwise warn.
    """
    return "fail" if severity == "critical" else "warn"


# --- Defining ClickHouse Snapshot Functions
def fetch_observed_columns(client: Any, table: SchemaTableConfig) -> tuple[ObservedColumn, ...]:
    """
    Read one bounded ClickHouse schema from system.columns.

    Args:
        client: clickhouse-connect client.
        table: Validated table schema contract.

    Returns:
        Ordered observed columns. An empty tuple means the table was not found.
    """
    database_name, table_name = split_table_name(table.qualified_name)
    result = client.query(
        f"""
        SELECT
            position,
            name,
            type,
            default_kind,
            default_expression
        FROM system.columns
        WHERE database = {quote_sql_literal(database_name)}
          AND table = {quote_sql_literal(table_name)}
        ORDER BY position
        LIMIT {MAX_COLUMNS_PER_TABLE}
        """
    )

    columns = tuple(
        ObservedColumn(
            position=int(position),
            name=str(name),
            data_type=str(data_type).strip(),
            default_kind=str(default_kind or "").strip().upper(),
            default_expression=str(default_expression or "").strip(),
        )
        for position, name, data_type, default_kind, default_expression in result.result_rows
    )

    logger.info(
        "Captured ClickHouse schema | table=%s columns=%d",
        table.qualified_name,
        len(columns),
    )

    return columns


# --- Defining Comparison Helpers
def build_result(
    *,
    run_id: str,
    snapshot_id: UUID,
    observed_at: datetime,
    contract: SchemaContractConfig,
    contract_sha256: str,
    qualified_name: str,
    check_type: str,
    status: str,
    severity: str = "info",
    column_name: str = "",
    expected_value: str = "",
    actual_value: str = "",
    details: dict[str, Any] | None = None,
) -> SchemaComparisonResult:
    """
    Build one stable comparison result.

    Args:
        run_id: Correlation identifier.
        snapshot_id: Parent table snapshot UUID.
        observed_at: Shared UTC observation timestamp.
        contract: Validated schema contract.
        contract_sha256: Hash of normalized contract content.
        qualified_name: Compared table.
        check_type: Deterministic check category.
        status: pass, warn, fail, or skip.
        severity: Finding severity.
        column_name: Optional compared column.
        expected_value: Human-readable expected value.
        actual_value: Human-readable observed value.
        details: Optional structured evidence.

    Returns:
        SchemaComparisonResult with deterministic UUID.
    """
    identity = f"{run_id}|{qualified_name}|{check_type}|{column_name}"

    return SchemaComparisonResult(
        result_id=uuid5(SCHEMA_DRIFT_UUID_NAMESPACE, identity),
        snapshot_id=snapshot_id,
        run_id=run_id,
        observed_at=observed_at,
        contract_name=contract.contract_name,
        contract_version=contract.contract_version,
        contract_sha256=contract_sha256,
        qualified_name=qualified_name,
        column_name=column_name,
        check_type=check_type,
        status=status,
        severity=severity,
        expected_value=expected_value,
        actual_value=actual_value,
        details=details or {},
    )


def skipped_column_result(
    *,
    run_id: str,
    snapshot_id: UUID,
    observed_at: datetime,
    contract: SchemaContractConfig,
    contract_sha256: str,
    table: SchemaTableConfig,
    column: SchemaColumnConfig,
    check_type: str,
    expected_value: str,
) -> SchemaComparisonResult:
    """
    Build a non-finding result when a prerequisite column is missing.

    Args:
        run_id: Correlation identifier.
        snapshot_id: Parent table snapshot UUID.
        observed_at: Shared UTC observation timestamp.
        contract: Validated schema contract.
        contract_sha256: Contract hash.
        table: Parent table contract.
        column: Missing expected column.
        check_type: Dependent check category.
        expected_value: Value that could not be compared.

    Returns:
        Skipped SchemaComparisonResult retaining prerequisite evidence.
    """
    return build_result(
        run_id=run_id,
        snapshot_id=snapshot_id,
        observed_at=observed_at,
        contract=contract,
        contract_sha256=contract_sha256,
        qualified_name=table.qualified_name,
        column_name=column.name,
        check_type=check_type,
        status="skip",
        expected_value=expected_value,
        details={"reason": "column_presence_failed"},
    )


def compare_table_schema(
    *,
    run_id: str,
    observed_at: datetime,
    contract: SchemaContractConfig,
    contract_sha256: str,
    table: SchemaTableConfig,
    observed_columns: tuple[ObservedColumn, ...],
) -> tuple[TableSchemaSnapshot, tuple[SchemaComparisonResult, ...]]:
    """
    Compare one observed table schema with its explicit contract.

    Args:
        run_id: Correlation identifier.
        observed_at: Shared UTC observation timestamp.
        contract: Validated parent contract.
        contract_sha256: Hash of normalized contract content.
        table: Expected table definition.
        observed_columns: Ordered actual ClickHouse columns.

    Returns:
        Table snapshot and deterministic comparison rows.
    """
    database_name, table_name = split_table_name(table.qualified_name)
    snapshot_id = uuid5(SCHEMA_DRIFT_UUID_NAMESPACE, f"{run_id}|{table.qualified_name}")
    results: list[SchemaComparisonResult] = []

    if not observed_columns:
        severity = contract.policy.missing_table
        results.append(
            build_result(
                run_id=run_id,
                snapshot_id=snapshot_id,
                observed_at=observed_at,
                contract=contract,
                contract_sha256=contract_sha256,
                qualified_name=table.qualified_name,
                check_type="table_presence",
                status=finding_status(severity),
                severity=severity,
                expected_value="present",
                actual_value="missing",
            )
        )
    else:
        results.append(
            build_result(
                run_id=run_id,
                snapshot_id=snapshot_id,
                observed_at=observed_at,
                contract=contract,
                contract_sha256=contract_sha256,
                qualified_name=table.qualified_name,
                check_type="table_presence",
                status="pass",
                expected_value="present",
                actual_value="present",
            )
        )

        observed_by_name = {column.name: column for column in observed_columns}
        expected_names   = {column.name for column in table.columns}

        for expected_position, expected in enumerate(table.columns, start=1):
            actual = observed_by_name.get(expected.name)

            if actual is None:
                severity = contract.policy.missing_column
                results.append(
                    build_result(
                        run_id=run_id,
                        snapshot_id=snapshot_id,
                        observed_at=observed_at,
                        contract=contract,
                        contract_sha256=contract_sha256,
                        qualified_name=table.qualified_name,
                        column_name=expected.name,
                        check_type="column_presence",
                        status=finding_status(severity),
                        severity=severity,
                        expected_value="present",
                        actual_value="missing",
                    )
                )
                results.append(
                    skipped_column_result(
                        run_id=run_id,
                        snapshot_id=snapshot_id,
                        observed_at=observed_at,
                        contract=contract,
                        contract_sha256=contract_sha256,
                        table=table,
                        column=expected,
                        check_type="column_type",
                        expected_value=expected.data_type,
                    )
                )

                if table.check_column_order:
                    results.append(
                        skipped_column_result(
                            run_id=run_id,
                            snapshot_id=snapshot_id,
                            observed_at=observed_at,
                            contract=contract,
                            contract_sha256=contract_sha256,
                            table=table,
                            column=expected,
                            check_type="column_position",
                            expected_value=str(expected_position),
                        )
                    )

                if table.check_defaults:
                    results.append(
                        skipped_column_result(
                            run_id=run_id,
                            snapshot_id=snapshot_id,
                            observed_at=observed_at,
                            contract=contract,
                            contract_sha256=contract_sha256,
                            table=table,
                            column=expected,
                            check_type="column_default",
                            expected_value=json.dumps(
                                {
                                    "kind": expected.default_kind,
                                    "expression": expected.default_expression,
                                },
                                sort_keys=True,
                            ),
                        )
                    )

                continue

            results.append(
                build_result(
                    run_id=run_id,
                    snapshot_id=snapshot_id,
                    observed_at=observed_at,
                    contract=contract,
                    contract_sha256=contract_sha256,
                    qualified_name=table.qualified_name,
                    column_name=expected.name,
                    check_type="column_presence",
                    status="pass",
                    expected_value="present",
                    actual_value="present",
                )
            )

            type_matches = expected.data_type == actual.data_type
            type_severity = contract.policy.type_mismatch
            results.append(
                build_result(
                    run_id=run_id,
                    snapshot_id=snapshot_id,
                    observed_at=observed_at,
                    contract=contract,
                    contract_sha256=contract_sha256,
                    qualified_name=table.qualified_name,
                    column_name=expected.name,
                    check_type="column_type",
                    status="pass" if type_matches else finding_status(type_severity),
                    severity="info" if type_matches else type_severity,
                    expected_value=expected.data_type,
                    actual_value=actual.data_type,
                )
            )

            if table.check_column_order:
                position_matches = expected_position == actual.position
                position_severity = contract.policy.position_mismatch
                results.append(
                    build_result(
                        run_id=run_id,
                        snapshot_id=snapshot_id,
                        observed_at=observed_at,
                        contract=contract,
                        contract_sha256=contract_sha256,
                        qualified_name=table.qualified_name,
                        column_name=expected.name,
                        check_type="column_position",
                        status="pass" if position_matches else finding_status(position_severity),
                        severity="info" if position_matches else position_severity,
                        expected_value=str(expected_position),
                        actual_value=str(actual.position),
                    )
                )

            if table.check_defaults:
                expected_default = {
                    "kind": expected.default_kind,
                    "expression": expected.default_expression,
                }
                actual_default = {
                    "kind": actual.default_kind,
                    "expression": actual.default_expression,
                }
                default_matches  = expected_default == actual_default
                default_severity = contract.policy.default_mismatch
                results.append(
                    build_result(
                        run_id=run_id,
                        snapshot_id=snapshot_id,
                        observed_at=observed_at,
                        contract=contract,
                        contract_sha256=contract_sha256,
                        qualified_name=table.qualified_name,
                        column_name=expected.name,
                        check_type="column_default",
                        status="pass" if default_matches else finding_status(default_severity),
                        severity="info" if default_matches else default_severity,
                        expected_value=json.dumps(expected_default, sort_keys=True),
                        actual_value=json.dumps(actual_default, sort_keys=True),
                    )
                )

        unexpected = [column.name for column in observed_columns if column.name not in expected_names]
        unexpected_severity = contract.policy.unexpected_columns
        results.append(
            build_result(
                run_id=run_id,
                snapshot_id=snapshot_id,
                observed_at=observed_at,
                contract=contract,
                contract_sha256=contract_sha256,
                qualified_name=table.qualified_name,
                check_type="unexpected_columns",
                status="pass" if not unexpected else finding_status(unexpected_severity),
                severity="info" if not unexpected else unexpected_severity,
                expected_value="[]",
                actual_value=json.dumps(unexpected, ensure_ascii=True),
                details={"unexpected_columns": unexpected},
            )
        )

    findings = [result for result in results if result.is_finding]

    if findings:
        highest_severity = max(findings, key=lambda result: SEVERITY_RANK[result.severity]).severity
        status           = "fail" if highest_severity == "critical" else "warn"
    else:
        highest_severity = "info"
        status           = "pass"

    snapshot = TableSchemaSnapshot(
        snapshot_id=snapshot_id,
        run_id=run_id,
        observed_at=observed_at,
        contract_name=contract.contract_name,
        contract_version=contract.contract_version,
        contract_sha256=contract_sha256,
        qualified_name=table.qualified_name,
        database_name=database_name,
        table_name=table_name,
        schema_sha256=observed_schema_sha256(observed_columns),
        status=status,
        highest_severity=highest_severity,
        comparison_count=len(results),
        finding_count=len(findings),
        columns=observed_columns,
    )

    logger.info(
        "Compared table schema | table=%s status=%s comparisons=%d findings=%d",
        table.qualified_name,
        status,
        len(results),
        len(findings),
    )

    return snapshot, tuple(results)


# --- Defining Evaluation Function
def evaluate_schema_contract(
    client: Any,
    contract: SchemaContractConfig,
    run_id: str,
    observed_at: datetime | None = None,
) -> SchemaDriftEvaluation:
    """
    Capture and compare every table in a validated schema contract.

    Args:
        client: clickhouse-connect client.
        contract: Validated schema contract.
        run_id: Airflow or CLI run correlation identifier.
        observed_at: Optional deterministic UTC timestamp used by tests.

    Returns:
        Complete SchemaDriftEvaluation ready for persistence.
    """
    validated_run_id = validate_schema_run_id(run_id)
    timestamp        = observed_at or datetime.now(timezone.utc)

    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    else:
        timestamp = timestamp.astimezone(timezone.utc)

    contract_hash = schema_contract_sha256(contract)
    snapshots: list[TableSchemaSnapshot]      = []
    results: list[SchemaComparisonResult]     = []

    logger.info(
        "Starting deterministic schema evaluation | run_id=%s contract=%s tables=%d",
        validated_run_id,
        contract.contract_name,
        len(contract.tables),
    )

    for table in contract.tables:
        observed = fetch_observed_columns(client=client, table=table)
        snapshot, table_results = compare_table_schema(
            run_id=validated_run_id,
            observed_at=timestamp,
            contract=contract,
            contract_sha256=contract_hash,
            table=table,
            observed_columns=observed,
        )
        snapshots.append(snapshot)
        results.extend(table_results)

    evaluation = SchemaDriftEvaluation(
        run_id=validated_run_id,
        observed_at=timestamp,
        contract_name=contract.contract_name,
        contract_sha256=contract_hash,
        fail_on_severity=contract.policy.fail_on_severity,
        snapshots=tuple(snapshots),
        results=tuple(results),
    )

    logger.info("Schema evaluation completed | summary=%s", json.dumps(evaluation.as_dict(), sort_keys=True))

    return evaluation
