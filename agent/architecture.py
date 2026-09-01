####
## Supervisor-Lite Agent Architecture Registry for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

from dataclasses import dataclass

from pipelines.common.logging import logger


# --- Defining Data Models
@dataclass(frozen=True)
class SpecialistNodeSpec:
    """
    Describe one specialist node in the supervisor-lite triage graph.

    Attributes:
        node_name: LangGraph node name used by the workflow.
        specialist_name: Human-readable specialist role.
        responsibility: Main job owned by the node.
        tools: Tool names or project modules used by the node.
        reads_state: TriageState fields read by the node.
        writes_state: TriageState fields written by the node.
        handoff_contract: Short description of what the next node can rely on.
    """

    node_name: str
    specialist_name: str
    responsibility: str
    tools: tuple[str, ...]
    reads_state: tuple[str, ...]
    writes_state: tuple[str, ...]
    handoff_contract: str


# --- Defining Constants
SUPERVISOR_ARCHITECTURE_NAME = "supervisor_lite_langgraph_triage"
SHARED_STATE_MODEL           = "TriageState"
BOUNDED_SPECIALIST_PILOTS    = (
    "incident_triage_agent",
    "metadata_lineage_agent",
)
CROSS_AGENT_CONTRACTS        = (
    "AgentTaskEnvelope",
    "AgentResultEnvelope",
    "SupervisorState",
)

SPECIALIST_NODE_SPECS = (
    SpecialistNodeSpec(
        node_name="load_alert",
        specialist_name="Alert Context Specialist",
        responsibility="Load one alert from ClickHouse and normalize it into agent state.",
        tools=("agent.tools.alerts",),
        reads_state=("alert_id", "alert_key", "agent_run_id"),
        writes_state=("alert",),
        handoff_contract="Downstream nodes receive one validated Alert with table, metric, severity, dt, and details.",
    ),
    SpecialistNodeSpec(
        node_name="plan_evidence",
        specialist_name="Evidence Planning Specialist",
        responsibility="Create a typed evidence-category plan and enforce deterministic collection policy before tools run.",
        tools=("llm_router", "agent.planning.evidence"),
        reads_state=("alert", "agent_run_id"),
        writes_state=("evidence_plan", "errors"),
        handoff_contract="Evidence collection receives one policy-enforced EvidencePlan containing allowlisted categories only.",
    ),
    SpecialistNodeSpec(
        node_name="gather_context",
        specialist_name="Evidence Collection Specialist",
        responsibility=(
            "Map the EvidencePlan to deterministic SQL, DQ history, incident history, pipeline run, "
            "dbt lineage, and schema drift collectors."
        ),
        tools=(
            "clickhouse_sql",
            "dq_history",
            "incident_history",
            "pipeline_runs",
            "dbt_lineage",
            "schema_drift",
        ),
        reads_state=("alert", "evidence_plan", "agent_run_id"),
        writes_state=("evidence", "errors"),
        handoff_contract="Hypothesis nodes receive evidence items with tool names, summaries, rows, and audit trails.",
    ),
    SpecialistNodeSpec(
        node_name="generate_hypotheses",
        specialist_name="Hypothesis Generation Specialist",
        responsibility="Create deterministic candidates, then apply policy-bounded model wording grounded in evidence IDs.",
        tools=("agent.graph.build_hypotheses_for_state", "agent.reasoning.hypotheses", "llm_router"),
        reads_state=("alert", "evidence"),
        writes_state=("hypotheses", "hypothesis_framing", "errors"),
        handoff_contract="Ranking receives policy-owned confidence and categories plus validated operator-friendly wording.",
    ),
    SpecialistNodeSpec(
        node_name="rank_hypotheses",
        specialist_name="Hypothesis Ranking Specialist",
        responsibility="Sort hypotheses by confidence and decide whether the evidence loop is strong enough.",
        tools=("TriageState.top_hypothesis",),
        reads_state=("hypotheses", "confidence_threshold", "evidence_iterations"),
        writes_state=("hypotheses",),
        handoff_contract="Finalize or extra-evidence nodes receive the routing decision from ranked confidence.",
    ),
    SpecialistNodeSpec(
        node_name="collect_extra_evidence",
        specialist_name="Extra Evidence Specialist",
        responsibility="Retry one bounded alert-specific evidence query when confidence is below threshold.",
        tools=("clickhouse_sql", "schema_drift"),
        reads_state=("alert", "evidence_iterations", "agent_run_id"),
        writes_state=("evidence", "evidence_iterations", "errors"),
        handoff_contract="Hypothesis generation receives one more bounded evidence item and an incremented loop count.",
    ),
    SpecialistNodeSpec(
        node_name="finalize_report",
        specialist_name="Report Writing Specialist",
        responsibility="Build Markdown/JSON-ready report content, including optional routed LLM narrative evidence.",
        tools=("llm_router", "agent.graph.build_report_from_state"),
        reads_state=("alert", "hypotheses", "evidence", "errors"),
        writes_state=("report", "evidence"),
        handoff_contract="Storage node receives a complete TriageReport with evidence, hypotheses, and approval-gated actions.",
    ),
    SpecialistNodeSpec(
        node_name="store_report",
        specialist_name="Artifact Storage Specialist",
        responsibility="Persist Markdown and JSON reports to SeaweedFS S3 and update alert lifecycle status.",
        tools=("s3_artifacts", "alert_lifecycle"),
        reads_state=("report", "alert", "agent_run_id"),
        writes_state=("report", "audit_events", "errors"),
        handoff_contract="Final audit node receives stored report URIs and alert lifecycle outcome.",
    ),
    SpecialistNodeSpec(
        node_name="write_final_audit",
        specialist_name="Audit Specialist",
        responsibility="Write the final triage audit event summarizing completion, evidence count, and report URI.",
        tools=("agent_audit_log",),
        reads_state=("alert", "report", "evidence", "errors", "agent_run_id"),
        writes_state=("audit_events",),
        handoff_contract="Workflow ends with an auditable final event in ClickHouse agent_audit_log.",
    ),
)

