####
## Bounded Evidence Worker Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Test the evidence worker adapter with deterministic tool and ledger doubles only."""

# --- Importing Libraries
from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from agent.specialists.contracts import ContextReference, ContextReferenceType
from agent.specialists.evidence_worker import (
    EVIDENCE_WORKER_COST_BUDGET_USD,
    EVIDENCE_WORKER_MODEL_CALL_BUDGET,
    EVIDENCE_WORKER_TIMEOUT_SECONDS,
    EVIDENCE_WORKER_TOKEN_BUDGET,
    EvidenceWorkerRuntimeConfig,
    build_evidence_worker_task,
    run_evidence_worker,
)
from agent.specialists.registry import required_tools_for_task


# --- Defining Test Doubles
class AuditRecorder:
    """Retain append-only audit calls without a ClickHouse dependency."""

    def __init__(self) -> None:
        """Initialize the audit event buffer."""
        self.events: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> UUID:
        """Record one audit event and return its supplied parent run identifier."""
        self.events.append(kwargs)
        return UUID(str(kwargs["agent_run_id"]))


class Ledger:
    """Expose the existing supervisor-ledger snapshot interface used by the adapter."""

    def __init__(self, model_calls: int = 0, tokens: int = 0, cost: float = 0.0) -> None:
        """Initialize deterministic accounting values."""
        self.model_calls = model_calls
        self.tokens = tokens
        self.cost = cost

    def snapshot(self, latency_ms: int) -> SimpleNamespace:
        """Return a supervisor-budget compatible usage snapshot."""
        del latency_ms
        return SimpleNamespace(
            model_calls=self.model_calls,
            tokens=self.tokens,
            estimated_cost_usd=self.cost,
        )


class InterpretationResult:
    """Provide a minimal validated-interpretation shape without a provider call."""

    def __init__(self) -> None:
        """Initialize fixed strict-Gemini execution metadata."""
        self.execution = SimpleNamespace(
            executed_route="cheap_summary",
            provider="Gemini",
            model="gemini-2.5-flash",
            input_tokens=80,
            output_tokens=40,
            estimated_cost_usd=0.0004,
            duration_ms=11,
            attempted_routes=["cheap_summary"],
        )

    def model_dump(self, mode: str = "python") -> dict[str, Any]:
        """Return a safe, compact interpretation payload for result serialization."""
        assert mode == "json"
        return {
            "purpose": "test_only",
            "input_evidence_ids": ["bounded-evidence"],
            "cited_evidence_ids": ["bounded-evidence"],
            "interpretation": {"summary": "A cited bounded interpretation was returned."},
            "execution": {
                "provider": self.execution.provider,
                "model": self.execution.model,
            },
        }


def authoritative_alert(alert_key: str = "alert-001") -> SimpleNamespace:
    """Return the authoritative alert shape read by every worker task."""
    return SimpleNamespace(
        alert_key=alert_key,
        table_name="dq.orders",
        metric="row_count_nonzero",
        dt=date(2026, 9, 8),
    )


def runtime(
    recorder: AuditRecorder,
    *,
    external_allowed: bool = False,
    globally_enabled: bool = True,
    ledger: Ledger | None = None,
    alert: Any | None = None,
    dq_history_fetcher: Any | None = None,
    pipeline_runs_fetcher: Any | None = None,
    metadata_getter: Any | None = None,
    blast_radius_fetcher: Any | None = None,
    dq_history_interpreter: Any | None = None,
    pipeline_run_interpreter: Any | None = None,
    metadata_lineage_interpreter: Any | None = None,
) -> EvidenceWorkerRuntimeConfig:
    """Build an injected worker runtime with no network or direct provider behavior."""
    return EvidenceWorkerRuntimeConfig(
        alert_loader=lambda **_: alert or authoritative_alert(),
        dq_history_fetcher=dq_history_fetcher or (lambda **_: {"rows": []}),
        pipeline_runs_fetcher=pipeline_runs_fetcher or (lambda **_: {"rows": []}),
        metadata_getter=metadata_getter or (lambda **_: {"qualified_name": "dq.orders"}),
        blast_radius_fetcher=blast_radius_fetcher or (lambda **_: {"matched": True}),
        dq_history_interpreter=dq_history_interpreter or (lambda **_: InterpretationResult()),
        pipeline_run_interpreter=pipeline_run_interpreter or (lambda **_: InterpretationResult()),
        metadata_lineage_interpreter=metadata_lineage_interpreter or (lambda **_: InterpretationResult()),
        external_llm_allowed=lambda: external_allowed,
        external_llm_enabled=lambda: globally_enabled,
        budget_getter=lambda: ledger if ledger is not None else Ledger(),
        audit_client_factory=lambda **_: object(),
        audit_writer=recorder,
    )


