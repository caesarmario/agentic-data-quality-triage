"""Single-use runtime gates and provider/generator isolation."""
from __future__ import annotations

import ast
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.recovery import runtime
from agent.tools.incident_resolution_store import ResolutionConflict


@pytest.mark.parametrize("counts", [(0, 0), (1, 0), (0, 1)])
def test_raw_only_guard_checks_both_derived_relations_before_publication(counts):
    from datetime import date
    calls = []
    values = iter(counts)
    class Warehouse:
        def query(self, sql):
            calls.append(sql)
            return SimpleNamespace(result_rows=[(next(values),)])
    if counts == (0, 0):
        runtime.require_raw_only_scope(Warehouse(), date(2026, 1, 11))
        assert len(calls) == 2
    else:
        with pytest.raises(ResolutionConflict, match="replay-and-rebuild"):
            runtime.require_raw_only_scope(Warehouse(), date(2026, 1, 11))
    assert all("2026-01-11" in sql for sql in calls)


def test_publication_and_business_verification_have_separate_ledger_phases():
    source = Path(runtime.__file__).read_text(encoding="utf-8")
    assert 'job_name="recovery_publication_"' in source
    assert '"phase": "publication", "business_validation": "not_checked"' in source
    assert 'job_name="recovery_verification_"' in source
    assert source.index('result = (verify or capture_revalidation)') < source.index('job_name="recovery_verification_"')
    assert 'status="success" if result["status"] == "pass" else "warning"' in source


def run_status(monkeypatch, status, *, finished_at=None):
    execution = SimpleNamespace(status=status, plan_id="plan-1", finished_at=finished_at,
                                model_dump=lambda **_kwargs: {"status": status})
    plan = SimpleNamespace()
    work_calls = []

    class Cursor:
        pass

    @contextmanager
    def transaction():
        yield Cursor()

    monkeypatch.setattr(runtime, "control_transaction", transaction)
    monkeypatch.setattr(runtime, "read_execution", lambda _cursor, _id: execution)
    monkeypatch.setattr(runtime, "read_plan", lambda _cursor, _id: plan)

    def work(*_args, **_kwargs):
        work_calls.append("started")

    return lambda: runtime.run_execution("execution-1", client=object(), work=work), work_calls


@pytest.mark.parametrize("status", ["running", "unknown"])
def test_started_or_terminal_unknown_execution_never_replays_pipeline(monkeypatch, status):
    finish = datetime.now(timezone.utc) if status == "unknown" else None
    invoke, work_calls = run_status(monkeypatch, status, finished_at=finish)

    with pytest.raises(ResolutionConflict):
        invoke()
    assert work_calls == []


@pytest.mark.parametrize("status", ["succeeded", "failed"])
def test_terminal_execution_returns_existing_record_without_replay(monkeypatch, status):
    invoke, work_calls = run_status(monkeypatch, status)

    assert invoke() == {"status": status}
    assert work_calls == []


def test_runtime_module_has_no_model_or_data_generator_import_path():
    path = Path(runtime.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden = {name for name in imported if name.startswith((
        "agent.llm", "agent.nodes", "agent.specialists", "pipelines.seeding.generate_orders",
        "pipelines.seeding.incident_policy",
    ))}
    assert forbidden == set()


def test_runtime_source_never_uses_scenario_or_ground_truth_as_recovery_input():
    source = Path(runtime.__file__).read_text(encoding="utf-8").casefold()
    assert "ground_truth" not in source
    assert "incident_scenario" not in source
    assert "generate_orders" not in source
