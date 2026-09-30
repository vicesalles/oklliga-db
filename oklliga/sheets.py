"""Ingesta de fitxes de partit (rfep_gr_{idp}_{idm}.php) i plantilles.

Flux per a una edició (idc):
  1. Plantilles (stats_1_{idc}.php, tipo_stats=plantillas): id_player + nom
     per team_entry_id. Crea jugadors (external_id 'player') i enregistra
     la pertinença a l'edició com a external_id 'squad_member' amb
     external_id = '{team_entry_id}:{id_player}'.
  2. Per cada partit amb idp: fitxa -> pavelló/localitat a match (camps
     simples), àrbitres (entitats referee + match_referee), alineacions
     (match_player, resoltes per nom dins la plantilla del mateix equip)
     i incidències (match_event, per id_player).

Resolució de jugadors de l'acta: per nom normalitzat dins la plantilla
del mateix team_entry_id. Si no coincideix, la fila s'omet i el nom va a
pending_name_resolution (context kind='match_sheet_lineup') — mai es
crea res a cegues. Esdeveniments amb id_player=0 o sense jugador:
player_id NULL (equipatiu).
"""
from __future__ import annotations

import json
from typing import Any, Optional

from .client import OkLligaDB
from .resolution import normalize_name
from .sidgad import (
    SidgadClient,
    SheetEvent,
    parse_match_sheet,
    parse_squads,
)

# Tipus normalitzat del parser -> enum match_event_type.
# 'foul', 'timeout', 'penalty_awarded', 'free_direct_awarded', 'no_goal'
# no són esdeveniments del model: queden a value_json per auditoria.
EVENT_TYPE_MAP = {
    "goal": "goal",
    "blue_card": "blue_card",
    "yellow_card": "yellow_card",
    "red_card": "red_card",
}


def _lineup_key(name: str) -> str:
    """Clau de resolució de nom d'alineació -> nom de plantilla."""
    return normalize_name(name).upper().replace(" ", "")


def _clock_to_minute(period: Optional[str], clock: Optional[str]) -> Optional[int]:
    """Converteix ('P2', '17:56') -> minut absolut aproximat."""
    if not period or not clock:
        return None
    try:
        mm, _ss = clock.split(":")
        base = 0 if period.upper() in ("P1", "PR") else 25
        return base + int(mm)
    except ValueError:
        return None


def _period_half(period: Optional[str]) -> Optional[int]:
    if not period:
        return None
    p = period.upper()
    if p in ("P1", "PR"):
        return 1
    if p == "P2":
        return 2
    return None


