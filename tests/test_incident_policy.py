####
## Incident Policy Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

from datetime import date

from pipelines.seeding.incident_policy import (
    is_auto_incident_scenario,
    resolve_incident_scenario_for_date,
)


# --- Defining Functions
def test_auto_incident_policy_is_deterministic_for_same_date() -> None:
    """
    Validate that auto scenario resolution is reproducible for one business date.

    Returns:
        None.
    """
    first_resolution  = resolve_incident_scenario_for_date(date(2026, 5, 13), "auto")
    second_resolution = resolve_incident_scenario_for_date(date(2026, 5, 13), "auto")

    assert first_resolution.resolved_scenario == second_resolution.resolved_scenario
    assert first_resolution.policy_score == second_resolution.policy_score


def test_auto_incident_policy_can_select_non_baseline_dates() -> None:
    """
    Validate that the configured deterministic policy can produce real incidents.

    Returns:
        None.
    """
    resolution = resolve_incident_scenario_for_date(date(2026, 5, 13), "auto")

    assert resolution.resolved_scenario == "duplicates_spike"
    assert resolution.selected_incidents == ["duplicate_orders"]


def test_explicit_incident_scenario_bypasses_auto_policy() -> None:
    """
    Validate that manual/testing scenarios are not replaced by daily policy.

    Returns:
        None.
    """
    resolution = resolve_incident_scenario_for_date(date(2026, 5, 13), "baseline")

    assert resolution.resolved_scenario == "baseline"
    assert resolution.policy_mode == "explicit"
    assert resolution.selected_incidents == []


def test_auto_incident_scenario_aliases_are_supported() -> None:
    """
    Validate supported aliases for deterministic daily policy mode.

    Returns:
        None.
    """
    assert is_auto_incident_scenario("auto") is True
    assert is_auto_incident_scenario("daily_auto") is True
    assert is_auto_incident_scenario("baseline") is False
