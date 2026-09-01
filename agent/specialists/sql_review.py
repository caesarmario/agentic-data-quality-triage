####
## SQL Safety And Review Specialist for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Approve or reject SQL proposals using deterministic policy and warehouse evidence."""

# --- Importing Libraries
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent.specialists.contracts import (
    AgentModelRoute,
    AgentResultEnvelope,
    AgentRiskTier,
    AgentTaskEnvelope,
    AgentTaskStatus,
    EvidenceReference,
)
from agent.specialists.registry import (
    SQL_REVIEW_SPECIALIST_NAME,
    enforce_task_capability,
    required_tools_for_task,
)
from agent.tools.audit_log import hash_sql, write_agent_audit_event
from agent.tools.metadata_catalog import get_metadata_asset
from agent.tools.sql_review import (
    SqlFindingSeverity,
    SqlPolicyFinding,
    SqlReviewDecision,
    SqlRiskLevel,
    TableScanEstimate,
    TableTrustAssessment,
    TableTrustStatus,
    assess_table_trust,
    build_sql_guardrail_review,
    fetch_table_statistics,
)
from pipelines.common.clickhouse import build_clickhouse_client
from pipelines.common.logging import logger


# --- Defining Constants
SPECIALIST_TOOL_NAME   = "sql_safety_review_agent"
DEFAULT_HARD_LIMIT     = 100
MAX_HARD_LIMIT         = 1_000
DEFAULT_MAX_SCAN_BYTES = 1024 * 1024 * 1024
MAX_MAX_SCAN_BYTES     = 1024 * 1024 * 1024 * 1024


# --- Defining Specialist Models
class SqlReviewTaskInput(BaseModel):
    """
    Define the bounded SQL proposal and deterministic review policy.

    Attributes:
        sql_proposal: Single SQL statement to review but never execute.
        purpose: Optional operator-provided reason for the query.
        hard_limit: Maximum result rows enforced in guarded SQL.
        require_date_filter: Whether known large tables require date predicates.
        max_scan_bytes: Maximum conservative active-part upper bound.
    """

    model_config = ConfigDict(extra="forbid")

    sql_proposal: str                = Field(min_length=1, max_length=20_000)
    purpose: str                     = Field(default="", max_length=500)
    hard_limit: int                  = Field(default=DEFAULT_HARD_LIMIT, ge=1, le=MAX_HARD_LIMIT)
    require_date_filter: bool        = True
    max_scan_bytes: int              = Field(
        default=DEFAULT_MAX_SCAN_BYTES,
        ge=1024 * 1024,
        le=MAX_MAX_SCAN_BYTES,
    )

    @field_validator("sql_proposal")
    @classmethod
    def normalize_sql_proposal(cls, value: str) -> str:
        """
        Normalize SQL outer whitespace while preserving valid multiline formatting.

        Args:
            value: Raw SQL proposal.

        Returns:
            Trimmed SQL proposal.

        Raises:
            ValueError: If the proposal contains a null byte.
        """
        normalized = value.strip()

        if "\x00" in normalized:
            raise ValueError("SQL proposal cannot contain null bytes.")

        return normalized

    @field_validator("purpose")
    @classmethod
    def normalize_purpose(cls, value: str) -> str:
        """
        Normalize the optional single-line query purpose.

        Args:
            value: Raw purpose text.

        Returns:
            Trimmed purpose.

        Raises:
            ValueError: If purpose contains line breaks.
        """
        normalized = value.strip()

        if "\n" in normalized or "\r" in normalized:
            raise ValueError("SQL review purpose must remain single-line.")

        return normalized


