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
            SELECT 1 FROM match m
            WHERE m.home_club_id = c.club_id OR m.away_club_id = c.club_id
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

            # Reassignació de totes les FK que apunten al club erroni
            for table, col in (
                ("club_name", "club_id"),
                ("participation", "club_id"),
                ("squad_membership", "club_id"),
                ("match_player", "club_id"),
                ("match_event", "club_id"),
                ("team_match_stat", "club_id"),
            ):
                c.execute(
                    f"UPDATE {table} SET {col} = %s WHERE {col} = %s",
                    (kept_club_id, merged_club_id),
                )
            # Partits ENTRE els dos clubs fusionats: després de la fusió
            # serien "club contra si mateix" i violen match_home_away_diff.
            # Son simulacres erronis (els dos clubs son la mateixa identitat).
            c.execute(
                """
                DELETE FROM match
                WHERE (home_club_id = %s AND away_club_id = %s)
                   OR (home_club_id = %s AND away_club_id = %s)
                """,
                (merged_club_id, kept_club_id,
                 kept_club_id, merged_club_id),
            )
            # Reassignacio atomica: un sol UPDATE per no passar mai per
            # un estat intermig home=away (la constraint match_home_away_diff
            # es validaria fila a fila en dos UPDATE separats).
            c.execute(
                """
                UPDATE match SET
                    home_club_id = CASE WHEN home_club_id = %s THEN %s
                                        ELSE home_club_id END,
                    away_club_id = CASE WHEN away_club_id = %s THEN %s
                                        ELSE away_club_id END
                WHERE %s IN (home_club_id, away_club_id)
                """,
                (merged_club_id, kept_club_id,
                 merged_club_id, kept_club_id, merged_club_id),
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
