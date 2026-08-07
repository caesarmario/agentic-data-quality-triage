####
## Triage Evaluation Contracts for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Add repo root before importing project packages when this file is executed by path.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.tools.s3 import parse_s3_uri
from pipelines.common.logging import logger
from pipelines.seeding.upload_to_s3 import build_s3_client


# --- Defining Constants
DEFAULT_INCIDENT_CONFIG_DIR = PROJECT_ROOT / "configs" / "incidents"
DEFAULT_CATEGORY_ALIASES = {
    "missing_partition_or_empty_partition": {"missing_partition", "freshness_gap"},
    "segment_drop": {"missing_segment"},
    "late_arriving_batch": {"late_arriving", "freshness_gap"},
    "duplicate_ingestion": {"duplicate_ingestion", "unknown_data_issue"},
    "completeness_regression": {"completeness_regression", "unknown_data_issue"},
    "no_incident": {"no_incident"},
}


# --- Defining Data Models
@dataclass(frozen=True)
class EvaluationCheck:
    """
    Represent one triage evaluation check result.

    Attributes:
        name: Evaluation check name.
        status: Check status, usually pass or fail.
        details: JSON-serializable check metadata.
    """

    name: str
    status: str
    details: dict[str, Any]


@dataclass(frozen=True)
class EvaluationScenario:
    """
    Represent one incident scenario available for triage evaluation.

    Attributes:
        scenario_id: Stable scenario identifier.
        dataset: Dataset name covered by the scenario.
        enabled: Whether this scenario should be included in default eval catalogs.
        path: YAML config path.
        root_cause_category: Expected ground-truth root cause category.
        expected_alert: Whether the scenario should generate an alert.
        expected_signal_count: Number of expected DQ signals in the config.
        triage_required: Whether the scenario is expected to require triage.
        description: Human-readable scenario description.
    """

    scenario_id: str
    dataset: str
    enabled: bool
    path: str
    root_cause_category: str
    expected_alert: bool
    expected_signal_count: int
    triage_required: bool
    description: str


# --- Defining Load Functions
def load_yaml_file(path: Path) -> dict[str, Any]:
    """
    Load a YAML file into a dictionary.

    Args:
        path: YAML file path.

    Returns:
        Parsed YAML dictionary.

    Raises:
        FileNotFoundError: If the YAML file is missing.
        ValueError: If the YAML content is not an object.
    """
    logger.info("Loading YAML file | path=%s", path)

    payload = yaml.safe_load(path.read_text(encoding="utf-8"))

    if not isinstance(payload, dict):
        raise ValueError(f"YAML file must contain an object: {path}")

    return payload


def resolve_scenario_path(scenario: str, config_dir: Path = DEFAULT_INCIDENT_CONFIG_DIR) -> Path:
    """
    Resolve an incident scenario id into a config file path.

    Args:
        scenario: Scenario id such as missing_latest_day.
        config_dir: Directory containing incident YAML configs.

    Returns:
        Incident scenario YAML path.
    """
    return config_dir / f"{scenario}.yml"


def discover_scenario_paths(config_dir: Path = DEFAULT_INCIDENT_CONFIG_DIR) -> list[Path]:
    """
    Discover incident scenario YAML files from the config directory.

    Args:
        config_dir: Directory containing incident YAML configs.

    Returns:
        Sorted list of scenario YAML paths. Policy files are excluded.
    """
    paths = []

    for path in sorted(config_dir.glob("*.yml")):
        if path.name == "daily_policy.yml":
            continue

        paths.append(path)

    logger.info("Discovered incident scenario configs | count=%d dir=%s", len(paths), config_dir)

    return paths


def validate_scenario_config(path: Path, scenario: dict[str, Any]) -> None:
    """
    Validate the minimum ground-truth fields required for triage evaluation.

    Args:
        path: Scenario YAML path.
        scenario: Parsed scenario dictionary.

    Returns:
        None.

    Raises:
        ValueError: If required scenario fields are missing or malformed.
    """
    required_top_level = ["scenario_id", "dataset", "enabled", "description", "ground_truth"]

    for key in required_top_level:
        if key not in scenario:
            raise ValueError(f"Scenario config missing required field '{key}': {path}")

    ground_truth = scenario.get("ground_truth")

    if not isinstance(ground_truth, dict):
        raise ValueError(f"Scenario ground_truth must be an object: {path}")

    for key in ["root_cause_category", "expected_alert", "expected_dq_signals"]:
        if key not in ground_truth:
            raise ValueError(f"Scenario ground_truth missing required field '{key}': {path}")

    expected_signals = ground_truth.get("expected_dq_signals")

    if not isinstance(expected_signals, list):
        raise ValueError(f"Scenario expected_dq_signals must be a list: {path}")

    for index, signal in enumerate(expected_signals):
        if not isinstance(signal, dict):
            raise ValueError(f"Scenario expected_dq_signals[{index}] must be an object: {path}")

        for key in ["table_name", "check_name", "severity"]:
            if key not in signal:
                raise ValueError(f"Scenario signal {index} missing required field '{key}': {path}")


