"""Safety tests for journaled, date-scoped partition publication."""
from __future__ import annotations

import hashlib
import io
import json
from contextlib import contextmanager, nullcontext
from datetime import date
from pathlib import Path

import pytest

from pipelines.common import publication
from pipelines.common.publication import (
    Preconditions,
    PreparedPartition,
    PublicationConflict,
    PublicationUnknown,
    Structure,
)


DT = date(2026, 10, 14)
EXECUTION = "writer:dag:run:task"
TARGET = "dq.raw_orders"
STAGE = "dq_pub_0123456789abcdef01234567.raw_orders"
BACKUP = "dq_pub_0123456789abcdef01234567.raw_orders_backup"


class FakeResult:
    def __init__(self, rows):
        self.result_rows = rows


class ScopeClient:
    def __init__(self, out_of_scope: int):
        self.out_of_scope = out_of_scope
        self.queries = []

    def query(self, sql):
        self.queries.append(sql)
        return FakeResult([(self.out_of_scope,)])


class JournalLock:
    def __init__(self, *, fail_finish=False):
        self.execution_id = EXECUTION
        self.partition_dt = DT
        self.owned_revisions = {Preconditions(3, "wm-3")}
        self.status = "prepared"
        self.transitions = []
        self.fail_finish = fail_finish

    def state(self):
        return Preconditions(3, "wm-3")

    def require(self, expected):
        assert self.state() == expected

    def journal_status(self, publication_id):
        return self.status

    def transition(self, publication_id, before, after):
        assert self.status == before
        self.transitions.append((before, after))
        self.status = after

    def finish(self, prepared, expected):
        if self.fail_finish:
            raise TimeoutError("control store acknowledgement timed out")
        self.status = "published"
        return Preconditions(expected.revision + 1, "published")


class PublishClient:
    def __init__(self, *, command_error=None):
        self.live_partition = "old"
        self.command_error = command_error
        self.commands = []

    def command(self, sql):
        self.commands.append(sql)
        if self.command_error:
            raise self.command_error
        self.live_partition = "new"


def prepared(target=TARGET):
    return PreparedPartition(
        publication_id="pub-1", execution_id=EXECUTION, target_table=target,
        stage_table=STAGE, backup_table=BACKUP, partition_dt=DT,
        preconditions=Preconditions(3, "wm-3"), before_fingerprint="before",
        staged_fingerprint="after", structure=Structure((), (), "(dt Date)"),
        rows=10, intent="daily",
    )


def prepare_publish_seams(monkeypatch, *, current_fingerprint="before"):
    monkeypatch.setattr(publication, "_check_prepared", lambda *_: None)
    monkeypatch.setattr(publication, "partition_fingerprint", lambda *_: current_fingerprint)


def test_failed_atomic_replace_keeps_old_partition_and_marks_journal_unknown(monkeypatch):
    """A failed atomic REPLACE must not be followed by destructive drop/retry."""
    lock = JournalLock()
    client = PublishClient(command_error=RuntimeError("server rejected replacement"))
    prepare_publish_seams(monkeypatch)

    with pytest.raises(PublicationUnknown, match="no blind retry"):
        publication.publish_partition(client, lock, prepared())

    assert client.live_partition == "old"
    assert len(client.commands) == 1
    assert "REPLACE PARTITION" in client.commands[0]
    assert "DROP PARTITION" not in client.commands[0]
    assert lock.transitions == [("prepared", "publishing"), ("publishing", "unknown")]


def test_timeout_or_unknown_journal_is_never_retried(monkeypatch):
    lock = JournalLock()
    client = PublishClient(command_error=TimeoutError("command response lost"))
    prepare_publish_seams(monkeypatch)
    item = prepared()

    with pytest.raises(PublicationUnknown):
        publication.publish_partition(client, lock, item)
    assert lock.status == "unknown"

    # A caller retry is rejected by journal state before another client command.
    with pytest.raises(PublicationUnknown, match="do not retry"):
        publication.publish_partition(client, lock, item)
    assert len(client.commands) == 1


def test_date_scope_rejects_any_row_outside_approved_partition():
    client = ScopeClient(out_of_scope=1)

    with pytest.raises(PublicationConflict, match="outside the approved date"):
        publication.validate_partition_scope(client, STAGE, DT)

    assert len(client.queries) == 1
    assert "dt != toDate('2026-10-14')" in client.queries[0]


@pytest.mark.parametrize("target", ["dq.other_orders", "other.raw_orders", "dq.raw_orders; DROP TABLE dq.x"])
def test_exact_target_allowlist_rejects_unapproved_table_without_io(target):
    with pytest.raises(ValueError, match="allowlisted orders table"):
        publication.prepare_partition(object(), object(), target, STAGE)


