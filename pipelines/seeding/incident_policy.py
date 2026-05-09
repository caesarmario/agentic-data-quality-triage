####
## Daily Incident Policy Resolver for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from pipelines.common.logging import logger
from pipelines.seeding.helpers import iter_dates, parse_date
from pipelines.seeding.incidents import resolve_incident_names


# --- Defining Constants
PROJECT_ROOT                       = Path(__file__).resolve().parents[2]
DEFAULT_DAILY_INCIDENT_POLICY_PATH = PROJECT_ROOT / "configs" / "incidents" / "daily_policy.yml"
DEFAULT_ARTIFACTS_BUCKET           = "dq-artifacts"
AUTO_INCIDENT_SCENARIOS            = {"auto", "daily_auto", "deterministic_random"}

PolicyMode = Literal["deterministic_random"]


# --- Defining Classes
class DailyIncidentPolicy(BaseModel):
    """
    Config model for deterministic daily incident selection.

    Attributes:
        enabled: Whether automatic daily incident selection is active.
        mode: Selection mode. Currently only deterministic_random is supported.
        seed_salt: Stable salt used with the business date to choose a scenario.
        default_scenario: Scenario returned when the policy is disabled or protected.
        scenario_weights: Scenario probability weights used by the resolver.
        protected_dates: Business dates that should always use default_scenario.
    """

    enabled: bool                      = True
    mode: PolicyMode                   = "deterministic_random"
    seed_salt: str                     = Field(min_length=1)
    default_scenario: str              = "baseline"
    scenario_weights: Dict[str, float]
    protected_dates: List[date]        = Field(default_factory=list)

    @field_validator("scenario_weights")
    @classmethod
    def validate_scenario_weights(cls, value: Dict[str, float]) -> Dict[str, float]:
        """
        Validate scenario probability weights from YAML.

        Args:
            value: Mapping of scenario name to positive or zero probability weight.

        Returns:
            The original mapping when it contains at least one positive weight.

        Raises:
            ValueError: If weights are empty, negative, or all zero.
        """
        if not value:
            raise ValueError("scenario_weights must not be empty")

        if any(weight < 0 for weight in value.values()):
            raise ValueError("scenario_weights must not contain negative values")

        if sum(value.values()) <= 0:
            raise ValueError("scenario_weights must contain at least one positive value")

        return value

    @model_validator(mode="after")
    def validate_scenarios(self) -> "DailyIncidentPolicy":
        """
        Validate that configured scenarios are supported by incident handlers.

        Returns:
            The current policy instance when all scenarios are valid.

        Raises:
            ValueError: If default_scenario or a weighted scenario is unsupported.
        """
        resolve_incident_names(self.default_scenario)

        for scenario_name in self.scenario_weights:
            resolve_incident_names(scenario_name)

        return self

    @property
    def normalized_weights(self) -> Dict[str, float]:
        """
        Normalize configured scenario weights to probabilities.

        Returns:
            Mapping of scenario name to normalized probability.
        """
        total = sum(self.scenario_weights.values())

        return {
            scenario_name: weight / total
            for scenario_name, weight in self.scenario_weights.items()
            if weight > 0
        }


class IncidentResolution(BaseModel):
    """
    Resolved incident scenario for one business date.

    Attributes:
        dt: Business date being generated.
        requested_scenario: Scenario requested by Airflow/CLI.
        resolved_scenario: Actual scenario applied by the generator.
        selected_incidents: Canonical incident handler names.
        policy_mode: Policy selection mode.
        policy_enabled: Whether the policy was active.
        policy_path: Config file used for policy resolution.
        policy_score: Stable hash score used for weighted selection.
        reason: Human-readable reason for the selected scenario.
    """

    dt: date
    requested_scenario: str
    resolved_scenario: str
    selected_incidents: List[str]
    policy_mode: str
    policy_enabled: bool
    policy_path: str
    policy_score: float | None         = None
    reason: str


# --- Defining Functions
def is_auto_incident_scenario(incident_scenario: str | None) -> bool:
    """
    Check whether a requested incident scenario should use daily auto policy.

    Args:
        incident_scenario: Scenario string from Airflow or CLI.

    Returns:
        True when the scenario asks for deterministic daily auto selection.
    """
    scenario = (incident_scenario or "").strip().lower()

    return scenario in AUTO_INCIDENT_SCENARIOS


def load_daily_incident_policy(path: str | Path | None = None) -> DailyIncidentPolicy:
    """
    Load and validate deterministic daily incident policy YAML.

    Args:
        path: Optional policy path. Defaults to configs/incidents/daily_policy.yml.

    Returns:
        Validated DailyIncidentPolicy instance.

    Raises:
        FileNotFoundError: If the policy YAML does not exist.
        ValueError: If the policy YAML is malformed or invalid.
    """
    policy_path = Path(path) if path else DEFAULT_DAILY_INCIDENT_POLICY_PATH
    return _load_daily_incident_policy_cached(str(policy_path.resolve()))


