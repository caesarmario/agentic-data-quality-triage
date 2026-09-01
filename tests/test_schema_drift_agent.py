####
## Schema Drift Agent Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Validate exact-run schema assessment, impact policy, audit, and non-mutation."""

# --- Importing Libraries
from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from agent.specialists.contracts import AgentModelRoute, AgentTaskStatus
from agent.specialists.registry import (
    SCHEMA_DRIFT_ALLOWED_TOOLS,
    SCHEMA_DRIFT_SPECIALIST_NAME,
)
from agent.specialists.schema_drift import (
    SchemaChangeAssessment,
    SchemaDriftAgentOutput,
    SchemaDriftRuntimeConfig,
    SchemaImpactLevel,
    build_schema_drift_task,
    run_schema_drift_agent,
)


# --- Defining Test Doubles
class AuditRecorder:
    """Capture append-only schema specialist audit writes."""

    def __init__(self) -> None:
        """Initialize an empty event list."""
        self.events: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> UUID:
        """
        Record one audit event.

        Args:
            kwargs: Audit writer keyword arguments.

        Returns:
            Correlated parent run UUID.
        """
        self.events.append(kwargs)

        return UUID(str(kwargs["agent_run_id"]))


class SchemaDependencies:
    """Return deterministic schema, metadata, and blast-radius evidence."""

    def __init__(
        self,
        schema_context: dict[str, Any],
        schema_error: Exception | None = None,
    ) -> None:
        """
        Configure specialist dependency behavior.

        Args:
            schema_context: Exact persisted detector evidence returned to the agent.
            schema_error: Optional evidence failure raised by the schema tool.

        Returns:
            None.
        """
        self.schema_context = schema_context
        self.schema_error   = schema_error
        self.calls: list[str] = []

    def fetch_schema(self, **kwargs: Any) -> dict[str, Any]:
        """
        Return one exact persisted schema-run context.

        Args:
            kwargs: Exact run, table, bounds, and audit correlation.

        Returns:
            Persisted schema snapshot and bounded findings.

        Raises:
            Exception: Configured source evidence failure.
        """
        self.calls.append(
            f"schema:{kwargs['source_run_id']}:{kwargs['qualified_name']}"
        )

        if self.schema_error:
            raise self.schema_error

        return self.schema_context

    def get_metadata(self, **kwargs: Any) -> dict[str, Any]:
        """
        Return one trusted metadata asset.

        Args:
            kwargs: Exact warehouse asset and audit correlation.

        Returns:
            Public metadata context.
        """
        qualified_name = str(kwargs["qualified_name"])
        self.calls.append(f"metadata:{qualified_name}")

        return {
            "qualified_name": qualified_name,
            "technical_owner": "Data Platform Engineering",
            "domain": "commerce",
            "criticality": "high",
            "certification_status": "candidate",
            "lifecycle_status": "active",
        }

    def get_blast_radius(self, **kwargs: Any) -> dict[str, Any]:
        """
        Return bounded dbt downstream impact.

        Args:
            kwargs: Exact root table, traversal bounds, and audit correlation.

        Returns:
            Bounded downstream asset and test counts.
        """
        table_name = str(kwargs["table_name"])
        self.calls.append(f"blast:{table_name}")

        return {
            "table_name": table_name,
            "matched": True,
            "node": {
                "unique_id": "source.dq.raw_orders",
                "resource_type": "source",
            },
            "impacted_asset_count": 2,
            "impacted_test_count": 1,
            "total_impacted_nodes": 3,
            "truncated": False,
            "summary": "Found two downstream assets and one dbt test.",
        }


