####
## Airflow Control Plane Supervisor Trigger for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Validate and trigger one manual Control Plane Supervisor Airflow run."""

# --- Importing Libraries
from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence
from zoneinfo import ZoneInfo


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipelines.common.logging import logger


# --- Defining Constants
CONTROL_PLANE_SUPERVISOR_DAG_ID = "98_dag_dq_control_plane_supervisor_smoke"
LOCAL_TIMEZONE                  = ZoneInfo("Asia/Bangkok")

SUPPORTED_INTENTS = (
    "auto",
    "triage_alert",
    "asset_context",
    "blast_radius",
    "trusted_asset_search",
    "review_sql",
    "schema_drift_assessment",
)

SAFE_ALERT_REFERENCE = re.compile(r"^[A-Za-z0-9_.|:-]{0,500}$")
SAFE_QUALIFIED_NAME  = re.compile(r"^([A-Za-z0-9_]+\.[A-Za-z0-9_]+)?$")
SAFE_QUESTION        = re.compile(r"^[A-Za-z0-9 _.-]{0,1000}$")
SAFE_SEARCH_QUERY    = re.compile(r"^[A-Za-z0-9 _.-]{0,120}$")
SAFE_SQL_PURPOSE     = re.compile(r"^[A-Za-z0-9 _.,()/+-]{0,500}$")
SAFE_SCHEMA_RUN_ID   = re.compile(r"^[A-Za-z0-9_.:+-]{0,250}$")
SAFE_AIRFLOW_RUN_ID  = re.compile(r"^[A-Za-z0-9_.:+-]{1,250}$")
SAFE_DOMAIN          = re.compile(r"^[A-Za-z0-9_]{0,80}$")
SAFE_S3_URI          = re.compile(
    r"^s3://[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]"
    r"(?:/[A-Za-z0-9][A-Za-z0-9._/-]{0,2000})?$"
)
SAFE_S3_BUCKET       = re.compile(
    r"^(?=.{3,63}$)(?!.*\.\.)(?!\d+\.\d+\.\d+\.\d+$)"
    r"[a-z0-9][a-z0-9.-]*[a-z0-9]$"
)
SAFE_ARTIFACT_PREFIX = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,199}$")
SUPPORTED_SQL_DECISIONS = ("", "approved", "rejected")
SUPPORTED_SCHEMA_ASSESSMENTS = (
    "",
    "compatible",
    "review_required",
    "breaking_change",
)
SUPPORTED_DATA_LAYERS = ("", "raw", "staging", "mart")
SUPPORTED_CERTIFICATION_STATUSES = (
    "",
    "experimental",
    "candidate",
    "certified",
    "deprecated",
)
SUPPORTED_LIFECYCLE_STATUSES = ("", "active", "deprecated")
SUPPORTED_EXECUTION_MODES    = ("single", "fanout")

DEFAULT_SQL_REVIEW_PROPOSAL = (
    "SELECT country, count() AS order_count "
    "FROM dq.raw_orders "
    "WHERE dt = toDate('2026-08-08') "
    "GROUP BY country "
    "LIMIT 50"
)


# --- Defining Typed Trigger Configuration
@dataclass(frozen=True, slots=True)
class ValidatedSupervisorTrigger:
    """
    Hold normalized supervisor values that are safe to pass into Airflow.

    Attributes:
        intent: Allowlisted supervisor intent.
        question: Bounded operator wording.
        alert_key: Alert Ref or stable system key.
        qualified_name: Exact database.table identifier.
        query: Trusted metadata search text.
        domain: Optional metadata domain filter.
        data_layer: Optional raw, staging, or mart filter.
        certification_status: Optional asset certification filter.
        lifecycle_status: Optional active or deprecated filter.
        token_budget: Aggregate model token budget.
        latency_budget_ms: Aggregate specialist latency budget.
        sql_proposal_base64: Base64-encoded SQL review proposal.
        sql_purpose: Bounded operator reason for SQL review.
        sql_hard_limit: Guarded SQL result-row limit.
        sql_require_date_filter: Whether large tables require date predicates.
        sql_max_scan_bytes: Conservative scan-risk ceiling.
        expected_sql_decision: Optional verifier expectation.
        schema_run_id: Exact schema detector DagRun identifier.
        schema_finding_limit: Maximum schema findings returned.
        expected_schema_assessment: Optional schema verifier expectation.
        result_limit: Maximum metadata search results.
        max_depth: Maximum lineage traversal depth.
        max_nodes: Maximum lineage graph nodes.
        confidence_threshold: Confidence required before skipping extra evidence.
        max_evidence_iterations: Maximum bounded extra-evidence loops.
        manifest_s3_uri: Optional system-owned dbt manifest location.
        artifacts_bucket: Optional report artifact bucket.
        artifacts_prefix: Relative S3 prefix for report artifacts.
        execution_mode: Single-handoff or explicitly enabled fan-out execution.
        max_workers: Maximum immutable workers in the execution plan.
        max_concurrency: Maximum workers running concurrently.
        allow_external_llm: Request-level external provider permission.
        max_handoffs: Maximum specialist handoffs for the pilot.
        max_retries: Maximum specialist retries for the pilot.
        max_model_calls: Maximum external provider attempts.
        estimated_cost_budget_usd: Aggregate estimated provider-cost ceiling.
    """

    intent: str
    question: str
    alert_key: str
    qualified_name: str
    query: str
    domain: str
    data_layer: str
    certification_status: str
    lifecycle_status: str
    token_budget: int
    latency_budget_ms: int
    sql_proposal_base64: str
    sql_purpose: str
    sql_hard_limit: int
    sql_require_date_filter: bool
    sql_max_scan_bytes: int
    expected_sql_decision: str
    schema_run_id: str
    schema_finding_limit: int
    expected_schema_assessment: str
    result_limit: int
    max_depth: int
    max_nodes: int
    confidence_threshold: float
    max_evidence_iterations: int
    manifest_s3_uri: str
    artifacts_bucket: str
    artifacts_prefix: str
    execution_mode: str
    max_workers: int
    max_concurrency: int
    allow_external_llm: bool
    max_handoffs: int
    max_retries: int
    max_model_calls: int
    estimated_cost_budget_usd: float