class SheetIngest:
    """Orquestra la ingesta de plantilles i fitxes d'una edició."""

    def __init__(self, db: OkLligaDB, client: Optional[SidgadClient] = None):
        self.db = db
        self.client = client or SidgadClient()
        self.source_id: Optional[int] = None

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

    # ------------------------------------------------------------- jugadors
    def upsert_player(self, id_player: str, full_name: str) -> int:
        """Jugador per external_id. Crea player + player_name si és nou."""
        c = self.db.conn.cursor()
        c.execute(
            """
            SELECT internal_id FROM external_id
            WHERE source_id = %s AND entity_type = 'player' AND external_id = %s
            """,
            (self.source_id, id_player),
        )
        row = c.fetchone()
        if row:
            player_id = row["internal_id"]
            c.execute(
                "UPDATE player SET full_name = %(n)s"
                " WHERE player_id = %(i)s AND full_name <> %(n)s",
                {"n": full_name, "i": player_id},
            )
            return player_id
        c.execute(
            "INSERT INTO player (full_name) VALUES (%(n)s) RETURNING player_id",
            {"n": full_name},
        )
        player_id = c.fetchone()["player_id"]
        c.execute(
            """
            INSERT INTO player_name (player_id, name) VALUES (%(p)s, %(n)s)
            ON CONFLICT DO NOTHING
            """,
            {"p": player_id, "n": full_name},
        )
        c.execute(
            """
            INSERT INTO external_id (source_id, entity_type, internal_id, external_id)
            VALUES (%(s)s, 'player', %(i)s, %(e)s)
            ON CONFLICT (source_id, entity_type, external_id) DO UPDATE
                SET fetched_at = now()
            """,
            {"s": self.source_id, "i": player_id, "e": id_player},
        )
        return player_id

    def resolve_player_by_id(
        self, team_entry_id: str, id_player: str
    ) -> Optional[int]:
        """player_id per (team_entry, id_player) via squad_member."""
        c = self.db.conn.cursor()
        c.execute(
            """
            SELECT internal_id FROM external_id
            WHERE source_id = %s AND entity_type = 'squad_member'
              AND external_id = %s
            """,
            (self.source_id, f"{team_entry_id}:{id_player}"),
        )
        row = c.fetchone()
        return row["internal_id"] if row else None

    def squad_by_name(self, team_entry_id: str) -> dict[str, int]:
        """Mapa _lineup_key(nom de plantilla) -> player_id de l'equip."""
        c = self.db.conn.cursor()
        c.execute(
            """
            SELECT e.external_id, e.internal_id, p.full_name
            FROM external_id e
            JOIN player p ON p.player_id = e.internal_id
            WHERE e.source_id = %s AND e.entity_type = 'squad_member'
              AND e.external_id LIKE %s
            """,
            (self.source_id, f"{team_entry_id}:%"),
        )
        out: dict[str, int] = {}
        for row in c.fetchall():
            out[_lineup_key(row["full_name"])] = row["internal_id"]
        return out

    # ------------------------------------------------------------ plantilles
    def ingest_squads(self, idc: int) -> int:
        """Plantilla de l'edició: jugadors + squad_member + snapshot."""
        html = self.client.fetch(
            f"rfep/rfep_stats_1_{idc}.php",
            params={"idc": str(idc), "tipo_stats": "plantillas", "idm": "1"},
        )
        c = self.db.conn.cursor()
        c.execute(
            """
            INSERT INTO raw_snapshot
                (source_id, endpoint, params, body_hash, body, parser_version)
            VALUES (%(s)s, %(e)s, %(p)s, %(h)s, %(b)s, %(v)s)
            ON CONFLICT (endpoint, body_hash) DO UPDATE
                SET fetched_at = now(), parser_version = EXCLUDED.parser_version
            RETURNING raw_snapshot_id
            """,
            {
                "s": self.source_id,
                "e": f"rfep_stats_1_{idc}.php",
                "p": json.dumps({"tipo_stats": "plantillas"}),
                "h": str(len(html)),
                "b": html,
                "v": "sidgad-squads-v0",
            },
        )
        squads = parse_squads(html)
        for sq in squads:
            full = f"{sq.surname}, {sq.given_name}".strip(", ")
            player_id = self.upsert_player(sq.id_player, full)
            c.execute(
                """
                INSERT INTO external_id
                    (source_id, entity_type, internal_id, external_id)
                VALUES (%(s)s, 'squad_member', %(p)s, %(x)s)
                ON CONFLICT (source_id, entity_type, external_id) DO UPDATE
                    SET fetched_at = now()
                """,
                {
                    "s": self.source_id,
                    "p": player_id,
                    "x": f"{sq.team_entry_id}:{sq.id_player}",
                },
            )
        return len(squads)

    # --------------------------------------------------------------- fitxes
    def _referee_id(self, full_name: str) -> int:
        c = self.db.conn.cursor()
        c.execute(
            """
            INSERT INTO referee (full_name) VALUES (%(n)s)
            ON CONFLICT (full_name) DO UPDATE SET full_name = EXCLUDED.full_name
            RETURNING referee_id
            """,
            {"n": full_name},
        )
        return c.fetchone()["referee_id"]

    def ingest_match_sheet(
        self,
        match_id: int,
        idp: str,
        team_by_entry: dict[str, int],
        entry_by_edition_name: dict[str, str],
    ) -> bool:
        """Ingereix la fitxa d'un partit. Retorna False si no hi ha fitxa.

        team_by_entry: team_entry_id -> team_id intern.
        entry_by_edition_name: nom visible de l'edició (teams_array) ->
        team_entry_id; la fitxa mostra el mateix nom.
        """
        import urllib.error
        try:
            html = self.client.fetch_match_sheet(int(idp))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False
            raise
        sheet = parse_match_sheet(html)
        if sheet is None:
            return False
        c = self.db.conn.cursor()

        c.execute(
            "UPDATE match SET venue = COALESCE(%(v)s, venue) WHERE match_id = %(m)s",
            {"v": sheet.venue, "m": match_id},
        )

        for name, role in sheet.referees:
            rid = self._referee_id(name)
            c.execute(
                """
                INSERT INTO match_referee (match_id, referee_id, role)
                VALUES (%(m)s, %(r)s, %(role)s)
                ON CONFLICT (match_id, referee_id, role) DO NOTHING
                """,
                {"m": match_id, "r": rid, "role": role},
            )

        for ev in sheet.events:
            self._ingest_event(c, match_id, ev, team_by_entry)

        home_entry = entry_by_edition_name.get(sheet.home_name.strip())
        away_entry = entry_by_edition_name.get(sheet.away_name.strip())
        for entry, lineup in (
            (home_entry, sheet.home_lineup),
            (away_entry, sheet.away_lineup),
        ):
            if not entry or entry not in team_by_entry:
                continue
            team_id = team_by_entry[entry]
            squad = self.squad_by_name(entry)
            for row in lineup:
                player_id = squad.get(_lineup_key(row.name))
                if player_id is None:
                    self._pend_lineup(row.name, entry, match_id)
                    continue
                c.execute(
                    """
                    INSERT INTO match_player
                        (match_id, team_id, player_id, is_goalkeeper, source_id)
                    VALUES (%(m)s, %(t)s, %(p)s, %(g)s, %(s)s)
                    ON CONFLICT (match_id, player_id) DO NOTHING
                    """,
                    {
                        "m": match_id, "t": team_id, "p": player_id,
                        "g": row.is_goalkeeper, "s": self.source_id,
                    },
                )
        return True

    def _ingest_event(
        self,
        c: Any,
        match_id: int,
        ev: SheetEvent,
        team_by_entry: dict[str, int],
    ) -> None:
        entry = ev.team_entry_id or ""
        team_id = team_by_entry.get(entry)
        if team_id is None:
            return
        player_id = None
        if ev.id_player:
            player_id = self.resolve_player_by_id(entry, ev.id_player)
        etype = EVENT_TYPE_MAP.get(ev.event_type)
        if etype is None:
            return
        if etype == "goal":
            if ev.detail == "falta directa":
                etype = "free_kick_goal"
            elif ev.detail == "penalti":
                etype = "penalty_goal"
        c.execute(
            """
            INSERT INTO match_event
                (match_id, team_id, player_id, event_type, minute, half,
                 value_json, source_id, confidence)
            VALUES (%(m)s, %(t)s, %(p)s, %(e)s, %(min)s, %(h)s,
                    %(vj)s, %(s)s, 'high')
            ON CONFLICT DO NOTHING
            """,
            {
                "m": match_id, "t": team_id, "p": player_id, "e": etype,
                "min": _clock_to_minute(ev.period, ev.clock),
                "h": _period_half(ev.period),
                "vj": json.dumps({
                    "clock": ev.clock, "period": ev.period,
                    "detail": ev.detail, "dorsal": ev.dorsal,
                    "score_after": ev.score_after, "raw": ev.raw_text,
                }),
                "s": self.source_id,
            },
        )

    def _pend_lineup(self, name: str, team_entry_id: str, match_id: int) -> None:
        c = self.db.conn.cursor()
        c.execute(
            """
            INSERT INTO pending_name_resolution
                (raw_name, context, source_id, status)
            VALUES (%(n)s, %(ctx)s, %(s)s, 'pending')
            ON CONFLICT DO NOTHING
            """,
            {
                "n": name,
                "ctx": json.dumps({
                    "kind": "match_sheet_lineup",
                    "team_entry_id": team_entry_id,
                    "match_id": match_id,
                }),
                "s": self.source_id,
            },
        )
