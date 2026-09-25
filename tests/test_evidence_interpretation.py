####
## Read-Only Evidence Interpretation Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Validate isolated, budgeted, citation-grounded evidence interpretation."""

# --- Importing Libraries
from __future__ import annotations

import json
import time
from contextlib import contextmanager
from typing import Any, Iterator
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from agent.llm.client import LlmResponse
from agent.specialists import evidence_interpretation
from agent.specialists.evidence_interpretation import (
    DQ_HISTORY_PROMPT,
    EVIDENCE_INTERPRETATION_ROUTE,
    METADATA_LINEAGE_PROMPT,
    PIPELINE_RUN_PROMPT,
    DeterministicEvidence,
    DqHistoryInterpretationInput,
    DqHistoryInterpretationOutput,
    EvidenceCitationError,
    EvidenceInterpretationBudgetScopeError,
    EvidenceInterpretationFallbackError,
    MetadataLineageImpactInterpretationInput,
    MetadataLineageImpactInterpretationOutput,
    PipelineRunInterpretationInput,
    PipelineRunInterpretationOutput,
    interpret_dq_history,
    interpret_metadata_lineage_impact,
    interpret_pipeline_run,
)
from agent.supervisor.budgets import supervisor_llm_budget_scope


# --- Defining Test Helpers
@contextmanager
def interpretation_budget() -> Iterator[None]:
    """Install the same one-call budget scope expected from the future worker adapter."""
    with supervisor_llm_budget_scope(
        max_model_calls=1,
        token_budget=4_000,
        estimated_cost_budget_usd=0.01,
        deadline_monotonic=time.monotonic() + 30,
    ):
        yield


def dq_request(evidence: list[DeterministicEvidence] | None = None) -> DqHistoryInterpretationInput:
    """Build representative bounded DQ history input."""
    return DqHistoryInterpretationInput(
        asset_name="dq.fct_orders_daily",
        check_name="row_count_positive",
        evaluation_window="2026-08-01 through 2026-08-07",
        evidence=evidence if evidence is not None else [
            DeterministicEvidence(
                evidence_id="EV-DQ-1",
                evidence_type="dq_history",
                source_tool="dq_history",
                summary="Three of seven daily checks failed in the supplied window.",
                facts={"status_counts": {"failed": 3, "passed": 4}},
            )
        ],
    )


def pipeline_request() -> PipelineRunInterpretationInput:
    """Build representative bounded pipeline-run input."""
    return PipelineRunInterpretationInput(
        pipeline_name="orders_daily",
        evaluation_window="2026-08-07",
        evidence=[
            DeterministicEvidence(
                evidence_id="EV-RUN-1",
                evidence_type="pipeline_run",
                source_tool="pipeline_runs",
                summary="The load task failed before the mart task started.",
                facts={"status": "failed", "task_id": "load_orders", "rows_written": 0},
            )
        ],
    )


def metadata_request() -> MetadataLineageImpactInterpretationInput:
    """Build representative bounded metadata and lineage impact input."""
    return MetadataLineageImpactInterpretationInput(
        asset_name="dq.fct_orders_daily",
        change_summary="The order_status column contract changed from required to optional.",
        evidence=[
            DeterministicEvidence(
                evidence_id="EV-LINEAGE-1",
                evidence_type="blast_radius",
                source_tool="dbt_blast_radius",
                summary="The bounded traversal found two downstream marts and was not truncated.",
                facts={"impacted_asset_count": 2, "truncated": False, "max_depth_reached": 2},
            )
        ],
    )


def structured_payload(purpose: str, evidence_id: str) -> dict[str, Any]:
    """Build one valid purpose-specific structured model payload."""
    common = {
        "summary": "The supplied deterministic evidence supports a bounded operational interpretation.",
        "findings": [
            {
                "statement": "The supplied evidence contains a concrete signal relevant to this scope.",
                "basis": "observed",
                "evidence_ids": [evidence_id],
            }
        ],
        "missing_evidence": [
            {
                "description": "Longer comparison history was not supplied for this interpretation.",
                "consequence": "The result cannot establish whether the observed signal is a long-term norm.",
            }
        ],
    }

    if purpose == "dq_history":
        return {**common, "history_pattern": "mixed"}
    if purpose == "pipeline_run":
        return {**common, "run_assessment": "task_failure"}

    return {**common, "impact_scope": "multi_asset"}


