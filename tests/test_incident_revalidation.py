####
## Scoped Incident Revalidation Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Isolated warehouse fakes and static DAG checks; no production schema writes."""

# --- Importing Libraries
from __future__ import annotations

import ast
import json
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from pipelines.dq import incident_revalidation as adapter
from pipelines.schema_drift.config import load_named_schema_contract


# --- Defining Test Fixtures
BUSINESS_DATE = date(2026, 10, 5)
DAG_PATH = Path(__file__).resolve().parents[1] / "dags" / "90_03_dag_dq_incident_revalidation.py"


def dq_scope(metric: str = "segment_coverage__country_channel", table: str = "dq.fct_orders_daily") -> dict[str, Any]:
    """Build the same dimension convention used by the alert producer."""
    return {"kind": "dq_failure", "table_name": table, "dt": BUSINESS_DATE.isoformat(),
            "metric": metric, "dimension": metric.split("__", 1)[1] if "__" in metric else ""}


def schema_scope(table: str = "dq.fct_orders_daily") -> dict[str, Any]:
    """Schema revalidation is table-scoped, not incident-date partitioned."""
    return {"kind": "schema_drift", "table_name": table, "dt": BUSINESS_DATE.isoformat(),
            "metric": "schema_contract_drift", "dimension": ""}


class FakeWarehouse:
    """Answer only explicit SELECTs and record observability inserts."""

    def __init__(self) -> None:
        """Start with clean synthetic contracted schemas and non-empty data."""
        contracts = [load_named_schema_contract(alias)[0] for alias in ("orders", "resolution_acceptance")]
        self.columns = {
            table.qualified_name: [(index, column.name, column.data_type, column.default_kind, column.default_expression)
                                  for index, column in enumerate(table.columns, 1)]
            for contract in contracts for table in contract.tables
        }
        self.segments = [(country, channel) for country in ["ID", "SG", "MY", "TH", "VN"]
                         for channel in ["organic", "paid", "referral", "direct", "affiliate"]]
        self.physical_rows = 25
        self.numerator = 0.0
        self.denominator = 500.0
        self.history_days = 4
        self.average_rows = 500.0
        self.bad_rows = 0
        self.max_dt = BUSINESS_DATE
        self.part_reads = 0
        self.schema_reads = 0
        self.change_parts = False
        self.change_schema = False
        self.read_error = False
        self.insert_error = False
        self.queries: list[str] = []
        self.inserts: list[dict[str, Any]] = []

    def query(self, sql: str) -> SimpleNamespace:
        """Dispatch known bounded query shapes; reject mutations or surprises."""
        normalized = " ".join(sql.split())
        assert normalized.startswith("SELECT ")
        self.queries.append(normalized)
        if self.read_error:
            raise RuntimeError("Synthetic infrastructure read error")
        if "FROM system.parts" in normalized:
            self.part_reads += 1
            generation = 2 if self.change_parts and self.part_reads > 1 else 1
            return SimpleNamespace(result_rows=[(f"20261005_{generation}_1_0", self.physical_rows, "2026-10-05T12:00:00", generation)])
        if "FROM dq.pipeline_runs FINAL" in normalized:
            return SimpleNamespace(result_rows=[(0, None, None, None)])
        if "FROM system.columns" in normalized:
            self.schema_reads += 1
            for name, rows in self.columns.items():
                if f"table = '{name.split('.')[1]}'" in normalized:
                    if self.change_schema and self.schema_reads > 1:
                        rows = rows + [(len(rows) + 1, "new_column", "String", "", "")]
                    return SimpleNamespace(result_rows=rows)
            return SimpleNamespace(result_rows=[])
        assert f"toDate('{BUSINESS_DATE.isoformat()}')" in normalized
        if "GROUP BY country, channel" in normalized:
            rows = self.segments
        elif "uniqExact(concat" in normalized:
            rows = [(len(self.segments),)]
        elif "SELECT count() FROM" in normalized:
            rows = [(self.physical_rows,)]
        elif "SELECT max(dt)" in normalized:
            rows = [(self.max_dt,)]
        elif "AS numerator" in normalized:
            rows = [(self.numerator, self.denominator)]
        elif "AS negative_rows" in normalized:
            rows = [(self.bad_rows, -5 if self.bad_rows else 0)]
        elif "AS total_rows" in normalized:
            rows = [(self.physical_rows, self.bad_rows)]
        elif "AS history_days" in normalized:
            rows = [(self.history_days, self.average_rows)]
        elif "SELECT sum(row_count)" in normalized:
            rows = [(self.denominator,)]
        else:
            raise AssertionError(f"Unexpected SQL shape: {normalized}")
        return SimpleNamespace(result_rows=rows)

    def insert(self, *, table: str, data: list[Any], column_names: Any) -> None:
        """Restrict all writes to existing observability tables."""
        assert table in {"dq.dq_check_results", "dq.schema_snapshots", "dq.schema_drift_results"}
        if self.insert_error:
            raise RuntimeError("Synthetic observability persistence error")
        self.inserts.append({"table": table, "data": data, "column_names": column_names})

    def command(self, sql: str) -> None:
        """Any DDL or warehouse mutation fails the isolated test immediately."""
        raise AssertionError("Revalidation cannot issue command/DDL/mutation.")


