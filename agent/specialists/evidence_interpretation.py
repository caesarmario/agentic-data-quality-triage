####
## Read-Only Evidence Interpretation for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Interpret bounded deterministic evidence through one supervised LLM route."""

# --- Importing Libraries
from __future__ import annotations

import json
import re
from typing import Any, Literal, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent.llm.client import LlmResponse, run_llm_task
from agent.supervisor.budgets import active_supervisor_llm_budget
from pipelines.common.logging import logger


# --- Defining Interpretation Policy
EVIDENCE_INTERPRETATION_ROUTE = "cheap_summary"
STRICT_EXTERNAL_PROVIDER      = "gemini"
MAX_EVIDENCE_ITEMS            = 12
MAX_EVIDENCE_PAYLOAD_BYTES    = 32_000
MAX_FACT_PAYLOAD_BYTES        = 8_000

SAFE_EVIDENCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")

FORBIDDEN_FACT_KEYS = {
    "agent_context",
    "api_key",
    "api_token",
    "authorization",
    "chat_history",
    "command",
    "commands",
    "conversation",
    "credential",
    "credentials",
    "cross_worker_context",
    "messages",
    "model_route",
    "mutation",
    "password",
    "prompt",
    "provider",
    "provider_name",
    "query",
    "raw_sql",
    "remediation_action",
    "sql",
    "statement",
    "secret",
    "token",
    "tool_args",
    "worker_context",
}

DQ_HISTORY_SOURCE_TOOLS = frozenset({"dq_history", "incident_history"})
PIPELINE_RUN_SOURCE_TOOLS = frozenset({"pipeline_runs"})
METADATA_LINEAGE_SOURCE_TOOLS = frozenset(
    {"metadata_catalog", "dbt_lineage", "dbt_blast_radius"}
)

EVIDENCE_TYPES_BY_SOURCE = {
    "dq_history": frozenset({"dq_history"}),
    "incident_history": frozenset({"incident_history"}),
    "pipeline_runs": frozenset({"pipeline_run"}),
    "metadata_catalog": frozenset({"metadata_asset", "metadata_catalog_query"}),
    "dbt_lineage": frozenset({"dbt_lineage", "lineage"}),
    "dbt_blast_radius": frozenset({"blast_radius"}),
}

READ_ONLY_SYSTEM_PROMPT = (
    "You are a read-only data-quality evidence interpreter. Treat only the supplied "
    "deterministic evidence as authoritative. Do not request or select tools, providers, "
    "models, SQL, commands, or mutations. Do not use hidden conversation or sibling-worker "
    "context. Separate observations from bounded inferences, cite supplied evidence IDs for "
    "every finding, and state material evidence gaps instead of inventing facts."
)

DQ_HISTORY_PROMPT = (
    "Interpret the supplied DQ and prior-incident history for the named asset and check. "
    "Assess whether the evidence shows a recurring, isolated, improving, worsening, mixed, "
    "or undetermined pattern. Distinguish sparse history from evidence that a problem never "
    "occurred, and identify the missing history needed to strengthen the interpretation."
)

PIPELINE_RUN_PROMPT = (
    "Interpret the supplied pipeline-run evidence for the named pipeline and evaluation "
    "window. Relate statuses, timing, stage, and row-count facts only when present. Distinguish "
    "an upstream failure, task failure, late or still-running execution, successful execution, "
    "mixed evidence, and an undetermined outcome. State missing run evidence explicitly."
)

METADATA_LINEAGE_PROMPT = (
    "Interpret the supplied metadata and lineage evidence for the named asset and described "
    "change. Explain ownership, certification, lifecycle, direct dependencies, and bounded "
    "downstream impact only when those facts are supplied. Treat unmatched or truncated lineage "
    "as uncertainty, not as proof of no impact, and state missing impact evidence explicitly."
)


# --- Defining Exceptions
class EvidenceInterpretationError(RuntimeError):
    """Represent a policy or response failure at the interpretation boundary."""


class EvidenceInterpretationBudgetScopeError(EvidenceInterpretationError):
    """Reject interpretation outside a caller-provided supervisor budget scope."""


