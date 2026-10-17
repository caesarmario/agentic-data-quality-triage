"""Isolated seeding snapshot contracts; no services, credentials, or historical replay."""
from __future__ import annotations

import hashlib
import io
import json
import sys
from datetime import date
from types import ModuleType

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from pandas.testing import assert_frame_equal

from pipelines.seeding import snapshots as ss
from pipelines.seeding.config import load_orders_config
from pipelines.seeding.generate_orders import generate_and_write_orders, generate_orders_for_date
from pipelines.seeding.incident_policy import resolve_incident_scenario_for_date
from pipelines.seeding.upload_to_s3 import upload_orders_partition


DT = date(2026, 5, 13)
CORRELATION = "manual__source-contract-20260513"


class StorageError(Exception):
    """Minimal S3 error shape, including conditional-create conflicts."""

    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class BoundedBody(io.BytesIO):
    """Record all reads and forbid unbounded read calls."""

    def __init__(self, payload):
        super().__init__(payload)
        self.read_sizes = []

    def read(self, size=-1):
        assert 0 < size <= 65536
        self.read_sizes.append(size)
        return super().read(size)


class FakeS3:
    """In-memory byte store with create-only semantics and intentionally fake ETags."""

    def __init__(self):
        self.objects = {}
        self.puts = []
        self.gets = []
        self.bodies = []

    def get_object(self, *, Bucket, Key):
        self.gets.append((Bucket, Key))
        if (Bucket, Key) not in self.objects:
            raise StorageError("NoSuchKey")
        data = self.objects[Bucket, Key]
        body = BoundedBody(data)
        self.bodies.append(body)
        return {"Body": body, "ContentLength": len(data), "ETag": "not-a-checksum"}

    def put_object(self, **kwargs):
        self.puts.append(kwargs)
        address = kwargs["Bucket"], kwargs["Key"]
        if kwargs.get("IfNoneMatch") == "*" and address in self.objects:
            raise StorageError("PreconditionFailed")
        self.objects[address] = kwargs["Body"]
        return {"ETag": "not-a-checksum"}


@pytest.fixture(autouse=True)
def no_registry(monkeypatch):
    """All storage tests are isolated even inside a DB-configured runner."""
    monkeypatch.delenv("DQ_CONTROL_DATABASE_URL", raising=False)


@pytest.fixture
def config(tmp_path):
    """Keep focused tests small without changing the configured shape or policy."""
    config = load_orders_config().model_copy(deep=True)
    config.countries = {
        code: config.countries[code].model_copy(update={"base_orders": 20})
        for code in ("ID", "SG")
    }
    config.channels = {name: config.channels[name] for name in ("organic", "paid")}
    config.output.local_dir = tmp_path
    return config


@pytest.fixture
def source(config):
    """Return the captured pre-fault frame, not an inferred clean landing."""
    return generate_orders_for_date(DT, config, "baseline").attrs[ss.SOURCE_FRAME_ATTR]


def publish(source, client=None, **kwargs):
    """Publish one bounded fixture source with stable correlation."""
    return ss.publish_source_snapshot(source, dt=DT, correlation_id=CORRELATION,
                                      s3_client=client or FakeS3(), **kwargs)


def reidentify(manifest, **changes):
    """Recompute the envelope identity to test deeper guards, not only ID mismatch."""
    values = manifest.model_dump(mode="json", exclude={"snapshot_id"})
    values.update(changes)
    return {**values, "snapshot_id": ss._sha256(ss._canonical_json(values))}


@pytest.mark.parametrize("scenario", [
    "baseline", "auto", "missing_segment", "duplicate_orders", "cancelled_revenue_leak",
    "fx_rate_spike", "late_arriving_batch", "null_spike", "missing_latest_day",
    "missing_segment,duplicate_orders,fx_rate_spike",
])
def test_base_business_values_are_scenario_independent(config, source, scenario):
    frame = generate_orders_for_date(DT, config, scenario)
    captured = frame.attrs[ss.SOURCE_FRAME_ATTR]
    assert_frame_equal(captured, source)
    assert "incident_scenario" not in captured
    assert not captured.attrs
    assert ss.serialize_snapshot_frame(captured, "upstream") == ss.serialize_snapshot_frame(source, "upstream")