# --- Testing Builder And Permission Boundaries
@pytest.mark.parametrize(
    "task_type",
    [
        "interpret_dq_history",
        "interpret_pipeline_run",
        "interpret_metadata_lineage",
    ],
)
def test_builder_uses_exact_registered_permissions_and_fixed_budgets(task_type: str) -> None:
    """Every worker task must retain its exact registry tuple and hard execution ceiling."""
    task = build_evidence_worker_task(uuid4(), task_type, "alert-001")

    assert task.allowed_tools == required_tools_for_task(task.specialist_name, task_type)
    assert task.context_references == []
    assert task.model_call_budget == EVIDENCE_WORKER_MODEL_CALL_BUDGET
    assert task.token_budget == EVIDENCE_WORKER_TOKEN_BUDGET
    assert task.estimated_cost_budget_usd == EVIDENCE_WORKER_COST_BUDGET_USD
    assert task.timeout_seconds == EVIDENCE_WORKER_TIMEOUT_SECONDS
    assert task.input_payload == {"alert_key": "alert-001", "manifest_s3_uri": ""}


@pytest.mark.parametrize(
    "unsafe_update",
    [
        {"input_payload": {"alert_key": "alert-001", "unknown": "rejected"}},
        {
            "context_references": [
                ContextReference(
                    reference_type=ContextReferenceType.ALERT,
                    reference="another-alert",
                    description="Caller context is not accepted by this worker.",
                )
            ]
        },
        {"timeout_seconds": EVIDENCE_WORKER_TIMEOUT_SECONDS - 1},
    ],
)
def test_rejects_unknown_context_or_execution_limit_before_tool_use(
    unsafe_update: dict[str, Any],
) -> None:
    """Invalid payload, context, or timeout must block before deterministic reads run."""
    recorder = AuditRecorder()
    calls: list[str] = []
    task = build_evidence_worker_task(uuid4(), "interpret_dq_history", "alert-001")
    unsafe_task = task.model_copy(update=unsafe_update)
    config = runtime(
        recorder,
        alert=authoritative_alert(),
    )
    config = EvidenceWorkerRuntimeConfig(
        **{
            **config.__dict__,
            "alert_loader": lambda **_: calls.append("alerts") or authoritative_alert(),
        }
    )

    result = run_evidence_worker(unsafe_task, config)

    assert result.status.value == "blocked"
    assert calls == []
    assert recorder.events[0]["action"] == "evidence_worker_rejected"


def test_requires_existing_supervisor_budget_scope_before_alert_lookup() -> None:
    """A worker must not bypass caller-owned LLM admission even when external calls are off."""
    recorder = AuditRecorder()
    calls: list[str] = []
    task = build_evidence_worker_task(uuid4(), "interpret_dq_history", "alert-001")
    config = runtime(recorder)
    config = EvidenceWorkerRuntimeConfig(
        **{
            **config.__dict__,
            "budget_getter": lambda: None,
            "alert_loader": lambda **_: calls.append("alerts") or authoritative_alert(),
        }
    )

    result = run_evidence_worker(task, config)

    assert result.status.value == "blocked"
    assert calls == []
    assert result.model_call_count == 0


# --- Testing Deterministic Evidence Paths
@pytest.mark.parametrize("task_type", [
    "interpret_dq_history", "interpret_pipeline_run", "interpret_metadata_lineage",
])
@pytest.mark.parametrize("reference", ["DQ-20260908-A1B2C3", "dq-20260908-a1b2c3"])
def test_worker_accepts_authoritative_display_reference(task_type: str, reference: str) -> None:
    """Human references resolve to the same authoritative alert without weakening scope."""
    alert = authoritative_alert("orders|dq_failure|2026-09-08|dq.orders|row_count|table")
    alert.alert_display_id = "DQ-20260908-A1B2C3"
    task = build_evidence_worker_task(uuid4(), task_type, reference)
    result = run_evidence_worker(task, runtime(AuditRecorder(), alert=alert))
    assert result.status.value == "success"
    assert result.model_call_count == 0


