####
## Control Plane Supervisor Runner for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Run one bounded supervisor request from Airflow or an operator CLI."""

# --- Importing Libraries
from __future__ import annotations

import argparse
import base64
import binascii
import json
import sys
from pathlib import Path


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.specialists.contracts import AgentTaskStatus
from agent.supervisor.fanout import run_control_plane_fanout
from agent.supervisor.models import (
    SupervisorExecutionMode,
    SupervisorIntent,
    SupervisorRequest,
    SupervisorRunResult,
)
from agent.supervisor.runtime import run_control_plane_supervisor
from pipelines.common.logging import logger


# --- Defining Terminal Status Policy
VERIFIABLE_TERMINAL_STATUSES = {
    AgentTaskStatus.SUCCESS,
    AgentTaskStatus.PARTIAL,
}


# --- Defining CLI Helpers
def build_parser() -> argparse.ArgumentParser:
    """
    Build the bounded supervisor CLI parser.

    Returns:
        Configured ArgumentParser without arbitrary tools, routes, or commands.
    """
    parser = argparse.ArgumentParser(
        description="Run one policy-driven Control Plane Supervisor request."
    )

    parser.add_argument("--run-id", required=True, help="Airflow or operator run correlation ID.")
    parser.add_argument(
        "--intent",
        default=SupervisorIntent.ASSET_CONTEXT.value,
        choices=[item.value for item in SupervisorIntent],
        help="Explicit or deterministic auto supervisor intent.",
    )
    parser.add_argument("--question", default="", help="Bounded operator wording for auto intent.")
    parser.add_argument("--alert-id", default="", help="Optional alert UUID.")
    parser.add_argument("--alert-key", default="", help="Optional Alert Ref or system alert key.")
    parser.add_argument("--qualified-name", default="", help="Optional database.table asset.")
    parser.add_argument("--query", default="", help="Optional trusted metadata search query.")
    parser.add_argument("--domain", default="", help="Optional metadata domain filter.")
    parser.add_argument("--data-layer", default="", help="Optional raw, staging, or mart filter.")
    parser.add_argument("--certification-status", default="", help="Optional certification filter.")
    parser.add_argument("--lifecycle-status", default="", help="Optional lifecycle filter.")
    parser.add_argument(
        "--sql-proposal-base64",
        default="",
        help="Base64-encoded SQL proposal; raw SQL is not accepted through the shell boundary.",
    )
    parser.add_argument("--sql-purpose", default="", help="Optional single-line SQL purpose.")
    parser.add_argument("--sql-hard-limit", type=int, default=100)
    parser.add_argument(
        "--sql-require-date-filter",
        default="true",
        choices=("true", "false"),
    )
    parser.add_argument("--sql-max-scan-bytes", type=int, default=1024 * 1024 * 1024)
    parser.add_argument(
        "--schema-run-id",
        default="",
        help="Exact persisted schema detector DagRun identifier.",
    )
    parser.add_argument("--schema-finding-limit", type=int, default=50)
    parser.add_argument("--result-limit", type=int, default=10)
    parser.add_argument("--max-depth", type=int, default=5)
    parser.add_argument("--max-nodes", type=int, default=100)
    parser.add_argument("--confidence-threshold", type=float, default=0.70)
    parser.add_argument("--max-evidence-iterations", type=int, default=2)
    parser.add_argument("--manifest-s3-uri", default="")
    parser.add_argument("--artifacts-bucket", default="")
    parser.add_argument("--artifacts-prefix", default="agent-reports")
    parser.add_argument("--requester", default="airflow")
    parser.add_argument(
        "--execution-mode",
        default=SupervisorExecutionMode.SINGLE.value,
        choices=[item.value for item in SupervisorExecutionMode],
        help="Single handoff by default; fanout is manual and policy-bounded.",
    )
    parser.add_argument("--max-workers", type=int, default=1)
    parser.add_argument("--max-concurrency", type=int, default=1)
    parser.add_argument(
        "--allow-external-llm",
        default="false",
        choices=("true", "false"),
        help="Request-level LLM permission; the global provider switch must also be enabled.",
    )
    parser.add_argument("--max-handoffs", type=int, default=1)
    parser.add_argument("--max-retries", type=int, default=0)
    parser.add_argument("--max-model-calls", type=int, default=3)
    parser.add_argument("--token-budget", type=int, default=16_384)
    parser.add_argument("--estimated-cost-budget-usd", type=float, default=0.05)
    parser.add_argument("--latency-budget-ms", type=int, default=300_000)

    return parser


