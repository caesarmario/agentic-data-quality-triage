####
## Isolated Multi-Agent Worker Runner for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Execute one authorized specialist task inside a cancellable child process."""

# --- Importing Libraries
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Sequence


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.llm.config import external_llm_permission_scope
from agent.specialists.contracts import SupervisorState
from agent.specialists.registry import enforce_task_capability
from agent.supervisor.budgets import supervisor_llm_budget_scope
from agent.supervisor.execution_plan import PlannedAgentTask
from agent.supervisor.models import SupervisorRequest, SupervisorRoute
from agent.supervisor.runtime import (
    SupervisorRuntimeConfig,
    invoke_specialist_with_resilience,
    reconcile_specialist_result_usage,
)
from pipelines.common.logging import logger


# --- Defining Constants
MAX_WORKER_INPUT_BYTES = 128_000
MAX_WORKER_OUTPUT_BYTES = 512_000


# --- Defining File Helpers
def validate_contract_paths(input_path: str, output_path: str) -> tuple[Path, Path]:
    """
    Validate absolute sibling paths used for one internal worker contract.

    Args:
        input_path: JSON input path created by the parent fan-out runtime.
        output_path: JSON result path written atomically by this worker.

    Returns:
        Resolved input and output paths.

    Raises:
        ValueError: If paths are relative, unrelated, missing, or unsafe.
    """
    resolved_input  = Path(input_path).expanduser().resolve()
    resolved_output = Path(output_path).expanduser().resolve()

    if not Path(input_path).is_absolute() or not Path(output_path).is_absolute():
        raise ValueError("Agent worker contract paths must be absolute.")

    if resolved_input.parent != resolved_output.parent:
        raise ValueError("Agent worker input and output must share one temporary directory.")

    if not resolved_input.is_file():
        raise ValueError("Agent worker input contract does not exist.")

    if resolved_output.exists():
        raise ValueError("Agent worker output path must not already exist.")

    return resolved_input, resolved_output


def load_worker_contract(input_path: Path) -> tuple[SupervisorRequest, PlannedAgentTask]:
    """
    Load and validate one bounded parent request and worker task.

    Args:
        input_path: Existing JSON contract path.

    Returns:
        Validated SupervisorRequest and PlannedAgentTask.

    Raises:
        ValueError: If the file is oversized or contains an invalid contract.
    """
    if input_path.stat().st_size > MAX_WORKER_INPUT_BYTES:
        raise ValueError("Agent worker input contract exceeds the bounded file size.")

    payload = json.loads(input_path.read_text(encoding="utf-8"))

    if not isinstance(payload, dict) or set(payload) != {"request", "worker"}:
        raise ValueError("Agent worker input must contain only request and worker contracts.")

    request = SupervisorRequest.model_validate(payload["request"])
    worker  = PlannedAgentTask.model_validate(payload["worker"])

    enforce_task_capability(worker.task)

    return request, worker


def write_worker_result(output_path: Path, result_json: str) -> None:
    """
    Atomically persist a bounded specialist result for the parent process.

    Args:
        output_path: Final result path.
        result_json: Validated AgentResultEnvelope JSON.

    Returns:
        None.

    Raises:
        ValueError: If serialized output exceeds the worker boundary.
    """
    encoded = result_json.encode("utf-8")

    if len(encoded) > MAX_WORKER_OUTPUT_BYTES:
        raise ValueError("Agent worker output exceeds the bounded file size.")

    temporary_path = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary_path.write_bytes(encoded)
    os.replace(temporary_path, output_path)


# --- Defining Worker Runtime
def run_worker_contract(
    request: SupervisorRequest,
    worker: PlannedAgentTask,
) -> str:
    """
    Execute one specialist with its own deadline, retries, and model budget.

    Args:
        request: Parent supervisor request containing runtime permission policy.
        worker: Fully authorized planned specialist task.

    Returns:
        Validated AgentResultEnvelope JSON.
    """
    task    = worker.task
    runtime = SupervisorRuntimeConfig()
    client  = runtime.audit_client_factory(
        host=runtime.clickhouse_host,
        port=runtime.clickhouse_port,
    )
    route = SupervisorRoute(
        intent=worker.intent,
        specialist_name=task.specialist_name,
        task_type=task.task_type,
        rationale=worker.rationale or "Authorized fan-out worker task.",
    )
    state = SupervisorState(
        parent_run_id=task.parent_run_id,
        max_handoffs=1,
        max_retries=worker.retry_budget,
        max_model_calls=task.model_call_budget,
        token_budget=task.token_budget,
        estimated_cost_budget_usd=task.estimated_cost_budget_usd,
        latency_budget_ms=max(1_000, task.timeout_seconds * 1_000),
    )
    deadline = time.monotonic() + task.timeout_seconds

    try:
        with external_llm_permission_scope(request.allow_external_llm):
            with supervisor_llm_budget_scope(
                max_model_calls=task.model_call_budget,
                token_budget=task.token_budget,
                estimated_cost_budget_usd=task.estimated_cost_budget_usd,
                deadline_monotonic=deadline,
            ) as ledger:
                invocation = invoke_specialist_with_resilience(
                    task=task,
                    request=request,
                    route=route,
                    state=state,
                    config=runtime,
                    client=client,
                    parent_run_id=task.parent_run_id,
                    deadline_monotonic=deadline,
                )
                usage = ledger.snapshot(
                    latency_ms=max(0, task.timeout_seconds * 1_000 - int(
                        max(0.0, deadline - time.monotonic()) * 1_000
                    )),
                )

        result = reconcile_specialist_result_usage(
            result=invocation.result,
            llm_usage=usage,
        )

        logger.info(
            "Isolated agent worker completed | task_id=%s specialist=%s task_type=%s status=%s",
            task.task_id,
            task.specialist_name,
            task.task_type,
            result.status.value,
        )

        return result.model_dump_json(indent=2)

    finally:
        close = getattr(client, "close", None)

        if callable(close):
            close()


# --- Defining CLI Helpers
def build_parser() -> argparse.ArgumentParser:
    """
    Build the internal isolated-worker command parser.

    Returns:
        Configured parser accepting only contract file paths.
    """
    parser = argparse.ArgumentParser(description="Run one isolated bounded agent worker.")
    parser.add_argument("--input", required=True, help="Absolute parent-created contract path.")
    parser.add_argument("--output", required=True, help="Absolute atomic result path.")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """
    Load, execute, and persist one internal worker contract.

    Args:
        argv: Optional explicit argument sequence used by tests.

    Returns:
        Zero when the specialist returned a typed terminal result.
    """
    args = build_parser().parse_args(argv)
    input_path, output_path = validate_contract_paths(args.input, args.output)
    request, worker         = load_worker_contract(input_path)
    result_json             = run_worker_contract(request=request, worker=worker)
    write_worker_result(output_path=output_path, result_json=result_json)

    return 0


# --- Running CLI Entrypoint
if __name__ == "__main__":
    raise SystemExit(main())
