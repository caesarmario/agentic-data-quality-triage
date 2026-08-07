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
DISCORD_SERVICE    := discord-bot
API_SERVICE       := api

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
REPORT_S3_URI ?= s3://dq-artifacts/manual/report.md
REPORT_JSON_S3_URI ?=
EVAL_SCENARIO ?= missing_latest_day
LLM_ROUTE ?= cheap_summary
LLM_REQUIRE_PROVIDER ?= false
LLM_SMOKE_RUN_ID ?=
CHECKPOINT_SMOKE_RUN_ID ?=
CHECKPOINT_SMOKE_THREAD_ID ?=
LIFE_SCENARIO ?= missing_latest_day
LIFE_REPORT_S3_URI ?=
LIFE_MIN_CONFIDENCE ?= 0.70
LIFE_ARTIFACT_PREFIX ?= agent-life
LIFE_FAIL_ON_EVAL_FAILURE ?= false
LIFE_AIRFLOW_RUN_ID ?=
LIFE_EVALUATION_RUN_ID ?=
METADATA_REGISTRY ?= orders
METADATA_SYNC_RUN_ID ?=
VALIDATION_SUITE ?= all
VALIDATION_RUN_ID ?=
REQUIRE_API ?= false
VALIDATION_DAG_ID := 91_dag_dq_platform_validation
LLM_SMOKE_DAG_ID := 92_dag_dq_llm_provider_smoke
CHECKPOINT_SMOKE_DAG_ID := 93_dag_dq_agent_checkpoint_smoke
LIFE_EVALUATION_DAG_ID := 94_dag_dq_agent_life_evaluation
METADATA_SYNC_DAG_ID := 95_dag_dq_metadata_registry_sync
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
	echo "  make logs-api               Tail optional FastAPI logs"
	echo "  make logs-discord            Tail Discord bot logs"
	echo ""
	echo "One-shot init jobs:"
	echo "  make run-airflow-init        Run airflow-init once (db migrate + auth file)"
	echo "  make run-s3-init             Run s3-init once (create buckets)"
	echo "  make airflow-dags            List parsed Airflow DAGs"
	echo "  make airflow-import-errors   List Airflow DAG import errors"
	echo "  make airflow-validate VALIDATION_SUITE=all Trigger manual Airflow validation"
	echo "  make airflow-validation-runs List validation DagRuns"
	echo "  make airflow-validation-tasks VALIDATION_RUN_ID=... Show task states"
	echo "  make airflow-validation-logs VALIDATION_RUN_ID=... Show retained task logs"
	echo "  make airflow-llm-smoke LLM_ROUTE=cheap_summary Trigger fallback-safe LLM provider smoke"
	echo "  make airflow-llm-runs       List LLM provider smoke DagRuns"
	echo "  make airflow-llm-tasks LLM_SMOKE_RUN_ID=... Show provider smoke task states"
	echo "  make airflow-llm-logs LLM_SMOKE_RUN_ID=... Show retained provider smoke logs"
	echo "  make airflow-checkpoint-smoke Trigger cross-process LangGraph checkpoint smoke"
	echo "  make airflow-checkpoint-runs List checkpoint smoke DagRuns"
	echo "  make airflow-checkpoint-tasks CHECKPOINT_SMOKE_RUN_ID=... Show checkpoint task states"
	echo "  make airflow-checkpoint-logs CHECKPOINT_SMOKE_RUN_ID=... Show retained checkpoint logs"
	echo "  make airflow-metadata-sync METADATA_REGISTRY=orders Sync and verify trusted metadata"
	echo "  make airflow-metadata-runs List metadata sync DagRuns"
	echo "  make airflow-metadata-tasks METADATA_SYNC_RUN_ID=... Show metadata task states"
	echo "  make airflow-metadata-logs METADATA_SYNC_RUN_ID=... Show retained metadata logs"
	echo ""
	echo "Force recreate (per service/group):"
	echo "  make fr-all                 Force recreate all long-running services"
	echo "  make fr-svc SVC=streamlit   Force recreate one service"
	echo "  make fr-streamlit           Force recreate streamlit"
	echo "  make fr-runner              Force recreate dq-runner"
	echo "  make fr-discord              Force recreate Discord bot profile service"
	echo "  make fr-clickhouse          Force recreate clickhouse + ch-ui"
	echo "  make fr-seaweed             Force recreate seaweed services"
	echo "  make fr-airflow             Force recreate airflow services"
	echo ""
	echo "Bootstrap helpers:"
	echo "  make ch-bootstrap           Apply ClickHouse bootstrap SQL (idempotent)"
	echo "  make migrate-alerts-lifecycle Migrate dq.alerts sorting key for triage lifecycle updates"
	echo "  make migrate-alert-display-id Add/backfill human-facing alert display ids"
	echo "  make ch-client              Open ClickHouse client shell"
	echo ""
	echo "Python utilities (in dq-runner):"
	echo "  make pip-freeze             Show installed python packages inside runner"
	echo "  make test                   Run pytest inside runner as fast feedback only"
	echo "  make smoke-readiness        Run read-only ClickHouse/S3 readiness checks"
	echo "  make smoke-ready            Run local preflight checks; final acceptance uses airflow-validate"
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
	echo "  make agent-mark-triaged ALERT_KEY=\"...\" REPORT_S3_URI=s3://... Mark alert triaged"
	echo "  make agent-llm-smoke LLM_ROUTE=cheap_summary Alias for Airflow LLM provider smoke"
	echo "  make triage ALERT_KEY=\"...\" Run LangGraph triage and store Markdown/JSON report artifacts"
	echo "  make triage-alerts DT=YYYY-MM-DD Run agent triage for open alerts on a date"
	echo "  make triage-eval EVAL_SCENARIO=... REPORT_JSON_S3_URI=s3://... Evaluate report vs ground truth"
	echo "  make triage-eval-scenarios List incident configs available for triage eval"
	echo "  make life-eval LIFE_REPORT_S3_URI=s3://... Trigger LIFE evaluation through Airflow"
	echo "  make life-eval-scenarios   List incident ground-truth scenarios"
	echo "  make api-smoke              Inspect FastAPI app routes"
	echo "  make api-up                 Start optional FastAPI profile service"
	echo "  make api-down               Stop optional FastAPI service"
	echo "  make mcp-tools              Inspect MCP tool registry"
	echo "  make mcp-server             Start local MCP server over stdio"
	echo "  make discord-smoke           Inspect Discord startup and slash-command diagnostics"
	echo "  make discord-up              Start optional Discord bot profile with FastAPI"
	echo "  make discord-down            Stop optional Discord bot service"
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
	$(DC) --ansi never --progress plain build $(RUNNER_SERVICE)

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

