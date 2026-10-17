"""Validate actual DAG wiring with small public-semantics doubles, not Airflow internals."""

# --- Importing Libraries
from __future__ import annotations

import ast
import itertools
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from jinja2 import StrictUndefined
from jinja2.nativetypes import NativeEnvironment


# --- Defining Test Doubles
ROOT = Path(__file__).resolve().parents[1]
FLAG_NAMES = ("run_seed", "run_load", "run_dbt", "run_dq", "run_triage")
INVALID_FLAGS = (None, 0, 1, 0.0, 1.0, "", "0", "1", "yes", "no", "on", "off", " true ", "true\n", [], {}, [True])


class Task:
    """Record operator configuration and dependency edges only."""

    def __init__(self, task_id, **kwargs):
        self.task_id = task_id
        self.options = kwargs
        self.upstream = set()
        self.downstream = set()
        Dag.current.tasks[task_id] = self

    def __rshift__(self, other):
        self.downstream.add(other.task_id)
        other.upstream.add(self.task_id)
        return other


class Dag:
    """Context manager for executing repository DAG declarations without Airflow."""

    current = None

    def __init__(self, **kwargs):
        self.tasks = {}
        self.options = kwargs

    def __enter__(self):
        Dag.current = self
        return self

    def __exit__(self, *args):
        Dag.current = None


class Parameter:
    """Retain JSON-schema options to check the manual-run flag contract."""

    def __init__(self, default, **kwargs):
        self.default = default
        self.schema = kwargs


