####
## LIFE Scenario Replay Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Contract tests for explicit, deterministic, and non-mutating LIFE replays."""

# --- Importing Libraries
from __future__ import annotations

import json

import pytest

from agent.evaluation import replay
from agent.evaluation.life import (
    build_life_artifact_keys,
    evaluate_life_report,
    render_life_evaluation,
)
from agent.evaluation.triage import load_yaml_file, resolve_scenario_path
from scripts import (
    prepare_life_source_report,
    trigger_airflow_life_evaluation,
    verify_life_evaluation,
)


# --- Defining Constants
SCENARIO_ID      = "schema_breaking_change"
REPLAY_RUN_ID    = "life-schema-replay-test"
EXPECTED_JSON_URI = (
    "s3://dq-artifacts/agent-replays/scenario=schema_breaking_change/"
    "run_id=life-schema-replay-test/report.json"
)


# --- Defining Test Helpers
def load_schema_replay_scenario() -> dict:
    """
    Load the repository schema replay ground truth.

    Returns:
        Parsed schema-breaking evaluation scenario.
    """
    return load_yaml_file(resolve_scenario_path(SCENARIO_ID))


class FakeS3Body:
    """Provide the streaming body contract used by the operational verifier."""

    def __init__(self, value: str) -> None:
        """
        Store one UTF-8 object body.

        Args:
            value: Text returned by read().

        Returns:
            None.
        """
        self.value = value.encode("utf-8")

    def read(self) -> bytes:
        """
        Return the stored object bytes.

        Returns:
            UTF-8 encoded object body.
        """
        return self.value


class FakeS3Client:
    """Return allowlisted in-memory objects for replay verification."""

    def __init__(self, objects: dict[str, str]) -> None:
        """
        Store object bodies by bucket/key correlation.

        Args:
            objects: Mapping from object key to text body.

        Returns:
            None.
        """
        self.objects = objects

    def get_object(self, Bucket: str, Key: str) -> dict:  # noqa: N803 - boto3 contract
        """
        Return one boto3-compatible get_object response.

        Args:
            Bucket: Requested artifacts bucket.
            Key: Requested object key.

        Returns:
            Dictionary containing a readable Body object.
        """
        assert Bucket == "dq-artifacts"

        return {"Body": FakeS3Body(self.objects[Key])}


# --- Defining Replay Contract Tests
def test_schema_replay_report_is_deterministic_and_transparent() -> None:
    """
    Ensure repeated builds keep identity stable and disclose non-production provenance.

    Returns:
        None.
    """
    scenario = load_schema_replay_scenario()
    first    = replay.build_life_replay_report(scenario=scenario, replay_run_id=REPLAY_RUN_ID)
    second   = replay.build_life_replay_report(scenario=scenario, replay_run_id=REPLAY_RUN_ID)

    assert first == second
    assert first["replay_provenance"] == {
        "source_mode": "scenario_replay",
        "scenario_id": SCENARIO_ID,
        "replay_run_id": REPLAY_RUN_ID,
        "deterministic_fixture": True,
        "warehouse_schema_mutated": False,
        "production_triage_claimed": False,
    }
    assert first["evidence"][0]["tool_name"] == "schema_drift"
    assert first["evidence"][0]["rows"][0]["check_type"] == "column_type"
    assert first["top_hypothesis"]["root_cause_category"] == "breaking_schema_change"


def test_schema_replay_passes_life_evidence_and_safety_policy() -> None:
    """
    Ensure the replay exercises schema-specific RCA, evidence, and no-mutation checks.

    Returns:
        None.
    """
    scenario   = load_schema_replay_scenario()
    report     = replay.build_life_replay_report(scenario=scenario, replay_run_id=REPLAY_RUN_ID)
    evaluation = evaluate_life_report(
        scenario=scenario,
        report=report,
        report_s3_uri=EXPECTED_JSON_URI,
        evaluation_run_id=REPLAY_RUN_ID,
    )

    assert evaluation.eval_status == "pass"
    assert evaluation.failed_checks == []
    assert {check.name for check in evaluation.checks} >= {
        "root_cause_category",
        "expected_evidence",
        "action_safety",
    }


