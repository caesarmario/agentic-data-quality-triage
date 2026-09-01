####
## LIFE Scenario Replay Helpers for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Build and persist deterministic evaluation-only triage report replays."""

# --- Importing Libraries
from __future__ import annotations

import re
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from agent.evaluation.life import normalize_evaluation_run_id
from agent.tools.audit_log import write_agent_audit_event
from agent.tools.s3 import (
    put_json_artifact,
    put_text_artifact,
    resolve_artifacts_bucket,
)
from pipelines.common.clickhouse import build_clickhouse_client
from pipelines.common.logging import logger


# --- Defining Constants
DEFAULT_LIFE_REPLAY_PREFIX = "agent-replays"
LIFE_SOURCE_MODES          = ("stored_report", "scenario_replay")
LIFE_REPLAY_SCENARIO_NAMES = ("schema_breaking_change",)

SAFE_SCENARIO_ID   = re.compile(r"^[a-z][a-z0-9_]{1,99}$")
SAFE_REPLAY_PREFIX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./=-]{0,199}$")


# --- Defining Validation Helpers
def validate_replay_scenario(scenario: dict[str, Any]) -> str:
    """
    Validate that a scenario explicitly allows deterministic replay generation.

    Args:
        scenario: Parsed incident ground-truth configuration.

    Returns:
        Validated replay scenario identifier.

    Raises:
        ValueError: If the scenario is not allowlisted or lacks replay-only policy.
    """
    scenario_id = str(scenario.get("scenario_id") or "").strip()
    evaluation  = scenario.get("evaluation") or {}

    if scenario_id not in LIFE_REPLAY_SCENARIO_NAMES:
        raise ValueError(f"Scenario does not support LIFE replay: {scenario_id}")

    if evaluation.get("replay_only") is not True or evaluation.get("source_mode") != "scenario_replay":
        raise ValueError(f"Scenario is missing explicit replay-only policy: {scenario_id}")

    return scenario_id


def normalize_replay_prefix(prefix: str = DEFAULT_LIFE_REPLAY_PREFIX) -> str:
    """
    Normalize a path-safe S3 prefix for deterministic replay artifacts.

    Args:
        prefix: Requested top-level artifact prefix.

    Returns:
        Normalized path-safe prefix.

    Raises:
        ValueError: If the prefix contains traversal or unsupported characters.
    """
    normalized = prefix.strip().strip("/")
    segments   = normalized.split("/")

    if (
        not normalized
        or not SAFE_REPLAY_PREFIX.fullmatch(normalized)
        or any(segment in {"", ".", ".."} for segment in segments)
    ):
        raise ValueError("LIFE replay artifact prefix must be path-safe.")

    return normalized


# --- Defining Artifact Identity Helpers
def build_replay_artifact_keys(
    scenario_id: str,
    replay_run_id: str,
    prefix: str = DEFAULT_LIFE_REPLAY_PREFIX,
) -> tuple[str, str]:
    """
    Build deterministic JSON and Markdown keys for one replay source report.

    Args:
        scenario_id: Allowlisted evaluation-only scenario identifier.
        replay_run_id: Stable evaluation correlation identifier.
        prefix: Top-level replay artifact prefix.

    Returns:
        Tuple containing JSON and Markdown source-report keys.

    Raises:
        ValueError: If scenario, run ID, or prefix is unsafe.
    """
    normalized_scenario = scenario_id.strip()

    if normalized_scenario not in LIFE_REPLAY_SCENARIO_NAMES or not SAFE_SCENARIO_ID.fullmatch(
        normalized_scenario
    ):
        raise ValueError(f"Unknown LIFE replay scenario: {scenario_id}")

    normalized_run    = normalize_evaluation_run_id(replay_run_id)
    normalized_prefix = normalize_replay_prefix(prefix)
    base_key          = (
        f"{normalized_prefix}/scenario={normalized_scenario}/run_id={normalized_run}"
    )

    return f"{base_key}/report.json", f"{base_key}/report.md"


def build_replay_report_s3_uri(
    scenario_id: str,
    replay_run_id: str,
    bucket: str | None = None,
    prefix: str = DEFAULT_LIFE_REPLAY_PREFIX,
) -> str:
    """
    Resolve the deterministic S3 URI consumed by the LIFE evaluator.

    Args:
        scenario_id: Allowlisted evaluation-only scenario identifier.
        replay_run_id: Stable evaluation correlation identifier.
        bucket: Optional artifacts bucket override.
        prefix: Top-level replay artifact prefix.

    Returns:
        S3 URI ending in report.json.
    """
    json_key, _ = build_replay_artifact_keys(
        scenario_id=scenario_id,
        replay_run_id=replay_run_id,
        prefix=prefix,
    )

    return f"s3://{resolve_artifacts_bucket(bucket)}/{json_key}"