def test_stage_must_match_exact_execution_and_target_relation(monkeypatch):
    lock = JournalLock()
    monkeypatch.setattr(publication, "table_structure", lambda *_: Structure((), (), "(dt Date)"))

    with pytest.raises(PublicationConflict, match="Stage is not bound"):
        publication.prepare_partition(
            object(), lock, "dq.stg_orders", STAGE, expected=Preconditions(3, "wm-3")
        )


def test_each_approved_recovery_runbook_honors_its_exact_publication_targets(monkeypatch):
    """The runtime must not silently widen or narrow the approved runbook."""
    from pipelines.dbt import run_dbt

    class Locked:
        def require(self, expected):
            assert expected == Preconditions(3, "wm-3")

    @contextmanager
    def fake_lock(*_args, **_kwargs):
        yield Locked()

    class Frame:
        def copy(self):
            return self

        def __setitem__(self, key, value):
            assert key == "incident_scenario" and value == ""

    class Raw:
        target_table = "dq.raw_orders"
        stage_table = "stage.raw_orders"

    class Target:
        def __init__(self, table):
            self.target_table = table

    class Build:
        prepared = [Target("dq.stg_orders"), Target("dq.fct_orders_daily")]
        artifact_dir = None

    monkeypatch.setattr(run_dbt, "publication_lock", fake_lock)
    monkeypatch.setattr(run_dbt, "model_contract_hash", lambda *_: "model-hash")
    from pipelines.loading import load_clickhouse
    from pipelines.seeding import snapshots

    monkeypatch.setattr(snapshots, "read_pinned_snapshot", lambda *_args, **_kwargs: Frame())
    monkeypatch.setattr(load_clickhouse, "prepare_raw_partition", lambda *_args, **_kwargs: Raw())
    monkeypatch.setattr(run_dbt, "build_scoped_dbt_outputs", lambda *_args, **_kwargs: Build())
    monkeypatch.setattr(run_dbt, "publish_group", lambda _client, _lock, members, **_kwargs: list(members))

    expected = {
        "replay_raw_partition": ["dq.raw_orders"],
        "rebuild_derived_partition": ["dq.stg_orders", "dq.fct_orders_daily"],
        "replay_and_rebuild_partition": ["dq.raw_orders", "dq.stg_orders", "dq.fct_orders_daily"],
    }
    for runbook, targets in expected.items():
        published = []

        def build_scoped(*_args, **_kwargs):
            return Build()

        def publish(_client, _lock, members, **_kwargs):
            published.extend(members)
            return list(members)

        monkeypatch.setattr(run_dbt, "build_scoped_dbt_outputs", build_scoped)
        monkeypatch.setattr(run_dbt, "publish_group", publish)
        run_dbt.execute_recovery_pipeline(
            {"runbook": runbook, "partition_dt": DT, "snapshot_manifest": {},
             "expected_revision": 3, "expected_watermark": "wm-3",
             "expected_model_hash": "model-hash", "compensation_preapproved": False},
            "exec-1", validate_recovery=lambda context: True, client=object(), store=object(),
        )
        labels = [item.target_table for item in published]
        assert labels == targets


def dbt_manifest_fixture(scratch: str) -> dict:
    """Build an isolated dbt manifest with known and unrelated relations."""
    return {
        "metadata": {"env": {"CLICKHOUSE_PASSWORD": "must-not-publish"}},
        "nodes": {
            "model.project.stg_orders": {
                "resource_type": "model", "name": "stg_orders", "alias": "stg_orders",
                "schema": scratch, "relation_name": f"`{scratch}`.`stg_orders`",
                "original_file_path": "models/staging/stg_orders.sql",
                "compiled_code": "SELECT secret_source_column FROM scratch",
            },
            "model.project.fct_orders_daily": {
                "resource_type": "model", "name": "fct_orders_daily", "alias": "fct_orders_daily",
                "schema": scratch, "relation_name": f"`{scratch}`.`fct_orders_daily`",
                "original_file_path": "models/marts/fct_orders_daily.sql",
                "compiled_code": "SELECT * FROM scratch.stg_orders",
            },
            "test.project.not_null_stg_orders_order_id": {
                "resource_type": "test", "name": "not_null_stg_orders_order_id",
                "original_file_path": "models/staging/schema.yml",
                "test_metadata": {"name": "not_null", "kwargs": {"value": "must-not-publish"}},
                "compiled_code": "select secret from private_schema",
            },
            "model.project.unrelated": {
                "resource_type": "model", "name": "unrelated", "alias": "unrelated",
                "schema": scratch, "relation_name": f"`{scratch}`.`unrelated`",
                "original_file_path": "models/private/unrelated.sql",
                "compiled_code": "SELECT private_data FROM scratch.unrelated",
            },
        },
        "sources": {
            "source.project.raw.raw_orders": {
                "resource_type": "source", "source_name": "raw", "name": "raw_orders",
                "identifier": "raw_orders_input", "schema": scratch,
                "relation_name": f"`{scratch}`.`raw_orders_input`",
                "original_file_path": "models/sources.yml",
            },
        },
        "parent_map": {
            "model.project.stg_orders": ["source.project.raw.raw_orders"],
            "model.project.fct_orders_daily": ["model.project.stg_orders"],
            "test.project.not_null_stg_orders_order_id": ["model.project.stg_orders"],
            "model.project.unrelated": [],
        },
        "child_map": {
            "source.project.raw.raw_orders": ["model.project.stg_orders"],
            "model.project.stg_orders": ["model.project.fct_orders_daily", "test.project.not_null_stg_orders_order_id"],
            "model.project.fct_orders_daily": [],
            "model.project.unrelated": [],
        },
    }


