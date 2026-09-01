####
## Schema Drift Persistence for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Persist and verify append-versioned schema drift evidence in ClickHouse."""

# --- Importing Libraries
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pipelines.common.clickhouse import quote_sql_literal
from pipelines.common.logging import logger
from pipelines.schema_drift.config import SchemaContractConfig
from pipelines.schema_drift.detector import schema_contract_sha256, validate_schema_run_id
from pipelines.schema_drift.models import (
    SEVERITY_RANK,
    SchemaDriftEvaluation,
    SchemaDriftVerification,
)


# --- Defining Constants
PROJECT_ROOT = Path(__file__).resolve().parents[2]

SCHEMA_DRIFT_DDL_PATH     = PROJECT_ROOT / "infra" / "init" / "clickhouse" / "06_schema_drift_tables.sql"
SCHEMA_SNAPSHOTS_TABLE    = "dq.schema_snapshots"
SCHEMA_DRIFT_RESULTS_TABLE = "dq.schema_drift_results"

MAX_SCHEMA_TABLES          = 200
MAX_SCHEMA_RESULTS         = 20000

SCHEMA_SNAPSHOT_COLUMNS = (
    "snapshot_id",
    "run_id",
    "observed_at",
    "contract_name",
    "contract_version",
    "contract_sha256",
    "qualified_name",
    "database_name",
    "table_name",
    "schema_sha256",
    "status",
    "highest_severity",
    "comparison_count",
    "finding_count",
    "columns_json",
)

SCHEMA_DRIFT_RESULT_COLUMNS = (
    "result_id",
    "snapshot_id",
    "run_id",
    "observed_at",
    "contract_name",
    "contract_version",
    "contract_sha256",
    "qualified_name",
    "column_name",
    "check_type",
    "status",
    "severity",
    "expected_value",
    "actual_value",
    "details_json",
)


# --- Defining Text Helpers
def clickhouse_text(value: Any) -> str:
    """
    Normalize String and FixedString values returned by clickhouse-connect.

    Args:
        value: Raw ClickHouse scalar value.

    Returns:
        Text without FixedString null padding.
    """
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").rstrip("\x00")

    return str(value or "").rstrip("\x00")


# --- Defining DDL Functions
def read_schema_drift_ddl_statements(path: Path = SCHEMA_DRIFT_DDL_PATH) -> tuple[str, ...]:
    """
    Read the modular schema drift DDL into bounded statements.

    Args:
        path: DDL file containing exactly two CREATE TABLE statements.

    Returns:
        Tuple containing the snapshot and comparison table statements.

    Raises:
        FileNotFoundError: If the DDL file is absent.
        ValueError: If the file does not contain exactly two table statements.
    """
    if not path.is_file():
        raise FileNotFoundError(f"Schema drift DDL not found: {path}")

    ddl        = path.read_text(encoding="utf-8-sig")
    statements = tuple(statement.strip() for statement in ddl.split(";") if statement.strip())

    if len(statements) != 2 or sum("CREATE TABLE" in statement.upper() for statement in statements) != 2:
        raise ValueError("Schema drift DDL must contain exactly two CREATE TABLE statements.")

    return statements


def ensure_schema_drift_tables(client: Any, path: Path = SCHEMA_DRIFT_DDL_PATH) -> None:
    """
    Apply idempotent schema drift table DDL for existing local volumes.

    Args:
        client: clickhouse-connect client.
        path: Modular ClickHouse DDL path.

    Returns:
        None.
    """
    statements = read_schema_drift_ddl_statements(path)
    logger.info("Ensuring schema drift tables | ddl=%s statements=%d", path, len(statements))

    for statement in statements:
        client.command(statement)