class SqlReviewAgentOutput(BaseModel):
    """
    Define the explainable and non-executing SQL review result.

    Attributes:
        decision: Approved or rejected policy decision.
        summary: Human-readable result.
        proposal_sql_hash: Stable SHA-256 proposal reference.
        guarded_sql: Read-only SQL after deterministic LIMIT enforcement.
        guardrails_applied: Existing execution-boundary guardrail labels.
        policy_findings: Explainable policy, trust, and cost findings.
        reviewed_tables: Registry-backed table trust assessments.
        scan_estimates: Conservative active-part scan evidence.
        total_estimated_scan_bytes: Sum of active-part upper bounds.
        max_scan_bytes: Caller policy budget.
        query_risk_level: Overall deterministic query risk.
        estimate_basis: Honest scan-estimate qualification.
        execution_performed: Must always remain false.
    """

    model_config = ConfigDict(extra="forbid")

    decision: SqlReviewDecision
    summary: str                                      = Field(min_length=1, max_length=2_000)
    proposal_sql_hash: str                            = Field(min_length=64, max_length=64)
    guarded_sql: str                                  = Field(default="", max_length=20_000)
    guardrails_applied: list[str]                     = Field(default_factory=list, max_length=30)
    policy_findings: list[SqlPolicyFinding]           = Field(default_factory=list, max_length=100)
    reviewed_tables: list[TableTrustAssessment]       = Field(default_factory=list, max_length=12)
    scan_estimates: list[TableScanEstimate]           = Field(default_factory=list, max_length=12)
    total_estimated_scan_bytes: int                   = Field(default=0, ge=0)
    max_scan_bytes: int                               = Field(ge=1024 * 1024)
    query_risk_level: SqlRiskLevel
    estimate_basis: str                               = Field(min_length=1, max_length=500)
    execution_performed: bool                         = False

    @model_validator(mode="after")
    def prevent_sql_execution_claim(self) -> "SqlReviewAgentOutput":
        """
        Ensure a reviewer can never claim that it executed the proposal.

        Returns:
            Current output when execution_performed remains false.

        Raises:
            ValueError: If any caller attempts to mark execution as performed.
        """
        if self.execution_performed:
            raise ValueError("SQL Review Agent cannot execute SQL proposals.")

        return self


@dataclass(frozen=True)
class SqlReviewRuntimeConfig:
    """
    Inject deterministic metadata, statistics, and audit dependencies.

    Attributes:
        metadata_getter: Exact trusted metadata lookup callable.
        statistics_fetcher: Fixed ClickHouse active-part statistics callable.
        audit_client_factory: ClickHouse client factory for handoff audits.
        audit_writer: Append-only audit event writer.
        clickhouse_host: Optional ClickHouse host override.
        clickhouse_port: Optional ClickHouse HTTP port override.
    """

    metadata_getter: Callable[..., dict[str, Any]] = get_metadata_asset
    statistics_fetcher: Callable[..., list[TableScanEstimate]] = fetch_table_statistics
    audit_client_factory: Callable[..., Any] = build_clickhouse_client
    audit_writer: Callable[..., UUID]        = write_agent_audit_event
    clickhouse_host: str | None              = None
    clickhouse_port: int | None              = None


# --- Validating And Building Handoffs
def validate_sql_review_task(task: AgentTaskEnvelope) -> SqlReviewTaskInput:
    """
    Validate specialist capability and bounded SQL review input.

    Args:
        task: Typed supervisor handoff.

    Returns:
        Validated SqlReviewTaskInput.
    """
    enforce_task_capability(task)
    task_input = SqlReviewTaskInput.model_validate(task.input_payload)

    logger.info(
        "Validated SQL review task | task_id=%s proposal_hash=%s hard_limit=%d",
        task.task_id,
        hash_sql(task_input.sql_proposal),
        task_input.hard_limit,
    )

    return task_input


def build_sql_review_task(
    parent_run_id: UUID,
    sql_proposal: str,
    purpose: str = "",
    hard_limit: int = DEFAULT_HARD_LIMIT,
    require_date_filter: bool = True,
    max_scan_bytes: int = DEFAULT_MAX_SCAN_BYTES,
    requester: str = "control_plane",
    alert_key: str = "",
) -> AgentTaskEnvelope:
    """
    Build one least-privilege SQL review task without executing the proposal.

    Args:
        parent_run_id: Parent supervisor correlation UUID.
        sql_proposal: SQL statement to review.
        purpose: Optional operator purpose.
        hard_limit: Maximum result rows allowed by guarded SQL.
        require_date_filter: Whether known large tables require a date predicate.
        max_scan_bytes: Conservative active-part scan budget.
        requester: Calling interface identity.
        alert_key: Optional alert correlation key.

    Returns:
        Validated AgentTaskEnvelope for the SQL Review Agent.
    """
    task_input = SqlReviewTaskInput(
        sql_proposal=sql_proposal,
        purpose=purpose,
        hard_limit=hard_limit,
        require_date_filter=require_date_filter,
        max_scan_bytes=max_scan_bytes,
    )
    task = AgentTaskEnvelope(
        parent_run_id=parent_run_id,
        specialist_name=SQL_REVIEW_SPECIALIST_NAME,
        task_type="review_sql",
        risk_tier=AgentRiskTier.MEDIUM,
        allowed_tools=required_tools_for_task(
            specialist_name=SQL_REVIEW_SPECIALIST_NAME,
            task_type="review_sql",
        ),
        model_route=AgentModelRoute.NO_LLM_FALLBACK,
        model_call_budget=0,
        token_budget=0,
        estimated_cost_budget_usd=0.0,
        timeout_seconds=60,
        requester=requester,
        alert_key=alert_key,
        input_payload=task_input.model_dump(mode="json"),
    )

    validate_sql_review_task(task)

    logger.info(
        "Built SQL review handoff | task_id=%s parent_run_id=%s proposal_hash=%s",
        task.task_id,
        task.parent_run_id,
        hash_sql(task_input.sql_proposal),
    )

    return task


