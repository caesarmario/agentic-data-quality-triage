####
## Control Plane Supervisor Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Validate deterministic routing, budgets, specialist reuse, isolation, and trigger safety."""

# --- Importing Libraries
from __future__ import annotations

import json
from datetime import date
from typing import Any
from uuid import UUID, uuid4

import pytest

from agent.context.models import IncidentMemoryRecord, RunContextEvent, RunContextPhase
from agent.specialists.contracts import (
    AgentApprovalState,
    AgentModelRoute,
    AgentResultEnvelope,
    AgentTaskStatus,
    ContextReferenceType,
    EvidenceReference,
    HandoffRecord,
    SupervisorState,
)
from agent.specialists.incident_triage import (
    IncidentTriageRuntimeConfig,
    build_incident_triage_task,
    run_incident_triage_agent,
)
from agent.specialists.registry import (
    AGENT_CAPABILITY_REGISTRY,
    INCIDENT_TRIAGE_ALLOWED_TOOLS,
    INCIDENT_TRIAGE_SPECIALIST_NAME,
    METADATA_LINEAGE_SPECIALIST_NAME,
    SCHEMA_DRIFT_SPECIALIST_NAME,
    SQL_REVIEW_SPECIALIST_NAME,
    get_agent_capability,
)
from agent.state import (
    Alert,
    ApprovalGatedAction,
    EvidenceItem,
    Hypothesis,
    TriageReport,
)
from agent.supervisor.models import SupervisorIntent, SupervisorRequest
from agent.supervisor.budgets import (
    evaluate_post_handoff_budgets,
    evaluate_pre_handoff_budgets,
)
from agent.supervisor.routing import (
    SupervisorRoutingError,
    classify_supervisor_intent,
    resolve_supervisor_route,
)
from agent.supervisor.runtime import (
    SupervisorRuntimeConfig,
    derive_supervisor_parent_run_id,
    run_control_plane_supervisor,
)
from scripts.run_control_plane_supervisor import (
    build_operator_summary,
    require_verifiable_terminal_result,
)
from scripts.trigger_airflow_control_plane_supervisor import (
    CONTROL_PLANE_SUPERVISOR_DAG_ID,
    build_trigger_command,
    resolve_trigger_context_defaults,
    validate_trigger_inputs,
)
from scripts.verify_control_plane_supervisor import verify_triage_terminal_evidence


# --- Defining Test Doubles
class FakeQueryResult:
    """Provide clickhouse-connect-compatible query columns and rows."""

    def __init__(self, columns: list[str], rows: list[tuple[Any, ...]]) -> None:
        """
        Initialize a fixed query result.

        Args:
            columns: Result column names.
            rows: Result row tuples.

        Returns:
            None.
        """
        self.column_names = columns
        self.result_rows  = rows


class FakeAuditClient:
    """Return one heuristic route audit for child usage aggregation."""

    def query(self, sql: str) -> FakeQueryResult:
        """
        Return fixed no-provider model usage.

        Args:
            sql: Fixed child usage SQL.

        Returns:
            FakeQueryResult containing one heuristic route event.
        """
        assert "llm_route_completed" in sql
        assert "LIMIT 100" in sql

        return FakeQueryResult(
            columns=["output_json"],
            rows=[
                (
                    json.dumps(
                        {
                            "provider": "heuristic",
                            "used_heuristic": True,
                            "input_tokens": 250,
                            "output_tokens": 100,
                            "estimated_cost_usd": 0.0,
                        }
                    ),
                )
            ],
        )


class AuditRecorder:
    """Capture parent and specialist audit writes without live ClickHouse."""

    def __init__(self) -> None:
        """Initialize an empty audit event list."""
        self.events: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> UUID:
        """
        Record one append-only audit event.

        Args:
            kwargs: Audit writer keyword arguments.

        Returns:
            Correlated parent or child UUID.
        """
        self.events.append(kwargs)

        return UUID(str(kwargs["agent_run_id"]))


class FailFirstOutcomeAuditRecorder(AuditRecorder):
    """Fail one terminal-outcome append before accepting the fallback write."""

    def __init__(self) -> None:
        """Initialize an empty recorder and one-shot failure marker."""
        super().__init__()
        self.outcome_failure_injected = False

    def __call__(self, **kwargs: Any) -> UUID:
        """
        Reject the first terminal outcome and retain every subsequent audit event.

        Args:
            kwargs: Audit writer keyword arguments.

        Returns:
            Correlated parent UUID after the injected failure is consumed.

        Raises:
            RuntimeError: On the first specialist outcome write only.
        """
        if (
            kwargs.get("action") == "supervisor_specialist_outcome"
            and not self.outcome_failure_injected
        ):
            self.outcome_failure_injected = True
            raise RuntimeError("Controlled first terminal-outcome audit failure.")

        return super().__call__(**kwargs)


class ContextPersistenceRecorder:
    """Capture temporary context and durable memory writes without ClickHouse."""

    def __init__(self) -> None:
        """Initialize empty schema, context-event, and incident-memory histories."""
        self.schema_ensure_count = 0
        self.events: list[RunContextEvent] = []
        self.memories: list[IncidentMemoryRecord] = []

    def ensure(self, client: Any) -> None:
        """
        Record one idempotent schema-ensure boundary.

        Args:
            client: Supervisor-owned ClickHouse client placeholder.

        Returns:
            None.
        """
        assert client is not None
        self.schema_ensure_count += 1

    def write_event(self, client: Any, event: RunContextEvent) -> UUID:
        """
        Capture one validated temporary context event.

        Args:
            client: Supervisor-owned ClickHouse client placeholder.
            event: Validated context event.

        Returns:
            Deterministic context event UUID.
        """
        assert client is not None
        self.events.append(event)

        return event.context_event_id

    def write_memory(self, client: Any, record: IncidentMemoryRecord) -> UUID:
        """
        Capture one validated durable incident-memory record.

        Args:
            client: Supervisor-owned ClickHouse client placeholder.
            record: Validated incident outcome.

        Returns:
            Deterministic incident-memory UUID.
        """
        assert client is not None
        self.memories.append(record)

        return record.memory_id


