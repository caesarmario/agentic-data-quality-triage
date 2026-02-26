-- ##############################################
-- SQL Initialization Script for PostgreSQL
-- Author: Mario Caesar // caesarmario87@gmail.com
-- ##############################################

\set ON_ERROR_STOP on

SELECT 'CREATE DATABASE airflow'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'airflow') \gexec