EXPECTED_NODE_ORDER = tuple(spec.node_name for spec in SPECIALIST_NODE_SPECS)


# --- Defining Functions
def list_specialist_node_specs() -> tuple[SpecialistNodeSpec, ...]:
    """
    Return the supervisor-lite specialist node registry.

    Returns:
        Tuple of SpecialistNodeSpec entries in expected graph order.
    """
    logger.info("Listing supervisor-lite specialist node specs | count=%d", len(SPECIALIST_NODE_SPECS))

    return SPECIALIST_NODE_SPECS


def list_expected_node_names() -> tuple[str, ...]:
    """
    Return expected LangGraph node names in workflow order.

    Returns:
        Tuple of node names.
    """
    return EXPECTED_NODE_ORDER


def architecture_summary() -> dict[str, object]:
    """
    Build a compact architecture summary for docs, tests, and future MCP exposure.

    Returns:
        Dictionary describing the supervisor-lite architecture contract.
    """
    summary = {
        "architecture_name": SUPERVISOR_ARCHITECTURE_NAME,
        "shared_state_model": SHARED_STATE_MODEL,
        "node_count": len(SPECIALIST_NODE_SPECS),
        "node_order": list(EXPECTED_NODE_ORDER),
        "handoff_style": "state_based_explicit_handoff",
        "autonomy_boundary": "single_supervisor_graph_not_fully_autonomous_boss_child_agents",
        "bounded_specialist_pilots": list(BOUNDED_SPECIALIST_PILOTS),
        "cross_agent_contracts": list(CROSS_AGENT_CONTRACTS),
        "default_runtime": "supervisor_lite",
        "control_plane_supervisor_status": "manual_airflow_pilot",
        "control_plane_routing": "deterministic_single_handoff",
    }

    logger.info("Supervisor-lite architecture summary built | summary=%s", summary)

    return summary