# --- Defining Fixtures
def sample_triage_report(
    with_approval: bool = True,
    investigation_errors: list[str] | None = None,
) -> TriageReport:
    """
    Build one compact report produced by the existing triage graph contract.

    Args:
        with_approval: Whether to include one approval-gated backfill proposal.
        investigation_errors: Optional non-fatal evidence gaps retained by triage.

    Returns:
        TriageReport with deterministic evidence and S3 artifact URIs.
    """
    alert = Alert(
        alert_key=(
            "orders|dq_failure|2026-08-08|dq.raw_orders|row_count_positive|table"
        ),
        alert_display_id="DQ-20260808-A1B2C3",
        severity="critical",
        table_name="dq.raw_orders",
        metric="row_count_positive",
        dt=date(2026, 8, 8),
    )
    evidence = EvidenceItem(
        evidence_type="sql_result",
        tool_name="clickhouse_sql",
        description="Checked the affected partition.",
        summary="The affected partition contains zero rows.",
        row_count=1,
    )
    hypothesis = Hypothesis(
        title="The raw partition was not loaded",
        description="The landing-to-raw load did not produce the expected partition.",
        likelihood=0.90,
        confidence=0.88,
        root_cause_category="missing_partition",
        supporting_evidence_ids=[evidence.evidence_id],
        recommended_action="Review the landing file and prepare an approval-gated backfill.",
    )
    approval_actions = (
        [
            ApprovalGatedAction(
                action_type="backfill",
                reason="Restore the missing partition through Airflow.",
                target_dag_id="10_dag_dq_orders_landing_orchestrator",
                start_date=alert.dt,
                end_date=alert.dt,
            )
        ]
        if with_approval
        else []
    )

    return TriageReport(
        agent_run_id=uuid4(),
        alert=alert,
        summary="Orders data is missing for the selected date.",
        impact="Downstream daily order metrics may be incomplete.",
        hypotheses=[hypothesis],
        top_hypothesis=hypothesis,
        evidence=[evidence],
        investigation_errors=investigation_errors or [],
        confidence=0.88,
        recommended_actions=["Validate the landing object before requesting a backfill."],
        approval_gated_actions=approval_actions,
        report_id="RPT-A1B2C3",
        markdown_report_s3_uri="s3://dq-artifacts/agent-reports/report.md",
        json_report_s3_uri="s3://dq-artifacts/agent-reports/report.json",
    )


def successful_metadata_result(task: Any) -> AgentResultEnvelope:
    """
    Build one successful deterministic metadata specialist result.

    Args:
        task: Supervisor-generated AgentTaskEnvelope.

    Returns:
        Success AgentResultEnvelope matching the handoff identity.
    """
    return AgentResultEnvelope(
        task_id=task.task_id,
        parent_run_id=task.parent_run_id,
        specialist_name=task.specialist_name,
        task_type=task.task_type,
        status=AgentTaskStatus.SUCCESS,
        evidence_references=[
            EvidenceReference(
                evidence_type="metadata_catalog_query",
                source_tool="metadata_catalog",
                reference=f"task:{task.task_id}",
                summary="Bounded metadata catalog evidence was retained for the handoff.",
            )
        ],
        structured_output={
            "summary": "Raw Orders is active and requires trust review.",
            "trust_status": "review",
        },
        confidence=0.85,
        model_route=AgentModelRoute.NO_LLM_FALLBACK,
        token_usage=0,
        estimated_cost_usd=0.0,
        duration_ms=25,
        recommended_next_step="Review candidate certification before production use.",
    )


def successful_schema_result(task: Any) -> AgentResultEnvelope:
    """
    Build one successful deterministic schema specialist result.

    Args:
        task: Supervisor-generated AgentTaskEnvelope.

    Returns:
        Compatible schema assessment matching the handoff identity.
    """
    return AgentResultEnvelope(
        task_id=task.task_id,
        parent_run_id=task.parent_run_id,
        specialist_name=task.specialist_name,
        task_type=task.task_type,
        status=AgentTaskStatus.SUCCESS,
        evidence_references=[
            EvidenceReference(
                evidence_type="schema_assessment",
                source_tool="schema_drift",
                reference="schema-run:manual__schema_agent_source",
                summary="Persisted schema drift evidence was retained for the assessment.",
            )
        ],
        structured_output={
            "summary": "The persisted schema matches the configured contract.",
            "assessment": "compatible",
            "impact_level": "none",
            "source_schema_run_id": "manual__schema_agent_source",
            "finding_count": 0,
            "impacted_asset_count": 2,
            "impacted_test_count": 1,
            "execution_performed": False,
        },
        confidence=0.95,
        model_route=AgentModelRoute.NO_LLM_FALLBACK,
        token_usage=0,
        estimated_cost_usd=0.0,
        duration_ms=25,
        recommended_next_step="Continue monitoring the next detector run.",
    )


def successful_incident_result(task: Any) -> AgentResultEnvelope:
    """
    Build one successful incident result with canonical identity and durable evidence.

    Args:
        task: Supervisor-generated AgentTaskEnvelope.

    Returns:
        Success AgentResultEnvelope suitable for context and memory persistence.
    """
    report   = sample_triage_report(with_approval=True)
    evidence = EvidenceReference(
        evidence_type="report_artifact",
        source_tool="s3_artifacts",
        reference=report.json_report_s3_uri,
        summary="Structured incident evidence persisted for operator review.",
    )

    return AgentResultEnvelope(
        task_id=task.task_id,
        parent_run_id=task.parent_run_id,
        specialist_name=task.specialist_name,
        task_type=task.task_type,
        status=AgentTaskStatus.SUCCESS,
        evidence_references=[evidence],
        structured_output={
            "summary": report.summary,
            "alert_key": report.alert.alert_key,
            "alert_display_id": report.alert.alert_display_id,
            "report_id": report.report_id,
            "markdown_report_s3_uri": report.markdown_report_s3_uri,
            "json_report_s3_uri": report.json_report_s3_uri,
            "top_hypothesis": {
                "category": report.top_hypothesis.root_cause_category,
            },
        },
        confidence=report.confidence,
        model_route=AgentModelRoute.NO_LLM_FALLBACK,
        token_usage=0,
        estimated_cost_usd=0.0,
        duration_ms=25,
        recommended_next_step="Review and approve the proposed backfill if evidence is sufficient.",
        requires_human_approval=True,
    )


# --- Testing Capability Registry
def test_registry_contains_four_bounded_specialists() -> None:
    """The pilot must register four bounded and non-overlapping specialists."""
    assert set(AGENT_CAPABILITY_REGISTRY) == {
        INCIDENT_TRIAGE_SPECIALIST_NAME,
        METADATA_LINEAGE_SPECIALIST_NAME,
        SCHEMA_DRIFT_SPECIALIST_NAME,
        SQL_REVIEW_SPECIALIST_NAME,
    }


def test_incident_triage_capability_reuses_existing_guarded_tools() -> None:
    """Incident triage must not receive an Airflow or remediation executor tool."""
    capability = get_agent_capability(INCIDENT_TRIAGE_SPECIALIST_NAME)

    assert capability.allowed_tools == INCIDENT_TRIAGE_ALLOWED_TOOLS
    assert capability.default_model_route == AgentModelRoute.DEEPTHINK_LLM
    assert capability.mutation_allowed is False
    assert capability.allowed_side_effects == (
        "append_audit_events",
        "write_report_artifacts",
        "update_alert_lifecycle",
    )
    assert "airflow_backfill_executor" not in capability.allowed_tools
    assert "remediation_executor" not in capability.allowed_tools