.PHONY: logs-api
logs-api:
	$(DC) --profile api logs -f --tail=200 $(API_SERVICE)

.PHONY: logs-discord
logs-discord:
	$(DC) logs -f --tail=200 $(DISCORD_SERVICE)

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

.PHONY: airflow-validate
airflow-validate:
	$(DC) exec -T $(AIRFLOW_WEB) python /opt/airflow/project/scripts/trigger_airflow_validation.py \
		--suite "$(VALIDATION_SUITE)" \
		$(if $(filter true 1 yes,$(REQUIRE_API)),--require-api,) \
		$(if $(strip $(VALIDATION_RUN_ID)),--run-id "$(VALIDATION_RUN_ID)",)

.PHONY: airflow-validation-runs
airflow-validation-runs:
	$(DC) exec -T $(AIRFLOW_WEB) airflow dags list-runs -o table $(VALIDATION_DAG_ID)

.PHONY: airflow-validation-tasks
airflow-validation-tasks:
	$(if $(strip $(VALIDATION_RUN_ID)),,$(error VALIDATION_RUN_ID is required))
	$(DC) exec -T $(AIRFLOW_WEB) airflow tasks states-for-dag-run \
		-o table $(VALIDATION_DAG_ID) "$(VALIDATION_RUN_ID)"

.PHONY: airflow-validation-logs
airflow-validation-logs:
	$(if $(strip $(VALIDATION_RUN_ID)),,$(error VALIDATION_RUN_ID is required))
	$(DC) exec -T $(AIRFLOW_WORKER) python /opt/airflow/project/scripts/read_airflow_validation_logs.py \
		--run-id "$(VALIDATION_RUN_ID)"

