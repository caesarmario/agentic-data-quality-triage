####
## Alert Context Nodes for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Load alert context and create a bounded evidence plan."""

# --- Importing Libraries
from __future__ import annotations

from agent.nodes.base import GraphState, TriageNodeMixin
from agent.tools.alerts import load_alert
from pipelines.common.logging import logger


# --- Defining Alert Context Nodes
class AlertContextNodes(TriageNodeMixin):
    """Provide alert loading and evidence-planning nodes."""

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
