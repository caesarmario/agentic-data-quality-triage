"""Fixture setup is isolated from read-only incident revalidation."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts import prepare_resolution_schema_fixture as fixture


@pytest.mark.parametrize("mode", ["warn", "clean"])
def test_setup_changes_only_owned_acceptance_table(monkeypatch, mode):
    client = Mock()
    client.query.return_value = SimpleNamespace(result_rows=[(fixture.MARKER,)])
    monkeypatch.setattr(fixture, "build_clickhouse_client", lambda: client)
    result = fixture.prepare(mode)
    statements = [call.args[0] for call in client.command.call_args_list]
    assert len(statements) == 2
    assert statements[0] == "CREATE DATABASE IF NOT EXISTS dq_resolution_acceptance"
    assert statements[1].startswith(f"ALTER TABLE {fixture.TABLE} ")
    assert "acceptance_extra" in statements[1]
    assert result["external_llm_calls"] == 0
    assert "not Resolve" in result["purpose"]


def test_unowned_table_refuses_any_table_change(monkeypatch):
    client = Mock()
    client.query.return_value = SimpleNamespace(result_rows=[("someone_else",)])
    monkeypatch.setattr(fixture, "build_clickhouse_client", lambda: client)
    with pytest.raises(RuntimeError, match="ownership marker mismatch"):
        fixture.prepare("clean")
    statements = [call.args[0] for call in client.command.call_args_list]
    assert all("ALTER TABLE" not in item and "CREATE TABLE" not in item for item in statements)


def test_invalid_setup_mode_cannot_create_client(monkeypatch):
    build = Mock()
    monkeypatch.setattr(fixture, "build_clickhouse_client", build)
    with pytest.raises(ValueError):
        fixture.prepare("drop_everything")
    build.assert_not_called()


def test_new_fixture_has_fixed_table_and_ownership_marker(monkeypatch):
    client = Mock()
    client.query.return_value = SimpleNamespace(result_rows=[])
    monkeypatch.setattr(fixture, "build_clickhouse_client", lambda: client)
    fixture.prepare("warn")
    statement = client.command.call_args_list[1].args[0]
    assert f"CREATE TABLE {fixture.TABLE} " in statement
    assert f"COMMENT '{fixture.MARKER}'" in statement