# --- Resolving Deterministic Review Decisions
def resolve_query_risk(
    findings: list[SqlPolicyFinding],
    scan_estimates: list[TableScanEstimate],
    join_count: int,
) -> SqlRiskLevel:
    """
    Resolve overall risk from blocking findings, scan evidence, and join complexity.

    Args:
        findings: Policy and trust findings.
        scan_estimates: Conservative active-part estimates.
        join_count: Number of JOIN clauses.

    Returns:
        Deterministic query risk level.
    """
    scan_risks = {estimate.risk_level for estimate in scan_estimates}

    if SqlRiskLevel.CRITICAL in scan_risks:
        return SqlRiskLevel.CRITICAL

    if any(item.severity == SqlFindingSeverity.BLOCKING for item in findings):
        return SqlRiskLevel.HIGH

    if SqlRiskLevel.HIGH in scan_risks or join_count >= 3:
        return SqlRiskLevel.HIGH

    if (
        SqlRiskLevel.MEDIUM in scan_risks
        or join_count > 0
        or any(item.severity == SqlFindingSeverity.WARNING for item in findings)
    ):
        return SqlRiskLevel.MEDIUM

    return SqlRiskLevel.LOW


def build_review_summary(
    decision: SqlReviewDecision,
    findings: list[SqlPolicyFinding],
    reviewed_tables: list[TableTrustAssessment],
) -> str:
    """
    Build a compact operator-facing SQL review summary.

    Args:
        decision: Approved or rejected policy result.
        findings: Complete explainable finding list.
        reviewed_tables: Registry trust assessments.

    Returns:
        Human-readable review summary.
    """
    blocking_count = sum(
        item.severity == SqlFindingSeverity.BLOCKING
        for item in findings
    )
    warning_count = sum(
        item.severity == SqlFindingSeverity.WARNING
        for item in findings
    )

    if decision == SqlReviewDecision.APPROVED:
        return (
            "SQL proposal passed deterministic read-only policy. "
            f"Reviewed {len(reviewed_tables)} registered table(s) with "
            f"{warning_count} warning(s); the proposal was not executed."
        )

    return (
        "SQL proposal was rejected by deterministic policy. "
        f"Found {blocking_count} blocking issue(s) and {warning_count} warning(s); "
        "the proposal was not executed."
    )


def build_recommended_next_step(
    decision: SqlReviewDecision,
    findings: list[SqlPolicyFinding],
) -> str:
    """
    Build a safe next step from the final review decision.

    Args:
        decision: Approved or rejected disposition.
        findings: Explainable findings used by the decision.

    Returns:
        Operator-facing next step.
    """
    if decision == SqlReviewDecision.APPROVED:
        return (
            "Use only the returned guarded SQL through the existing read-only SQL execution "
            "tool when an explicit caller requests evidence collection."
        )

    first_blocking = next(
        (
            item.message
            for item in findings
            if item.severity == SqlFindingSeverity.BLOCKING
        ),
        "Resolve the blocking policy findings before submitting the SQL again.",
    )

    return f"Revise the proposal before execution. First blocking issue: {first_blocking}"


