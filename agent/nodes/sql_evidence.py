####
## SQL Evidence Nodes for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Collect current and historical partition evidence through guarded SQL."""

# --- Importing Libraries
from __future__ import annotations

from agent.nodes.base import TriageNodeMixin
from agent.state import EvidenceItem, TriageState
from agent.tools.clickhouse_sql import run_guarded_sql


# --- Defining SQL Evidence Nodes
class SqlEvidenceNodes(TriageNodeMixin):
    """Provide read-only current and recent partition evidence collectors."""

    def collect_current_partition_evidence(self, state: TriageState) -> EvidenceItem:
        """
        Collect current partition row count evidence.

        Args:
            state: Current triage state.

        Returns:
            EvidenceItem with one row_count result.
        """
        if not state.alert:
            raise ValueError("Alert must be loaded before partition evidence.")

        result = run_guarded_sql(
            sql=self.current_partition_sql(state.alert),
            agent_run_id=state.agent_run_id,
            alert_id=state.alert.alert_id,
            alert_key=state.alert.alert_key,
            hard_limit=10,
            require_date_filter=True,
            clickhouse_host=self.config.clickhouse_host,
            clickhouse_port=self.config.clickhouse_port,
        )
        row_count = int(result.rows[0].get("row_count", 0)) if result.rows else 0
        summary   = f"Current partition row count for {state.alert.table_name} on {state.alert.dt} is {row_count}."

        return self.sql_result_to_evidence(
            result,
            "Current partition row count for the alert table/date.",
            summary,
        )


    def collect_recent_partition_evidence(self, state: TriageState) -> EvidenceItem:
        """
        Collect a bounded recent partition row-count trend.

        Args:
            state: Current triage state containing a loaded alert.

        Returns:
            EvidenceItem with recent partition row-count rows.
        """
        if not state.alert:
            raise ValueError("Alert must be loaded before recent partition evidence.")

        result = run_guarded_sql(
            sql=self.recent_partition_sql(state.alert),
            agent_run_id=state.agent_run_id,
            alert_id=state.alert.alert_id,
            alert_key=state.alert.alert_key,
            hard_limit=20,
            require_date_filter=True,
            clickhouse_host=self.config.clickhouse_host,
            clickhouse_port=self.config.clickhouse_port,
        )
        summary = f"Recent partition row counts collected for {state.alert.table_name} around {state.alert.dt}."

        return self.sql_result_to_evidence(
            result,
            "Recent partition row counts for bounded trend comparison.",
            summary,
        )

    # --- Defining Hypothesis Nodes