# --- Defining DQ Adapter Tests
def test_fresh_coverage_pass_has_exact_persisted_evidence_and_counts() -> None:
    """A clean exact-date rule produces new evidence, not historical PASS."""
    client = FakeWarehouse()
    result = adapter.capture_revalidation(client, dq_scope())
    assert result["status"] == "pass"
    assert result["expected"] == result["observed"] == 25
    assert result["blockers"] == []
    assert "Check passed" in result["summary"]
    assert len(result["checks"]) == 2
    assert len(result["contract_hash"]) == len(result["data_watermark"]) == len(result["schema_hash"]) == 64
    assert result["contract_hash"] == adapter.current_contract_hash(dq_scope())
    assert result["data_watermark"] == adapter.current_data_watermark(client, dq_scope())
    assert result["schema_hash"] == adapter.current_schema_hash(client, dq_scope())
    stored_ids = {str(row[0]) for row in client.inserts[0]["data"]}
    assert stored_ids == {check["check_run_id"] for check in result["checks"]}
    assert stored_ids == {reference.rsplit("/", 1)[1] for reference in result["evidence_references"]}
    assert result["checks"][0]["metric"] == "segment_coverage__country_channel"
    assert result["checks"][0]["dt"] == BUSINESS_DATE.isoformat()
    assert result["checks"][0]["dimension"] == "country_channel"
    assert datetime.fromisoformat(result["observed_at"]).tzinfo is not None
    assert all("FROM dq.dq_check_results" not in sql for sql in client.queries)
    json.dumps(result)


def test_missing_segment_warn_blocks_closure() -> None:
    client = FakeWarehouse()
    client.segments.pop()
    result = adapter.capture_revalidation(client, dq_scope())
    assert result["status"] == "blocked"
    assert (result["expected"], result["observed"]) == (25, 24)
    assert result["checks"][0]["status"] == "warn"
    assert result["checks"][0]["details"]["missing_segments"]


def test_unknown_segment_cannot_replace_missing_expected_segment() -> None:
    client = FakeWarehouse()
    client.segments[-1] = ("ZZ", "other")
    result = adapter.capture_revalidation(client, dq_scope())
    assert len(client.segments) == 25
    assert result["observed"] == 24
    assert result["status"] == "blocked"


def test_unexpected_segment_blocks_closure_even_when_all_expected_pairs_exist() -> None:
    client = FakeWarehouse()
    client.segments.append(("ZZ", "other"))
    result = adapter.capture_revalidation(client, dq_scope())
    assert result["status"] == "blocked"
    assert result["observed"] == 25
    assert result["checks"][0]["details"]["unexpected_segments"] == [["ZZ", "other"]]
    assert result["checks"][0]["status"] == "warn"
    assert len([sql for sql in client.queries if "GROUP BY country, channel" in sql]) == 1


def test_empty_segment_evidence_never_passes_despite_positive_row_prerequisite() -> None:
    client = FakeWarehouse()
    client.segments = []
    result = adapter.capture_revalidation(client, dq_scope())
    assert result["status"] == "blocked"
    assert result["observed"] == 0
    assert result["checks"][0]["status"] == "warn"