def scenario_to_eval_spec(path: Path, scenario: dict[str, Any]) -> EvaluationScenario:
    """
    Convert a parsed scenario config into compact evaluation catalog metadata.

    Args:
        path: Scenario YAML path.
        scenario: Parsed scenario dictionary.

    Returns:
        EvaluationScenario metadata object.
    """
    validate_scenario_config(path=path, scenario=scenario)

    ground_truth     = scenario["ground_truth"]
    pipeline_behavior = scenario.get("expected_pipeline_behavior") or {}

    return EvaluationScenario(
        scenario_id=str(scenario["scenario_id"]),
        dataset=str(scenario["dataset"]),
        enabled=bool(scenario["enabled"]),
        path=str(path),
        root_cause_category=str(ground_truth["root_cause_category"]),
        expected_alert=bool(ground_truth["expected_alert"]),
        expected_signal_count=len(ground_truth["expected_dq_signals"]),
        triage_required=bool(pipeline_behavior.get("triage_required", False)),
        description=str(scenario["description"]),
    )


def load_scenario_catalog(
    config_dir: Path = DEFAULT_INCIDENT_CONFIG_DIR,
    include_disabled: bool = False,
) -> list[EvaluationScenario]:
    """
    Load and validate all incident scenarios available for evaluation.

    Args:
        config_dir: Directory containing incident YAML configs.
        include_disabled: Whether disabled scenarios should be returned.

    Returns:
        List of EvaluationScenario metadata objects.
    """
    scenarios = []

    for path in discover_scenario_paths(config_dir=config_dir):
        scenario = load_yaml_file(path)
        spec     = scenario_to_eval_spec(path=path, scenario=scenario)

        if spec.enabled or include_disabled:
            scenarios.append(spec)

    logger.info("Loaded scenario evaluation catalog | count=%d include_disabled=%s", len(scenarios), include_disabled)

    return scenarios


def build_scenario_catalog_summary(scenarios: list[EvaluationScenario]) -> dict[str, Any]:
    """
    Build a JSON-serializable summary for discovered evaluation scenarios.

    Args:
        scenarios: EvaluationScenario objects.

    Returns:
        Dictionary containing scenario count and compact scenario metadata.
    """
    return {
        "status": "success",
        "scenario_count": len(scenarios),
        "scenarios": [asdict(scenario) for scenario in scenarios],
    }


def load_json_report(report_json_path: str | None = None, report_s3_uri: str | None = None) -> dict[str, Any]:
    """
    Load a triage report JSON from local disk or S3-compatible storage.

    Args:
        report_json_path: Optional local JSON report path.
        report_s3_uri: Optional S3 URI to a JSON report artifact.

    Returns:
        Parsed report JSON dictionary.

    Raises:
        ValueError: If neither or both report sources are provided.
    """
    if bool(report_json_path) == bool(report_s3_uri):
        raise ValueError("Provide exactly one of --report-json-path or --report-s3-uri.")

    if report_json_path:
        path = Path(report_json_path)
        logger.info("Loading local triage report JSON | path=%s", path)

        return json.loads(path.read_text(encoding="utf-8"))

    assert report_s3_uri is not None

    bucket, key = parse_s3_uri(report_s3_uri)
    client      = build_s3_client()

    logger.info("Loading S3 triage report JSON | bucket=%s key=%s", bucket, key)

    response = client.get_object(Bucket=bucket, Key=key)

    return json.loads(response["Body"].read())


# --- Defining Extraction Functions
def extract_top_root_cause_category(report: dict[str, Any]) -> str:
    """
    Extract the top hypothesis root cause category from a report.

    Args:
        report: Parsed triage report JSON.

    Returns:
        Top root cause category, or an empty string when unavailable.
    """
    top_hypothesis = report.get("top_hypothesis") or {}

    return str(top_hypothesis.get("root_cause_category") or "")


def extract_alert_signature(report: dict[str, Any]) -> dict[str, str]:
    """
    Extract table/metric/severity signature from a triage report alert.

    Args:
        report: Parsed triage report JSON.

    Returns:
        Dictionary containing table_name, metric, and severity.
    """
    alert = report.get("alert") or {}

    return {
        "table_name": str(alert.get("table_name") or ""),
        "metric": str(alert.get("metric") or ""),
        "severity": str(alert.get("severity") or ""),
    }


# --- Defining Evaluation Functions
def accepted_categories_for(expected_category: str) -> set[str]:
    """
    Resolve accepted agent categories for one ground-truth category.

    Args:
        expected_category: Ground-truth root cause category from scenario config.

    Returns:
        Set of accepted agent output categories.
    """
    return DEFAULT_CATEGORY_ALIASES.get(expected_category, {expected_category})