def test_daily_fault_policy_remains_variable_and_applied(config):
    baseline = generate_orders_for_date(DT, config, "baseline")
    faulted = generate_orders_for_date(DT, config, "auto")
    assert faulted.attrs["resolved_incident_scenario"] == resolve_incident_scenario_for_date(DT, "auto").resolved_scenario
    assert len(faulted) > len(baseline)
    choices = {resolve_incident_scenario_for_date(date(2026, 5, day), "auto").resolved_scenario
               for day in range(1, 29)}
    assert len(choices) > 1


def test_source_is_copied_before_in_place_faults(config, source):
    faulted = generate_orders_for_date(DT, config, "null_spike")
    assert faulted["customer_id"].eq("").any()
    assert not faulted.attrs[ss.SOURCE_FRAME_ATTR]["customer_id"].eq("").any()
    assert_frame_equal(faulted.attrs[ss.SOURCE_FRAME_ATTR], source)


def test_content_addressed_roundtrip_and_idempotency(source):
    client = FakeS3()
    first = publish(source, client)
    second = publish(source, client)
    assert first == second
    assert len(client.puts) == 2
    assert all(put["IfNoneMatch"] == "*" for put in client.puts)
    assert first.sha256 in first.key and first.dt.isoformat() in first.key
    assert first.snapshot_id in first.manifest_key
    result = ss.read_pinned_snapshot(first.model_dump(mode="json"), client)
    assert_frame_equal(result, ss.validate_recovery_source(source, DT))
    assert not result.attrs
    assert ss.load_source_manifest(first.manifest_uri, client, dt=DT) == first
    assert all(body.closed for body in client.bodies)


def test_manifest_contains_only_public_allowlisted_metadata(source):
    manifest = publish(source)
    assert set(manifest.model_dump()) == {
        "dataset", "kind", "snapshot_id", "dt", "bucket", "key", "sha256",
        "schema_sha256", "size_bytes", "row_count", "version", "correlation_id",
    }
    assert manifest.kind == "upstream"
    with pytest.raises(ValueError):
        manifest.row_count = 1


def test_distinct_correlation_preserves_data_but_versions_manifest(source):
    client = FakeS3()
    first = publish(source, client)
    second = ss.publish_source_snapshot(source, dt=DT, correlation_id="different-run", s3_client=client)
    assert first.key == second.key and first.sha256 == second.sha256
    assert first.snapshot_id != second.snapshot_id
    assert len(client.puts) == 3


@pytest.mark.parametrize("changes", [
    {"dataset": "customers"}, {"kind": "source"}, {"bucket": "dq-artifacts"},
    {"key": "orders/../secret.parquet"}, {"key": "orders/dt=2026-05-13/orders.parquet"},
    {"schema_sha256": "0" * 64}, {"dt": "2026-05-14"}, {"size_bytes": 0},
    {"size_bytes": ss.MAX_SNAPSHOT_BYTES + 1}, {"row_count": -1}, {"row_count": True},
    {"row_count": ss.MAX_SNAPSHOT_ROWS + 1}, {"version": 2}, {"version": True},
    {"correlation_id": "https://host/?token=private"}, {"incident_scenario": "hidden"},
    {"ground_truth": {}}, {"credentials": "secret"}, {"snapshot_id": "0" * 64},
    {"dt": "2026-05-13T00:00:00"},
])
def test_manifest_rejects_forged_scope_schema_size_and_extra_fields(source, changes):
    manifest = publish(source)
    with pytest.raises(ss.SnapshotValidationError):
        ss.load_source_manifest({**manifest.model_dump(mode="json"), **changes})


