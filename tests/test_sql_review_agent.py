####
## SQL Safety And Review Agent Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Validate SQL review policy, trust, scan risk, audit, and non-execution boundaries."""

# --- Importing Libraries
from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from agent.specialists.contracts import AgentTaskStatus
from agent.specialists.sql_review import (
    SqlReviewAgentOutput,
    SqlReviewRuntimeConfig,
    build_sql_review_task,
    run_sql_review_agent,
)
from agent.tools.sql_review import (
    SqlReviewDecision,
    SqlRiskLevel,
    TableScanEstimate,
    build_sql_guardrail_review,
    build_table_statistics_sql,
    extract_referenced_tables,
)


# --- Defining Test Doubles
class AuditRecorder:
    """Capture SQL specialist audit writes without a live ClickHouse table."""

    def __init__(self) -> None:
        """Initialize an empty event list."""
        self.events: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> UUID:
        """
        Record one append-only event.

        Args:
            kwargs: Audit writer keyword arguments.

        Returns:
            Correlated parent run UUID.
        """
        self.events.append(kwargs)

        return UUID(str(kwargs["agent_run_id"]))


class DependencyRecorder:
    """Return deterministic metadata/statistics while retaining requested inputs."""

    def __init__(
        self,
        metadata_found: bool = True,
        active_bytes: int = 10_000,
    ) -> None:
        """
        Configure deterministic dependency behavior.

        Args:
            metadata_found: Whether exact metadata lookup succeeds.
            active_bytes: Active-part upper bound returned for each table.
        """
        self.metadata_found = metadata_found
        self.active_bytes   = active_bytes
        self.metadata_calls: list[str] = []
        self.statistics_calls: list[list[str]] = []

    def get_metadata(self, **kwargs: Any) -> dict[str, Any]:
        """
        Return one active candidate warehouse asset.

        Args:
            kwargs: Exact metadata lookup arguments.

        Returns:
            Public metadata asset dictionary.

        Raises:
            LookupError: If configured to simulate an unregistered asset.
        """
        qualified_name = str(kwargs["qualified_name"])
        self.metadata_calls.append(qualified_name)

        if not self.metadata_found:
            raise LookupError(f"Metadata asset not found: {qualified_name}")

        return {
            "qualified_name": qualified_name,
            "certification_status": "candidate",
            "lifecycle_status": "active",
            "sensitivity": "internal",
            "contains_pii": False,
        }

    def get_statistics(self, **kwargs: Any) -> list[TableScanEstimate]:
        """
        Return conservative active-part estimates without receiving proposal SQL.

        Args:
            kwargs: Statistics lookup arguments.

        Returns:
            One estimate per qualified table.
        """
        qualified_names = list(kwargs["qualified_names"])
        self.statistics_calls.append(qualified_names)

        return [
            TableScanEstimate(
                qualified_name=qualified_name,
                active_rows=100,
                active_bytes=self.active_bytes,
                active_parts=1,
                estimate_status="estimated",
                estimate_basis="test active-parts upper bound",
                risk_level=SqlRiskLevel.LOW,
            )
            for qualified_name in qualified_names
        ]


# --- Defining Fixtures
def safe_orders_sql() -> str:
    """
    Return one date-filtered and bounded warehouse query.

    Returns:
        Safe SQL proposal used by deterministic review tests.
    """
    return (
        "SELECT country, count() AS order_count "
        "FROM dq.raw_orders "
        "WHERE dt = toDate('2026-08-08') "
        "GROUP BY country "
        "LIMIT 50"
    )


def runtime_config(
    dependencies: DependencyRecorder,
    audit: AuditRecorder,
) -> SqlReviewRuntimeConfig:
    """
    Build SQL review runtime dependencies without live services.

    Args:
        dependencies: Metadata/statistics test double.
        audit: Append-only audit recorder.

    Returns:
        SqlReviewRuntimeConfig for isolated tests.
    """
    return SqlReviewRuntimeConfig(
        metadata_getter=dependencies.get_metadata,
        statistics_fetcher=dependencies.get_statistics,
        audit_client_factory=lambda **_: object(),
        audit_writer=audit,
    )


# --- Testing Static SQL Parsing And Policy
def test_extract_referenced_tables_ignores_cte_aliases() -> None:
    """Physical tables must be retained while CTE aliases are excluded."""
    sql = (
        "WITH recent AS ("
        "SELECT * FROM dq.raw_orders WHERE dt = toDate('2026-08-08')"
        ") SELECT country FROM recent LIMIT 10"
    )

    assert extract_referenced_tables(sql) == ["dq.raw_orders"]


def test_static_review_rejects_missing_date_filter() -> None:
    """Known large tables must retain a date predicate before any evidence query."""
    review = build_sql_guardrail_review(
        sql_proposal="SELECT country FROM dq.raw_orders LIMIT 10",
        hard_limit=100,
        require_date_filter=True,
    )

    assert review.guardrail_passed is False
    assert review.guarded_sql == ""
    assert review.findings[0].code == "sql_guardrail_rejected"


