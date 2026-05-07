####
## Makefile for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####
# --- Setting Make Runtime
SHELL := /bin/sh

MAKEFLAGS += --no-print-directory
.SILENT:

COMPOSE_FILE := infra/docker-compose.yml
ENV_FILE     := infra/.env
DC           := docker compose --env-file $(ENV_FILE) -f $(COMPOSE_FILE)

# --- Services (keep in sync with docker-compose.yml)
CH_SERVICE        := clickhouse
CH_UI_SERVICE     := ch-ui

SEAWEED_SERVICES  := seaweed-master seaweed-volume seaweed-filer seaweed-s3
S3_INIT_SERVICE   := s3-init

AIRFLOW_DB        := airflow-postgres
AIRFLOW_REDIS     := airflow-redis
AIRFLOW_INIT      := airflow-init
AIRFLOW_WEB       := airflow-api-server
AIRFLOW_SCHED     := airflow-scheduler
AIRFLOW_TRIG      := airflow-triggerer
AIRFLOW_WORKER    := airflow-worker
AIRFLOW_FLOWER    := flower
AIRFLOW_DAGPROC   := airflow-dag-processor

AIRFLOW_SERVICES  := $(AIRFLOW_DB) $(AIRFLOW_REDIS) $(AIRFLOW_WEB) $(AIRFLOW_SCHED) $(AIRFLOW_TRIG) $(AIRFLOW_WORKER) $(AIRFLOW_FLOWER) $(AIRFLOW_DAGPROC)

STREAMLIT_SERVICE := streamlit
RUNNER_SERVICE    := dq-runner

# Long-running services only (exclude one-shot init jobs)
LONGRUN_SERVICES  := $(CH_SERVICE) $(CH_UI_SERVICE) $(SEAWEED_SERVICES) $(AIRFLOW_SERVICES) $(STREAMLIT_SERVICE) $(RUNNER_SERVICE)

# --- Optional params
DT ?= $(shell date +%F 2>/dev/null || echo 2026-02-26)
START ?= $(DT)
END ?= $(DT)
INCIDENT_SCENARIO ?= baseline
ALERT_ID ?=
ALERT_KEY ?= orders|dq_failure|2026-05-04|dq.raw_orders|row_count_positive|table
TABLE ?= dq.stg_orders
SQL ?= SELECT alert_key, severity FROM dq.alerts WHERE dt = toDate('2026-05-04') LIMIT 5
TRIAGE_LIMIT ?= 5
TRIAGE_STATUS ?= open
TRIAGE_ALERT_ARG := $(if $(strip $(ALERT_ID)),--alert-id "$(ALERT_ID)",--alert-key "$(ALERT_KEY)")
TRIAGE_ALERT_LOG := $(if $(strip $(ALERT_ID)),alert_id=$(ALERT_ID),alert_key=$(ALERT_KEY))