# --- Building Deterministic Replay Reports
def build_schema_breaking_change_report(
    scenario: dict[str, Any],
    replay_run_id: str,
) -> dict[str, Any]:
    """
    Build one evidence-backed breaking-schema report without querying or mutating ClickHouse.

    Args:
        scenario: Validated schema-breaking ground truth.
        replay_run_id: Stable evaluation correlation identifier.

    Returns:
        JSON-like triage report marked as a deterministic evaluation fixture.
    """
    scenario_id  = validate_replay_scenario(scenario)
    run_id       = normalize_evaluation_run_id(replay_run_id)
    ground_truth = scenario["ground_truth"]
    signal       = ground_truth["expected_dq_signals"][0]
    agent_run_id = str(uuid5(NAMESPACE_URL, f"agentic-dq:{scenario_id}:{run_id}:agent"))
    alert_id     = str(uuid5(NAMESPACE_URL, f"agentic-dq:{scenario_id}:{run_id}:alert"))
    evidence_id  = str(uuid5(NAMESPACE_URL, f"agentic-dq:{scenario_id}:{run_id}:evidence"))
    alert_key    = (
        f"orders|schema_drift|2026-08-08|{signal['table_name']}|"
        f"{signal['check_name']}|table"
    )
    contract_hash = "a" * 64
    schema_hash   = "b" * 64
    finding       = {
        "contract_name": "orders_warehouse_schema",
        "contract_version": 1,
        "contract_sha256": contract_hash,
        "schema_sha256": schema_hash,
        "qualified_name": signal["table_name"],
        "column_name": "customer_id",
        "check_type": "column_type",
        "status": "fail",
        "severity": "critical",
        "expected_value": "String",
        "actual_value": "Nullable(String)",
        "details": {"change_kind": "breaking", "source": "deterministic_replay"},
    }

    return {
        "agent_run_id": agent_run_id,
        "alert": {
            "alert_id": alert_id,
            "alert_key": alert_key,
            "alert_type": "schema_drift",
            "table_name": signal["table_name"],
            "metric": signal["check_name"],
            "severity": signal["severity"],
            "details": {
                "source_schema_run_id": f"replay__{run_id}",
                "contract_sha256": contract_hash,
                "schema_sha256": schema_hash,
                "finding_count": 1,
            },
        },
        "summary": (
            "A breaking schema contract change was detected on the raw orders table and needs "
            "producer and consumer review before any migration is considered."
        ),
        "impact": (
            "The customer identifier type no longer matches the published contract, so staging models, "
            "downstream marts, and reporting consumers may fail or interpret nullability incorrectly."
        ),
        "confidence": 0.94,
        "top_hypothesis": {
            "title": "Breaking schema contract change detected",
            "root_cause_category": ground_truth["root_cause_category"],
            "supporting_evidence_ids": [evidence_id],
            "recommended_action": (
                "Review producer intent and downstream compatibility, then prepare a versioned migration "
                "plan for explicit human approval. Do not alter the warehouse schema automatically."
            ),
        },
        "evidence": [
            {
                "evidence_id": evidence_id,
                "evidence_type": "schema_drift",
                "tool_name": "schema_drift",
                "description": "Persisted schema contract comparison evidence for the affected table.",
                "query": "",
                "rows": [finding],
                "row_count": 1,
                "summary": (
                    "The deterministic contract comparison found one critical customer_id type mismatch "
                    "between String and Nullable(String)."
                ),
            }
        ],
        "evidence_plan": {
            "planner_source": "deterministic_replay",
            "requested_categories": ["schema_drift", "dbt_lineage", "pipeline_runs"],
        },
        "hypothesis_framing": {"source": "deterministic_policy"},
        "recommended_actions": [
            (
                "Review producer intent and downstream compatibility, then prepare a versioned migration "
                "plan for explicit human approval. Do not alter the warehouse schema automatically."
            )
        ],
        "approval_gated_actions": [],
        "replay_provenance": {
            "source_mode": "scenario_replay",
            "scenario_id": scenario_id,
            "replay_run_id": run_id,
            "deterministic_fixture": True,
            "warehouse_schema_mutated": False,
            "production_triage_claimed": False,
        },
    }


