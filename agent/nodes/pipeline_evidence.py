####
## Pipeline Run Evidence Nodes for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Collect Airflow pipeline-run evidence correlated to one data alert."""

# --- Importing Libraries
from __future__ import annotations

from agent.nodes.base import TriageNodeMixin
from agent.state import EvidenceItem, TriageState
from agent.tools.pipeline_runs import collect_pipeline_runs_evidence


# --- Defining Pipeline Evidence Nodes
class PipelineEvidenceNodes(TriageNodeMixin):
    """Provide bounded pipeline-run evidence collection."""

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
