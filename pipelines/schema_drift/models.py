####
## Schema Drift Domain Models for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Typed runtime records for schema snapshots, comparisons, and verification."""

# --- Importing Libraries
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


# --- Defining Constants
SEVERITY_RANK = {
    "info": 0,
    "warning": 1,
    "critical": 2,
}

FINDING_STATUSES = {"warn", "fail"}


# --- Defining Domain Models
@dataclass(frozen=True)
class ObservedColumn:
    """
    Represent one column observed from ClickHouse system metadata.

    Attributes:
        position: One-based ordinal position.
        name: Column identifier.
        data_type: Exact type returned by system.columns.
        default_kind: ClickHouse default kind or an empty string.
        default_expression: ClickHouse default expression or an empty string.
    """

    position: int
    name: str
    data_type: str
    default_kind: str
    default_expression: str

    def as_dict(self) -> dict[str, Any]:
        """
        Serialize the observed column for snapshot evidence.

        Returns:
            JSON-safe dictionary in deterministic key order.
        """
        return {
            "position": self.position,
            "name": self.name,
            "data_type": self.data_type,
            "default_kind": self.default_kind,
            "default_expression": self.default_expression,
        }


@dataclass(frozen=True)
class SchemaComparisonResult:
    """
    Represent one deterministic contract comparison outcome.

    Attributes:
        result_id: Stable UUID for one run/table/check/column identity.
        snapshot_id: Parent schema snapshot UUID.
        run_id: Airflow or CLI correlation identifier.
        observed_at: UTC observation timestamp.
        contract_name: Schema contract identifier.
        contract_version: Contract format version.
        contract_sha256: Hash of the validated contract content.
        qualified_name: Compared database.table asset.
        column_name: Column under comparison, empty for table-level checks.
        check_type: Deterministic schema comparison category.
        status: pass, warn, fail, or skip.
        severity: Operational severity when the comparison is a finding.
        expected_value: Human-readable expected value.
        actual_value: Human-readable observed value.
        details: Bounded structured evidence.
    """

    result_id: UUID
    snapshot_id: UUID
    run_id: str
    observed_at: datetime
    contract_name: str
    contract_version: int
    contract_sha256: str
    qualified_name: str
    column_name: str
    check_type: str
    status: str
    severity: str
    expected_value: str
    actual_value: str
    details: dict[str, Any]

    @property
    def is_finding(self) -> bool:
        """
        Return whether this outcome represents observable drift.

        Returns:
            True for warning or failure outcomes.
        """
        return self.status in FINDING_STATUSES

    def as_insert_row(self) -> tuple[Any, ...]:
        """
        Convert the result into ClickHouse insertion order.

        Returns:
            Tuple aligned with SCHEMA_DRIFT_RESULT_COLUMNS.
        """
        return (
            self.result_id,
            self.snapshot_id,
            self.run_id,
            self.observed_at,
            self.contract_name,
            self.contract_version,
            self.contract_sha256,
            self.qualified_name,
            self.column_name,
            self.check_type,
            self.status,
            self.severity,
            self.expected_value,
            self.actual_value,
            json.dumps(self.details, ensure_ascii=True, sort_keys=True),
        )


@dataclass(frozen=True)
class TableSchemaSnapshot:
    """
    Persist one table schema observation and its comparison summary.

    Attributes:
        snapshot_id: Stable UUID derived from run and table identity.
        run_id: Airflow or CLI correlation identifier.
        observed_at: UTC observation timestamp.
        contract_name: Schema contract identifier.
        contract_version: Contract format version.
        contract_sha256: Hash of validated contract content.
        qualified_name: Observed database.table asset.
        database_name: ClickHouse database name.
        table_name: ClickHouse table name.
        schema_sha256: Hash of normalized observed columns.
        status: pass, warn, or fail table result.
        highest_severity: Highest finding severity, or info when clean.
        comparison_count: Number of deterministic comparison rows.
        finding_count: Number of warning/failure outcomes.
        columns: Ordered observed columns retained as evidence.
    """

    snapshot_id: UUID
    run_id: str
    observed_at: datetime
    contract_name: str
    contract_version: int
    contract_sha256: str
    qualified_name: str
    database_name: str
    table_name: str
    schema_sha256: str
    status: str
    highest_severity: str
    comparison_count: int
    finding_count: int
    columns: tuple[ObservedColumn, ...]

    def as_insert_row(self) -> tuple[Any, ...]:
        """
        Convert the snapshot into ClickHouse insertion order.

        Returns:
            Tuple aligned with SCHEMA_SNAPSHOT_COLUMNS.
        """
        columns_json = json.dumps(
            [column.as_dict() for column in self.columns],
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )

        return (
            self.snapshot_id,
            self.run_id,
            self.observed_at,
            self.contract_name,
            self.contract_version,
            self.contract_sha256,
            self.qualified_name,
            self.database_name,
            self.table_name,
            self.schema_sha256,
            self.status,
            self.highest_severity,
            self.comparison_count,
            self.finding_count,
            columns_json,
        )


