####
## Deterministic SQL Review Tools for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Review SQL policy, metadata trust, and conservative ClickHouse scan evidence."""

# --- Importing Libraries
from __future__ import annotations

import re
import time
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from agent.tools.audit_log import write_agent_audit_event
from agent.tools.clickhouse_sql import (
    GuardrailViolation,
    SqlGuardrailConfig,
    guard_sql,
    normalize_sql,
    rows_to_dicts,
)
from pipelines.common.clickhouse import (
    build_clickhouse_client,
    quote_sql_literal,
    split_table_name,
    validate_qualified_table_name,
)
from pipelines.common.logging import logger


# --- Defining Constants
SQL_POLICY_TOOL_NAME         = "sql_policy_review"
WAREHOUSE_STATS_TOOL_NAME    = "warehouse_statistics"
MAX_REVIEWED_TABLES          = 12
LOW_SCAN_BYTES_THRESHOLD     = 100 * 1024 * 1024
MEDIUM_SCAN_BYTES_THRESHOLD  = 1024 * 1024 * 1024
HIGH_SCAN_BYTES_THRESHOLD    = 10 * 1024 * 1024 * 1024

TABLE_REFERENCE_PATTERN = re.compile(
    r"\b(?:from|join)\s+"
    r"((?:`?[A-Za-z_][A-Za-z0-9_]*`?\s*\.\s*)?`?[A-Za-z_][A-Za-z0-9_]*`?)",
    flags=re.IGNORECASE,
)

CTE_NAME_PATTERN = re.compile(
    r"(?:\bwith\b|,)\s*`?([A-Za-z_][A-Za-z0-9_]*)`?\s+as\s*\(",
    flags=re.IGNORECASE,
)

WILDCARD_PROJECTION_PATTERN = re.compile(
    r"\bselect\s+(?:distinct\s+)?(?:[A-Za-z_][A-Za-z0-9_]*\.)?\*",
    flags=re.IGNORECASE,
)


# --- Defining Enumerations
class SqlReviewDecision(str, Enum):
    """Represent the deterministic disposition of one SQL proposal."""

    APPROVED = "approved"
    REJECTED = "rejected"


class SqlFindingSeverity(str, Enum):
    """Represent the operational weight of one explainable review finding."""

    INFO     = "info"
    WARNING  = "warning"
    BLOCKING = "blocking"


class SqlRiskLevel(str, Enum):
    """Represent an explainable query or upper-bound scan risk level."""

    LOW      = "low"
    MEDIUM   = "medium"
    HIGH     = "high"
    CRITICAL = "critical"
    UNKNOWN  = "unknown"


class TableTrustStatus(str, Enum):
    """Represent metadata trust policy for one referenced warehouse table."""

    TRUSTED = "trusted"
    REVIEW  = "review"
    BLOCKED = "blocked"


# --- Defining Review Models
class SqlPolicyFinding(BaseModel):
    """
    Describe one deterministic policy, trust, or cost finding.

    Attributes:
        code: Stable machine-readable finding code.
        severity: Info, warning, or blocking policy level.
        message: Human-readable explanation.
        table_name: Optional affected warehouse table.
    """

    model_config = ConfigDict(extra="forbid")

    code: str
    severity: SqlFindingSeverity
    message: str                      = Field(min_length=1, max_length=1_000)
    table_name: str                   = Field(default="", max_length=255)


class SqlGuardrailReview(BaseModel):
    """
    Capture static guardrail results without executing the proposed SQL.

    Attributes:
        guardrail_passed: Whether the existing SQL execution guardrails accepted the proposal.
        guarded_sql: Read-only SQL after hard LIMIT enforcement.
        guardrails_applied: Guardrail labels returned by the execution boundary.
        referenced_tables: Tables extracted from FROM and JOIN clauses.
        findings: Explainable static policy findings.
        join_count: Number of JOIN keywords in the normalized proposal.
        uses_wildcard_projection: Whether SELECT * is present.
    """

    model_config = ConfigDict(extra="forbid")

    guardrail_passed: bool
    guarded_sql: str                         = Field(default="", max_length=20_000)
    guardrails_applied: list[str]            = Field(default_factory=list, max_length=30)
    referenced_tables: list[str]             = Field(default_factory=list, max_length=MAX_REVIEWED_TABLES)
    findings: list[SqlPolicyFinding]          = Field(default_factory=list, max_length=50)
    join_count: int                           = Field(default=0, ge=0, le=100)
    uses_wildcard_projection: bool            = False