def test_segment_evidence_overflow_produces_no_pass_or_persisted_checks() -> None:
    from pipelines.dq.segments import MAX_COVERAGE_SEGMENTS

    client = FakeWarehouse()
    client.segments = [(str(index), "direct") for index in range(MAX_COVERAGE_SEGMENTS + 1)]
    with pytest.raises(ValueError, match="evidence exceeded"):
        adapter.capture_revalidation(client, dq_scope())
    assert client.inserts == []


@pytest.mark.parametrize("scope_factory", [dq_scope, schema_scope])
def test_contract_hash_invalidates_legacy_and_changed_semantic_pass(monkeypatch, scope_factory) -> None:
    scope = scope_factory()
    _, dq_contract, schema_contract, _ = adapter._resolve(scope)
    schema_hash = adapter.schema_contract_sha256(schema_contract)
    legacy = schema_hash if dq_contract is None else adapter._hash({
        "dq": dq_contract.model_dump(mode="json"), "schema": schema_hash,
    })
    current = adapter.current_contract_hash(scope)
    assert current != legacy
    monkeypatch.setattr(adapter, "REVALIDATION_SEMANTIC_VERSION", "next-test-only-version")
    assert adapter.current_contract_hash(scope) != current
    assert adapter.capture_revalidation(FakeWarehouse(), scope)["contract_hash"] == adapter.current_contract_hash(scope)


@pytest.mark.parametrize("metric", ["duplicate_rate__daily_total", "late_arriving_rate__daily_total"])
@pytest.mark.parametrize("denominator", [0.0, -1.0, float("nan"), float("inf")])
def test_zero_or_invalid_denominator_never_passes(metric: str, denominator: float) -> None:
    client = FakeWarehouse()
    client.denominator = denominator
    result = adapter.capture_revalidation(client, dq_scope(metric))
    assert result["status"] == "blocked"
    assert "positive_denominator_required" in result["blockers"]
    assert result["checks"][0]["status"] != "pass"
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("metric,table", [
    ("row_count_positive", "dq.raw_orders"),
    ("freshness__max_dt_lag", "dq.stg_orders"),
    ("not_null__customer_id", "dq.stg_orders"),
    ("accepted_values__country", "dq.stg_orders"),
    ("segment_coverage__country_channel", "dq.fct_orders_daily"),
    ("rowcount_anomaly__daily_total_vs_lookback_avg", "dq.fct_orders_daily"),
    ("duplicate_rate__daily_total", "dq.fct_orders_daily"),
    ("late_arriving_rate__daily_total", "dq.fct_orders_daily"),
    ("non_negative__recognized_revenue_usd", "dq.fct_orders_daily"),
])
def test_all_supported_rules_run_only_affected_table(metric: str, table: str) -> None:
    client = FakeWarehouse()
    result = adapter.capture_revalidation(client, dq_scope(metric, table))
    assert result["status"] == "pass"
    assert all(check["table_name"] == table for check in result["checks"])
    for other_table in {"dq.raw_orders", "dq.stg_orders", "dq.fct_orders_daily"} - {table}:
        assert not any(f"FROM {other_table} " in sql for sql in client.queries)
        assert not any(f"table = '{other_table.split('.')[1]}'" in sql for sql in client.queries)


@pytest.mark.parametrize("metric", ["not_null__customer_id", "accepted_values__country"])
def test_empty_table_never_looks_like_complete_or_valid_data(metric: str) -> None:
    client = FakeWarehouse()
    client.physical_rows = 0
    result = adapter.capture_revalidation(client, dq_scope(metric, "dq.stg_orders"))
    assert result["status"] == "blocked"
    assert "positive_rows_required" in result["blockers"]
    assert all(check["status"] != "pass" for check in result["checks"])


def test_empty_aggregate_denominator_with_nonempty_physical_table_blocks() -> None:
    client = FakeWarehouse()
    client.denominator = 0
    result = adapter.capture_revalidation(client, dq_scope("duplicate_rate__daily_total"))
    assert result["checks"][1]["status"] == "pass"
    assert result["checks"][0]["status"] == "fail"
    assert result["status"] == "blocked"


def test_anomaly_skip_does_not_count_as_pass() -> None:
    client = FakeWarehouse()
    client.history_days = 0
    result = adapter.capture_revalidation(client, dq_scope("rowcount_anomaly__daily_total_vs_lookback_avg"))
    assert result["status"] == "blocked"
    assert result["checks"][0]["status"] == "skip"