# --- Defining Fixtures
def schema_context(
    findings: list[dict[str, Any]] | None = None,
    highest_severity: str = "info",
) -> dict[str, Any]:
    """
    Build one exact persisted schema detector context.

    Args:
        findings: Optional persisted warning or failure rows.
        highest_severity: Persisted snapshot severity.

    Returns:
        Source-run snapshot and bounded findings.
    """
    rows  = findings or []
    count = len(rows)

    return {
        "status": "success",
        "run_id": "manual__schema_agent_source",
        "table_name": "dq.raw_orders",
        "snapshot": {
            "run_id": "manual__schema_agent_source",
            "contract_name": "orders_warehouse_schema",
            "contract_version": 1,
            "contract_sha256": "a" * 64,
            "qualified_name": "dq.raw_orders",
            "schema_sha256": "b" * 64,
            "snapshot_status": "pass" if count == 0 else "fail",
            "highest_severity": highest_severity,
            "comparison_count": 20,
            "finding_count": count,
        },
        "findings": rows,
        "finding_count": count,
        "visible_finding_count": count,
        "findings_truncated": 0,
        "summary": (
            "The persisted schema matches the configured contract."
            if count == 0
            else f"The persisted schema contains {count} finding(s)."
        ),
    }


def runtime_config(
    dependencies: SchemaDependencies,
    audit: AuditRecorder,
) -> SchemaDriftRuntimeConfig:
    """
    Build an isolated schema specialist runtime.

    Args:
        dependencies: Deterministic evidence test double.
        audit: Append-only audit recorder.

    Returns:
        SchemaDriftRuntimeConfig without live infrastructure.
    """
    return SchemaDriftRuntimeConfig(
        schema_context_fetcher=dependencies.fetch_schema,
        metadata_getter=dependencies.get_metadata,
        blast_radius_fetcher=dependencies.get_blast_radius,
        audit_client_factory=lambda **_: object(),
        audit_writer=audit,
    )


# --- Testing Task Contracts
def test_schema_drift_task_uses_exact_run_and_least_privilege_tools() -> None:
    """The supervisor handoff must be deterministic, bounded, and no-LLM."""
    task = build_schema_drift_task(
        parent_run_id=uuid4(),
        source_schema_run_id="manual__schema_agent_source",
        qualified_name="dq.raw_orders",
    )

    assert task.specialist_name == SCHEMA_DRIFT_SPECIALIST_NAME
    assert task.task_type == "assess_schema_drift"
    assert task.allowed_tools == SCHEMA_DRIFT_ALLOWED_TOOLS
    assert task.model_route == AgentModelRoute.NO_LLM_FALLBACK
    assert task.token_budget == 0
    assert task.input_payload["source_schema_run_id"] == "manual__schema_agent_source"
    assert {item.reference_type.value for item in task.context_references} == {
        "audit_run",
        "metadata_asset",
    }


def test_schema_drift_output_rejects_execution_claim() -> None:
    """The structured output contract must make schema mutation unrepresentable."""
    with pytest.raises(ValidationError, match="execution_performed"):
        SchemaDriftAgentOutput(
            source_schema_run_id="manual__schema_agent_source",
            qualified_name="dq.raw_orders",
            contract_name="orders_warehouse_schema",
            contract_version=1,
            contract_sha256="a" * 64,
            schema_sha256="b" * 64,
            snapshot_status="pass",
            highest_severity="info",
            assessment=SchemaChangeAssessment.COMPATIBLE,
            impact_level=SchemaImpactLevel.NONE,
            finding_count=0,
            visible_finding_count=0,
            findings_truncated=0,
            execution_performed=True,
            summary="No schema drift was detected.",
        )


