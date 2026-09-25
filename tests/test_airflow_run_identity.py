"""Exercise parent/child identity without importing Airflow internals."""

# --- Importing Libraries
import ast
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from jinja2 import StrictUndefined
from jinja2.nativetypes import NativeEnvironment

from dags.dq_platform.run_identity import trigger_logical_date, trigger_run_suffix


# --- Checking Manual And Scheduled Identity
def test_helper_imports_support_airflow_standalone_file_discovery() -> None:
    """Airflow may load this helper as a file rather than as a package member."""
    helper = Path(__file__).resolve().parents[1] / "dags/dq_platform/helpers.py"
    imports = [node for node in ast.walk(ast.parse(helper.read_text(encoding="utf-8")))
               if isinstance(node, ast.ImportFrom)]
    assert all(node.level == 0 for node in imports)
    assert any(node.module == "dq_platform.run_identity" for node in imports)


def test_undated_manual_run_is_repeatable_and_distinct() -> None:
    """No timestamp template is required, and distinct parent runs cannot alias."""
    parent = SimpleNamespace(logical_date=None, dag_id="00_daily", run_id="manual_a")
    first = trigger_run_suffix(parent)
    assert first == trigger_run_suffix(parent)
    assert first.startswith("manual_")
    parent.run_id = "manual_b"
    assert first != trigger_run_suffix(parent)
    assert trigger_logical_date(parent) is None


def test_dated_parent_preserves_existing_child_run_ids() -> None:
    """Existing dated DagRuns retain their old timestamp-based child identifiers."""
    parent = SimpleNamespace(logical_date=datetime(2026, 9, 23, 23, 24, tzinfo=timezone.utc))
    assert trigger_run_suffix(parent) == "20260923T232400"
    assert trigger_logical_date(parent) == "2026-09-23T23:24:00+00:00"


def test_missing_parent_identity_fails_closed() -> None:
    """An undated parent must not silently share an empty child identity."""
    with pytest.raises(ValueError, match="stable parent"):
        trigger_run_suffix(SimpleNamespace(logical_date=None, dag_id="00_daily", run_id=""))


def test_orchestrator_templates_render_without_logical_date() -> None:
    """Render actual trigger expressions with Airflow 3's undated manual context."""
    import re

    root = Path(__file__).resolve().parents[1] / "dags"
    parent = SimpleNamespace(logical_date=None, dag_id="00_daily", run_id="manual_test")
    env = NativeEnvironment(undefined=StrictUndefined)
    env.globals.update(trigger_run_suffix=trigger_run_suffix, trigger_logical_date=trigger_logical_date)
    for filename in ("00_dag_dq_platform_daily_orchestrator.py", "10_dag_dq_orders_landing_orchestrator.py"):
        source = (root / filename).read_text(encoding="utf-8")
        expressions = re.findall(r'(trigger_run_id|logical_date)="([^"]+)"', source)
        assert expressions
        for field, template in expressions:
            result = env.from_string(template).render(dag_run=parent)
            if field == "logical_date":
                assert result is None
            else:
                assert result.endswith(trigger_run_suffix(parent))
