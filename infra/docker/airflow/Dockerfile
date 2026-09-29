####
## Airflow Control Plane Image for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Defining Airflow Base Image
ARG AIRFLOW_VERSION=3.1.7
FROM apache/airflow:${AIRFLOW_VERSION}


# --- Installing Control Plane Dependencies
USER airflow

RUN python -m pip install --no-cache-dir \
    clickhouse-connect==0.11.0 \
    "psycopg[binary]>=3.2.1,<4.0.0"