# --- Testing Deterministic Routing
@pytest.mark.parametrize(
    ("supervisor_request", "expected_intent", "expected_specialist"),
    [
        (
            SupervisorRequest(intent="triage_alert", alert_key="DQ-20260808-A1B2C3"),
            SupervisorIntent.TRIAGE_ALERT,
            INCIDENT_TRIAGE_SPECIALIST_NAME,
        ),
        (
            SupervisorRequest(intent="asset_context", qualified_name="dq.raw_orders"),
            SupervisorIntent.ASSET_CONTEXT,
            METADATA_LINEAGE_SPECIALIST_NAME,
        ),
        (
            SupervisorRequest(intent="blast_radius", qualified_name="dq.raw_orders"),
            SupervisorIntent.BLAST_RADIUS,
            METADATA_LINEAGE_SPECIALIST_NAME,
        ),
        (
            SupervisorRequest(intent="trusted_asset_search", query="orders"),
            SupervisorIntent.TRUSTED_ASSET_SEARCH,
            METADATA_LINEAGE_SPECIALIST_NAME,
        ),
        (
            SupervisorRequest(
                intent="review_sql",
                sql_proposal=(
                    "SELECT country FROM dq.raw_orders "
                    "WHERE dt = toDate('2026-08-08') LIMIT 10"
                ),
            ),
            SupervisorIntent.REVIEW_SQL,
            SQL_REVIEW_SPECIALIST_NAME,
        ),
        (
            SupervisorRequest(
                intent="schema_drift_assessment",
                schema_run_id="manual__schema_agent_source",
                qualified_name="dq.raw_orders",
            ),
            SupervisorIntent.SCHEMA_DRIFT_ASSESSMENT,
            SCHEMA_DRIFT_SPECIALIST_NAME,
        ),
    ],
)
def test_explicit_intents_route_to_one_registered_specialist(
    supervisor_request: SupervisorRequest,
    expected_intent: SupervisorIntent,
    expected_specialist: str,
) -> None:
    """
    Explicit intent must map reproducibly to one registered specialist.

    Args:
        supervisor_request: Typed supervisor request.
        expected_intent: Deterministic expected intent.
        expected_specialist: Deterministic expected specialist.

    Returns:
        None.
    """
    route = resolve_supervisor_route(supervisor_request)

    assert route.intent == expected_intent
    assert route.specialist_name == expected_specialist


def test_auto_intent_rejects_ambiguous_alert_and_search_context() -> None:
    """Auto classification must block instead of guessing between two intents."""
    request = SupervisorRequest(
        intent="auto",
        question="Triage this alert and find trusted table",
        alert_key="DQ-20260808-A1B2C3",
        query="orders",
    )

    with pytest.raises(SupervisorRoutingError, match="exactly one supported intent"):
        classify_supervisor_intent(request)


def test_auto_intent_prioritizes_exact_schema_run_over_free_text() -> None:
    """An exact persisted run must route reproducibly without keyword guessing."""
    request = SupervisorRequest(
        intent="auto",
        question="Please explain the schema impact",
        schema_run_id="manual__schema_agent_source",
        qualified_name="dq.raw_orders",
    )

    assert classify_supervisor_intent(request) == SupervisorIntent.SCHEMA_DRIFT_ASSESSMENT


# --- Testing Incident Specialist Wrapper
def test_incident_specialist_reuses_triage_report_and_zeroes_heuristic_tokens() -> None:
    """Heuristic fallback must remain no-cost while preserving report and approval context."""
    recorder = AuditRecorder()
    report   = sample_triage_report(with_approval=True)
    task     = build_incident_triage_task(
        parent_run_id=uuid4(),
        alert_key=report.alert.alert_key,
        requester="airflow",
    )
    runtime = IncidentTriageRuntimeConfig(
        triage_runner=lambda **_: report,
        audit_client_factory=lambda **_: FakeAuditClient(),
        audit_writer=recorder,
    )

    result = run_incident_triage_agent(task=task, config=runtime)

    assert result.status == AgentTaskStatus.SUCCESS
    assert result.model_route == AgentModelRoute.NO_LLM_FALLBACK
    assert result.token_usage == 0
    assert result.estimated_cost_usd == 0.0
    assert result.requires_human_approval is True
    assert result.structured_output["child_agent_run_id"] == str(report.agent_run_id)
    assert result.structured_output["report_id"] == report.report_id
    assert result.structured_output["top_hypothesis"]["category"] == "missing_partition"
    assert [event["action"] for event in recorder.events] == [
        "specialist_handoff_started",
        "specialist_handoff_completed",
    ]
    assert recorder.events[-1]["report_s3_uri"] == report.markdown_report_s3_uri


def test_incident_specialist_marks_retained_evidence_gap_partial() -> None:
    """A non-fatal evidence gap must remain visible, partial, and approval-gated."""
    recorder = AuditRecorder()
    report   = sample_triage_report(
        with_approval=False,
        investigation_errors=[
            "dbt_lineage failed: NoSuchKey for the requested manifest artifact."
        ],
    )
    task = build_incident_triage_task(
        parent_run_id=uuid4(),
        alert_key=report.alert.alert_key,
        requester="airflow",
    )
    runtime = IncidentTriageRuntimeConfig(
        triage_runner=lambda **_: report,
        audit_client_factory=lambda **_: FakeAuditClient(),
        audit_writer=recorder,
    )

    result = run_incident_triage_agent(task=task, config=runtime)

    assert result.status == AgentTaskStatus.PARTIAL
    assert result.errors == report.investigation_errors
    assert result.requires_human_approval is True
    assert result.structured_output["investigation_errors"] == report.investigation_errors
    assert "Resolve the retained evidence gaps" in result.recommended_next_step
    assert recorder.events[-1]["status"] == "partial"
    assert recorder.events[-1]["output_payload"]["investigation_error_count"] == 1
    assert recorder.events[-1]["error_message"] == report.investigation_errors[0]


def test_supervisor_verifier_accepts_bounded_partial_lineage_gap() -> None:
    """A failed optional lineage read may pass only as an explicit partial result."""
    error = "dbt_lineage failed: NoSuchKey for the requested manifest artifact."
    child_rows = [
        {"action": action, "status": "success"}
        for action in sorted(
            {
                "fetch_incident_history",
                "store_triage_report",
                "mark_alert_triaged",
                "triage_completed",
            }
        )
    ]
    child_rows.append(
        {
            "action": "fetch_dbt_lineage",
            "status": "failed",
            "error_message": "NoSuchKey",
        }
    )
    completion_row = {
        "status": "partial",
        "error_message": error,
    }
    completion_payload = {
        "result_status": "partial",
        "investigation_errors": [error],
        "investigation_error_count": 1,
    }

    retained_errors = verify_triage_terminal_evidence(
        completion_row=completion_row,
        completion_payload=completion_payload,
        child_rows=child_rows,
        terminal_status="partial",
        complexity_reason_codes=["critical_severity", "unresolved_tool_errors"],
        routing_policy_evidence={
            "post_evidence": {"human_approval_required": True}
        },
    )

    assert retained_errors == [error]


