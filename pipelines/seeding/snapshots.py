"""Immutable, bounded orders snapshots. Recovery never regenerates historical data.

Only infrastructure code should pass frames around; manifests contain no evaluation
labels, configuration, credentials, or ground truth. Inject an S3 client for tests.
"""
from __future__ import annotations

# --- Importing Libraries
import hashlib
import io
import json
import math
import os
import re
from datetime import date
from pathlib import Path
from typing import Any, Literal, Mapping

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pipelines.common.logging import logger


# --- Defining Contracts And Limits
SNAPSHOT_BUCKET = "dq-landing"
SNAPSHOT_PREFIX = "orders/snapshots"
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
MAX_DECODED_BYTES = 128 * 1024 * 1024
MAX_SNAPSHOT_ROWS = 250_000
MAX_MANIFEST_BYTES = 8192
SOURCE_FRAME_ATTR = "source_snapshot_frame"
SnapshotKind = Literal["upstream", "landing"]

SOURCE_SCHEMA = pa.schema([
    ("dt", pa.date32()), ("order_id", pa.string()), ("order_date", pa.date32()),
    ("order_ts", pa.timestamp("us", tz="UTC")),
    ("ingestion_ts", pa.timestamp("us", tz="UTC")),
    ("customer_id", pa.string()), ("country", pa.string()), ("channel", pa.string()),
    ("status", pa.string()), ("currency", pa.string()),
    ("gross_amount_local", pa.float64()), ("fx_rate_to_usd", pa.float64()),
    ("gross_amount_usd", pa.float64()), ("discount_usd", pa.float64()),
    ("refund_amount_usd", pa.float64()), ("recognized_revenue_usd", pa.float64()),
    ("source_system", pa.string()), ("is_test", pa.bool_()),
    ("business_date_version", pa.int64()),
])
LANDING_SCHEMA = SOURCE_SCHEMA.append(pa.field("incident_scenario", pa.string()))


class SnapshotValidationError(ValueError):
    """A snapshot failed a deterministic guard; messages never include raw data."""


def _canonical_json(value: Any) -> bytes:
    """Encode stable JSON without whitespace, non-finite values, or timestamps."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha256(payload: bytes) -> str:
    """Hash the actual object bytes, never an ETag or object metadata field."""
    return hashlib.sha256(payload).hexdigest()


def schema_fingerprint(schema: pa.Schema) -> str:
    """Hash ordered Arrow fields without pandas/attrs/evaluation metadata."""
    return _sha256(_canonical_json([
        {"name": field.name, "type": str(field.type), "nullable": field.nullable}
        for field in schema
    ]))


def _schema(kind: SnapshotKind) -> pa.Schema:
    """Return the supported version-one schema for an allowlisted kind."""
    if kind not in ("upstream", "landing"):
        raise SnapshotValidationError("Unsupported snapshot kind")
    return SOURCE_SCHEMA if kind == "upstream" else LANDING_SCHEMA


def _object_key(kind: SnapshotKind, dt: date, version: int, sha256: str) -> str:
    """Bind the data address to kind, partition, version, and exact bytes."""
    return f"{SNAPSHOT_PREFIX}/{kind}/dt={dt.isoformat()}/v={version}/{sha256}.parquet"


class SourceSnapshotManifest(BaseModel):
    """Frozen PG/public contract; snapshot_id hashes all other manifest fields.

    version is the supported serialization/schema contract version (currently 1).
    correlation_id is a caller-supplied opaque pipeline ID, not free-form metadata.
    Both kinds use this contract; only upstream is eligible for recovery replay.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    dataset: Literal["orders"] = "orders"
    kind: SnapshotKind
    snapshot_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    dt: date
    bucket: Literal["dq-landing"] = SNAPSHOT_BUCKET
    key: str = Field(max_length=256)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    schema_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(gt=0, le=MAX_SNAPSHOT_BYTES, strict=True)
    row_count: int = Field(ge=0, le=MAX_SNAPSHOT_ROWS, strict=True)
    version: Literal[1] = 1
    correlation_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_.:+=-]+$")

    @field_validator("dt", mode="before")
    @classmethod
    def validate_date(cls, value: Any) -> date:
        """Disallow implicit timestamp-to-date scope coercion."""
        return _date(value)

    @field_validator("version", mode="before")
    @classmethod
    def validate_version(cls, value: Any) -> int:
        """Reject bool/string aliases for the serialization contract version."""
        if type(value) is not int or value != 1:
            raise ValueError("Unsupported snapshot version")
        return value

    @model_validator(mode="after")
    def validate_identity(self) -> "SourceSnapshotManifest":
        """Reject forged addresses, unsupported schemas, and modified manifests."""
        if self.key != _object_key(self.kind, self.dt, self.version, self.sha256):
            raise ValueError("Snapshot key does not match scope/content")
        if self.schema_sha256 != schema_fingerprint(_schema(self.kind)):
            raise ValueError("Unsupported snapshot schema")
        identity = self.model_dump(mode="json", exclude={"snapshot_id"})
        if self.snapshot_id != _sha256(_canonical_json(identity)):
            raise ValueError("Snapshot manifest identity mismatch")
        return self

    @property
    def manifest_key(self) -> str:
        """Return the immutable JSON address; the registry stores this full model."""
        return f"{SNAPSHOT_PREFIX}/manifests/{self.snapshot_id}.json"

    @property
    def manifest_uri(self) -> str:
        """Return an unsigned locator without credentials or query parameters."""
        return f"s3://{self.bucket}/{self.manifest_key}"


