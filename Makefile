####
## Makefile for Agentic Data Quality Triage
## Author: Mario Caesar // caesarmario87@gmail.com
####
SHELL := /bin/sh

MAKEFLAGS += --no-print-directory
.SILENT:

COMPOSE_FILE := infra/docker-compose.yml
ENV_FILE     := infra/.env
DC           := docker compose --env-file $(ENV_FILE) -f $(COMPOSE_FILE)

# -----------------------------
# Services (keep in sync with docker-compose.yml)
# -----------------------------
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

# -----------------------------
# Optional params
# -----------------------------
DT ?= $(shell date +%F 2>/dev/null || echo 2026-02-26)
START ?= $(DT)
END ?= $(DT)
ALERT_ID ?= 1

# -----------------------------
# Help
# -----------------------------
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
	echo "Pipelines (placeholders):"
	echo "  make seed DT=YYYY-MM-DD     Run daily seeding pipeline"
	echo "  make backfill START=... END=...  Backfill date range"
	echo "  make dbt-debug              dbt debug"
	echo "  make dbt-run                dbt run"
	echo "  make dbt-test               dbt test"
	echo "  make triage ALERT_ID=1      Run triage once (placeholder)"
	echo ""
	echo "  make urls                   Print local URLs"
	echo ""

# -----------------------------
# Core compose commands
# -----------------------------
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

# -----------------------------
# Logs
# -----------------------------
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

# -----------------------------
# One-shot init jobs
# -----------------------------
.PHONY: run-airflow-init
run-airflow-init:
	$(DC) up -d --force-recreate $(AIRFLOW_INIT)

.PHONY: run-s3-init
run-s3-init:
	$(DC) up -d --force-recreate $(S3_INIT_SERVICE)

# -----------------------------
# Force recreate helpers
# -----------------------------
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

# -----------------------------
# Bootstrap helpers
# -----------------------------
.PHONY: ch-bootstrap
ch-bootstrap:
	echo "Applying ClickHouse bootstrap SQL..."
	docker exec -i dq_clickhouse clickhouse-client --multiquery < infra/init/clickhouse_bootstrap.sql

.PHONY: ch-client
ch-client:
	docker exec -it dq_clickhouse clickhouse-client

# -----------------------------
# Python utilities (in dq-runner)
# -----------------------------
.PHONY: pip-freeze
pip-freeze:
	$(DC) exec -T $(RUNNER_SERVICE) python -m pip freeze

.PHONY: test
test:
	$(DC) exec -T $(RUNNER_SERVICE) pytest -q

# -----------------------------
# Pipelines (placeholders; update paths as you implement scripts)
# -----------------------------
.PHONY: seed
seed:
	echo "Seeding for dt=$(DT) ..."
	$(DC) exec -T $(RUNNER_SERVICE) python pipelines/seeding/run_daily.py --dt $(DT)

.PHONY: backfill
backfill:
	echo "Backfill from $(START) to $(END) ..."
	$(DC) exec -T $(RUNNER_SERVICE) python pipelines/seeding/run_daily.py --start $(START) --end $(END)

# -----------------------------
# dbt (expects dbt project under warehouse/dbt)
# -----------------------------
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

# -----------------------------
# Agent (placeholder)
# -----------------------------
.PHONY: triage
triage:
	echo "Running triage for alert_id=$(ALERT_ID) ..."
	$(DC) exec -T $(RUNNER_SERVICE) python scripts/run_triage_once.py --alert-id $(ALERT_ID)

# -----------------------------
# Convenience
# -----------------------------
.PHONY: urls
urls:
	echo "Airflow UI:      http://localhost:8080"
	echo "Streamlit UI:    http://localhost:8501"
	echo "ClickHouse HTTP: http://localhost:8123"
	echo "CH-UI:           http://localhost:3488"
	echo "Seaweed S3:      http://localhost:8333"
	echo "Seaweed Master:  http://localhost:9333"
	echo "Seaweed Filer:   http://localhost:8888"