# --- Defining Intent-Aware Defaults
def resolve_trigger_context_defaults(
    intent: str,
    qualified_name: str | None,
    query: str | None,
) -> tuple[str, str]:
    """
    Resolve only the context required by the selected supervisor intent.

    Args:
        intent: Requested supervisor intent.
        qualified_name: Optional exact database.table asset supplied by the operator.
        query: Optional trusted metadata search text supplied by the operator.

    Returns:
        Intent-aware qualified name and search query defaults.

    Notes:
        Incident triage must not inherit unrelated metadata context merely because
        the trigger helper also supports metadata and lineage requests.
    """
    normalized_intent = intent.strip().lower()
    resolved_asset    = (qualified_name or "").strip()
    resolved_query    = (query or "").strip()

    if not resolved_asset and normalized_intent in {
        "asset_context",
        "blast_radius",
        "schema_drift_assessment",
    }:
        resolved_asset = "dq.raw_orders"

    if not resolved_query and normalized_intent == "trusted_asset_search":
        resolved_query = "orders"

    return resolved_asset, resolved_query


def load_sql_proposal(intent: str, sql_file: str | None) -> str:
    """
    Load SQL review input from a repository-owned file or safe smoke default.

    Args:
        intent: Requested supervisor intent.
        sql_file: Optional repository-relative SQL file.

    Returns:
        SQL proposal for review, or an empty string for non-review intents.

    Raises:
        ValueError: If the file escapes the repository or is not a regular file.
    """
    normalized_intent = intent.strip().lower()

    if not sql_file:
        if normalized_intent == "review_sql":
            return DEFAULT_SQL_REVIEW_PROPOSAL

        return ""

    candidate = Path(sql_file)

    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate

    resolved_path = candidate.resolve()

    if not resolved_path.is_relative_to(PROJECT_ROOT.resolve()):
        raise ValueError("SQL review file must remain inside the project repository.")

    if not resolved_path.is_file():
        raise ValueError(f"SQL review file does not exist: {sql_file}")

    sql_proposal = resolved_path.read_text(encoding="utf-8").strip()

    if not sql_proposal:
        raise ValueError("SQL review file cannot be empty.")

    return sql_proposal


