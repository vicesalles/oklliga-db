"""Ingesta de dades del portal RFEP/SIDGAD a la BD OK Lliga.

Flux per a una temporada (vegeu informe tècnic):
  1. Catàleg (rfep_ls_1.php) → mapa temp_id → edicions (idc) → teams_array
  2. Calendari de l'edició (rfep_cal_idc_{idc}_1.php) → partits
  3. Per cada partit: resolució de club per ID extern (rfep club_*) amb
     fallback a resolució per nom assistida (cua pending_name_resolution)
  4. Upsert idempotent de partits + snapshots crus + external_ids

Cap resolució automàtica de noms: si un club no es pot identificar,
l'partit s'omet i el nom va a la cua per decisió humana.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any, Optional

from .client import OkLligaDB
from .sidgad import (
    SidgadClient,
    SidgadMatch,
    SidgadTeam,
    body_hash,
    parse_calendar,
    parse_catalog_teams,
)

PARSER_VERSION = "sidgad-v0"


class SidgadIngest:
    """Orquestra la ingesta d'una edició (idc) a la BD."""

    def __init__(self, db: OkLligaDB, client: Optional[SidgadClient] = None):
        self.db = db
        self.client = client or SidgadClient()
        self.source_id: Optional[int] = None

    # ------------------------------------------------------------- source

    def ensure_source(self, name: str = "RFEP - hockeypatines.fep.es") -> int:
        c = self.db.conn.cursor()
        c.execute(
            """
            INSERT INTO source (name, url, notes)
            VALUES (%(name)s, %(url)s, %(notes)s)
            ON CONFLICT (name) DO UPDATE SET url = EXCLUDED.url
            RETURNING source_id
            """,
            {
                "name": name,
                "url": "https://www.hockeypatines.fep.es/",
                "notes": "Portal de competicions RFEP (backend SIDGAD)",
            },
        )
        self.source_id = c.fetchone()["source_id"]
        return self.source_id

    # ------------------------------------------------------------- snapshot

    def store_snapshot(self, endpoint: str, body: str, params: dict) -> int:
        """Guarda el fragment cru; idempotent per (endpoint, hash)."""
        c = self.db.conn.cursor()
        c.execute(
            """
            INSERT INTO raw_snapshot (source_id, endpoint, params, body_hash, body, parser_version)
            VALUES (%(s)s, %(e)s, %(p)s, %(h)s, %(b)s, %(v)s)
            ON CONFLICT (endpoint, body_hash) DO UPDATE
                SET fetched_at = now(), parser_version = EXCLUDED.parser_version
            RETURNING raw_snapshot_id
            """,
            {
                "s": self.source_id,
                "e": endpoint,
                "p": json.dumps(params),
                "h": body_hash(body),
                "b": body,
                "v": PARSER_VERSION,
            },
        )
        return c.fetchone()["raw_snapshot_id"]

    # ---------------------------------------------------------- external ids

    def upsert_external_id(
        self, entity_type: str, internal_id: int, external_id: str
    ) -> None:
        c = self.db.conn.cursor()
        c.execute(
            """
            INSERT INTO external_id (source_id, entity_type, internal_id, external_id)
            VALUES (%(s)s, %(t)s, %(i)s, %(e)s)
            ON CONFLICT (source_id, entity_type, external_id) DO UPDATE
                SET internal_id = EXCLUDED.internal_id, fetched_at = now()
            """,
            {"s": self.source_id, "t": entity_type, "i": internal_id, "e": external_id},
        )

    def resolve_club_by_external_id(self, external_id: str) -> Optional[int]:
        """Club per ID extern RFEP (ex: '3058'). None si no registrat."""
        c = self.db.conn.cursor()
        c.execute(
            """
            SELECT internal_id FROM external_id
            WHERE source_id = %s AND entity_type = 'club' AND external_id = %s
            """,
            (self.source_id, external_id),
        )
        row = c.fetchone()
        return row["internal_id"] if row else None

    # ------------------------------------------------------------- partits

    def register_team_entry(
        self,
        team: SidgadTeam,
        club_id: int,
    ) -> None:
        """Registra team_entry_id com a ID extern de l'edició i el nom visible."""
        self.upsert_external_id("team_entry", club_id, team.team_entry_id)
        c = self.db.conn.cursor()
        # El nom amb patrocinador de l'edició queda a club_name amb confiança
        # alta però sense vigència exacta si ja hi ha una entrada igual.
        c.execute(
            """
            SELECT 1 FROM club_name
            WHERE club_id = %s AND name = %s AND name_normalized = %s
            """,
            (club_id, team.name, team.name),
        )

    def ingest_calendar(
        self,
        idc: int,
        season_competition_id: int,
        teams: list[SidgadTeam],
        club_by_team_entry: dict[str, int],
        season_start_year: int,
    ) -> dict[str, int]:
        """Descarrega i ingereix tot el calendari d'una edició.

        Retorna comptadors: {'matches': n, 'skipped': n, 'queued': n}
        """
        endpoint = f"rfep/rfep_cal_idc_{idc}_1.php"
        body = self.client.fetch_calendar(idc)
        self.store_snapshot(endpoint, body, {"idc": str(idc)})
        matches = parse_calendar(body)

        counts = {"matches": 0, "skipped": 0, "queued": 0}
        resolver_queue: list[tuple[str, dict]] = []

        for m in matches:
            home_club = club_by_team_entry.get(self._team_key(teams, m.home_abbr, m.home_name))
            away_club = club_by_team_entry.get(self._team_key(teams, m.away_abbr, m.away_name))
            if home_club is None:
                resolver_queue.append((m.home_name, self._context(m, "home")))
                counts["queued"] += 1
                continue
            if away_club is None:
                resolver_queue.append((m.away_name, self._context(m, "away")))
                counts["queued"] += 1
                continue
            self._upsert_match(
                m, season_competition_id, home_club, away_club, season_start_year
            )
            counts["matches"] += 1

        self._enqueue_unresolved(resolver_queue)
        return counts

    def _team_key(
        self, teams: list[SidgadTeam], abbr: str, name: str
    ) -> Optional[str]:
        """Clau de team_entry: primer per nom exacte (únic), després per
        sigles només si no hi ha ambigüitat (ex: CPV és ambigu entre
        Voltregà/Vilafranca a la 2024/25)."""
        uname = name.strip().upper()
        for team in teams:
            if team.name.strip().upper() == uname:
                return team.team_entry_id
        matches = [t for t in teams if t.abbr.strip() == abbr.strip()]
        if len(matches) == 1:
            return matches[0].team_entry_id
        return None

    def _context(self, m: SidgadMatch, side: str) -> dict:
        return {
            "idc": m.raw_attrs.get("idc"),
            "jornada": m.round,
            "data": m.gamedate,
            "partit": f"{m.home_name} vs {m.away_name}",
            "costat": side,
        }

    def _enqueue_unresolved(self, queue: list[tuple[str, dict]]) -> None:
        c = self.db.conn.cursor()
        for raw_name, context in queue:
            # Idempotent: el mateix nom pendent de la mateixa font no s'enfila dues vegades
            c.execute(
                """
                SELECT pending_id FROM pending_name_resolution
                WHERE raw_name = %(n)s AND status = 'pending'
                  AND source_id = %(s)s
                """,
                {"n": raw_name, "s": self.source_id},
            )
            if c.fetchone():
                continue
            c.execute(
                """
                INSERT INTO pending_name_resolution
                    (raw_name, context, source_id, status)
                VALUES (%(n)s, %(ctx)s, %(s)s, 'pending')
                """,
                {"n": raw_name, "ctx": json.dumps(context), "s": self.source_id},
            )

    def _upsert_match(
        self,
        m: SidgadMatch,
        season_competition_id: int,
        home_club: int,
        away_club: int,
        season_start_year: int,
    ) -> None:
        played = m.home_goals is not None and m.away_goals is not None
        gd = m.gamedate
        matchday = date(int(gd[0:4]), int(gd[4:6]), int(gd[6:8])) if len(gd) == 8 else None
        disciplinary = "resolución disciplinaria" in (m.raw_attrs.get("notes") or "").lower()

        c = self.db.conn.cursor()
        c.execute(
            """
            INSERT INTO match (
                season_competition_id, round, stage, matchday_date, status,
                home_club_id, away_club_id, home_goals, away_goals,
                source_id, source_url, source_ref, confidence, result_type, notes
            ) VALUES (
                %(sc)s, %(round)s, 'regular', %(md)s, %(st)s,
                %(h)s, %(a)s, %(hg)s, %(ag)s,
                %(src)s, %(surl)s, %(sref)s, 'high', %(rt)s, %(notes)s
            )
            ON CONFLICT DO NOTHING
            """,
            {
                "sc": season_competition_id,
                "round": m.round,
                "md": matchday,
                "st": "played" if played else "scheduled",
                "h": home_club,
                "a": away_club,
                "hg": m.home_goals,
                "ag": m.away_goals,
                "src": self.source_id,
                "surl": f"https://www.hockeypatines.fep.es/league/{m.raw_attrs.get('idc', '')}",
                "sref": m.idp,
                "rt": "disciplinary" if disciplinary else None,
                "notes": m.raw_attrs.get("notes"),
            },
        )
        if m.idp and m.idp.isdigit():
            # match_id per l'external_id: busca'l per (sc, ref)
            c.execute(
                "SELECT match_id FROM match WHERE season_competition_id = %s AND source_ref = %s",
                (season_competition_id, m.idp),
            )
            row = c.fetchone()
            if row:
                self.upsert_external_id("match", row["match_id"], m.idp)
