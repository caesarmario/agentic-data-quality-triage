####
## Shared Triage Node Contracts for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Shared dependency declarations and state helpers for modular triage nodes."""

# --- Importing Libraries
from __future__ import annotations

from typing import Any, Callable

from agent.state import Alert, EvidenceItem, TriageState


# --- Defining Type Aliases
GraphState = dict[str, TriageState]


# --- Defining Shared Node Contract
class TriageNodeMixin:
    """
    Declare the runtime dependencies shared by every triage node mixin.

    The concrete ``TriageNodeFactory`` supplies these attributes through its
    dataclass constructor. Keeping the dependency surface here makes each
    specialist node module independently readable without introducing a
    second runtime container or changing the public graph contract.
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