def lineage_source_uri(upload_run_id: str, partition_dt: date) -> str:
    return ("s3://dq-artifacts/dbt-artifacts/orders/runs/"
            f"run_id={upload_run_id}/start_dt={partition_dt.isoformat()}/"
            f"end_dt={partition_dt.isoformat()}/{partition_dt.isoformat()}/build/manifest.json")


def test_lineage_view_normalizes_only_allowlisted_orders_relations():
    from pipelines.dbt import run_dbt

    execution_id = "dbt:20_dag_dq_orders_dbt_transform:manual__lineage"
    scratch = publication.scratch_database(execution_id, DT)
    original = json.dumps(dbt_manifest_fixture(scratch)).encode()
    upload_run_id = "20261016T120000000000Z_aabbccdd"
    original_sha = hashlib.sha256(original).hexdigest()

    view_bytes = run_dbt.build_lineage_manifest_view(
        original, source_manifest_uri=lineage_source_uri(upload_run_id, DT),
        source_manifest_sha256=original_sha, execution_id=execution_id,
        upload_run_id=upload_run_id, model_hash="a" * 64,
        partition_dt=DT, scratch_schema=scratch,
    )
    view = json.loads(view_bytes)

    assert view["sources"]["source.orders.raw.raw_orders"]["relation_name"] == "dq.raw_orders"
    assert view["nodes"]["model.orders.stg_orders"]["relation_name"] == "dq.stg_orders"
    assert view["nodes"]["model.orders.fct_orders_daily"]["relation_name"] == "dq.fct_orders_daily"
    assert view["parent_map"]["model.orders.stg_orders"] == ["source.orders.raw.raw_orders"]
    assert view["child_map"]["model.orders.stg_orders"] == [
        "model.orders.fct_orders_daily", "test.orders.stg_orders.not_null.001",
    ]
    assert view["nodes"]["test.orders.stg_orders.not_null.001"]["name"] == "not_null"
    assert all("unrelated" not in key for key in view["nodes"])
    assert scratch not in view_bytes.decode()
    assert "compiled_code" not in view_bytes.decode()
    assert "must-not-publish" not in view_bytes.decode()
    assert view["lineage_provenance"]["source_manifest_sha256"] == original_sha
    assert view["lineage_provenance"]["source_manifest_uri"] == lineage_source_uri(upload_run_id, DT)
    from agent.tools.dbt_lineage import build_lineage_summary
    lineage = build_lineage_summary(view, "dq.stg_orders")
    assert lineage["matched"] is True
    assert lineage["parents"][0]["relation_name"] == "dq.raw_orders"
    assert lineage["children"][0]["relation_name"] == "dq.fct_orders_daily"
    assert [item["name"] for item in lineage["tests"]] == ["not_null"]


def test_lineage_view_rejects_unexpected_scratch_relation():
    from pipelines.dbt import run_dbt

    execution_id = "dbt:20_dag_dq_orders_dbt_transform:manual__lineage"
    scratch = publication.scratch_database(execution_id, DT)
    manifest = dbt_manifest_fixture(scratch)
    manifest["nodes"]["model.project.stg_orders"]["relation_name"] = "dq.stg_orders"
    payload = json.dumps(manifest).encode()

    with pytest.raises(PublicationConflict, match="not isolated"):
        run_dbt.build_lineage_manifest_view(
            payload, source_manifest_uri=lineage_source_uri("20261016T120000000000Z_aabbccdd", DT),
            source_manifest_sha256=hashlib.sha256(payload).hexdigest(), execution_id=execution_id,
            upload_run_id="20261016T120000000000Z_aabbccdd", model_hash="a" * 64,
            partition_dt=DT, scratch_schema=scratch,
        )