# --- Validating And Serializing Frames
def _date(value: date | str) -> date:
    """Accept only a business date, never a timestamp or ambiguous date string."""
    if type(value) is date:
        return value
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return date.fromisoformat(value)
    raise SnapshotValidationError("Expected an ISO business date")


def _frame_table(frame: pd.DataFrame, kind: SnapshotKind) -> pa.Table:
    """Build the explicit schema without serializing DataFrame attrs or index."""
    schema = _schema(kind)
    if list(frame.columns) != schema.names or len(frame) > MAX_SNAPSHOT_ROWS:
        raise SnapshotValidationError("Snapshot columns or row bound invalid")
    clean = frame.copy(deep=False)
    clean.attrs = {}
    try:
        table = pa.Table.from_pandas(clean, schema=schema, preserve_index=False, safe=True)
    except (pa.ArrowException, TypeError, ValueError):
        raise SnapshotValidationError("Snapshot field types invalid") from None
    if table.nbytes > MAX_DECODED_BYTES:
        raise SnapshotValidationError("Snapshot decoded size exceeds limit")
    return table.replace_schema_metadata(None)


def _validate_partition(frame: pd.DataFrame, dt: date) -> None:
    """Require every row to belong to the requested business partition."""
    for column in ("dt", "order_date"):
        if frame[column].isna().any() or not frame[column].eq(dt).all():
            raise SnapshotValidationError("Snapshot business date scope mismatch")


def validate_recovery_source(frame: pd.DataFrame, dt: date | str) -> pd.DataFrame:
    """Validate replay structure and required source values; return an attrs-free copy.

    Checks include nonempty/unique identities, required nonnull/nonblank fields,
    finite nonnegative amounts, positive FX/version, status and revenue consistency,
    and timestamp/partition scope. This is deterministic, not a warehouse DQ run.
    Raises SnapshotValidationError without echoing cell values on any failure.
    """
    table = _frame_table(frame, "upstream")
    clean = table.to_pandas(ignore_metadata=True)
    if clean.empty or clean.isna().any().any():
        raise SnapshotValidationError("Recovery source is empty or has missing required values")
    _validate_partition(clean, _date(dt))
    if clean["order_id"].duplicated().any():
        raise SnapshotValidationError("Recovery source has duplicate order identities")
    for field in SOURCE_SCHEMA:
        if pa.types.is_string(field.type) and clean[field.name].str.strip().eq("").any():
            raise SnapshotValidationError("Recovery source has blank required values")
        if pa.types.is_floating(field.type):
            if not clean[field.name].map(math.isfinite).all() or clean[field.name].lt(0).any():
                raise SnapshotValidationError("Recovery source has invalid numeric values")
    if clean["fx_rate_to_usd"].le(0).any() or clean["business_date_version"].le(0).any():
        raise SnapshotValidationError("Recovery source has invalid FX or business version")
    if not clean["status"].isin(["paid", "cancelled", "pending", "refunded"]).all():
        raise SnapshotValidationError("Recovery source has unsupported status")
    if not clean["order_ts"].dt.date.eq(_date(dt)).all():
        raise SnapshotValidationError("Recovery source event date mismatch")
    if clean["ingestion_ts"].lt(clean["order_ts"]).any():
        raise SnapshotValidationError("Recovery source ingestion precedes event")
    net = (clean["gross_amount_usd"] - clean["discount_usd"]).clip(lower=0)
    expected_revenue = net.where(clean["status"].eq("paid"), 0)
    expected_refund = net.where(clean["status"].eq("refunded"), 0)
    if ((clean["recognized_revenue_usd"] - expected_revenue).abs().gt(0.011).any()
            or (clean["refund_amount_usd"] - expected_refund).abs().gt(0.011).any()):
        raise SnapshotValidationError("Recovery source revenue/status consistency invalid")
    clean.attrs = {}
    return clean


