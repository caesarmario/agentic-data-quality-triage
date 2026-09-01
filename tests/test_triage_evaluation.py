####
## Triage Evaluation Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

from pathlib import Path

from scripts.evaluate_triage_report import (
    accepted_categories_for,
    build_scenario_catalog_summary,
    discover_scenario_paths,
    evaluate_alert_signal,
    evaluate_report,
    evaluate_root_cause_category,
    load_scenario_catalog,
    load_yaml_file,
    resolve_scenario_path,
    scenario_to_eval_spec,
)


# --- Defining Constants
PROJECT_ROOT = Path(__file__).resolve().parents[1]


# --- Defining Test Helpers
def build_report(table_name: str, metric: str, severity: str, root_cause_category: str) -> dict:
    """
    Build a compact synthetic triage report for evaluation tests.

    Args:
        table_name: Alert table name.
        metric: Alert metric/check name.
        severity: Alert severity.
        root_cause_category: Top hypothesis root cause category.

    Returns:
        Dictionary matching the report fields used by the evaluator.
    """
    return {
        "alert": {
            "table_name": table_name,
            "metric": metric,
            "severity": severity,
        },
        "top_hypothesis": {
            "root_cause_category": root_cause_category,
        },
    }


# --- Defining Tests
def test_resolve_scenario_path_points_to_incident_config() -> None:
    """
    Ensure scenario ids resolve to the expected YAML config path.

    Returns:
        None.
    """
    path = resolve_scenario_path("missing_latest_day")

    assert path == PROJECT_ROOT / "configs" / "incidents" / "missing_latest_day.yml"


def test_load_yaml_file_reads_scenario_config() -> None:
    """
    Ensure incident scenario YAML can be loaded for evaluation.

    Returns:
        None.
    """
    scenario = load_yaml_file(resolve_scenario_path("missing_latest_day"))

    assert scenario["scenario_id"] == "missing_latest_day"
    assert scenario["ground_truth"]["root_cause_category"] == "missing_partition_or_empty_partition"


def test_discover_scenario_paths_excludes_daily_policy() -> None:
    """
    Ensure scenario discovery skips policy files that are not eval ground truth configs.

    Returns:
        None.
    """
    paths = discover_scenario_paths()
    names = {path.name for path in paths}

    assert "daily_policy.yml" not in names
    assert "missing_latest_day.yml" in names
    assert "baseline.yml" in names


def test_load_scenario_catalog_validates_all_incident_configs() -> None:
    """
    Ensure every enabled incident config can be loaded into the evaluation catalog.

    Returns:
        None.
    """
    catalog      = load_scenario_catalog()
    scenario_ids = {scenario.scenario_id for scenario in catalog}

    assert scenario_ids == {
        "baseline",
        "duplicates_spike",
        "late_arriving",
        "missing_latest_day",
        "missing_segment",
        "null_spike",
        "schema_breaking_change",
    }

    assert all(scenario.enabled for scenario in catalog)
    assert all(scenario.dataset == "orders" for scenario in catalog)


def test_scenario_to_eval_spec_returns_ground_truth_metadata() -> None:
    """
    Ensure one scenario config is converted into compact eval catalog metadata.

    Returns:
        None.
    """
    path     = resolve_scenario_path("null_spike")
    scenario = load_yaml_file(path)
    spec     = scenario_to_eval_spec(path=path, scenario=scenario)

    assert spec.scenario_id == "null_spike"
    assert spec.root_cause_category == "completeness_regression"
    assert spec.expected_alert is True
    assert spec.expected_signal_count == 1
    assert spec.triage_required is True


def test_build_scenario_catalog_summary_is_json_ready() -> None:
    """
    Ensure the scenario catalog summary can be printed by the CLI list mode.

    Returns:
        None.
    """
    catalog = load_scenario_catalog()
    summary = build_scenario_catalog_summary(catalog)

    assert summary["status"] == "success"
    assert summary["scenario_count"] == 7
    assert summary["scenarios"][0]["scenario_id"] == "baseline"


def test_accepted_categories_maps_ground_truth_to_agent_categories() -> None:
    """
    Ensure known ground-truth categories accept operational agent categories.

    Returns:
        None.
    """
    accepted = accepted_categories_for("missing_partition_or_empty_partition")

    assert "missing_partition" in accepted
    assert "freshness_gap" in accepted


def test_evaluate_root_cause_category_passes_with_alias() -> None:
    """
    Ensure root cause evaluation passes when the agent category is an accepted alias.

    Returns:
        None.
    """
    scenario = load_yaml_file(resolve_scenario_path("missing_latest_day"))
    report = build_report(
        table_name="dq.raw_orders",
        metric="row_count_positive",
        severity="critical",
        root_cause_category="missing_partition",
    )

    check = evaluate_root_cause_category(scenario=scenario, report=report)

    assert check.status == "pass"
    assert check.details["actual"] == "missing_partition"


def test_evaluate_alert_signal_passes_for_expected_signal() -> None:
    """
    Ensure alert signal evaluation passes for one expected DQ signal.

    Returns:
        None.
    """
    scenario = load_yaml_file(resolve_scenario_path("missing_latest_day"))
    report = build_report(
        table_name="dq.stg_orders",
        metric="row_count_positive",
        severity="critical",
        root_cause_category="missing_partition",
    )

    check = evaluate_alert_signal(scenario=scenario, report=report)

    assert check.status == "pass"
    assert check.details["actual"]["table_name"] == "dq.stg_orders"


def test_evaluate_report_fails_when_signal_does_not_match() -> None:
    """
    Ensure report evaluation fails when the alert signal contradicts ground truth.

    Returns:
        None.
    """
    scenario = load_yaml_file(resolve_scenario_path("missing_segment"))
    report = build_report(
        table_name="dq.raw_orders",
        metric="row_count_positive",
        severity="critical",
        root_cause_category="missing_partition",
    )

    summary = evaluate_report(scenario=scenario, report=report)

    assert summary["status"] == "fail"
    assert summary["failed"] == 2
