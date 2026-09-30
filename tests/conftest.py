"""Fixtures compartides: base de dades temporal amb esquema + migracions."""

import os
from pathlib import Path

import psycopg
import pytest

from oklliga.config import load_env

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "sql" / "01_schema.sql"
MIGRATIONS = sorted((ROOT / "sql" / "migrations").glob("*.sql"))

_env = load_env()
ADMIN_DSN = os.environ.get("OKLLIGA_ADMIN_DSN") or _env.get(
    "OKLLIGA_ADMIN_DSN", "postgresql://postgres:test@localhost/postgres"
)
TEST_DB = "oklliga_connector_test"


def _swap_db(conninfo: str, dbname: str) -> str:
    """Substitueix el nom de la base de dades d'un DSN, mantenint host/usuari."""
    from psycopg.conninfo import make_conninfo

    params = psycopg.conninfo.conninfo_to_dict(conninfo)
    params["dbname"] = dbname
    return make_conninfo(**params)


@pytest.fixture(scope="session")
def dsn():
    admin = psycopg.connect(ADMIN_DSN, autocommit=True)
    try:
        admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB}")
        admin.execute(f"CREATE DATABASE {TEST_DB}")
    finally:
        admin.close()

    test_conn = psycopg.connect(_swap_db(ADMIN_DSN, TEST_DB), autocommit=True)
    try:
        test_conn.execute(SCHEMA.read_text(encoding="utf-8"))
        for mig in MIGRATIONS:
            test_conn.execute(mig.read_text(encoding="utf-8"))
    finally:
        test_conn.close()

    yield _swap_db(ADMIN_DSN, TEST_DB)

    admin = psycopg.connect(ADMIN_DSN, autocommit=True)
    try:
        admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB}")
    finally:
        admin.close()
