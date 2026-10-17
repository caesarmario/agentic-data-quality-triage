"""Explicit synthetic fixture setup. Never imported by a recovery executor.

The dates are fixed, non-current acceptance scopes. Existing nonempty scopes are
refused instead of overwritten. This prepares faults, not an autonomous repair.
"""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from uuid import uuid4

CASES = {"raw": "2026-01-11", "derived": "2026-01-12", "both": "2026-01-10", "source_invalid": "2026-01-13"}


def prepare(case: str) -> dict:
    from pipelines.common.clickhouse import build_clickhouse_client, format_date_literal
    from pipelines.seeding.config import load_orders_config
    from pipelines.seeding.generate_orders import generate_and_write_orders, generate_orders_for_date
    from pipelines.seeding.upload_to_s3 import upload_orders_partition
    from pipelines.loading.load_clickhouse import load_orders_partition
    from pipelines.dbt.run_dbt import run_dbt_step
    from pipelines.dq.config import load_orders_dq_contract
    from pipelines.dq.run_checks import run_dq_checks
    from pipelines.dq.generate_alerts import run_alert_generation
    if case not in CASES:
        raise ValueError("Unknown synthetic acceptance case.")
    dt = date.fromisoformat(CASES[case])
    client, config = build_clickhouse_client(), load_orders_config()
    if case == "source_invalid":
        from agent.recovery.evidence import require_source_contract
        frame = generate_orders_for_date(dt, config, "baseline")
        frame.loc[0, "country"] = "ZZ"
        try:
            require_source_contract(frame, dt)
        except ValueError:
            return {"case": case, "status": "blocked", "reason": "Invalid upstream cannot become a recovery input", "warehouse_writes": 0}
        raise AssertionError("Invalid source unexpectedly passed.")
    if case != "both":
        for table in ("dq.raw_orders", "dq.stg_orders", "dq.fct_orders_daily"):
            if client.query(f"SELECT count() FROM {table} WHERE dt={format_date_literal(dt)}").result_rows[0][0]:
                raise ValueError("Fixture date already contains data; inspect the previous attempt instead of regenerating it.")
        from agent.recovery.store import latest_source_snapshot
        if latest_source_snapshot(dt.isoformat()):
            raise ValueError("Fixture source already exists; inspect rather than regenerate.")
        scenario = "missing_latest_day" if case == "raw" else "baseline"
        generate_and_write_orders(dt, config, incident_scenario=scenario,
                                  correlation_id="recovery_acceptance_" + case)
        upload_orders_partition(dt, config, correlation_id="recovery_acceptance_" + case)
        load_orders_partition(dt, config, allow_empty_simulation=(case == "raw"))
    if case in {"derived", "both"}:
        run_dbt_step("run", dt, dt, [dt], execution_id="acceptance_build:" + case + ":" + uuid4().hex)
    if case == "derived":
        from pipelines.common.publication import publication_lock, scratch_database, clone_live_table, prepare_partition, publish_partition
        execution_id = "acceptance_inject_derived:" + uuid4().hex
        with publication_lock("orders", dt, execution_id) as locked:
            stage = scratch_database(execution_id, dt) + ".fct_orders_daily"
            clone_live_table(client, "dq.fct_orders_daily", stage)
            client.command(f"INSERT INTO {stage} SELECT * FROM dq.fct_orders_daily WHERE dt={format_date_literal(dt)} "
                           "AND NOT (country='ID' AND channel='organic')")
            prepared = prepare_partition(client, locked, "dq.fct_orders_daily", stage, intent="daily")
            publish_partition(client, locked, prepared)
    contract = load_orders_dq_contract()
    run_dq_checks([dt], contract)
    run_alert_generation([dt], contract)
    table, metric = ("dq.raw_orders", "row_count_positive") if case == "raw" else ("dq.fct_orders_daily", "segment_coverage__country_channel")
    rows = client.query("SELECT alert_display_id,alert_key FROM dq.alerts FINAL "
        f"WHERE dt={format_date_literal(dt)} AND table_name='{table}' AND metric='{metric}' ORDER BY updated_at DESC LIMIT 1").result_rows
    if not rows:
        raise AssertionError("Fixture did not produce its expected incident.")
    result = {"case": case, "dt": dt.isoformat(), "alert_ref": rows[0][0], "alert_key": rows[0][1],
              "status": "ready", "fixture_is_not_recovery": True}
    root = Path("data/acceptance/recovery")
    root.mkdir(parents=True, exist_ok=True)
    (root / (case + ".json")).write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", required=True, choices=sorted(CASES))
    print(json.dumps(prepare(parser.parse_args().case), sort_keys=True))


if __name__ == "__main__":
    main()
