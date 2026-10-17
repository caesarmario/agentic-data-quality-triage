####
## Evidence Orchestration Nodes for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Resolve allowlisted collectors and gather bounded triage evidence."""

# --- Importing Libraries
from __future__ import annotations

from typing import Callable

from agent.context.bundle import MAX_SEGMENTS
from agent.nodes.base import GraphState, TriageNodeMixin
from agent.state import EvidenceCategory, EvidenceItem, TriageState
from agent.tools.clickhouse_sql import run_guarded_sql
from pipelines.common.clickhouse import format_date_literal, validate_qualified_table_name
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

        state.refresh_incident_context_bundle()
        return {"state": state}

    def collect_discriminating_evidence(self, state: TriageState) -> EvidenceItem | None:
        """Collect exact segment sets, never another undifferentiated partition count.

        Source/landing adapters remain caller-injected. Warehouse comparisons use
        only explicit trusted table mappings; the known country/channel coverage
        check may inspect its own alert table without inventing an upstream table.
        Failed queries consume their attempt and are not repeated automatically.
        """
        if not state.alert or not state.alert.dt:
            return None
        bundle = state.refresh_incident_context_bundle()
        tables = [s.table_name for s in bundle.snapshots if s.layer in {"raw", "mart"} and s.table_name]
        if state.alert.metric == "segment_coverage__country_channel":
            tables.append(state.alert.table_name)
        for table in dict.fromkeys(tables):
            table = validate_qualified_table_name(table)
            sql = (
                f"SELECT country, channel, count() AS row_count FROM {table} "
                f"WHERE dt = {format_date_literal(state.alert.dt)} "
                f"GROUP BY country, channel ORDER BY country, channel LIMIT {MAX_SEGMENTS + 1}"
            )
            if sql in state.attempted_context_queries or any(e.query.strip() == sql for e in state.evidence):
                continue
            state.attempted_context_queries.append(sql)
            result = run_guarded_sql(
                sql=sql, agent_run_id=state.agent_run_id, alert_id=state.alert.alert_id,
                alert_key=state.alert.alert_key, hard_limit=MAX_SEGMENTS + 1,
                require_date_filter=True, clickhouse_host=self.config.clickhouse_host,
                clickhouse_port=self.config.clickhouse_port,
            )
            return self.sql_result_to_evidence(
                result, "Exact bounded country/channel aggregates for source/raw/derived comparison.",
                "Exact segment aggregate collected; the bundle evaluates completeness and provenance.",
            )
        return None

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