def perform_sql_review(
    task: AgentTaskEnvelope,
    task_input: SqlReviewTaskInput,
    config: SqlReviewRuntimeConfig,
) -> tuple[SqlReviewAgentOutput, list[EvidenceReference]]:
    """
    Run static policy, metadata trust, and scan estimation without SQL execution.

    Args:
        task: Correlated specialist handoff.
        task_input: Validated SQL review input.
        config: Runtime dependency configuration.

    Returns:
        Structured review output and deterministic evidence references.
    """
    proposal_hash = hash_sql(task_input.sql_proposal)
    static_review = build_sql_guardrail_review(
        sql_proposal=task_input.sql_proposal,
        hard_limit=task_input.hard_limit,
        require_date_filter=task_input.require_date_filter,
    )
    findings        = list(static_review.findings)
    reviewed_tables: list[TableTrustAssessment] = []
    scan_estimates: list[TableScanEstimate]      = []

    if static_review.guardrail_passed:
        qualified_tables = [
            table_name
            for table_name in static_review.referenced_tables
            if "." in table_name
        ]

        for qualified_name in qualified_tables:
            metadata_asset: dict[str, Any] | None = None

            try:
                metadata_asset = config.metadata_getter(
                    qualified_name=qualified_name,
                    agent_run_id=task.parent_run_id,
                    alert_key=task.alert_key,
                    clickhouse_host=config.clickhouse_host,
                    clickhouse_port=config.clickhouse_port,
                )

            except LookupError:
                logger.warning(
                    "SQL review metadata asset not found | table=%s task_id=%s",
                    qualified_name,
                    task.task_id,
                )

            assessment, trust_findings = assess_table_trust(
                qualified_name=qualified_name,
                metadata_asset=metadata_asset,
                uses_wildcard_projection=static_review.uses_wildcard_projection,
            )
            reviewed_tables.append(assessment)
            findings.extend(trust_findings)

        if qualified_tables:
            raw_estimates = config.statistics_fetcher(
                qualified_names=qualified_tables,
                agent_run_id=task.parent_run_id,
                alert_key=task.alert_key,
                clickhouse_host=config.clickhouse_host,
                clickhouse_port=config.clickhouse_port,
            )
            scan_estimates = [
                TableScanEstimate.model_validate(item)
                for item in raw_estimates
            ]

    total_scan_bytes = sum(item.active_bytes for item in scan_estimates)

    if total_scan_bytes > task_input.max_scan_bytes:
        findings.append(
            SqlPolicyFinding(
                code="scan_budget_exceeded",
                severity=SqlFindingSeverity.BLOCKING,
                message=(
                    f"Active-part upper bound {total_scan_bytes} bytes exceeds the configured "
                    f"review budget of {task_input.max_scan_bytes} bytes."
                ),
            )
        )

    decision = (
        SqlReviewDecision.REJECTED
        if any(item.severity == SqlFindingSeverity.BLOCKING for item in findings)
        else SqlReviewDecision.APPROVED
    )
    query_risk = resolve_query_risk(
        findings=findings,
        scan_estimates=scan_estimates,
        join_count=static_review.join_count,
    )
    output = SqlReviewAgentOutput(
        decision=decision,
        summary=build_review_summary(
            decision=decision,
            findings=findings,
            reviewed_tables=reviewed_tables,
        ),
        proposal_sql_hash=proposal_hash,
        guarded_sql=static_review.guarded_sql,
        guardrails_applied=static_review.guardrails_applied,
        policy_findings=findings,
        reviewed_tables=reviewed_tables,
        scan_estimates=scan_estimates,
        total_estimated_scan_bytes=total_scan_bytes,
        max_scan_bytes=task_input.max_scan_bytes,
        query_risk_level=query_risk,
        estimate_basis=(
            "active_parts_upper_bound when tables are present; no proposal SQL was executed"
        ),
        execution_performed=False,
    )
    evidence_references = [
        EvidenceReference(
            evidence_type="sql_policy_review",
            source_tool="sql_policy_review",
            reference=f"sha256:{proposal_hash}",
            summary=(
                f"Deterministic SQL policy decision={decision.value}; "
                f"blocking_findings={sum(item.severity == SqlFindingSeverity.BLOCKING for item in findings)}."
            ),
        )
    ]

    for assessment in reviewed_tables:
        evidence_references.append(
            EvidenceReference(
                evidence_type="metadata_trust",
                source_tool="metadata_catalog",
                reference=assessment.qualified_name,
                summary=f"Metadata trust status={assessment.trust_status.value}.",
            )
        )

    for estimate in scan_estimates:
        evidence_references.append(
            EvidenceReference(
                evidence_type="scan_upper_bound",
                source_tool="warehouse_statistics",
                reference=estimate.qualified_name,
                summary=(
                    f"Active-part upper bound={estimate.active_bytes} bytes; "
                    f"risk={estimate.risk_level.value}."
                ),
            )
        )

    return output, evidence_references


