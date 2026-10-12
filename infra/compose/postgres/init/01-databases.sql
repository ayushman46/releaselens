-- Runs once, on first initialisation of the Postgres volume (docker-entrypoint-initdb.d).
-- The application database (rlens) is created by POSTGRES_DB. MLflow and Airflow metadata get
-- their own databases on the same server (dev only; ADR-0015).
CREATE DATABASE mlflow;
CREATE DATABASE airflow;
