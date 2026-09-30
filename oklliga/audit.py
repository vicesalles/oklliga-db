"""Auditoria i fusió de clubs: detectar i corregir errors d'identitat.

- detect_suspect_clubs(): anomalies que poden indicar clubs duplicats
  o dades inconsistents. Cap càrrega es dóna per bona sense auditoria.
- merge_clubs(): correcció auditada i reversible-log de clubs duplicats.
"""

from __future__ import annotations

from typing import Any, Optional

import psycopg
from psycopg.rows import dict_row

SUSPECT_QUERIES: dict[str, str] = {
    "clubs_sense_noms": """
        SELECT c.club_id, c.canonical_name
        FROM club c
        WHERE NOT EXISTS (SELECT 1 FROM club_name cn WHERE cn.club_id = c.club_id)
    """,
    "clubs_sense_partits": """
        SELECT c.club_id, c.canonical_name
        FROM club c
        WHERE NOT EXISTS (
            SELECT 1
            FROM team t
            JOIN match m
              ON m.home_team_id = t.team_id OR m.away_team_id = t.team_id
            WHERE t.club_id = c.club_id
        )
    """,
    "noms_gairebe_identics": """
        SELECT a.club_id AS club_a, a.canonical_name AS nom_a,
               b.club_id AS club_b, b.canonical_name AS nom_b,
               a.canonical_name <> b.canonical_name AS diferent
        FROM club a
        JOIN club b ON a.club_id < b.club_id
        WHERE lower(a.canonical_name) = lower(b.canonical_name)
    """,
}


class ClubAuditor:
    """Auditoria d'integritat de la identitat de clubs."""

    def __init__(self, conninfo: str):
        self._conninfo = conninfo
        self._conn: Optional[psycopg.Connection] = None

    def connect(self) -> None:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(
                self._conninfo, row_factory=dict_row, autocommit=True
            )

    def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            self._conn.close()

    def __enter__(self) -> "ClubAuditor":
        self.connect()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @property
    def conn(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            raise RuntimeError("No connectat")
        return self._conn

    # ------------------------------------------------------------ auditoria

    def detect_suspect_clubs(self) -> dict[str, list[dict[str, Any]]]:
        """Retorna anomalies per categoria. Clau buida = tot correcte."""
        results: dict[str, list[dict[str, Any]]] = {}
        c = self.conn.cursor()
        for name, query in SUSPECT_QUERIES.items():
            try:
                c.execute(query)
                rows = list(c.fetchall())
            except psycopg.errors.UndefinedTable:
                rows = []
            if rows:
                results[name] = rows
        return results

    # --------------------------------------------------------------- fusió

    def merge_clubs(
        self,
        merged_club_id: int,
        kept_club_id: int,
        merged_by: str = "human",
        reason: Optional[str] = None,
    ) -> None:
        """Fusiona el club erroni (merged) dins del correcte (kept).

        Reassigna totes les referències (noms, partits, participacions,
        fitxatges, esdeveniments, estadístiques), registra la fusió al
        log i elimina el club erroni. Tot atòmic i auditat.
        """
        if merged_club_id == kept_club_id:
            raise ValueError("No es pot fusionar un club amb si mateix")
        c = self.conn.cursor()
        with self.conn.transaction():
            # Verifica que tots dos existeixen
            c.execute(
                "SELECT club_id FROM club WHERE club_id IN (%s, %s)",
                (merged_club_id, kept_club_id),
            )
            if len(c.fetchall()) != 2:
                raise ValueError(
                    f"Algun dels clubs no existeix: {merged_club_id}, {kept_club_id}"
                )

            # Reassignació de les FK directes del club (noms, fitxatges)
            for table, col in (
                ("club_name", "club_id"),
                ("squad_membership", "club_id"),
            ):
                c.execute(
                    f"UPDATE {table} SET {col} = %s WHERE {col} = %s",
                    (kept_club_id, merged_club_id),
                )
            # Els equips del club erroni es mouen al correcte: per cada
            # label, si el club correcte ja té un equip amb la mateixa
            # label, les referències es reapunten a l'equip existent;
            # si no, l'equip canvia de club.
            c.execute(
                """
                SELECT team_id, label FROM team WHERE club_id = %s
                """,
                (merged_club_id,),
            )
            merged_teams = list(c.fetchall())
            for mt in merged_teams:
                c.execute(
                    "SELECT team_id FROM team WHERE club_id = %s AND label = %s",
                    (kept_club_id, mt["label"]),
                )
                kept_team = c.fetchone()
                target = kept_team["team_id"] if kept_team else None
                if target is not None:
                    # Partits ENTRE l'equip duplicat i l'equip del club bo:
                    # després de la fusió serien "club contra si mateix"
                    # (simulacres erronis: eren la mateixa identitat).
                    c.execute(
                        """
                        DELETE FROM match
                        WHERE (home_team_id = %s AND away_team_id = %s)
                           OR (home_team_id = %s AND away_team_id = %s)
                        """,
                        (mt["team_id"], target, target, mt["team_id"]),
                    )
                    # Reassignacio atomica dels partits: un sol UPDATE per
                    # no passar mai per un estat intermig home=away.
                    c.execute(
                        """
                        UPDATE match SET
                            home_team_id = CASE WHEN home_team_id = %s THEN %s
                                                ELSE home_team_id END,
                            away_team_id = CASE WHEN away_team_id = %s THEN %s
                                                ELSE away_team_id END
                        WHERE %s IN (home_team_id, away_team_id)
                        """,
                        (mt["team_id"], target,
                         mt["team_id"], target, mt["team_id"]),
                    )
                    # Reapunta la resta de referències a l'equip del club bo
                    for table, col in (
                        ("participation", "team_id"),
                        ("match_player", "team_id"),
                        ("match_event", "team_id"),
                        ("team_match_stat", "team_id"),
                    ):
                        c.execute(
                            f"UPDATE {table} SET {col} = %s WHERE {col} = %s",
                            (target, mt["team_id"]),
                        )
                    c.execute("DELETE FROM team WHERE team_id = %s", (mt["team_id"],))
                else:
                    c.execute(
                        "UPDATE team SET club_id = %s WHERE team_id = %s",
                        (kept_club_id, mt["team_id"]),
                    )
            

            # Noms que queden duplicats al club correcte després de la fusió
            c.execute(
                """
                DELETE FROM club_name a
                USING club_name b
                WHERE a.club_id = %s AND b.club_id = %s
                  AND a.club_name_id > b.club_name_id
                  AND a.name = b.name
                """,
                (kept_club_id, kept_club_id),
            )

            # Log de la fusió, PRIMER que l'eliminació per auditabilitat
            c.execute(
                """
                INSERT INTO club_merge_log
                    (merged_club_id, kept_club_id, merged_by, reason)
                VALUES (%s, %s, %s, %s)
                """,
                (merged_club_id, kept_club_id, merged_by, reason),
            )
            c.execute("DELETE FROM club WHERE club_id = %s", (merged_club_id,))

    def merge_log(self) -> list[dict[str, Any]]:
        c = self.conn.cursor()
        c.execute(
            """
            SELECT l.*, kc.canonical_name AS kept_name
            FROM club_merge_log l
            LEFT JOIN club kc ON kc.club_id = l.kept_club_id
            ORDER BY l.merged_at DESC
            """
        )
        return list(c.fetchall())
