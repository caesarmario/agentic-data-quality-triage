####
## Metadata Registry CLI for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Synchronize or verify an allowlisted metadata registry through ClickHouse."""

# --- Importing Libraries
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[2]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipelines.common.clickhouse import build_clickhouse_client
from pipelines.common.logging import logger
from pipelines.metadata.config import METADATA_REGISTRY_NAMES, load_named_metadata_registry
from pipelines.metadata.registry import sync_metadata_registry, verify_metadata_registry


# --- Defining CLI Functions
def build_parser() -> argparse.ArgumentParser:
    """
    Build the bounded metadata registry command-line parser.

    Returns:
        Configured ArgumentParser with allowlisted registry and mode values.
    """
    parser = argparse.ArgumentParser(description="Synchronize trusted warehouse metadata into ClickHouse.")
    parser.add_argument("--registry", default="orders", choices=METADATA_REGISTRY_NAMES)
    parser.add_argument("--mode", default="sync", choices=("sync", "verify"))

    return parser


def run_metadata_command(registry_name: str, mode: str) -> dict[str, object]:
    """
    Execute one allowlisted metadata sync or verification command.

    Args:
        registry_name: Allowlisted metadata registry name.
        mode: `sync` to append required versions or `verify` for read-only comparison.

    Returns:
        JSON-serializable operational result.

    Raises:
        RuntimeError: If post-sync or explicit verification finds a mismatch.
        ValueError: If the mode is unsupported.
    """
    config, source_path = load_named_metadata_registry(registry_name)
    client              = build_clickhouse_client()

    try:
        if mode == "sync":
            plan         = sync_metadata_registry(client=client, config=config, source_path=source_path)
            verification = verify_metadata_registry(client=client, config=config, source_path=source_path)
            payload: dict[str, object] = {
                "mode": mode,
                "registry_name": config.registry_name,
                "dataset": config.dataset,
                "plan": plan.as_dict(),
                "verification": verification.as_dict(),
            }
        elif mode == "verify":
            verification = verify_metadata_registry(client=client, config=config, source_path=source_path)
            payload = {
                "mode": mode,
                "registry_name": config.registry_name,
                "dataset": config.dataset,
                "verification": verification.as_dict(),
            }
        else:
            raise ValueError(f"Unsupported metadata registry mode: {mode}")

        if verification.status != "pass":
            raise RuntimeError(
                "Metadata registry verification failed: "
                + "; ".join(verification.errors)
            )

        return payload
    finally:
        close = getattr(client, "close", None)

        if callable(close):
            close()


def main(argv: Sequence[str] | None = None) -> int:
    """
    Parse arguments, run the metadata operation, and print Airflow-friendly JSON.

    Args:
        argv: Optional CLI arguments used by tests.

    Returns:
        Zero when synchronization and verification succeed.
    """
    args    = build_parser().parse_args(argv)
    payload = run_metadata_command(registry_name=args.registry, mode=args.mode)

    logger.info(
        "Metadata registry command completed | registry=%s mode=%s",
        args.registry,
        args.mode,
    )
    print(json.dumps(payload, indent=2, sort_keys=True))

    return 0


# --- Running CLI Entrypoint
if __name__ == "__main__":
    raise SystemExit(main())