# --- Defining Validation Helpers
def validate_trigger_inputs(
    intent: str,
    question: str,
    alert_key: str,
    qualified_name: str,
    query: str,
    token_budget: int,
    latency_budget_ms: int,
    domain: str = "",
    data_layer: str = "",
    certification_status: str = "",
    lifecycle_status: str = "",
    execution_mode: str = "single",
    max_workers: int = 1,
    max_concurrency: int = 1,
    allow_external_llm: bool = False,
    max_handoffs: int = 1,
    max_retries: int = 0,
    max_model_calls: int = 3,
    estimated_cost_budget_usd: float = 0.05,
    sql_proposal: str = "",
    sql_purpose: str = "",
    sql_hard_limit: int = 100,
    sql_require_date_filter: bool = True,
    sql_max_scan_bytes: int = 1024 * 1024 * 1024,
    expected_sql_decision: str = "",
    schema_run_id: str = "",
    schema_finding_limit: int = 50,
    expected_schema_assessment: str = "",
    result_limit: int = 10,
    max_depth: int = 5,
    max_nodes: int = 100,
    confidence_threshold: float = 0.70,
    max_evidence_iterations: int = 2,
    manifest_s3_uri: str = "",
    artifacts_bucket: str = "",
    artifacts_prefix: str = "agent-reports",
) -> ValidatedSupervisorTrigger:
    """
    Validate operator inputs before they enter Airflow configuration.

    Args:
        intent: Requested supervisor intent.
        question: Optional bounded auto-classification wording.
        alert_key: Optional Alert Ref or system alert key.
        qualified_name: Optional exact database.table asset.
        query: Optional metadata search query.
        token_budget: Aggregate supervisor token budget.
        latency_budget_ms: Aggregate supervisor latency budget.
        domain: Optional metadata domain filter.
        data_layer: Optional raw, staging, or mart filter.
        certification_status: Optional asset certification filter.
        lifecycle_status: Optional active or deprecated filter.
        execution_mode: Single-handoff or explicitly enabled fan-out mode.
        max_workers: Maximum immutable tasks admitted into a fan-out plan.
        max_concurrency: Maximum workers allowed to run concurrently.
        allow_external_llm: Request-level permission gated by the global runtime switch.
        max_handoffs: Maximum specialist handoffs for this pilot run.
        max_retries: Maximum specialist retries; currently policy-locked to zero.
        max_model_calls: Maximum aggregate external provider attempts.
        estimated_cost_budget_usd: Maximum aggregate estimated provider cost.
        sql_proposal: Optional SQL statement encoded before entering Airflow configuration.
        sql_purpose: Optional bounded operator reason for the SQL.
        sql_hard_limit: Maximum result rows allowed by the guarded SQL tool.
        sql_require_date_filter: Whether known large tables require date predicates.
        sql_max_scan_bytes: Maximum conservative active-part scan upper bound.
        expected_sql_decision: Optional approved or rejected verification expectation.
        schema_run_id: Exact persisted schema detector DagRun identifier.
        schema_finding_limit: Maximum persisted finding rows returned.
        expected_schema_assessment: Optional verifier compatibility expectation.
        result_limit: Maximum metadata rows returned.
        max_depth: Maximum lineage traversal depth.
        max_nodes: Maximum lineage graph nodes.
        confidence_threshold: Confidence required before skipping extra evidence.
        max_evidence_iterations: Maximum bounded extra-evidence loops.
        manifest_s3_uri: Optional system-owned dbt manifest artifact URI.
        artifacts_bucket: Optional report artifact bucket.
        artifacts_prefix: Relative S3 report prefix.

    Returns:
        Typed normalized values safe to serialize into Airflow configuration.

    Raises:
        ValueError: If any value violates the trigger allowlist or bounds.
    """
    normalized_intent            = intent.strip().lower()
    normalized_question          = question.strip()
    normalized_alert             = alert_key.strip()
    normalized_asset             = qualified_name.strip()
    normalized_query             = query.strip()
    normalized_domain            = domain.strip()
    normalized_data_layer        = data_layer.strip().lower()
    normalized_certification     = certification_status.strip().lower()
    normalized_lifecycle         = lifecycle_status.strip().lower()
    normalized_execution_mode    = execution_mode.strip().lower()
    normalized_sql               = sql_proposal.strip()
    normalized_sql_purpose       = sql_purpose.strip()
    normalized_expected_decision = expected_sql_decision.strip().lower()
    normalized_schema_run        = schema_run_id.strip()
    normalized_schema_assessment = expected_schema_assessment.strip().lower()
    normalized_manifest_uri      = manifest_s3_uri.strip()
    normalized_artifacts_bucket  = artifacts_bucket.strip()
    normalized_artifacts_prefix  = artifacts_prefix.strip()

    if normalized_intent not in SUPPORTED_INTENTS:
        raise ValueError(f"Unsupported supervisor intent: {intent}")

    if not SAFE_QUESTION.fullmatch(normalized_question):
        raise ValueError("Supervisor question contains unsupported characters.")

    if not SAFE_ALERT_REFERENCE.fullmatch(normalized_alert):
        raise ValueError("Supervisor alert key contains unsupported characters.")

    if not SAFE_QUALIFIED_NAME.fullmatch(normalized_asset):
        raise ValueError("Supervisor qualified name must use database.table format.")

    if not SAFE_SEARCH_QUERY.fullmatch(normalized_query):
        raise ValueError("Supervisor search query contains unsupported characters.")

    if not SAFE_DOMAIN.fullmatch(normalized_domain):
        raise ValueError("Supervisor domain contains unsupported characters.")

    if normalized_data_layer not in SUPPORTED_DATA_LAYERS:
        raise ValueError("Data layer must be raw, staging, mart, or blank.")

    if normalized_certification not in SUPPORTED_CERTIFICATION_STATUSES:
        raise ValueError(
            "Certification status must be experimental, candidate, certified, "
            "deprecated, or blank."
        )

    if normalized_lifecycle not in SUPPORTED_LIFECYCLE_STATUSES:
        raise ValueError("Lifecycle status must be active, deprecated, or blank.")

    if normalized_execution_mode not in SUPPORTED_EXECUTION_MODES:
        raise ValueError("Execution mode must be single or fanout.")

    if not SAFE_SQL_PURPOSE.fullmatch(normalized_sql_purpose):
        raise ValueError("Supervisor SQL purpose contains unsupported characters.")

    if "\x00" in normalized_sql or len(normalized_sql) > 20_000:
        raise ValueError("Supervisor SQL proposal is invalid or exceeds 20000 characters.")

    if not 1 <= sql_hard_limit <= 1_000:
        raise ValueError("SQL hard limit must be between 1 and 1000.")

    if not 1024 * 1024 <= sql_max_scan_bytes <= 1024 * 1024 * 1024 * 1024:
        raise ValueError("SQL max scan bytes must be between 1 MiB and 1 TiB.")

    if normalized_expected_decision not in SUPPORTED_SQL_DECISIONS:
        raise ValueError("Expected SQL decision must be approved, rejected, or blank.")

    if not SAFE_SCHEMA_RUN_ID.fullmatch(normalized_schema_run):
        raise ValueError("Schema run ID contains unsupported characters.")

    if not 1 <= schema_finding_limit <= 100:
        raise ValueError("Schema finding limit must be between 1 and 100.")

    if normalized_schema_assessment not in SUPPORTED_SCHEMA_ASSESSMENTS:
        raise ValueError(
            "Expected schema assessment must be compatible, review_required, "
            "breaking_change, or blank."
        )

    if not 1 <= result_limit <= 25:
        raise ValueError("Result limit must be between 1 and 25.")

    if not 1 <= max_depth <= 10:
        raise ValueError("Lineage max depth must be between 1 and 10.")

    if not 1 <= max_nodes <= 250:
        raise ValueError("Lineage max nodes must be between 1 and 250.")

    if not 0.10 <= confidence_threshold <= 0.95:
        raise ValueError("Confidence threshold must be between 0.10 and 0.95.")

    if not 0 <= max_evidence_iterations <= 5:
        raise ValueError("Maximum evidence iterations must be between 0 and 5.")

    if normalized_manifest_uri and not SAFE_S3_URI.fullmatch(normalized_manifest_uri):
        raise ValueError("Manifest S3 URI must use a safe s3://bucket/path format.")

    if (
        normalized_manifest_uri
        and ".." in normalized_manifest_uri.removeprefix("s3://").split("/")
    ):
        raise ValueError("Manifest S3 URI cannot contain parent-directory traversal.")

    if (
        normalized_artifacts_bucket
        and not SAFE_S3_BUCKET.fullmatch(normalized_artifacts_bucket)
    ):
        raise ValueError("Artifacts bucket must be a valid S3 bucket name.")

    if not SAFE_ARTIFACT_PREFIX.fullmatch(normalized_artifacts_prefix):
        raise ValueError("Artifacts prefix contains unsupported characters.")

    if ".." in normalized_artifacts_prefix.split("/"):
        raise ValueError("Artifacts prefix cannot contain parent-directory traversal.")

    if not 0 <= token_budget <= 64_000:
        raise ValueError("Supervisor token budget must be between 0 and 64000.")

    if normalized_execution_mode == "single":
        if max_workers != 1 or max_concurrency != 1 or max_handoffs != 1:
            raise ValueError(
                "Single execution requires max_workers=1, max_concurrency=1, and max_handoffs=1."
            )
    else:
        if not 2 <= max_workers <= 10:
            raise ValueError("Fan-out max workers must be between 2 and 10.")

        if not 1 <= max_concurrency <= min(3, max_workers):
            raise ValueError("Fan-out concurrency must be between 1 and 3 and not exceed workers.")

        if not max_workers <= max_handoffs <= 10:
            raise ValueError("Fan-out handoff budget must cover every worker and cannot exceed 10.")

    if max_retries != 0:
        raise ValueError(
            "Supervisor retries must remain 0 until specialist side effects are idempotent."
        )

    if not 0 <= max_model_calls <= 10:
        raise ValueError("Supervisor max model calls must be between 0 and 10.")

    if not 0.0 <= estimated_cost_budget_usd <= 0.15:
        raise ValueError("Supervisor estimated cost budget must be between 0 and 0.15 USD.")

    if not 1_000 <= latency_budget_ms <= 900_000:
        raise ValueError("Supervisor latency budget must be between 1000 and 900000 ms.")

    if normalized_intent == "triage_alert" and not normalized_alert:
        raise ValueError("triage_alert requires an alert key or Alert Ref.")

    if normalized_intent in {"asset_context", "blast_radius"} and not normalized_asset:
        raise ValueError(f"{normalized_intent} requires qualified_name.")

    if normalized_intent == "trusted_asset_search" and not (
        normalized_query or normalized_question
    ):
        raise ValueError("trusted_asset_search requires query or question.")

    if normalized_intent == "review_sql" and not normalized_sql:
        raise ValueError("review_sql requires a SQL proposal or SQL file.")

    if normalized_intent == "schema_drift_assessment" and not (
        normalized_schema_run and normalized_asset
    ):
        raise ValueError(
            "schema_drift_assessment requires schema_run_id and qualified_name."
        )

    if normalized_sql and normalized_intent not in {"auto", "review_sql"}:
        raise ValueError("SQL proposal is accepted only for auto or review_sql intent.")

    if normalized_expected_decision and normalized_intent not in {"auto", "review_sql"}:
        raise ValueError("Expected SQL decision is valid only for auto or review_sql intent.")

    if normalized_schema_run and normalized_intent not in {
        "auto",
        "schema_drift_assessment",
    }:
        raise ValueError(
            "Schema run ID is accepted only for auto or schema_drift_assessment intent."
        )

    if normalized_schema_run and not normalized_asset:
        raise ValueError("Schema run ID requires qualified_name.")

    if normalized_schema_assessment and normalized_intent not in {
        "auto",
        "schema_drift_assessment",
    }:
        raise ValueError(
            "Expected schema assessment is valid only for auto or "
            "schema_drift_assessment intent."
        )

    encoded_sql = (
        base64.b64encode(normalized_sql.encode("utf-8")).decode("ascii")
        if normalized_sql
        else ""
    )

    return ValidatedSupervisorTrigger(
        intent=normalized_intent,
        question=normalized_question,
        alert_key=normalized_alert,
        qualified_name=normalized_asset,
        query=normalized_query,
        domain=normalized_domain,
        data_layer=normalized_data_layer,
        certification_status=normalized_certification,
        lifecycle_status=normalized_lifecycle,
        token_budget=token_budget,
        latency_budget_ms=latency_budget_ms,
        sql_proposal_base64=encoded_sql,
        sql_purpose=normalized_sql_purpose,
        sql_hard_limit=sql_hard_limit,
        sql_require_date_filter=bool(sql_require_date_filter),
        sql_max_scan_bytes=sql_max_scan_bytes,
        expected_sql_decision=normalized_expected_decision,
        schema_run_id=normalized_schema_run,
        schema_finding_limit=schema_finding_limit,
        expected_schema_assessment=normalized_schema_assessment,
        result_limit=result_limit,
        max_depth=max_depth,
        max_nodes=max_nodes,
        confidence_threshold=confidence_threshold,
        max_evidence_iterations=max_evidence_iterations,
        manifest_s3_uri=normalized_manifest_uri,
        artifacts_bucket=normalized_artifacts_bucket,
        artifacts_prefix=normalized_artifacts_prefix,
        execution_mode=normalized_execution_mode,
        max_workers=max_workers,
        max_concurrency=max_concurrency,
        allow_external_llm=bool(allow_external_llm),
        max_handoffs=max_handoffs,
        max_retries=max_retries,
        max_model_calls=max_model_calls,
        estimated_cost_budget_usd=estimated_cost_budget_usd,
    )