class TableTrustAssessment(BaseModel):
    """
    Describe registry-backed trust for one referenced table.

    Attributes:
        qualified_name: Fully qualified warehouse asset.
        registry_found: Whether the trusted metadata registry contains the asset.
        certification_status: Current asset certification.
        lifecycle_status: Current lifecycle status.
        sensitivity: Current sensitivity classification.
        contains_pii: Whether metadata declares PII.
        trust_status: Trusted, review, or blocked disposition.
        reasons: Explainable trust reasons.
    """

    model_config = ConfigDict(extra="forbid")

    qualified_name: str
    registry_found: bool
    certification_status: str              = ""
    lifecycle_status: str                  = ""
    sensitivity: str                       = ""
    contains_pii: bool                     = False
    trust_status: TableTrustStatus
    reasons: list[str]                     = Field(default_factory=list, max_length=20)


class TableScanEstimate(BaseModel):
    """
    Describe a conservative scan upper bound from active ClickHouse parts.

    Attributes:
        qualified_name: Fully qualified warehouse table.
        active_rows: Rows reported across active MergeTree parts.
        active_bytes: Bytes on disk across active MergeTree parts.
        active_parts: Active part count.
        estimate_status: estimated or no_active_parts.
        estimate_basis: Honest description of the estimate source.
        risk_level: Risk derived from active_bytes thresholds.
    """

    model_config = ConfigDict(extra="forbid")

    qualified_name: str
    active_rows: int                       = Field(default=0, ge=0)
    active_bytes: int                      = Field(default=0, ge=0)
    active_parts: int                      = Field(default=0, ge=0)
    estimate_status: str                   = Field(default="estimated", max_length=40)
    estimate_basis: str                    = Field(min_length=1, max_length=500)
    risk_level: SqlRiskLevel               = SqlRiskLevel.UNKNOWN


# --- Parsing SQL References
def normalize_table_reference(value: str) -> str:
    """
    Normalize one regex-extracted ClickHouse table reference.

    Args:
        value: Raw identifier containing optional backticks and whitespace.

    Returns:
        Lowercase normalized table reference.
    """
    return re.sub(r"\s+", "", value.replace("`", "")).lower()


def extract_cte_names(sql: str) -> set[str]:
    """
    Extract CTE aliases so they are not mistaken for physical warehouse tables.

    Args:
        sql: Normalized SQL statement.

    Returns:
        Lowercase CTE alias set.
    """
    return {match.group(1).lower() for match in CTE_NAME_PATTERN.finditer(sql)}


def extract_referenced_tables(sql: str) -> list[str]:
    """
    Extract bounded physical table references from FROM and JOIN clauses.

    Args:
        sql: Normalized single SQL statement.

    Returns:
        Stable de-duplicated table references in statement order.
    """
    cte_names = extract_cte_names(sql)
    tables: list[str] = []

    for match in TABLE_REFERENCE_PATTERN.finditer(sql):
        table_name = normalize_table_reference(match.group(1))

        if "." not in table_name and table_name in cte_names:
            continue

        if table_name not in tables:
            tables.append(table_name)

    return tables