# --- Testing Specialist Assessments
def test_clean_schema_run_returns_compatible_without_approval_or_execution() -> None:
    """A clean detector run must remain a no-cost compatible assessment."""
    audit        = AuditRecorder()
    dependencies = SchemaDependencies(schema_context())
    task         = build_schema_drift_task(
        parent_run_id=uuid4(),
        source_schema_run_id="manual__schema_agent_source",
        qualified_name="dq.raw_orders",
    )
    result = run_schema_drift_agent(
        task=task,
        config=runtime_config(dependencies, audit),
    )

    assert result.status == AgentTaskStatus.SUCCESS
    assert result.model_route == AgentModelRoute.NO_LLM_FALLBACK
    assert result.token_usage == 0
    assert result.estimated_cost_usd == 0.0
    assert result.requires_human_approval is False
    assert result.structured_output["assessment"] == "compatible"
    assert result.structured_output["impact_level"] == "none"
    assert result.structured_output["execution_performed"] is False
    assert result.structured_output["impacted_asset_count"] == 2
    assert dependencies.calls == [
        "schema:manual__schema_agent_source:dq.raw_orders",
        "metadata:dq.raw_orders",
        "blast:dq.raw_orders",
    ]
    assert {event["action"] for event in audit.events} == {
        "specialist_handoff_started",
        "assess_schema_drift_policy",
        "specialist_handoff_completed",
    }


def test_breaking_schema_run_requires_human_review_and_safe_migration_plan() -> None:
    """Critical type drift must produce guidance without executing any DDL."""
    audit = AuditRecorder()
    context = schema_context(
        findings=[
            {
                "qualified_name": "dq.raw_orders",
                "column_name": "order_id",
                "check_type": "column_type",
                "status": "fail",
                "severity": "critical",
                "expected_value": "String",
                "actual_value": "UInt64",
            }
        ],
        highest_severity="critical",
    )
    dependencies = SchemaDependencies(context)
    task = build_schema_drift_task(
        parent_run_id=uuid4(),
        source_schema_run_id="manual__schema_agent_source",
        qualified_name="dq.raw_orders",
    )
    result = run_schema_drift_agent(
        task=task,
        config=runtime_config(dependencies, audit),
    )
    migration_text = " ".join(result.structured_output["migration_plan"]).lower()

    assert result.status == AgentTaskStatus.SUCCESS
    assert result.requires_human_approval is True
    assert result.structured_output["assessment"] == "breaking_change"
    assert result.structured_output["impact_level"] == "high"
    assert result.structured_output["execution_performed"] is False
    assert "human approval" in migration_text
    assert "versioned column or table" in migration_text
    assert "execute alter" not in migration_text


def test_unauthorized_schema_tool_is_blocked_before_evidence_collection() -> None:
    """Capability drift must fail closed before deterministic tools run."""
    audit        = AuditRecorder()
    dependencies = SchemaDependencies(schema_context())
    task         = build_schema_drift_task(
        parent_run_id=uuid4(),
        source_schema_run_id="manual__schema_agent_source",
        qualified_name="dq.raw_orders",
    ).model_copy(
        update={"allowed_tools": (*SCHEMA_DRIFT_ALLOWED_TOOLS, "schema_mutation")}
    )
    result = run_schema_drift_agent(
        task=task,
        config=runtime_config(dependencies, audit),
    )

    assert result.status == AgentTaskStatus.BLOCKED
    assert dependencies.calls == []
    assert audit.events[-1]["action"] == "specialist_handoff_rejected"
    assert "unauthorized" in result.errors[0].lower()


def test_schema_tool_failure_is_isolated_without_impact_handoff() -> None:
    """Missing persisted evidence must fail one specialist without cascading."""
    audit = AuditRecorder()
    dependencies = SchemaDependencies(
        schema_context(),
        schema_error=RuntimeError("persisted schema snapshot unavailable"),
    )
    task = build_schema_drift_task(
        parent_run_id=uuid4(),
        source_schema_run_id="manual__schema_agent_source",
        qualified_name="dq.raw_orders",
    )
    result = run_schema_drift_agent(
        task=task,
        config=runtime_config(dependencies, audit),
    )

    assert result.status == AgentTaskStatus.FAILED
    assert dependencies.calls == [
        "schema:manual__schema_agent_source:dq.raw_orders",
    ]
    assert audit.events[-1]["action"] == "specialist_handoff_failed"
    assert result.structured_output == {}
    assert result.requires_human_approval is False
