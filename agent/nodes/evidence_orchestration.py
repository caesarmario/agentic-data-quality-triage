####
## Evidence Orchestration Nodes for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Resolve allowlisted collectors and gather bounded triage evidence."""

# --- Importing Libraries
from __future__ import annotations

from typing import Callable

from agent.nodes.base import GraphState, TriageNodeMixin
from agent.state import EvidenceCategory, EvidenceItem, TriageState
from pipelines.common.logging import logger


# --- Defining Evidence Orchestration Nodes
class EvidenceOrchestrationNodes(TriageNodeMixin):
    """Provide evidence collector selection and baseline collection nodes."""

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
            EvidenceCategory.INCIDENT_HISTORY.value: self.collect_incident_history_for_state,
            EvidenceCategory.PIPELINE_RUNS.value: self.collect_pipeline_runs_for_state,
            EvidenceCategory.DBT_LINEAGE.value: self.collect_lineage_evidence,
            EvidenceCategory.SCHEMA_DRIFT.value: self.collect_schema_drift_for_state,
            EvidenceCategory.RECENT_PARTITION_TREND.value: self.collect_recent_partition_evidence,
        }

        if not state.evidence_plan:
            logger.warning(
                "Evidence plan missing; using deterministic baseline collectors | agent_run_id=%s",
                state.agent_run_id,
            )

            if state.alert and state.alert.is_schema_drift:
                return [
                    (EvidenceCategory.SCHEMA_DRIFT.value, self.collect_schema_drift_for_state),
                    (EvidenceCategory.INCIDENT_HISTORY.value, self.collect_incident_history_for_state),
                    (EvidenceCategory.PIPELINE_RUNS.value, self.collect_pipeline_runs_for_state),
                    (EvidenceCategory.DBT_LINEAGE.value, self.collect_lineage_evidence),
                ]

            return [
                (EvidenceCategory.CURRENT_PARTITION_ROW_COUNT.value, self.collect_current_partition_evidence),
                (EvidenceCategory.DQ_HISTORY.value, self.collect_dq_history_for_state),
                (EvidenceCategory.INCIDENT_HISTORY.value, self.collect_incident_history_for_state),
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