# --- Auditing Specialist Handoffs
def write_sql_review_audit(
    config: SqlReviewRuntimeConfig,
    client: Any,
    task: AgentTaskEnvelope,
    task_input: SqlReviewTaskInput,
    action: str,
    status: str,
    duration_ms: int | None = None,
    output_payload: dict[str, Any] | None = None,
    error_message: str = "",
) -> None:
    """
    Persist a SQL review event without storing raw proposal text.

    Args:
        config: Runtime dependencies containing the audit writer.
        client: ClickHouse audit client.
        task: Correlated specialist handoff.
        task_input: Validated SQL review input.
        action: Stable review lifecycle action.
        status: running, approved, rejected, blocked, or failed.
        duration_ms: Optional duration.
        output_payload: Optional bounded review output metadata.
        error_message: Optional sanitized error.

    Returns:
        None.
    """
    config.audit_writer(
        client=client,
        action=action,
        status=status,
        agent_run_id=task.parent_run_id,
        alert_key=task.alert_key,
        actor="supervisor_lite",
        tool_name=SPECIALIST_TOOL_NAME,
        duration_ms=duration_ms,
        input_payload={
            "task_id": str(task.task_id),
            "parent_run_id": str(task.parent_run_id),
            "specialist_name": task.specialist_name,
            "task_type": task.task_type,
            "risk_tier": task.risk_tier.value,
            "allowed_tools": list(task.allowed_tools),
            "model_route": task.model_route.value,
            "model_call_budget": task.model_call_budget,
            "token_budget": task.token_budget,
            "estimated_cost_budget_usd": task.estimated_cost_budget_usd,
            "timeout_seconds": task.timeout_seconds,
            "requester": task.requester,
            "proposal_sql_hash": hash_sql(task_input.sql_proposal),
            "has_purpose": bool(task_input.purpose),
            "hard_limit": task_input.hard_limit,
            "require_date_filter": task_input.require_date_filter,
            "max_scan_bytes": task_input.max_scan_bytes,
        },
        output_payload=output_payload or {},
        error_message=error_message,
        sql=task_input.sql_proposal,
    )


def sanitize_specialist_error(exc: Exception) -> str:
    """
    Build a bounded single-line SQL specialist error.

    Args:
        exc: Policy, metadata, statistics, or audit exception.

    Returns:
        Sanitized error type and message.
    """
    return f"{type(exc).__name__}: {' '.join(str(exc).split())[:1_500]}"


def build_failed_result(
    task: AgentTaskEnvelope,
    status: AgentTaskStatus,
    error_message: str,
    duration_ms: int,
) -> AgentResultEnvelope:
    """
    Isolate specialist policy or tool failures at the handoff boundary.

    Args:
        task: Source handoff.
        status: Blocked or failed state.
        error_message: Sanitized error detail.
        duration_ms: Runtime before failure.

    Returns:
        Failure-isolated AgentResultEnvelope.
    """
    return AgentResultEnvelope(
        task_id=task.task_id,
        parent_run_id=task.parent_run_id,
        specialist_name=task.specialist_name,
        task_type=task.task_type,
        status=status,
        confidence=0.0,
        model_route=AgentModelRoute.NO_LLM_FALLBACK,
        token_usage=0,
        estimated_cost_usd=0.0,
        duration_ms=duration_ms,
        errors=[error_message],
        recommended_next_step="Review the handoff policy or deterministic evidence failure.",
        requires_human_approval=False,
    )


