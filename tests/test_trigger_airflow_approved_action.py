####
## Airflow Approved Action Trigger Helper Tests
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from scripts import trigger_airflow_approved_action


# --- Testing Validation And Command Construction
def test_build_trigger_command_contains_only_request_id_and_dry_run() -> None:
    command = trigger_airflow_approved_action.build_trigger_command(
        "APR-ABCDEF0123456789ABCD",
        "manual__approved_action_test",
        True,
    )
    conf = json.loads(command[command.index("-c") + 1])

    assert conf == {
        "approval_request_id": "APR-ABCDEF0123456789ABCD",
        "dry_run": True,
    }
    assert command[-1] == trigger_airflow_approved_action.APPROVED_ACTION_DAG_ID


def test_request_and_run_ids_fail_closed() -> None:
    with pytest.raises(ValueError):
        trigger_airflow_approved_action.validate_request_id("APR-invalid")
    with pytest.raises(ValueError):
        trigger_airflow_approved_action.build_trigger_command(
            "APR-ABCDEF0123456789ABCD",
            "manual;rm",
            False,
        )


def test_build_run_id_is_unique_and_auditable() -> None:
    run_id = trigger_airflow_approved_action.build_run_id(
        "APR-ABCDEF0123456789ABCD",
        dry_run=False,
        now=datetime(2026, 9, 28, 1, 2, 3, 456789, tzinfo=timezone.utc),
    )

    assert run_id == (
        "manual__approved_action_execute_apr-abcdef0123456789abcd_"
        "20260928T010203456789"
    )


def test_trigger_unpauses_before_dispatch(monkeypatch) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(
        trigger_airflow_approved_action.subprocess,
        "run",
        lambda command, check: commands.append(command),
    )

    run_id = trigger_airflow_approved_action.trigger_approved_action(
        "APR-ABCDEF0123456789ABCD",
        dry_run=True,
        run_id="manual__approved_action_test",
    )

    assert run_id == "manual__approved_action_test"
    assert commands[0] == [
        "airflow",
        "dags",
        "unpause",
        trigger_airflow_approved_action.APPROVED_ACTION_DAG_ID,
    ]
    assert commands[1][-1] == trigger_airflow_approved_action.APPROVED_ACTION_DAG_ID
