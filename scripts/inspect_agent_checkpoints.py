####
## Agent Checkpoint History Inspector for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Inspect sanitized LangGraph checkpoint history through a read-only operator boundary."""

# --- Importing Libraries
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.checkpoint_inspection import (
    DEFAULT_REPLAY_NEXT_NODE,
    clean_optional,
    inspect_checkpoint_history,
    parse_bool_flag,
    resolve_checkpoint_correlation,
)
from agent.checkpointing import CHECKPOINT_MODE_OFF, MAX_CHECKPOINT_HISTORY
from agent.graph import DEFAULT_REPORT_PREFIX, TriageRuntimeConfig


# --- Defining Constants
DEFAULT_MANIFEST_S3_URI = "s3://dq-artifacts/dbt-artifacts/orders/latest/manifest.json"


# --- Defining CLI Helpers
def build_parser() -> argparse.ArgumentParser:
    """
    Build the checkpoint history inspector argument parser.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(description="Inspect sanitized agent checkpoint history.")

    parser.add_argument("--enabled", default="true")
    parser.add_argument("--alert-id", default=None)
    parser.add_argument("--alert-key", default=None)
    parser.add_argument("--checkpoint-mode", default=CHECKPOINT_MODE_OFF)
    parser.add_argument("--checkpoint-namespace", default=None)
    parser.add_argument("--checkpoint-sqlite-path", default=None)
    parser.add_argument("--checkpoint-busy-timeout-ms", type=int, default=None)
    parser.add_argument("--history-limit", type=int, default=50, choices=range(1, MAX_CHECKPOINT_HISTORY + 1))
    parser.add_argument("--select-next-node", default=DEFAULT_REPLAY_NEXT_NODE)
    parser.add_argument("--manifest-path", default=None)
    parser.add_argument("--manifest-s3-uri", default=DEFAULT_MANIFEST_S3_URI)
    parser.add_argument("--endpoint-url", default=None)
    parser.add_argument("--artifacts-bucket", default=None)
    parser.add_argument("--artifacts-prefix", default=DEFAULT_REPORT_PREFIX)
    parser.add_argument("--clickhouse-host", default=None)
    parser.add_argument("--clickhouse-port", type=int, default=None)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """
    Parse CLI arguments and print sanitized checkpoint evidence.

    Args:
        argv: Optional argument sequence used by contract tests.

    Returns:
        Zero when inspection or the disabled no-op succeeds.
    """
    parser = build_parser()
    args   = parser.parse_args(argv)
    config = TriageRuntimeConfig(
        manifest_path=clean_optional(args.manifest_path),
        manifest_s3_uri=clean_optional(args.manifest_s3_uri),
        s3_endpoint_url=clean_optional(args.endpoint_url),
        artifacts_bucket=clean_optional(args.artifacts_bucket),
        artifacts_prefix=args.artifacts_prefix,
        clickhouse_host=clean_optional(args.clickhouse_host),
        clickhouse_port=args.clickhouse_port,
    )

    try:
        result = inspect_checkpoint_history(
            enabled=args.enabled,
            alert_id=args.alert_id,
            alert_key=args.alert_key,
            checkpoint_mode=args.checkpoint_mode,
            checkpoint_namespace=args.checkpoint_namespace,
            checkpoint_sqlite_path=args.checkpoint_sqlite_path,
            checkpoint_busy_timeout_ms=args.checkpoint_busy_timeout_ms,
            history_limit=args.history_limit,
            select_next_node=args.select_next_node,
            runtime_config=config,
        )

    except ValueError as exc:
        parser.error(str(exc))

    print(json.dumps(result, indent=2, sort_keys=True))

    selected = result.get("selected_checkpoint") or {}

    if selected:
        print(f"CHECKPOINT_THREAD_ID={result['thread_id']}")
        print(f"CHECKPOINT_SELECTED_ID={selected['checkpoint_id']}")
        print(f"CHECKPOINT_SELECTED_NEXT_NODES={','.join(selected['next_nodes'])}")

    return 0


# --- Running CLI Entrypoint
if __name__ == "__main__":
    raise SystemExit(main())
