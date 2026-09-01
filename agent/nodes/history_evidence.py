####
## DQ And Incident History Nodes for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Collect bounded DQ and prior incident history for one alert."""

# --- Importing Libraries
from __future__ import annotations

from agent.nodes.base import TriageNodeMixin
from agent.state import EvidenceItem, TriageState
from agent.tools.dq_history import collect_dq_history_evidence
from agent.tools.incident_history import collect_incident_history_evidence


# --- Defining History Evidence Nodes
class HistoryEvidenceNodes(TriageNodeMixin):
    """Provide deterministic DQ and incident history evidence collectors."""

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

    def collect_incident_history_for_state(self, state: TriageState) -> EvidenceItem:
        """
        Collect bounded prior investigation outcomes for the exact loaded alert.

        Args:
            state: Current triage state.

        Returns:
            EvidenceItem with sanitized prior outcomes and report references.
        """
        if not state.alert:
            raise ValueError("Alert must be loaded before incident history evidence.")

        return collect_incident_history_evidence(
            alert=state.alert,
            agent_run_id=state.agent_run_id,
            clickhouse_host=self.config.clickhouse_host,
            clickhouse_port=self.config.clickhouse_port,
        )
