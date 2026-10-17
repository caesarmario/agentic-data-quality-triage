"""Public, versioned incident facts shared by triage and Copilot.

This module performs no I/O. ``trusted_source_metadata`` is a server-side seam
for validated catalog/contracts and read-only source snapshots, never request or
model output. Unknown provenance stays unknown. References are opaque IDs, not
URLs; arbitrary details, summaries, SQL, environment and source rows are excluded.
The bundle is decision support, not an executable recovery plan or approval.
"""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, model_validator


# --- Defining Public Contracts
MAX_BUNDLE_BYTES = 32_000
MAX_EXTRA_EVIDENCE_LOOPS = 2
MAX_SEGMENTS = 128
EVIDENCE_MAX_AGE = timedelta(minutes=10)
Layer = Literal["upstream", "landing", "raw", "mart"]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=320)]
Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:-]{1,160}$")]
Column = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")]
Fingerprint = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
_PRIVATE = re.compile(
    r"(?i)(://|[?&]|\$\{|\benv\b|environment|password|credential|secret|api[_ -]?key|"
    r"access[_ -]?token|authorization|bearer\s|sk-(?:proj-)?|eyJ[A-Za-z0-9_-]{12}|"
    r"scenario|fault[_ -]?manifest|ground[_ -]?truth|hidden[_ -]?oracle|"
    r"raw[_ -]?(?:rows|payload)|seed[_ -]?config|-----BEGIN|[\r\n\x00])"
)


def _get(value: Any, key: str, default: Any = None) -> Any:
    """Read a field without serializing unrelated or private object state."""
    return value.get(key, default) if isinstance(value, Mapping) else getattr(value, key, default)


def public_text(value: Any, default: str = "unavailable") -> str:
    """Return short public text, failing closed on private transport/content."""
    if not isinstance(value, str) or not value.strip() or len(value) > 320 or _PRIVATE.search(value):
        return default
    return value.strip()


