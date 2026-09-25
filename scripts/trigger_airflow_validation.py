####
## Airflow Validation Trigger Helper for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dags.dq_platform.validation import VALIDATION_SUITE_NAMES
from pipelines.common.logging import logger
from scripts.web_operator_readiness import validate_web_operator_request_id


# --- Defining Constants
VALIDATION_DAG_ID = "91_dag_dq_platform_validation"


# --- Defining Functions
def build_validation_run_id(suite: str, now: datetime | None = None) -> str:
    """
    Build a unique, readable run id for an Airflow validation run.

    Args:
        suite: Allowlisted validation suite name.
        now: Optional UTC timestamp used by tests.

    Returns:
        Airflow run id without shell-sensitive characters.
    """
    current = now or datetime.now(timezone.utc)

    return f"manual__validation_{suite}_{current.strftime('%Y%m%dT%H%M%S%f')}"


def validate_suite(suite: str) -> str:
    """
    Validate and normalize a requested suite name.

    Args:
        suite: Raw suite value from CLI.

    Returns:
        Normalized allowlisted suite name.

    Raises:
        ValueError: If the suite is not supported.
    """
    normalized = suite.strip().lower()

    if normalized not in VALIDATION_SUITE_NAMES:
        raise ValueError(f"Unknown validation suite: {suite}")

    return normalized


def build_trigger_command(
    suite: str,
    run_id: str,
    require_api: bool = False,
    require_web: bool = False,
    require_web_report: bool = False,
    web_operator_request_id: str = "",
) -> list[str]:
    """
    Build an Airflow CLI trigger command without shell interpolation.

    Args:
        suite: Allowlisted validation suite.
        run_id: Explicit Airflow run id.
        require_api: Whether platform readiness must require the optional API profile.
        require_web: Whether readiness must require the optional web profile.
        require_web_report: Whether a real persisted report must match server HTML.

    Returns:
        Subprocess argument list containing valid JSON conf.
    """
    normalized = validate_suite(suite)
    conf_payload: dict[str, object] = {"validation_suite": normalized}

    if require_api:
        conf_payload["require_api"] = True
    if require_web:
        conf_payload["require_web"] = True
    if require_web_report:
        conf_payload["require_web_report"] = True
    if web_operator_request_id:
        conf_payload["web_operator_request_id"] = validate_web_operator_request_id(web_operator_request_id)
        conf_payload["require_web"] = True

    conf = json.dumps(conf_payload, separators=(",", ":"))

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
        VALIDATION_DAG_ID,
    ]


def run_command(command: list[str]) -> None:
    """
    Run one Airflow CLI command and stream output to the operator terminal.

    Args:
        command: Subprocess argument list.

    Returns:
        None.

    Raises:
        CalledProcessError: If the Airflow CLI command fails.
    """
    logger.info("Running Airflow validation control command | command=%s", command)
    subprocess.run(command, check=True)


def trigger_validation(
    suite: str,
    run_id: str = "",
    require_api: bool = False,
    require_web: bool = False,
    require_web_report: bool = False,
    web_operator_request_id: str = "",
) -> str:
    """
    Unpause and trigger the manual Airflow validation DAG.

    Args:
        suite: Allowlisted validation suite name.
        run_id: Optional explicit run id.
        require_api: Whether the readiness task must require FastAPI.
        require_web: Whether the readiness task must require the premium web UI.
        require_web_report: Whether the selected stored report must pass the HTML gate.

    Returns:
        Run id created for the validation DagRun.
    """
    normalized      = validate_suite(suite)
    resolved_run_id = run_id.strip() or build_validation_run_id(normalized)
    if web_operator_request_id:
        validate_web_operator_request_id(web_operator_request_id)

    run_command(["airflow", "dags", "unpause", VALIDATION_DAG_ID])
    run_command(
        build_trigger_command(
            normalized,
            resolved_run_id,
            require_api=require_api,
            require_web=require_web,
            require_web_report=require_web_report,
            web_operator_request_id=web_operator_request_id,
        )
    )

    print(f"VALIDATION_DAG_ID={VALIDATION_DAG_ID}")
    print(f"VALIDATION_RUN_ID={resolved_run_id}")
    print(f"VALIDATION_SUITE={normalized}")
    print(f"VALIDATION_REQUIRE_API={str(require_api).lower()}")
    print(f"VALIDATION_REQUIRE_WEB={str(require_web or bool(web_operator_request_id)).lower()}")
    print(f"VALIDATION_REQUIRE_WEB_REPORT={str(require_web_report).lower()}")
    print(f"VALIDATION_WEB_OPERATOR_REQUEST_ID={web_operator_request_id}")

    return resolved_run_id


# --- Defining CLI Helpers
def build_parser() -> argparse.ArgumentParser:
    """
    Build the Airflow validation trigger parser.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(description="Trigger the manual Airflow validation DAG.")

    parser.add_argument("--suite", default="all", choices=VALIDATION_SUITE_NAMES)
    parser.add_argument("--run-id", default="", help="Optional explicit run id for audit lookup.")
    parser.add_argument(
        "--require-api",
        action="store_true",
        help="Require the optional control-plane API during readiness validation.",
    )
    parser.add_argument(
        "--require-web",
        action="store_true",
        help="Require the optional premium web UI during readiness validation.",
    )

    parser.add_argument("--require-web-report", action="store_true", help="Require a real stored report to match selected server HTML.")
    parser.add_argument("--web-operator-request-id", default="", help="Verify one synthetic browser approval (implies web readiness).")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """
    Parse arguments and trigger validation.

    Args:
        argv: Optional argument sequence for tests.

    Returns:
        Zero when the trigger succeeds.
    """
    args = build_parser().parse_args(argv)
    trigger_validation(
        suite=args.suite,
        run_id=args.run_id,
        require_api=args.require_api,
        require_web=args.require_web,
        require_web_report=args.require_web_report,
        web_operator_request_id=args.web_operator_request_id,
    )

    return 0


# --- Running CLI Entrypoint
if __name__ == "__main__":
    raise SystemExit(main())