# --- Applying Static SQL Policy
def build_sql_guardrail_review(
    sql_proposal: str,
    hard_limit: int,
    require_date_filter: bool,
) -> SqlGuardrailReview:
    """
    Apply existing read-only/date/LIMIT guardrails without executing SQL.

    Args:
        sql_proposal: Proposed SQL supplied for review.
        hard_limit: Maximum result rows allowed by the execution boundary.
        require_date_filter: Whether large tables require date predicates.

    Returns:
        SqlGuardrailReview containing guarded SQL and explainable findings.
    """
    findings: list[SqlPolicyFinding] = []

    try:
        guarded_sql, applied = guard_sql(
            sql=sql_proposal,
            config=SqlGuardrailConfig(
                hard_limit=hard_limit,
                require_date_filter=require_date_filter,
            ),
        )

    except GuardrailViolation as exc:
        findings.append(
            SqlPolicyFinding(
                code="sql_guardrail_rejected",
                severity=SqlFindingSeverity.BLOCKING,
                message=str(exc),
            )
        )

        return SqlGuardrailReview(
            guardrail_passed=False,
            findings=findings,
        )

    referenced_tables = extract_referenced_tables(guarded_sql)

    if len(referenced_tables) > MAX_REVIEWED_TABLES:
        findings.append(
            SqlPolicyFinding(
                code="too_many_referenced_tables",
                severity=SqlFindingSeverity.BLOCKING,
                message=(
                    f"SQL references {len(referenced_tables)} tables; the review boundary allows "
                    f"at most {MAX_REVIEWED_TABLES}."
                ),
            )
        )

    for table_name in referenced_tables:
        if "." not in table_name:
            findings.append(
                SqlPolicyFinding(
                    code="unqualified_table_reference",
                    severity=SqlFindingSeverity.BLOCKING,
                    message="Warehouse SQL must use an explicit database.table reference.",
                    table_name=table_name,
                )
            )

    for label in applied:
        if label.startswith("limit_added_") or label.startswith("limit_capped_"):
            findings.append(
                SqlPolicyFinding(
                    code="hard_limit_rewritten",
                    severity=SqlFindingSeverity.WARNING,
                    message=f"The execution boundary rewrote the proposal using {label}.",
                )
            )

    wildcard_projection = bool(WILDCARD_PROJECTION_PATTERN.search(guarded_sql))

    if wildcard_projection:
        findings.append(
            SqlPolicyFinding(
                code="wildcard_projection",
                severity=SqlFindingSeverity.WARNING,
                message="SELECT * increases scan and exposure risk; select only required columns.",
            )
        )

    findings.append(
        SqlPolicyFinding(
            code="read_only_guardrails_passed",
            severity=SqlFindingSeverity.INFO,
            message="Read-only, single-statement, date-filter, and hard LIMIT policy passed.",
        )
    )

    return SqlGuardrailReview(
        guardrail_passed=True,
        guarded_sql=guarded_sql,
        guardrails_applied=applied,
        referenced_tables=referenced_tables[:MAX_REVIEWED_TABLES],
        findings=findings,
        join_count=len(re.findall(r"\bjoin\b", guarded_sql, flags=re.IGNORECASE)),
        uses_wildcard_projection=wildcard_projection,
    )


