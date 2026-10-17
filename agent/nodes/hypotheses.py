####
## Hypothesis Nodes for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Generate, frame, rank, and enrich bounded root-cause hypotheses."""

# --- Importing Libraries
from __future__ import annotations

from agent.context.bundle import MAX_EXTRA_EVIDENCE_LOOPS, public_text
from agent.nodes.base import GraphState, TriageNodeMixin
from agent.state import EvidenceItem
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
        bundle = state.refresh_incident_context_bundle()
        # The legacy framing helper samples rows. Supply only the shared public
        # projection so private tool rows cannot bypass the new context boundary.
        framing_state = state.model_copy(update={
            "alert": state.alert.model_copy(update={"details": {}}) if state.alert else None,
            "evidence": [EvidenceItem(
                evidence_id=fact.evidence_id, evidence_type="note", tool_name=fact.tool_name,
                description="Public bounded incident observation.", summary=fact.observation,
                rows=[s.model_dump() for s in fact.segment_counts or []],
                created_at=fact.collected_at or bundle.built_at,
            ) for fact in bundle.evidence],
        })
        result = self.frame_hypotheses_for_state(framing_state, baseline_hypotheses)
        state.hypotheses         = result.hypotheses
        state.hypothesis_framing = result.framing
        state.refresh_incident_context_bundle()
        for hypothesis in state.hypotheses:
            hypothesis.title = public_text(hypothesis.title, "Provisional explanation")
            hypothesis.description = public_text(hypothesis.description, "Exact source evidence is incomplete.")
            hypothesis.recommended_action = state.incident_context_bundle.recovery_recommendation
        state.refresh_incident_context_bundle()

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

        if state.evidence_iterations >= min(state.max_evidence_iterations, MAX_EXTRA_EVIDENCE_LOOPS):
            return {"state": state}

        try:
            if state.alert.is_schema_drift:
                # One transient retry is useful only when no exact schema facts were loaded.
                if state.evidence_iterations == 0 and not any(e.tool_name == "schema_drift" and e.rows for e in state.evidence):
                    state.add_evidence(self.collect_schema_drift_for_state(state))
            else:
                item = self.collect_discriminating_evidence(state)
                if item is not None:
                    state.add_evidence(item)

        except Exception as exc:
            self.append_error(state, f"extra_discriminating_evidence failed: {type(exc).__name__}")

        state.evidence_iterations += 1
        state.refresh_incident_context_bundle()
        logger.info(
            "Extra evidence loop completed | agent_run_id=%s iteration=%d",
            state.agent_run_id,
            state.evidence_iterations,
        )

        return {"state": state}

    # --- Defining Report Nodes
