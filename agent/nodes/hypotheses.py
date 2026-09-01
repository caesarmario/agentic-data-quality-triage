####
## Hypothesis Nodes for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Generate, frame, rank, and enrich bounded root-cause hypotheses."""

# --- Importing Libraries
from __future__ import annotations

from agent.nodes.base import GraphState, TriageNodeMixin
from pipelines.common.logging import logger


# --- Defining Hypothesis Nodes
class HypothesisNodes(TriageNodeMixin):
    """Provide deterministic hypothesis and bounded extra-evidence nodes."""

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
            if state.alert.is_schema_drift:
                # Retry the exact source evidence once when the initial read was incomplete or transiently unavailable.
                state.add_evidence(self.collect_schema_drift_for_state(state))
            else:
                state.add_evidence(self.collect_recent_partition_evidence(state))

        except Exception as exc:
            evidence_name = "extra_schema_drift_evidence" if state.alert.is_schema_drift else "extra_partition_evidence"
            self.append_error(state, f"{evidence_name} failed: {exc}")

        state.evidence_iterations += 1
        logger.info(
            "Extra evidence loop completed | agent_run_id=%s iteration=%d",
            state.agent_run_id,
            state.evidence_iterations,
        )

        return {"state": state}

    # --- Defining Report Nodes