# --- Assessing Metadata Trust
def assess_table_trust(
    qualified_name: str,
    metadata_asset: dict[str, Any] | None,
    uses_wildcard_projection: bool,
) -> tuple[TableTrustAssessment, list[SqlPolicyFinding]]:
    """
    Classify one table using trusted registry metadata.

    Args:
        qualified_name: Fully qualified warehouse table.
        metadata_asset: Public metadata registry record, or None when missing.
        uses_wildcard_projection: Whether the proposal uses SELECT *.

    Returns:
        TableTrustAssessment and explainable findings.
    """
    if metadata_asset is None:
        reason = "The table is not registered in the trusted metadata catalog."

        return (
            TableTrustAssessment(
                qualified_name=qualified_name,
                registry_found=False,
                trust_status=TableTrustStatus.BLOCKED,
                reasons=[reason],
            ),
            [
                SqlPolicyFinding(
                    code="metadata_asset_not_found",
                    severity=SqlFindingSeverity.BLOCKING,
                    message=reason,
                    table_name=qualified_name,
                )
            ],
        )

    certification = str(metadata_asset.get("certification_status", "")).strip().lower()
    lifecycle     = str(metadata_asset.get("lifecycle_status", "")).strip().lower()
    sensitivity   = str(metadata_asset.get("sensitivity", "")).strip().lower()
    contains_pii  = bool(metadata_asset.get("contains_pii", False))
    reasons: list[str] = []
    findings: list[SqlPolicyFinding] = []
    trust_status = TableTrustStatus.TRUSTED

    if lifecycle != "active":
        trust_status = TableTrustStatus.BLOCKED
        reasons.append(f"Lifecycle status is {lifecycle or 'unknown'}, not active.")
        findings.append(
            SqlPolicyFinding(
                code="inactive_metadata_asset",
                severity=SqlFindingSeverity.BLOCKING,
                message=reasons[-1],
                table_name=qualified_name,
            )
        )

    if certification in {"experimental", "deprecated", ""}:
        trust_status = TableTrustStatus.BLOCKED
        reasons.append(f"Certification status is {certification or 'unknown'}.")
        findings.append(
            SqlPolicyFinding(
                code="untrusted_certification",
                severity=SqlFindingSeverity.BLOCKING,
                message=reasons[-1],
                table_name=qualified_name,
            )
        )

    elif certification == "candidate" and trust_status != TableTrustStatus.BLOCKED:
        trust_status = TableTrustStatus.REVIEW
        reasons.append("The asset is a candidate, not a fully certified data product.")
        findings.append(
            SqlPolicyFinding(
                code="candidate_certification",
                severity=SqlFindingSeverity.WARNING,
                message=reasons[-1],
                table_name=qualified_name,
            )
        )

    elif certification == "certified":
        reasons.append("The asset is active and certified.")

    if contains_pii and uses_wildcard_projection:
        trust_status = TableTrustStatus.BLOCKED
        reasons.append("Wildcard projection is not allowed on an asset declared as containing PII.")
        findings.append(
            SqlPolicyFinding(
                code="pii_wildcard_projection",
                severity=SqlFindingSeverity.BLOCKING,
                message=reasons[-1],
                table_name=qualified_name,
            )
        )

    elif contains_pii:
        if trust_status == TableTrustStatus.TRUSTED:
            trust_status = TableTrustStatus.REVIEW

        reasons.append("The asset contains PII and requires a purpose-aware column review.")
        findings.append(
            SqlPolicyFinding(
                code="pii_asset_review",
                severity=SqlFindingSeverity.WARNING,
                message=reasons[-1],
                table_name=qualified_name,
            )
        )

    if sensitivity in {"confidential", "restricted"}:
        if trust_status == TableTrustStatus.TRUSTED:
            trust_status = TableTrustStatus.REVIEW

        reasons.append(f"Sensitivity is {sensitivity}; access policy must be verified.")
        findings.append(
            SqlPolicyFinding(
                code="sensitive_asset_review",
                severity=SqlFindingSeverity.WARNING,
                message=reasons[-1],
                table_name=qualified_name,
            )
        )

    if not reasons:
        reasons.append("No blocking metadata trust condition was detected.")

    return (
        TableTrustAssessment(
            qualified_name=qualified_name,
            registry_found=True,
            certification_status=certification,
            lifecycle_status=lifecycle,
            sensitivity=sensitivity,
            contains_pii=contains_pii,
            trust_status=trust_status,
            reasons=reasons,
        ),
        findings,
    )


# --- Estimating ClickHouse Scan Risk
def classify_scan_risk(active_bytes: int) -> SqlRiskLevel:
    """
    Classify conservative active-part bytes into an explainable risk level.

    Args:
        active_bytes: Total bytes on disk across referenced active parts.

    Returns:
        Low, medium, high, or critical scan risk.
    """
    if active_bytes <= LOW_SCAN_BYTES_THRESHOLD:
        return SqlRiskLevel.LOW

    if active_bytes <= MEDIUM_SCAN_BYTES_THRESHOLD:
        return SqlRiskLevel.MEDIUM

    if active_bytes <= HIGH_SCAN_BYTES_THRESHOLD:
        return SqlRiskLevel.HIGH

    return SqlRiskLevel.CRITICAL


def build_table_statistics_sql(qualified_names: list[str]) -> str:
    """
    Build one fixed system.parts query from validated table identities.

    Args:
        qualified_names: Fully qualified warehouse tables.

    Returns:
        Read-only bounded ClickHouse statistics SQL.

    Raises:
        ValueError: If no tables are supplied or an identifier is unsafe.
    """
    if not qualified_names:
        raise ValueError("At least one qualified table is required for statistics lookup.")

    unique_names = list(dict.fromkeys(qualified_names))

    if len(unique_names) > MAX_REVIEWED_TABLES:
        raise ValueError(f"Statistics lookup allows at most {MAX_REVIEWED_TABLES} tables.")

    predicates: list[str] = []

    for qualified_name in unique_names:
        validate_qualified_table_name(qualified_name)
        database, table = split_table_name(qualified_name)
        predicates.append(
            "(database = "
            + quote_sql_literal(database)
            + " AND table = "
            + quote_sql_literal(table)
            + ")"
        )

    return f"""
        SELECT
            database,
            table,
            sum(rows) AS active_rows,
            sum(bytes_on_disk) AS active_bytes,
            count() AS active_parts
        FROM system.parts
        WHERE active
          AND ({' OR '.join(predicates)})
        GROUP BY database, table
        ORDER BY database, table
        LIMIT {len(unique_names)}
    """


