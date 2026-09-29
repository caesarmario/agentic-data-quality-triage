####
## Airflow Approved Action Trigger Helper for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import argparse
import json
import re
import subprocess
from datetime import datetime, timezone
from typing import Sequence


# --- Defining Constants
APPROVED_ACTION_DAG_ID = "90_02_dag_dq_platform_approved_actions"
SAFE_REQUEST_ID = re.compile(r"^APR-[A-F0-9]{20}$")
SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9_.-]+$")


# --- Building Airflow Commands
def validate_request_id(request_id: str) -> str:
    """Validate one transactional approval request identifier."""
    normalized = request_id.strip().upper()
    if not SAFE_REQUEST_ID.fullmatch(normalized):
        raise ValueError("Approval request ID must match APR- followed by 20 hexadecimal characters.")
    return normalized


def build_run_id(request_id: str, dry_run: bool, now: datetime | None = None) -> str:
    """Build one unique Airflow run identifier for preview or execution."""
    normalized = validate_request_id(request_id).lower()
    mode = "preview" if dry_run else "execute"
    current = now or datetime.now(timezone.utc)
    return f"manual__approved_action_{mode}_{normalized}_{current.strftime('%Y%m%dT%H%M%S%f')}"


def build_trigger_command(request_id: str, run_id: str, dry_run: bool) -> list[str]:
    """Build an Airflow CLI command without shell interpolation."""
    normalized_request = validate_request_id(request_id)
    normalized_run = run_id.strip()
    if not SAFE_RUN_ID.fullmatch(normalized_run):
        raise ValueError("Airflow run ID contains unsupported characters.")
    conf = json.dumps(
        {"approval_request_id": normalized_request, "dry_run": bool(dry_run)},
        separators=(",", ":"),
    )
    return [
        "airflow",
        "dags",
        "trigger",
        "-r",
        normalized_run,
        "-c",
        conf,
        "-o",
        "table",
        APPROVED_ACTION_DAG_ID,
    ]


def trigger_approved_action(request_id: str, *, dry_run: bool = True, run_id: str = "") -> str:
    """Unpause and trigger one approval action preview or execution."""
    normalized_request = validate_request_id(request_id)
    resolved_run = run_id.strip() or build_run_id(normalized_request, dry_run)
    subprocess.run(["airflow", "dags", "unpause", APPROVED_ACTION_DAG_ID], check=True)
    subprocess.run(build_trigger_command(normalized_request, resolved_run, dry_run), check=True)
    print(f"APPROVED_ACTION_DAG_ID={APPROVED_ACTION_DAG_ID}")
    print(f"APPROVED_ACTION_RUN_ID={resolved_run}")
    print(f"APPROVED_ACTION_REQUEST_ID={normalized_request}")
    print(f"APPROVED_ACTION_DRY_RUN={str(dry_run).lower()}")
    return resolved_run


# --- Defining CLI Entrypoint
def build_parser() -> argparse.ArgumentParser:
    """Build the bounded trigger parser."""
    parser = argparse.ArgumentParser(description="Trigger the approved action Airflow DAG.")
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--execute", action="store_true", help="Execute instead of the default preview.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and trigger one DagRun."""
    args = build_parser().parse_args(argv)
    trigger_approved_action(args.request_id, dry_run=not args.execute, run_id=args.run_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