def test_worker_rejects_other_alert_display_reference() -> None:
    """A friendly identifier must still exactly match the authoritative lookup result."""
    alert = authoritative_alert("orders|dq_failure|2026-09-08|dq.orders|row_count|table")
    alert.alert_display_id = "DQ-20260908-A1B2C3"
    calls: list[str] = []
    task = build_evidence_worker_task(uuid4(), "interpret_dq_history", "DQ-20260908-FFFFFF")
    result = run_evidence_worker(task, runtime(
        AuditRecorder(), alert=alert,
        dq_history_fetcher=lambda **_: calls.append("history"),
    ))
    assert result.status.value == "failed"
    assert result.model_call_count == 0
    assert calls == []


def test_disabled_external_llm_returns_sanitized_dq_history_without_interpretation() -> None:
    """The disabled path returns source references only and never forwards raw query content."""
    recorder = AuditRecorder()
    calls: list[str] = []

    def history_fetcher(**kwargs: Any) -> dict[str, Any]:
        """Return tool-like output including fields that must never reach a model or result."""
        calls.append("dq_history")
        assert kwargs["table_name"] == "dq.orders"
        assert kwargs["dt"] == date(2026, 9, 8)
        return {
            "sql": "SELECT password FROM secrets",
            "rows": [
                {
                    "dt": "2026-09-08",
                    "check_name": "row_count_nonzero",
                    "status": "failed",
                    "observed_value": 0,
                    "expected_value": 1,
                    "raw_sql": "DROP TABLE dq.orders",
                    "credentials": "must-not-leak",
                }
            ],
            "error_message": "raw database failure",
        }

    def unexpected_interpreter(**_: Any) -> InterpretationResult:
        """Fail if the disabled branch attempts an external interpretation."""
        raise AssertionError("disabled external LLM branch must not interpret evidence")

    task = build_evidence_worker_task(uuid4(), "interpret_dq_history", "alert-001")
    result = run_evidence_worker(
        task,
        runtime(
            recorder,
            dq_history_fetcher=history_fetcher,
            dq_history_interpreter=unexpected_interpreter,
        ),
    )
    serialized = json.dumps(result.model_dump(mode="json"))

    assert result.status.value == "success"
    assert result.confidence == 0.0
    assert result.model_call_count == 0
    assert result.structured_output["execution"]["mode"] == "deterministic_only"
    assert result.structured_output["interpretation_status"] == "not_run_external_llm_disabled"
    assert [item.source_tool for item in result.evidence_references] == ["dq_history"]
    assert calls == ["dq_history"]
    assert "SELECT password" not in serialized
    assert "DROP TABLE" not in serialized
    assert "must-not-leak" not in serialized
    assert [event["action"] for event in recorder.events] == [
        "evidence_worker_started",
        "evidence_worker_completed",
    ]


def test_global_external_llm_kill_switch_returns_deterministic_evidence() -> None:
    """A true request scope cannot bypass the global external-provider kill switch."""
    recorder = AuditRecorder()
    task = build_evidence_worker_task(uuid4(), "interpret_pipeline_run", "alert-001")
    result = run_evidence_worker(
        task,
        runtime(
            recorder,
            external_allowed=True,
            globally_enabled=False,
            pipeline_run_interpreter=lambda **_: pytest.fail("interpreter must not run"),
        ),
    )

    assert result.status.value == "success"
    assert result.model_call_count == 0
    assert result.structured_output["execution"]["mode"] == "deterministic_only"