# --- Defining Airflow Commands
def build_supervisor_run_id(intent: str) -> str:
    """
    Build a unique Asia/Bangkok-aligned Airflow run ID.

    Args:
        intent: Normalized supervisor intent.

    Returns:
        Manual Airflow run ID.
    """
    timestamp = datetime.now(LOCAL_TIMEZONE).strftime("%Y%m%dT%H%M%S%f")

    return f"manual__control_plane_{intent}_{timestamp}"


def validate_airflow_run_id(run_id: str) -> str:
    """
    Validate one explicit or generated Airflow DagRun identifier.

    Args:
        run_id: Candidate Airflow run identifier.

    Returns:
        Trimmed safe run identifier.

    Raises:
        ValueError: If the value contains shell-sensitive or unsupported characters.
    """
    normalized = run_id.strip()

    if not SAFE_AIRFLOW_RUN_ID.fullmatch(normalized):
        raise ValueError("Airflow run ID contains unsupported characters.")

    return normalized


def build_trigger_configuration(trigger: ValidatedSupervisorTrigger) -> dict[str, object]:
    """
    Build the complete DAG 98 configuration from validated typed values.

    Args:
        trigger: Validated supervisor trigger configuration.

    Returns:
        JSON-serializable Airflow DagRun configuration.
    """
    return {
        "intent": trigger.intent,
        "question": trigger.question,
        "alert_key": trigger.alert_key,
        "qualified_name": trigger.qualified_name,
        "query": trigger.query,
        "domain": trigger.domain,
        "data_layer": trigger.data_layer,
        "certification_status": trigger.certification_status,
        "lifecycle_status": trigger.lifecycle_status,
        "execution_mode": trigger.execution_mode,
        "max_workers": trigger.max_workers,
        "max_concurrency": trigger.max_concurrency,
        "allow_external_llm": trigger.allow_external_llm,
        "expected_worker_count": 0,
        "max_handoffs": trigger.max_handoffs,
        "max_retries": trigger.max_retries,
        "max_model_calls": trigger.max_model_calls,
        "token_budget": trigger.token_budget,
        "estimated_cost_budget_usd": trigger.estimated_cost_budget_usd,
        "latency_budget_ms": trigger.latency_budget_ms,
        "sql_proposal_base64": trigger.sql_proposal_base64,
        "sql_purpose": trigger.sql_purpose,
        "sql_hard_limit": trigger.sql_hard_limit,
        "sql_require_date_filter": trigger.sql_require_date_filter,
        "sql_max_scan_bytes": trigger.sql_max_scan_bytes,
        "expected_sql_decision": trigger.expected_sql_decision,
        "schema_run_id": trigger.schema_run_id,
        "schema_finding_limit": trigger.schema_finding_limit,
        "expected_schema_assessment": trigger.expected_schema_assessment,
        "result_limit": trigger.result_limit,
        "max_depth": trigger.max_depth,
        "max_nodes": trigger.max_nodes,
        "confidence_threshold": trigger.confidence_threshold,
        "max_evidence_iterations": trigger.max_evidence_iterations,
        "manifest_s3_uri": trigger.manifest_s3_uri,
        "artifacts_bucket": trigger.artifacts_bucket,
        "artifacts_prefix": trigger.artifacts_prefix,
    }


