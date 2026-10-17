####
## Scoped Incident Revalidation for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Read one contracted incident scope and persist fresh verification evidence.

This adapter does not trigger pipelines, generate alerts, close incidents, or
invoke a model. Infrastructure errors propagate to the request owner rather
than being represented as a successful business check.
"""

# --- Importing Libraries
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from pipelines.common.clickhouse import format_date_literal, quote_sql_literal, split_table_name
from pipelines.common.logging import logger
from pipelines.dq.config import OrdersDqContract, load_orders_dq_contract
from pipelines.dq import run_checks
from pipelines.schema_drift.config import SchemaContractConfig, SchemaTableConfig, load_named_schema_contract
from pipelines.schema_drift.detector import (
    compare_table_schema,
    fetch_observed_columns,
    observed_schema_sha256,
    schema_contract_sha256,
)
from pipelines.schema_drift.models import ObservedColumn, SchemaDriftEvaluation
from pipelines.schema_drift.storage import persist_schema_evaluation


# --- Defining Constants
MAX_WATERMARK_PARTS = 10000
REVALIDATION_SEMANTIC_VERSION = "2-exact-country-channel"
SCOPE_FIELDS = frozenset({"kind", "table_name", "dt", "metric", "dimension"})
SCHEMA_REVALIDATION_ALIASES = {
    "dq_resolution_acceptance.schema_fixture": "resolution_acceptance",
}


# --- Defining Scope Contracts
@dataclass(frozen=True)
class _Scope:
    """Validated incident identity; no SQL, paths, or client-selected contract."""

    kind: str
    table_name: str
    dt: date | None
    metric: str
    dimension: str


def _parse_scope(scope: dict[str, Any]) -> _Scope:
    """Validate scope shape before any warehouse query.

    Raises:
        ValueError: For unsupported kinds, extra fields, or malformed identifiers.
    """
    if not isinstance(scope, dict) or set(scope) - SCOPE_FIELDS:
        raise ValueError("Revalidation accepts incident scope fields only.")
    kind = scope.get("kind")
    if not isinstance(kind, str) or kind not in {"dq_failure", "schema_drift"}:
        raise ValueError("Unsupported incident kind for revalidation.")
    table_name = scope.get("table_name")
    if not isinstance(table_name, str):
        raise ValueError("Incident table_name is required.")
    split_table_name(table_name)
    metric = scope.get("metric", "")
    dimension = scope.get("dimension", "")
    if not isinstance(metric, str) or not isinstance(dimension, str):
        raise ValueError("Incident metric and dimension must be strings.")
    if len(metric) > 200 or len(dimension) > 200:
        raise ValueError("Incident metric or dimension is too long.")
    value = scope.get("dt")
    if isinstance(value, datetime):
        raise ValueError("Incident dt must be a business date, not a timestamp.")
    try:
        business_date = value if isinstance(value, date) else date.fromisoformat(value) if value else None
    except (TypeError, ValueError) as exc:
        raise ValueError("Incident dt must use YYYY-MM-DD.") from exc
    if isinstance(value, str) and business_date and value != business_date.isoformat():
        raise ValueError("Incident dt must use YYYY-MM-DD.")
    if kind == "dq_failure" and business_date is None:
        raise ValueError("DQ revalidation requires the incident business date.")
    return _Scope(kind, table_name, business_date, metric, "" if dimension == "table" else dimension)


def _schema_table(contract: SchemaContractConfig, scope: _Scope) -> SchemaTableConfig:
    """Find the affected table in the approved repository contract."""
    for table in contract.tables:
        if table.qualified_name == scope.table_name:
            return table
    raise ValueError("Incident table is not in the approved repository schema contract.")


def _dq_rule(contract: OrdersDqContract, scope: _Scope) -> tuple[str, set[str]]:
    """Resolve the exact contracted rule and required columns.

    Returns:
        Quality configuration key and required column names.

    Raises:
        ValueError: If the table, metric, or dimension does not match a rule.
    """
    quality = contract.quality
    if scope.metric == "row_count_positive":
        if not any(check.table_name == scope.table_name for check in quality.row_count_positive):
            raise ValueError("Row-count rule is not contracted for this table.")
        key, columns, dimension = "row_count_positive", {"dt"}, ""
    elif scope.metric.startswith("not_null__"):
        column = scope.metric.removeprefix("not_null__")
        if column not in quality.not_null.columns:
            raise ValueError("Null check column is not contracted.")
        key, columns, dimension = "not_null", {"dt", column}, column
    elif scope.metric.startswith("accepted_values__"):
        column = scope.metric.removeprefix("accepted_values__")
        if column not in quality.accepted_values.columns:
            raise ValueError("Accepted-values column is not contracted.")
        key, columns, dimension = "accepted_values", {"dt", column}, column
    else:
        rules = {
            "freshness__max_dt_lag": ("freshness", {quality.freshness.date_column}, "max_dt_lag"),
            "segment_coverage__country_channel": ("segment_coverage", {"dt", "country", "channel"}, "country_channel"),
            "rowcount_anomaly__daily_total_vs_lookback_avg": ("rowcount_anomaly", {"dt", "row_count"}, "daily_total_vs_lookback_avg"),
            "duplicate_rate__daily_total": ("duplicate_rate", {"dt", "duplicate_order_count", "row_count"}, "daily_total"),
            "late_arriving_rate__daily_total": ("late_arriving_rate", {"dt", "late_arriving_count", "row_count"}, "daily_total"),
            f"non_negative__{quality.revenue_non_negative.metric_column}": (
                "revenue_non_negative", {"dt", quality.revenue_non_negative.metric_column}, quality.revenue_non_negative.metric_column,
            ),
        }
        if scope.metric not in rules:
            raise ValueError("Incident rule is not supported by the approved DQ contract.")
        key, columns, dimension = rules[scope.metric]
    if key != "row_count_positive" and getattr(quality, key).table_name != scope.table_name:
        raise ValueError("Incident rule is not contracted for this table.")
    if scope.dimension != dimension:
        raise ValueError("Incident dimension does not match the contracted rule.")
    return key, columns


def _resolve(scope: dict[str, Any]) -> tuple[_Scope, OrdersDqContract | None, SchemaContractConfig, SchemaTableConfig]:
    """Load only fixed repository contracts and validate the scope."""
    parsed = _parse_scope(scope)
    # The fixture alias is chosen only by this exact authoritative table identity.
    alias = SCHEMA_REVALIDATION_ALIASES.get(parsed.table_name, "orders") if parsed.kind == "schema_drift" else "orders"
    schema_contract, _ = load_named_schema_contract(alias)
    table = _schema_table(schema_contract, parsed)
    dq_contract = load_orders_dq_contract() if parsed.kind == "dq_failure" else None
    if dq_contract is not None:
        _dq_rule(dq_contract, parsed)
    elif parsed.metric != "schema_contract_drift" or parsed.dimension:
        raise ValueError("Schema incident scope must use the table-level schema drift metric.")
    return parsed, dq_contract, schema_contract, table


# --- Defining Change Detection Helpers
def _hash(payload: Any) -> str:
    """Hash JSON-normalized metadata without retaining credentials or raw rows."""
    encoded = json.dumps(payload, default=str, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _json_safe(payload: Any) -> Any:
    """Keep non-finite observations out of JSON evidence and API responses."""
    if isinstance(payload, float) and not math.isfinite(payload):
        return None
    if isinstance(payload, dict):
        return {key: _json_safe(value) for key, value in payload.items()}
    if isinstance(payload, (list, tuple)):
        return [_json_safe(value) for value in payload]
    return payload


def _contract_hash(dq_contract: OrdersDqContract | None, schema_contract: SchemaContractConfig) -> str:
    """Invalidate saved PASS when contracts or revalidation semantics change."""
    schema_hash = schema_contract_sha256(schema_contract)
    return _hash({
        "revalidation_semantic_version": REVALIDATION_SEMANTIC_VERSION,
        "dq": dq_contract.model_dump(mode="json") if dq_contract is not None else None,
        "schema": schema_hash,
    })


def current_contract_hash(scope: dict[str, Any]) -> str:
    """Return the current approved contract hash for this exact incident scope."""
    _, dq_contract, schema_contract, _ = _resolve(scope)
    return _contract_hash(dq_contract, schema_contract)


def current_schema_hash(client: Any, scope: dict[str, Any]) -> str:
    """Return a fresh affected-table schema fingerprint from system.columns."""
    _, _, _, table = _resolve(scope)
    return observed_schema_sha256(fetch_observed_columns(client, table))


def _data_watermark(client: Any, scope: _Scope, contract: OrdersDqContract | None) -> str:
    """Fingerprint active parts and recorded writes without scanning raw data.

    Part merges conservatively invalidate proof. This is not a transactional
    warehouse lock or a guarantee against unlogged direct warehouse changes.
    """
    database, table = split_table_name(scope.table_name)
    part_filter = ""
    run_filter = ""
    if scope.kind == "dq_failure":
        run_filter = f" AND (partition_dt IS NULL OR partition_dt = {format_date_literal(scope.dt)})"
        if scope.metric == "freshness__max_dt_lag":
            part_filter = f" AND partition <= {quote_sql_literal(scope.dt.isoformat())}"
        else:
            lower = scope.dt
            if scope.metric == "rowcount_anomaly__daily_total_vs_lookback_avg":
                lower -= timedelta(days=contract.quality.rowcount_anomaly.lookback_days)
            part_filter = (
                f" AND partition >= {quote_sql_literal(lower.isoformat())}"
                f" AND partition <= {quote_sql_literal(scope.dt.isoformat())}"
            )
            run_filter = (
                f" AND (partition_dt IS NULL OR (partition_dt >= {format_date_literal(lower)}"
                f" AND partition_dt <= {format_date_literal(scope.dt)}))"
            )
    parts = client.query(
        f"""SELECT name, rows, modification_time, data_version
        FROM system.parts
        WHERE database = {quote_sql_literal(database)} AND table = {quote_sql_literal(table)}
          AND active{part_filter}
        ORDER BY name LIMIT {MAX_WATERMARK_PARTS + 1}"""
    ).result_rows
    if len(parts) > MAX_WATERMARK_PARTS:
        raise ValueError("Scoped data watermark exceeded the bounded part limit.")
    target_filter = f"target_table = {quote_sql_literal(scope.table_name)}"
    if scope.table_name in {"dq.stg_orders", "dq.fct_orders_daily"}:
        # dbt records its outputs under a namespace instead of individual tables.
        target_filter = f"({target_filter} OR (target_table = 'warehouse/dbt' AND job_name = 'dbt_run'))"
    runs = client.query(
        f"""SELECT count(), max(created_at), max(started_at), max(ended_at)
        FROM dq.pipeline_runs FINAL
        WHERE {target_filter}{run_filter}"""
    ).result_rows
    if len(runs) != 1:
        raise ValueError("Recorded data watermark is incomplete.")
    return _hash({"table": scope.table_name, "parts": parts, "recorded_writes": runs})


def current_data_watermark(client: Any, scope: dict[str, Any]) -> str:
    """Return the current read-only data-change fingerprint for the scoped table."""
    parsed, contract, _, _ = _resolve(scope)
    return _data_watermark(client, parsed, contract)


# --- Defining DQ Capture Helpers
def _scoped_contract(contract: OrdersDqContract, scope: _Scope) -> OrdersDqContract:
    """Narrow list-based existing check functions to one table or column."""
    row_checks = [
        check.model_copy(update={"min_rows": max(1, check.min_rows)})
        for check in contract.quality.row_count_positive if check.table_name == scope.table_name
    ]
    not_null = contract.quality.not_null.model_copy(update={"columns": [scope.dimension]})
    accepted = contract.quality.accepted_values.model_copy(
        update={"columns": {scope.dimension: contract.quality.accepted_values.columns.get(scope.dimension, [])}}
    )
    quality = contract.quality.model_copy(update={
        "row_count_positive": row_checks, "not_null": not_null, "accepted_values": accepted,
    })
    return contract.model_copy(update={"quality": quality})


def _run_rule(client: Any, scope: _Scope, contract: OrdersDqContract, key: str, timestamp: datetime) -> run_checks.DqCheckResult:
    """Invoke the existing deterministic function for the exact rule only."""
    if key in {"duplicate_rate", "late_arriving_rate"}:
        config = getattr(contract.quality, key)
        result = run_checks.check_rate_threshold(
            client=client, table_name=scope.table_name, dt=scope.dt, run_at=timestamp,
            metric_column="duplicate_order_count" if key == "duplicate_rate" else "late_arriving_count",
            denominator_column="row_count", check_name=scope.metric,
            severity=config.severity, max_rate=config.max_rate,
        )
    else:
        functions = {
            "freshness": run_checks.check_freshness,
            "row_count_positive": run_checks.check_row_count_positive,
            "not_null": run_checks.check_not_null,
            "accepted_values": run_checks.check_accepted_values,
            "segment_coverage": run_checks.check_segment_coverage,
            "rowcount_anomaly": run_checks.check_rowcount_anomaly,
            "revenue_non_negative": run_checks.check_revenue_non_negative,
        }
        result = functions[key](client=client, contract=contract, dt=scope.dt, run_at=timestamp)
    if isinstance(result, list):
        if len(result) != 1:
            raise ValueError("Scoped rule did not return exactly one result.")
        result = result[0]
    if result.table_name != scope.table_name or result.check_name != scope.metric or result.dt != scope.dt:
        raise ValueError("Scoped result identity does not match the incident.")
    return result


def _serialize_dq(result: run_checks.DqCheckResult, dimension: str) -> dict[str, Any]:
    """Expose exact persisted evidence IDs and scope, without raw records."""
    return {
        "check_run_id": str(result.check_run_id), "run_at": result.run_at.isoformat(),
        "table_name": result.table_name, "dt": result.dt.isoformat(), "metric": result.check_name,
        "dimension": dimension, "status": result.status, "expected": result.expected_value,
        "observed": result.observed_value, "threshold": result.threshold_value, "details": result.details,
    }


def _capture_dq(client: Any, scope: _Scope, contract: OrdersDqContract, columns: tuple[ObservedColumn, ...], timestamp: datetime) -> tuple[list[run_checks.DqCheckResult], list[str]]:
    """Capture one rule with metadata, positive-volume, and denominator gates."""
    key, required_columns = _dq_rule(contract, scope)
    scoped = _scoped_contract(contract, scope)
    required_columns.add("dt")
    available = {column.name for column in columns}
    missing = sorted(required_columns - available)
    blockers: list[str] = []
    if not columns or missing:
        blockers.append("table_missing" if not columns else "required_columns_missing")
        target = run_checks.build_check_result(
            run_at=timestamp, dt=scope.dt, table_name=scope.table_name, check_name=scope.metric,
            check_type="prerequisite", status="fail", severity="critical",
            details={"missing_columns": missing, "resolution_blockers": blockers},
        )
        return [target], blockers
    if next(column for column in columns if column.name == "dt").data_type not in {"Date", "Date32"}:
        raise ValueError("DQ business-date column no longer has a supported date type.")
    if not scoped.quality.row_count_positive:
        raise ValueError("Scoped table has no contracted positive-row prerequisite.")
    prerequisite = run_checks.check_row_count_positive(client, scoped, scope.dt, timestamp)[0]
    if key == "row_count_positive":
        return [prerequisite], [] if prerequisite.status == "pass" else ["positive_rows_required"]
    if prerequisite.status != "pass":
        blockers.append("positive_rows_required")
        target = run_checks.build_check_result(
            run_at=timestamp, dt=scope.dt, table_name=scope.table_name, check_name=scope.metric,
            check_type="prerequisite", status="fail", severity="critical",
            details={"resolution_blockers": blockers, "row_count": prerequisite.observed_value},
        )
    else:
        target = _run_rule(client, scope, scoped, key, timestamp)
        if key in {"duplicate_rate", "late_arriving_rate"}:
            denominator = target.details.get("denominator")
            numerator = target.details.get("numerator")
            if denominator is None or not math.isfinite(denominator) or denominator <= 0:
                blockers.append("positive_denominator_required")
            if numerator is None or not math.isfinite(numerator) or numerator < 0:
                blockers.append("valid_numerator_required")
        if target.observed_value is None or not math.isfinite(target.observed_value):
            blockers.append("observed_value_unavailable")
        if target.status != "pass":
            blockers.append("rule_not_pass")
    if blockers:
        target = replace(target, status=target.status if target.status in {"warn", "fail", "skip"} else "fail",
                         details=dict(target.details, resolution_blockers=blockers))
    return [target, prerequisite], blockers


# --- Defining Public Capture Adapter
def capture_revalidation(client: Any, scope: dict[str, Any]) -> dict[str, Any]:
    """Capture fresh exact-scope evidence and persist observability records only.

    Args:
        client: ClickHouse client with query and observability insert access.
        scope: Authoritative kind/table_name/dt/metric/dimension dictionary.

    Returns:
        JSON-safe pass/blocked result, exact evidence references, contract hash,
        data watermark, schema hash, and scoped checks. No lifecycle is changed.

    Raises:
        ValueError: Unsupported scope or incomplete bounded evidence.
        Exception: Warehouse read or observability persistence failure. The parent
            must record infrastructure errors, never treat them as PASS.
    """
    parsed, dq_contract, schema_contract, table = _resolve(scope)
    timestamp = datetime.now(timezone.utc)
    contract_hash = _contract_hash(dq_contract, schema_contract)
    before_watermark = _data_watermark(client, parsed, dq_contract)
    columns = fetch_observed_columns(client, table)
    schema_hash = observed_schema_sha256(columns)
    if dq_contract is not None:
        results, blockers = _capture_dq(client, parsed, dq_contract, columns, timestamp)
        expected, observed = _json_safe(results[0].expected_value), _json_safe(results[0].observed_value)
        if parsed.metric == "segment_coverage__country_channel":
            expected = results[0].details.get("expected_segment_count", expected)
            observed = results[0].details.get("observed_segment_count", observed)
        schema_after = observed_schema_sha256(fetch_observed_columns(client, table))
        after_watermark = _data_watermark(client, parsed, dq_contract)
        if schema_after != schema_hash or after_watermark != before_watermark:
            blockers.append("scope_changed_during_check")
            results[0] = replace(results[0], status="fail", details=dict(results[0].details, resolution_blockers=blockers))
        status = "pass" if not blockers and all(result.status == "pass" for result in results) else "blocked"
        results = [replace(result, observed_value=_json_safe(result.observed_value),
                           expected_value=_json_safe(result.expected_value), threshold_value=_json_safe(result.threshold_value),
                           details=_json_safe(result.details)) for result in results]
        run_checks.insert_check_results(client, results)
        checks = [_serialize_dq(result, parsed.dimension if index == 0 else "") for index, result in enumerate(results)]
        references = [f"clickhouse://dq.dq_check_results/{result.check_run_id}" for result in results]
    else:
        run_id = f"incident_revalidation_{uuid4().hex}"
        snapshot, comparisons = compare_table_schema(
            run_id=run_id, observed_at=timestamp, contract=schema_contract,
            contract_sha256=contract_hash, table=table, observed_columns=columns,
        )
        evaluation = SchemaDriftEvaluation(
            run_id=run_id, observed_at=timestamp, contract_name=schema_contract.contract_name,
            contract_sha256=contract_hash, fail_on_severity=schema_contract.policy.fail_on_severity,
            snapshots=(snapshot,), results=comparisons,
        )
        schema_after = observed_schema_sha256(fetch_observed_columns(client, table))
        after_watermark = _data_watermark(client, parsed, None)
        clean = snapshot.status == "pass" and bool(comparisons) and all(result.status == "pass" for result in comparisons)
        blockers = [] if clean else ["schema_contract_not_pass"]
        if schema_after != schema_hash or after_watermark != before_watermark:
            blockers.append("scope_changed_during_check")
        status = "pass" if not blockers else "blocked"
        persist_schema_evaluation(client, evaluation)
        expected, observed = "Approved schema without warnings or failures", f"{snapshot.finding_count} schema findings"
        checks = [{"result_id": str(result.result_id), "snapshot_id": str(result.snapshot_id),
                   "run_id": result.run_id, "observed_at": result.observed_at.isoformat(),
                   "table_name": result.qualified_name, "metric": result.check_type, "dimension": result.column_name,
                   "status": result.status, "expected": result.expected_value, "observed": result.actual_value}
                  for result in comparisons]
        references = [f"clickhouse://dq.schema_snapshots/{snapshot.snapshot_id}"]
        references.extend(f"clickhouse://dq.schema_drift_results/{result.result_id}" for result in comparisons)
    summary = (
        "Check passed. The issue was not found in the latest check for this incident."
        if status == "pass" else "Cannot close yet. A problem remains or verification evidence is incomplete."
    )
    logger.info("Scoped incident revalidation completed | table=%s kind=%s status=%s checks=%d",
                parsed.table_name, parsed.kind, status, len(checks))
    return {"status": status, "expected": expected, "observed": observed, "summary": summary,
            "evidence_references": references, "contract_hash": contract_hash,
            "data_watermark": before_watermark, "schema_hash": schema_hash, "checks": checks,
            "blockers": blockers, "observed_at": timestamp.isoformat()}
