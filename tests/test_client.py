"""Tests d'integració del connector contra PostgreSQL real.

Requereix: psycopg >= 3.1
Ús:
    OKLLIGA_TEST_DSN="postgresql://..." python -m pytest tests/ -v

Si no hi ha DSN, es crea una base de dades de test temporal.
"""

import os
import tempfile
from pathlib import Path

import psycopg
import pytest

from oklliga import OkLligaDB

SCHEMA = Path(__file__).resolve().parent.parent / "sql" / "01_schema.sql"

ADMIN_DSN = os.environ.get(
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
    finally:
        test_conn.close()

    yield _swap_db(ADMIN_DSN, TEST_DB)

    admin = psycopg.connect(ADMIN_DSN, autocommit=True)
    try:
        admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB}")
    finally:
        admin.close()


def test_upsert_club_idempotent(dsn):
    with OkLligaDB(dsn) as db:
        c1 = db.upsert_club("Igualada Rigat HC", city="Igualada")
        c2 = db.upsert_club("Igualada Rigat HC", city="Igualada")
        assert c1 == c2


def test_club_name_resolution(dsn):
    with OkLligaDB(dsn) as db:
        club_id = db.upsert_club("Igualada Rigat HC", city="Igualada")
        db.add_club_name(club_id, "Hormipresa Igualada HC",
                         valid_from="2000-07-01", valid_until="2003-06-30",
                         is_sponsor_name=True)
        db.add_club_name(club_id, "Igualada HC")

        assert db.resolve_club("Hormipresa Igualada HC") == club_id
        assert db.resolve_club("Igualada Rigat HC") == club_id
        assert db.resolve_club("Equip Inexistent") is None

        # Amb data: fora de vigència no ha de resoldre
        assert db.resolve_club("Hormipresa Igualada HC", on_date="2020-01-01") is None
        assert db.resolve_club("Hormipresa Igualada HC", on_date="2002-11-09") == club_id


def test_competition_season_flow(dsn):
    with OkLligaDB(dsn) as db:
        ok = db.upsert_competition(1, notes="Màxima categoria")
        plata = db.upsert_competition(2, notes="Segona categoria")
        assert ok != plata

        db.add_competition_name(ok, "Divisió d'Honor", 1965, 2001)
        db.add_competition_name(ok, "OK Lliga", 2002)

        season = db.upsert_season(2002)
        sc = db.season_competition_id(ok, season, "OK Lliga")
        assert db.season_competition_id(ok, season, "OK Lliga") == sc


def test_match_upsert_and_events(dsn):
    with OkLligaDB(dsn) as db:
        igualada = db.upsert_club("Igualada Rigat HC")
        reus = db.upsert_club("Reus Deportiu", city="Reus")

        ok = db.upsert_competition(1)
        season = db.upsert_season(2002)
        sc = db.season_competition_id(ok, season, "OK Lliga")

        m1 = db.upsert_match(
            sc, igualada, reus,
            round=5, matchday_date="2002-11-09", status="played",
            home_goals=3, away_goals=1,
            home_goals_first_half=2, away_goals_first_half=0,
        )
        m2 = db.upsert_match(
            sc, igualada, reus,
            round=5, matchday_date="2002-11-09", status="played",
            home_goals=3, away_goals=1,
        )
        assert m1 == m2

        ev = db.add_match_event(m1, igualada, "goal", minute=12, half=1)
        assert ev > 0
        db.add_match_event(m1, reus, "blue_card", minute=41, half=1, value_num=2)

        matches = db.matches_by_season(2002)
        assert len(matches) == 1
        assert matches[0]["home_name_used"] == "Igualada Rigat HC"
        assert matches[0]["home_goals"] == 3


def test_transaction_atomic(dsn):
    with OkLligaDB(dsn) as db:
        with pytest.raises(Exception):
            with db.transaction() as tx:
                db.upsert_club("Club Rollback Test", cur=tx.cursor())
                raise RuntimeError("provocat")
        assert db.resolve_club("Club Rollback Test") is None