def test_supervisor_verifier_rejects_partial_core_action_failure() -> None:
    """A lifecycle or report failure must never be downgraded to an accepted evidence gap."""
    error = "alert_lifecycle failed: ClickHouse mutation did not complete."
    child_rows = [
        {"action": "fetch_incident_history", "status": "success"},
        {"action": "store_triage_report", "status": "success"},
        {"action": "mark_alert_triaged", "status": "failed"},
        {"action": "triage_completed", "status": "success"},
    ]

    with pytest.raises(RuntimeError, match="core action did not succeed"):
        verify_triage_terminal_evidence(
            completion_row={"status": "partial", "error_message": error},
            completion_payload={
                "result_status": "partial",
                "investigation_errors": [error],
                "investigation_error_count": 1,
            },
            child_rows=child_rows,
            terminal_status="partial",
            complexity_reason_codes=["unresolved_tool_errors"],
            routing_policy_evidence={
                "post_evidence": {"human_approval_required": True}
            },
        )


# --- Testing Supervisor Runtime
def test_supervisor_executes_exactly_one_metadata_handoff_and_audits_decisions() -> None:
    """One metadata request must produce one handoff and no autonomous follow-on task."""
    recorder         = AuditRecorder()
    context_recorder = ContextPersistenceRecorder()
    calls: list[str] = []

    def metadata_runner(task: Any, **_: Any) -> AgentResultEnvelope:
        """Capture one selected handoff and return deterministic success."""
        calls.append(task.task_type)

        return successful_metadata_result(task)

    runtime = SupervisorRuntimeConfig(
        metadata_lineage_runner=metadata_runner,
        audit_client_factory=lambda **_: object(),
        audit_writer=recorder,
        context_schema_ensurer=context_recorder.ensure,
        context_event_writer=context_recorder.write_event,
        incident_memory_writer=context_recorder.write_memory,
    )
    request = SupervisorRequest(
        intent="asset_context",
        qualified_name="dq.raw_orders",
    )

    result = run_control_plane_supervisor(
        request=request,
        external_run_id="manual__supervisor_metadata_test",
        config=runtime,
    )

    assert result.status == AgentTaskStatus.SUCCESS
    assert result.selected_specialist == METADATA_LINEAGE_SPECIALIST_NAME
    assert result.resolved_intent == SupervisorIntent.ASSET_CONTEXT
    assert calls == ["asset_context"]
    assert len(result.supervisor_state.handoff_history) == 1
    assert len(result.supervisor_state.specialist_results) == 1
    assert result.supervisor_state.approval_state == AgentApprovalState.NOT_REQUIRED
    assert [event.phase for event in context_recorder.events] == [
        RunContextPhase.STARTED,
        RunContextPhase.ROUTED,
        RunContextPhase.COMPLETED,
    ]
    assert context_recorder.schema_ensure_count == 1
    assert context_recorder.memories == []
    assert len(result.supervisor_state.run_context_event_ids) == 3
    assert result.supervisor_state.incident_memory_ids == []
    assert [event["action"] for event in recorder.events] == [
        "supervisor_run_started",
        "supervisor_intent_classified",
        "supervisor_budget_prechecked",
        "supervisor_circuit_checked",
        "supervisor_route_selected",
        "supervisor_handoff_started",
        "supervisor_specialist_attempt_started",
        "supervisor_specialist_attempt_completed",
        "supervisor_specialist_outcome",
        "supervisor_handoff_completed",
        "supervisor_budget_reconciled",
        "supervisor_final_decision",
    ]
    final_event = recorder.events[-1]

    assert final_event["output_payload"]["approval_state"] == "not_required"
    assert final_event["output_payload"]["resilience"]["attempt_count"] == 1
    assert final_event["output_payload"]["resilience"]["retry_count"] == 0
    assert final_event["sql"] == ""

    summary = build_operator_summary(result)

    assert "supervisor_state" not in summary
    assert summary["handoff_count"] == 1
    assert summary["trust_status"] == "review"
    assert summary["run_context_event_count"] == 3
    assert summary["incident_memory_count"] == 0
    assert summary["model_call_count"] == 0
    assert summary["budget"]["allowed"] is True
    assert summary["resilience"]["attempt_count"] == 1
    assert summary["resilience"]["retry_count"] == 0
    assert summary["resilience"]["circuit"]["request_allowed"] is True


def test_supervisor_retries_terminal_outcome_audit_after_first_append_failure() -> None:
    """A failed first audit append must not suppress the fallback terminal outcome."""
    recorder         = FailFirstOutcomeAuditRecorder()
    context_recorder = ContextPersistenceRecorder()

    runtime = SupervisorRuntimeConfig(
        metadata_lineage_runner=lambda task, **_: successful_metadata_result(task),
        audit_client_factory=lambda **_: object(),
        audit_writer=recorder,
        context_schema_ensurer=context_recorder.ensure,
        context_event_writer=context_recorder.write_event,
        incident_memory_writer=context_recorder.write_memory,
    )
    result = run_control_plane_supervisor(
        request=SupervisorRequest(
            intent="asset_context",
            qualified_name="dq.raw_orders",
        ),
        external_run_id="manual__supervisor_outcome_audit_recovery",
        config=runtime,
    )
    outcome_events = [
        event
        for event in recorder.events
        if event["action"] == "supervisor_specialist_outcome"
    ]

    assert recorder.outcome_failure_injected is True
    assert result.status == AgentTaskStatus.PARTIAL
    assert result.failure_isolated is True
    require_verifiable_terminal_result(result)
    assert len(outcome_events) == 1
    assert outcome_events[0]["status"] == "failed"
    assert recorder.events[-1]["action"] == "supervisor_final_decision"
    assert recorder.events[-1]["status"] == "partial"


def test_supervisor_executes_exactly_one_schema_handoff_without_mutation() -> None:
    """One exact detector run must dispatch only the bounded Schema Drift Agent."""
    recorder         = AuditRecorder()
    context_recorder = ContextPersistenceRecorder()
    calls: list[str] = []

    def schema_runner(task: Any, **_: Any) -> AgentResultEnvelope:
        """Capture one selected schema handoff and return compatible evidence."""
        calls.append(task.task_type)

        return successful_schema_result(task)

    runtime = SupervisorRuntimeConfig(
        schema_drift_runner=schema_runner,
        audit_client_factory=lambda **_: object(),
        audit_writer=recorder,
        context_schema_ensurer=context_recorder.ensure,
        context_event_writer=context_recorder.write_event,
        incident_memory_writer=context_recorder.write_memory,
    )
    request = SupervisorRequest(
        intent="schema_drift_assessment",
        schema_run_id="manual__schema_agent_source",
        qualified_name="dq.raw_orders",
    )
    result = run_control_plane_supervisor(
        request=request,
        external_run_id="manual__supervisor_schema_test",
        config=runtime,
    )
    summary = build_operator_summary(result)

    assert result.status == AgentTaskStatus.SUCCESS
    assert result.selected_specialist == SCHEMA_DRIFT_SPECIALIST_NAME
    assert result.resolved_intent == SupervisorIntent.SCHEMA_DRIFT_ASSESSMENT
    assert calls == ["assess_schema_drift"]
    assert len(result.supervisor_state.handoff_history) == 1
    assert result.supervisor_state.approval_state == AgentApprovalState.NOT_REQUIRED
    assert summary["schema_assessment"] == "compatible"
    assert summary["schema_impact_level"] == "none"
    assert summary["source_schema_run_id"] == "manual__schema_agent_source"
    assert summary["execution_performed"] is False
    assert [event.phase for event in context_recorder.events] == [
        RunContextPhase.STARTED,
        RunContextPhase.ROUTED,
        RunContextPhase.COMPLETED,
    ]
    assert context_recorder.memories == []


