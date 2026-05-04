-- ##############################################
-- SQL Initialization Script for PostgreSQL
-- Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
-- ##############################################

\set ON_ERROR_STOP on

SELECT 'CREATE DATABASE airflow'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'airflow') \gexec