def test_lineage_view_enforces_output_size_bound(monkeypatch):
    from pipelines.dbt import run_dbt

    execution_id = "dbt:20_dag_dq_orders_dbt_transform:manual__lineage"
    scratch = publication.scratch_database(execution_id, DT)
    payload = json.dumps(dbt_manifest_fixture(scratch)).encode()
    monkeypatch.setattr(run_dbt, "MAX_LINEAGE_VIEW_BYTES", 64)

    with pytest.raises(PublicationConflict, match="write bound"):
        run_dbt.build_lineage_manifest_view(
            payload, source_manifest_uri=lineage_source_uri("20261016T120000000000Z_aabbccdd", DT),
            source_manifest_sha256=hashlib.sha256(payload).hexdigest(), execution_id=execution_id,
            upload_run_id="20261016T120000000000Z_aabbccdd", model_hash="a" * 64,
            partition_dt=DT, scratch_schema=scratch,
        )


class MemoryS3:
    def __init__(self):
        self.objects = {}
        self.metadata = {}
        self.puts = []

    def get_object(self, *, Bucket, Key):
        payload = self.objects[(Bucket, Key)]
        return {"ContentLength": len(payload), "Body": io.BytesIO(payload)}

    def head_object(self, *, Bucket, Key):
        identity = (Bucket, Key)
        if identity not in self.objects:
            error = FileNotFoundError("missing S3 object")
            error.response = {"Error": {"Code": "NoSuchKey"}}
            raise error
        return {"Metadata": self.metadata.get(identity, {})}

    def put_object(self, *, Bucket, Key, Body, ContentType, Metadata):
        self.objects[(Bucket, Key)] = bytes(Body)
        self.metadata[(Bucket, Key)] = Metadata
        self.puts.append((Bucket, Key, ContentType, Metadata))


def test_artifact_upload_keeps_original_and_publishes_verified_lineage_view(tmp_path, monkeypatch):
    from pipelines.dbt import run_dbt

    execution_id = "dbt:20_dag_dq_orders_dbt_transform:manual__lineage"
    scratch = publication.scratch_database(execution_id, DT)
    manifest_bytes = json.dumps(dbt_manifest_fixture(scratch)).encode()
    artifact_dir = run_dbt.execution_artifact_dir(tmp_path, execution_id, DT)
    (artifact_dir / "target").mkdir(parents=True)
    (artifact_dir / "target" / "manifest.json").write_bytes(manifest_bytes)
    model_hash = "b" * 64
    (artifact_dir / "publication.json").write_text(json.dumps({
        "execution_id": execution_id, "partition_dt": DT.isoformat(),
        "database": scratch, "model_hash": model_hash,
    }), encoding="utf-8")
    (artifact_dir / "build.intent.json").write_text(json.dumps({
        "execution_id": execution_id, "partition_dt": DT.isoformat(), "model_hash": model_hash,
    }), encoding="utf-8")

    s3 = MemoryS3()
    monkeypatch.setattr(run_dbt, "build_s3_client", lambda **_kwargs: s3)
    monkeypatch.setattr(run_dbt, "write_dbt_pipeline_runs", lambda **_kwargs: None)
    monkeypatch.setattr(run_dbt, "_lineage_pointer_lock", lambda: nullcontext())

    def upload(local_path, bucket, key, endpoint_url=None):
        s3.objects[(bucket, key)] = Path(local_path).read_bytes()
        return f"s3://{bucket}/{key}"

    monkeypatch.setattr(run_dbt, "upload_file_to_s3", upload)
    result = run_dbt.upload_dbt_artifacts(
        start_dt=DT, end_dt=DT, partitions=[DT], project_dir=tmp_path,
        bucket="dq-artifacts", execution_id=execution_id,
    )

    versioned = next(item["versioned_uri"] for item in result["artifacts"]
                     if item["filename"] == f"{DT.isoformat()}/build/manifest.json")
    original_key = versioned.removeprefix("s3://dq-artifacts/")
    latest_key = "dbt-artifacts/orders/latest/manifest.json"
    view = json.loads(s3.objects[("dq-artifacts", latest_key)])
    assert s3.objects[("dq-artifacts", original_key)] == manifest_bytes
    assert result["lineage_manifest"]["status"] == "published"
    assert view["lineage_provenance"]["source_manifest_uri"] == versioned
    assert view["lineage_provenance"]["source_manifest_sha256"] == hashlib.sha256(manifest_bytes).hexdigest()
    assert view["lineage_provenance"]["execution_id"] == execution_id
    assert view["lineage_provenance"]["model_hash"] == model_hash
    assert s3.puts[0][1] == latest_key and s3.puts[0][2] == "application/json"