@lru_cache(maxsize=16)
def _load_daily_incident_policy_cached(policy_path_text: str) -> DailyIncidentPolicy:
    """
    Load and validate a daily incident policy with process-local caching.

    Args:
        policy_path_text: Resolved policy file path.

    Returns:
        Validated DailyIncidentPolicy instance.

    Raises:
        FileNotFoundError: If the policy YAML does not exist.
        ValueError: If the policy YAML is malformed or invalid.
    """
    policy_path = Path(policy_path_text)
    logger.info("Loading daily incident policy | path=%s", policy_path)

    if not policy_path.exists():
        logger.error("Daily incident policy not found | path=%s", policy_path)
        raise FileNotFoundError(f"Daily incident policy not found: {policy_path}")

    with policy_path.open("r", encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh) or {}

    if not isinstance(loaded, dict):
        logger.error("Daily incident policy root is not a mapping | path=%s", policy_path)
        raise ValueError(f"Daily incident policy root must be a mapping: {policy_path}")

    policy = DailyIncidentPolicy.model_validate(loaded)
    logger.info(
        "Daily incident policy validated | enabled=%s mode=%s scenarios=%s",
        policy.enabled,
        policy.mode,
        list(policy.scenario_weights.keys()),
    )

    return policy


def stable_policy_score(dt: date, seed_salt: str) -> float:
    """
    Build a stable pseudo-random score for one business date.

    Args:
        dt: Business date being generated.
        seed_salt: Stable salt from the incident policy config.

    Returns:
        Float in the [0.0, 1.0) range.
    """
    hash_input = f"{seed_salt}|{dt.isoformat()}".encode("utf-8")
    hash_value = int(hashlib.sha256(hash_input).hexdigest()[:16], 16)

    return hash_value / float(16**16)


def choose_weighted_scenario(policy: DailyIncidentPolicy, dt: date) -> tuple[str, float]:
    """
    Choose one scenario from policy weights using a deterministic date score.

    Args:
        policy: Validated daily incident policy.
        dt: Business date being generated.

    Returns:
        Tuple of selected scenario name and stable policy score.
    """
    score      = stable_policy_score(dt=dt, seed_salt=policy.seed_salt)
    cumulative = 0.0

    for scenario_name, probability in policy.normalized_weights.items():
        cumulative += probability

        if score < cumulative:
            logger.info(
                "Daily incident scenario selected | dt=%s scenario=%s score=%.6f cumulative=%.6f",
                dt,
                scenario_name,
                score,
                cumulative,
            )

            return scenario_name, score

    # Floating point edge case: fall back to the last configured positive-weight scenario.
    fallback = next(reversed(policy.normalized_weights))
    logger.info("Daily incident scenario fallback selected | dt=%s scenario=%s score=%.6f", dt, fallback, score)

    return fallback, score


def resolve_incident_scenario_for_date(
    dt: date,
    requested_scenario: str | None = "baseline",
    policy_path: str | Path | None = None,
) -> IncidentResolution:
    """
    Resolve requested scenario into the actual scenario for a business date.

    Args:
        dt: Business date being generated.
        requested_scenario: Scenario requested by Airflow/CLI. "auto" activates policy selection.
        policy_path: Optional daily policy YAML path.

    Returns:
        IncidentResolution describing the final scenario and why it was selected.
    """
    requested = (requested_scenario or "baseline").strip() or "baseline"

    if not is_auto_incident_scenario(requested):
        selected_incidents = resolve_incident_names(requested)

        logger.info("Using explicit incident scenario | dt=%s scenario=%s", dt, requested)

        return IncidentResolution(
            dt=dt,
            requested_scenario=requested,
            resolved_scenario=requested,
            selected_incidents=selected_incidents,
            policy_mode="explicit",
            policy_enabled=False,
            policy_path=str(policy_path or ""),
            reason="explicit_scenario_requested",
        )

    policy               = load_daily_incident_policy(policy_path)
    resolved_scenario    = policy.default_scenario
    policy_score         = None
    reason               = "policy_disabled"

    if policy.enabled and dt not in set(policy.protected_dates):
        resolved_scenario, policy_score = choose_weighted_scenario(policy=policy, dt=dt)
        reason                          = "deterministic_random_selection"

    elif dt in set(policy.protected_dates):
        reason = "protected_date"

    selected_incidents = resolve_incident_names(resolved_scenario)

    resolution = IncidentResolution(
        dt=dt,
        requested_scenario=requested,
        resolved_scenario=resolved_scenario,
        selected_incidents=selected_incidents,
        policy_mode=policy.mode,
        policy_enabled=policy.enabled,
        policy_path=str(Path(policy_path) if policy_path else DEFAULT_DAILY_INCIDENT_POLICY_PATH),
        policy_score=policy_score,
        reason=reason,
    )

    logger.info("Incident scenario resolved | resolution=%s", resolution.model_dump(mode="json"))

    return resolution