# --- Running The Specialist
def run_sql_review_agent(
    task: AgentTaskEnvelope,
    config: SqlReviewRuntimeConfig | None = None,
) -> AgentResultEnvelope:
    """
    Execute one bounded SQL review and never execute the proposed statement.

    Args:
        task: Typed supervisor handoff.
        config: Optional dependency overrides.

    Returns:
        Success, blocked, or failed result with explicit non-execution evidence.
    """
    runtime       = config or SqlReviewRuntimeConfig()
    started       = time.monotonic()
    audit_client: Any | None = None
    task_input: SqlReviewTaskInput | None = None

    try:
        audit_client = runtime.audit_client_factory(
            host=runtime.clickhouse_host,
            port=runtime.clickhouse_port,
        )

        try:
            task_input = validate_sql_review_task(task)

        except (LookupError, PermissionError, ValueError) as exc:
            duration_ms   = int((time.monotonic() - started) * 1_000)
            error_message = sanitize_specialist_error(exc)
            failed_input  = SqlReviewTaskInput.model_construct(
                sql_proposal=str(task.input_payload.get("sql_proposal", "invalid")) or "invalid",
                purpose="",
                hard_limit=DEFAULT_HARD_LIMIT,
                require_date_filter=True,
                max_scan_bytes=DEFAULT_MAX_SCAN_BYTES,
            )
            blocked_result = build_failed_result(
                task=task,
                status=AgentTaskStatus.BLOCKED,
                error_message=error_message,
                duration_ms=duration_ms,
            )
            write_sql_review_audit(
                config=runtime,
                client=audit_client,
                task=task,
                task_input=failed_input,
                action="specialist_handoff_rejected",
                status="blocked",
                duration_ms=duration_ms,
                output_payload={"result_status": blocked_result.status.value},
                error_message=error_message,
            )

            return blocked_result

        write_sql_review_audit(
            config=runtime,
            client=audit_client,
            task=task,
            task_input=task_input,
            action="specialist_handoff_started",
            status="running",
        )
        output, evidence_references = perform_sql_review(
            task=task,
            task_input=task_input,
            config=runtime,
        )
        duration_ms = int((time.monotonic() - started) * 1_000)
        recommended_next_step = build_recommended_next_step(
            decision=output.decision,
            findings=output.policy_findings,
        )
        final_result = AgentResultEnvelope(
            task_id=task.task_id,
            parent_run_id=task.parent_run_id,
            specialist_name=task.specialist_name,
            task_type=task.task_type,
            status=AgentTaskStatus.SUCCESS,
            evidence_references=evidence_references,
            structured_output=output.model_dump(mode="json"),
            confidence=1.0,
            model_route=AgentModelRoute.NO_LLM_FALLBACK,
            token_usage=0,
            estimated_cost_usd=0.0,
            duration_ms=duration_ms,
            recommended_next_step=recommended_next_step,
            requires_human_approval=False,
        )
        audit_payload = {
            "result_status": final_result.status.value,
            "decision": output.decision.value,
            "proposal_sql_hash": output.proposal_sql_hash,
            "query_risk_level": output.query_risk_level.value,
            "reviewed_tables": [item.qualified_name for item in output.reviewed_tables],
            "blocking_finding_count": sum(
                item.severity == SqlFindingSeverity.BLOCKING
                for item in output.policy_findings
            ),
            "total_estimated_scan_bytes": output.total_estimated_scan_bytes,
            "execution_performed": output.execution_performed,
            "model_route": final_result.model_route.value,
            "model_call_count": final_result.model_call_count,
            "token_usage": 0,
            "estimated_cost_usd": 0.0,
            "requires_human_approval": False,
        }
        write_sql_review_audit(
            config=runtime,
            client=audit_client,
            task=task,
            task_input=task_input,
            action="review_sql_policy",
            status=output.decision.value,
            duration_ms=duration_ms,
            output_payload=audit_payload,
        )
        write_sql_review_audit(
            config=runtime,
            client=audit_client,
            task=task,
            task_input=task_input,
            action="specialist_handoff_completed",
            status="success",
            duration_ms=duration_ms,
            output_payload=audit_payload,
        )

        logger.info(
            "SQL review handoff completed | task_id=%s decision=%s risk=%s execution_performed=false",
            task.task_id,
            output.decision.value,
            output.query_risk_level.value,
        )

        return final_result

    except Exception as exc:
        duration_ms   = int((time.monotonic() - started) * 1_000)
        error_message = sanitize_specialist_error(exc)
        failed_result = build_failed_result(
            task=task,
            status=AgentTaskStatus.FAILED,
            error_message=error_message,
            duration_ms=duration_ms,
        )

        logger.exception(
            "SQL review handoff failed | task_id=%s parent_run_id=%s",
            task.task_id,
            task.parent_run_id,
        )

        if audit_client is not None and task_input is not None:
            try:
                write_sql_review_audit(
                    config=runtime,
                    client=audit_client,
                    task=task,
                    task_input=task_input,
                    action="specialist_handoff_failed",
                    status="failed",
                    duration_ms=duration_ms,
                    output_payload={"result_status": failed_result.status.value},
                    error_message=error_message,
                )

            except Exception:
                logger.exception(
                    "Failed to persist SQL specialist failure audit | task_id=%s",
                    task.task_id,
                )

        return failed_result
