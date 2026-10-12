"""Create Airflow's metadata database if it does not exist yet (idempotent).

`infra/compose/postgres/init/01-databases.sql` creates the `airflow` database, but Postgres only
runs init scripts on a *fresh* volume. This script makes `airflow-init` safe on any existing
volume: it connects to the server's maintenance database and issues `CREATE DATABASE` only when
the target is missing. It runs in the Airflow venv (psycopg2 comes from Airflow's constraints).

Usage: python ensure_metadata_db.py            # reads AIRFLOW__DATABASE__SQL_ALCHEMY_CONN
"""

from __future__ import annotations

import os
import sys
import time

import psycopg2
from psycopg2 import sql
from sqlalchemy.engine import make_url

MAINTENANCE_DB = "postgres"
CONNECT_ATTEMPTS = 30


def _connect(url_str: str) -> psycopg2.extensions.connection:
    url = make_url(url_str)
    last_error: Exception | None = None
    for _ in range(CONNECT_ATTEMPTS):
        try:
            return psycopg2.connect(
                host=url.host,
                port=url.port or 5432,
                user=url.username,
                password=url.password,
                dbname=MAINTENANCE_DB,
                connect_timeout=5,
            )
        except psycopg2.OperationalError as exc:  # server still starting
            last_error = exc
            time.sleep(1)
    raise SystemExit(f"cannot reach postgres at {url.host}:{url.port}: {last_error}")


def ensure_database(url_str: str) -> bool:
    """Return True if the database was created, False if it already existed."""
    database = make_url(url_str).database
    if not database:
        raise SystemExit("sql_alchemy_conn has no database name")
    conn = _connect(url_str)
    try:
        conn.autocommit = True  # CREATE DATABASE cannot run inside a transaction
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database,))
            if cur.fetchone():
                print(f"metadata database {database!r} already exists")
                return False
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
            print(f"created metadata database {database!r}")
            return True
    finally:
        conn.close()


def main() -> int:
    url_str = os.environ.get("AIRFLOW__DATABASE__SQL_ALCHEMY_CONN", "")
    if not url_str.startswith("postgresql"):
        print("AIRFLOW__DATABASE__SQL_ALCHEMY_CONN is not a postgres URL; nothing to do")
        return 0
    ensure_database(url_str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