# --- Defining Output Helpers
def build_operator_summary(result: SupervisorRunResult) -> dict[str, object]:
    """
    Build a compact Airflow-safe summary without dumping specialist graph state.

    Args:
        result: Typed supervisor result.

    Returns:
        Bounded parent-run, route, budget, approval, and artifact summary.
    """
    state               = result.supervisor_state
    specialist_results  = state.specialist_results
    specialist_result   = specialist_results[0] if specialist_results else None
    structured_output   = specialist_result.structured_output if specialist_result else {}
    model_call_count    = sum(item.model_call_count for item in specialist_results)
    token_usage         = sum(item.token_usage for item in specialist_results)
    estimated_cost      = sum(item.estimated_cost_usd for item in specialist_results)
    evidence_count      = sum(len(item.evidence_references) for item in specialist_results)
    specialist_duration = sum(item.duration_ms for item in specialist_results)

    return {
        "status": result.status.value,
        "execution_mode": result.execution_mode.value,
        "execution_plan_hash": result.execution_plan_hash,
        "worker_count": result.worker_count,
        "parent_run_id": str(result.parent_run_id),
        "requested_intent": result.requested_intent.value,
        "resolved_intent": result.resolved_intent.value if result.resolved_intent else "",
        "selected_specialist": result.selected_specialist,
        "task_type": result.task_type,
        "task_id": str(result.task_id) if result.task_id else "",
        "handoff_count": len(state.handoff_history),
        "run_context_event_count": len(state.run_context_event_ids),
        "run_context_event_ids": [str(item) for item in state.run_context_event_ids],
        "incident_memory_count": len(state.incident_memory_ids),
        "incident_memory_ids": [str(item) for item in state.incident_memory_ids],
        "failure_isolated": result.failure_isolated,
        "approval_state": state.approval_state.value,
        "actual_model_route": (
            specialist_result.model_route.value if specialist_result else ""
        ),
        "requested_model_routes": structured_output.get("requested_model_routes", []),
        "executed_model_routes": structured_output.get("executed_model_routes", []),
        "model_providers": structured_output.get("model_providers", []),
        "model_names": structured_output.get("model_names", []),
        "fallback_reasons": structured_output.get("fallback_reasons", []),
        "strong_review_requested": bool(
            structured_output.get("strong_review_requested", False)
        ),
        "strong_review_satisfied": bool(
            structured_output.get("strong_review_satisfied", False)
        ),
        "model_call_count": model_call_count,
        "token_usage": token_usage,
        "estimated_cost_usd": estimated_cost,
        "specialist_duration_ms": specialist_duration,
        "confidence": specialist_result.confidence if specialist_result else 0.0,
        "complexity_tier": str(structured_output.get("complexity_tier", "")),
        "complexity_score": int(structured_output.get("complexity_score", 0) or 0),
        "complexity_reason_codes": structured_output.get(
            "complexity_reason_codes",
            [],
        ),
        "investigation_errors": structured_output.get("investigation_errors", []),
        "investigation_error_count": len(
            structured_output.get("investigation_errors", [])
        ),
        "evidence_reference_count": evidence_count,
        "worker_results": [
            {
                "task_id": str(item.task_id),
                "specialist_name": item.specialist_name,
                "task_type": item.task_type,
                "status": item.status.value,
                "model_route": item.model_route.value,
                "model_call_count": item.model_call_count,
                "token_usage": item.token_usage,
                "estimated_cost_usd": item.estimated_cost_usd,
                "duration_ms": item.duration_ms,
                "evidence_reference_count": len(item.evidence_references),
            }
            for item in specialist_results
        ],
        "aggregation": result.aggregation,
        "report_id": str(structured_output.get("report_id", "")),
        "alert_display_id": str(structured_output.get("alert_display_id", "")),
        "markdown_report_s3_uri": str(
            structured_output.get("markdown_report_s3_uri", "")
        ),
        "json_report_s3_uri": str(structured_output.get("json_report_s3_uri", "")),
        "trust_status": str(structured_output.get("trust_status", "")),
        "sql_review_decision": str(structured_output.get("decision", "")),
        "proposal_sql_hash": str(structured_output.get("proposal_sql_hash", "")),
        "query_risk_level": str(structured_output.get("query_risk_level", "")),
        "schema_assessment": str(structured_output.get("assessment", "")),
        "schema_impact_level": str(structured_output.get("impact_level", "")),
        "source_schema_run_id": str(structured_output.get("source_schema_run_id", "")),
        "schema_finding_count": int(structured_output.get("finding_count", 0) or 0),
        "impacted_asset_count": int(
            structured_output.get("impacted_asset_count", 0) or 0
        ),
        "impacted_test_count": int(
            structured_output.get("impacted_test_count", 0) or 0
        ),
        "execution_performed": bool(structured_output.get("execution_performed", False)),
        "max_handoffs": state.max_handoffs,
        "max_retries": state.max_retries,
        "max_model_calls": state.max_model_calls,
        "token_budget": state.token_budget,
        "estimated_cost_budget_usd": state.estimated_cost_budget_usd,
        "latency_budget_ms": state.latency_budget_ms,
        "budget": result.audit_summary.get("budget", {}),
        "routing_policy": result.audit_summary.get("routing_policy", {}),
        "resilience": result.audit_summary.get("resilience", {}),
        "final_response": result.final_response,
        "errors": state.errors,
    }