def test_modified_model_copy_cannot_bypass_reader_validation(source):
    manifest = publish(source)
    forged = manifest.model_copy(update={"bucket": "private"})
    with pytest.raises(ss.SnapshotValidationError):
        ss.read_pinned_snapshot(forged, FakeS3())


@pytest.mark.parametrize("uri", [
    "s3://private/orders/snapshots/manifests/" + "a" * 64 + ".json",
    "https://host/manifest.json?token=secret", "s3://dq-landing/orders/snapshots/../private.json",
    "s3://dq-landing/orders/snapshots/manifests/" + "a" * 64 + ".json?token=secret",
])
def test_manifest_uri_rejected_before_storage_access(uri):
    client = FakeS3()
    with pytest.raises(ss.SnapshotValidationError):
        ss.load_source_manifest(uri, client)
    assert not client.gets


def test_manifest_wrong_date_and_wrong_address_are_rejected(source):
    client = FakeS3()
    manifest = publish(source, client)
    with pytest.raises(ss.SnapshotValidationError, match="scope"):
        ss.load_source_manifest(manifest, client, dt="2026-05-14")
    bad_key = f"{ss.SNAPSHOT_PREFIX}/manifests/{'0' * 64}.json"
    client.objects[ss.SNAPSHOT_BUCKET, bad_key] = client.objects[ss.SNAPSHOT_BUCKET, manifest.manifest_key]
    with pytest.raises(ss.SnapshotValidationError, match="checksum"):
        ss.load_source_manifest(f"s3://{ss.SNAPSHOT_BUCKET}/{bad_key}", client)


def test_checksum_is_actual_sha256_never_etag(source):
    client = FakeS3()
    manifest = publish(source, client)
    payload = client.objects[manifest.bucket, manifest.key]
    client.objects[manifest.bucket, manifest.key] = payload[:-1] + bytes([payload[-1] ^ 1])
    with pytest.raises(ss.SnapshotValidationError, match="checksum"):
        ss.read_pinned_snapshot(manifest, client)


@pytest.mark.parametrize("changes", [{"size_bytes": 1}, {"row_count": 1}])
def test_reader_rejects_validly_hashed_false_size_or_row_count(source, changes):
    client = FakeS3()
    manifest = publish(source, client)
    with pytest.raises(ss.SnapshotValidationError):
        ss.read_pinned_snapshot(reidentify(manifest, **changes), client)


def test_immutable_conflict_is_not_overwritten(source):
    client = FakeS3()
    manifest = publish(source, client)
    client.objects[manifest.bucket, manifest.key] = b"x" * manifest.size_bytes
    with pytest.raises(ss.SnapshotValidationError, match="conflict"):
        publish(source, client)
    assert len(client.puts) == 2


def test_create_race_checks_actual_bytes(source):
    class RacingS3(FakeS3):
        def put_object(self, **kwargs):
            if kwargs["Key"].endswith(".parquet"):
                self.objects[kwargs["Bucket"], kwargs["Key"]] = kwargs["Body"]
            return super().put_object(**kwargs)

    client = RacingS3()
    manifest = publish(source, client)
    assert ss.read_pinned_snapshot(manifest, client).shape == source.shape


def test_gateway_without_conditional_put_fails_closed(source):
    class UnsupportedS3(FakeS3):
        def put_object(self, **kwargs):
            raise StorageError("NotImplemented")

    with pytest.raises(StorageError, match="NotImplemented"):
        publish(source, UnsupportedS3())


@pytest.mark.parametrize("actual,advertised,limit", [
    (b"abc", 100, 10), (b"abc", 2, 10), (b"abc", 4, 10),
    (b"abc", None, 10), (b"abc", -1, 10),
])
def test_bounded_reader_rejects_overflow_truncation_and_bad_length(actual, advertised, limit):
    body = BoundedBody(actual)

    class BadResponse:
        def get_object(self, **kwargs):
            return {"Body": body, "ContentLength": advertised}

    with pytest.raises(ss.SnapshotValidationError):
        ss._read_object(BadResponse(), ss.SNAPSHOT_BUCKET, "ignored", limit)
    assert body.closed


