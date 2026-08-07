####
## Supervisor-Lite Architecture Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

from pathlib import Path

from agent.architecture import architecture_summary, list_expected_node_names, list_specialist_node_specs
from agent.nodes import TriageNodeFactory


# --- Defining Constants
PROJECT_ROOT = Path(__file__).resolve().parents[1]
GRAPH_PATH   = PROJECT_ROOT / "agent" / "graph.py"
DOC_PATH     = PROJECT_ROOT / "docs" / "agent_supervisor_lite_architecture.md"
NODES_PATH   = PROJECT_ROOT / "agent" / "nodes" / "triage_nodes.py"


# --- Defining Tests
def test_specialist_registry_has_expected_supervisor_lite_shape() -> None:
    """
    Ensure the supervisor-lite registry documents the intended architecture boundary.

    Returns:
        None.
    """
    summary = architecture_summary()

    assert summary["architecture_name"] == "supervisor_lite_langgraph_triage"
    assert summary["shared_state_model"] == "TriageState"
    assert summary["handoff_style"] == "state_based_explicit_handoff"
    assert summary["node_count"] == 9


def test_specialist_registry_entries_are_complete() -> None:
    """
    Ensure every specialist node has responsibility, tools, and handoff documentation.

    Returns:
        None.
    """
    specs = list_specialist_node_specs()

    assert len(specs) == len(list_expected_node_names())

    for spec in specs:
        assert spec.node_name
        assert spec.specialist_name
        assert spec.responsibility
        assert spec.tools
        assert spec.reads_state
        assert spec.writes_state
        assert "receive" in spec.handoff_contract.lower() or "workflow ends" in spec.handoff_contract.lower()


def test_registry_node_names_match_langgraph_add_node_calls() -> None:
    """
    Ensure documented specialist nodes match actual LangGraph node registration.

    Returns:
        None.
    """
    graph_content = GRAPH_PATH.read_text(encoding="utf-8")

    for node_name in list_expected_node_names():
        assert f'workflow.add_node("{node_name}"' in graph_content


def test_graph_uses_node_factory_instead_of_nested_node_definitions() -> None:
    """
    Ensure graph.py stays focused on wiring specialist node modules.

    Returns:
        None.
    """
    graph_content = GRAPH_PATH.read_text(encoding="utf-8")
    nodes_content = NODES_PATH.read_text(encoding="utf-8")

    assert "TriageNodeFactory(" in graph_content
    assert "def load_alert_node" not in graph_content
    assert "def gather_context_node" not in graph_content
    assert "def load_alert_node" in nodes_content
    assert "def gather_context_node" in nodes_content


def test_specialist_node_factory_exposes_registered_node_methods() -> None:
    """
    Ensure every registered graph node has a specialist factory method.

    Returns:
        None.
    """
    for node_name in list_expected_node_names():
        assert hasattr(TriageNodeFactory, f"{node_name}_node") or node_name == "collect_extra_evidence"

    assert hasattr(TriageNodeFactory, "collect_extra_evidence_node")
    assert hasattr(TriageNodeFactory, "route_after_rank")


def test_report_node_persists_successful_and_failed_llm_route_audits() -> None:
    """
    Ensure report finalization records both successful and failed LLM route calls.

    Returns:
        None.
    """
    nodes_content = NODES_PATH.read_text(encoding="utf-8")

    assert "write_llm_route_audit_event(" in nodes_content
    assert 'action="llm_route_failed"' in nodes_content
    assert 'tool_name="llm_router"' in nodes_content
    assert "llm_route_audit failed" in nodes_content
    assert "llm_route_failure_audit failed" in nodes_content


def test_supervisor_lite_doc_mentions_core_boundaries() -> None:
    """
    Ensure the architecture document captures key non-negotiable design boundaries.

    Returns:
        None.
    """
    doc_content = DOC_PATH.read_text(encoding="utf-8")

    assert "not a fully autonomous boss and child agent system" in doc_content
    assert "TriageState" in doc_content
    assert "EvidencePlanProposal" in doc_content
    assert "collector allowlist" in doc_content
    assert "HypothesisFramingProposal" in doc_content
    assert "confidence and ranking" in doc_content
    assert "approval-gated" in doc_content
    assert "heuristic fallback" in doc_content
    assert "MCP" in doc_content

def test_evidence_planning_node_precedes_tool_collection() -> None:
    """
    Ensure the graph plans categories before any deterministic collector executes.

    Returns:
        None.
    """
    graph_content = GRAPH_PATH.read_text(encoding="utf-8")
    nodes_content = NODES_PATH.read_text(encoding="utf-8")

    assert 'workflow.add_edge("load_alert", "plan_evidence")' in graph_content
    assert 'workflow.add_edge("plan_evidence", "gather_context")' in graph_content
    assert "collector_allowlist" in nodes_content
    assert "getattr(self" not in nodes_content