@pytest.mark.parametrize("missing_table", [True, False])
def test_missing_table_or_required_column_is_blocked_without_bad_data_sql(missing_table: bool) -> None:
    client = FakeWarehouse()
    client.columns["dq.stg_orders"] = [] if missing_table else [
        row for row in client.columns["dq.stg_orders"] if row[1] != "customer_id"
    ]
    result = adapter.capture_revalidation(client, dq_scope("not_null__customer_id", "dq.stg_orders"))
    assert result["status"] == "blocked"
    assert result["blockers"] == ["table_missing" if missing_table else "required_columns_missing"]
    assert not any("FROM dq.stg_orders " in sql for sql in client.queries)


@pytest.mark.parametrize("change", ["change_parts", "change_schema"])
def test_scope_change_while_reading_invalidates_pass(change: str) -> None:
    client = FakeWarehouse()
    setattr(client, change, True)
    result = adapter.capture_revalidation(client, dq_scope())
    assert result["status"] == "blocked"
    assert "scope_changed_during_check" in result["blockers"]
    assert result["checks"][0]["status"] == "fail"


def test_each_capture_has_new_dq_evidence_ids() -> None:
    client = FakeWarehouse()
    first = adapter.capture_revalidation(client, dq_scope())
    second = adapter.capture_revalidation(client, dq_scope())
    assert not set(first["evidence_references"]) & set(second["evidence_references"])


# --- Defining Schema Adapter Tests
def test_clean_schema_pass_captures_one_table_and_persists_exact_ids() -> None:
    client = FakeWarehouse()
    result = adapter.capture_revalidation(client, schema_scope())
    assert result["status"] == "pass"
    assert result["observed"] == "0 schema findings"
    assert all(check["table_name"] == "dq.fct_orders_daily" for check in result["checks"])
    assert {item["table"] for item in client.inserts} == {"dq.schema_snapshots", "dq.schema_drift_results"}
    snapshots = next(item for item in client.inserts if item["table"] == "dq.schema_snapshots")
    comparisons = next(item for item in client.inserts if item["table"] == "dq.schema_drift_results")
    assert len(snapshots["data"]) == 1
    persisted_ids = {str(row[0]) for insert in client.inserts for row in insert["data"]}
    assert persisted_ids == {reference.rsplit("/", 1)[1] for reference in result["evidence_references"]}
    assert len(comparisons["data"]) == len(result["checks"])
    assert result["contract_hash"] == adapter.current_contract_hash(schema_scope())
    assert all("partition_dt" not in sql and "AND partition " not in sql for sql in client.queries)
    json.dumps(result)


@pytest.mark.parametrize("drift", ["missing_table", "missing_column", "type_mismatch", "warning_column", "warning_order", "warning_default"])
def test_schema_fail_and_warning_both_block_despite_critical_only_gate(drift: str) -> None:
    client = FakeWarehouse()
    rows = client.columns["dq.fct_orders_daily"]
    if drift == "missing_table":
        client.columns["dq.fct_orders_daily"] = []
    elif drift == "missing_column":
        rows.pop()
    elif drift == "type_mismatch":
        rows[0] = (1, "dt", "String", "", "")
    elif drift == "warning_column":
        rows.append((len(rows) + 1, "new_nullable_column", "Nullable(String)", "", ""))
    elif drift == "warning_order":
        rows[0] = (9, *rows[0][1:])
    else:
        rows[0] = (*rows[0][:3], "DEFAULT", "today()")
    result = adapter.capture_revalidation(client, schema_scope())
    assert result["status"] == "blocked"
    assert "schema_contract_not_pass" in result["blockers"]
    assert any(check["status"] in {"warn", "fail"} for check in result["checks"])
    if drift.startswith("warning"):
        assert not any(check["status"] == "fail" for check in result["checks"])


def test_schema_ignores_historical_incident_date_but_reads_fresh_metadata() -> None:
    client = FakeWarehouse()
    scope = schema_scope()
    scope["dt"] = "2026-01-01"
    result = adapter.capture_revalidation(client, scope)
    assert result["status"] == "pass"
    assert all("2026-01-01" not in sql for sql in client.queries)
    assert client.schema_reads == 2


