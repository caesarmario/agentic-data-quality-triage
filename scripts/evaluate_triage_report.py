####
## Triage Evaluation CLI for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Backward-compatible CLI wrapper for the modular triage evaluator."""

# --- Importing Libraries
import sys
from pathlib import Path


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.evaluation.triage import (
    DEFAULT_CATEGORY_ALIASES,
    DEFAULT_INCIDENT_CONFIG_DIR,
    EvaluationCheck,
    EvaluationScenario,
    accepted_categories_for,
    build_parser,
    build_scenario_catalog_summary,
    discover_scenario_paths,
    evaluate_alert_signal,
    evaluate_report,
    evaluate_root_cause_category,
    extract_alert_signature,
    extract_top_root_cause_category,
    load_json_report,
    load_scenario_catalog,
    load_yaml_file,
    main,
    resolve_scenario_path,
    scenario_to_eval_spec,
    validate_scenario_config,
)


# --- Defining Public API
__all__ = [
    "DEFAULT_CATEGORY_ALIASES",
    "DEFAULT_INCIDENT_CONFIG_DIR",
    "EvaluationCheck",
    "EvaluationScenario",
    "accepted_categories_for",
    "build_parser",
    "build_scenario_catalog_summary",
    "discover_scenario_paths",
    "evaluate_alert_signal",
    "evaluate_report",
    "evaluate_root_cause_category",
    "extract_alert_signature",
    "extract_top_root_cause_category",
    "load_json_report",
    "load_scenario_catalog",
    "load_yaml_file",
    "resolve_scenario_path",
    "scenario_to_eval_spec",
    "validate_scenario_config",
]


# --- Running CLI Entrypoint
if __name__ == "__main__":
    main()
