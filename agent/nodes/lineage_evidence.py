####
## Lineage And Schema Evidence Nodes for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Collect dbt lineage and deterministic schema-drift evidence."""

# --- Importing Libraries
from __future__ import annotations

from agent.nodes.base import TriageNodeMixin
from agent.state import EvidenceItem, TriageState
from agent.tools.dbt_lineage import collect_dbt_lineage_evidence
from agent.tools.schema_drift import collect_schema_drift_evidence


# --- Defining Lineage Evidence Nodes
class LineageEvidenceNodes(TriageNodeMixin):
    """Provide dbt lineage and schema-contract evidence collectors."""

    def collect_lineage_evidence(self, state: TriageState) -> EvidenceItem:
        """
        Collect dbt lineage evidence for the affected table.

        Args:
            state: Current triage state.

        Returns:
            EvidenceItem with lineage rows.
        """
        if not state.alert:
            raise ValueError("Alert must be loaded before lineage evidence.")

        return collect_dbt_lineage_evidence(
            alert=state.alert,
            agent_run_id=state.agent_run_id,
            manifest_path=self.config.manifest_path,
            manifest_s3_uri=self.config.manifest_s3_uri,
            endpoint_url=self.config.s3_endpoint_url,
            clickhouse_host=self.config.clickhouse_host,
            clickhouse_port=self.config.clickhouse_port,
        )

    def collect_schema_drift_for_state(self, state: TriageState) -> EvidenceItem:
        """
        Collect exact persisted schema contract findings for the loaded alert.

        Args:
            state: Current triage state containing a schema drift alert.

        Returns:
            EvidenceItem with bounded exact-run and exact-table schema findings.
        """
        if not state.alert:
            raise ValueError("Alert must be loaded before schema drift evidence.")

        return collect_schema_drift_evidence(
            alert=state.alert,
            agent_run_id=state.agent_run_id,
            clickhouse_host=self.config.clickhouse_host,
            clickhouse_port=self.config.clickhouse_port,
        )