def test_supervisor_blocks_declared_triage_budget_before_specialist_execution() -> None:
    """Parent token policy must reject triage before any child runner is invoked."""
    recorder         = AuditRecorder()
    context_recorder = ContextPersistenceRecorder()
    calls: list[str] = []

    def incident_runner(**_: Any) -> AgentResultEnvelope:
        """Fail the test if pre-handoff policy does not block execution."""
        calls.append("incident")
        raise AssertionError("Incident runner should not execute above parent token budget.")

    runtime = SupervisorRuntimeConfig(
        incident_runner=incident_runner,
        audit_client_factory=lambda **_: object(),
        audit_writer=recorder,
        context_schema_ensurer=context_recorder.ensure,
        context_event_writer=context_recorder.write_event,
        incident_memory_writer=context_recorder.write_memory,
    )
    request = SupervisorRequest(
        intent="triage_alert",
        alert_key="DQ-20260808-A1B2C3",
        token_budget=1_000,
    )

    result = run_control_plane_supervisor(
        request=request,
        external_run_id="manual__supervisor_budget_blocked",
        config=runtime,
    )

    assert result.status == AgentTaskStatus.BLOCKED
    assert result.failure_isolated is True

    with pytest.raises(RuntimeError, match="status=blocked"):
        require_verifiable_terminal_result(result)
    assert calls == []
    assert result.supervisor_state.handoff_history == []
    assert "tokens_budget_exceeded" in result.supervisor_state.errors[0]
    assert result.audit_summary["budget"]["stage"] == "pre_handoff"
    assert result.audit_summary["budget"]["allowed"] is False
    assert result.audit_summary["budget"]["violations"] == [
        "tokens_budget_exceeded"
    ]
    assert [event["action"] for event in recorder.events] == [
        "supervisor_run_started",
        "supervisor_intent_classified",
        "supervisor_budget_prechecked",
        "supervisor_handoff_rejected",
        "supervisor_budget_exceeded",
        "supervisor_final_decision",
    ]
    assert recorder.events[-1]["action"] == "supervisor_final_decision"
    assert recorder.events[-1]["status"] == "blocked"
    assert [event.phase for event in context_recorder.events] == [
        RunContextPhase.STARTED,
        RunContextPhase.BLOCKED,
    ]
    assert context_recorder.memories == []


def test_supervisor_rejects_model_call_overrun_without_accepting_child_result() -> None:
    """Actual provider attempts above policy must remain audit-only parent evidence."""
    recorder         = AuditRecorder()
    context_recorder = ContextPersistenceRecorder()

    def over_budget_incident_runner(task: Any, **_: Any) -> AgentResultEnvelope:
        """Return a structurally valid result whose actual provider calls exceed policy."""
        return AgentResultEnvelope(
            task_id=task.task_id,
            parent_run_id=task.parent_run_id,
            specialist_name=task.specialist_name,
            task_type=task.task_type,
            status=AgentTaskStatus.SUCCESS,
            evidence_references=[
                EvidenceReference(
                    evidence_type="report_artifact",
                    source_tool="s3_artifacts",
                    reference="s3://dq-artifacts/agent-reports/over-budget.json",
                    summary="The over-budget result retained its report evidence for audit.",
                )
            ],
            structured_output={"summary": "Triage completed above its model-call budget."},
            confidence=0.80,
            model_route=AgentModelRoute.DEEPTHINK_LLM,
            model_call_count=4,
            token_usage=1_000,
            estimated_cost_usd=0.01,
            duration_ms=25,
            recommended_next_step="Review the retained usage audit before retrying.",
        )

    runtime = SupervisorRuntimeConfig(
        incident_runner=over_budget_incident_runner,
        audit_client_factory=lambda **_: object(),
        audit_writer=recorder,
        context_schema_ensurer=context_recorder.ensure,
        context_event_writer=context_recorder.write_event,
        incident_memory_writer=context_recorder.write_memory,
    )
    result = run_control_plane_supervisor(
        request=SupervisorRequest(
            intent="triage_alert",
            alert_key="DQ-20260808-A1B2C3",
            max_model_calls=3,
        ),
        external_run_id="manual__supervisor_model_call_overrun",
        config=runtime,
    )

    assert result.status == AgentTaskStatus.PARTIAL
    assert result.failure_isolated is True
    assert len(result.supervisor_state.handoff_history) == 1
    assert result.supervisor_state.specialist_results == []
    assert result.supervisor_state.incident_memory_ids == []
    assert context_recorder.memories == []
    assert [event.phase for event in context_recorder.events] == [
        RunContextPhase.STARTED,
        RunContextPhase.ROUTED,
        RunContextPhase.BLOCKED,
    ]
    assert result.audit_summary["budget"]["stage"] == "post_handoff"
    assert result.audit_summary["budget"]["violations"] == [
        "model_calls_budget_exceeded"
    ]
    assert any(
        event["action"] == "supervisor_budget_exceeded"
        for event in recorder.events
    )


def test_budget_policy_rejects_model_call_admission_retry_and_latency_overrun() -> None:
    """Every configured budget dimension must have deterministic rejection evidence."""
    parent_run_id = uuid4()
    task = build_incident_triage_task(
        parent_run_id=parent_run_id,
        alert_key="DQ-20260808-A1B2C3",
    )
    state = SupervisorState(
        parent_run_id=parent_run_id,
        max_handoffs=1,
        max_retries=0,
        max_model_calls=2,
        token_budget=16_384,
        estimated_cost_budget_usd=0.05,
        latency_budget_ms=300_000,
    )
    pre_decision = evaluate_pre_handoff_budgets(task=task, state=state)

    assert pre_decision.allowed is False
    assert pre_decision.violations == ("model_calls_budget_exceeded",)

    latency_capped_state = state.model_copy(update={"max_model_calls": 3})
    latency_capped       = evaluate_pre_handoff_budgets(
        task=task,
        state=latency_capped_state,
        elapsed_ms=1,
    )
    exhausted_latency = evaluate_pre_handoff_budgets(
        task=task,
        state=latency_capped_state,
        elapsed_ms=300_000,
    )

    assert latency_capped.allowed is True
    assert latency_capped.usage.latency_ms == 300_000
    assert exhausted_latency.allowed is False
    assert exhausted_latency.violations == ("latency_ms_budget_exceeded",)

    deterministic_result = AgentResultEnvelope(
        task_id=task.task_id,
        parent_run_id=task.parent_run_id,
        specialist_name=task.specialist_name,
        task_type=task.task_type,
        status=AgentTaskStatus.SUCCESS,
        model_route=AgentModelRoute.NO_LLM_FALLBACK,
        duration_ms=10,
        recommended_next_step="No action required.",
    )
    retry_record = HandoffRecord(
        task_id=task.task_id,
        specialist_name=task.specialist_name,
        task_type=task.task_type,
        status=AgentTaskStatus.SUCCESS,
        duration_ms=10,
        retry_count=1,
    )
    retry_state = state.model_copy(update={"max_model_calls": 3})
    post_decision = evaluate_post_handoff_budgets(
        state=retry_state,
        record=retry_record,
        result=deterministic_result,
        elapsed_ms=300_001,
    )

    assert post_decision.allowed is False
    assert post_decision.violations == (
        "retries_budget_exceeded",
        "latency_ms_budget_exceeded",
    )