def test_decoder_rejects_wrong_schema_and_partition(source):
    client = FakeS3()
    bad = source.drop(columns=["customer_id"])
    buffer = io.BytesIO()
    pq.write_table(pa.Table.from_pandas(bad, preserve_index=False), buffer)
    with pytest.raises(ss.SnapshotValidationError, match="schema"):
        ss.publish_snapshot(buffer.getvalue(), dt=DT, kind="upstream", correlation_id=CORRELATION, s3_client=client)
    with pytest.raises(ss.SnapshotValidationError, match="scope"):
        ss.publish_snapshot(ss.serialize_snapshot_frame(source, "upstream"), dt="2026-05-14",
                            kind="upstream", correlation_id=CORRELATION, s3_client=client)
    assert not client.puts


@pytest.mark.parametrize("column,value", [
    ("customer_id", ""), ("customer_id", None), ("gross_amount_usd", float("nan")),
    ("gross_amount_usd", float("inf")), ("gross_amount_local", -1.0),
    ("fx_rate_to_usd", 0.0), ("business_date_version", 0), ("status", "unknown"),
    ("recognized_revenue_usd", 1_000_000.0), ("order_date", date(2026, 5, 14)),
    ("order_ts", pd.Timestamp("2026-05-14", tz="UTC")),
    ("ingestion_ts", pd.Timestamp("2026-05-12", tz="UTC")),
])
def test_required_recovery_source_checks(source, column, value):
    bad = source.copy()
    bad.loc[bad.index[0], column] = value
    with pytest.raises(ss.SnapshotValidationError):
        ss.validate_recovery_source(bad, DT)


def test_recovery_rejects_empty_duplicate_and_evaluation_columns(source):
    for frame in (source.iloc[:0], pd.concat([source, source.iloc[:1]]), source.assign(incident_scenario="baseline")):
        with pytest.raises(ss.SnapshotValidationError):
            ss.validate_recovery_source(frame, DT)


def test_decoded_size_and_row_bounds_checked_before_read(source, monkeypatch):
    payload = ss.serialize_snapshot_frame(source, "upstream")
    monkeypatch.setattr(ss, "MAX_SNAPSHOT_ROWS", 1)
    with pytest.raises(ss.SnapshotValidationError, match="bounds"):
        ss._decode(payload, "upstream", DT)
    monkeypatch.setattr(ss, "MAX_SNAPSHOT_ROWS", 250_000)
    monkeypatch.setattr(ss, "MAX_DECODED_BYTES", 1)
    with pytest.raises(ss.SnapshotValidationError, match="bounds"):
        ss._decode(payload, "upstream", DT)


def test_parquet_never_serializes_private_frame_attrs(source):
    dirty = source.copy()
    dirty.attrs = {"scenario": "secret-evaluator", "credentials": "secret-key", "raw": source}
    payload = ss.serialize_snapshot_frame(dirty, "upstream")
    assert b"secret-evaluator" not in payload and b"secret-key" not in payload
    assert pq.ParquetFile(io.BytesIO(payload)).schema_arrow.metadata is None


def test_corrected_file_registration_validates_existing_bytes_only(source, monkeypatch):
    client = FakeS3()
    key = f"orders/dt={DT}/corrected.parquet"
    payload = ss.serialize_snapshot_frame(source, "upstream")
    client.objects[ss.SNAPSHOT_BUCKET, key] = payload
    monkeypatch.setattr("pipelines.seeding.generate_orders.generate_orders_for_date",
                        lambda *a, **kw: pytest.fail("Recovery cannot regenerate data"))
    manifest = ss.register_corrected_file(bucket=ss.SNAPSHOT_BUCKET, key=key, dt=DT,
                                          correlation_id=CORRELATION, s3_client=client)
    assert manifest.sha256 == hashlib.sha256(payload).hexdigest()
    assert client.objects[ss.SNAPSHOT_BUCKET, key] == payload
    assert all(put["Key"] != key for put in client.puts)