def test_static_review_enforces_hard_limit_without_execution() -> None:
    """A safe proposal may be rewritten with a hard LIMIT but is not executed."""
    review = build_sql_guardrail_review(
        sql_proposal=(
            "SELECT country FROM dq.raw_orders "
            "WHERE dt = toDate('2026-08-08')"
        ),
        hard_limit=25,
        require_date_filter=True,
    )

    assert review.guardrail_passed is True
    assert review.guarded_sql.endswith("LIMIT 25")
    assert "limit_added_25" in review.guardrails_applied
    assert review.referenced_tables == ["dq.raw_orders"]


def test_statistics_sql_uses_only_fixed_system_parts_query() -> None:
    """Scan estimation must use validated identities and never embed proposal SQL."""
    sql = build_table_statistics_sql(["dq.raw_orders", "dq.fct_orders_daily"])

    assert "FROM system.parts" in sql
    assert "database = 'dq'" in sql
    assert "table = 'raw_orders'" in sql
    assert "table = 'fct_orders_daily'" in sql
    assert "LIMIT 2" in sql


# --- Testing Specialist Decisions
def test_sql_review_approves_safe_candidate_asset_without_executing_sql() -> None:
    """Candidate metadata may warn, but a safe bounded query remains approved."""
    audit        = AuditRecorder()
    dependencies = DependencyRecorder()
    task         = build_sql_review_task(
        parent_run_id=uuid4(),
        sql_proposal=safe_orders_sql(),
    )
    result = run_sql_review_agent(
        task=task,
        config=runtime_config(dependencies, audit),
    )

    assert result.status == AgentTaskStatus.SUCCESS
    assert result.structured_output["decision"] == SqlReviewDecision.APPROVED.value
    assert result.structured_output["execution_performed"] is False
    assert result.structured_output["reviewed_tables"][0]["trust_status"] == "review"
    assert dependencies.metadata_calls == ["dq.raw_orders"]
    assert dependencies.statistics_calls == [["dq.raw_orders"]]
    assert {event["action"] for event in audit.events} == {
        "specialist_handoff_started",
        "review_sql_policy",
        "specialist_handoff_completed",
    }
    assert all(
        "sql_proposal" not in event["input_payload"]
        for event in audit.events
    )


def test_sql_review_returns_policy_rejection_as_successful_review() -> None:
    """Unsafe SQL is a rejected decision, not an unhandled specialist failure."""
    audit        = AuditRecorder()
    dependencies = DependencyRecorder()
    task         = build_sql_review_task(
        parent_run_id=uuid4(),
        sql_proposal="DELETE FROM dq.raw_orders WHERE dt = toDate('2026-08-08')",
    )
    result = run_sql_review_agent(
        task=task,
        config=runtime_config(dependencies, audit),
    )

    assert result.status == AgentTaskStatus.SUCCESS
    assert result.structured_output["decision"] == SqlReviewDecision.REJECTED.value
    assert result.structured_output["execution_performed"] is False
    assert dependencies.metadata_calls == []
    assert dependencies.statistics_calls == []
    assert next(
        event for event in audit.events if event["action"] == "review_sql_policy"
    )["status"] == "rejected"


def test_sql_review_rejects_unregistered_table() -> None:
    """A read-only query cannot be approved when its table lacks trust metadata."""
    audit        = AuditRecorder()
    dependencies = DependencyRecorder(metadata_found=False)
    task         = build_sql_review_task(
        parent_run_id=uuid4(),
        sql_proposal=safe_orders_sql(),
    )
    result = run_sql_review_agent(
        task=task,
        config=runtime_config(dependencies, audit),
    )

    assert result.status == AgentTaskStatus.SUCCESS
    assert result.structured_output["decision"] == SqlReviewDecision.REJECTED.value
    assert result.structured_output["reviewed_tables"][0]["registry_found"] is False
    assert result.structured_output["execution_performed"] is False


def test_sql_review_rejects_conservative_scan_budget_excess() -> None:
    """An active-part upper bound above policy budget must block approval."""
    audit        = AuditRecorder()
    dependencies = DependencyRecorder(active_bytes=20 * 1024 * 1024)
    task         = build_sql_review_task(
        parent_run_id=uuid4(),
        sql_proposal=safe_orders_sql(),
        max_scan_bytes=10 * 1024 * 1024,
    )
    result = run_sql_review_agent(
        task=task,
        config=runtime_config(dependencies, audit),
    )

    assert result.status == AgentTaskStatus.SUCCESS
    assert result.structured_output["decision"] == SqlReviewDecision.REJECTED.value
    assert any(
        finding["code"] == "scan_budget_exceeded"
        for finding in result.structured_output["policy_findings"]
    )


def test_sql_review_output_cannot_claim_execution() -> None:
    """The structured contract must reject any execution claim by construction."""
    with pytest.raises(ValidationError, match="cannot execute SQL proposals"):
        SqlReviewAgentOutput(
            decision=SqlReviewDecision.APPROVED,
            summary="Invalid execution claim.",
            proposal_sql_hash="a" * 64,
            max_scan_bytes=1024 * 1024,
            query_risk_level=SqlRiskLevel.LOW,
            estimate_basis="test",
            execution_performed=True,
        )