.PHONY: airflow-llm-smoke
airflow-llm-smoke:
	$(DC) exec -T $(AIRFLOW_WEB) python /opt/airflow/project/scripts/trigger_airflow_llm_smoke.py \
		--route "$(LLM_ROUTE)" \
		$(if $(filter true 1 yes,$(LLM_REQUIRE_PROVIDER)),--require-provider,) \
		$(if $(strip $(LLM_SMOKE_RUN_ID)),--run-id "$(LLM_SMOKE_RUN_ID)",)

.PHONY: airflow-llm-runs
airflow-llm-runs:
	$(DC) exec -T $(AIRFLOW_WEB) airflow dags list-runs -o table $(LLM_SMOKE_DAG_ID)

.PHONY: airflow-llm-tasks
airflow-llm-tasks:
	$(if $(strip $(LLM_SMOKE_RUN_ID)),,$(error LLM_SMOKE_RUN_ID is required))
	$(DC) exec -T $(AIRFLOW_WEB) airflow tasks states-for-dag-run \
		-o table $(LLM_SMOKE_DAG_ID) "$(LLM_SMOKE_RUN_ID)"

.PHONY: airflow-llm-logs
airflow-llm-logs:
	$(if $(strip $(LLM_SMOKE_RUN_ID)),,$(error LLM_SMOKE_RUN_ID is required))
	$(DC) exec -T $(AIRFLOW_WORKER) python /opt/airflow/project/scripts/read_airflow_validation_logs.py \
		--dag-id $(LLM_SMOKE_DAG_ID) \
		--run-id "$(LLM_SMOKE_RUN_ID)"

.PHONY: airflow-checkpoint-smoke
airflow-checkpoint-smoke:
	$(DC) exec -T $(AIRFLOW_WEB) python /opt/airflow/project/scripts/trigger_airflow_checkpoint_smoke.py \
		$(if $(strip $(CHECKPOINT_SMOKE_RUN_ID)),--run-id "$(CHECKPOINT_SMOKE_RUN_ID)",) \
		$(if $(strip $(CHECKPOINT_SMOKE_THREAD_ID)),--thread-id "$(CHECKPOINT_SMOKE_THREAD_ID)",)

.PHONY: airflow-checkpoint-runs
airflow-checkpoint-runs:
	$(DC) exec -T $(AIRFLOW_WEB) airflow dags list-runs -o table $(CHECKPOINT_SMOKE_DAG_ID)

.PHONY: airflow-checkpoint-tasks
airflow-checkpoint-tasks:
	$(if $(strip $(CHECKPOINT_SMOKE_RUN_ID)),,$(error CHECKPOINT_SMOKE_RUN_ID is required))
	$(DC) exec -T $(AIRFLOW_WEB) airflow tasks states-for-dag-run \
		-o table $(CHECKPOINT_SMOKE_DAG_ID) "$(CHECKPOINT_SMOKE_RUN_ID)"

.PHONY: airflow-checkpoint-logs
airflow-checkpoint-logs:
	$(if $(strip $(CHECKPOINT_SMOKE_RUN_ID)),,$(error CHECKPOINT_SMOKE_RUN_ID is required))
	$(DC) exec -T $(AIRFLOW_WORKER) python /opt/airflow/project/scripts/read_airflow_validation_logs.py \
		--dag-id $(CHECKPOINT_SMOKE_DAG_ID) \
		--run-id "$(CHECKPOINT_SMOKE_RUN_ID)"

# --- LIFE Agent Reliability Evaluation
.PHONY: airflow-life-eval
airflow-life-eval:
	$(if $(strip $(LIFE_REPORT_S3_URI)),,$(error LIFE_REPORT_S3_URI is required))
	$(DC) exec -T $(AIRFLOW_WEB) python /opt/airflow/project/scripts/trigger_airflow_life_evaluation.py \
		--scenario "$(LIFE_SCENARIO)" \
		--report-s3-uri "$(LIFE_REPORT_S3_URI)" \
		--minimum-confidence "$(LIFE_MIN_CONFIDENCE)" \
		--artifact-prefix "$(LIFE_ARTIFACT_PREFIX)" \
		$(if $(filter true 1 yes,$(LIFE_FAIL_ON_EVAL_FAILURE)),--fail-on-eval-failure,) \
		$(if $(strip $(LIFE_AIRFLOW_RUN_ID)),--run-id "$(LIFE_AIRFLOW_RUN_ID)",) \
		$(if $(strip $(LIFE_EVALUATION_RUN_ID)),--evaluation-run-id "$(LIFE_EVALUATION_RUN_ID)",)