@pytest.mark.parametrize("bucket,key", [
    ("private", "orders/dt=2026-05-13/corrected.parquet"),
    ("dq-landing", "orders/dt=2026-05-14/corrected.parquet"),
    ("dq-landing", "orders/dt=2026-05-13/../secret.parquet"),
    ("dq-landing", "orders/dt=2026-05-13/corrected.parquet?token=secret"),
    ("dq-landing", "orders/dt=2026-05-13/sub/corrected.parquet"),
    ("dq-landing", "orders/snapshots/upstream/private.parquet"),
])
def test_corrected_registration_rejects_wrong_scope_before_io(bucket, key):
    client = FakeS3()
    with pytest.raises(ss.SnapshotValidationError):
        ss.register_corrected_file(bucket=bucket, key=key, dt=DT, correlation_id=CORRELATION, s3_client=client)
    assert not client.gets and not client.puts


def test_corrected_registration_rejects_scenario_and_malformed_bytes(config):
    client = FakeS3()
    key = f"orders/dt={DT}/corrected.parquet"
    landing = generate_orders_for_date(DT, config, "baseline")
    for payload in (ss.serialize_snapshot_frame(landing, "landing"), b"not-parquet"):
        client.objects[ss.SNAPSHOT_BUCKET, key] = payload
        with pytest.raises(ss.SnapshotValidationError):
            ss.register_corrected_file(bucket=ss.SNAPSHOT_BUCKET, key=key, dt=DT,
                                       correlation_id=CORRELATION, s3_client=client)
    assert not client.puts


def test_generate_upload_keeps_source_and_exact_faulted_landing_separate(config):
    generated = generate_and_write_orders(DT, config, incident_scenario="null_spike", correlation_id=CORRELATION)
    client = FakeS3()
    uploaded = upload_orders_partition(DT, config, s3_client=client)
    upstream = uploaded["source_manifests"]["upstream"]
    landing = uploaded["source_manifests"]["landing"]
    assert upstream["correlation_id"] == landing["correlation_id"] == CORRELATION
    assert upstream["sha256"] == generated["source_sha256"]
    assert landing["sha256"] != upstream["sha256"]
    actual = client.objects[ss.SNAPSHOT_BUCKET, config.output.object_key(DT)]
    assert actual == client.objects[ss.SNAPSHOT_BUCKET, landing["key"]]
    assert actual == config.output.local_path(DT).read_bytes()
    assert pd.read_parquet(io.BytesIO(actual))["customer_id"].eq("").any()
    assert not ss.read_pinned_snapshot(upstream, client)["customer_id"].eq("").any()
    assert all(p["IfNoneMatch"] == "*" for p in client.puts[:-1])
    assert "IfNoneMatch" not in client.puts[-1]
    with pytest.raises(ss.SnapshotValidationError, match="scope"):
        ss.read_pinned_snapshot(landing, client)


def test_empty_faulted_landing_preserves_schema_and_nonempty_source(config):
    generate_and_write_orders(DT, config, incident_scenario="missing_latest_day")
    client = FakeS3()
    uploaded = upload_orders_partition(DT, config, s3_client=client)
    assert uploaded["source_manifests"]["landing"]["row_count"] == 0
    assert len(ss.read_pinned_snapshot(uploaded["source_manifests"]["upstream"], client)) > 0
    assert pq.ParquetFile(config.output.local_path(DT)).schema_arrow.equals(ss.LANDING_SCHEMA)


