####
## Airflow Triage And Checkpoint Operator Trigger for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Trigger source triage, checkpoint inspection, or historical replay through Airflow."""

# --- Importing Libraries
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.checkpoint_operations import (
    DEFAULT_HISTORY_LIMIT,
    DEFAULT_HISTORY_NODE,
    TRIAGE_ACTIONS,
    TRIAGE_DAG_ID,
    build_trigger_conf,
    clean_optional,
    validate_alert_identity,
    validate_triage_inputs,
)
from pipelines.common.logging import logger


# --- Defining Constants
SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,199}$")


# --- Defining Validation Helpers
def validate_airflow_run_id(run_id: str) -> str:
    """
    Validate one operator-supplied Airflow DagRun identifier.

    Args:
        run_id: Raw DagRun identifier.

    Returns:
        Validated DagRun identifier.

    Raises:
        ValueError: If the identifier contains unsupported characters.
    """
    normalized = run_id.strip()

    if not SAFE_RUN_ID.fullmatch(normalized):
        raise ValueError("Airflow run ID contains unsupported characters.")

    return normalized


# --- Defining Airflow Trigger Helpers
def build_triage_run_id(action: str, now: datetime | None = None) -> str:
    """
    Build a unique and audit-friendly triage DagRun identifier.

    Args:
        action: Requested triage operator action.
        now: Optional UTC timestamp used by tests.

    Returns:
        Validated Airflow DagRun identifier.
    """
    normalized_action = action.strip().lower()

    if normalized_action not in TRIAGE_ACTIONS:
        raise ValueError(f"Unsupported triage action: {action}")

    current = now or datetime.now(timezone.utc)
    run_id  = f"manual__triage_{normalized_action}_{current.strftime('%Y%m%dT%H%M%S%f')}"

    return validate_airflow_run_id(run_id)


def build_trigger_command(request: dict[str, Any], run_id: str) -> list[str]:
    """
    Build an Airflow CLI command without shell interpolation.

    Args:
        request: Normalized and validated triage request.
        run_id: Explicit Airflow DagRun identifier.

    Returns:
        Subprocess argument list containing compact JSON configuration.
    """
    normalized_request = validate_triage_inputs(
        action=str(request.get("action", "")),
        alert_id=str(request.get("alert_id", "")),
        alert_key=str(request.get("alert_key", "")),
        checkpoint_namespace=str(request.get("checkpoint_namespace", "")),
        checkpoint_id=str(request.get("checkpoint_id", "")),
        replay_request_id=str(request.get("replay_request_id", "")),
        history_limit=int(request.get("history_limit", DEFAULT_HISTORY_LIMIT)),
        history_next_node=str(request.get("history_next_node", DEFAULT_HISTORY_NODE)),
    )
    normalized_run_id = validate_airflow_run_id(run_id)
    conf              = json.dumps(build_trigger_conf(normalized_request), separators=(",", ":"))

    return [
        "airflow",
        "dags",
        "trigger",
        "-r",
        normalized_run_id,
        "-c",
        conf,
        "-o",
        "table",
        TRIAGE_DAG_ID,
    ]


def run_command(command: list[str]) -> None:
    """
    Run one Airflow CLI command and stream its output.

    Args:
        command: Subprocess argument list.

    Returns:
        None.

    Raises:
        CalledProcessError: If the Airflow command fails.
    """
    logger.info("Running Airflow triage control command | command=%s", command)
    subprocess.run(command, check=True)


def trigger_airflow_triage(
    action: str,
    alert_id: str = "",
    alert_key: str = "",
    checkpoint_namespace: str = "",
    checkpoint_id: str = "",
    replay_request_id: str = "",
    history_limit: int = DEFAULT_HISTORY_LIMIT,
    history_next_node: str = DEFAULT_HISTORY_NODE,
    run_id: str = "",
) -> tuple[str, str]:
    """
    Unpause and trigger one checkpoint-aware operational triage DagRun.

    Args:
        action: Source triage, read-only inspect, or historical replay.
        alert_id: Optional ClickHouse alert UUID.
        alert_key: Optional stable system alert key.
        checkpoint_namespace: Shared namespace across source, inspect, and replay.
        checkpoint_id: Exact selected checkpoint for replay.
        replay_request_id: Stable replay idempotency key.
        history_limit: Maximum checkpoint rows for inspect.
        history_next_node: Pending graph node selected during inspect.
        run_id: Optional explicit Airflow DagRun identifier.

    Returns:
        Tuple containing the Airflow run ID and resolved checkpoint namespace.
    """
    resolved_run_id = validate_airflow_run_id(run_id) if run_id.strip() else build_triage_run_id(action)
    resolved_namespace = checkpoint_namespace.strip()

    if not resolved_namespace:
        if action.strip().lower() != "triage":
            raise ValueError("checkpoint_namespace is required for inspect and replay actions.")

        # A new source run receives a unique namespace that can be reused by later operator actions.
        resolved_namespace = resolved_run_id

    request = validate_triage_inputs(
        action=action,
        alert_id=alert_id,
        alert_key=alert_key,
        checkpoint_namespace=resolved_namespace,
        checkpoint_id=checkpoint_id,
        replay_request_id=replay_request_id,
        history_limit=history_limit,
        history_next_node=history_next_node,
    )

    run_command(["airflow", "dags", "unpause", TRIAGE_DAG_ID])
    run_command(build_trigger_command(request=request, run_id=resolved_run_id))

    print(f"TRIAGE_DAG_ID={TRIAGE_DAG_ID}")
    print(f"TRIAGE_RUN_ID={resolved_run_id}")
    print(f"TRIAGE_ACTION={request['action']}")
    print(f"TRIAGE_CHECKPOINT_NAMESPACE={request['checkpoint_namespace']}")

    return resolved_run_id, request["checkpoint_namespace"]


# --- Defining CLI Helpers
def build_parser() -> argparse.ArgumentParser:
    """
    Build the Airflow triage operator parser.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(description="Trigger checkpoint-aware triage operations through Airflow.")

    parser.add_argument("--action", choices=TRIAGE_ACTIONS, default="triage")
    parser.add_argument("--alert-id", default="")
    parser.add_argument("--alert-key", default="")
    parser.add_argument("--checkpoint-namespace", default="")
    parser.add_argument("--checkpoint-id", default="")
    parser.add_argument("--replay-request-id", default="")
    parser.add_argument("--history-limit", type=int, default=DEFAULT_HISTORY_LIMIT)
    parser.add_argument("--history-next-node", default=DEFAULT_HISTORY_NODE)
    parser.add_argument("--run-id", default="")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """
    Parse arguments and trigger the requested Airflow triage operation.

    Args:
        argv: Optional argument sequence used by contract tests.

    Returns:
        Zero when the Airflow trigger succeeds.
    """
    args = build_parser().parse_args(argv)
    trigger_airflow_triage(
        action=args.action,
        alert_id=args.alert_id,
        alert_key=args.alert_key,
        checkpoint_namespace=args.checkpoint_namespace,
        checkpoint_id=args.checkpoint_id,
        replay_request_id=args.replay_request_id,
        history_limit=args.history_limit,
        history_next_node=args.history_next_node,
        run_id=args.run_id,
    )

    return 0


# --- Running CLI Entrypoint
if __name__ == "__main__":
    raise SystemExit(main())