def fetch_table_statistics(
    qualified_names: list[str],
    agent_run_id: UUID | str | None = None,
    alert_key: str = "",
    clickhouse_host: str | None = None,
    clickhouse_port: int | None = None,
) -> list[TableScanEstimate]:
    """
    Fetch active-part scan upper bounds and audit the fixed metadata query.

    Args:
        qualified_names: Fully qualified warehouse tables from static SQL parsing.
        agent_run_id: Optional parent supervisor correlation UUID.
        alert_key: Optional alert correlation key.
        clickhouse_host: Optional ClickHouse host override.
        clickhouse_port: Optional ClickHouse HTTP port override.

    Returns:
        One TableScanEstimate per requested table.
    """
    if not qualified_names:
        return []

    unique_names          = list(dict.fromkeys(qualified_names))
    resolved_agent_run_id = UUID(str(agent_run_id)) if agent_run_id else uuid4()
    client                = build_clickhouse_client(
        host=clickhouse_host,
        port=clickhouse_port,
    )
    sql     = build_table_statistics_sql(unique_names)
    started = time.monotonic()

    try:
        result      = client.query(sql)
        duration_ms = int((time.monotonic() - started) * 1_000)
        rows        = rows_to_dicts(
            columns=list(result.column_names or []),
            rows=result.result_rows,
        )
        by_name = {
            f"{row['database']}.{row['table']}": row
            for row in rows
        }
        estimates: list[TableScanEstimate] = []

        for qualified_name in unique_names:
            row          = by_name.get(qualified_name, {})
            active_rows  = int(row.get("active_rows", 0) or 0)
            active_bytes = int(row.get("active_bytes", 0) or 0)
            active_parts = int(row.get("active_parts", 0) or 0)
            estimates.append(
                TableScanEstimate(
                    qualified_name=qualified_name,
                    active_rows=active_rows,
                    active_bytes=active_bytes,
                    active_parts=active_parts,
                    estimate_status=("estimated" if active_parts else "no_active_parts"),
                    estimate_basis=(
                        "Upper bound from all active MergeTree parts; this is not a precise "
                        "query-plan estimate and may overstate partition-pruned scans."
                    ),
                    risk_level=classify_scan_risk(active_bytes),
                )
            )

        write_agent_audit_event(
            client=client,
            action="fetch_table_statistics",
            status="success",
            agent_run_id=resolved_agent_run_id,
            alert_key=alert_key,
            tool_name=WAREHOUSE_STATS_TOOL_NAME,
            duration_ms=duration_ms,
            input_payload={"qualified_names": unique_names},
            output_payload={
                "table_count": len(estimates),
                "total_active_rows": sum(item.active_rows for item in estimates),
                "total_active_bytes": sum(item.active_bytes for item in estimates),
                "estimate_basis": "active_parts_upper_bound",
            },
            sql=sql,
            row_count=len(estimates),
        )

        logger.info(
            "Fetched SQL review statistics | tables=%d total_active_bytes=%d",
            len(estimates),
            sum(item.active_bytes for item in estimates),
        )

        return estimates

    except Exception as exc:
        duration_ms = int((time.monotonic() - started) * 1_000)
        logger.exception("Failed to fetch SQL review statistics | tables=%s", unique_names)
        write_agent_audit_event(
            client=client,
            action="fetch_table_statistics",
            status="failed",
            agent_run_id=resolved_agent_run_id,
            alert_key=alert_key,
            tool_name=WAREHOUSE_STATS_TOOL_NAME,
            duration_ms=duration_ms,
            input_payload={"qualified_names": unique_names},
            error_message=str(exc),
            sql=sql,
        )

        raise

