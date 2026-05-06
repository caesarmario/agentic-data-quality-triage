<!--
####
## README for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####
-->

# Agentic Data Quality Triage Platform

Local, Docker-based mini platform for demonstrating production-flavored data quality triage:

`SeaweedFS S3 -> ClickHouse raw -> dbt -> DQ/profiling -> alerts -> agentic triage -> Streamlit`

## Current Stack

- Airflow 3.1.7 with CeleryExecutor, Postgres metadata DB, and Redis broker.
- SeaweedFS master/volume/filer/S3 gateway for local S3-compatible landing and artifacts.
- ClickHouse for analytical warehouse tables.
- CH-UI for manual ClickHouse inspection and demo operations.
- dbt-clickhouse for staging and marts transformations.
- Python pipelines for synthetic data generation, S3 upload, raw ClickHouse loading, profiling, DQ checks, and alert generation.
- Agent playbook and guarded ClickHouse SQL tool for evidence-driven triage.
- Local Python runner image with Git, dbt, Streamlit, and project requirements pre-installed.
- Streamlit for the demo UI skeleton.

## Prerequisites

- Windows 11 with Docker Desktop running.
- Copy `.env.example` to `infra/.env`.
- Do not commit real secrets or API keys.

## First Local Checks

Run these before starting the full stack:

```bash
make compose-check
python -m pipelines.seeding.run_daily --dt 2026-05-03 --no-upload
python -m pipelines.loading.load_clickhouse --help
python -m pipelines.dq.config
```

## Start Services

```bash
make up
make ps
make urls
```

Useful URLs:

- Airflow UI: `http://localhost:8080`
- Streamlit UI: `http://localhost:8501`
- ClickHouse HTTP: `http://localhost:8123`
- CH-UI: `http://localhost:3488`
- SeaweedFS S3: `http://localhost:8333`
- SeaweedFS Filer: `http://localhost:8888`

## First Pipeline Path

Generate and upload one orders partition:

```bash
make seed DT=2026-05-03
```

Load that partition into ClickHouse raw storage:

```bash
make load DT=2026-05-03
```

Run dbt transformations and tests:

```bash
make dbt-debug
make dbt-run
make dbt-test
```

Run deterministic profiling, DQ checks, and alert generation:

```bash
make profile DT=2026-05-03
make dq-checks DT=2026-05-03
make alerts DT=2026-05-03
```

Optional: simulate a missing latest-day alert path:

```bash
make dq-checks DT=2026-05-04
make alerts DT=2026-05-04
```

Run a guarded agent SQL evidence query:

```bash
make agent-sql SQL="SELECT alert_key, severity FROM dq.alerts WHERE dt = toDate('2026-05-04') LIMIT 5"
```

Run agent evidence tools:

```bash
make agent-alerts DT=2026-05-04
make agent-dq-history ALERT_KEY="orders|dq_failure|2026-05-04|dq.raw_orders|row_count_positive|table"
make agent-pipeline-runs ALERT_KEY="orders|dq_failure|2026-05-04|dq.raw_orders|row_count_positive|table"
make agent-dbt-lineage TABLE=dq.stg_orders
```

Run one full LangGraph triage workflow and store Markdown/JSON reports to `dq-artifacts`:

```bash
make triage ALERT_KEY="orders|dq_failure|2026-05-04|dq.raw_orders|row_count_positive|table"
```

Backfill a date range:

```bash
make backfill START=2026-05-01 END=2026-05-03
make load-backfill START=2026-05-01 END=2026-05-03
make profile-backfill START=2026-05-01 END=2026-05-03
make dq-checks-backfill START=2026-05-01 END=2026-05-03
make alerts-backfill START=2026-05-01 END=2026-05-03
```

## Notes

- Generated local Parquet files are written under `data/landing/...` and ignored by Git.
- Raw ClickHouse loads use partition replacement by `dt` for idempotent reruns.
- dbt currently rebuilds `stg_orders` and `fct_orders_daily` as ClickHouse tables.
- DQ checks write to `dq.dq_check_results`.
- Profiling writes to `dq.data_profile_results`.
- Alert generation writes stable-key open alerts to `dq.alerts`.
- Agent SQL tool is read-only, date-filter guarded for large tables, hard-limited, and audited in `dq.agent_audit_log`.
- Agent evidence tools can load alerts, inspect DQ history, inspect pipeline runs, parse dbt lineage, and store final reports.
- LangGraph triage writes Markdown and JSON reports under `s3://dq-artifacts/agent-reports/...`.
- MCP layer is planned after the core agent tools and UI path are stable.