def build_validated_trigger_command(
    trigger: ValidatedSupervisorTrigger,
    run_id: str,
) -> list[str]:
    """
    Serialize one validated supervisor request into an Airflow CLI command.

    Args:
        trigger: Validated supervisor trigger configuration.
        run_id: Validated Airflow DagRun identifier.

    Returns:
        Subprocess-safe Airflow CLI argument list.
    """
    conf = json.dumps(
        build_trigger_configuration(trigger),
        separators=(",", ":"),
    )

    return [
        "airflow",
        "dags",
        "trigger",
        "-r",
        run_id,
        "-c",
        conf,
        "-o",
        "table",
        CONTROL_PLANE_SUPERVISOR_DAG_ID,
    ]


def build_trigger_command(
    intent: str,
    question: str,
    alert_key: str,
    qualified_name: str,
    query: str,
    token_budget: int,
    latency_budget_ms: int,
    run_id: str,
    domain: str = "",
    data_layer: str = "",
    certification_status: str = "",
    lifecycle_status: str = "",
    execution_mode: str = "single",
    max_workers: int = 1,
    max_concurrency: int = 1,
    allow_external_llm: bool = False,
    max_handoffs: int = 1,
    max_retries: int = 0,
    max_model_calls: int = 3,
    estimated_cost_budget_usd: float = 0.05,
    sql_proposal: str = "",
    sql_purpose: str = "",
    sql_hard_limit: int = 100,
    sql_require_date_filter: bool = True,
    sql_max_scan_bytes: int = 1024 * 1024 * 1024,
    expected_sql_decision: str = "",
    schema_run_id: str = "",
    schema_finding_limit: int = 50,
    expected_schema_assessment: str = "",
    result_limit: int = 10,
    max_depth: int = 5,
    max_nodes: int = 100,
    confidence_threshold: float = 0.70,
    max_evidence_iterations: int = 2,
    manifest_s3_uri: str = "",
    artifacts_bucket: str = "",
    artifacts_prefix: str = "agent-reports",
) -> list[str]:
    """
    Build one structured Airflow CLI trigger command.

    Args:
        intent: Requested supervisor intent.
        question: Optional bounded operator wording.
        alert_key: Optional Alert Ref or system alert key.
        qualified_name: Optional exact metadata asset.
        query: Optional trusted asset search query.
        token_budget: Aggregate token budget.
        latency_budget_ms: Aggregate latency budget.
        run_id: Explicit Airflow run ID.
        domain: Optional metadata domain filter.
        data_layer: Optional raw, staging, or mart filter.
        certification_status: Optional asset certification filter.
        lifecycle_status: Optional active or deprecated filter.
        execution_mode: Single-handoff or explicitly enabled fan-out mode.
        max_workers: Maximum tasks admitted into the fan-out plan.
        max_concurrency: Maximum workers allowed to execute concurrently.
        allow_external_llm: Request-level provider permission.
        max_handoffs: Maximum specialist handoffs for the pilot.
        max_retries: Maximum specialist retries; currently zero.
        max_model_calls: Maximum aggregate external provider attempts.
        estimated_cost_budget_usd: Maximum aggregate estimated provider cost.
        sql_proposal: Optional SQL statement for non-executing review.
        sql_purpose: Optional bounded operator reason.
        sql_hard_limit: Maximum result rows.
        sql_require_date_filter: Whether known large tables require dates.
        sql_max_scan_bytes: Maximum conservative scan upper bound.
        expected_sql_decision: Optional verifier expectation.
        schema_run_id: Exact persisted schema detector DagRun identifier.
        schema_finding_limit: Maximum persisted finding rows returned.
        expected_schema_assessment: Optional verifier compatibility expectation.
        result_limit: Maximum metadata rows returned.
        max_depth: Maximum lineage traversal depth.
        max_nodes: Maximum lineage graph nodes.
        confidence_threshold: Confidence required before skipping extra evidence.
        max_evidence_iterations: Maximum bounded extra-evidence loops.
        manifest_s3_uri: Optional system-owned dbt manifest artifact URI.
        artifacts_bucket: Optional report artifact bucket.
        artifacts_prefix: Relative S3 report prefix.

    Returns:
        Subprocess-safe Airflow CLI argument list.
    """
    trigger = validate_trigger_inputs(
        intent=intent,
        question=question,
        alert_key=alert_key,
        qualified_name=qualified_name,
        query=query,
        token_budget=token_budget,
        latency_budget_ms=latency_budget_ms,
        domain=domain,
        data_layer=data_layer,
        certification_status=certification_status,
        lifecycle_status=lifecycle_status,
        execution_mode=execution_mode,
        max_workers=max_workers,
        max_concurrency=max_concurrency,
        allow_external_llm=allow_external_llm,
        max_handoffs=max_handoffs,
        max_retries=max_retries,
        max_model_calls=max_model_calls,
        estimated_cost_budget_usd=estimated_cost_budget_usd,
        sql_proposal=sql_proposal,
        sql_purpose=sql_purpose,
        sql_hard_limit=sql_hard_limit,
        sql_require_date_filter=sql_require_date_filter,
        sql_max_scan_bytes=sql_max_scan_bytes,
        expected_sql_decision=expected_sql_decision,
        schema_run_id=schema_run_id,
        schema_finding_limit=schema_finding_limit,
        expected_schema_assessment=expected_schema_assessment,
        result_limit=result_limit,
        max_depth=max_depth,
        max_nodes=max_nodes,
        confidence_threshold=confidence_threshold,
        max_evidence_iterations=max_evidence_iterations,
        manifest_s3_uri=manifest_s3_uri,
        artifacts_bucket=artifacts_bucket,
        artifacts_prefix=artifacts_prefix,
    )
    resolved_run_id = validate_airflow_run_id(run_id)

    return build_validated_trigger_command(trigger=trigger, run_id=resolved_run_id)