def serialize_snapshot_frame(frame: pd.DataFrame, kind: SnapshotKind, *, compression: str | None = "snappy") -> bytes:
    """Serialize canonical metadata-free Parquet, including typed empty landings."""
    table = _frame_table(frame, kind)
    buffer = io.BytesIO()
    pq.write_table(table, buffer, compression=compression, version="2.6", row_group_size=65536)
    payload = buffer.getvalue()
    if len(payload) > MAX_SNAPSHOT_BYTES:
        raise SnapshotValidationError("Snapshot encoded size exceeds limit")
    return payload


def _decode(payload: bytes, kind: SnapshotKind, dt: date) -> pd.DataFrame:
    """Inspect footer/schema/decoded bounds before materializing Parquet rows."""
    if not 0 < len(payload) <= MAX_SNAPSHOT_BYTES:
        raise SnapshotValidationError("Snapshot byte size invalid")
    try:
        parquet = pq.ParquetFile(io.BytesIO(payload))
        metadata = parquet.metadata
        if not parquet.schema_arrow.equals(_schema(kind), check_metadata=False):
            raise SnapshotValidationError("Snapshot schema mismatch")
        decoded_size = sum(metadata.row_group(i).total_byte_size for i in range(metadata.num_row_groups))
        if metadata.num_rows > MAX_SNAPSHOT_ROWS or decoded_size > MAX_DECODED_BYTES:
            raise SnapshotValidationError("Snapshot decoded bounds exceeded")
        table = parquet.read().replace_schema_metadata(None)
        if table.nbytes > MAX_DECODED_BYTES:
            raise SnapshotValidationError("Snapshot decoded bounds exceeded")
        frame = table.to_pandas(ignore_metadata=True)
    except SnapshotValidationError:
        raise
    except (pa.ArrowException, OSError, ValueError, TypeError):
        raise SnapshotValidationError("Invalid snapshot Parquet") from None
    _validate_partition(frame, dt)
    return validate_recovery_source(frame, dt) if kind == "upstream" else frame


# --- Bounded Storage And Immutable Publication
def _client(s3_client: Any = None) -> Any:
    """Resolve storage lazily; validation/imports never touch runtime credentials."""
    if s3_client is not None:
        return s3_client
    from pipelines.seeding.upload_to_s3 import build_s3_client
    return build_s3_client()


def _missing(exc: Exception) -> bool:
    """Only a definite missing object permits an immutable create attempt."""
    return getattr(exc, "response", {}).get("Error", {}).get("Code") in {"NoSuchKey", "404", "NotFound"}


def _read_object(client: Any, bucket: str, key: str, limit: int) -> bytes:
    """Read at most limit+1 bytes, checking advertised length and closing streams."""
    response = client.get_object(Bucket=bucket, Key=key)
    body = response["Body"]
    try:
        length = response.get("ContentLength")
        if type(length) is not int or not 0 <= length <= limit:
            raise SnapshotValidationError("Object advertised size invalid")
        chunks = []
        count = 0
        while count <= length:
            chunk = body.read(min(65536, length + 1 - count))
            if not chunk:
                break
            count += len(chunk)
            if count > length:
                raise SnapshotValidationError("Object actual size mismatch")
            chunks.append(chunk)
        if count != length:
            raise SnapshotValidationError("Object actual size mismatch")
        return b"".join(chunks)
    finally:
        body.close()


def _put_immutable(client: Any, key: str, payload: bytes, content_type: str) -> None:
    """Create-only write plus full byte verification, including races and retries.

    If a gateway cannot honor IfNoneMatch, fail closed. Never fall back to an
    unconditional PUT, overwrite an existing object, or trust an ETag.
    """
    try:
        existing = _read_object(client, SNAPSHOT_BUCKET, key, len(payload))
    except Exception as exc:
        if not _missing(exc):
            raise
    else:
        if existing != payload:
            raise SnapshotValidationError("Immutable object content conflict")
        return
    try:
        client.put_object(Bucket=SNAPSHOT_BUCKET, Key=key, Body=payload,
                          ContentType=content_type, IfNoneMatch="*")
    except Exception as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code")
        if code not in {"PreconditionFailed", "412", "ConditionalRequestConflict", "409"}:
            raise
    if _read_object(client, SNAPSHOT_BUCKET, key, len(payload)) != payload:
        raise SnapshotValidationError("Immutable upload verification failed")