def test_failed_specialist_is_isolated_without_second_handoff() -> None:
    """A terminal specialist failure must remain explicit and must not cascade."""
    recorder         = AuditRecorder()
    context_recorder = ContextPersistenceRecorder()
    calls: list[str] = []

    def failed_metadata_runner(task: Any, **_: Any) -> AgentResultEnvelope:
        """Return a failure-isolated specialist envelope."""
        calls.append(task.task_type)

        return AgentResultEnvelope(
            task_id=task.task_id,
            parent_run_id=task.parent_run_id,
            specialist_name=task.specialist_name,
            task_type=task.task_type,
            status=AgentTaskStatus.FAILED,
            model_route=AgentModelRoute.NO_LLM_FALLBACK,
            errors=["RuntimeError: metadata registry unavailable"],
            duration_ms=10,
            recommended_next_step="Inspect metadata registry readiness.",
        )

    runtime = SupervisorRuntimeConfig(
        metadata_lineage_runner=failed_metadata_runner,
        audit_client_factory=lambda **_: object(),
        audit_writer=recorder,
        context_schema_ensurer=context_recorder.ensure,
        context_event_writer=context_recorder.write_event,
        incident_memory_writer=context_recorder.write_memory,
    )
    result = run_control_plane_supervisor(
        request=SupervisorRequest(
            intent="asset_context",
            qualified_name="dq.raw_orders",
        ),
        external_run_id="manual__supervisor_failure_isolation",
        config=runtime,
    )

    assert result.status == AgentTaskStatus.FAILED
    assert result.failure_isolated is True
    assert calls == ["asset_context"]
    assert len(result.supervisor_state.handoff_history) == 1
    assert len(result.supervisor_state.specialist_results) == 1
    assert recorder.events[-1]["status"] == "failed"
    assert [event.phase for event in context_recorder.events] == [
        RunContextPhase.STARTED,
        RunContextPhase.ROUTED,
        RunContextPhase.COMPLETED,
    ]
    assert context_recorder.events[-1].status == AgentTaskStatus.FAILED
    assert context_recorder.memories == []


def test_supervisor_persists_one_evidence_driven_incident_memory() -> None:
    """Alert triage must retain bounded run context and one durable outcome record."""
    recorder         = AuditRecorder()
    context_recorder = ContextPersistenceRecorder()
    received_tasks: list[Any] = []

    def incident_runner(task: Any, **_: Any) -> AgentResultEnvelope:
        """Capture the policy-owned handoff and return evidence-driven success."""
        received_tasks.append(task)

        return successful_incident_result(task)

    runtime = SupervisorRuntimeConfig(
        incident_runner=incident_runner,
        audit_client_factory=lambda **_: object(),
        audit_writer=recorder,
        context_schema_ensurer=context_recorder.ensure,
        context_event_writer=context_recorder.write_event,
        incident_memory_writer=context_recorder.write_memory,
    )
    result = run_control_plane_supervisor(
        request=SupervisorRequest(
            intent="triage_alert",
            alert_key="DQ-20260808-A1B2C3",
        ),
        external_run_id="manual__supervisor_incident_memory",
        config=runtime,
    )
    memory = context_recorder.memories[0]
    summary = build_operator_summary(result)

    assert result.status == AgentTaskStatus.SUCCESS
    assert len(received_tasks) == 1
    assert any(
        reference.reference_type == ContextReferenceType.RUN_CONTEXT
        for reference in received_tasks[0].context_references
    )
    assert [event.phase for event in context_recorder.events] == [
        RunContextPhase.STARTED,
        RunContextPhase.ROUTED,
        RunContextPhase.COMPLETED,
    ]
    assert len(context_recorder.memories) == 1
    assert memory.alert_key.startswith("orders|dq_failure|")
    assert memory.alert_display_id == "DQ-20260808-A1B2C3"
    assert memory.report_s3_uri.endswith("report.md")
    assert memory.evidence_references
    assert memory.approval_state == AgentApprovalState.PENDING
    assert result.supervisor_state.incident_memory_ids == [memory.memory_id]
    assert summary["run_context_event_count"] == 3
    assert summary["incident_memory_count"] == 1


def test_supervisor_contains_final_context_persistence_failure() -> None:
    """
    Preserve a completed specialist result when final context persistence fails.

    The parent run must become partial, retain the successful specialist evidence,
    write a blocked lifecycle event, and avoid starting a second specialist handoff.
    """
    recorder         = AuditRecorder()
    context_recorder = ContextPersistenceRecorder()
    calls: list[str] = []

    def incident_runner(task: Any, **_: Any) -> AgentResultEnvelope:
        """Return one successful, evidence-driven incident result."""
        calls.append(task.task_type)

        return successful_incident_result(task)

    def fail_completed_context(client: Any, event: RunContextEvent) -> UUID:
        """
        Simulate a terminal context-write failure while preserving other phases.

        Args:
            client: Supervisor-owned ClickHouse client placeholder.
            event: Validated lifecycle event selected for persistence.

        Returns:
            Deterministic context event UUID for non-terminal phases.

        Raises:
            RuntimeError: When the supervisor attempts the completed phase.
        """
        if event.phase == RunContextPhase.COMPLETED:
            raise RuntimeError("simulated completed-context persistence failure")

        return context_recorder.write_event(client=client, event=event)

    runtime = SupervisorRuntimeConfig(
        incident_runner=incident_runner,
        audit_client_factory=lambda **_: object(),
        audit_writer=recorder,
        context_schema_ensurer=context_recorder.ensure,
        context_event_writer=fail_completed_context,
        incident_memory_writer=context_recorder.write_memory,
    )
    result = run_control_plane_supervisor(
        request=SupervisorRequest(
            intent="triage_alert",
            alert_key="DQ-20260808-A1B2C3",
        ),
        external_run_id="manual__supervisor_context_failure_isolation",
        config=runtime,
    )

    assert result.status == AgentTaskStatus.PARTIAL
    assert result.failure_isolated is True
    assert calls == ["triage_alert"]
    assert len(result.supervisor_state.handoff_history) == 1
    assert len(result.supervisor_state.specialist_results) == 1
    assert result.supervisor_state.specialist_results[0].status == AgentTaskStatus.SUCCESS
    assert [event.phase for event in context_recorder.events] == [
        RunContextPhase.STARTED,
        RunContextPhase.ROUTED,
        RunContextPhase.BLOCKED,
    ]
    assert context_recorder.events[-1].status == AgentTaskStatus.PARTIAL
    assert len(context_recorder.memories) == 1
    assert recorder.events[-1]["action"] == "supervisor_final_decision"
    assert recorder.events[-1]["status"] == "partial"
    assert "persist all required control-plane context" in result.final_response