def run_command(command: list[str]) -> None:
    """
    Run one Airflow CLI command and stream output.

    Args:
        command: Subprocess argument list.

    Returns:
        None.

    Raises:
        CalledProcessError: If Airflow CLI returns non-zero.
    """
    logger.info("Running Airflow supervisor control command | command=%s", command)
    subprocess.run(command, check=True)


def trigger_control_plane_supervisor(
    intent: str,
    question: str,
    alert_key: str,
    qualified_name: str,
    query: str,
    token_budget: int,
    latency_budget_ms: int,
    run_id: str = "",
    domain: str = "",
    data_layer: str = "",
    certification_status: str = "",
    lifecycle_status: str = "",
    execution_mode: str = "single",
    max_workers: int = 1,
    max_concurrency: int = 1,
    allow_external_llm: bool = False,
    max_handoffs: int = 1,
    max_retries: int = 0,
    max_model_calls: int = 3,
    estimated_cost_budget_usd: float = 0.05,
    sql_proposal: str = "",
    sql_purpose: str = "",
    sql_hard_limit: int = 100,
    sql_require_date_filter: bool = True,
    sql_max_scan_bytes: int = 1024 * 1024 * 1024,
    expected_sql_decision: str = "",
    schema_run_id: str = "",
    schema_finding_limit: int = 50,
    expected_schema_assessment: str = "",
    result_limit: int = 10,
    max_depth: int = 5,
    max_nodes: int = 100,
    confidence_threshold: float = 0.70,
    max_evidence_iterations: int = 2,
    manifest_s3_uri: str = "",
    artifacts_bucket: str = "",
    artifacts_prefix: str = "agent-reports",
) -> str:
    """
    Unpause and trigger the manual supervisor smoke DAG.

    Args:
        intent: Requested supervisor intent.
        question: Optional bounded operator wording.
        alert_key: Optional Alert Ref or system alert key.
        qualified_name: Optional exact metadata asset.
        query: Optional trusted metadata search query.
        token_budget: Aggregate token budget.
        latency_budget_ms: Aggregate latency budget.
        run_id: Optional explicit Airflow run ID.
        domain: Optional metadata domain filter.
        data_layer: Optional raw, staging, or mart filter.
        certification_status: Optional asset certification filter.
        lifecycle_status: Optional active or deprecated filter.
        execution_mode: Single-handoff or explicitly enabled fan-out mode.
        max_workers: Maximum tasks admitted into the fan-out plan.
        max_concurrency: Maximum workers allowed to execute concurrently.
        allow_external_llm: Request-level provider permission.
        max_handoffs: Maximum specialist handoffs for the pilot.
        max_retries: Maximum specialist retries; currently zero.
        max_model_calls: Maximum aggregate external provider attempts.
        estimated_cost_budget_usd: Maximum aggregate estimated provider cost.
        sql_proposal: Optional SQL statement for non-executing review.
        sql_purpose: Optional bounded query purpose.
        sql_hard_limit: Maximum result rows.
        sql_require_date_filter: Whether known large tables require dates.
        sql_max_scan_bytes: Maximum conservative scan upper bound.
        expected_sql_decision: Optional verifier expectation.
        schema_run_id: Exact persisted schema detector DagRun identifier.
        schema_finding_limit: Maximum persisted finding rows returned.
        expected_schema_assessment: Optional verifier compatibility expectation.
        result_limit: Maximum metadata rows returned.
        max_depth: Maximum lineage traversal depth.
        max_nodes: Maximum lineage graph nodes.
        confidence_threshold: Confidence required before skipping extra evidence.
        max_evidence_iterations: Maximum bounded extra-evidence loops.
        manifest_s3_uri: Optional system-owned dbt manifest artifact URI.
        artifacts_bucket: Optional report artifact bucket.
        artifacts_prefix: Relative S3 report prefix.

    Returns:
        Airflow run ID created for the supervisor request.
    """
    trigger = validate_trigger_inputs(
        intent=intent,
        question=question,
        alert_key=alert_key,
        qualified_name=qualified_name,
        query=query,
        token_budget=token_budget,
        latency_budget_ms=latency_budget_ms,
        domain=domain,
        data_layer=data_layer,
        certification_status=certification_status,
        lifecycle_status=lifecycle_status,
        execution_mode=execution_mode,
        max_workers=max_workers,
        max_concurrency=max_concurrency,
        allow_external_llm=allow_external_llm,
        max_handoffs=max_handoffs,
        max_retries=max_retries,
        max_model_calls=max_model_calls,
        estimated_cost_budget_usd=estimated_cost_budget_usd,
        sql_proposal=sql_proposal,
        sql_purpose=sql_purpose,
        sql_hard_limit=sql_hard_limit,
        sql_require_date_filter=sql_require_date_filter,
        sql_max_scan_bytes=sql_max_scan_bytes,
        expected_sql_decision=expected_sql_decision,
        schema_run_id=schema_run_id,
        schema_finding_limit=schema_finding_limit,
        expected_schema_assessment=expected_schema_assessment,
        result_limit=result_limit,
        max_depth=max_depth,
        max_nodes=max_nodes,
        confidence_threshold=confidence_threshold,
        max_evidence_iterations=max_evidence_iterations,
        manifest_s3_uri=manifest_s3_uri,
        artifacts_bucket=artifacts_bucket,
        artifacts_prefix=artifacts_prefix,
    )
    resolved_run_id = validate_airflow_run_id(
        run_id.strip() or build_supervisor_run_id(trigger.intent)
    )

    run_command(["airflow", "dags", "unpause", CONTROL_PLANE_SUPERVISOR_DAG_ID])
    run_command(
        build_validated_trigger_command(
            trigger=trigger,
            run_id=resolved_run_id,
        )
    )

    print(f"CONTROL_PLANE_SUPERVISOR_DAG_ID={CONTROL_PLANE_SUPERVISOR_DAG_ID}")
    print(f"CONTROL_PLANE_SUPERVISOR_RUN_ID={resolved_run_id}")
    print(f"CONTROL_PLANE_SUPERVISOR_INTENT={trigger.intent}")
    print(f"CONTROL_PLANE_EXECUTION_MODE={trigger.execution_mode}")
    print(f"CONTROL_PLANE_MAX_WORKERS={trigger.max_workers}")
    print(f"CONTROL_PLANE_MAX_CONCURRENCY={trigger.max_concurrency}")
    print(f"CONTROL_PLANE_EXTERNAL_LLM_ALLOWED={trigger.allow_external_llm}")

    if trigger.schema_run_id:
        print(f"CONTROL_PLANE_SCHEMA_RUN_ID={trigger.schema_run_id}")

    return resolved_run_id


