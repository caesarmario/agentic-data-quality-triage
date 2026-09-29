####
## Transactional Approval Action CLI for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


# --- Configuring Project Path
LOCAL_PROJECT_ROOT = Path(__file__).resolve().parents[1]
AIRFLOW_PROJECT_ROOT = Path(os.getenv("DQ_PROJECT_ROOT", "/opt/airflow/project"))
PROJECT_ROOT = (
    AIRFLOW_PROJECT_ROOT if AIRFLOW_PROJECT_ROOT.is_dir() else LOCAL_PROJECT_ROOT
)

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.tools.approved_actions import (
    ApprovedActionType,
    action_readiness,
    build_approval_control_store,
    create_action_request,
)


# --- Parsing Bounded Input
def parse_scope_json(value: str) -> dict[str, Any]:
    """Parse one JSON object; action-specific validation runs before persistence."""
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError("--scope-json must be valid JSON.") from exc
    if not isinstance(payload, dict):
        raise argparse.ArgumentTypeError("--scope-json must contain a JSON object.")
    return payload


def record_payload(record: Any) -> dict[str, Any]:
    """Serialize one non-sensitive control-store record for operator output."""
    return {
        "request_id": record.request_id,
        "request_generation": record.request_generation,
        "action_type": record.action_type,
        "scope": record.scope,
        "requested_by": record.requested_by,
        "reason": record.reason,
        "status": record.status,
        "version": record.version,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
        "decided_by": record.decided_by or "",
        "decided_at": record.decided_at.isoformat() if record.decided_at else "",
        "execution_run_id": record.execution_run_id or "",
        "execution_status": record.execution_status,
        "execution_error": record.execution_error,
    }


# --- Building CLI Commands
def build_parser() -> argparse.ArgumentParser:
    """Build the allowlisted transactional approval CLI."""
    parser = argparse.ArgumentParser(description="Manage approval-gated action requests.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser("create", help="Create or reuse an exact-scope request.")
    create_parser.add_argument("--action", choices=[item.value for item in ApprovedActionType], required=True)
    create_parser.add_argument("--scope-json", type=parse_scope_json, required=True)
    create_parser.add_argument("--requested-by", required=True)
    create_parser.add_argument("--reason", required=True)
    create_parser.add_argument(
        "--generation",
        type=int,
        default=1,
        help="Explicit request generation; increment only for an intentional repeat of the same scope.",
    )

    decide_parser = subparsers.add_parser("decide", help="Approve or reject a pending request.")
    decide_parser.add_argument("--request-id", required=True)
    decide_parser.add_argument("--decision", choices=["approve", "reject"], required=True)
    decide_parser.add_argument("--decided-by", required=True)
    decide_parser.add_argument("--expected-version", type=int, required=True)
    decide_parser.add_argument("--comment", default="")

    cancel_parser = subparsers.add_parser("cancel", help="Cancel a pre-execution request.")
    cancel_parser.add_argument("--request-id", required=True)
    cancel_parser.add_argument("--cancelled-by", required=True)
    cancel_parser.add_argument("--expected-version", type=int, required=True)

    get_parser = subparsers.add_parser("get", help="Read one request and provider readiness.")
    get_parser.add_argument("--request-id", required=True)
    return parser


# --- Executing CLI Commands
def run(args: argparse.Namespace) -> dict[str, Any]:
    """Execute one validated command against the transactional store."""
    store = build_approval_control_store()

    if args.command == "create":
        record, created = create_action_request(
            action_type=args.action,
            scope=args.scope_json,
            requested_by=args.requested_by,
            reason=args.reason,
            store=store,
            request_generation=args.generation,
        )
        return {"created_new": created, "request": record_payload(record)}

    if args.command == "decide":
        record = store.decide(
            args.request_id,
            decision=args.decision,
            decided_by=args.decided_by,
            expected_version=args.expected_version,
            comment=args.comment,
        )
        return {"state_changed": True, "request": record_payload(record)}

    if args.command == "cancel":
        record = store.cancel(
            args.request_id,
            cancelled_by=args.cancelled_by,
            expected_version=args.expected_version,
        )
        return {"state_changed": True, "request": record_payload(record)}

    record = store.read_latest(args.request_id)
    if record is None:
        raise LookupError(f"Approval request was not found: {args.request_id}")
    return {"request": record_payload(record), "readiness": action_readiness(record)}


def main() -> int:
    """Run the transactional approval CLI and print sanitized JSON."""
    args = build_parser().parse_args()
    print(json.dumps(run(args), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