.PHONY: airflow-life-runs
airflow-life-runs:
	$(DC) exec -T $(AIRFLOW_WEB) airflow dags list-runs -o table $(LIFE_EVALUATION_DAG_ID)

.PHONY: airflow-life-tasks
airflow-life-tasks:
	$(if $(strip $(LIFE_AIRFLOW_RUN_ID)),,$(error LIFE_AIRFLOW_RUN_ID is required))
	$(DC) exec -T $(AIRFLOW_WEB) airflow tasks states-for-dag-run \
		-o table $(LIFE_EVALUATION_DAG_ID) "$(LIFE_AIRFLOW_RUN_ID)"

.PHONY: airflow-life-logs
airflow-life-logs:
	$(if $(strip $(LIFE_AIRFLOW_RUN_ID)),,$(error LIFE_AIRFLOW_RUN_ID is required))
	$(DC) exec -T $(AIRFLOW_WORKER) python /opt/airflow/project/scripts/read_airflow_validation_logs.py \
		--dag-id $(LIFE_EVALUATION_DAG_ID) \
		--run-id "$(LIFE_AIRFLOW_RUN_ID)"

.PHONY: life-eval
life-eval: airflow-life-eval

.PHONY: life-eval-scenarios
life-eval-scenarios: triage-eval-scenarios

# --- Trusted Metadata Registry
.PHONY: airflow-metadata-sync
airflow-metadata-sync:
	$(DC) exec -T $(AIRFLOW_WEB) python /opt/airflow/project/scripts/trigger_airflow_metadata_sync.py \
		--registry "$(METADATA_REGISTRY)" \
		$(if $(strip $(METADATA_SYNC_RUN_ID)),--run-id "$(METADATA_SYNC_RUN_ID)",)

.PHONY: airflow-metadata-runs
airflow-metadata-runs:
	$(DC) exec -T $(AIRFLOW_WEB) airflow dags list-runs -o table $(METADATA_SYNC_DAG_ID)

.PHONY: airflow-metadata-tasks
airflow-metadata-tasks:
	$(if $(strip $(METADATA_SYNC_RUN_ID)),,$(error METADATA_SYNC_RUN_ID is required))
	$(DC) exec -T $(AIRFLOW_WEB) airflow tasks states-for-dag-run \
		-o table $(METADATA_SYNC_DAG_ID) "$(METADATA_SYNC_RUN_ID)"

.PHONY: airflow-metadata-logs
airflow-metadata-logs:
	$(if $(strip $(METADATA_SYNC_RUN_ID)),,$(error METADATA_SYNC_RUN_ID is required))
	$(DC) exec -T $(AIRFLOW_WORKER) python /opt/airflow/project/scripts/read_airflow_validation_logs.py \
		--dag-id $(METADATA_SYNC_DAG_ID) \
		--run-id "$(METADATA_SYNC_RUN_ID)"

# --- Force recreate helpers
.PHONY: fr-all
fr-all:
	$(DC) up -d --remove-orphans --force-recreate $(LONGRUN_SERVICES)

.PHONY: fr-svc
fr-svc:
	$(if $(strip $(SVC)),,$(error SVC is required. Usage: make fr-svc SVC=<service_name>))
	$(DC) up -d --force-recreate $(SVC)

.PHONY: fr-streamlit
fr-streamlit:
	$(DC) up -d --force-recreate $(STREAMLIT_SERVICE)

.PHONY: fr-runner
fr-runner:
	$(DC) up -d --force-recreate $(RUNNER_SERVICE)

.PHONY: fr-discord
fr-discord:
	$(DC) --profile discord up -d --force-recreate $(DISCORD_SERVICE)

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

.PHONY: migrate-alerts-lifecycle
migrate-alerts-lifecycle: migrate-alert-display-id
	echo "Migrating dq.alerts lifecycle schema if needed ..."
	$(DC) exec -T $(RUNNER_SERVICE) python -m pipelines.maintenance.migrate_alerts_lifecycle

