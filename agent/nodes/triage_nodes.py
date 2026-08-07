####
## Triage Specialist Nodes for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from agent.state import (
    Alert,
    EvidenceCategory,
    EvidenceItem,
    EvidenceType,
    TriageState,
)
from agent.tools.alert_lifecycle import mark_alert_triaged
from agent.tools.alerts import load_alert
from agent.tools.audit_log import write_agent_audit_event, write_llm_route_audit_event
from agent.tools.clickhouse_sql import run_guarded_sql
from agent.tools.dbt_lineage import collect_dbt_lineage_evidence
from agent.tools.dq_history import collect_dq_history_evidence
from agent.tools.pipeline_runs import collect_pipeline_runs_evidence
from agent.tools.s3 import store_triage_report
from pipelines.common.clickhouse import build_clickhouse_client
from pipelines.common.logging import logger


# --- Defining Type Aliases
GraphState = dict[str, TriageState]


# --- Defining Classes
@dataclass(frozen=True)
class TriageNodeFactory:
    """
    Build specialist node callables for the supervisor-lite triage graph.

    Args:
        config: Runtime configuration object with ClickHouse, S3, and artifact overrides.
        append_error: Callback for recording non-fatal errors on TriageState.
        current_partition_sql: Callback that builds current partition SQL for an alert.
        recent_partition_sql: Callback that builds recent partition SQL for an alert.
        sql_result_to_evidence: Callback that converts guarded SQL results into evidence.
        build_evidence_plan_for_state: Callback that creates a typed allowlisted evidence plan.
        build_hypotheses_for_state: Callback that creates hypotheses from state.
        frame_hypotheses_for_state: Callback that applies bounded model wording to policy candidates.
        build_llm_report_narrative: Callback that builds optional routed LLM narrative.
        llm_response_to_evidence: Callback that converts LLM metadata into evidence.
        build_report_from_state: Callback that finalizes a TriageReport from state.
        evidence_planning_route_name: LLM route used by the bounded evidence planner.
        hypothesis_framing_route_name: LLM route used by bounded hypothesis framing.
        llm_route_name: Configured LLM route used by report narrative generation.
        tool_name: Tool name used by final audit logging.
    """

    config: Any
    append_error: Callable[[TriageState, str], None]
    current_partition_sql: Callable[[Alert], str]
    recent_partition_sql: Callable[[Alert], str]
    sql_result_to_evidence: Callable[[Any, str, str], EvidenceItem]
    build_evidence_plan_for_state: Callable[[TriageState], Any]
    build_hypotheses_for_state: Callable[[TriageState], list[Any]]
    frame_hypotheses_for_state: Callable[[TriageState, list[Any]], Any]
    build_llm_report_narrative: Callable[[TriageState], Any]
    llm_response_to_evidence: Callable[[Any], EvidenceItem]
    build_report_from_state: Callable[[TriageState, Any | None], Any]
    evidence_planning_route_name: str
    hypothesis_framing_route_name: str
    llm_route_name: str
    tool_name: str

    # --- Defining State Helpers
    def get_state(self, graph_state: GraphState) -> TriageState:
        """
        Extract the shared TriageState from LangGraph state.

        Args:
            graph_state: LangGraph state dictionary.

        Returns:
            Shared TriageState object.
        """
        return graph_state["state"]

    # --- Defining Alert Context Node
    def load_alert_node(self, graph_state: GraphState) -> GraphState:
        """
        Load alert context from ClickHouse.

        Args:
            graph_state: Current graph state.

        Returns:
            Updated graph state with alert populated.
        """
        state       = self.get_state(graph_state)
        state.alert = load_alert(
            alert_id=str(state.alert_id) if state.alert_id else None,
            alert_key=state.alert_key or None,
            agent_run_id=state.agent_run_id,
            clickhouse_host=self.config.clickhouse_host,
            clickhouse_port=self.config.clickhouse_port,
        )

        logger.info("Graph loaded alert | agent_run_id=%s alert_key=%s", state.agent_run_id, state.alert.alert_key)

        return {"state": state}

    # --- Defining Evidence Planning Node
    def plan_evidence_node(self, graph_state: GraphState) -> GraphState:
        """
        Build a typed plan before deterministic evidence collectors run.

        Args:
            graph_state: Current graph state containing a loaded alert.

        Returns:
            Updated graph state with an allowlisted EvidencePlan.
        """
        state  = self.get_state(graph_state)
        result = self.build_evidence_plan_for_state(state)

        state.evidence_plan = result.plan

        if result.error_type:
            self.append_error(state, f"evidence_planning failed: {result.error_type}")

            try:
                self.write_llm_failure_audit(
                    state=state,
                    error_type=result.error_type,
                    requested_route=self.evidence_planning_route_name,
                )

            except Exception as exc:
                self.append_error(state, f"evidence_planning_audit failed: {type(exc).__name__}")

        elif result.llm_response:
            try:
                self.write_llm_success_audit(state=state, response=result.llm_response)

            except Exception as exc:
                self.append_error(state, f"evidence_planning_audit failed: {type(exc).__name__}")

        logger.info(
            "Evidence planning node completed | agent_run_id=%s source=%s categories=%s",
            state.agent_run_id,
            state.evidence_plan.planner_source,
            [str(item.category) for item in state.evidence_plan.requests],
        )

        return {"state": state}

    # --- Defining Evidence Collection Node
    def gather_context_node(self, graph_state: GraphState) -> GraphState:
        """
        Gather baseline evidence using deterministic tools.

        Args:
            graph_state: Current graph state.

        Returns:
            Updated graph state with evidence appended.
        """
        state = self.get_state(graph_state)

        if not state.alert:
            raise ValueError("Alert must be loaded before gathering context.")

        evidence_builders = self.resolve_evidence_builders(state=state)

        for name, builder in evidence_builders:
            try:
                state.add_evidence(builder(state))

            except Exception as exc:
                self.append_error(state, f"{name} failed: {exc}")

        return {"state": state}

    def resolve_evidence_builders(
        self,
        state: TriageState,
    ) -> list[tuple[str, Callable[[TriageState], EvidenceItem]]]:
        """
        Map an EvidencePlan to internal collector callables through an allowlist.

        Args:
            state: Triage state containing the optional evidence plan.

        Returns:
            Ordered category and collector pairs. Unknown categories cannot enter the mapping.
        """
        collector_allowlist = {
            EvidenceCategory.CURRENT_PARTITION_ROW_COUNT.value: self.collect_current_partition_evidence,
            EvidenceCategory.DQ_HISTORY.value: self.collect_dq_history_for_state,
            EvidenceCategory.PIPELINE_RUNS.value: self.collect_pipeline_runs_for_state,
            EvidenceCategory.DBT_LINEAGE.value: self.collect_lineage_evidence,
            EvidenceCategory.RECENT_PARTITION_TREND.value: self.collect_recent_partition_evidence,
        }

        if not state.evidence_plan:
            logger.warning(
                "Evidence plan missing; using deterministic baseline collectors | agent_run_id=%s",
                state.agent_run_id,
            )

            return [
                (EvidenceCategory.CURRENT_PARTITION_ROW_COUNT.value, self.collect_current_partition_evidence),
                (EvidenceCategory.DQ_HISTORY.value, self.collect_dq_history_for_state),
                (EvidenceCategory.PIPELINE_RUNS.value, self.collect_pipeline_runs_for_state),
                (EvidenceCategory.DBT_LINEAGE.value, self.collect_lineage_evidence),
            ]

        builders = []

        for request in state.evidence_plan.requests:
            category = request.category.value if isinstance(request.category, EvidenceCategory) else str(request.category)
            builder  = collector_allowlist.get(category)

            if builder is None:
                # EvidenceRequest validation should make this unreachable; retain fail-closed behavior.
                self.append_error(state, f"evidence category blocked by collector allowlist: {category}")
                continue

            builders.append((category, builder))

        logger.info(
            "Resolved evidence collector allowlist | agent_run_id=%s categories=%s",
            state.agent_run_id,
            [name for name, _ in builders],
        )

        return builders

    def collect_current_partition_evidence(self, state: TriageState) -> EvidenceItem:
        """
        Collect current partition row count evidence.

        Args:
            state: Current triage state.

        Returns:
            EvidenceItem with one row_count result.
        """
        if not state.alert:
            raise ValueError("Alert must be loaded before partition evidence.")

        result = run_guarded_sql(
            sql=self.current_partition_sql(state.alert),
            agent_run_id=state.agent_run_id,
            alert_id=state.alert.alert_id,
            alert_key=state.alert.alert_key,
            hard_limit=10,
            require_date_filter=True,
            clickhouse_host=self.config.clickhouse_host,
            clickhouse_port=self.config.clickhouse_port,
        )
        row_count = int(result.rows[0].get("row_count", 0)) if result.rows else 0
        summary   = f"Current partition row count for {state.alert.table_name} on {state.alert.dt} is {row_count}."

        return self.sql_result_to_evidence(
            result,
            "Current partition row count for the alert table/date.",
            summary,
        )

    def collect_dq_history_for_state(self, state: TriageState) -> EvidenceItem:
        """
        Collect DQ history evidence for the loaded alert.

        Args:
            state: Current triage state.

        Returns:
            EvidenceItem with recent DQ check history.
        """
        if not state.alert:
            raise ValueError("Alert must be loaded before DQ history evidence.")

        return collect_dq_history_evidence(
            alert=state.alert,
            agent_run_id=state.agent_run_id,
            clickhouse_host=self.config.clickhouse_host,
            clickhouse_port=self.config.clickhouse_port,
        )

    def collect_pipeline_runs_for_state(self, state: TriageState) -> EvidenceItem:
        """
        Collect pipeline run evidence for the loaded alert.

        Args:
            state: Current triage state.

        Returns:
            EvidenceItem with recent pipeline run history.
        """
        if not state.alert:
            raise ValueError("Alert must be loaded before pipeline run evidence.")

        return collect_pipeline_runs_evidence(
            alert=state.alert,
            agent_run_id=state.agent_run_id,
            clickhouse_host=self.config.clickhouse_host,
            clickhouse_port=self.config.clickhouse_port,
        )

    def collect_lineage_evidence(self, state: TriageState) -> EvidenceItem:
        """
        Collect dbt lineage evidence for the affected table.

        Args:
            state: Current triage state.

        Returns:
            EvidenceItem with lineage rows.
        """
        if not state.alert:
            raise ValueError("Alert must be loaded before lineage evidence.")

        return collect_dbt_lineage_evidence(
            alert=state.alert,
            agent_run_id=state.agent_run_id,
            manifest_path=self.config.manifest_path,
            manifest_s3_uri=self.config.manifest_s3_uri,
            endpoint_url=self.config.s3_endpoint_url,
            clickhouse_host=self.config.clickhouse_host,
            clickhouse_port=self.config.clickhouse_port,
        )

    def collect_recent_partition_evidence(self, state: TriageState) -> EvidenceItem:
        """
        Collect a bounded recent partition row-count trend.

        Args:
            state: Current triage state containing a loaded alert.

        Returns:
            EvidenceItem with recent partition row-count rows.
        """
        if not state.alert:
            raise ValueError("Alert must be loaded before recent partition evidence.")

        result = run_guarded_sql(
            sql=self.recent_partition_sql(state.alert),
            agent_run_id=state.agent_run_id,
            alert_id=state.alert.alert_id,
            alert_key=state.alert.alert_key,
            hard_limit=20,
            require_date_filter=True,
            clickhouse_host=self.config.clickhouse_host,
            clickhouse_port=self.config.clickhouse_port,
        )
        summary = f"Recent partition row counts collected for {state.alert.table_name} around {state.alert.dt}."

        return self.sql_result_to_evidence(
            result,
            "Recent partition row counts for bounded trend comparison.",
            summary,
        )

    # --- Defining Hypothesis Nodes
    def generate_hypotheses_node(self, graph_state: GraphState) -> GraphState:
        """
        Generate hypotheses from the current alert and evidence.

        Args:
            graph_state: Current graph state.

        Returns:
            Updated graph state with ranked hypotheses.
        """
        state                    = self.get_state(graph_state)
        baseline_hypotheses      = self.build_hypotheses_for_state(state)
        result                   = self.frame_hypotheses_for_state(state, baseline_hypotheses)
        state.hypotheses         = result.hypotheses
        state.hypothesis_framing = result.framing

        if result.error_type:
            self.append_error(state, f"hypothesis_framing failed: {result.error_type}")

            try:
                self.write_llm_failure_audit(
                    state=state,
                    error_type=result.error_type,
                    requested_route=self.hypothesis_framing_route_name,
                )

            except Exception as exc:
                self.append_error(state, f"hypothesis_framing_audit failed: {type(exc).__name__}")

        elif result.llm_response:
            try:
                self.write_llm_success_audit(state=state, response=result.llm_response)

            except Exception as exc:
                self.append_error(state, f"hypothesis_framing_audit failed: {type(exc).__name__}")

        logger.info(
            "Generated hypotheses | agent_run_id=%s count=%d framing_source=%s",
            state.agent_run_id,
            len(state.hypotheses),
            state.hypothesis_framing.source,
        )

        return {"state": state}

    def rank_hypotheses_node(self, graph_state: GraphState) -> GraphState:
        """
        Rank hypotheses by confidence.

        Args:
            graph_state: Current graph state.

        Returns:
            Updated graph state with hypotheses sorted descending.
        """
        state            = self.get_state(graph_state)
        state.hypotheses = sorted(state.hypotheses, key=lambda item: item.confidence, reverse=True)
        top              = state.top_hypothesis

        logger.info(
            "Ranked hypotheses | agent_run_id=%s top=%s confidence=%s",
            state.agent_run_id,
            top.title if top else "none",
            top.confidence if top else None,
        )

        return {"state": state}

    # --- Defining Extra Evidence Node
    def collect_extra_evidence_node(self, graph_state: GraphState) -> GraphState:
        """
        Collect additional evidence when confidence is below threshold.

        Args:
            graph_state: Current graph state.

        Returns:
            Updated graph state with extra evidence appended.
        """
        state = self.get_state(graph_state)

        if not state.alert:
            raise ValueError("Alert must be loaded before extra evidence.")

        try:
            state.add_evidence(self.collect_recent_partition_evidence(state))

        except Exception as exc:
            self.append_error(state, f"extra_partition_evidence failed: {exc}")

        state.evidence_iterations += 1
        logger.info(
            "Extra evidence loop completed | agent_run_id=%s iteration=%d",
            state.agent_run_id,
            state.evidence_iterations,
        )

        return {"state": state}

    # --- Defining Report Nodes
    def write_llm_success_audit(self, state: TriageState, response: Any) -> None:
        """
        Persist a successful or fallback LLM route decision for one alert.

        Args:
            state: Current triage state containing alert correlation identifiers.
            response: Normalized LLM route response.

        Returns:
            None.

        Raises:
            ValueError: If alert context is unavailable.
        """
        if not state.alert:
            raise ValueError("Alert context is required for LLM route audit logging.")

        client = build_clickhouse_client(host=self.config.clickhouse_host, port=self.config.clickhouse_port)

        write_llm_route_audit_event(
            client=client,
            response=response,
            agent_run_id=state.agent_run_id,
            alert_id=state.alert.alert_id,
            alert_key=state.alert.alert_key,
        )

    def write_llm_failure_audit(
        self,
        state: TriageState,
        error_type: str,
        requested_route: str | None = None,
    ) -> None:
        """
        Persist a sanitized failed LLM route event for one alert.

        Args:
            state: Current triage state containing alert correlation identifiers.
            error_type: Exception class name without provider response details.
            requested_route: Optional route override for the failed planning or report task.

        Returns:
            None.

        Raises:
            ValueError: If alert context is unavailable.
        """
        if not state.alert:
            raise ValueError("Alert context is required for failed LLM route audit logging.")

        client = build_clickhouse_client(host=self.config.clickhouse_host, port=self.config.clickhouse_port)

        write_agent_audit_event(
            client=client,
            action="llm_route_failed",
            status="failed",
            agent_run_id=state.agent_run_id,
            alert_id=state.alert.alert_id,
            alert_key=state.alert.alert_key,
            tool_name="llm_router",
            input_payload={"requested_route": requested_route or self.llm_route_name},
            output_payload={"error_type": error_type},
            error_message=error_type,
        )

    def finalize_report_node(self, graph_state: GraphState) -> GraphState:
        """
        Finalize the in-memory triage report.

        Args:
            graph_state: Current graph state.

        Returns:
            Updated graph state with report populated.
        """
        state         = self.get_state(graph_state)
        llm_narrative = None

        try:
            llm_narrative = self.build_llm_report_narrative(state)
            state.add_evidence(self.llm_response_to_evidence(llm_narrative))

        except Exception as exc:
            error_type = type(exc).__name__

            self.append_error(state, f"llm_report_narrative failed: {error_type}")

            try:
                self.write_llm_failure_audit(state=state, error_type=error_type)

            except Exception as audit_exc:
                self.append_error(state, f"llm_route_failure_audit failed: {type(audit_exc).__name__}")

        else:
            try:
                self.write_llm_success_audit(state=state, response=llm_narrative)

            except Exception as exc:
                self.append_error(state, f"llm_route_audit failed: {type(exc).__name__}")

        state.report = self.build_report_from_state(state, llm_narrative)

        return {"state": state}

    def store_report_node(self, graph_state: GraphState) -> GraphState:
        """
        Store Markdown and JSON report artifacts to S3.

        Args:
            graph_state: Current graph state.

        Returns:
            Updated graph state with report S3 URIs populated.
        """
        state = self.get_state(graph_state)

        if not state.report:
            raise ValueError("Report must be finalized before storing artifacts.")

        storage_result = store_triage_report(
            report=state.report,
            bucket=self.config.artifacts_bucket,
            prefix=self.config.artifacts_prefix,
            endpoint_url=self.config.s3_endpoint_url,
            clickhouse_host=self.config.clickhouse_host,
            clickhouse_port=self.config.clickhouse_port,
        )
        state.add_evidence(
            EvidenceItem(
                evidence_type=EvidenceType.ARTIFACT,
                tool_name="s3_artifacts",
                description="Stored final Markdown and JSON triage report artifacts.",
                rows=[storage_result],
                row_count=2,
                summary=f"Stored report artifacts at {storage_result['markdown_report_s3_uri']}.",
                s3_uri=storage_result["markdown_report_s3_uri"],
            )
        )

        lifecycle_result = mark_alert_triaged(
            alert=state.report.alert,
            report_s3_uri=storage_result["markdown_report_s3_uri"],
            agent_run_id=state.agent_run_id,
            clickhouse_host=self.config.clickhouse_host,
            clickhouse_port=self.config.clickhouse_port,
        )
        state.add_evidence(
            EvidenceItem(
                evidence_type=EvidenceType.NOTE,
                tool_name="alert_lifecycle",
                description="Updated alert lifecycle metadata after report storage.",
                rows=[lifecycle_result],
                row_count=1,
                summary=f"Alert lifecycle update finished with status {lifecycle_result.get('status')}.",
                s3_uri=storage_result["markdown_report_s3_uri"],
            )
        )

        if lifecycle_result.get("status") != "success":
            self.append_error(state, f"alert_lifecycle failed: {lifecycle_result.get('error_message', 'unknown error')}")

        logger.info("Stored report node completed | agent_run_id=%s", state.agent_run_id)

        return {"state": state}

    # --- Defining Audit Node
    def write_final_audit_node(self, graph_state: GraphState) -> GraphState:
        """
        Write the final triage completion audit event.

        Args:
            graph_state: Current graph state.

        Returns:
            Current graph state after audit logging.
        """
        state = self.get_state(graph_state)

        if not state.alert or not state.report:
            raise ValueError("Alert and report are required before final audit logging.")

        client      = build_clickhouse_client(host=self.config.clickhouse_host, port=self.config.clickhouse_port)
        started_at  = time.monotonic()
        duration_ms = int((time.monotonic() - started_at) * 1000)

        write_agent_audit_event(
            client=client,
            action="triage_completed",
            status="success",
            agent_run_id=state.agent_run_id,
            alert_id=state.alert.alert_id,
            alert_key=state.alert.alert_key,
            tool_name=self.tool_name,
            duration_ms=duration_ms,
            input_payload={"alert_key": state.alert.alert_key},
            output_payload={
                "report_id": state.report.report_id,
                "confidence": state.report.confidence,
                "hypothesis_count": len(state.hypotheses),
                "evidence_count": len(state.evidence),
                "evidence_plan_source": state.evidence_plan.planner_source if state.evidence_plan else "none",
                "evidence_plan_categories": [
                    str(request.category)
                    for request in (state.evidence_plan.requests if state.evidence_plan else [])
                ],
                "evidence_plan_policy_added": (
                    state.evidence_plan.policy_added_categories if state.evidence_plan else []
                ),
                "evidence_plan_policy_adjusted": (
                    state.evidence_plan.policy_adjusted_categories if state.evidence_plan else []
                ),
                "hypothesis_framing_source": (
                    state.hypothesis_framing.source if state.hypothesis_framing else "none"
                ),
                "hypothesis_framing_provider": (
                    state.hypothesis_framing.provider if state.hypothesis_framing else ""
                ),
                "hypothesis_framing_model": (
                    state.hypothesis_framing.model if state.hypothesis_framing else ""
                ),
                "hypothesis_framing_policy_adjustments": (
                    state.hypothesis_framing.policy_adjustments if state.hypothesis_framing else []
                ),
                "errors": state.errors,
            },
            row_count=len(state.evidence),
            report_s3_uri=state.report.markdown_report_s3_uri,
        )

        logger.info("Triage completion audited | agent_run_id=%s", state.agent_run_id)

        return {"state": state}

    # --- Defining Routing Logic
    def route_after_rank(self, graph_state: GraphState) -> str:
        """
        Decide whether the workflow needs another evidence collection loop.

        Args:
            graph_state: Current graph state.

        Returns:
            Next route label.
        """
        state = self.get_state(graph_state)

        if state.should_collect_more_evidence:
            return "collect_extra_evidence"

        return "finalize_report"
