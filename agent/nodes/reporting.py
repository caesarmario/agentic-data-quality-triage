####
## Report And Audit Nodes for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Finalize, persist, and audit evidence-driven triage reports."""

# --- Importing Libraries
from __future__ import annotations

import time
from typing import Any

from agent.nodes.base import GraphState, TriageNodeMixin
from agent.state import EvidenceItem, EvidenceType, TriageState
from agent.supervisor.policy import assess_incident_complexity, resolve_report_reasoning_policy
from agent.tools.alert_lifecycle import mark_alert_triaged
from agent.tools.audit_log import (
    build_audit_idempotency_key,
    write_agent_audit_event,
    write_llm_route_audit_event,
)
from agent.tools.s3 import store_triage_report
from pipelines.common.clickhouse import build_clickhouse_client
from pipelines.common.logging import logger


# --- Defining Report And Audit Nodes
class ReportNodes(TriageNodeMixin):
    """Provide LLM route audit, report storage, lifecycle, and final audit nodes."""

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
        state                  = self.get_state(graph_state)
        llm_narrative          = None
        top_confidence         = state.top_hypothesis.confidence if state.top_hypothesis else 0.0
        complexity_assessment = assess_incident_complexity(state)
        report_route_policy    = resolve_report_reasoning_policy(
            confidence=top_confidence,
            confidence_threshold=state.confidence_threshold,
            complexity_assessment=complexity_assessment,
        )

        try:
            llm_narrative = self.build_llm_report_narrative(state)
            state.add_evidence(self.llm_response_to_evidence(llm_narrative))

        except Exception as exc:
            error_type = type(exc).__name__

            self.append_error(state, f"llm_report_narrative failed: {error_type}")

            try:
                self.write_llm_failure_audit(
                    state=state,
                    error_type=error_type,
                    requested_route=report_route_policy.provider_route,
                )

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
        audit_idempotency_key = build_audit_idempotency_key(
            "triage_completed",
            state.agent_run_id,
            state.alert.alert_key,
            state.report.report_id,
            state.report.markdown_report_s3_uri,
        )

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
                "llm_runtime": state.report.llm_runtime.model_dump(mode="json"),
                "errors": state.errors,
            },
            row_count=len(state.evidence),
            report_s3_uri=state.report.markdown_report_s3_uri,
            idempotency_key=audit_idempotency_key,
        )

        logger.info("Triage completion audited | agent_run_id=%s", state.agent_run_id)

        return {"state": state}