def test_replay_artifact_keys_are_bounded_and_path_safe() -> None:
    """
    Ensure scenario replay artifacts cannot escape their configured prefix.

    Returns:
        None.
    """
    json_key, markdown_key = replay.build_replay_artifact_keys(
        scenario_id=SCENARIO_ID,
        replay_run_id=REPLAY_RUN_ID,
    )

    assert json_key.endswith("/report.json")
    assert markdown_key.endswith("/report.md")
    assert replay.build_replay_report_s3_uri(SCENARIO_ID, REPLAY_RUN_ID) == EXPECTED_JSON_URI

    with pytest.raises(ValueError, match="path-safe"):
        replay.build_replay_artifact_keys(SCENARIO_ID, REPLAY_RUN_ID, prefix="../escape")


def test_prepare_replay_source_persists_expected_uri(monkeypatch) -> None:
    """
    Ensure Airflow source preparation publishes only the deterministic replay object.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    captured = {}

    def fake_persist(**kwargs):
        """Capture replay persistence arguments without external S3 writes."""
        captured.update(kwargs)

        return {"replay_provenance": {"json_report_s3_uri": EXPECTED_JSON_URI}}

    monkeypatch.setattr(prepare_life_source_report, "persist_life_replay_report", fake_persist)

    summary = prepare_life_source_report.prepare_life_source_report(
        source_mode="scenario_replay",
        scenario_id=SCENARIO_ID,
        report_s3_uri=EXPECTED_JSON_URI,
        evaluation_run_id=REPLAY_RUN_ID,
    )

    assert summary["source_created"] is True
    assert summary["report_s3_uri"] == EXPECTED_JSON_URI
    assert captured["replay_run_id"] == REPLAY_RUN_ID


def test_persist_replay_source_writes_json_markdown_and_audit(monkeypatch) -> None:
    """
    Ensure replay publication is artifact-backed and durably identified as non-mutating.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    scenario = load_schema_replay_scenario()
    writes   = []
    audits   = []

    def fake_put_text_artifact(**kwargs):
        """Capture one Markdown replay artifact write."""
        writes.append(("text", kwargs))

        return f"s3://{kwargs['bucket']}/{kwargs['key']}"

    def fake_put_json_artifact(**kwargs):
        """Capture one JSON replay artifact write."""
        writes.append(("json", kwargs))

        return f"s3://{kwargs['bucket']}/{kwargs['key']}"

    def fake_write_agent_audit_event(**kwargs):
        """Capture the durable replay source audit event."""
        audits.append(kwargs)

        return kwargs["agent_run_id"]

    monkeypatch.setattr(replay, "put_text_artifact", fake_put_text_artifact)
    monkeypatch.setattr(replay, "put_json_artifact", fake_put_json_artifact)
    monkeypatch.setattr(replay, "write_agent_audit_event", fake_write_agent_audit_event)

    report = replay.persist_life_replay_report(
        scenario=scenario,
        replay_run_id=REPLAY_RUN_ID,
        clickhouse_client=object(),
    )

    assert [kind for kind, _ in writes] == ["text", "json"]
    assert len(audits) == 1
    assert audits[0]["action"] == "life_replay_source_published"
    assert audits[0]["output_payload"]["warehouse_schema_mutated"] is False
    assert report["replay_provenance"]["json_report_s3_uri"] == EXPECTED_JSON_URI


def test_prepare_replay_source_rejects_non_deterministic_uri() -> None:
    """
    Ensure replay mode cannot redirect source publication to an arbitrary S3 key.

    Returns:
        None.
    """
    with pytest.raises(ValueError, match="deterministic artifact key"):
        prepare_life_source_report.prepare_life_source_report(
            source_mode="scenario_replay",
            scenario_id=SCENARIO_ID,
            report_s3_uri="s3://dq-artifacts/agent-replays/other/report.json",
            evaluation_run_id=REPLAY_RUN_ID,
        )


