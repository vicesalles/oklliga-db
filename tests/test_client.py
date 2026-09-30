"""Tests d'integració del connector contra PostgreSQL real.

Requereix: psycopg >= 3.1
Ús:
    OKLLIGA_ADMIN_DSN="postgresql://..." python -m pytest tests/ -v

Fixtures (dsn, _swap_db) definides a conftest.py.
"""

import pytest

from oklliga import OkLligaDB

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
        igualada = db.upsert_club("Igualada Rigat HC MatchTest")
        reus = db.upsert_club("Reus Deportiu MatchTest", city="Reus")

        ok = db.upsert_competition(1)
        season = db.upsert_season(2002)
        sc = db.season_competition_id(ok, season, "OK Lliga MatchTest")

        t_igualada = db.upsert_team(igualada, "first")
        t_reus = db.upsert_team(reus, "first")
        m1 = db.upsert_match(
            sc, t_igualada, t_reus,
            round=5, matchday_date="2002-11-09", status="played",
            home_goals=3, away_goals=1,
            home_goals_first_half=2, away_goals_first_half=0,
        )
        m2 = db.upsert_match(
            sc, t_igualada, t_reus,
            round=5, matchday_date="2002-11-09", status="played",
            home_goals=3, away_goals=1,
        )
        assert m1 == m2

        ev = db.add_match_event(m1, t_igualada, "goal", minute=12, half=1)
        assert ev > 0
        db.add_match_event(m1, reus, "blue_card", minute=41, half=1, value_num=2)

        matches = [m for m in db.matches_by_season(2002)
                   if m["competition_name"] == "OK Lliga MatchTest"]
        assert len(matches) == 1
        assert matches[0]["home_name_used"] == "Igualada Rigat HC MatchTest"
        assert matches[0]["home_goals"] == 3


def test_transaction_atomic(dsn):
    with OkLligaDB(dsn) as db:
        with pytest.raises(Exception):
            with db.transaction() as tx:
                db.upsert_club("Club Rollback Test", cur=tx.cursor())
                raise RuntimeError("provocat")
        assert db.resolve_club("Club Rollback Test") is None