def test_capture_receipt_rejects_stale_or_modified_landing(config):
    generate_and_write_orders(DT, config)
    config.output.local_path(DT).write_bytes(b"different-file")
    client = FakeS3()
    with pytest.raises(ss.SnapshotValidationError, match="receipt"):
        upload_orders_partition(DT, config, s3_client=client)
    assert not client.puts


def test_capture_receipt_rejects_modified_source(config):
    generated = generate_and_write_orders(DT, config)
    from pathlib import Path
    Path(generated["source_local_path"]).write_bytes(b"different-file")
    with pytest.raises(ss.SnapshotValidationError, match="checksum"):
        upload_orders_partition(DT, config, s3_client=FakeS3())


def test_no_historical_source_synthesized_for_standalone_developer_upload(config):
    from pipelines.seeding.generate_orders import write_orders_parquet
    frame = generate_orders_for_date(DT, config, "null_spike")
    write_orders_parquet(frame, DT, config)
    result = upload_orders_partition(DT, config, s3_client=FakeS3())
    assert set(result["source_manifests"]) == {"landing"}


def test_configured_runtime_requires_capture_before_upload(config, monkeypatch):
    from pipelines.seeding.generate_orders import write_orders_parquet
    write_orders_parquet(generate_orders_for_date(DT, config), DT, config)
    monkeypatch.setenv("DQ_CONTROL_DATABASE_URL", "configured-no-network-used")
    client = FakeS3()
    with pytest.raises(ss.SnapshotValidationError, match="capture"):
        upload_orders_partition(DT, config, s3_client=client)
    assert not client.puts


def registry_double(monkeypatch, register):
    """Provide the future registry lazily without modifying agent/recovery/store.py."""
    package = ModuleType("agent.recovery")
    package.__path__ = []
    store = ModuleType("agent.recovery.store")
    store.register_source_snapshot = register
    monkeypatch.setitem(sys.modules, "agent.recovery", package)
    monkeypatch.setitem(sys.modules, "agent.recovery.store", store)
    monkeypatch.setenv("DQ_CONTROL_DATABASE_URL", "configured-no-network-used")


def test_pg_registration_occurs_only_after_verified_data_and_manifest(source, monkeypatch):
    client = FakeS3()
    registered = []

    def register(manifest):
        pinned = ss.load_source_manifest(manifest)
        assert (pinned.bucket, pinned.key) in client.objects
        assert (pinned.bucket, pinned.manifest_key) in client.objects
        registered.append(manifest)

    registry_double(monkeypatch, register)
    manifest = publish(source, client)
    assert registered == [manifest.model_dump(mode="json")]


def test_failed_upload_does_not_register_pg(source, monkeypatch):
    registered = []
    registry_double(monkeypatch, registered.append)

    class FailedS3(FakeS3):
        def put_object(self, **kwargs):
            raise StorageError("AccessDenied")

    with pytest.raises(StorageError):
        publish(source, FailedS3())
    assert not registered


def test_configured_registry_failure_propagates_and_retry_keeps_bytes(source, monkeypatch):
    client = FakeS3()

    def unavailable(manifest):
        raise RuntimeError("Registry unavailable")

    registry_double(monkeypatch, unavailable)
    with pytest.raises(RuntimeError, match="Registry unavailable"):
        publish(source, client)
    assert len(client.puts) == 2
    registered = []
    sys.modules["agent.recovery.store"].register_source_snapshot = registered.append
    publish(source, client)
    assert len(client.puts) == 2 and len(registered) == 1


def test_registry_failure_does_not_publish_mutable_legacy_key(config, monkeypatch):
    generate_and_write_orders(DT, config)
    registry_double(monkeypatch, lambda manifest: (_ for _ in ()).throw(RuntimeError("Registry unavailable")))
    client = FakeS3()
    with pytest.raises(RuntimeError, match="Registry unavailable"):
        upload_orders_partition(DT, config, s3_client=client)
    assert (ss.SNAPSHOT_BUCKET, config.output.object_key(DT)) not in client.objects