# --- Help
.PHONY: help
help:
	echo ""
	echo "Targets:"
	echo "  make up                     Start all services (detached)"
	echo "  make up-force               Start all services with --force-recreate"
	echo "  make down                   Stop services"
	echo "  make down-v                 Stop services + remove volumes (DANGEROUS)"
	echo "  make restart                Restart all services"
	echo "  make ps                     Show running containers"
	echo "  make pull                   Pull images"
	echo "  make build-runner           Build local Python runner image"
	echo "  make compose-check          Validate Docker Compose config"
	echo ""
	echo "Logs:"
	echo "  make logs                   Tail logs from all services"
	echo "  make logs-airflow           Tail Airflow web logs"
	echo "  make logs-ch                Tail ClickHouse logs"
	echo "  make logs-s3                Tail SeaweedFS S3 logs"
	echo "  make logs-streamlit         Tail Streamlit logs"
	echo ""
	echo "One-shot init jobs:"
	echo "  make run-airflow-init        Run airflow-init once (db migrate + auth file)"
	echo "  make run-s3-init             Run s3-init once (create buckets)"
	echo "  make airflow-dags            List parsed Airflow DAGs"
	echo "  make airflow-import-errors   List Airflow DAG import errors"
	echo ""
	echo "Force recreate (per service/group):"
	echo "  make fr-all                 Force recreate all long-running services"
	echo "  make fr-svc SVC=streamlit   Force recreate one service"
	echo "  make fr-streamlit           Force recreate streamlit"
	echo "  make fr-runner              Force recreate dq-runner"
	echo "  make fr-clickhouse          Force recreate clickhouse + ch-ui"
	echo "  make fr-seaweed             Force recreate seaweed services"
	echo "  make fr-airflow             Force recreate airflow services"
	echo ""
	echo "Bootstrap helpers:"
	echo "  make ch-bootstrap           Apply ClickHouse bootstrap SQL (idempotent)"
	echo "  make ch-client              Open ClickHouse client shell"
	echo ""
	echo "Python utilities (in dq-runner):"
	echo "  make pip-freeze             Show installed python packages inside runner"
	echo "  make test                   Run pytest inside runner"
	echo ""
	echo "Pipelines:"
	echo "  make seed DT=YYYY-MM-DD INCIDENT_SCENARIO=baseline Run daily seeding pipeline"
	echo "  make seed-local DT=YYYY-MM-DD INCIDENT_SCENARIO=baseline Generate local Parquet without S3 upload"
	echo "  make backfill START=... END=... INCIDENT_SCENARIO=baseline Backfill date range"
	echo "  make load DT=YYYY-MM-DD     Load one S3 partition to ClickHouse raw_orders"
	echo "  make load-backfill START=... END=... Load S3 partitions to ClickHouse raw_orders"
	echo "  make dbt-debug              dbt debug"
	echo "  make dbt-run                dbt run"
	echo "  make dbt-test               dbt test"
	echo "  make dbt-flow DT=YYYY-MM-DD Run dbt debug/run/test and upload dbt artifacts"
	echo "  make dbt-artifacts DT=YYYY-MM-DD Upload dbt artifacts to dq-artifacts"
	echo "  make profile DT=YYYY-MM-DD  Write profile metrics to ClickHouse"
	echo "  make dq-checks DT=YYYY-MM-DD Run deterministic DQ checks"
	echo "  make alerts DT=YYYY-MM-DD   Generate alerts from DQ failures/warnings"
	echo "  make dq-flow DT=YYYY-MM-DD  Run profile + DQ checks + alert generation"
	echo "  make agent-alerts DT=YYYY-MM-DD List open alerts for a date"
	echo "  make agent-sql SQL=\"...\"    Run guarded read-only ClickHouse SQL"
	echo "  make agent-dq-history ALERT_KEY=\"...\" Fetch DQ history evidence"
	echo "  make agent-pipeline-runs ALERT_KEY=\"...\" Fetch pipeline run evidence"
	echo "  make agent-dbt-lineage TABLE=dq.stg_orders Fetch dbt lineage evidence"
	echo "  make agent-s3-smoke        Write a small artifact to dq-artifacts"
	echo "  make triage ALERT_KEY=\"...\" Run LangGraph triage and store Markdown/JSON report artifacts"
	echo "  make triage-alerts DT=YYYY-MM-DD Run agent triage for open alerts on a date"
	echo ""
	echo "  make urls                   Print local URLs"
	echo ""

# --- Core compose commands
.PHONY: up
up:
	$(DC) up -d --remove-orphans

.PHONY: up-force
up-force:
	$(DC) up -d --remove-orphans --force-recreate

.PHONY: down
down:
	$(DC) down --remove-orphans

.PHONY: down-v
down-v:
	$(DC) down -v --remove-orphans

.PHONY: restart
restart:
	$(DC) restart

.PHONY: ps
ps:
	$(DC) ps

.PHONY: pull
pull:
	$(DC) pull

.PHONY: build-runner
build-runner:
	COMPOSE_ANSI=never COMPOSE_PROGRESS=plain $(DC) build $(RUNNER_SERVICE)

.PHONY: compose-check
compose-check:
	$(DC) config --quiet

# --- Logs
.PHONY: logs
logs:
	$(DC) logs -f --tail=200

.PHONY: logs-airflow
logs-airflow:
	$(DC) logs -f --tail=200 $(AIRFLOW_WEB)

.PHONY: logs-ch
logs-ch:
	$(DC) logs -f --tail=200 $(CH_SERVICE)

.PHONY: logs-s3
logs-s3:
	$(DC) logs -f --tail=200 seaweed-s3

.PHONY: logs-streamlit
logs-streamlit:
	$(DC) logs -f --tail=200 $(STREAMLIT_SERVICE)

# --- One-shot init jobs
.PHONY: run-airflow-init
run-airflow-init:
	$(DC) up -d --force-recreate $(AIRFLOW_INIT)

.PHONY: run-s3-init
run-s3-init:
	$(DC) up -d --force-recreate $(S3_INIT_SERVICE)

.PHONY: airflow-dags
airflow-dags:
	$(DC) exec -T $(AIRFLOW_WEB) airflow dags list

.PHONY: airflow-import-errors
airflow-import-errors:
	$(DC) exec -T $(AIRFLOW_WEB) airflow dags list-import-errors

# --- Force recreate helpers
.PHONY: fr-all
fr-all:
	$(DC) up -d --remove-orphans --force-recreate $(LONGRUN_SERVICES)