def evaluate_root_cause_category(scenario: dict[str, Any], report: dict[str, Any]) -> EvaluationCheck:
    """
    Compare expected and actual root cause categories.

    Args:
        scenario: Parsed incident scenario config.
        report: Parsed triage report JSON.

    Returns:
        EvaluationCheck for root cause category matching.
    """
    ground_truth      = scenario.get("ground_truth") or {}
    expected_category = str(ground_truth.get("root_cause_category") or "")
    actual_category   = extract_top_root_cause_category(report)
    accepted          = accepted_categories_for(expected_category)

    status = "pass" if actual_category in accepted else "fail"

    return EvaluationCheck(
        name="root_cause_category",
        status=status,
        details={
            "expected": expected_category,
            "accepted": sorted(accepted),
            "actual": actual_category,
        },
    )


def evaluate_alert_signal(scenario: dict[str, Any], report: dict[str, Any]) -> EvaluationCheck:
    """
    Compare report alert table/metric/severity against expected scenario DQ signals.

    Args:
        scenario: Parsed incident scenario config.
        report: Parsed triage report JSON.

    Returns:
        EvaluationCheck for expected alert signal coverage.
    """
    ground_truth     = scenario.get("ground_truth") or {}
    expected_alert   = bool(ground_truth.get("expected_alert", False))
    expected_signals = ground_truth.get("expected_dq_signals") or []
    actual           = extract_alert_signature(report)

    if not expected_alert:
        return EvaluationCheck(
            name="alert_signal",
            status="pass" if not actual["table_name"] else "fail",
            details={"expected_alert": False, "actual": actual},
        )

    for signal in expected_signals:
        table_match    = actual["table_name"] == str(signal.get("table_name") or "")
        metric_match   = actual["metric"] == str(signal.get("check_name") or "")
        severity_match = actual["severity"] == str(signal.get("severity") or "")

        if table_match and metric_match and severity_match:
            return EvaluationCheck(
                name="alert_signal",
                status="pass",
                details={"expected_alert": True, "matched_signal": signal, "actual": actual},
            )

    return EvaluationCheck(
        name="alert_signal",
        status="fail",
        details={"expected_alert": True, "expected_signals": expected_signals, "actual": actual},
    )


def evaluate_report(scenario: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    """
    Evaluate one triage report against one incident scenario config.

    Args:
        scenario: Parsed incident scenario config.
        report: Parsed triage report JSON.

    Returns:
        Evaluation summary dictionary with pass/fail checks.
    """
    checks = [
        evaluate_root_cause_category(scenario=scenario, report=report),
        evaluate_alert_signal(scenario=scenario, report=report),
    ]
    failed_checks = [check for check in checks if check.status != "pass"]

    summary = {
        "status": "pass" if not failed_checks else "fail",
        "scenario_id": scenario.get("scenario_id"),
        "checks": [asdict(check) for check in checks],
        "passed": len(checks) - len(failed_checks),
        "failed": len(failed_checks),
    }

    logger.info(
        "Triage evaluation completed | scenario=%s status=%s passed=%d failed=%d",
        summary["scenario_id"],
        summary["status"],
        summary["passed"],
        summary["failed"],
    )

    return summary


# --- Defining CLI Functions
def build_parser() -> argparse.ArgumentParser:
    """
    Build the command-line parser for triage report evaluation.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(description="Evaluate a triage report against incident scenario ground truth.")

    parser.add_argument("--scenario", default=None, help="Incident scenario id, for example missing_latest_day.")
    parser.add_argument("--scenario-path", default=None, help="Optional explicit scenario YAML path.")
    parser.add_argument("--report-json-path", default=None, help="Optional local report.json path.")
    parser.add_argument("--report-s3-uri", default=None, help="Optional S3 URI to report.json.")
    parser.add_argument("--list-scenarios", action="store_true", help="List incident scenarios available for eval.")
    parser.add_argument("--include-disabled", action="store_true", help="Include disabled scenarios when listing.")
    parser.add_argument("--allow-failure", action="store_true", help="Print failed evaluation without non-zero exit.")

    return parser


def main() -> None:
    """
    Run triage report evaluation from CLI.

    Returns:
        None.

    Raises:
        SystemExit: With code 1 when evaluation fails unless --allow-failure is set.
    """
    parser = build_parser()
    args   = parser.parse_args()

    if args.list_scenarios:
        scenarios = load_scenario_catalog(include_disabled=args.include_disabled)
        print(json.dumps(build_scenario_catalog_summary(scenarios), indent=2, default=str))

        return

    if not args.scenario and not args.scenario_path:
        parser.error("Provide --scenario, --scenario-path, or --list-scenarios.")

    scenario_path = Path(args.scenario_path) if args.scenario_path else resolve_scenario_path(args.scenario)
    scenario      = load_yaml_file(scenario_path)
    report        = load_json_report(report_json_path=args.report_json_path, report_s3_uri=args.report_s3_uri)
    summary       = evaluate_report(scenario=scenario, report=report)

    print(json.dumps(summary, indent=2, default=str))

    if summary["status"] != "pass" and not args.allow_failure:
        raise SystemExit(1)


# --- Running CLI Entrypoint
if __name__ == "__main__":
    main()
