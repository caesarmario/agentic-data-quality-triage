####
## Triage Specialist Node Factory for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Compose focused triage node modules behind one backward-compatible factory."""

# --- Importing Libraries
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from agent.nodes.action_planning import ActionPlanningNodes
from agent.nodes.alert_context import AlertContextNodes
from agent.nodes.evidence_orchestration import EvidenceOrchestrationNodes
from agent.nodes.history_evidence import HistoryEvidenceNodes
from agent.nodes.hypotheses import HypothesisNodes
from agent.nodes.lineage_evidence import LineageEvidenceNodes
from agent.nodes.pipeline_evidence import PipelineEvidenceNodes
from agent.nodes.reporting import ReportNodes
from agent.nodes.sql_evidence import SqlEvidenceNodes
from agent.state import Alert, EvidenceItem, TriageState


# --- Defining Composite Factory
@dataclass(frozen=True)
class TriageNodeFactory(
    AlertContextNodes,
    EvidenceOrchestrationNodes,
    SqlEvidenceNodes,
    HistoryEvidenceNodes,
    PipelineEvidenceNodes,
    LineageEvidenceNodes,
    HypothesisNodes,
    ReportNodes,
    ActionPlanningNodes,
):
    """
    Compose specialist node callables for the supervisor-lite triage graph.

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