def build_life_replay_report(
    scenario: dict[str, Any],
    replay_run_id: str,
) -> dict[str, Any]:
    """
    Dispatch one allowlisted scenario to its deterministic replay builder.

    Args:
        scenario: Parsed replay-only scenario configuration.
        replay_run_id: Stable evaluation correlation identifier.

    Returns:
        JSON-like source report for the LIFE evaluator.

    Raises:
        ValueError: If the scenario has no replay implementation.
    """
    scenario_id = validate_replay_scenario(scenario)

    if scenario_id == "schema_breaking_change":
        return build_schema_breaking_change_report(
            scenario=scenario,
            replay_run_id=replay_run_id,
        )

    raise ValueError(f"No LIFE replay builder is registered for scenario: {scenario_id}")


# --- Rendering And Persisting Replay Reports
def render_life_replay_report(report: dict[str, Any]) -> str:
    """
    Render a transparent operator-facing description of one replay source.

    Args:
        report: Deterministic replay source report.

    Returns:
        Markdown text that explicitly distinguishes replay from production triage.
    """
    provenance = report.get("replay_provenance") or {}
    alert      = report.get("alert") or {}
    evidence   = report.get("evidence") or []

    return "\n".join(
        [
            "# LIFE Evaluation Replay Source",
            "",
            "> This is a deterministic evaluation fixture. It is not a production triage result and did not mutate the warehouse schema.",
            "",
            f"- Scenario: `{provenance.get('scenario_id', '')}`",
            f"- Replay Run: `{provenance.get('replay_run_id', '')}`",
            f"- Table: `{alert.get('table_name', '')}`",
            f"- Metric: `{alert.get('metric', '')}`",
            f"- Severity: `{alert.get('severity', '')}`",
            f"- Evidence Items: `{len(evidence)}`",
            f"- Warehouse Schema Mutated: `{provenance.get('warehouse_schema_mutated', False)}`",
            "",
            "## Scenario Summary",
            "",
            str(report.get("summary") or ""),
            "",
        ]
    )


def persist_life_replay_report(
    scenario: dict[str, Any],
    replay_run_id: str,
    bucket: str | None = None,
    prefix: str = DEFAULT_LIFE_REPLAY_PREFIX,
    endpoint_url: str | None = None,
    clickhouse_client: Any | None = None,
) -> dict[str, Any]:
    """
    Persist deterministic replay JSON and Markdown artifacts idempotently.

    Args:
        scenario: Parsed replay-only scenario configuration.
        replay_run_id: Stable evaluation correlation identifier.
        bucket: Optional artifacts bucket override.
        prefix: Top-level replay artifact prefix.
        endpoint_url: Optional S3-compatible endpoint override.
        clickhouse_client: Optional injected ClickHouse client for durable audit logging.

    Returns:
        Stored report payload with source artifact URIs in replay provenance.
    """
    scenario_id            = validate_replay_scenario(scenario)
    report                 = build_life_replay_report(scenario=scenario, replay_run_id=replay_run_id)
    resolved_bucket        = resolve_artifacts_bucket(bucket)
    json_key, markdown_key = build_replay_artifact_keys(
        scenario_id=scenario_id,
        replay_run_id=replay_run_id,
        prefix=prefix,
    )
    json_uri     = f"s3://{resolved_bucket}/{json_key}"
    markdown_uri = f"s3://{resolved_bucket}/{markdown_key}"
    report["replay_provenance"].update(
        {
            "json_report_s3_uri": json_uri,
            "markdown_report_s3_uri": markdown_uri,
        }
    )

    put_text_artifact(
        bucket=resolved_bucket,
        key=markdown_key,
        text=render_life_replay_report(report),
        content_type="text/markdown; charset=utf-8",
        endpoint_url=endpoint_url,
    )
    put_json_artifact(
        bucket=resolved_bucket,
        key=json_key,
        payload=report,
        endpoint_url=endpoint_url,
    )

    alert  = report.get("alert") or {}
    client = clickhouse_client or build_clickhouse_client()
    write_agent_audit_event(
        client=client,
        action="life_replay_source_published",
        status="success",
        agent_run_id=report.get("agent_run_id"),
        alert_id=alert.get("alert_id"),
        alert_key=str(alert.get("alert_key") or ""),
        actor="life_evaluator",
        tool_name="life_replay_builder",
        input_payload={
            "scenario_id": scenario_id,
            "replay_run_id": replay_run_id,
            "source_mode": "scenario_replay",
        },
        output_payload={
            "json_report_s3_uri": json_uri,
            "markdown_report_s3_uri": markdown_uri,
            "warehouse_schema_mutated": False,
            "production_triage_claimed": False,
        },
        row_count=len(report.get("evidence") or []),
        report_s3_uri=json_uri,
    )

    logger.info(
        "LIFE replay source persisted | scenario=%s run_id=%s json_uri=%s",
        scenario_id,
        replay_run_id,
        json_uri,
    )

    return report