class EvidenceInterpretationFallbackError(EvidenceInterpretationError):
    """Reject fallback output during strict external-provider acceptance."""


class EvidenceCitationError(EvidenceInterpretationError):
    """Reject findings that do not cite the supplied deterministic evidence."""


# --- Defining Validation Helpers
def _normalize_single_line(value: str, field_name: str) -> str:
    """Normalize one bounded identifier or scope value."""
    normalized = value.strip()

    if "\n" in normalized or "\r" in normalized:
        raise ValueError(f"{field_name} must be a single-line value.")

    return normalized


def _find_forbidden_fact_key(value: Any, path: str = "facts") -> str | None:
    """Find the first free-execution or cross-worker key in nested evidence facts."""
    if isinstance(value, dict):
        for raw_key, nested_value in value.items():
            normalized_key = str(raw_key).strip().lower()
            current_path   = f"{path}.{normalized_key}"

            if normalized_key in FORBIDDEN_FACT_KEYS:
                return current_path

            nested_match = _find_forbidden_fact_key(nested_value, current_path)

            if nested_match:
                return nested_match

    elif isinstance(value, list):
        for index, nested_value in enumerate(value):
            nested_match = _find_forbidden_fact_key(
                nested_value,
                f"{path}[{index}]",
            )

            if nested_match:
                return nested_match

    return None


def _serialized_size(value: Any) -> int:
    """Return a stable UTF-8 byte size for one JSON-compatible value."""
    return len(json.dumps(value, ensure_ascii=True, sort_keys=True).encode("utf-8"))