def _manifest(value: SourceSnapshotManifest | Mapping[str, Any]) -> SourceSnapshotManifest:
    """Revalidate even model instances; model_copy/model_construct are not trusted."""
    try:
        values = value.model_dump(mode="json") if isinstance(value, SourceSnapshotManifest) else dict(value)
        if len(_canonical_json(values)) > MAX_MANIFEST_BYTES:
            raise ValueError("Manifest too large")
        return SourceSnapshotManifest.model_validate(values)
    except (ValueError, TypeError, OverflowError):
        raise SnapshotValidationError("Invalid source snapshot manifest") from None


def _register(manifest: SourceSnapshotManifest) -> None:
    """Register verified snapshots only; configured control DB failures are fatal."""
    if not os.getenv("DQ_CONTROL_DATABASE_URL", "").strip():
        return
    from agent.recovery.store import register_source_snapshot
    register_source_snapshot(manifest.model_dump(mode="json"))


def publish_snapshot(
    payload: bytes, *, dt: date | str, kind: SnapshotKind, correlation_id: str,
    s3_client: Any = None, version: int = 1,
) -> SourceSnapshotManifest:
    """Validate exact bytes, publish data+JSON create-only, then register in PG.

    No bucket creation, historical generation, legacy-key mutation, or DB work
    occurs before content validation. On PG failure retry the same publication.
    """
    run_dt = _date(dt)
    frame = _decode(payload, kind, run_dt)
    checksum = _sha256(payload)
    values = dict(dataset="orders", kind=kind, dt=run_dt.isoformat(), bucket=SNAPSHOT_BUCKET,
                  key=_object_key(kind, run_dt, version, checksum), sha256=checksum,
                  schema_sha256=schema_fingerprint(_schema(kind)), size_bytes=len(payload),
                  row_count=len(frame), version=version, correlation_id=correlation_id)
    manifest = _manifest({**values, "snapshot_id": _sha256(_canonical_json(values))})
    client = _client(s3_client)
    _put_immutable(client, manifest.key, payload, "application/vnd.apache.parquet")
    _put_immutable(client, manifest.manifest_key, _canonical_json(manifest.model_dump(mode="json")), "application/json")
    _register(manifest)
    logger.info("Snapshot published | kind=%s dt=%s snapshot_id=%s rows=%d bytes=%d",
                kind, run_dt, manifest.snapshot_id, len(frame), len(payload))
    return manifest


def publish_source_snapshot(frame: pd.DataFrame, *, dt: date | str, correlation_id: str,
                            s3_client: Any = None) -> SourceSnapshotManifest:
    """Publish a captured pre-fault source, never a regenerated historical source."""
    clean = validate_recovery_source(frame, dt)
    return publish_snapshot(serialize_snapshot_frame(clean, "upstream"), dt=dt, kind="upstream",
                            correlation_id=correlation_id, s3_client=s3_client)


def publish_landing_snapshot(payload: bytes, *, dt: date | str, correlation_id: str,
                             s3_client: Any = None) -> SourceSnapshotManifest:
    """Publish the exact legacy landing bytes, preserving actual injected faults."""
    return publish_snapshot(payload, dt=dt, kind="landing", correlation_id=correlation_id, s3_client=s3_client)


def load_source_manifest(manifest: SourceSnapshotManifest | Mapping[str, Any] | str,
                         s3_client: Any = None, *, dt: date | str | None = None,
                         kind: SnapshotKind = "upstream") -> SourceSnapshotManifest:
    """Load a pinned model/dict or allowlisted immutable manifest URI; never latest.

    dt optionally binds a registry lookup to the incident partition. Raw JSON and
    arbitrary keys/URLs are not accepted; caller must pin the resulting identity.
    """
    expected_id = None
    if isinstance(manifest, str):
        match = re.fullmatch(r"s3://dq-landing/orders/snapshots/manifests/([a-f0-9]{64})\.json", manifest)
        if match is None:
            raise SnapshotValidationError("Manifest URI outside allowlisted scope")
        expected_id = match.group(1)
        payload = _read_object(_client(s3_client), SNAPSHOT_BUCKET,
                               f"{SNAPSHOT_PREFIX}/manifests/{expected_id}.json", MAX_MANIFEST_BYTES)
        try:
            manifest = json.loads(payload)
        except (ValueError, UnicodeError):
            raise SnapshotValidationError("Invalid snapshot manifest JSON") from None
    result = _manifest(manifest)
    if result.kind != kind or (dt is not None and result.dt != _date(dt)):
        raise SnapshotValidationError("Manifest kind/date scope mismatch")
    if expected_id is not None and result.snapshot_id != expected_id:
        raise SnapshotValidationError("Manifest address checksum mismatch")
    if expected_id is not None and payload != _canonical_json(result.model_dump(mode="json")):
        raise SnapshotValidationError("Manifest bytes are not canonical")
    return result