def test_metadata_worker_uses_authoritative_table_and_strips_catalog_extras() -> None:
    """Metadata interpretation receives only selected catalog and bounded impact facts."""
    recorder = AuditRecorder()
    captured: list[Any] = []

    def metadata_getter(**kwargs: Any) -> dict[str, Any]:
        """Return catalog content with raw metadata that must be excluded."""
        assert kwargs["qualified_name"] == "dq.orders"
        return {
            "qualified_name": "dq.orders",
            "technical_owner": "platform@example.com",
            "certification_status": "candidate",
            "compiled_sql": "select * from secret_source",
            "metadata_json": {"credential": "nope"},
        }

    def blast_radius_fetcher(**kwargs: Any) -> dict[str, Any]:
        """Return bounded blast radius plus untrusted extra content."""
        assert kwargs["table_name"] == "dq.orders"
        return {
            "matched": True,
            "truncated": False,
            "impacted_asset_count": 2,
            "resource_type_counts": {"model": 2},
            "unresolved_nodes": ["model.secret"],
            "raw_query": "select credentials",
        }

    def interpreter(request: Any, **kwargs: Any) -> InterpretationResult:
        """Capture the strict request without directly calling any provider."""
        captured.append((request, kwargs))
        return InterpretationResult()

    task = build_evidence_worker_task(
        uuid4(),
        "interpret_metadata_lineage",
        "alert-001",
        manifest_s3_uri="s3://dq-artifacts/manifest.json",
    )
    result = run_evidence_worker(
        task,
        runtime(
            recorder,
            external_allowed=True,
            ledger=Ledger(model_calls=1, tokens=120, cost=0.0004),
            metadata_getter=metadata_getter,
            blast_radius_fetcher=blast_radius_fetcher,
            metadata_lineage_interpreter=interpreter,
        ),
    )
    request, kwargs = captured[0]
    request_json = json.dumps(request.model_dump(mode="json"))

    assert result.status.value == "success"
    assert kwargs["strict_external"] is True
    assert kwargs["agent_run_id"] == task.parent_run_id
    assert [item.source_tool for item in result.evidence_references] == [
        "metadata_catalog",
        "dbt_blast_radius",
    ]
    assert "compiled_sql" not in request_json
    assert "metadata_json" not in request_json
    assert "credential" not in request_json
    assert "raw_query" not in request_json
    assert "unresolved_nodes" not in request_json
    assert result.structured_output["execution"] == {
        "mode": "external_interpretation",
        "llm_provider": "gemini",
        "llm_model": "gemini-2.5-flash",
        "input_tokens": 80,
        "output_tokens": 40,
        "tokens": 120,
        "estimated_cost_usd": 0.0004,
        "model_calls": 1,
    }
    assert [event["action"] for event in recorder.events] == [
        "evidence_worker_started",
        "llm_route_completed",
        "evidence_worker_completed",
    ]
    llm_event = recorder.events[1]
    assert llm_event["tool_name"] == "llm_router"
    assert "prompt" not in json.dumps(
        {"input": llm_event["input_payload"], "output": llm_event["output_payload"]}
    )


def test_pipeline_worker_runs_exactly_one_strict_interpreter_and_keeps_raw_error_out() -> None:
    """Pipeline evidence can reach one strict interpreter call without source error leakage."""
    recorder = AuditRecorder()
    captured: list[Any] = []

    def pipeline_fetcher(**_: Any) -> dict[str, Any]:
        """Return a representative pipeline row plus prohibited raw fields."""
        return {
            "rows": [
                {
                    "run_id": "run-001",
                    "dag_id": "daily_dq",
                    "status": "failed",
                    "duration_ms": 123,
                    "error_message": "password=secret",
                    "metadata_json": {"api_key": "secret"},
                }
            ]
        }

    def interpreter(request: Any, **kwargs: Any) -> InterpretationResult:
        """Retain the only allowed interpreter invocation."""
        captured.append((request, kwargs))
        return InterpretationResult()

    task = build_evidence_worker_task(uuid4(), "interpret_pipeline_run", "alert-001")
    result = run_evidence_worker(
        task,
        runtime(
            recorder,
            external_allowed=True,
            ledger=Ledger(model_calls=1, tokens=120, cost=0.0004),
            pipeline_runs_fetcher=pipeline_fetcher,
            pipeline_run_interpreter=interpreter,
        ),
    )
    request, kwargs = captured[0]
    request_json = json.dumps(request.model_dump(mode="json"))

    assert result.status.value == "success"
    assert len(captured) == 1
    assert kwargs["strict_external"] is True
    assert "error_message" not in request_json
    assert "metadata_json" not in request_json
    assert "password=secret" not in request_json


def test_authoritative_alert_identity_mismatch_fails_without_interpretation() -> None:
    """A fetched alert must match the envelope identity before evidence collection continues."""
    recorder = AuditRecorder()
    task = build_evidence_worker_task(uuid4(), "interpret_dq_history", "alert-001")
    result = run_evidence_worker(
        task,
        runtime(
            recorder,
            external_allowed=True,
            alert=authoritative_alert("different-alert"),
        ),
    )

    assert result.status.value == "failed"
    assert result.model_call_count == 0
    assert result.evidence_references == []
    assert recorder.events[-1]["action"] == "evidence_worker_failed"