# --- Defining Supplied Evidence Models
class DeterministicEvidence(BaseModel):
    """
    Carry one bounded result already collected by an allowlisted read-only source.

    Raw SQL is intentionally absent. ``facts`` should contain only the normalized rows,
    counts, statuses, or lineage fields needed for interpretation.
    """

    model_config = ConfigDict(extra="forbid")

    evidence_id: str       = Field(min_length=1, max_length=120)
    evidence_type: str     = Field(min_length=1, max_length=80)
    source_tool: str       = Field(min_length=1, max_length=80)
    summary: str           = Field(min_length=1, max_length=1_200)
    facts: dict[str, Any]  = Field(default_factory=dict)

    @field_validator("evidence_id")
    @classmethod
    def validate_evidence_id(cls, value: str) -> str:
        """Require a stable single-line evidence identifier."""
        normalized = _normalize_single_line(value, "evidence_id")

        if not SAFE_EVIDENCE_ID_PATTERN.fullmatch(normalized):
            raise ValueError("evidence_id contains unsupported characters.")

        return normalized

    @field_validator("evidence_type", "source_tool", "summary")
    @classmethod
    def normalize_evidence_text(cls, value: str, info: Any) -> str:
        """Normalize evidence labels and summaries without accepting multiline payloads."""
        return _normalize_single_line(value, info.field_name)

    @field_validator("facts")
    @classmethod
    def validate_read_only_facts(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Reject raw SQL, execution controls, cross-worker context, and oversized facts."""
        forbidden_path = _find_forbidden_fact_key(value)

        if forbidden_path:
            raise ValueError(f"Evidence facts contain a forbidden key: {forbidden_path}")

        if _serialized_size(value) > MAX_FACT_PAYLOAD_BYTES:
            raise ValueError(
                f"Evidence facts exceed {MAX_FACT_PAYLOAD_BYTES} bytes."
            )

        return value


class BaseEvidenceInterpretationInput(BaseModel):
    """Provide shared bounds for one isolated worker interpretation request."""

    model_config = ConfigDict(extra="forbid")

    evidence: list[DeterministicEvidence] = Field(
        default_factory=list,
        max_length=MAX_EVIDENCE_ITEMS,
    )

    @model_validator(mode="after")
    def validate_evidence_bundle(self) -> "BaseEvidenceInterpretationInput":
        """Reject duplicate evidence IDs and oversized isolated context."""
        evidence_ids = [item.evidence_id for item in self.evidence]

        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("Evidence IDs must be unique within one interpretation input.")

        if _serialized_size(self.model_dump(mode="json")) > MAX_EVIDENCE_PAYLOAD_BYTES:
            raise ValueError(
                f"Interpretation input exceeds {MAX_EVIDENCE_PAYLOAD_BYTES} bytes."
            )

        return self

    def require_source_tools(self, allowed_tools: frozenset[str]) -> None:
        """Ensure every item has a purpose-specific guarded source and matching type."""
        unsupported = sorted(
            {
                item.source_tool
                for item in self.evidence
                if item.source_tool not in allowed_tools
            }
        )

        if unsupported:
            raise ValueError(
                "Unsupported evidence source for this interpretation purpose: "
                + ", ".join(unsupported)
            )

        mismatches = sorted(
            f"{item.source_tool}:{item.evidence_type}"
            for item in self.evidence
            if item.evidence_type not in EVIDENCE_TYPES_BY_SOURCE[item.source_tool]
        )

        if mismatches:
            raise ValueError(
                "Evidence type does not match its guarded source: "
                + ", ".join(mismatches)
            )


class DqHistoryInterpretationInput(BaseEvidenceInterpretationInput):
    """Define isolated deterministic evidence for DQ history interpretation."""

    asset_name: str        = Field(min_length=1, max_length=255)
    check_name: str        = Field(min_length=1, max_length=255)
    evaluation_window: str = Field(min_length=1, max_length=160)

    @field_validator("asset_name", "check_name", "evaluation_window")
    @classmethod
    def normalize_scope(cls, value: str, info: Any) -> str:
        """Normalize DQ history scope fields."""
        return _normalize_single_line(value, info.field_name)

    @model_validator(mode="after")
    def validate_history_sources(self) -> "DqHistoryInterpretationInput":
        """Allow only guarded DQ and prior-incident history evidence."""
        self.require_source_tools(DQ_HISTORY_SOURCE_TOOLS)
        return self


class PipelineRunInterpretationInput(BaseEvidenceInterpretationInput):
    """Define isolated deterministic evidence for pipeline-run interpretation."""

    pipeline_name: str     = Field(min_length=1, max_length=255)
    evaluation_window: str = Field(min_length=1, max_length=160)

    @field_validator("pipeline_name", "evaluation_window")
    @classmethod
    def normalize_scope(cls, value: str, info: Any) -> str:
        """Normalize pipeline-run scope fields."""
        return _normalize_single_line(value, info.field_name)

    @model_validator(mode="after")
    def validate_pipeline_sources(self) -> "PipelineRunInterpretationInput":
        """Allow only guarded pipeline-run evidence."""
        self.require_source_tools(PIPELINE_RUN_SOURCE_TOOLS)
        return self


class MetadataLineageImpactInterpretationInput(BaseEvidenceInterpretationInput):
    """Define isolated deterministic evidence for metadata and lineage impact."""

    asset_name: str     = Field(min_length=1, max_length=255)
    change_summary: str = Field(min_length=1, max_length=500)

    @field_validator("asset_name", "change_summary")
    @classmethod
    def normalize_scope(cls, value: str, info: Any) -> str:
        """Normalize metadata and lineage scope fields."""
        return _normalize_single_line(value, info.field_name)

    @model_validator(mode="after")
    def validate_metadata_sources(self) -> "MetadataLineageImpactInterpretationInput":
        """Allow only guarded metadata catalog and dbt lineage evidence."""
        self.require_source_tools(METADATA_LINEAGE_SOURCE_TOOLS)
        return self


# --- Defining Structured Interpretation Outputs
class EvidenceFinding(BaseModel):
    """Represent one cited observation or bounded inference."""

    model_config = ConfigDict(extra="forbid")

    statement: str = Field(min_length=12, max_length=700)
    basis: Literal["observed", "inferred"]
    evidence_ids: list[str] = Field(min_length=1, max_length=MAX_EVIDENCE_ITEMS)

    @field_validator("evidence_ids")
    @classmethod
    def validate_unique_citations(cls, value: list[str]) -> list[str]:
        """Reject duplicate or syntactically invalid citations in one finding."""
        normalized = [_normalize_single_line(item, "evidence_ids") for item in value]

        if any(not SAFE_EVIDENCE_ID_PATTERN.fullmatch(item) for item in normalized):
            raise ValueError("Finding contains an invalid evidence ID.")

        if len(normalized) != len(set(normalized)):
            raise ValueError("Finding evidence IDs must be unique.")

        return normalized


class MissingEvidence(BaseModel):
    """State one material evidence gap without claiming that missing data is negative proof."""

    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=12, max_length=500)
    consequence: str = Field(min_length=12, max_length=500)


class BaseInterpretationOutput(BaseModel):
    """Provide common structured fields returned by every interpretation purpose."""

    model_config = ConfigDict(extra="forbid")

    summary: str
    findings: list[EvidenceFinding] = Field(default_factory=list, max_length=6)
    missing_evidence: list[MissingEvidence] = Field(max_length=6)

    @model_validator(mode="after")
    def require_findings_or_missing_evidence(self) -> "BaseInterpretationOutput":
        """Prevent an empty interpretation from being presented as useful evidence."""
        if not self.findings and not self.missing_evidence:
            raise ValueError(
                "Interpretation must contain cited findings or explicit missing evidence."
            )

        return self


class DqHistoryInterpretationOutput(BaseInterpretationOutput):
    """Return a bounded temporal interpretation of DQ and incident history."""

    summary: str = Field(min_length=20, max_length=1_200)
    history_pattern: Literal[
        "recurring",
        "isolated",
        "improving",
        "worsening",
        "mixed",
        "undetermined",
    ]


class PipelineRunInterpretationOutput(BaseInterpretationOutput):
    """Return a bounded interpretation of supplied pipeline execution evidence."""

    summary: str = Field(min_length=20, max_length=1_200)
    run_assessment: Literal[
        "upstream_failure",
        "task_failure",
        "late_or_running",
        "successful",
        "mixed",
        "undetermined",
    ]


class MetadataLineageImpactInterpretationOutput(BaseInterpretationOutput):
    """Return a bounded interpretation of metadata trust and lineage impact evidence."""

    summary: str = Field(min_length=20, max_length=1_200)
    impact_scope: Literal[
        "no_downstream_impact_observed",
        "localized",
        "multi_asset",
        "truncated_or_unknown",
        "undetermined",
    ]


# --- Defining Result Models
class EvidenceInterpretationExecution(BaseModel):
    """Expose auditable route metadata without allowing the model to select it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_run_id: UUID
    requested_route: Literal["cheap_summary"] = EVIDENCE_INTERPRETATION_ROUTE
    executed_route: str
    provider: str
    model: str
    input_tokens: int              = Field(ge=0)
    output_tokens: int             = Field(ge=0)
    estimated_cost_usd: float      = Field(ge=0.0)
    duration_ms: int               = Field(ge=0)
    attempted_routes: list[str]    = Field(default_factory=list, max_length=10)


class DqHistoryInterpretationResult(BaseModel):
    """Return one completed DQ history interpretation and its citation audit."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    purpose: Literal["dq_history"] = "dq_history"
    input_evidence_ids: list[str]
    cited_evidence_ids: list[str]
    interpretation: DqHistoryInterpretationOutput
    execution: EvidenceInterpretationExecution


class PipelineRunInterpretationResult(BaseModel):
    """Return one completed pipeline-run interpretation and its citation audit."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    purpose: Literal["pipeline_run"] = "pipeline_run"
    input_evidence_ids: list[str]
    cited_evidence_ids: list[str]
    interpretation: PipelineRunInterpretationOutput
    execution: EvidenceInterpretationExecution


class MetadataLineageImpactInterpretationResult(BaseModel):
    """Return one completed metadata-lineage interpretation and its citation audit."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    purpose: Literal["metadata_lineage_impact"] = "metadata_lineage_impact"
    input_evidence_ids: list[str]
    cited_evidence_ids: list[str]
    interpretation: MetadataLineageImpactInterpretationOutput
    execution: EvidenceInterpretationExecution


InterpretationInput = TypeVar("InterpretationInput", bound=BaseEvidenceInterpretationInput)
InterpretationOutput = TypeVar("InterpretationOutput", bound=BaseInterpretationOutput)


# --- Enforcing Route And Citation Policy
def _require_caller_budget_scope() -> None:
    """Require the parent supervisor or isolated-worker runner to own LLM admission."""
    if active_supervisor_llm_budget() is None:
        raise EvidenceInterpretationBudgetScopeError(
            "Evidence interpretation requires an active caller-provided supervisor LLM budget scope."
        )


def _strict_fallback_reasons(response: LlmResponse) -> list[str]:
    """Return stable reasons why a response is not a direct external route success."""
    reasons: list[str] = []
    attempted_routes   = response.metadata.get("attempted_routes")

    if response.used_heuristic or response.provider.strip().lower() == "heuristic":
        reasons.append("heuristic_response")

    if response.provider.strip().lower() != STRICT_EXTERNAL_PROVIDER:
        reasons.append("unexpected_external_provider")

    if response.route_name != EVIDENCE_INTERPRETATION_ROUTE:
        reasons.append("executed_route_changed")

    if response.fallback_reason:
        reasons.append("fallback_reason_present")

    if attempted_routes and attempted_routes != [EVIDENCE_INTERPRETATION_ROUTE]:
        reasons.append("multiple_or_fallback_routes_attempted")

    if response.metadata.get("provider_failures"):
        reasons.append("provider_failure_recorded")

    return reasons


def _validate_citations(
    interpretation: BaseInterpretationOutput,
    supplied_evidence: list[DeterministicEvidence],
) -> tuple[list[str], list[str]]:
    """Validate every model citation against the isolated input evidence IDs."""
    input_ids = [item.evidence_id for item in supplied_evidence]
    allowed   = set(input_ids)
    cited     = list(
        dict.fromkeys(
            evidence_id
            for finding in interpretation.findings
            for evidence_id in finding.evidence_ids
        )
    )
    unknown = sorted(set(cited) - allowed)

    if unknown:
        raise EvidenceCitationError(
            "Interpretation cited evidence IDs not supplied to this worker: "
            + ", ".join(unknown)
        )

    if input_ids and not cited:
        raise EvidenceCitationError(
            "Interpretation did not cite any supplied deterministic evidence."
        )

    if not input_ids and interpretation.findings:
        raise EvidenceCitationError(
            "Interpretation produced findings without supplied deterministic evidence."
        )

    if not input_ids and not interpretation.missing_evidence:
        raise EvidenceCitationError(
            "Interpretation without supplied evidence must state missing evidence."
        )

    return input_ids, cited


def _build_execution(response: LlmResponse) -> EvidenceInterpretationExecution:
    """Normalize route telemetry for a future specialist result-envelope adapter."""
    attempted_routes = response.metadata.get("attempted_routes")

    return EvidenceInterpretationExecution(
        agent_run_id=response.agent_run_id,
        executed_route=response.route_name,
        provider=response.provider,
        model=response.model,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        estimated_cost_usd=response.estimated_cost_usd,
        duration_ms=response.duration_ms,
        attempted_routes=(
            [str(item) for item in attempted_routes]
            if isinstance(attempted_routes, list)
            else [response.route_name]
        ),
    )


def _run_interpretation(
    request: InterpretationInput,
    *,
    prompt: str,
    purpose: str,
    response_model: type[InterpretationOutput],
    response_schema_name: str,
    agent_run_id: UUID | str,
    strict_external: bool,
) -> tuple[InterpretationOutput, list[str], list[str], EvidenceInterpretationExecution]:
    """Run one routed call and enforce structured-output and citation policy."""
    _require_caller_budget_scope()
    resolved_agent_run_id = UUID(str(agent_run_id))
    response = run_llm_task(
        route_name=EVIDENCE_INTERPRETATION_ROUTE,
        prompt=prompt,
        system_prompt=READ_ONLY_SYSTEM_PROMPT,
        context={
            "purpose": purpose,
            "input": request.model_dump(mode="json"),
            "policy": {
                "allowed_evidence_ids": [item.evidence_id for item in request.evidence],
                "read_only": True,
                "isolated_input_only": True,
                "missing_evidence_must_be_explicit": True,
            },
        },
        agent_run_id=resolved_agent_run_id,
        response_model=response_model,
        response_schema_name=response_schema_name,
    )

    if response.agent_run_id != resolved_agent_run_id:
        raise EvidenceInterpretationError(
            "LLM response correlation does not match the requested agent run ID."
        )

    fallback_reasons = _strict_fallback_reasons(response)

    if strict_external and fallback_reasons:
        raise EvidenceInterpretationFallbackError(
            "Strict external evidence interpretation rejected fallback: "
            + ", ".join(fallback_reasons)
        )

    if not response.structured_output:
        raise EvidenceInterpretationError(
            "Evidence interpretation requires validated structured model output."
        )

    interpretation = response_model.model_validate(response.structured_output)
    input_ids, cited_ids = _validate_citations(
        interpretation=interpretation,
        supplied_evidence=request.evidence,
    )
    execution = _build_execution(response)

    logger.info(
        "Evidence interpretation completed | purpose=%s agent_run_id=%s provider=%s model=%s evidence=%d citations=%d",
        purpose,
        response.agent_run_id,
        response.provider,
        response.model,
        len(input_ids),
        len(cited_ids),
    )

    return interpretation, input_ids, cited_ids, execution


# --- Defining Public Interpretation Functions
def interpret_dq_history(
    request: DqHistoryInterpretationInput,
    *,
    agent_run_id: UUID | str,
    strict_external: bool = True,
) -> DqHistoryInterpretationResult:
    """Interpret only supplied DQ and prior-incident history evidence."""
    interpretation, input_ids, cited_ids, execution = _run_interpretation(
        request,
        prompt=DQ_HISTORY_PROMPT,
        purpose="dq_history",
        response_model=DqHistoryInterpretationOutput,
        response_schema_name="dq_history_interpretation",
        agent_run_id=agent_run_id,
        strict_external=strict_external,
    )

    return DqHistoryInterpretationResult(
        input_evidence_ids=input_ids,
        cited_evidence_ids=cited_ids,
        interpretation=interpretation,
        execution=execution,
    )


def interpret_pipeline_run(
    request: PipelineRunInterpretationInput,
    *,
    agent_run_id: UUID | str,
    strict_external: bool = True,
) -> PipelineRunInterpretationResult:
    """Interpret only supplied pipeline execution evidence."""
    interpretation, input_ids, cited_ids, execution = _run_interpretation(
        request,
        prompt=PIPELINE_RUN_PROMPT,
        purpose="pipeline_run",
        response_model=PipelineRunInterpretationOutput,
        response_schema_name="pipeline_run_interpretation",
        agent_run_id=agent_run_id,
        strict_external=strict_external,
    )

    return PipelineRunInterpretationResult(
        input_evidence_ids=input_ids,
        cited_evidence_ids=cited_ids,
        interpretation=interpretation,
        execution=execution,
    )


def interpret_metadata_lineage_impact(
    request: MetadataLineageImpactInterpretationInput,
    *,
    agent_run_id: UUID | str,
    strict_external: bool = True,
) -> MetadataLineageImpactInterpretationResult:
    """Interpret only supplied metadata catalog and dbt lineage impact evidence."""
    interpretation, input_ids, cited_ids, execution = _run_interpretation(
        request,
        prompt=METADATA_LINEAGE_PROMPT,
        purpose="metadata_lineage_impact",
        response_model=MetadataLineageImpactInterpretationOutput,
        response_schema_name="metadata_lineage_impact_interpretation",
        agent_run_id=agent_run_id,
        strict_external=strict_external,
    )

    return MetadataLineageImpactInterpretationResult(
        input_evidence_ids=input_ids,
        cited_evidence_ids=cited_ids,
        interpretation=interpretation,
        execution=execution,
    )
