####
## LIFE Evaluation Runner for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Evaluate one triage report and persist human-reviewed reliability findings."""

# --- Importing Libraries
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.evaluation.life import (
    DEFAULT_LIFE_ARTIFACT_PREFIX,
    DEFAULT_MIN_CONFIDENCE,
    LIFE_SCENARIO_NAMES,
    LifeEvaluationReport,
    evaluate_life_report,
    persist_life_evaluation,
    validate_report_s3_uri,
)
from agent.evaluation.triage import (
    load_json_report,
    load_yaml_file,
    resolve_scenario_path,
    validate_scenario_config,
)
from pipelines.common.logging import logger


# --- Defining Evaluation Functions
def load_life_inputs(
    scenario_id: str,
    report_json_path: str | None = None,
    report_s3_uri: str | None = None,
) -> tuple[dict, dict, str]:
    """
    Load one allowlisted scenario and one source triage report.

    Args:
        scenario_id: Incident scenario identifier from the project catalog.
        report_json_path: Optional local report JSON path.
        report_s3_uri: Optional SeaweedFS S3 URI for report.json.

    Returns:
        Tuple containing scenario, report, and normalized report reference.

    Raises:
        ValueError: If the scenario is unknown or report source selection is invalid.
        FileNotFoundError: If the scenario or local report file is missing.
    """
    normalized_scenario = scenario_id.strip()

    if normalized_scenario not in LIFE_SCENARIO_NAMES:
        allowed = ", ".join(LIFE_SCENARIO_NAMES)
        raise ValueError(f"Unknown LIFE scenario: {scenario_id}. Allowed scenarios: {allowed}")

    if bool(report_json_path) == bool(report_s3_uri):
        raise ValueError("Provide exactly one LIFE source report using local path or S3 URI.")

    normalized_report_s3_uri = validate_report_s3_uri(report_s3_uri) if report_s3_uri else None

    scenario_path = resolve_scenario_path(normalized_scenario)
    scenario      = load_yaml_file(scenario_path)
    validate_scenario_config(path=scenario_path, scenario=scenario)
    report        = load_json_report(
        report_json_path=report_json_path,
        report_s3_uri=normalized_report_s3_uri,
    )
    report_ref = normalized_report_s3_uri or str(Path(str(report_json_path)).resolve())

    logger.info(
        "Loaded LIFE evaluation inputs | scenario=%s source=%s",
        normalized_scenario,
        report_ref,
    )

    return scenario, report, report_ref


def run_life_evaluation(
    scenario_id: str,
    report_json_path: str | None = None,
    report_s3_uri: str | None = None,
    evaluation_run_id: str | None = None,
    minimum_confidence: float = DEFAULT_MIN_CONFIDENCE,
    bucket: str | None = None,
    prefix: str = DEFAULT_LIFE_ARTIFACT_PREFIX,
    endpoint_url: str | None = None,
) -> LifeEvaluationReport:
    """
    Evaluate and persist one report without changing project policy or source data.

    Args:
        scenario_id: Allowlisted incident ground-truth scenario.
        report_json_path: Optional local source report path.
        report_s3_uri: Optional source report S3 URI.
        evaluation_run_id: Optional stable evaluation correlation identifier.
        minimum_confidence: Review threshold between zero and one.
        bucket: Optional target artifacts bucket.
        prefix: Path-safe LIFE artifact prefix.
        endpoint_url: Optional S3-compatible endpoint override.

    Returns:
        Persisted LIFE evaluation report with JSON and Markdown artifact URIs.
    """
    scenario, report, report_ref = load_life_inputs(
        scenario_id=scenario_id,
        report_json_path=report_json_path,
        report_s3_uri=report_s3_uri,
    )
    evaluation = evaluate_life_report(
        scenario=scenario,
        report=report,
        report_s3_uri=report_ref,
        evaluation_run_id=evaluation_run_id,
        minimum_confidence=minimum_confidence,
    )

    return persist_life_evaluation(
        evaluation=evaluation,
        source_report=report,
        bucket=bucket,
        prefix=prefix,
        endpoint_url=endpoint_url,
    )


# --- Defining CLI Helpers
def build_parser() -> argparse.ArgumentParser:
    """
    Build the bounded LIFE evaluation command-line parser.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(
        description="Evaluate one triage report and store non-mutating LIFE reliability artifacts."
    )
    parser.add_argument("--scenario", required=True, choices=LIFE_SCENARIO_NAMES)

    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--report-json-path", default=None, help="Local path to report.json.")
    source.add_argument("--report-s3-uri", default=None, help="SeaweedFS S3 URI to report.json.")

    parser.add_argument("--evaluation-run-id", default=None, help="Stable path-safe evaluation run id.")
    parser.add_argument(
        "--minimum-confidence",
        type=float,
        default=DEFAULT_MIN_CONFIDENCE,
        help="Confidence threshold below which a report requires review.",
    )
    parser.add_argument("--bucket", default=None, help="Optional LIFE artifacts bucket override.")
    parser.add_argument("--artifact-prefix", default=DEFAULT_LIFE_ARTIFACT_PREFIX)
    parser.add_argument("--endpoint-url", default=None, help="Optional S3 endpoint override.")
    parser.add_argument(
        "--fail-on-eval-failure",
        action="store_true",
        help="Return non-zero after persisting artifacts when evaluation status is fail.",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """
    Parse arguments, persist an evaluation, and expose status to Airflow.

    Args:
        argv: Optional argument sequence used by tests.

    Returns:
        One only when a hard evaluation failure is configured to fail the task, otherwise zero.
    """
    args       = build_parser().parse_args(argv)
    evaluation = run_life_evaluation(
        scenario_id=args.scenario,
        report_json_path=args.report_json_path,
        report_s3_uri=args.report_s3_uri,
        evaluation_run_id=args.evaluation_run_id,
        minimum_confidence=args.minimum_confidence,
        bucket=args.bucket,
        prefix=args.artifact_prefix,
        endpoint_url=args.endpoint_url,
    )

    print(json.dumps(evaluation.model_dump(mode="json"), indent=2, ensure_ascii=True))

    if args.fail_on_eval_failure and evaluation.eval_status == "fail":
        logger.error(
            "LIFE evaluation hard failure requested | run_id=%s scenario=%s categories=%s",
            evaluation.run_id,
            evaluation.scenario_id,
            evaluation.failure_categories,
        )

        return 1

    return 0


# --- Running CLI Entrypoint
if __name__ == "__main__":
    raise SystemExit(main())