def test_lineage_pointer_lock_uses_dedicated_postgres_advisory_lock(monkeypatch):
    from pipelines.dbt import run_dbt

    class Cursor:
        def __init__(self):
            self.statements = []

        def execute(self, sql, params):
            self.statements.append((sql, params))

        def fetchone(self):
            return (True,)

        def close(self):
            pass

    class Connection:
        def __init__(self):
            self._cursor = Cursor()
            self.commits = 0
            self.closed = False

        def cursor(self):
            return self._cursor

        def commit(self):
            self.commits += 1

        def close(self):
            self.closed = True

    connection = Connection()
    store = type("Store", (), {"connection_factory": lambda _self: connection})()
    monkeypatch.setattr(run_dbt.PostgresPublicationStore, "from_env", classmethod(lambda _cls: store))

    with run_dbt._lineage_pointer_lock():
        assert not connection.closed

    assert [sql for sql, _ in connection._cursor.statements] == [
        "SELECT pg_try_advisory_lock(%s)", "SELECT pg_advisory_unlock(%s)",
    ]
    assert connection._cursor.statements[0][1] == (run_dbt.LINEAGE_POINTER_LOCK_KEY,)
    assert connection.closed


def test_lineage_view_is_not_published_if_versioned_original_fails_verification(monkeypatch):
    from pipelines.dbt import run_dbt

    execution_id = "dbt:20_dag_dq_orders_dbt_transform:manual__lineage"
    scratch = publication.scratch_database(execution_id, DT)
    original = json.dumps(dbt_manifest_fixture(scratch)).encode()
    upload_run_id = "20261016T120000000000Z_aabbccdd"
    source_uri = lineage_source_uri(upload_run_id, DT)
    s3 = MemoryS3()
    source_key = source_uri.removeprefix("s3://dq-artifacts/")
    s3.objects[("dq-artifacts", source_key)] = b"different immutable object bytes"
    monkeypatch.setattr(run_dbt, "_lineage_pointer_lock", lambda: nullcontext())

    with pytest.raises(PublicationConflict, match="byte/hash verification"):
        run_dbt.publish_lineage_manifest_view(
            source_manifest=original, source_manifest_uri=source_uri,
            source_manifest_sha256=hashlib.sha256(original).hexdigest(),
            execution_id=execution_id, upload_run_id=upload_run_id, model_hash="a" * 64,
            partition_dt=DT, scratch_schema=scratch, bucket="dq-artifacts",
            latest_key="dbt-artifacts/orders/latest/manifest.json", s3_client=s3,
        )
    assert ("dq-artifacts", "dbt-artifacts/orders/latest/manifest.json") not in s3.objects
    assert s3.puts == []


def test_older_upload_cannot_replace_newer_lineage_pointer(monkeypatch):
    from pipelines.dbt import run_dbt

    execution_id = "dbt:20_dag_dq_orders_dbt_transform:manual__lineage"
    scratch = publication.scratch_database(execution_id, DT)
    original = json.dumps(dbt_manifest_fixture(scratch)).encode()
    upload_run_id = "20261016T120000000000Z_aabbccdd"
    source_uri = lineage_source_uri(upload_run_id, DT)
    latest_key = "dbt-artifacts/orders/latest/manifest.json"
    s3 = MemoryS3()
    s3.objects[("dq-artifacts", source_uri.removeprefix("s3://dq-artifacts/"))] = original
    s3.objects[("dq-artifacts", latest_key)] = b"newer-pointer-bytes"
    s3.metadata[("dq-artifacts", latest_key)] = {"lineage-run-id": "20261016T120000000001Z_ffffffff"}
    monkeypatch.setattr(run_dbt, "_lineage_pointer_lock", lambda: nullcontext())

    outcome = run_dbt.publish_lineage_manifest_view(
        source_manifest=original, source_manifest_uri=source_uri,
        source_manifest_sha256=hashlib.sha256(original).hexdigest(),
        execution_id=execution_id, upload_run_id=upload_run_id, model_hash="a" * 64,
        partition_dt=DT, scratch_schema=scratch, bucket="dq-artifacts",
        latest_key=latest_key, s3_client=s3,
    )

    assert outcome["status"] == "superseded"
    assert s3.objects[("dq-artifacts", latest_key)] == b"newer-pointer-bytes"
    assert s3.puts == []