@dataclass(frozen=True)
class SchemaDriftEvaluation:
    """
    Aggregate one complete schema-contract evaluation.

    Attributes:
        run_id: Airflow or CLI correlation identifier.
        observed_at: UTC observation timestamp shared by all records.
        contract_name: Evaluated contract identifier.
        contract_sha256: Validated contract hash.
        fail_on_severity: Lowest finding severity that fails the gate.
        snapshots: Per-table schema snapshots.
        results: Per-check deterministic outcomes.
    """

    run_id: str
    observed_at: datetime
    contract_name: str
    contract_sha256: str
    fail_on_severity: str
    snapshots: tuple[TableSchemaSnapshot, ...]
    results: tuple[SchemaComparisonResult, ...]

    @property
    def findings(self) -> tuple[SchemaComparisonResult, ...]:
        """
        Return only warning and failure outcomes.

        Returns:
            Tuple of schema drift findings.
        """
        return tuple(result for result in self.results if result.is_finding)

    @property
    def highest_severity(self) -> str:
        """
        Return the highest observed finding severity.

        Returns:
            info when clean, otherwise warning or critical.
        """
        if not self.findings:
            return "info"

        return max(self.findings, key=lambda result: SEVERITY_RANK[result.severity]).severity

    @property
    def should_fail(self) -> bool:
        """
        Evaluate the configured operational severity gate.

        Returns:
            True when at least one finding reaches fail_on_severity.
        """
        threshold = SEVERITY_RANK[self.fail_on_severity]

        return any(SEVERITY_RANK[result.severity] >= threshold for result in self.findings)

    @property
    def status(self) -> str:
        """
        Return the aggregate operational status.

        Returns:
            fail when the severity gate is crossed, warn for lower findings, otherwise pass.
        """
        if self.should_fail:
            return "fail"
        if self.findings:
            return "warn"

        return "pass"

    def as_dict(self) -> dict[str, Any]:
        """
        Serialize a bounded Airflow-friendly evaluation summary.

        Returns:
            JSON-safe summary without raw snapshot payloads.
        """
        return {
            "run_id": self.run_id,
            "observed_at": self.observed_at.isoformat(),
            "contract_name": self.contract_name,
            "contract_sha256": self.contract_sha256,
            "fail_on_severity": self.fail_on_severity,
            "status": self.status,
            "highest_severity": self.highest_severity,
            "table_count": len(self.snapshots),
            "comparison_count": len(self.results),
            "finding_count": len(self.findings),
            "failed_count": sum(result.status == "fail" for result in self.results),
            "warning_count": sum(result.status == "warn" for result in self.results),
            "finding_types": sorted({result.check_type for result in self.findings}),
        }


@dataclass(frozen=True)
class SchemaDriftVerification:
    """
    Describe persisted schema-evaluation verification evidence.

    Attributes:
        status: pass or fail verification state.
        run_id: Correlated Airflow or CLI run identifier.
        expected_table_count: Number of tables declared by the contract.
        snapshot_count: Latest persisted snapshots found for the run.
        result_count: Latest persisted comparison rows found for the run.
        finding_count: Persisted warning/failure rows.
        gate_failure_count: Findings at or above fail_on_severity.
        errors: Exact verification mismatches.
    """

    status: str
    run_id: str
    expected_table_count: int
    snapshot_count: int
    result_count: int
    finding_count: int
    gate_failure_count: int
    errors: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        """
        Serialize verification evidence.

        Returns:
            JSON-safe summary dictionary.
        """
        return {
            "status": self.status,
            "run_id": self.run_id,
            "expected_table_count": self.expected_table_count,
            "snapshot_count": self.snapshot_count,
            "result_count": self.result_count,
            "finding_count": self.finding_count,
            "gate_failure_count": self.gate_failure_count,
            "errors": list(self.errors),
        }
