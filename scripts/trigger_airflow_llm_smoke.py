####
## Airflow LLM Provider Smoke Trigger Helper for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipelines.common.logging import logger


# --- Defining Constants
LLM_SMOKE_DAG_ID = "92_dag_dq_llm_provider_smoke"
EXTERNAL_SMOKE_ROUTE_NAMES = ("cheap_summary",)
SAFE_RUN_ID                = re.compile(r"^[A-Za-z0-9_.-]+$")


# --- Defining Functions
def validate_route_name(route_name: str) -> str:
    """
    Validate and normalize an LLM smoke route name.

    Args:
        route_name: Raw route name supplied by the operator.

    Returns:
        Normalized route name from the project allowlist.

    Raises:
        ValueError: If the route is not supported by the smoke DAG.
    """
    normalized = route_name.strip().lower()

    if normalized not in EXTERNAL_SMOKE_ROUTE_NAMES:
        raise ValueError(f"Unknown LLM smoke route: {route_name}")

    return normalized


def build_llm_smoke_run_id(
    route_name: str,
    now: datetime | None = None,
) -> str:
    """
    Build a unique and shell-safe Airflow run id for one provider smoke.

    Args:
        route_name: Allowlisted model route name.
        now: Optional UTC timestamp used by tests.

    Returns:
        Readable Airflow run id containing the route and timestamp.
    """
    normalized = validate_route_name(route_name)
    current    = now or datetime.now(timezone.utc)

    return f"manual__llm_smoke_{normalized}_{current.strftime('%Y%m%dT%H%M%S%f')}"


def build_trigger_command(
    route_name: str,
    run_id: str,
    require_provider: bool = False,
) -> list[str]:
    """
    Build an Airflow CLI trigger command without shell interpolation.

    Args:
        route_name: Allowlisted model route name.
        run_id: Explicit Airflow run id.
        require_provider: Explicit strict external-provider opt-in. When false,
            the selected task remains heuristic-only and zero-cost.

    Returns:
        Subprocess argument list containing one valid JSON conf argument.
    """
    normalized = validate_route_name(route_name)
    conf        = json.dumps(
        {
            "route_name": normalized,
            "run_external_provider": bool(require_provider),
        },
        separators=(",", ":"),
    )

    return [
        "airflow",
        "dags",
        "trigger",
        "-r",
        run_id,
        "-c",
        conf,
        "-o",
        "table",
        LLM_SMOKE_DAG_ID,
    ]


def run_command(command: list[str]) -> None:
    """
    Run one Airflow control command and stream its output.

    Args:
        command: Subprocess argument list.

    Returns:
        None.

    Raises:
        CalledProcessError: If the Airflow CLI command fails.
    """
    logger.info("Running Airflow LLM smoke control command | command=%s", command)
    subprocess.run(command, check=True)


def trigger_llm_smoke(
    route_name: str,
    require_provider: bool = False,
    run_id: str = "",
) -> str:
    """
    Unpause and trigger the manual LLM provider smoke DAG.

    Args:
        route_name: Allowlisted model route name.
        require_provider: Explicit strict external-provider opt-in. When false,
            the DagRun exercises only the zero-cost heuristic path.
        run_id: Optional explicit Airflow run id for audit correlation.

    Returns:
        Run id created for the LLM smoke DagRun.
    """
    normalized      = validate_route_name(route_name)
    resolved_run_id = run_id.strip() or build_llm_smoke_run_id(normalized)

    if not SAFE_RUN_ID.fullmatch(resolved_run_id):
        raise ValueError("Airflow run ID contains unsupported characters.")

    run_command(["airflow", "dags", "unpause", LLM_SMOKE_DAG_ID])
    run_command(
        build_trigger_command(
            route_name=normalized,
            run_id=resolved_run_id,
            require_provider=require_provider,
        )
    )

    print(f"LLM_SMOKE_DAG_ID={LLM_SMOKE_DAG_ID}")
    print(f"LLM_SMOKE_RUN_ID={resolved_run_id}")
    print(f"LLM_SMOKE_ROUTE={normalized}")
    print(f"LLM_SMOKE_EXTERNAL_PROVIDER={str(require_provider).lower()}")

    return resolved_run_id


# --- Defining CLI Helpers
def build_parser() -> argparse.ArgumentParser:
    """
    Build the Airflow LLM provider smoke trigger parser.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(description="Trigger the manual Airflow LLM provider smoke DAG.")

    parser.add_argument("--route", default="cheap_summary", choices=EXTERNAL_SMOKE_ROUTE_NAMES)
    parser.add_argument("--run-id", default="", help="Optional explicit run id for audit lookup.")
    parser.add_argument(
        "--require-provider",
        action="store_true",
        help="Explicitly run one strict external provider call; omitted means zero-cost heuristic mode.",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """
    Parse arguments and trigger one provider smoke DagRun.

    Args:
        argv: Optional argument sequence used by tests.

    Returns:
        Zero when the Airflow trigger succeeds.
    """
    args = build_parser().parse_args(argv)
    trigger_llm_smoke(
        route_name=args.route,
        require_provider=args.require_provider,
        run_id=args.run_id,
    )

    return 0


# --- Running CLI Entrypoint
if __name__ == "__main__":
    raise SystemExit(main())