def test_supervisor_parent_run_id_is_stable_for_airflow_audit_lookup() -> None:
    """The same Airflow run ID must resolve to the same parent audit UUID."""
    run_id = "manual__control_plane_asset_context_20260808T120000000000"

    assert derive_supervisor_parent_run_id(run_id) == derive_supervisor_parent_run_id(run_id)
    assert derive_supervisor_parent_run_id(run_id) != derive_supervisor_parent_run_id(
        run_id + "_other"
    )


# --- Testing Airflow Trigger Safety
@pytest.mark.parametrize(
    ("field", "value"),
    (
        pytest.param(
            "manifest_s3_uri",
            "s3://dq-artifacts/reports/../secret.json",
            id="manifest-traversal",
        ),
        pytest.param("artifacts_bucket", "DQ_ARTIFACTS", id="invalid-bucket"),
        pytest.param(
            "artifacts_prefix",
            "reports/../secret",
            id="artifact-prefix-traversal",
        ),
    ),
)
def test_supervisor_request_rejects_unsafe_storage_context(field: str, value: str) -> None:
    """
    Keep direct API requests behind the same storage boundary as Airflow triggers.

    Args:
        field: SupervisorRequest storage field under test.
        value: Unsafe value that must fail validation.

    Returns:
        None.
    """
    payload = {
        "intent": "asset_context",
        "qualified_name": "dq.raw_orders",
        field: value,
    }

    with pytest.raises(ValueError):
        SupervisorRequest.model_validate(payload)


def test_supervisor_trigger_uses_structured_bounded_configuration() -> None:
    """Trigger helper must pass JSON configuration without arbitrary shell commands."""
    command = build_trigger_command(
        intent="blast_radius",
        question="Show downstream impact",
        alert_key="",
        qualified_name="dq.raw_orders",
        query="orders",
        token_budget=16_384,
        latency_budget_ms=300_000,
        run_id="manual__control_plane_test",
    )
    conf = json.loads(command[command.index("-c") + 1])

    assert command[0:3] == ["airflow", "dags", "trigger"]
    assert command[-1] == CONTROL_PLANE_SUPERVISOR_DAG_ID
    assert conf == {
        "intent": "blast_radius",
        "question": "Show downstream impact",
        "alert_key": "",
        "qualified_name": "dq.raw_orders",
        "query": "orders",
        "domain": "",
        "data_layer": "",
        "certification_status": "",
        "lifecycle_status": "",
        "execution_mode": "single",
        "max_workers": 1,
        "max_concurrency": 1,
        "allow_external_llm": False,
        "expected_worker_count": 0,
        "max_handoffs": 1,
        "max_retries": 0,
        "max_model_calls": 3,
        "token_budget": 16_384,
        "estimated_cost_budget_usd": 0.05,
        "latency_budget_ms": 300_000,
        "sql_proposal_base64": "",
        "sql_purpose": "",
        "sql_hard_limit": 100,
        "sql_require_date_filter": True,
        "sql_max_scan_bytes": 1024 * 1024 * 1024,
        "expected_sql_decision": "",
        "schema_run_id": "",
        "schema_finding_limit": 50,
        "expected_schema_assessment": "",
        "result_limit": 10,
        "max_depth": 5,
        "max_nodes": 100,
        "confidence_threshold": 0.70,
        "max_evidence_iterations": 2,
        "manifest_s3_uri": "",
        "artifacts_bucket": "",
        "artifacts_prefix": "agent-reports",
    }
    assert ";" not in "".join(command)


def test_supervisor_trigger_accepts_only_bounded_opt_in_fanout() -> None:
    """Fan-out trigger configuration must retain safe capacity and no provider selector."""
    command = build_trigger_command(
        intent="asset_context",
        question="",
        alert_key="",
        qualified_name="dq.raw_orders",
        query="orders",
        token_budget=0,
        latency_budget_ms=300_000,
        run_id="manual__control_plane_fanout_test",
        execution_mode="fanout",
        max_workers=2,
        max_concurrency=2,
        allow_external_llm=False,
        max_handoffs=2,
        max_model_calls=0,
        estimated_cost_budget_usd=0.0,
    )
    conf = json.loads(command[command.index("-c") + 1])

    assert conf["execution_mode"] == "fanout"
    assert conf["max_workers"] == 2
    assert conf["max_concurrency"] == 2
    assert conf["max_handoffs"] == 2
    assert conf["allow_external_llm"] is False
    assert "provider" not in conf
    assert "model" not in conf


@pytest.mark.parametrize(
    "overrides",
    (
        {"execution_mode": "single", "max_workers": 2},
        {"execution_mode": "single", "max_concurrency": 2},
        {"execution_mode": "fanout", "max_workers": 11, "max_handoffs": 11},
        {"execution_mode": "fanout", "max_workers": 2, "max_concurrency": 4, "max_handoffs": 2},
        {"execution_mode": "fanout", "max_workers": 3, "max_concurrency": 2, "max_handoffs": 2},
    ),
)
def test_supervisor_trigger_rejects_unbounded_or_hidden_fanout(
    overrides: dict[str, object],
) -> None:
    """Invalid worker, concurrency, and handoff combinations must fail before Airflow."""
    values: dict[str, object] = {
        "intent": "asset_context",
        "question": "",
        "alert_key": "",
        "qualified_name": "dq.raw_orders",
        "query": "orders",
        "token_budget": 0,
        "latency_budget_ms": 300_000,
        "max_model_calls": 0,
        "estimated_cost_budget_usd": 0.0,
    }
    values.update(overrides)

    with pytest.raises(ValueError):
        validate_trigger_inputs(**values)


def test_supervisor_trigger_round_trips_complete_dag_context() -> None:
    """Preserve reproducible metadata, evidence-loop, and artifact controls."""
    command = build_trigger_command(
        intent="asset_context",
        question="",
        alert_key="",
        qualified_name="dq.raw_orders",
        query="orders",
        domain="commerce",
        data_layer="raw",
        certification_status="candidate",
        lifecycle_status="active",
        result_limit=25,
        max_depth=10,
        max_nodes=250,
        confidence_threshold=0.95,
        max_evidence_iterations=0,
        manifest_s3_uri="s3://dq-artifacts/dbt/manifest.json",
        artifacts_bucket="dq-artifacts",
        artifacts_prefix="control-plane/reports",
        token_budget=16_384,
        latency_budget_ms=300_000,
        run_id="manual__control_plane_full_context",
    )
    conf = json.loads(command[command.index("-c") + 1])

    assert conf["domain"] == "commerce"
    assert conf["data_layer"] == "raw"
    assert conf["certification_status"] == "candidate"
    assert conf["lifecycle_status"] == "active"
    assert conf["result_limit"] == 25
    assert conf["max_depth"] == 10
    assert conf["max_nodes"] == 250
    assert conf["confidence_threshold"] == 0.95
    assert conf["max_evidence_iterations"] == 0
    assert conf["manifest_s3_uri"] == "s3://dq-artifacts/dbt/manifest.json"
    assert conf["artifacts_bucket"] == "dq-artifacts"
    assert conf["artifacts_prefix"] == "control-plane/reports"
    assert "model_route" not in conf
    assert "complexity" not in conf
    assert "complexity_tier" not in conf


