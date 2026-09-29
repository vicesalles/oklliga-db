"""Client d'accés a la base de dades OK Lliga (psycopg 3).

Patró repository: mètodes d'alt nivell per a clubs, competicions,
temporades, partits i esdeveniments. Totes les escriptures són
idempotents (upsert) perquè l'scraper es pugui reexecutar sense
duplicats.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from typing import Any, Iterator, Optional

import psycopg
from psycopg.rows import dict_row


class OkLligaDB:
    """Connexió i operacions contra la base de dades de l'OK Lliga.

    Ús bàsic::

        db = OkLligaDB("postgresql://user:pass@localhost/oklliga")
        with db.transaction() as tx:
            ...
    """

    def __init__(self, conninfo: str):
        self._conninfo = conninfo
        self._conn: Optional[psycopg.Connection] = None

    # ------------------------------------------------------------------ connexió

    def connect(self) -> None:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(
                self._conninfo, row_factory=dict_row, autocommit=True
            )

    def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            self._conn.close()

    def __enter__(self) -> "OkLligaDB":
        self.connect()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @property
    def conn(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            raise RuntimeError("No connectat; crida connect() o usa el context manager")
        return self._conn

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Connection]:
        """Transacció explícita: feina d'una temporada/jornada atòmica."""
        with self.conn.transaction():
            yield self.conn

    # ------------------------------------------------------------ clubs i noms

    def upsert_club(
        self,
        canonical_name: str,
        cur: Optional[psycopg.Cursor] = None,
        **fields: Any,
    ) -> int:
        """Crea un club si no existeix (per nom canònic) i retorna el seu id."""
        c = cur or self.conn.cursor()
        c.execute(
            """
            INSERT INTO club (canonical_name, city, province, founded_on, notes)
            VALUES (%(canonical_name)s, %(city)s, %(province)s, %(founded_on)s, %(notes)s)
            ON CONFLICT (canonical_name) DO NOTHING
            RETURNING club_id
            """,
            {
                "canonical_name": canonical_name,
                "city": fields.get("city"),
                "province": fields.get("province"),
                "founded_on": fields.get("founded_on"),
                "notes": fields.get("notes"),
            },
        )
        row = c.fetchone()
        if row is None:
            c.execute(
                "SELECT club_id FROM club WHERE canonical_name = %s",
                (canonical_name,),
            )
            row = c.fetchone()
        return row["club_id"]

    def add_club_name(
        self,
        club_id: int,
        name: str,
        valid_from: Optional[date] = None,
        valid_until: Optional[date] = None,
        is_sponsor_name: bool = False,
        cur: Optional[psycopg.Cursor] = None,
    ) -> int:
        """Registra un nom històric d'un club (idempotent per nom+club)."""
        c = cur or self.conn.cursor()
        c.execute(
            """
            INSERT INTO club_name (club_id, name, valid_from, valid_until, is_sponsor_name)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING club_name_id
            """,
            (club_id, name, valid_from, valid_until, is_sponsor_name),
        )
        row = c.fetchone()
        if row is None:
            # L'insert ha xocat amb una EXCLUDE (solapament de vigència o
            # nom duplicat entre clubs). Reintenta sense rang temporal per
            # mantenir idempotència amb noms ja registrats sense dates.
            c.execute(
                """
                SELECT club_name_id FROM club_name
                WHERE club_id = %s AND name = %s
                """,
                (club_id, name),
            )
            existing = c.fetchone()
            if existing is not None:
                return existing["club_name_id"]
            raise psycopg.errors.ExclusionViolation(
                f"No es pot registrar el nom '{name}' per al club {club_id}: "
                "solapa la vigència d'un altre nom del mateix club o el nom "
                "pertany a un altre club en el mateix període"
            )
        return row["club_name_id"]

    def resolve_club(self, name: str, on_date: Optional[date] = None) -> Optional[int]:
        """Resol un nom d'equip (històric o canònic) a club_id.

        Cerca per nom exacte a club_name i club.canonical_name.
        `on_date` opcionalment restringeix als noms vigents en aquella data.
        Retorna None si no es pot resoldre (l'scraper demanarà revisió manual).
        """
        c = self.conn.cursor()
        if on_date is None:
            c.execute(
                """
                SELECT club_id FROM club_name WHERE name = %s
                UNION
                SELECT club_id FROM club WHERE canonical_name = %s
                LIMIT 1
                """,
                (name, name),
            )
        else:
            c.execute(
                """
                SELECT club_id FROM club_name
                WHERE name = %s
                  AND (valid_from IS NULL OR valid_from <= %s)
                  AND (valid_until IS NULL OR valid_until >= %s)
                UNION
                SELECT club_id FROM club WHERE canonical_name = %s
                LIMIT 1
                """,
                (name, on_date, on_date, name),
            )
        row = c.fetchone()
        return row["club_id"] if row else None

    def club_names(self, club_id: int) -> list[dict[str, Any]]:
        """Historial complet de noms d'un club."""
        c = self.conn.cursor()
        c.execute(
            """
            SELECT name, valid_from, valid_until, is_sponsor_name
            FROM club_name WHERE club_id = %s
            ORDER BY valid_from NULLS FIRST
            """,
            (club_id,),
        )
        return list(c.fetchall())

    # ------------------------------------------------- competicions i temporades

    def upsert_competition(self, tier: int, notes: Optional[str] = None) -> int:
        """Competició per categoria (1 = OK Lliga, 2 = OK Lliga Plata...)."""
        c = self.conn.cursor()
        c.execute(
            """
            INSERT INTO competition (tier, notes)
            VALUES (%s, %s)
            ON CONFLICT DO NOTHING
            RETURNING competition_id
            """,
            (tier, notes),
        )
        row = c.fetchone()
        if row is None:
            c.execute("SELECT competition_id FROM competition WHERE tier = %s", (tier,))
            row = c.fetchone()
        return row["competition_id"]

    def add_competition_name(
        self,
        competition_id: int,
        name: str,
        valid_from_season: int,
        valid_until_season: Optional[int] = None,
    ) -> int:
        """Nom històric d'una competició per rang de temporades."""
        c = self.conn.cursor()
        c.execute(
            """
            INSERT INTO competition_name
                (competition_id, name, valid_from_season, valid_until_season)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING competition_name_id
            """,
            (competition_id, name, valid_from_season, valid_until_season),
        )
        row = c.fetchone()
        if row is None:
            c.execute(
                """
                SELECT competition_name_id FROM competition_name
                WHERE competition_id = %s AND name = %s
                """,
                (competition_id, name),
            )
            row = c.fetchone()
        return row["competition_name_id"]

    def upsert_season(self, start_year: int) -> int:
        """Temporada identificada per l'any d'inici (2026 = 2026/27)."""
        label = f"{start_year}/{str(start_year + 1)[-2:]}"
        c = self.conn.cursor()
        c.execute(
            """
            INSERT INTO season (start_year, label)
            VALUES (%s, %s)
            ON CONFLICT (start_year) DO UPDATE SET label = EXCLUDED.label
            RETURNING season_id
            """,
            (start_year, label),
        )
        return c.fetchone()["season_id"]

    def season_competition_id(
        self,
        competition_id: int,
        season_id: int,
        name_used: str,
        fmt: Optional[str] = None,
    ) -> int:
        """Instància d'una competició en una temporada (idempotent)."""
        c = self.conn.cursor()
        c.execute(
            """
            INSERT INTO season_competition (competition_id, season_id, name_used, format)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (competition_id, season_id) DO UPDATE
                SET name_used = EXCLUDED.name_used, format = EXCLUDED.format
            RETURNING season_competition_id
            """,
            (competition_id, season_id, name_used, fmt),
        )
        return c.fetchone()["season_competition_id"]

    # ------------------------------------------------------------------ partits

    def upsert_match(
        self,
        season_competition_id: int,
        home_club_id: int,
        away_club_id: int,
        **fields: Any,
    ) -> int:
        """Inserta o actualitza un partit pel criteri d'unicitat natural
        (competició+temporada, clubs, i jornada o data).

        Camps opcionals admesos: round, stage, matchday_date, status,
        home_goals, away_goals, home_goals_first_half, away_goals_first_half,
        home_goals_second_half, away_goals_second_half, venue, attendance,
        source_id, source_url, source_ref, confidence, notes.
        """
        allowed = {
            "round", "stage", "matchday_date", "status", "home_goals", "away_goals",
            "home_goals_first_half", "away_goals_first_half",
            "home_goals_second_half", "away_goals_second_half",
            "venue", "attendance", "source_id", "source_url", "source_ref",
            "confidence", "notes",
        }
        extra = {k: v for k, v in fields.items() if k in allowed}
        c = self.conn.cursor()
        with self.conn.transaction():
            return self._upsert_match_stmt(c, season_competition_id, home_club_id, away_club_id, extra)

    def _upsert_match_stmt(self, c, season_competition_id, home_club_id, away_club_id, extra):
        c.execute(
            """
            INSERT INTO match (
                season_competition_id, home_club_id, away_club_id,
                round, stage, matchday_date, status, home_goals, away_goals,
                home_goals_first_half, away_goals_first_half,
                home_goals_second_half, away_goals_second_half,
                venue, attendance, source_id, source_url, source_ref,
                confidence, notes
            )
            VALUES (
                %(sc)s, %(home)s, %(away)s,
                %(round)s, %(stage)s, %(matchday_date)s, %(status)s,
                %(home_goals)s, %(away_goals)s,
                %(home_goals_first_half)s, %(away_goals_first_half)s,
                %(home_goals_second_half)s, %(away_goals_second_half)s,
                %(venue)s, %(attendance)s, %(source_id)s, %(source_url)s,
                %(source_ref)s, %(confidence)s, %(notes)s
            )
            ON CONFLICT DO NOTHING
            RETURNING match_id
            """,
            {
                "sc": season_competition_id,
                "home": home_club_id,
                "away": away_club_id,
                **{k: extra.get(k) for k in (
                    "round", "stage", "matchday_date", "home_goals", "away_goals",
                    "home_goals_first_half", "away_goals_first_half",
                    "home_goals_second_half", "away_goals_second_half",
                    "venue", "attendance", "source_id", "source_url",
                    "source_ref", "notes",
                )},
                "status": extra.get("status", "played"),
                "confidence": extra.get("confidence", "high"),
            },
        )
        row = c.fetchone()
        if row is not None:
            return row["match_id"]
        # Ja existia: resol pel criteri natural (jornada o data) i actualitza
        if extra.get("round") is not None:
            c.execute(
                """
                SELECT match_id FROM match
                WHERE season_competition_id = %(sc)s
                  AND home_club_id = %(home)s AND away_club_id = %(away)s
                  AND round = %(round)s
                """,
                {"sc": season_competition_id, "home": home_club_id,
                 "away": away_club_id, "round": extra.get("round")},
            )
        elif extra.get("matchday_date") is not None:
            c.execute(
                """
                SELECT match_id FROM match
                WHERE season_competition_id = %(sc)s
                  AND home_club_id = %(home)s AND away_club_id = %(away)s
                  AND matchday_date = %(matchday_date)s
                """,
                {"sc": season_competition_id, "home": home_club_id,
                 "away": away_club_id, "matchday_date": extra.get("matchday_date")},
            )
        else:
            raise RuntimeError(
                "upsert_match sense jornada ni data: no es pot resoldre un "
                "partit existent per competició+clubs"
            )
        existing = c.fetchone()
        if existing is None:
            raise RuntimeError(
                "Conflicte d'upsert de partit sense criteri de resolució: "
                f"sc={season_competition_id} home={home_club_id} away={away_club_id}"
            )
        if extra:
            cols = ", ".join(f"{k} = %({k})s" for k in extra)
            c.execute(
                f"UPDATE match SET {cols} WHERE match_id = %(match_id)s",
                {**extra, "match_id": existing["match_id"]},
            )
            return existing["match_id"]

    # ------------------------------------------------------------------ esdeveniments

    def add_match_event(
        self,
        match_id: int,
        club_id: int,
        event_type: str,
        **fields: Any,
    ) -> int:
        """Afegeix un esdeveniment de partit (gol, targeta...)."""
        c = self.conn.cursor()
        c.execute(
            """
            INSERT INTO match_event (
                match_id, club_id, player_id, event_type, minute, half,
                value_num, value_json, source_id, source_url, confidence, notes
            )
            VALUES (
                %(match_id)s, %(club_id)s, %(player_id)s, %(event_type)s,
                %(minute)s, %(half)s,
                %(value_num)s, %(value_json)s, %(source_id)s, %(source_url)s,
                %(confidence)s, %(notes)s
            )
            RETURNING match_event_id
            """,
            {
                "match_id": match_id,
                "club_id": club_id,
                "player_id": fields.get("player_id"),
                "event_type": event_type,
                "minute": fields.get("minute"),
                "half": fields.get("half"),
                "value_num": fields.get("value_num"),
                "value_json": fields.get("value_json"),
                "source_id": fields.get("source_id"),
                "source_url": fields.get("source_url"),
                "confidence": fields.get("confidence", "high"),
                "notes": fields.get("notes"),
            },
        )
        return c.fetchone()["match_event_id"]

    # ------------------------------------------------------------------ consultes

    def matches_by_season(self, start_year: int) -> list[dict[str, Any]]:
        """Partits d'una temporada amb noms històrics resolts (vista)."""
        c = self.conn.cursor()
        c.execute(
            "SELECT * FROM v_match_with_names WHERE season = %s ORDER BY matchday_date NULLS LAST",
            (f"{start_year}/{str(start_year + 1)[-2:]}",),
        )
        return list(c.fetchall())

    def club_history(self, club_id: int) -> list[dict[str, Any]]:
        """Trajectòria completa d'un club per temporada (classificacions)."""
        c = self.conn.cursor()
        c.execute(
            """
            SELECT s.label AS season, sc.name_used AS competition,
                   p.final_position, p.points
            FROM participation p
            JOIN season_competition sc ON sc.season_competition_id = p.season_competition_id
            JOIN season s ON s.season_id = sc.season_id
            WHERE p.club_id = %s
            ORDER BY s.start_year, sc.name_used
            """,
            (club_id,),
        )
        return list(c.fetchall())