def test_replay_trigger_derives_source_uri_and_bounded_conf() -> None:
    """
    Ensure the operator trigger derives replay location instead of accepting free-form paths.

    Returns:
        None.
    """
    command = trigger_airflow_life_evaluation.build_trigger_command(
        run_id="manual__life_schema_replay_test",
        evaluation_run_id=REPLAY_RUN_ID,
        scenario_id=SCENARIO_ID,
        source_mode="scenario_replay",
    )
    conf = json.loads(command[command.index("-c") + 1])

    assert conf["source_mode"] == "scenario_replay"
    assert conf["report_s3_uri"] == EXPECTED_JSON_URI
    assert conf["scenario"] == SCENARIO_ID


def test_replay_trigger_rejects_data_incident_scenario() -> None:
    """
    Ensure data-generation scenarios cannot be mislabeled as deterministic schema replays.

    Returns:
        None.
    """
    with pytest.raises(ValueError, match="does not support LIFE replay"):
        trigger_airflow_life_evaluation.build_trigger_command(
            run_id="manual__life_invalid_replay",
            evaluation_run_id="life-invalid-replay",
            scenario_id="missing_latest_day",
            source_mode="scenario_replay",
        )


def test_replay_verifier_checks_source_hash_provenance_and_both_audits(monkeypatch) -> None:
    """
    Ensure DAG verification proves replay source integrity and durable audit correlation.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    scenario = load_schema_replay_scenario()
    report   = replay.build_life_replay_report(scenario=scenario, replay_run_id=REPLAY_RUN_ID)
    report["replay_provenance"].update(
        {
            "json_report_s3_uri": EXPECTED_JSON_URI,
            "markdown_report_s3_uri": EXPECTED_JSON_URI.replace("report.json", "report.md"),
        }
    )
    evaluation = evaluate_life_report(
        scenario=scenario,
        report=report,
        report_s3_uri=EXPECTED_JSON_URI,
        evaluation_run_id=REPLAY_RUN_ID,
    )
    life_json_key, life_markdown_key = build_life_artifact_keys(REPLAY_RUN_ID)
    life_json_uri                    = f"s3://dq-artifacts/{life_json_key}"
    life_markdown_uri                = f"s3://dq-artifacts/{life_markdown_key}"
    persisted = evaluation.model_copy(
        update={
            "json_report_s3_uri": life_json_uri,
            "markdown_report_s3_uri": life_markdown_uri,
        }
    )
    persisted.markdown_report = render_life_evaluation(persisted)
    source_key = EXPECTED_JSON_URI.removeprefix("s3://dq-artifacts/")
    objects    = {
        source_key: json.dumps(report),
        life_json_key: json.dumps(persisted.model_dump(mode="json")),
        life_markdown_key: persisted.markdown_report,
    }
    evaluation_audit = (
        "life_evaluation_completed",
        "success",
        json.dumps(
            {
                "run_id": REPLAY_RUN_ID,
                "source_report_sha256": persisted.source_report_sha256,
                "critic_summary": persisted.critic_summary.model_dump(mode="json"),
            }
        ),
        life_json_uri,
    )
    replay_audit = (
        "life_replay_source_published",
        "success",
        json.dumps({"scenario_id": SCENARIO_ID, "replay_run_id": REPLAY_RUN_ID}),
        EXPECTED_JSON_URI,
    )

    monkeypatch.setattr(
        verify_life_evaluation,
        "query_life_audit_events",
        lambda client, evaluation_run_id: [evaluation_audit],
    )
    monkeypatch.setattr(
        verify_life_evaluation,
        "query_life_replay_audit_events",
        lambda client, evaluation_run_id: [replay_audit],
    )

    summary = verify_life_evaluation.verify_life_evaluation(
        evaluation_run_id=REPLAY_RUN_ID,
        scenario_id=SCENARIO_ID,
        expected_source_report_s3_uri=EXPECTED_JSON_URI,
        source_mode="scenario_replay",
        s3_client=FakeS3Client(objects),
        clickhouse_client=object(),
    )

    assert summary["status"] == "success"
    assert summary["source_mode"] == "scenario_replay"
    assert summary["audit_event_count"] == 1
    assert summary["replay_audit_event_count"] == 1