def helper_namespace():
    """Load only pure helpers and wiring factories through their actual AST."""
    path = ROOT / "dags/dq_platform/helpers.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = {
        "parse_stage_flag", "resolve_stage_flags", "select_stage_branch", "optional_stage",
        "single_date_conf", "common_dag_params", "default_incident_scenario",
        "acceptance_manual_only",
    }
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names
             or isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "STAGE_FLAG_DEFAULTS"
                                                    for target in node.targets)]
    namespace = {
        "Any": Any, "Param": Parameter, "EmptyOperator": Task, "BranchPythonOperator": Task,
        "logger": logging.getLogger(__name__), "os": os,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


def load_dag(filename):
    """Execute source declarations using doubles for external operators only."""
    helpers = helper_namespace()
    namespace = dict(helpers, DAG=Dag, TriggerDagRunOperator=Task, logging=logging,
                     DEFAULT_START_DATE=None, DAILY_PLATFORM_SCHEDULE="5 0 * * *",
                     DEFAULT_TRIGGER_RESET_DAG_RUN=True, default_dag_args=lambda: {},
                     default_user_defined_macros=lambda: {},
                     start_task=lambda: Task("t00_start"), finish_task=lambda: Task("t90_finish"))
    path = ROOT / "dags" / filename
    tree = ast.parse(path.read_text(encoding="utf-8"))
    tree.body = [node for node in tree.body if not isinstance(node, (ast.Import, ast.ImportFrom))]
    exec(compile(tree, str(path), "exec"), namespace)
    return namespace["dag"], helpers


def simulate(dag, conf, launched):
    """Apply all_success/none_failed_min_one_success and branch skip semantics.

    This is inner-loop graph evidence only; main owns live Airflow acceptance.
    """
    states = {}

    def descendants(task_id):
        found = set()
        pending = list(dag.tasks[task_id].downstream)
        while pending:
            current = pending.pop()
            if current not in found:
                found.add(current)
                pending.extend(dag.tasks[current].downstream)
        return found

    while len(states) < len(dag.tasks):
        before = len(states)
        for task_id, task in dag.tasks.items():
            if task_id in states or not task.upstream <= states.keys():
                continue
            upstream = [states[parent] for parent in task.upstream]
            if any(state in {"failed", "upstream_failed"} for state in upstream):
                states[task_id] = "upstream_failed"
                continue
            rule = task.options.get("trigger_rule", "all_success")
            assert rule in {"all_success", "none_failed_min_one_success"}
            if (rule == "all_success" and "skipped" in upstream
                    or rule == "none_failed_min_one_success" and "success" not in upstream):
                states[task_id] = "skipped"
                continue
            if "python_callable" in task.options:
                chosen = task.options["python_callable"](
                    **task.options["op_kwargs"], dag_run=SimpleNamespace(conf=conf),
                )
                assert chosen in task.downstream
                protected = descendants(chosen) | {chosen}
                for child in task.downstream - protected:
                    states[child] = "skipped"
            if "trigger_dag_id" in task.options:
                launched.append(task)
                states[task_id] = "failed" if conf.get("fail_task") == task_id else "success"
            else:
                states[task_id] = "success"
        assert len(states) > before, "DAG contains a cycle or unreachable dependency"
    return states


# --- Checking Strict Parsing And Propagation
@pytest.mark.parametrize("name", FLAG_NAMES)
@pytest.mark.parametrize("value", INVALID_FLAGS)
def test_invalid_stage_flags_never_silently_default_or_coerce(name, value):
    with pytest.raises(ValueError, match=name):
        helper_namespace()["resolve_stage_flags"]({name: value})


@pytest.mark.parametrize("value,expected", [(True, True), (False, False), ("true", True), ("false", False), ("TRUE", True), ("False", False)])
def test_explicit_boolean_and_true_false_strings_are_supported(value, expected):
    assert helper_namespace()["parse_stage_flag"](value, "run_seed") is expected


def test_defaults_and_manual_parameter_schemas_are_strict():
    from jsonschema import ValidationError, validate

    helpers = helper_namespace()
    defaults = dict(zip(FLAG_NAMES, (True, True, True, True, False)))
    assert helpers["resolve_stage_flags"]({}) == defaults
    assert helpers["resolve_stage_flags"](None) == defaults
    for name, default in defaults.items():
        param = helpers["common_dag_params"]()[name]
        assert param.default is default
        for valid in (True, False, "true", "false", "TRUE", "False"):
            validate(valid, param.schema)
        for invalid in INVALID_FLAGS:
            with pytest.raises(ValidationError):
                validate(invalid, param.schema)


def test_child_conf_propagates_every_flag_as_a_native_boolean_and_preserves_scenario_policy():
    helpers = helper_namespace()
    env = NativeEnvironment(undefined=StrictUndefined)
    env.globals.update(parse_stage_flag=helpers["parse_stage_flag"],
                       default_incident_scenario=helpers["default_incident_scenario"],
                       bangkok_ds=lambda _: "2026-10-05")
    for run_type, scenario in (("scheduled", "auto"), ("manual", "baseline")):
        parent = SimpleNamespace(conf={}, logical_date=None, run_type=run_type)
        templates = helpers["single_date_conf"]()
        rendered = {key: env.from_string(value).render(dag_run=parent) for key, value in templates.items()}
        assert rendered["incident_scenario"] == scenario
        assert rendered["dt"] == "2026-10-05"
        for name, default in helpers["STAGE_FLAG_DEFAULTS"].items():
            assert rendered[name] is default
            parent.conf[name] = "false" if default else "true"
            assert env.from_string(templates[name]).render(dag_run=parent) is not default
        parent.conf["incident_scenario"] = "missing_latest_day"
        assert env.from_string(templates["incident_scenario"]).render(dag_run=parent) == "missing_latest_day"


# --- Checking Independent Stage Routing
@pytest.mark.parametrize("values", list(itertools.product((False, True), repeat=5)))
@pytest.mark.parametrize("string_flags", [False, True])
def test_all_stage_combinations_skip_only_disabled_work(values, string_flags):
    flags = dict(zip(FLAG_NAMES, values))
    conf = {name: str(value).lower() if string_flags else value for name, value in flags.items()}
    daily, _ = load_dag("00_dag_dq_platform_daily_orchestrator.py")
    launched = []
    states = simulate(daily, conf, launched)
    assert states["t90_finish"] == "success"
    daily_expected = {
        "t10_trigger_landing": flags["run_seed"] or flags["run_load"],
        "t20_trigger_dbt_transform": flags["run_dbt"],
        "t30_trigger_quality_alerts": flags["run_dq"],
        "t40_trigger_agentic_triage": flags["run_triage"],
    }
    assert {task.task_id for task in launched} == {name for name, enabled in daily_expected.items() if enabled}
    for task_id, enabled in daily_expected.items():
        assert states[task_id] == ("success" if enabled else "skipped")
    landing, _ = load_dag("10_dag_dq_orders_landing_orchestrator.py")
    launched = []
    assert simulate(landing, conf, launched)["t90_finish"] == "success"
    assert {task.task_id for task in launched} == {
        task_id for task_id, name in (("t10_trigger_seed_to_s3", "run_seed"), ("t20_trigger_load_raw_clickhouse", "run_load"))
        if flags[name]
    }


@pytest.mark.parametrize("filename", ["00_dag_dq_platform_daily_orchestrator.py", "10_dag_dq_orders_landing_orchestrator.py"])
@pytest.mark.parametrize("name", FLAG_NAMES)
def test_invalid_flag_in_disabled_stage_is_rejected_before_any_trigger(filename, name):
    dag, _ = load_dag(filename)
    launched = []
    conf = {flag: False for flag in FLAG_NAMES}
    conf[name] = "off"
    with pytest.raises(ValueError, match=name):
        simulate(dag, conf, launched)
    assert launched == []


def test_enabled_stage_failure_is_not_masked_by_bypass_joins():
    dag, _ = load_dag("00_dag_dq_platform_daily_orchestrator.py")
    launched = []
    states = simulate(dag, {"run_seed": False, "run_load": False, "fail_task": "t20_trigger_dbt_transform"}, launched)
    assert states["t20_trigger_dbt_transform"] == "failed"
    assert states["t90_finish"] == "upstream_failed"
    assert [task.task_id for task in launched] == ["t20_trigger_dbt_transform"]