.PHONY: fr-svc
fr-svc:
	@if [ -z "$(SVC)" ]; then echo "Usage: make fr-svc SVC=<service_name>"; exit 1; fi
	$(DC) up -d --force-recreate $(SVC)

.PHONY: fr-streamlit
fr-streamlit:
	$(DC) up -d --force-recreate $(STREAMLIT_SERVICE)

.PHONY: fr-runner
fr-runner:
	$(DC) up -d --force-recreate $(RUNNER_SERVICE)

.PHONY: fr-clickhouse
fr-clickhouse:
	$(DC) up -d --force-recreate $(CH_SERVICE) $(CH_UI_SERVICE)

.PHONY: fr-seaweed
fr-seaweed:
	$(DC) up -d --force-recreate $(SEAWEED_SERVICES)

.PHONY: fr-airflow
fr-airflow:
	$(DC) up -d --force-recreate $(AIRFLOW_SERVICES)

# --- Bootstrap helpers
.PHONY: ch-bootstrap
ch-bootstrap:
	echo "Applying modular ClickHouse bootstrap SQL..."
	for f in infra/init/clickhouse/*.sql; do \
		echo "Applying $$f"; \
		docker exec -i dq_clickhouse clickhouse-client --multiquery < $$f; \
	done

.PHONY: ch-client
ch-client:
	docker exec -it dq_clickhouse clickhouse-client

# --- Python utilities (in dq-runner)
.PHONY: pip-freeze
pip-freeze:
	$(DC) exec -T $(RUNNER_SERVICE) python -m pip freeze

.PHONY: test
test:
	$(DC) exec -T $(RUNNER_SERVICE) pytest -q

# --- Pipelines
.PHONY: seed
seed:
	echo "Seeding for dt=$(DT) incident_scenario=$(INCIDENT_SCENARIO) ..."
	$(DC) exec -T $(RUNNER_SERVICE) python -m pipelines.seeding.run_daily --dt $(DT) --incident-scenario $(INCIDENT_SCENARIO)

.PHONY: seed-local
seed-local:
	echo "Generating local Parquet only for dt=$(DT) incident_scenario=$(INCIDENT_SCENARIO) ..."
	python -m pipelines.seeding.run_daily --dt $(DT) --incident-scenario $(INCIDENT_SCENARIO) --no-upload --skip-pipeline-log

.PHONY: backfill
backfill:
	echo "Backfill from $(START) to $(END) incident_scenario=$(INCIDENT_SCENARIO) ..."
	$(DC) exec -T $(RUNNER_SERVICE) python -m pipelines.seeding.run_daily --start $(START) --end $(END) --incident-scenario $(INCIDENT_SCENARIO)

.PHONY: load
load:
	echo "Loading ClickHouse raw_orders for dt=$(DT) ..."
	$(DC) exec -T $(RUNNER_SERVICE) python -m pipelines.loading.load_clickhouse --dt $(DT)

.PHONY: load-backfill
load-backfill:
	echo "Loading ClickHouse raw_orders from $(START) to $(END) ..."
	$(DC) exec -T $(RUNNER_SERVICE) python -m pipelines.loading.load_clickhouse --start $(START) --end $(END)

# --- dbt (expects dbt project under warehouse/dbt)
DBT_PROJECT_DIR := warehouse/dbt
DBT_PROFILES_DIR := warehouse/dbt

.PHONY: dbt-debug
dbt-debug:
	$(DC) exec -T $(RUNNER_SERVICE) dbt debug --project-dir $(DBT_PROJECT_DIR) --profiles-dir $(DBT_PROFILES_DIR)

.PHONY: dbt-run
dbt-run:
	$(DC) exec -T $(RUNNER_SERVICE) dbt run --project-dir $(DBT_PROJECT_DIR) --profiles-dir $(DBT_PROFILES_DIR)

.PHONY: dbt-test
dbt-test:
	$(DC) exec -T $(RUNNER_SERVICE) dbt test --project-dir $(DBT_PROJECT_DIR) --profiles-dir $(DBT_PROFILES_DIR)

.PHONY: dbt-debug-dt
dbt-debug-dt:
	$(DC) exec -T $(RUNNER_SERVICE) python -m pipelines.dbt.run_dbt --dt $(DT) --step debug

.PHONY: dbt-run-dt
dbt-run-dt:
	$(DC) exec -T $(RUNNER_SERVICE) python -m pipelines.dbt.run_dbt --dt $(DT) --step run

.PHONY: dbt-test-dt
dbt-test-dt:
	$(DC) exec -T $(RUNNER_SERVICE) python -m pipelines.dbt.run_dbt --dt $(DT) --step test

.PHONY: dbt-artifacts
dbt-artifacts:
	$(DC) exec -T $(RUNNER_SERVICE) python -m pipelines.dbt.run_dbt --dt $(DT) --step upload-artifacts

.PHONY: dbt-flow
dbt-flow: dbt-debug-dt dbt-run-dt dbt-test-dt dbt-artifacts

# --- DQ and profiling
.PHONY: profile
profile:
	echo "Profiling orders tables for dt=$(DT) ..."
	$(DC) exec -T $(RUNNER_SERVICE) python -m pipelines.profiling.profile_orders --dt $(DT)

.PHONY: profile-backfill
profile-backfill:
	echo "Profiling orders tables from $(START) to $(END) ..."
	$(DC) exec -T $(RUNNER_SERVICE) python -m pipelines.profiling.profile_orders --start $(START) --end $(END)

.PHONY: dq-checks
dq-checks:
	echo "Running DQ checks for dt=$(DT) ..."
	$(DC) exec -T $(RUNNER_SERVICE) python -m pipelines.dq.run_checks --dt $(DT)

.PHONY: dq-checks-backfill
dq-checks-backfill:
	echo "Running DQ checks from $(START) to $(END) ..."
	$(DC) exec -T $(RUNNER_SERVICE) python -m pipelines.dq.run_checks --start $(START) --end $(END)

.PHONY: alerts
alerts:
	echo "Generating alerts for dt=$(DT) ..."
	$(DC) exec -T $(RUNNER_SERVICE) python -m pipelines.dq.generate_alerts --dt $(DT)

.PHONY: alerts-backfill
alerts-backfill:
	echo "Generating alerts from $(START) to $(END) ..."
	$(DC) exec -T $(RUNNER_SERVICE) python -m pipelines.dq.generate_alerts --start $(START) --end $(END)

.PHONY: dq-flow
dq-flow: profile dq-checks alerts

# --- Agent
.PHONY: agent-alerts
agent-alerts:
	echo "Listing open alerts for dt=$(DT) ..."
	$(DC) exec -T $(RUNNER_SERVICE) python agent/tools/alerts.py --mode list --dt $(DT) --limit 20

.PHONY: agent-sql
agent-sql:
	echo "Running guarded agent SQL ..."
	$(DC) exec -T $(RUNNER_SERVICE) python agent/tools/clickhouse_sql.py --alert-key manual_agent_sql --sql "$(SQL)"

.PHONY: agent-dq-history
agent-dq-history:
	echo "Fetching DQ history evidence ..."
	$(DC) exec -T $(RUNNER_SERVICE) python agent/tools/dq_history.py --alert-key "$(ALERT_KEY)"

.PHONY: agent-pipeline-runs
agent-pipeline-runs:
	echo "Fetching pipeline run evidence ..."
	$(DC) exec -T $(RUNNER_SERVICE) python agent/tools/pipeline_runs.py --alert-key "$(ALERT_KEY)"

.PHONY: agent-dbt-lineage
agent-dbt-lineage:
	echo "Fetching dbt lineage evidence for table=$(TABLE) ..."
	$(DC) exec -T $(RUNNER_SERVICE) python agent/tools/dbt_lineage.py --table-name "$(TABLE)"

.PHONY: agent-s3-smoke
agent-s3-smoke:
	echo "Writing S3 smoke artifact ..."
	$(DC) exec -T $(RUNNER_SERVICE) python agent/tools/s3.py --text "agent s3 smoke from make"

.PHONY: triage
triage:
	echo "Running triage for $(TRIAGE_ALERT_LOG) ..."
	$(DC) exec -T $(RUNNER_SERVICE) python scripts/run_triage_once.py $(TRIAGE_ALERT_ARG)

.PHONY: triage-alerts
triage-alerts:
	echo "Running batch triage for dt=$(DT) status=$(TRIAGE_STATUS) limit=$(TRIAGE_LIMIT) ..."
	$(DC) exec -T $(RUNNER_SERVICE) python scripts/run_triage_alerts.py --dt $(DT) --status $(TRIAGE_STATUS) --limit $(TRIAGE_LIMIT)

# --- Convenience
.PHONY: urls
urls:
	echo "Airflow UI:      http://localhost:8080"
	echo "Streamlit UI:    http://localhost:8501"
	echo "ClickHouse HTTP: http://localhost:8123"
	echo "CH-UI:           http://localhost:3488"
	echo "Seaweed S3:      http://localhost:8333"
	echo "Seaweed Master:  http://localhost:9333"
	echo "Seaweed Filer:   http://localhost:8888"
