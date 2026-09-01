####
## Schema Drift CLI for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Run or verify deterministic schema drift detection through ClickHouse."""

# --- Importing Libraries
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[2]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipelines.common.clickhouse import build_clickhouse_client
from pipelines.common.logging import logger
from pipelines.schema_drift.config import SCHEMA_CONTRACT_NAMES, load_named_schema_contract
from pipelines.schema_drift.detector import evaluate_schema_contract
from pipelines.schema_drift.storage import (
    ensure_schema_drift_tables,
    persist_schema_evaluation,
    verify_persisted_schema_evaluation,
)


# --- Defining Constants
SCHEMA_GATE_MODES = ("enforce", "observe")
SEVERITY_GATE_ERROR_PREFIX = "schema drift severity gate crossed:"


# --- Defining CLI Functions
def build_parser() -> argparse.ArgumentParser:
    """
    Build the bounded schema drift command parser.

    Returns:
        Configured parser with allowlisted contract and mode values.
    """
    parser = argparse.ArgumentParser(description="Detect or verify ClickHouse schema drift.")
    parser.add_argument("--contract", default="orders", choices=SCHEMA_CONTRACT_NAMES)
    parser.add_argument("--mode", default="detect", choices=("detect", "verify"))
    parser.add_argument(
        "--gate-mode",
        default="enforce",
        choices=SCHEMA_GATE_MODES,
        help="enforce fails on policy severity; observe persists evidence without blocking alert generation.",
    )
    parser.add_argument("--run-id", required=True, help="Airflow run identifier used for audit correlation.")

    return parser


def evaluate_gate_success(
    gate_mode: str,
    gate_crossed: bool,
    blocking_errors: Sequence[str] = (),
) -> bool:
    """
    Resolve operational success without hiding evidence or infrastructure errors.

    Args:
        gate_mode: enforce for a strict policy gate or observe for daily alerting.
        gate_crossed: Whether deterministic findings crossed the configured severity.
        blocking_errors: Persistence or verification errors unrelated to the severity gate.

    Returns:
        True when the selected mode may continue safely.

    Raises:
        ValueError: If gate_mode is unsupported.
    """
    if gate_mode not in SCHEMA_GATE_MODES:
        raise ValueError(f"Unsupported schema gate mode: {gate_mode}")

    if blocking_errors:
        return False

    return gate_mode == "observe" or not gate_crossed


def run_schema_drift_command(
    contract_name: str,
    mode: str,
    run_id: str,
    gate_mode: str = "enforce",
) -> tuple[dict[str, Any], bool]:
    """
    Execute one allowlisted schema drift operation.

    Args:
        contract_name: Allowlisted schema contract alias.
        mode: detect to capture/persist or verify to inspect persisted evidence.
        run_id: Airflow or CLI correlation identifier.
        gate_mode: enforce to fail on critical drift, or observe to continue after evidence persistence.

    Returns:
        Tuple containing JSON-safe payload and operational success state.

    Raises:
        ValueError: If mode is unsupported.
    """
    contract, source_path = load_named_schema_contract(contract_name)
    client                = build_clickhouse_client()

    if gate_mode not in SCHEMA_GATE_MODES:
        raise ValueError(f"Unsupported schema gate mode: {gate_mode}")

    try:
        ensure_schema_drift_tables(client)

        if mode == "detect":
            evaluation = evaluate_schema_contract(client=client, contract=contract, run_id=run_id)
            writes     = persist_schema_evaluation(client=client, evaluation=evaluation)
            payload: dict[str, Any] = {
                "mode": mode,
                "gate_mode": gate_mode,
                "gate_crossed": evaluation.should_fail,
                "contract_alias": contract_name,
                "contract_source": source_path.relative_to(PROJECT_ROOT).as_posix(),
                "evaluation": evaluation.as_dict(),
                "writes": writes,
            }
            succeeded = evaluate_gate_success(
                gate_mode=gate_mode,
                gate_crossed=evaluation.should_fail,
            )
        elif mode == "verify":
            verification = verify_persisted_schema_evaluation(
                client=client,
                contract=contract,
                run_id=run_id,
            )
            payload = {
                "mode": mode,
                "gate_mode": gate_mode,
                "gate_crossed": verification.gate_failure_count > 0,
                "contract_alias": contract_name,
                "contract_source": source_path.relative_to(PROJECT_ROOT).as_posix(),
                "verification": verification.as_dict(),
            }
            blocking_errors = tuple(
                error
                for error in verification.errors
                if not error.startswith(SEVERITY_GATE_ERROR_PREFIX)
            )
            succeeded = evaluate_gate_success(
                gate_mode=gate_mode,
                gate_crossed=verification.gate_failure_count > 0,
                blocking_errors=blocking_errors,
            )
        else:
            raise ValueError(f"Unsupported schema drift mode: {mode}")

        return payload, succeeded
    finally:
        close = getattr(client, "close", None)

        if callable(close):
            close()


def main(argv: Sequence[str] | None = None) -> int:
    """
    Parse CLI arguments and emit Airflow-friendly JSON evidence.

    Args:
        argv: Optional argument sequence used by tests.

    Returns:
        Zero for a passing gate, otherwise one.
    """
    args               = build_parser().parse_args(argv)
    payload, succeeded = run_schema_drift_command(
        contract_name=args.contract,
        mode=args.mode,
        run_id=args.run_id,
        gate_mode=args.gate_mode,
    )

    logger.info(
        "Schema drift command completed | contract=%s mode=%s gate_mode=%s run_id=%s success=%s",
        args.contract,
        args.mode,
        args.gate_mode,
        args.run_id,
        succeeded,
    )
    print(json.dumps(payload, indent=2, sort_keys=True))

    return 0 if succeeded else 1


# --- Running CLI Entrypoint
if __name__ == "__main__":
    raise SystemExit(main())
