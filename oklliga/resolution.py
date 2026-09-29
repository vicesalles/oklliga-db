"""Resolució assistida de noms d'equip: cap càrrega a cegues.

Flux: l'scraper troba un nom → resolve_club() → si retorna None,
el nom va a la cua `pending_name_resolution` amb candidats proposats.
Un humà classifica cada pendent (club existent o club nou) i la decisió
queda persistida com a àlies a club_name: cada nom ambigu es decideix
UNA sola vegada en tot el projecte.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Optional

import psycopg
from psycopg.rows import dict_row


def normalize_name(name: str) -> str:
    """Mateixa normalització que name_normalized de la BD.

    Minúscules, sense accents ni puntuació, espais col·lapsats.
    Ha de mantenir-se sincronitzada amb la columna generada de club_name.
    """
    import unicodedata

    txt = unicodedata.normalize("NFKD", name)
    txt = "".join(c for c in txt if not unicodedata.combining(c))
    txt = "".join(c if c.isalnum() or c == " " else "" for c in txt.lower())
    return " ".join(txt.split())


class NameResolver:
    """Gestiona la cua de resolució assistida de noms."""

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

    def __enter__(self) -> "NameResolver":
        self.connect()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @property
    def conn(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            raise RuntimeError("No connectat")
        return self._conn

    # ------------------------------------------------------------- cua

    def enqueue(
        self,
        raw_name: str,
        context: Optional[dict[str, Any]] = None,
        source_id: Optional[int] = None,
    ) -> int:
        """Afegeix un nom no resolt a la cua (idempotent per nom+context)."""
        import json as _json

        context_json = _json.dumps(context) if context is not None else None
        c = self.conn.cursor()
        c.execute(
            """
            INSERT INTO pending_name_resolution (raw_name, context, source_id)
            SELECT %(raw_name)s, %(context_json)s::jsonb, %(source_id)s
            WHERE NOT EXISTS (
                SELECT 1 FROM pending_name_resolution
                WHERE raw_name = %(raw_name)s
                  AND status = 'pending'
                  AND context IS NOT DISTINCT FROM %(context_json)s::jsonb
            )
            RETURNING pending_id
            """,
            {"raw_name": raw_name, "context_json": context_json,
             "source_id": source_id},
        )
        row = c.fetchone()
        if row is None:
            c.execute(
                """
                SELECT pending_id FROM pending_name_resolution
                WHERE raw_name = %s AND status = 'pending'
                ORDER BY pending_id LIMIT 1
                """,
                (raw_name,),
            )
            row = c.fetchone()
        return row["pending_id"]

    def propose_candidates(self, raw_name: str, limit: int = 5) -> list[dict[str, Any]]:
        """Proposa candidats per similaritat (trigram-like manual amb % i normalització).

        Ordena per: coincidència exacta normalitzada > substring > prefix comú més llarg.
        Mai crea res: només suggereix. La decisió és humana.
        """
        norm = normalize_name(raw_name)
        tokens = norm.split()
        if not tokens:
            return []
        # Prefixos creixents del nom: "igualada", "igualada rigat", ...
        # Aixi "Igualada Rigat HC 2002" troba "Igualada Rigat HC".
        prefixes = [" ".join(tokens[:i]) for i in range(len(tokens), 0, -1)]
        c = self.conn.cursor()
        # La mateixa normalitzacio que la BD, aplicada a canonical_name
        canon_norm = """lower(regexp_replace(
            regexp_replace(name, '[^[:alnum:] ]', '', 'g'), '\s+', ' ', 'g'
        ))"""
        c.execute(
            """
            WITH candidats AS (
                SELECT c.club_id, c.canonical_name,
                       cn.name AS matched_name,
                       cn.valid_from, cn.valid_until,
                       cn.name_normalized AS norm
                FROM club_name cn
                JOIN club c ON c.club_id = cn.club_id
                UNION ALL
                SELECT c.club_id, c.canonical_name,
                       c.canonical_name AS matched_name,
                       NULL::date, NULL::date,
                       """ + canon_norm.replace("name", "c.canonical_name") + """ AS norm
                FROM club c
            )
            SELECT club_id, canonical_name, matched_name AS name,
                   valid_from, valid_until
            FROM candidats
            WHERE norm = ANY(%(exact_arr)s)
               OR norm LIKE %(sub)s
               OR EXISTS (
                   SELECT 1 FROM unnest(%(prefixes)s::text[]) AS pf
                   WHERE norm LIKE concat(pf, ' ', '%%')
                      OR norm = pf
               )
            ORDER BY
                (norm = %(exact)s)::int DESC,
                (norm LIKE %(sub)s)::int DESC,
                length(norm)
            LIMIT %(limit)s
            """,
            {
                "exact": norm,
                "exact_arr": [norm],
                "sub": f"%{norm}%",
                "prefixes": prefixes,
                "limit": limit,
            },
        )
        return list(c.fetchall())

    def pending(self) -> list[dict[str, Any]]:
        """Tots els pendents amb els seus candidats proposats."""
        c = self.conn.cursor()
        c.execute(
            """
            SELECT p.pending_id, p.raw_name, p.context, p.source_id
            FROM pending_name_resolution p
            WHERE p.status = 'pending'
            ORDER BY p.pending_id
            """
        )
        rows = list(c.fetchall())
        for r in rows:
            r["candidates"] = self.propose_candidates(r["raw_name"])
        return rows

    # ----------------------------------------------------- decisions humanes

    def resolve_as_existing(
        self,
        pending_id: int,
        club_id: int,
        resolved_by: str = "human",
        notes: Optional[str] = None,
    ) -> int:
        """Decisió humana: el nom és un àlies d'un club existent.

        Persisteix l'àlies a club_name (de vigència indefinida si no es
        passa data) i marca el pendent com resolt. L'àlies queda
        disponible per sempre per a futures resolucions automàtiques.
        """
        c = self.conn.cursor()
        with self.conn.transaction():
            c.execute(
                "SELECT raw_name FROM pending_name_resolution WHERE pending_id = %s",
                (pending_id,),
            )
            row = c.fetchone()
            if row is None:
                raise ValueError(f"pending_id {pending_id} no existeix")
            c.execute(
                """
                INSERT INTO club_name (club_id, name)
                VALUES (%s, %s)
                ON CONFLICT DO NOTHING
                """,
                (club_id, row["raw_name"]),
            )
            c.execute(
                """
                UPDATE pending_name_resolution
                SET status = 'resolved',
                    resolved_club_id = %s,
                    created_new_club = false,
                    resolved_by = %s,
                    resolved_at = now(),
                    notes = COALESCE(%s, notes)
                WHERE pending_id = %s
                """,
                (club_id, resolved_by, notes, pending_id),
            )
        return club_id

    def resolve_as_new_club(
        self,
        pending_id: int,
        canonical_name: str,
        resolved_by: str = "human",
        city: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> int:
        """Decisió humana: és un club genuïnament nou. Es crea i es registra."""
        c = self.conn.cursor()
        with self.conn.transaction():
            c.execute(
                """
                INSERT INTO club (canonical_name, city)
                VALUES (%s, %s)
                ON CONFLICT (canonical_name) DO NOTHING
                RETURNING club_id
                """,
                (canonical_name, city),
            )
            row = c.fetchone()
            if row is None:
                c.execute(
                    "SELECT club_id FROM club WHERE canonical_name = %s",
                    (canonical_name,),
                )
                row = c.fetchone()
            club_id = row["club_id"]
            c.execute(
                """
                SELECT raw_name FROM pending_name_resolution WHERE pending_id = %s
                """,
                (pending_id,),
            )
            raw = c.fetchone()["raw_name"]
            c.execute(
                """
                INSERT INTO club_name (club_id, name)
                VALUES (%s, %s)
                ON CONFLICT DO NOTHING
                """,
                (club_id, raw),
            )
            c.execute(
                """
                UPDATE pending_name_resolution
                SET status = 'resolved',
                    resolved_club_id = %s,
                    created_new_club = true,
                    resolved_by = %s,
                    resolved_at = now(),
                    notes = COALESCE(%s, notes)
                WHERE pending_id = %s
                """,
                (club_id, resolved_by, notes, pending_id),
            )
        return club_id

    def reject(
        self,
        pending_id: int,
        resolved_by: str = "human",
        notes: Optional[str] = None,
    ) -> None:
        """Decisió humana: el nom és un error de la font (OCR, tipografia...)."""
        c = self.conn.cursor()
        c.execute(
            """
            UPDATE pending_name_resolution
            SET status = 'rejected', resolved_by = %s, resolved_at = now(),
                notes = COALESCE(%s, notes)
            WHERE pending_id = %s
            """,
            (resolved_by, notes, pending_id),
        )