# --- Defining Persistence Functions
def persist_schema_evaluation(client: Any, evaluation: SchemaDriftEvaluation) -> dict[str, int]:
    """
    Persist deterministic snapshots and comparison rows.

    Args:
        client: clickhouse-connect client.
        evaluation: Completed schema drift evaluation.

    Returns:
        Insert counts for snapshots and comparison results.
    """
    snapshot_rows = [snapshot.as_insert_row() for snapshot in evaluation.snapshots]
    result_rows   = [result.as_insert_row() for result in evaluation.results]

    logger.info(
        "Persisting schema drift evidence | run_id=%s snapshots=%d results=%d",
        evaluation.run_id,
        len(snapshot_rows),
        len(result_rows),
    )

    if snapshot_rows:
        client.insert(
            table=SCHEMA_SNAPSHOTS_TABLE,
            data=snapshot_rows,
            column_names=SCHEMA_SNAPSHOT_COLUMNS,
        )

    if result_rows:
        client.insert(
            table=SCHEMA_DRIFT_RESULTS_TABLE,
            data=result_rows,
            column_names=SCHEMA_DRIFT_RESULT_COLUMNS,
        )

    return {
        "snapshots_written": len(snapshot_rows),
        "results_written": len(result_rows),
    }


# --- Defining Verification Functions
def verify_persisted_schema_evaluation(
    client: Any,
    contract: SchemaContractConfig,
    run_id: str,
) -> SchemaDriftVerification:
    """
    Verify a persisted run is complete and below its failure threshold.

    Args:
        client: clickhouse-connect client.
        contract: Validated source schema contract.
        run_id: Correlated Airflow or CLI run identifier.

    Returns:
        SchemaDriftVerification with exact mismatches and gate evidence.
    """
    validated_run_id = validate_schema_run_id(run_id)
    run_literal      = quote_sql_literal(validated_run_id)
    expected_hash    = schema_contract_sha256(contract)
    expected_tables  = {table.qualified_name for table in contract.tables}

    snapshot_result = client.query(
        f"""
        SELECT
            qualified_name,
            contract_sha256,
            status,
            finding_count
        FROM {SCHEMA_SNAPSHOTS_TABLE} FINAL
        WHERE run_id = {run_literal}
        ORDER BY qualified_name
        LIMIT {MAX_SCHEMA_TABLES}
        """
    )
    result_result = client.query(
        f"""
        SELECT
            qualified_name,
            status,
            severity
        FROM {SCHEMA_DRIFT_RESULTS_TABLE} FINAL
        WHERE run_id = {run_literal}
        ORDER BY qualified_name, check_type, column_name
        LIMIT {MAX_SCHEMA_RESULTS}
        """
    )

    snapshots = [
        (
            clickhouse_text(qualified_name),
            clickhouse_text(contract_hash),
            clickhouse_text(status),
            int(finding_count),
        )
        for qualified_name, contract_hash, status, finding_count in snapshot_result.result_rows
    ]
    results = [
        (
            clickhouse_text(qualified_name),
            clickhouse_text(status),
            clickhouse_text(severity),
        )
        for qualified_name, status, severity in result_result.result_rows
    ]

    errors: list[str] = []
    snapshot_names    = {row[0] for row in snapshots}

    for missing in sorted(expected_tables - snapshot_names):
        errors.append(f"missing persisted schema snapshot: {missing}")

    for unexpected in sorted(snapshot_names - expected_tables):
        errors.append(f"unexpected persisted schema snapshot: {unexpected}")

    for qualified_name, contract_hash, _, _ in snapshots:
        if contract_hash != expected_hash:
            errors.append(f"contract hash mismatch: {qualified_name}")

    result_tables = {row[0] for row in results}
    for qualified_name in sorted(expected_tables - result_tables):
        errors.append(f"missing persisted schema comparisons: {qualified_name}")

    finding_count = sum(status in {"warn", "fail"} for _, status, _ in results)
    threshold     = SEVERITY_RANK[contract.policy.fail_on_severity]
    gate_failures = sum(
        status in {"warn", "fail"} and SEVERITY_RANK.get(severity, 0) >= threshold
        for _, status, severity in results
    )

    if gate_failures:
        errors.append(
            f"schema drift severity gate crossed: threshold={contract.policy.fail_on_severity} "
            f"findings={gate_failures}"
        )

    verification = SchemaDriftVerification(
        status="pass" if not errors else "fail",
        run_id=validated_run_id,
        expected_table_count=len(expected_tables),
        snapshot_count=len(snapshots),
        result_count=len(results),
        finding_count=finding_count,
        gate_failure_count=gate_failures,
        errors=tuple(errors),
    )

    logger.info(
        "Schema drift persistence verification completed | payload=%s",
        json.dumps(verification.as_dict(), sort_keys=True),
    )

    return verification