def build_ground_truth_key(dt: date, dataset: str = "orders") -> str:
    """
    Build the S3 key for one incident ground-truth artifact.

    Args:
        dt: Business date represented by the artifact.
        dataset: Dataset name.

    Returns:
        S3 object key under dq-artifacts.
    """
    return f"ground-truth/{dataset}/dt={dt.isoformat()}/incident_resolution.json"


def write_incident_ground_truth_to_s3(
    resolution: IncidentResolution,
    generated_summary: dict[str, Any],
    uploaded_summary: dict[str, Any] | None,
    endpoint_url: str | None = None,
    bucket: str | None = None,
) -> str:
    """
    Write daily incident ground truth to the artifacts bucket.

    Args:
        resolution: Resolved incident scenario metadata.
        generated_summary: Summary returned by generate_and_write_orders.
        uploaded_summary: Summary returned by upload_orders_partition, if upload was enabled.
        endpoint_url: Optional S3 endpoint URL override.
        bucket: Optional artifacts bucket override.

    Returns:
        S3 URI of the written ground-truth JSON artifact.
    """
    from pipelines.seeding.upload_to_s3 import build_s3_client

    target_bucket = bucket or os.getenv("ARTIFACTS_BUCKET", DEFAULT_ARTIFACTS_BUCKET)
    object_key    = build_ground_truth_key(dt=resolution.dt)

    payload = {
        "dataset": "orders",
        "dt": resolution.dt.isoformat(),
        "resolution": resolution.model_dump(mode="json"),
        "generated": {
            "rows": generated_summary.get("rows"),
            "local_path": generated_summary.get("local_path"),
            "incident_results": generated_summary.get("incident_results", []),
        },
        "landing": uploaded_summary or {},
    }

    client = build_s3_client(endpoint_url)

    # Ground truth is for evaluation/audit only; the triage agent should not use it as evidence.
    client.put_object(
        Bucket=target_bucket,
        Key=object_key,
        Body=json.dumps(payload, indent=2, ensure_ascii=True, default=str).encode("utf-8"),
        ContentType="application/json",
    )

    s3_uri = f"s3://{target_bucket}/{object_key}"
    logger.info("Incident ground truth written | uri=%s", s3_uri)

    return s3_uri


def resolve_preview_dates(
    dt: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> list[date]:
    """
    Resolve CLI date arguments for incident policy previews.

    Args:
        dt: Optional single date in YYYY-MM-DD format.
        start: Optional inclusive preview start date.
        end: Optional inclusive preview end date.

    Returns:
        List of dates to preview.

    Raises:
        ValueError: If date arguments are missing or invalid.
    """
    if dt and (start or end):
        raise ValueError("Use either --dt or --start/--end, not both.")

    if dt:
        return [parse_date(dt)]

    if not start or not end:
        raise ValueError("Provide --dt or both --start and --end.")

    return iter_dates(start_date=parse_date(start), end_date=parse_date(end))


def build_parser() -> argparse.ArgumentParser:
    """
    Build the command-line parser for daily incident policy previews.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(description="Preview deterministic daily incident scenario selection.")

    parser.add_argument("--dt", default=None, help="Single business date to preview, in YYYY-MM-DD format.")
    parser.add_argument("--start", default=None, help="Inclusive preview start date, in YYYY-MM-DD format.")
    parser.add_argument("--end", default=None, help="Inclusive preview end date, in YYYY-MM-DD format.")
    parser.add_argument("--policy", default=None, help="Optional daily_policy.yml path override.")
    parser.add_argument("--incident-scenario", default="auto", help="Requested scenario to resolve. Defaults to auto.")

    return parser


def main() -> None:
    """
    Parse CLI arguments and print deterministic incident policy preview rows.

    Returns:
        None.
    """
    parser = build_parser()
    args   = parser.parse_args()

    try:
        preview_dates = resolve_preview_dates(dt=args.dt, start=args.start, end=args.end)

    except ValueError as exc:
        parser.error(str(exc))

    resolutions = [
        resolve_incident_scenario_for_date(
            dt=preview_dt,
            requested_scenario=args.incident_scenario,
            policy_path=args.policy,
        ).model_dump(mode="json")
        for preview_dt in preview_dates
    ]

    print(json.dumps(resolutions, indent=2, ensure_ascii=True, default=str))


# --- Running CLI Entrypoint
if __name__ == "__main__":
    main()