.PHONY: migrate-alert-display-id
migrate-alert-display-id:
	echo "Adding/backfilling dq.alerts alert_display_id if needed ..."
	$(DC) exec -T $(RUNNER_SERVICE) python -m pipelines.maintenance.migrate_alert_display_id

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

.PHONY: smoke-readiness
smoke-readiness:
	$(DC) exec -T $(RUNNER_SERVICE) python scripts/smoke_readiness.py

.PHONY: smoke-ready
smoke-ready: compose-check airflow-import-errors test triage-eval-scenarios api-smoke mcp-tools smoke-readiness

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

.PHONY: agent-mark-triaged
agent-mark-triaged:
	echo "Marking alert as triaged for $(TRIAGE_ALERT_LOG) ..."
	$(DC) exec -T $(RUNNER_SERVICE) python agent/tools/alert_lifecycle.py $(TRIAGE_ALERT_ARG) --report-s3-uri "$(REPORT_S3_URI)"

.PHONY: agent-llm-smoke
agent-llm-smoke: airflow-llm-smoke

.PHONY: triage
triage:
	echo "Running triage for $(TRIAGE_ALERT_LOG) ..."
	$(DC) exec -T $(RUNNER_SERVICE) python scripts/run_triage_once.py $(TRIAGE_ALERT_ARG)

.PHONY: triage-alerts
triage-alerts:
	echo "Running batch triage for dt=$(DT) status=$(TRIAGE_STATUS) limit=$(TRIAGE_LIMIT) ..."
	$(DC) exec -T $(RUNNER_SERVICE) python scripts/run_triage_alerts.py --dt $(DT) --status $(TRIAGE_STATUS) --limit $(TRIAGE_LIMIT)

.PHONY: triage-eval
triage-eval:
	$(DC) exec -T $(RUNNER_SERVICE) python scripts/evaluate_triage_report.py --scenario "$(EVAL_SCENARIO)" --report-s3-uri "$(REPORT_JSON_S3_URI)"

.PHONY: triage-eval-scenarios
triage-eval-scenarios:
	echo "Listing triage evaluation scenarios ..."
	$(DC) exec -T $(RUNNER_SERVICE) python scripts/evaluate_triage_report.py --list-scenarios

# --- FastAPI Backend
.PHONY: api-smoke
api-smoke:
	echo "Inspecting FastAPI app routes ..."
	$(DC) exec -T $(RUNNER_SERVICE) python -m apps.api.main --smoke

.PHONY: api-up
api-up:
	$(DC) --profile api up -d --no-deps --wait $(API_SERVICE)

.PHONY: api-down
api-down:
	$(DC) --profile api stop $(API_SERVICE)

# --- MCP
.PHONY: mcp-tools
mcp-tools:
	echo "Inspecting MCP tool registry ..."
	$(DC) exec -T $(RUNNER_SERVICE) python -m agent.mcp.server --list-tools

.PHONY: mcp-server
mcp-server:
	echo "Starting MCP server over stdio ..."
	$(DC) exec -T $(RUNNER_SERVICE) python -m agent.mcp.server --transport stdio

# --- Optional Discord Bot
.PHONY: discord-smoke
discord-smoke: api-up
	echo "Inspecting Discord startup, slash commands, and control-plane readiness ..."
	$(DC) exec -T -e CONTROL_PLANE_API_URL=http://api:8000 $(RUNNER_SERVICE) \
		python -m apps.discord_bot.bot --smoke --require-settings --check-api

.PHONY: discord-up
discord-up: api-up
	$(DC) --profile discord up -d --no-deps $(DISCORD_SERVICE)

.PHONY: discord-down
discord-down:
	$(DC) stop $(DISCORD_SERVICE)

# --- Convenience
.PHONY: urls
urls:
	echo "Airflow UI:      http://localhost:8080"
	echo "Streamlit UI:    http://localhost:8501"
	echo "FastAPI BFF:     http://localhost:8000/docs"
	echo "ClickHouse HTTP: http://localhost:8123"
	echo "CH-UI:           http://localhost:3488"
	echo "Seaweed S3:      http://localhost:8333"
	echo "Seaweed Master:  http://localhost:9333"
	echo "Seaweed Filer:   http://localhost:8888"