def test_schema_change_during_capture_blocks_clean_snapshot() -> None:
    client = FakeWarehouse()
    client.change_schema = True
    result = adapter.capture_revalidation(client, schema_scope())
    assert result["status"] == "blocked"
    assert result["blockers"] == ["scope_changed_during_check"]


def test_schema_evidence_is_new_for_each_request_capture() -> None:
    client = FakeWarehouse()
    first = adapter.capture_revalidation(client, schema_scope())
    second = adapter.capture_revalidation(client, schema_scope())
    assert not set(first["evidence_references"]) & set(second["evidence_references"])


@pytest.mark.parametrize("drift", [False, True])
def test_isolated_schema_fixture_uses_fixed_approved_alias(drift: bool) -> None:
    client = FakeWarehouse()
    table = "dq_resolution_acceptance.schema_fixture"
    if drift:
        client.columns[table].append((3, "unexpected", "String", "", ""))
    scope = schema_scope(table)
    result = adapter.capture_revalidation(client, scope)
    assert result["status"] == ("blocked" if drift else "pass")
    assert all(check["table_name"] == table for check in result["checks"])
    assert result["contract_hash"] == adapter.current_contract_hash(scope)
    assert result["schema_hash"] == adapter.current_schema_hash(client, scope)
    assert result["data_watermark"] == adapter.current_data_watermark(client, scope)
    assert all("orders'" not in sql for sql in client.queries)
    assert all("Date" in str(item["data"]) for item in client.inserts if item["table"] == "dq.schema_snapshots")


def test_client_cannot_select_acceptance_alias_for_any_other_table() -> None:
    client = FakeWarehouse()
    with pytest.raises(ValueError, match="scope fields"):
        adapter.capture_revalidation(client, dict(schema_scope(), contract_alias="resolution_acceptance"))
    with pytest.raises(ValueError, match="approved repository"):
        adapter.capture_revalidation(client, schema_scope("dq_resolution_acceptance.any_table"))
    with pytest.raises(ValueError, match="approved repository"):
        adapter.capture_revalidation(client, dq_scope("row_count_positive", "dq_resolution_acceptance.schema_fixture"))
    assert client.queries == []


def test_helpers_match_public_revalidation_result_without_type_coercion() -> None:
    from agent.resolution_contracts import RevalidationResult

    client = FakeWarehouse()
    for scope in (dq_scope(), schema_scope(), schema_scope("dq_resolution_acceptance.schema_fixture")):
        raw = adapter.capture_revalidation(client, scope)
        public = RevalidationResult.model_validate(raw).model_dump(mode="json")
        assert isinstance(public["data_watermark"], str)
        assert isinstance(public["schema_hash"], str)
        assert public["data_watermark"] == adapter.current_data_watermark(client, scope)
        assert public["schema_hash"] == adapter.current_schema_hash(client, scope)
        assert public["contract_hash"] == adapter.current_contract_hash(scope)


def test_recorded_write_watermark_includes_dbt_namespace_and_unpartitioned_runs() -> None:
    client = FakeWarehouse()
    adapter.current_data_watermark(client, dq_scope())
    sql = next(query for query in client.queries if "FROM dq.pipeline_runs" in query)
    assert "target_table = 'dq.fct_orders_daily'" in sql
    assert "target_table = 'warehouse/dbt'" in sql
    assert "job_name = 'dbt_run'" in sql
    assert "partition_dt IS NULL" in sql
    assert "max(started_at)" in sql and "max(ended_at)" in sql


# --- Defining Boundary Tests
@pytest.mark.parametrize("override", [
    {"kind": "anything"}, {"kind": []}, {"table_name": "dq.secret_table"}, {"table_name": "dq.fct_orders_daily; DROP TABLE dq.raw_orders"},
    {"metric": "not_a_rule"}, {"dimension": "other_country"}, {"metric": "not_null__secret_column"},
    {"table_name": "dq.stg_orders"}, {"dt": None}, {"dt": "2026-10-06' OR 1=1"},
    {"dt": "20261005"}, {"dt": datetime.now(timezone.utc)}, {"dt": 123},
    {"sql": "SELECT 1"}, {"contract_path": "arbitrary.yml"}, {"dimension": 12},
])
def test_invalid_scope_rejected_before_query(override: dict[str, Any]) -> None:
    client = FakeWarehouse()
    with pytest.raises(ValueError):
        adapter.capture_revalidation(client, dict(dq_scope(), **override))
    assert client.queries == []
    assert client.inserts == []