def _check_public(value: Any) -> None:
    """Validate nested allowlisted values without logging rejected contents."""
    if isinstance(value, str) and _PRIVATE.search(value):
        raise ValueError("Private or unsafe incident context value")
    if isinstance(value, Mapping):
        for item in value.values():
            _check_public(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _check_public(item)


def _size(payload: dict[str, Any]) -> int:
    """Bound both normal ASCII JSON and compact UTF-8 transports conservatively."""
    return len(json.dumps(payload, ensure_ascii=True, allow_nan=False).encode("utf-8"))


class PublicModel(BaseModel):
    """Reject unknown fields and prevent arbitrary text from entering public facts."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_public_values(self) -> "PublicModel":
        _check_public(self.model_dump(mode="json"))
        return self


class AssetMetadata(PublicModel):
    """Catalog semantics, including the definition of the measured metric."""

    grain: Text | None = None
    keys: list[Column] = Field(default_factory=list, max_length=16)
    metric_definition: Text | None = None
    owner: Text | None = None


class ContractFingerprint(PublicModel):
    """Fingerprint of a public validated contract, never its private contents."""

    kind: Literal["dq", "schema", "metadata"]
    fingerprint: Fingerprint
    version: Identifier


class Segment(PublicModel):
    """Allowlisted aggregate business grain, not a raw record or business key."""

    country: Annotated[str, StringConstraints(pattern=r"^[A-Za-z]{2,3}$")]
    channel: Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,39}$")]


class SegmentCount(Segment):
    """Non-sensitive aggregate count at the explicitly allowlisted segment grain."""

    row_count: int = Field(ge=1, strict=True)


class LayerSnapshot(PublicModel):
    """Exact partition aggregates with observation provenance and source version."""

    layer: Layer
    table_name: Annotated[str, StringConstraints(pattern=r"^[A-Za-z_]\w*\.[A-Za-z_]\w*$")] | None = None
    partition_dt: date
    observed_at: datetime
    evidence_ref: Identifier
    source_version: Identifier | None = None
    row_count: int | None = Field(default=None, ge=0, strict=True)
    metric_value: float | None = None
    segments: list[Segment] | None = Field(default=None, max_length=MAX_SEGMENTS)

    @model_validator(mode="after")
    def validate_snapshot(self) -> "LayerSnapshot":
        if self.observed_at.tzinfo is None:
            raise ValueError("Snapshot timestamp requires a timezone")
        if self.segments is not None and len({_pair(s) for s in self.segments}) != len(self.segments):
            raise ValueError("Snapshot segments must be unique")
        return self


class PipelineLineage(PublicModel):
    """Versioned public pipeline edge from an observed lineage snapshot."""

    upstream: Identifier
    downstream: Identifier
    pipeline_id: Identifier
    version: Identifier
    observed_at: datetime
    evidence_ref: Identifier


class RunbookPrerequisite(PublicModel):
    """A prerequisite is unknown until an explicitly referenced check satisfies it."""

    name: Text
    satisfied: bool | None = None
    evidence_ref: Identifier | None = None


class RecoveryRunbook(PublicModel):
    """Descriptive allowlisted actions, never commands, DAG parameters or SQL."""

    runbook_id: Identifier | None = None
    eligibility: Literal["eligible", "not_indicated", "blocked", "unknown"] = "unknown"
    prerequisites: list[RunbookPrerequisite] = Field(default_factory=list, max_length=16)
    actions: list[Literal[
        "restore_verified_source", "reload_verified_partition", "rebuild_derived_partition", "revalidate_quality"
    ]] = Field(default_factory=list, max_length=4)
    required_permissions: list[Identifier] = Field(default_factory=list, max_length=16)
    granted_permissions: list[Identifier] = Field(default_factory=list, max_length=16)
    blockers: list[Text] = Field(default_factory=list, max_length=16)


class TrustedSourceMetadata(PublicModel):
    """Optional caller-owned metadata seam; bind every snapshot to this incident revision.

    Populate only from authenticated catalog/contracts/snapshot adapters. The
    builder deliberately does not discover source versions or access permissions.
    """

    incident_id: Identifier
    revision: Identifier
    table_name: Annotated[str, StringConstraints(pattern=r"^[A-Za-z_]\w*\.[A-Za-z_]\w*$")]
    partition_dt: date
    metadata: AssetMetadata = Field(default_factory=AssetMetadata)
    contracts: list[ContractFingerprint] = Field(default_factory=list, max_length=3)
    expected_segments: list[Segment] | None = Field(default=None, max_length=MAX_SEGMENTS)
    snapshots: list[LayerSnapshot] = Field(default_factory=list, max_length=4)
    pipeline_lineage: list[PipelineLineage] = Field(default_factory=list, max_length=16)
    runbook: RecoveryRunbook = Field(default_factory=RecoveryRunbook)


class EvidenceFact(PublicModel):
    """A bounded public observation reference; collection time is not event time."""

    reference: Identifier
    evidence_id: Identifier
    tool_name: Identifier
    collected_at: datetime | None = None
    observed_at: datetime | None = None
    current: bool = False
    observation: Text
    segment_counts: list[SegmentCount] | None = Field(default=None, max_length=MAX_SEGMENTS)


class LayerComparison(PublicModel):
    """Exact set differences; None means unknown, [] means verified empty."""

    layer: Layer
    evidence_ref: Identifier
    current: bool
    missing_segments: list[Segment] | None = None
    unexpected_segments: list[Segment] | None = None


class CrossLayerComparison(PublicModel):
    """Adjacent-layer comparisons do not equate raw rows with derived row grain."""

    left: Layer
    right: Layer
    evidence_refs: list[Identifier]
    left_row_count: int | None = None
    right_row_count: int | None = None
    left_metric_value: float | None = None
    right_metric_value: float | None = None
    left_only_segments: list[Segment] | None = None
    right_only_segments: list[Segment] | None = None


class IncidentContextBundle(PublicModel):
    """Version 1 public incident context, capped at 32,000 serialized bytes.

    ``context_complete`` concerns supplied facts, not permission to execute.
    Numeric ``heuristic_score`` is policy ranking, not statistical probability.
    Legacy confidence fields outside this contract remain backward compatible.
    """

    version: Literal["1.0"] = "1.0"
    incident_id: Identifier
    revision: Identifier | None
    table_name: Text | None
    partition_dt: date | None
    metric: Text | None
    built_at: datetime
    metadata: AssetMetadata
    contracts: list[ContractFingerprint]
    expected_segments: list[Segment] | None
    evidence: list[EvidenceFact]
    snapshots: list[LayerSnapshot]
    layer_comparisons: list[LayerComparison]
    cross_layer_comparisons: list[CrossLayerComparison]
    pipeline_lineage: list[PipelineLineage]
    runbook: RecoveryRunbook
    confirmed_observations: list[Text]
    likely_explanation: Text
    missing_evidence: list[Text]
    heuristic_score: float = Field(ge=0, le=1)
    score_semantics: Literal["heuristic_score_not_statistical_probability"] = "heuristic_score_not_statistical_probability"
    context_complete: bool
    recovery_recommendation: Text
    executable_plan: None = None
    approval_required: Literal[True] = True
    truncated: bool = False
    omitted_fields: list[Text] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_bounded_contract(self) -> "IncidentContextBundle":
        if _size(self.model_dump(mode="json")) > MAX_BUNDLE_BYTES:
            raise ValueError("Incident context exceeds 32000 serialized bytes")
        if self.context_complete != (not self.missing_evidence):
            raise ValueError("Context completeness must match missing evidence")
        return self

    def serialized_bytes(self) -> bytes:
        """Return the canonical bounded JSON transport; reject post-build mutation."""
        payload = self.model_dump(mode="json")
        self.__class__.model_validate(payload)
        return json.dumps(payload, ensure_ascii=True, allow_nan=False).encode("utf-8")


# --- Projecting Safe, Exact Facts
def _pair(segment: Segment) -> tuple[str, str]:
    return segment.country, segment.channel


def _segments(pairs: set[tuple[str, str]]) -> list[Segment]:
    return [Segment(country=c, channel=h) for c, h in sorted(pairs)]


def _timestamp(value: Any) -> datetime | None:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else None
    except (TypeError, ValueError):
        return None


def _current(value: datetime | None, now: datetime) -> bool:
    return value is not None and timedelta(0) <= now - value <= EVIDENCE_MAX_AGE


def _exact_aggregate(item: Any, table: str, dt: date | None) -> tuple[str, list[dict[str, Any]]] | None:
    """Recognize only static whole-partition aggregates used by guarded collectors."""
    if _get(item, "tool_name") != "clickhouse_sql" or not dt:
        return None
    query = str(_get(item, "query", "")).strip().rstrip(";")
    predicate = rf"FROM\s+{re.escape(table)}\s+WHERE\s+dt\s*=\s*(?:toDate\('{dt}'\)|'{dt}')"
    if re.fullmatch(rf"SELECT\s+count\(\s*\*?\s*\)(?:\s+AS\s+row_count)?\s+{predicate}\s+LIMIT\s+\d+", query, re.I):
        return "count", list(_get(item, "rows", []) or [])
    if re.fullmatch(
        rf"SELECT\s+country,\s*channel,\s*count\(\)\s+AS\s+row_count\s+{predicate}"
        rf"\s+GROUP\s+BY\s+country,\s*channel\s+ORDER\s+BY\s+country,\s*channel\s+LIMIT\s+{MAX_SEGMENTS + 1}",
        query, re.I,
    ):
        return "segments", list(_get(item, "rows", []) or [])
    return None


def _project_evidence(items: Sequence[Any], table: str, dt: date | None, now: datetime) -> list[EvidenceFact]:
    facts = []
    for index, item in enumerate(items[:24], 1):
        if _get(item, "tool_name") == "llm_router":
            continue
        try:
            tool = public_text(_get(item, "tool_name"))
            observed = None
            segment_counts = None
            observation = "Evidence reference retained; exact public facts are unavailable."
            aggregate = _exact_aggregate(item, table, dt)
            if aggregate and aggregate[0] == "count" and len(aggregate[1]) == 1:
                count = aggregate[1][0].get("row_count")
                if type(count) is int and count >= 0:
                    observed = _timestamp(_get(item, "created_at"))
                    observation = f"Partition aggregate for {table} on {dt}: row_count={count}."
            elif aggregate and aggregate[0] == "segments" and len(aggregate[1]) <= MAX_SEGMENTS:
                segment_counts = [SegmentCount(country=r["country"], channel=r["channel"], row_count=r["row_count"]) for r in aggregate[1]]
                if len({_pair(s) for s in segment_counts}) != len(segment_counts):
                    continue
                observed = _timestamp(_get(item, "created_at"))
                observation = f"Exact country/channel aggregate for {table} on {dt}; {len(aggregate[1])} segments."
            facts.append(EvidenceFact(
                reference=f"E{index}", evidence_id=_get(item, "evidence_id", f"E{index}"),
                tool_name=tool, collected_at=_timestamp(_get(item, "created_at")),
                observed_at=observed, current=_current(observed, now), observation=observation,
                segment_counts=segment_counts,
            ))
        except (ValueError, TypeError, AttributeError, KeyError):
            continue
    return facts


def _merge_guarded_snapshots(source: TrustedSourceMetadata, evidence: Sequence[Any]) -> TrustedSourceMetadata:
    """Merge only exact aggregate observations for explicitly mapped warehouse layers."""
    snapshots = []
    for snapshot in source.snapshots:
        replacement = snapshot
        if snapshot.layer in {"raw", "mart"} and snapshot.table_name:
            for item in evidence:
                aggregate = _exact_aggregate(item, snapshot.table_name, source.partition_dt)
                observed = _timestamp(_get(item, "created_at"))
                if not aggregate or aggregate[0] != "segments" or not observed or observed < replacement.observed_at:
                    continue
                rows = aggregate[1]
                if len(rows) > MAX_SEGMENTS:
                    continue  # LIMIT sentinel means the complete set is unknown, not empty.
                try:
                    if any(type(r.get("row_count")) is not int or r["row_count"] <= 0 for r in rows):
                        continue
                    segments = [Segment(country=r["country"], channel=r["channel"]) for r in rows]
                    replacement = LayerSnapshot.model_validate({
                        **snapshot.model_dump(), "observed_at": observed,
                        "evidence_ref": _get(item, "evidence_id"),
                        "row_count": sum(r["row_count"] for r in rows), "segments": segments,
                        # Collection cannot prove which source version produced today's rows.
                        "source_version": snapshot.source_version if observed == snapshot.observed_at else None,
                    })
                except (KeyError, ValueError, TypeError):
                    continue
        snapshots.append(replacement)
    return source.model_copy(update={"snapshots": snapshots})


def _comparisons(source: TrustedSourceMetadata, now: datetime) -> tuple[list[LayerComparison], list[CrossLayerComparison]]:
    expected = None if source.expected_segments is None else {_pair(s) for s in source.expected_segments}
    layers = {s.layer: s for s in source.snapshots}
    comparisons = []
    for snapshot in source.snapshots:
        actual = None if snapshot.segments is None else {_pair(s) for s in snapshot.segments}
        known = actual is not None and expected is not None
        comparisons.append(LayerComparison(
            layer=snapshot.layer, evidence_ref=snapshot.evidence_ref, current=_current(snapshot.observed_at, now),
            missing_segments=_segments(expected - actual) if known else None,
            unexpected_segments=_segments(actual - expected) if known else None,
        ))
    cross = []
    for left, right in (("upstream", "landing"), ("landing", "raw"), ("raw", "mart")):
        a, b = layers.get(left), layers.get(right)
        if not a or not b or not _current(a.observed_at, now) or not _current(b.observed_at, now):
            continue
        known = a.segments is not None and b.segments is not None
        sa, sb = {_pair(s) for s in a.segments or []}, {_pair(s) for s in b.segments or []}
        cross.append(CrossLayerComparison(
            left=left, right=right, evidence_refs=[a.evidence_ref, b.evidence_ref],
            left_row_count=a.row_count, right_row_count=b.row_count,
            left_metric_value=a.metric_value, right_metric_value=b.metric_value,
            left_only_segments=_segments(sa - sb) if known else None,
            right_only_segments=_segments(sb - sa) if known else None,
        ))
    return comparisons, cross


def build_incident_context_bundle(
    alert: Any = None,
    evidence: Sequence[Any] | None = None,
    *,
    incident_id: str | None = None,
    revision: str | int | None = None,
    trusted_source_metadata: TrustedSourceMetadata | Mapping[str, Any] | None = None,
    top_hypothesis: Any = None,
    now: datetime | None = None,
) -> IncidentContextBundle:
    """Build safe shared context without guessing facts or performing tool calls.

    Args:
        alert: Existing Alert or public alert mapping; details are never copied.
        evidence: Guarded EvidenceItems. Arbitrary rows and summaries are excluded.
        incident_id: Authoritative incident identity; defaults to Alert UUID/ref.
        revision: Authoritative revision. Unknown revisions block recovery readiness.
        trusted_source_metadata: Optional authenticated server-side snapshot adapter.
        top_hypothesis: Ranking input only, never confirmed evidence.
        now: Aware UTC clock override for deterministic tests.

    Returns:
        Typed bounded context. Missing, stale, mismatched or omitted critical facts
        are explicit; even a complete bundle contains no executable recovery plan.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Bundle clock requires a timezone")
    identity = public_text(str(incident_id or _get(alert, "alert_id") or _get(alert, "alert_display_id") or "unselected"))
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", identity):
        identity = "unselected"
    rev = str(revision) if revision is not None else None
    if rev is not None and (public_text(rev) != rev or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", rev)):
        rev = None
    table = public_text(_get(alert, "table_name"), "")
    metric = public_text(_get(alert, "metric"), "")
    try:
        raw_dt = _get(alert, "dt")
        dt = raw_dt if type(raw_dt) is date else date.fromisoformat(str(raw_dt))
    except (ValueError, TypeError):
        dt = None
    items = list(evidence or [])
    input_truncated = len(items) > 24
    items = items[:24]
    facts = _project_evidence(items, table, dt, now)
    gaps = []
    source = None
    if trusted_source_metadata is not None:
        try:
            source = TrustedSourceMetadata.model_validate(
                trusted_source_metadata.model_dump() if isinstance(trusted_source_metadata, BaseModel) else trusted_source_metadata
            )
            if (source.incident_id, source.revision, source.table_name, source.partition_dt) != (identity, rev, table, dt):
                source = None
                gaps.append("trusted_source_scope_or_revision_mismatch")
            elif len({s.layer for s in source.snapshots}) != len(source.snapshots) or any(s.partition_dt != dt for s in source.snapshots):
                source = None
                gaps.append("ambiguous_or_cross_partition_snapshots")
        except (ValidationError, ValueError, TypeError):
            gaps.append("trusted_source_metadata_invalid")
    if source is None:
        source = TrustedSourceMetadata(
            incident_id=identity, revision=rev or "unknown", table_name=table if re.fullmatch(r"[A-Za-z_]\w*\.[A-Za-z_]\w*", table) else "unknown.unknown",
            partition_dt=dt or now.date(),
        )
    source = _merge_guarded_snapshots(source, items)
    score = _get(top_hypothesis, "confidence", 0.0)
    score = float(score) if isinstance(score, (int, float)) and not isinstance(score, bool) and math.isfinite(score) else 0.0
    explanation = public_text(_get(top_hypothesis, "title"), "No supported leading explanation is available.")
    omitted = ["evidence_references"] if input_truncated else []
    while True:
        missing = list(gaps)
        for condition, name in (
            (identity != "unselected", "incident_identity"), (bool(rev), "incident_revision"),
            (bool(table) and dt is not None, "incident_partition_scope"),
            (bool(source.metadata.grain), "metadata_grain"), (bool(source.metadata.keys), "metadata_keys"),
            (bool(source.metadata.metric_definition), "metric_definition"), (bool(source.metadata.owner), "metadata_owner"),
            ({c.kind for c in source.contracts} >= {"dq", "schema", "metadata"}, "contract_fingerprints"),
            (bool(source.expected_segments), "exact_expected_segments"),
            (bool(source.pipeline_lineage), "pipeline_lineage"),
            (bool(source.runbook.runbook_id and source.runbook.actions and source.runbook.prerequisites), "runbook_prerequisites_and_actions"),
            (bool(source.runbook.required_permissions), "required_permissions"),
        ):
            if not condition:
                missing.append(name)
        for layer in ("upstream", "landing", "raw", "mart"):
            snapshot = next((s for s in source.snapshots if s.layer == layer), None)
            if not snapshot:
                missing.append(f"{layer}_snapshot")
                continue
            if not _current(snapshot.observed_at, now):
                missing.append(f"{layer}_current_observation")
            if snapshot.source_version is None:
                missing.append(f"{layer}_source_version")
            if snapshot.segments is None:
                missing.append(f"{layer}_exact_segments")
            if snapshot.row_count is None or snapshot.metric_value is None:
                missing.append(f"{layer}_count_and_metric")
        if any(not _current(_timestamp(e.observed_at), now) for e in source.pipeline_lineage):
            missing.append("current_pipeline_lineage")
        if len({c.kind for c in source.contracts}) != len(source.contracts):
            missing.append("ambiguous_contract_fingerprints")
        if source.expected_segments is not None and len({_pair(s) for s in source.expected_segments}) != len(source.expected_segments):
            missing.append("ambiguous_expected_segments")
        if any(p.satisfied is not True or not p.evidence_ref for p in source.runbook.prerequisites):
            missing.append("unverified_runbook_prerequisites")
        if set(source.runbook.required_permissions) - set(source.runbook.granted_permissions):
            missing.append("permissions_not_granted")
        if source.runbook.blockers:
            missing.append("runbook_blockers_present")
        missing.extend(f"omitted:{name}" for name in omitted)
        comparisons, cross = _comparisons(source, now)
        observations = [f.observation for f in facts if f.current]
        observations.extend(f"{s.layer} snapshot [{s.evidence_ref}] observed {s.observed_at.isoformat()} for {s.partition_dt}." for s in source.snapshots if _current(s.observed_at, now))
        current_explanation = explanation
        if not missing and source.runbook.eligibility == "not_indicated":
            observations.insert(0, "Current warehouse business values match the trusted source and derived aggregates; this is not proof that every quality rule passes.")
            current_explanation = (
                "Current source and warehouse business values agree. Earlier detection: "
                + explanation[:170] + ". The incident still needs scoped revalidation."
            )
        if missing:
            recommendation = "Collect the missing exact evidence before proposing bounded manual recovery for this incident partition."
        elif source.runbook.eligibility == "not_indicated":
            recommendation = (
                "Do not replay or rebuild this partition: no current source/warehouse mismatch justifies it. "
                "Run a fresh read-only check of this incident rule; request closure only if it passes."
            )
        elif source.runbook.eligibility == "eligible":
            recommendation = (
                f"Review {source.runbook.runbook_id} for this partition and pinned source version. "
                "Human approval and a separate execution confirmation are required; revalidate afterward."
            )
        else:
            recommendation = "No eligible repair is established. Review current recovery eligibility before proposing any data-changing action."
        payload = dict(
            incident_id=identity, revision=rev, table_name=table or None, partition_dt=dt, metric=metric or None,
            built_at=now, metadata=source.metadata, contracts=source.contracts, expected_segments=source.expected_segments,
            evidence=facts, snapshots=source.snapshots, layer_comparisons=comparisons,
            cross_layer_comparisons=cross, pipeline_lineage=source.pipeline_lineage, runbook=source.runbook,
            confirmed_observations=observations[:16], likely_explanation=current_explanation,
            missing_evidence=list(dict.fromkeys(missing)), heuristic_score=max(0.0, min(score, 1.0)),
            context_complete=not missing,
            recovery_recommendation=recommendation, truncated=bool(omitted), omitted_fields=omitted,
        )
        # Validate only after pruning whole facts; partial exact sets must never masquerade as complete.
        transport = {k: v for k, v in payload.items()}
        encoded = json.loads(json.dumps(transport, default=lambda o: o.model_dump(mode="json") if isinstance(o, BaseModel) else o.isoformat()))
        if _size(encoded) <= MAX_BUNDLE_BYTES - 1_000:
            return IncidentContextBundle(**payload)
        if source.snapshots:
            removed = source.snapshots[-1]
            omitted.append(f"{removed.layer}_snapshot")
            source = source.model_copy(update={"snapshots": source.snapshots[:-1]})
        elif facts:
            omitted.append("evidence_references")
            facts = facts[:len(facts) // 2]
        elif source.pipeline_lineage:
            omitted.append("pipeline_lineage")
            source = source.model_copy(update={"pipeline_lineage": []})
        else:
            omitted.append("expected_segments")
            source = source.model_copy(update={"expected_segments": None})


def render_incident_context(bundle: IncidentContextBundle, language: str = "en") -> str:
    """Present observations, hypotheses and gaps without a statistical confidence claim."""
    bundle = IncidentContextBundle.model_validate(bundle.model_dump(mode="json"))
    labels = ("Confirmed observations", "Likely explanation", "Missing evidence") if language == "en" else ("Observasi terkonfirmasi", "Penjelasan yang mungkin", "Bukti yang belum tersedia")
    return (
        f"{labels[0]}\n" + "\n".join(f"- {o}" for o in bundle.confirmed_observations or ["No current exact observation supplied."])
        + f"\n\n{labels[1]}\n{bundle.likely_explanation}\n"
        + f"heuristic_score={bundle.heuristic_score:.2f} (not statistical probability).\n"
        + f"\n{labels[2]}\n" + ", ".join(bundle.missing_evidence or ["None in the supplied bounded context."])
        + f"\n{bundle.recovery_recommendation} No executable plan; explicit human approval remains required."
    )