def successful_response(
    agent_run_id: UUID | str,
    payload: dict[str, Any],
    *,
    route_name: str = EVIDENCE_INTERPRETATION_ROUTE,
    provider: str = "gemini",
    used_heuristic: bool = False,
    fallback_reason: str = "",
    attempted_routes: list[str] | None = None,
) -> LlmResponse:
    """Build a normalized mocked route response without any provider call."""
    return LlmResponse(
        agent_run_id=UUID(str(agent_run_id)),
        route_name=route_name,
        provider=provider,
        model="gemini-3.5-flash-lite" if provider == "gemini" else "heuristic-v1",
        content=json.dumps(payload),
        structured_output=payload,
        input_tokens=120,
        output_tokens=80,
        estimated_cost_usd=0.0003 if provider == "gemini" else 0.0,
        duration_ms=25,
        used_heuristic=used_heuristic,
        fallback_reason=fallback_reason,
        metadata={
            "requested_route": EVIDENCE_INTERPRETATION_ROUTE,
            "executed_route": route_name,
            "attempted_routes": attempted_routes or [route_name],
            "structured_output_status": "validated",
        },
    )


# --- Testing Input Boundaries
def test_inputs_reject_free_sql_provider_controls_and_wrong_guarded_source() -> None:
    """Keep raw execution and routing controls outside the sidecar contract."""
    with pytest.raises(ValidationError, match="forbidden key"):
        dq_request(
            evidence=[
                DeterministicEvidence(
                    evidence_id="EV-DQ-SQL",
                    evidence_type="dq_history",
                    source_tool="dq_history",
                    summary="A bounded DQ result was supplied.",
                    facts={"sql": "DELETE FROM dq.fct_orders_daily"},
                )
            ]
        )

    with pytest.raises(ValidationError):
        DqHistoryInterpretationInput.model_validate(
            {
                **dq_request().model_dump(mode="json"),
                "provider": "gemini",
                "model_route": "cheap_summary",
            }
        )

    with pytest.raises(ValidationError, match="Unsupported evidence source"):
        PipelineRunInterpretationInput(
            pipeline_name="orders_daily",
            evaluation_window="2026-08-07",
            evidence=dq_request().evidence,
        )

    with pytest.raises(ValidationError, match="does not match its guarded source"):
        PipelineRunInterpretationInput(
            pipeline_name="orders_daily",
            evaluation_window="2026-08-07",
            evidence=[
                DeterministicEvidence(
                    evidence_id="EV-RUN-MISMATCH",
                    evidence_type="dq_history",
                    source_tool="pipeline_runs",
                    summary="A pipeline status record was supplied with the wrong type.",
                )
            ],
        )

    with pytest.raises(ValidationError, match="forbidden key"):
        DeterministicEvidence(
            evidence_id="EV-SECRET",
            evidence_type="pipeline_run",
            source_tool="pipeline_runs",
            summary="A malformed evidence item contains a credential-like field.",
            facts={"api_token": "must-not-enter-provider-context"},
        )


def test_input_rejects_duplicate_evidence_ids() -> None:
    """Prevent ambiguous citation resolution within one worker input."""
    evidence = dq_request().evidence[0]

    with pytest.raises(ValidationError, match="Evidence IDs must be unique"):
        dq_request(evidence=[evidence, evidence.model_copy()])


