####
## Airflow LIFE Evaluation Helpers for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Airflow-native summary helpers for the LIFE reliability evaluation DAG."""

# --- Importing Libraries
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any
from uuid import uuid4

# --- Getting Logger
logger = logging.getLogger(__name__)


# --- Defining Constants
DEFAULT_LIFE_ARTIFACT_PREFIX = "agent-life"
DEFAULT_COMPARISON_ARTIFACT_PREFIX = "agent-life-comparisons"
DEFAULT_LIFE_REPLAY_PREFIX   = "agent-replays"
DEFAULT_MIN_CONFIDENCE       = 0.70

LIFE_SOURCE_MODES = (
    "stored_report",
    "scenario_replay",
    "supervisor_comparison",
)

LIFE_SCENARIO_NAMES = (
    "baseline",
    "duplicates_spike",
    "late_arriving",
    "missing_latest_day",
    "missing_segment",
    "null_spike",
    "schema_breaking_change",
)

LIFE_REPLAY_SCENARIO_NAMES = ("schema_breaking_change",)

SAFE_EVALUATION_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
SAFE_ARTIFACT_PREFIX   = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./=-]{0,199}$")
SAFE_REPORT_S3_URI     = re.compile(
    r"^s3://[A-Za-z0-9][A-Za-z0-9.-]{1,62}/[A-Za-z0-9][A-Za-z0-9._/=-]{1,1000}/report\.json$"
)
SAFE_SUPERVISOR_PARENT_RUN_ID = re.compile(
    r"^(?:|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12})$"
)

LIFE_EVALUATION_TASK_IDS = (
    "t05_prepare_source_report",
    "t10_evaluate_life_report",
    "t20_verify_life_artifacts",
)


# --- Defining DAG-Safe Contract Helpers
def normalize_evaluation_run_id(run_id: str | None = None) -> str:
    """
    Normalize one path-safe evaluation identifier without importing runtime packages.

    Args:
        run_id: Optional operator or Airflow-provided run identifier.

    Returns:
        Validated identifier or a generated UUID.

    Raises:
        ValueError: If the identifier contains unsupported characters.
    """
    normalized = (run_id or str(uuid4())).strip()

    if not SAFE_EVALUATION_RUN_ID.fullmatch(normalized):
        raise ValueError("LIFE evaluation run id contains unsupported characters.")

    return normalized


def build_life_artifact_keys(
    evaluation_run_id: str,
    prefix: str = DEFAULT_LIFE_ARTIFACT_PREFIX,
) -> tuple[str, str]:
    """
    Build deterministic LIFE artifact keys inside the Airflow DAG bundle.

    Args:
        evaluation_run_id: Stable evaluation correlation identifier.
        prefix: Path-safe top-level object prefix.

    Returns:
        Tuple containing JSON and Markdown object keys.

    Raises:
        ValueError: If the prefix contains unsupported or traversal segments.
    """
    run_id          = normalize_evaluation_run_id(evaluation_run_id)
    clean_prefix    = prefix.strip().strip("/")
    prefix_segments = clean_prefix.split("/")

    if (
        not clean_prefix
        or not SAFE_ARTIFACT_PREFIX.fullmatch(clean_prefix)
        or any(segment in {"", ".", ".."} for segment in prefix_segments)
    ):
        raise ValueError("LIFE artifact prefix must be path-safe.")

    base_key = f"{clean_prefix}/run_id={run_id}"

    return f"{base_key}/life_report.json", f"{base_key}/life_report.md"


def build_comparison_artifact_keys(
    comparison_run_id: str,
    prefix: str = DEFAULT_COMPARISON_ARTIFACT_PREFIX,
) -> tuple[str, str]:
    """
    Build deterministic supervisor comparison artifact keys inside the DAG bundle.

    Args:
        comparison_run_id: Stable comparison correlation identifier.
        prefix: Path-safe comparison artifact prefix.

    Returns:
        Tuple containing JSON and Markdown object keys.
    """
    life_json_key, _ = build_life_artifact_keys(comparison_run_id, prefix=prefix)
    base_key         = life_json_key.removesuffix("/life_report.json")

    return f"{base_key}/comparison.json", f"{base_key}/comparison.md"


# --- Defining Summary Functions
def emit_life_evaluation_summary(**context: Any) -> dict[str, Any]:
    """
    Emit an Airflow-native summary after evaluation and verification succeed.

    Args:
        context: Airflow task context containing the current DagRun.

    Returns:
        Summary containing source correlation, expected artifacts, and task state evidence.

    Raises:
        ValueError: If no DagRun context is available.
    """
    dag_run = context.get("dag_run")

    if dag_run is None:
        raise ValueError("Airflow dag_run context is required for LIFE evaluation summary.")

    conf              = dag_run.conf or {}
    evaluation_run_id = normalize_evaluation_run_id(
        str(conf.get("evaluation_run_id") or dag_run.run_id)
    )
    source_mode           = str(conf.get("source_mode", "stored_report"))
    default_prefix        = (
        DEFAULT_COMPARISON_ARTIFACT_PREFIX
        if source_mode == "supervisor_comparison"
        else DEFAULT_LIFE_ARTIFACT_PREFIX
    )
    artifact_prefix       = str(conf.get("artifact_prefix") or default_prefix)
    artifact_builder      = (
        build_comparison_artifact_keys
        if source_mode == "supervisor_comparison"
        else build_life_artifact_keys
    )
    json_key, markdown_key = artifact_builder(evaluation_run_id, prefix=artifact_prefix)
    bucket                = os.getenv("ARTIFACTS_BUCKET", "dq-artifacts")
    summary               = {
        "dag_id": dag_run.dag_id,
        "run_id": dag_run.run_id,
        "evaluation_run_id": evaluation_run_id,
        "scenario_id": str(conf.get("scenario", "")),
        "source_mode": source_mode,
        "source_report_s3_uri": str(conf.get("report_s3_uri", "")),
        "single_supervisor_run_id": str(conf.get("single_supervisor_run_id", "")),
        "fanout_supervisor_run_id": str(conf.get("fanout_supervisor_run_id", "")),
        "critic_enabled": bool(conf.get("enable_critic", False)),
        "json_report_s3_uri": f"s3://{bucket}/{json_key}",
        "markdown_report_s3_uri": f"s3://{bucket}/{markdown_key}",
        "result": "success",
        "state_evidence": "t30 reached after evaluation artifacts and audit evidence were verified",
        "task_states": {task_id: "success" for task_id in LIFE_EVALUATION_TASK_IDS},
    }

    logger.info("Airflow LIFE evaluation summary | payload=%s", json.dumps(summary, sort_keys=True))
    print(json.dumps(summary, indent=2, sort_keys=True))

    return summary