@pytest.mark.parametrize(
    ("field", "value", "error_match"),
    (
        pytest.param(
            "manifest_s3_uri",
            "s3://dq-artifacts/path'; touch /tmp/unsafe",
            "Manifest S3 URI",
            id="manifest-uri",
        ),
        pytest.param(
            "artifacts_bucket",
            "dq-artifacts;touch",
            "Artifacts bucket",
            id="artifacts-bucket",
        ),
        pytest.param(
            "artifacts_prefix",
            "reports/../secrets",
            "parent-directory traversal",
            id="artifacts-prefix",
        ),
    ),
)
def test_supervisor_trigger_rejects_unsafe_artifact_context(
    field: str,
    value: str,
    error_match: str,
) -> None:
    """
    Reject storage values that could escape the bounded DAG command contract.

    Args:
        field: Trigger field under test.
        value: Unsafe value that must be rejected.
        error_match: Expected validation message fragment.

    Returns:
        None.
    """
    overrides = {field: value}

    with pytest.raises(ValueError, match=error_match):
        build_trigger_command(
            intent="asset_context",
            question="",
            alert_key="",
            qualified_name="dq.raw_orders",
            query="orders",
            token_budget=16_384,
            latency_budget_ms=300_000,
            run_id="manual__control_plane_artifact_safety",
            **overrides,
        )


def test_supervisor_trigger_rejects_shell_sensitive_run_id() -> None:
    """Reject an explicit DagRun identifier before invoking the Airflow CLI."""
    with pytest.raises(ValueError, match="Airflow run ID"):
        build_trigger_command(
            intent="asset_context",
            question="",
            alert_key="",
            qualified_name="dq.raw_orders",
            query="orders",
            token_budget=16_384,
            latency_budget_ms=300_000,
            run_id="manual__safe;touch_tmp",
        )


@pytest.mark.parametrize(
    ("field", "value", "error_match"),
    (
        pytest.param("result_limit", 26, "Result limit", id="result-limit"),
        pytest.param("max_depth", 11, "Lineage max depth", id="max-depth"),
        pytest.param("max_nodes", 251, "Lineage max nodes", id="max-nodes"),
        pytest.param(
            "confidence_threshold",
            0.96,
            "Confidence threshold",
            id="confidence-threshold",
        ),
        pytest.param(
            "max_evidence_iterations",
            6,
            "Maximum evidence iterations",
            id="evidence-iterations",
        ),
    ),
)
def test_supervisor_trigger_bounds_match_dag_parameters(
    field: str,
    value: int | float,
    error_match: str,
) -> None:
    """
    Keep helper validation aligned with DAG 98 numeric parameter bounds.

    Args:
        field: Numeric trigger field under test.
        value: Out-of-range value that must be rejected.
        error_match: Expected validation message fragment.

    Returns:
        None.
    """
    overrides = {field: value}

    with pytest.raises(ValueError, match=error_match):
        build_trigger_command(
            intent="asset_context",
            question="",
            alert_key="",
            qualified_name="dq.raw_orders",
            query="orders",
            token_budget=16_384,
            latency_budget_ms=300_000,
            run_id="manual__control_plane_bounds",
            **overrides,
        )


def test_supervisor_trigger_defaults_only_required_intent_context() -> None:
    """Trigger defaults must not leak metadata context into incident triage requests."""
    triage_context = resolve_trigger_context_defaults(
        intent="triage_alert",
        qualified_name=None,
        query=None,
    )
    asset_context = resolve_trigger_context_defaults(
        intent="asset_context",
        qualified_name=None,
        query=None,
    )
    search_context = resolve_trigger_context_defaults(
        intent="trusted_asset_search",
        qualified_name=None,
        query=None,
    )
    schema_context = resolve_trigger_context_defaults(
        intent="schema_drift_assessment",
        qualified_name=None,
        query=None,
    )

    assert triage_context == ("", "")
    assert asset_context == ("dq.raw_orders", "")
    assert search_context == ("", "orders")
    assert schema_context == ("dq.raw_orders", "")


def test_supervisor_schema_trigger_carries_exact_run_and_expected_assessment() -> None:
    """DAG configuration must correlate one detector run with one table and result."""
    command = build_trigger_command(
        intent="schema_drift_assessment",
        question="",
        alert_key="",
        qualified_name="dq.raw_orders",
        query="",
        token_budget=0,
        latency_budget_ms=300_000,
        run_id="manual__control_plane_schema_test",
        schema_run_id="manual__schema_agent_source",
        schema_finding_limit=25,
        expected_schema_assessment="compatible",
    )
    conf = json.loads(command[command.index("-c") + 1])

    assert conf["intent"] == "schema_drift_assessment"
    assert conf["schema_run_id"] == "manual__schema_agent_source"
    assert conf["qualified_name"] == "dq.raw_orders"
    assert conf["schema_finding_limit"] == 25
    assert conf["expected_schema_assessment"] == "compatible"
    assert conf["sql_proposal_base64"] == ""


def test_supervisor_trigger_rejects_shell_sensitive_question() -> None:
    """Shell-sensitive wording must be rejected before entering dag_run.conf."""
    with pytest.raises(ValueError, match="unsupported characters"):
        validate_trigger_inputs(
            intent="auto",
            question="Show impact'; rm -rf /tmp/example",
            alert_key="",
            qualified_name="dq.raw_orders",
            query="",
            token_budget=16_384,
            latency_budget_ms=300_000,
        )


def test_supervisor_trigger_rejects_unsafe_or_incomplete_schema_context() -> None:
    """Schema assessment must require one allowlisted persisted run and exact table."""
    with pytest.raises(ValueError, match="unsupported characters"):
        validate_trigger_inputs(
            intent="schema_drift_assessment",
            question="",
            alert_key="",
            qualified_name="dq.raw_orders",
            query="",
            token_budget=16_384,
            latency_budget_ms=300_000,
            schema_run_id="manual__safe;drop_table",
        )

    with pytest.raises(ValueError, match="requires schema_run_id"):
        validate_trigger_inputs(
            intent="schema_drift_assessment",
            question="",
            alert_key="",
            qualified_name="dq.raw_orders",
            query="",
            token_budget=16_384,
            latency_budget_ms=300_000,
        )