def test_model_output_schemas_cannot_select_tools_providers_or_mutations() -> None:
    """Keep execution authority out of every model-authored output contract."""
    schemas = json.dumps(
        [
            DqHistoryInterpretationOutput.model_json_schema(),
            PipelineRunInterpretationOutput.model_json_schema(),
            MetadataLineageImpactInterpretationOutput.model_json_schema(),
        ]
    ).lower()

    for forbidden_field in (
        '"command"',
        '"model"',
        '"mutation"',
        '"provider"',
        '"sql"',
        '"tool"',
    ):
        assert forbidden_field not in schemas


# --- Testing Purpose-Specific Calls
def test_three_purposes_use_distinct_prompts_and_isolated_context(monkeypatch) -> None:
    """Issue exactly one fixed-route call per purpose without sibling-worker context."""
    captured: list[dict[str, Any]] = []

    def capture_call(**kwargs: Any) -> LlmResponse:
        """Capture one routed request and return purpose-matched structured output."""
        captured.append(kwargs)
        purpose     = str(kwargs["context"]["purpose"])
        evidence_id = str(kwargs["context"]["policy"]["allowed_evidence_ids"][0])

        return successful_response(
            agent_run_id=kwargs["agent_run_id"],
            payload=structured_payload(purpose, evidence_id),
        )

    monkeypatch.setattr(evidence_interpretation, "run_llm_task", capture_call)

    with interpretation_budget():
        dq_result = interpret_dq_history(dq_request(), agent_run_id=uuid4())
    with interpretation_budget():
        pipeline_result = interpret_pipeline_run(pipeline_request(), agent_run_id=uuid4())
    with interpretation_budget():
        metadata_result = interpret_metadata_lineage_impact(
            metadata_request(),
            agent_run_id=uuid4(),
        )

    assert len(captured) == 3
    assert [item["route_name"] for item in captured] == [
        EVIDENCE_INTERPRETATION_ROUTE,
        EVIDENCE_INTERPRETATION_ROUTE,
        EVIDENCE_INTERPRETATION_ROUTE,
    ]
    assert [item["prompt"] for item in captured] == [
        DQ_HISTORY_PROMPT,
        PIPELINE_RUN_PROMPT,
        METADATA_LINEAGE_PROMPT,
    ]
    assert [item["response_schema_name"] for item in captured] == [
        "dq_history_interpretation",
        "pipeline_run_interpretation",
        "metadata_lineage_impact_interpretation",
    ]
    assert all("worker_context" not in json.dumps(item["context"]) for item in captured)
    assert captured[0]["context"]["input"]["evidence"][0]["evidence_id"] == "EV-DQ-1"
    assert "EV-RUN-1" not in json.dumps(captured[0]["context"])
    assert dq_result.cited_evidence_ids == ["EV-DQ-1"]
    assert pipeline_result.cited_evidence_ids == ["EV-RUN-1"]
    assert metadata_result.cited_evidence_ids == ["EV-LINEAGE-1"]


def test_interpretation_requires_existing_supervisor_budget_scope(monkeypatch) -> None:
    """Reject direct use that would bypass caller-owned model-call admission."""
    called = False

    def unexpected_call(**kwargs: Any) -> LlmResponse:
        """Fail if budget validation does not happen before route execution."""
        nonlocal called
        called = True
        raise AssertionError("run_llm_task must not run without a supervisor budget scope")

    monkeypatch.setattr(evidence_interpretation, "run_llm_task", unexpected_call)

    with pytest.raises(EvidenceInterpretationBudgetScopeError):
        interpret_dq_history(dq_request(), agent_run_id=uuid4())

    assert called is False