def require_verifiable_terminal_result(result: SupervisorRunResult) -> None:
    """
    Allow complete or partial evidence to continue into the Airflow verifier task.

    Args:
        result: Typed supervisor terminal result.

    Returns:
        None when the result is eligible for strict downstream verification.

    Raises:
        RuntimeError: If the supervisor was blocked or failed before verification.
    """
    if result.status in VERIFIABLE_TERMINAL_STATUSES:
        return

    raise RuntimeError(
        f"Control Plane Supervisor ended with status={result.status.value}: "
        + "; ".join(result.supervisor_state.errors)
    )


def decode_sql_proposal(encoded_value: str) -> str:
    """
    Decode one Base64 SQL proposal at the Python boundary.

    Args:
        encoded_value: Base64 text carried safely through Airflow and shell layers.

    Returns:
        Decoded UTF-8 SQL, or an empty string when no proposal is supplied.

    Raises:
        ValueError: If the Base64 payload or UTF-8 content is invalid.
    """
    normalized = encoded_value.strip()

    if not normalized:
        return ""

    try:
        decoded = base64.b64decode(normalized, validate=True).decode("utf-8")

    except (binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError("sql_proposal_base64 must contain valid Base64-encoded UTF-8 SQL.") from exc

    return decoded


def build_request(args: argparse.Namespace) -> SupervisorRequest:
    """
    Build a strict SupervisorRequest from parsed CLI arguments.

    Args:
        args: Parsed CLI namespace.

    Returns:
        Validated SupervisorRequest.
    """
    return SupervisorRequest(
        intent=SupervisorIntent(args.intent),
        question=args.question,
        alert_id=args.alert_id,
        alert_key=args.alert_key,
        qualified_name=args.qualified_name,
        query=args.query,
        domain=args.domain,
        data_layer=args.data_layer,
        certification_status=args.certification_status,
        lifecycle_status=args.lifecycle_status,
        sql_proposal=decode_sql_proposal(args.sql_proposal_base64),
        sql_purpose=args.sql_purpose,
        sql_hard_limit=args.sql_hard_limit,
        sql_require_date_filter=args.sql_require_date_filter == "true",
        sql_max_scan_bytes=args.sql_max_scan_bytes,
        schema_run_id=args.schema_run_id,
        schema_finding_limit=args.schema_finding_limit,
        result_limit=args.result_limit,
        max_depth=args.max_depth,
        max_nodes=args.max_nodes,
        confidence_threshold=args.confidence_threshold,
        max_evidence_iterations=args.max_evidence_iterations,
        manifest_s3_uri=args.manifest_s3_uri,
        artifacts_bucket=args.artifacts_bucket,
        artifacts_prefix=args.artifacts_prefix,
        requester=args.requester,
        execution_mode=SupervisorExecutionMode(args.execution_mode),
        max_workers=args.max_workers,
        max_concurrency=args.max_concurrency,
        allow_external_llm=args.allow_external_llm == "true",
        max_handoffs=args.max_handoffs,
        max_retries=args.max_retries,
        max_model_calls=args.max_model_calls,
        token_budget=args.token_budget,
        estimated_cost_budget_usd=args.estimated_cost_budget_usd,
        latency_budget_ms=args.latency_budget_ms,
    )


# --- Defining Runtime
def run_from_args(args: argparse.Namespace) -> dict[str, object]:
    """
    Execute one supervisor request and emit compact operational evidence.

    Args:
        args: Parsed CLI namespace.

    Returns:
        JSON-safe compact supervisor summary.

    Raises:
        RuntimeError: If the supervisor is blocked or fails before audit verification.
    """
    request = build_request(args)

    logger.info(
        "Starting Control Plane Supervisor | run_id=%s requested_intent=%s execution_mode=%s workers=%d concurrency=%d external_llm=%s",
        args.run_id,
        request.intent.value,
        request.execution_mode.value,
        request.max_workers,
        request.max_concurrency,
        request.allow_external_llm,
    )

    runtime = (
        run_control_plane_fanout
        if request.execution_mode == SupervisorExecutionMode.FANOUT
        else run_control_plane_supervisor
    )
    result  = runtime(request=request, external_run_id=args.run_id)
    summary = build_operator_summary(result)

    print(json.dumps(summary, indent=2, sort_keys=True))

    require_verifiable_terminal_result(result)

    if result.status == AgentTaskStatus.PARTIAL:
        logger.warning(
            "Control Plane Supervisor retained partial evidence; continuing to strict Airflow verification | run_id=%s parent_run_id=%s errors=%s",
            args.run_id,
            result.parent_run_id,
            result.supervisor_state.errors,
        )

    logger.info(
        "Control Plane Supervisor completed | run_id=%s parent_run_id=%s specialist=%s task_type=%s status=%s",
        args.run_id,
        result.parent_run_id,
        result.selected_specialist,
        result.task_type,
        result.status.value,
    )

    return summary


def main() -> None:
    """
    Parse CLI arguments and run one bounded supervisor request.

    Returns:
        None.
    """
    args = build_parser().parse_args()

    run_from_args(args)


# --- Running CLI Entrypoint
if __name__ == "__main__":
    main()