def read_pinned_snapshot(manifest: SourceSnapshotManifest | Mapping[str, Any] | str,
                         s3_client: Any = None) -> pd.DataFrame:
    """Return a validated upstream frame from pinned immutable bytes, not legacy/latest."""
    return read_observed_snapshot(manifest, s3_client, kind="upstream")


def read_observed_snapshot(manifest: SourceSnapshotManifest | Mapping[str, Any] | str,
                           s3_client: Any = None, *, kind: SnapshotKind) -> pd.DataFrame:
    """Read a verified observed layer. Landing evidence is not a recovery source."""
    pinned = load_source_manifest(manifest, s3_client, kind=kind)
    client = _client(s3_client)
    payload = _read_object(client, pinned.bucket, pinned.key, pinned.size_bytes)
    if len(payload) != pinned.size_bytes or _sha256(payload) != pinned.sha256:
        raise SnapshotValidationError("Snapshot size/checksum mismatch")
    frame = _decode(payload, kind, pinned.dt)
    if len(frame) != pinned.row_count:
        raise SnapshotValidationError("Snapshot row count mismatch")
    return frame


def register_corrected_file(*, bucket: str, key: str, dt: date | str, correlation_id: str,
                             s3_client: Any = None, expected_sha256: str | None = None) -> SourceSnapshotManifest:
    """Validate an existing orders partition object and pin its exact corrected bytes.

    Only dq-landing/orders/dt=DATE/*.parquet is accepted (no nested paths, URLs,
    traversal, query strings, or other partitions). Corrected files must use the
    upstream schema, without incident_scenario or any other evaluation columns.
    This function never generates data or replaces the original object.
    """
    run_dt = _date(dt)
    pattern = rf"orders/dt={re.escape(run_dt.isoformat())}/[A-Za-z0-9_-][A-Za-z0-9_.-]*\.parquet"
    if bucket != SNAPSHOT_BUCKET or re.fullmatch(pattern, key) is None or ".." in key:
        raise SnapshotValidationError("Corrected file outside allowlisted bucket/partition")
    client = _client(s3_client)
    payload = _read_object(client, bucket, key, MAX_SNAPSHOT_BYTES)
    if expected_sha256 is not None and _sha256(payload) != expected_sha256:
        raise SnapshotValidationError("Corrected file checksum mismatch")
    return publish_snapshot(payload, dt=run_dt, kind="upstream", correlation_id=correlation_id, s3_client=client)


# --- Local Capture Handoff (Not A Recovery Source)
def capture_receipt_path(landing_path: str | Path) -> Path:
    """Locate the small local receipt joining pre-fault bytes to actual landing bytes."""
    return Path(str(landing_path) + ".snapshots.json")


def write_capture_receipt(source_frame: pd.DataFrame, landing_path: Path, *, dt: date,
                          correlation_id: str, landing_sha256: str) -> dict[str, Any]:
    """Persist the captured source sibling and a checksum-bound handoff receipt."""
    if _sha256(read_local_snapshot(landing_path)) != landing_sha256:
        raise SnapshotValidationError("Landing changed during source capture")
    source = serialize_snapshot_frame(validate_recovery_source(source_frame, dt), "upstream")
    source_sha = _sha256(source)
    path = landing_path.with_name(f"source-{source_sha}.parquet")
    try:
        with path.open("xb") as stream:
            stream.write(source)
    except FileExistsError:
        if read_local_snapshot(path) != source:
            raise SnapshotValidationError("Local source capture content conflict")
    receipt = dict(dt=dt.isoformat(), source_sha256=source_sha,
                   landing_sha256=landing_sha256, correlation_id=correlation_id)
    receipt_path = capture_receipt_path(landing_path)
    temporary = receipt_path.with_suffix(".tmp")
    temporary.write_bytes(_canonical_json(receipt))
    temporary.replace(receipt_path)
    return {"source_local_path": str(path), "source_sha256": source_sha,
            "snapshot_capture_path": str(receipt_path), "correlation_id": correlation_id}


def read_local_snapshot(path: Path, limit: int = MAX_SNAPSHOT_BYTES) -> bytes:
    """Read bounded local artifacts; never read an unbounded caller-provided file."""
    if not path.is_file() or not 0 < path.stat().st_size <= limit:
        raise SnapshotValidationError("Local snapshot file size invalid")
    with path.open("rb") as stream:
        payload = stream.read(limit + 1)
    if len(payload) > limit:
        raise SnapshotValidationError("Local snapshot file size invalid")
    return payload