# --- Testing Strict External Acceptance
@pytest.mark.parametrize(
    ("response_kwargs", "expected_reason"),
    [
        (
            {
                "provider": "heuristic",
                "used_heuristic": True,
            },
            "heuristic_response",
        ),
        (
            {
                "route_name": "openai_summary",
                "fallback_reason": "provider_error:gemini:RateLimitError",
                "attempted_routes": ["cheap_summary", "openai_summary"],
            },
            "executed_route_changed",
        ),
        (
            {
                "provider": "openai",
            },
            "unexpected_external_provider",
        ),
    ],
)
def test_strict_external_mode_rejects_heuristic_or_route_fallback(
    monkeypatch,
    response_kwargs: dict[str, Any],
    expected_reason: str,
) -> None:
    """Make genuine external acceptance fail instead of silently passing fallback output."""
    def fallback_call(**kwargs: Any) -> LlmResponse:
        """Return one mocked fallback response."""
        return successful_response(
            agent_run_id=kwargs["agent_run_id"],
            payload=structured_payload("dq_history", "EV-DQ-1"),
            **response_kwargs,
        )

    monkeypatch.setattr(evidence_interpretation, "run_llm_task", fallback_call)

    with interpretation_budget():
        with pytest.raises(EvidenceInterpretationFallbackError, match=expected_reason):
            interpret_dq_history(dq_request(), agent_run_id=uuid4(), strict_external=True)


def test_strict_external_mode_rejects_unknown_evidence_citation(monkeypatch) -> None:
    """Reject fabricated evidence IDs rather than filtering them into a passing result."""
    def invalid_citation_call(**kwargs: Any) -> LlmResponse:
        """Return direct external output containing one invented citation."""
        return successful_response(
            agent_run_id=kwargs["agent_run_id"],
            payload=structured_payload("pipeline_run", "EV-NOT-SUPPLIED"),
        )

    monkeypatch.setattr(evidence_interpretation, "run_llm_task", invalid_citation_call)

    with interpretation_budget():
        with pytest.raises(EvidenceCitationError, match="EV-NOT-SUPPLIED"):
            interpret_pipeline_run(
                pipeline_request(),
                agent_run_id=uuid4(),
                strict_external=True,
            )


def test_supplied_evidence_requires_at_least_one_valid_citation(monkeypatch) -> None:
    """Prevent uncited generic prose from passing as evidence interpretation."""
    payload = structured_payload("dq_history", "EV-DQ-1")
    payload["findings"] = []

    monkeypatch.setattr(
        evidence_interpretation,
        "run_llm_task",
        lambda **kwargs: successful_response(kwargs["agent_run_id"], payload),
    )

    with interpretation_budget():
        with pytest.raises(EvidenceCitationError, match="did not cite"):
            interpret_dq_history(dq_request(), agent_run_id=uuid4())


def test_empty_input_can_only_return_truthful_missing_evidence(monkeypatch) -> None:
    """Allow an explicit evidence gap while preventing unsupported findings."""
    payload = {
        "summary": "No deterministic DQ history evidence was supplied for this bounded request.",
        "history_pattern": "undetermined",
        "findings": [],
        "missing_evidence": [
            {
                "description": "No DQ history rows were supplied for the requested evaluation window.",
                "consequence": "Recurrence and trend direction cannot be determined from this input.",
            }
        ],
    }
    monkeypatch.setattr(
        evidence_interpretation,
        "run_llm_task",
        lambda **kwargs: successful_response(kwargs["agent_run_id"], payload),
    )

    with interpretation_budget():
        result = interpret_dq_history(
            dq_request(evidence=[]),
            agent_run_id=uuid4(),
        )

    assert result.input_evidence_ids == []
    assert result.cited_evidence_ids == []
    assert result.interpretation.history_pattern == "undetermined"
    assert result.interpretation.missing_evidence


def test_output_contract_rejects_generic_empty_interpretation(monkeypatch) -> None:
    """Require either cited findings or an explicit evidence gap in structured output."""
    payload = {
        "summary": "The evidence could not support an interpretation for this bounded request.",
        "impact_scope": "undetermined",
        "findings": [],
        "missing_evidence": [],
    }
    monkeypatch.setattr(
        evidence_interpretation,
        "run_llm_task",
        lambda **kwargs: successful_response(kwargs["agent_run_id"], payload),
    )

    with interpretation_budget():
        with pytest.raises(ValidationError, match="cited findings or explicit missing evidence"):
            interpret_metadata_lineage_impact(
                metadata_request(),
                agent_run_id=uuid4(),
            )