# --- Defining CLI
def build_parser() -> argparse.ArgumentParser:
    """
    Build the supervisor Airflow trigger CLI parser.

    Returns:
        Configured ArgumentParser.
    """
    parser = argparse.ArgumentParser(
        description="Trigger the manual Control Plane Supervisor smoke DAG."
    )

    parser.add_argument("--intent", default="asset_context", choices=SUPPORTED_INTENTS)
    parser.add_argument("--question", default="")
    parser.add_argument("--alert-key", default="")
    parser.add_argument("--qualified-name", default=None)
    parser.add_argument("--query", default=None)
    parser.add_argument("--domain", default="")
    parser.add_argument("--data-layer", default="", choices=SUPPORTED_DATA_LAYERS)
    parser.add_argument(
        "--certification-status",
        default="",
        choices=SUPPORTED_CERTIFICATION_STATUSES,
    )
    parser.add_argument(
        "--lifecycle-status",
        default="",
        choices=SUPPORTED_LIFECYCLE_STATUSES,
    )
    parser.add_argument(
        "--sql-file",
        default=None,
        help="Repository-owned SQL file used for review_sql; defaults to a safe smoke proposal.",
    )
    parser.add_argument("--sql-purpose", default="Airflow SQL safety review smoke test")
    parser.add_argument("--sql-hard-limit", type=int, default=100)
    parser.add_argument(
        "--sql-require-date-filter",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument("--sql-max-scan-bytes", type=int, default=1024 * 1024 * 1024)
    parser.add_argument(
        "--expected-sql-decision",
        default="",
        choices=SUPPORTED_SQL_DECISIONS,
    )
    parser.add_argument("--schema-run-id", default="")
    parser.add_argument("--schema-finding-limit", type=int, default=50)
    parser.add_argument(
        "--expected-schema-assessment",
        default="",
        choices=SUPPORTED_SCHEMA_ASSESSMENTS,
    )
    parser.add_argument("--result-limit", type=int, default=10)
    parser.add_argument("--max-depth", type=int, default=5)
    parser.add_argument("--max-nodes", type=int, default=100)
    parser.add_argument("--confidence-threshold", type=float, default=0.70)
    parser.add_argument("--max-evidence-iterations", type=int, default=2)
    parser.add_argument("--manifest-s3-uri", default="")
    parser.add_argument("--artifacts-bucket", default="")
    parser.add_argument("--artifacts-prefix", default="agent-reports")
    parser.add_argument(
        "--execution-mode",
        default="single",
        choices=SUPPORTED_EXECUTION_MODES,
    )
    parser.add_argument("--max-workers", type=int, default=1)
    parser.add_argument("--max-concurrency", type=int, default=1)
    parser.add_argument(
        "--allow-external-llm",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    parser.add_argument("--token-budget", type=int, default=16_384)
    parser.add_argument("--max-handoffs", type=int, default=1)
    parser.add_argument("--max-retries", type=int, default=0)
    parser.add_argument("--max-model-calls", type=int, default=3)
    parser.add_argument("--estimated-cost-budget-usd", type=float, default=0.05)
    parser.add_argument("--latency-budget-ms", type=int, default=300_000)
    parser.add_argument("--run-id", default="")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """
    Parse arguments and trigger one supervisor DagRun.

    Args:
        argv: Optional argument sequence used by tests.

    Returns:
        Zero when the Airflow trigger command succeeds.
    """
    args = build_parser().parse_args(argv)

    qualified_name, query = resolve_trigger_context_defaults(
        intent=args.intent,
        qualified_name=args.qualified_name,
        query=args.query,
    )
    sql_proposal = load_sql_proposal(
        intent=args.intent,
        sql_file=args.sql_file,
    )
    expected_sql_decision = args.expected_sql_decision

    if args.intent == "review_sql" and not expected_sql_decision:
        expected_sql_decision = "approved"

    expected_schema_assessment = args.expected_schema_assessment

    if args.intent == "schema_drift_assessment" and not expected_schema_assessment:
        expected_schema_assessment = "compatible"

    trigger_control_plane_supervisor(
        intent=args.intent,
        question=args.question,
        alert_key=args.alert_key,
        qualified_name=qualified_name,
        query=query,
        token_budget=args.token_budget,
        latency_budget_ms=args.latency_budget_ms,
        domain=args.domain,
        data_layer=args.data_layer,
        certification_status=args.certification_status,
        lifecycle_status=args.lifecycle_status,
        execution_mode=args.execution_mode,
        max_workers=args.max_workers,
        max_concurrency=args.max_concurrency,
        allow_external_llm=args.allow_external_llm,
        max_handoffs=args.max_handoffs,
        max_retries=args.max_retries,
        max_model_calls=args.max_model_calls,
        estimated_cost_budget_usd=args.estimated_cost_budget_usd,
        run_id=args.run_id,
        sql_proposal=sql_proposal,
        sql_purpose=args.sql_purpose if sql_proposal else "",
        sql_hard_limit=args.sql_hard_limit,
        sql_require_date_filter=args.sql_require_date_filter,
        sql_max_scan_bytes=args.sql_max_scan_bytes,
        expected_sql_decision=expected_sql_decision,
        schema_run_id=args.schema_run_id,
        schema_finding_limit=args.schema_finding_limit,
        expected_schema_assessment=expected_schema_assessment,
        result_limit=args.result_limit,
        max_depth=args.max_depth,
        max_nodes=args.max_nodes,
        confidence_threshold=args.confidence_threshold,
        max_evidence_iterations=args.max_evidence_iterations,
        manifest_s3_uri=args.manifest_s3_uri,
        artifacts_bucket=args.artifacts_bucket,
        artifacts_prefix=args.artifacts_prefix,
    )

    return 0


# --- Running CLI Entrypoint
if __name__ == "__main__":
    raise SystemExit(main())