@pytest.mark.parametrize("operation", [adapter.capture_revalidation, adapter.current_data_watermark, adapter.current_schema_hash])
def test_infrastructure_errors_are_not_business_pass(operation: Any) -> None:
    client = FakeWarehouse()
    client.read_error = True
    with pytest.raises(RuntimeError, match="infrastructure"):
        operation(client, dq_scope())


def test_persistence_error_propagates_instead_of_returning_proof() -> None:
    client = FakeWarehouse()
    client.insert_error = True
    with pytest.raises(RuntimeError, match="persistence"):
        adapter.capture_revalidation(client, dq_scope())


def test_watermark_overflow_is_not_silent_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adapter, "MAX_WATERMARK_PARTS", 0)
    with pytest.raises(ValueError, match="part limit"):
        adapter.capture_revalidation(FakeWarehouse(), dq_scope())


def test_no_llm_or_pipeline_execution_dependencies_in_adapter() -> None:
    source = Path(adapter.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    assert not any("agent.llm" in module or "run_schema_drift" in module or "generate_alerts" in module for module in imports)
    assert not any(isinstance(node, ast.Attribute) and node.attr == "command" for node in ast.walk(tree))


# --- Defining Static DAG Tests
def test_revalidation_dag_is_manual_exact_request_and_retry_free() -> None:
    """Validate policy without importing Airflow internals into unit tests."""
    source = DAG_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    dag_call = next(node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "DAG")
    options = {keyword.arg: keyword.value for keyword in dag_call.keywords}
    assert ast.literal_eval(options["schedule"]) is None
    assert ast.literal_eval(options["max_active_runs"]) == 1
    assert ast.literal_eval(options["catchup"]) is False
    assert ast.literal_eval(options["is_paused_upon_creation"]) is False
    assert "default_dag_args(retries=0)" in source
    assert "DEFAULT_START_DATE" in source
    assert "Asia/Bangkok" in source
    param_func = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "incident_revalidation_params")
    params = next(node for node in ast.walk(param_func) if isinstance(node, ast.Dict))
    assert [ast.literal_eval(key) for key in params.keys] == ["resolution_request_id"]
    cli_func = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "request_stage_command")
    module = ast.Module(body=[cli_func], type_ignores=[])
    namespace: dict[str, Any] = {}
    exec(compile(module, str(DAG_PATH), "exec"), namespace)
    for stage in ("load", "checks", "persist", "notify"):
        assert namespace["request_stage_command"](stage) == (
            "python -m agent.tools.incident_resolution --request-id '{{ params.resolution_request_id }}' "
            f"--stage {stage}"
        )
    with pytest.raises(ValueError):
        namespace["request_stage_command"]("checks; arbitrary")
    task_calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "runner_plain_bash_task"]
    assert len(task_calls) == 4
    assert [ast.literal_eval(next(keyword.value for keyword in node.keywords if keyword.arg == "task_id")) for node in task_calls] == [
        "t10_load_request", "t20_run_scoped_checks", "t30_persist_validation_result", "t40_notify_validation_result",
    ]
    assert "t00_start >> t10_load_request >> t20_run_scoped_checks" in source
    assert ">> t30_persist_validation_result >> t40_notify_validation_result >> t90_finish" in source
    assert "TriggerDagRunOperator" not in source


def test_uuid_parameter_cannot_inject_shell_command() -> None:
    import re

    tree = ast.parse(DAG_PATH.read_text(encoding="utf-8"))
    assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == "REQUEST_ID_PATTERN" for target in node.targets))
    pattern = re.compile(ast.literal_eval(assignment.value))
    value = "6ec0ec72-7a41-4d20-a02d-53ae57f926a6"
    assert pattern.fullmatch(value)
    UUID(value)
    for invalid in ("", "not-a-uuid", value + "' ; env", value + "\n", "../" + value, "$(id)"):
        assert pattern.fullmatch(invalid) is None
