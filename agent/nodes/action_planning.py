####
## Action Planning Routing for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Choose whether evidence is sufficient before final action recommendations."""

# --- Importing Libraries
from __future__ import annotations

from agent.nodes.base import GraphState, TriageNodeMixin


# --- Defining Action Planning Routing
class ActionPlanningNodes(TriageNodeMixin):
    """Route low-confidence investigations through one bounded evidence loop."""

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
