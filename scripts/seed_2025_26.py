"""Càrrega inicial de clubs de l'OK Lliga 2025/26 (Parlem OK Lliga).

Font: hockeypatines.fep.es — Jornada 26 (última de la fase regular) i
notícia del 04/05/2026 "Punto y final a la fase regular de la OK Liga
con el HC Liceo en primera posición". La posició final només es registra
per al Liceo (1r, fase regular); la resta es completarà amb l'scraping
de la classificació completa.

Idempotent: es pot reexecutar sense duplicats.
Ús (llegint OKLLIGA_DSN del fitxer .env de l'arrel):
    python scripts/seed_2025_26.py

O amb DSN explícit (override):
    python scripts/seed_2025_26.py postgresql://user:pass@localhost/oklliga
"""

import sys

sys.path.insert(0, ".")

from oklliga import OkLligaDB
from oklliga.config import get_dsn

SOURCE_NAME = "RFEP - hockeypatines.fep.es"
SOURCE_URL = "https://www.hockeypatines.fep.es/league/2477"
LEAGUE_URL = "https://www.hockeypatines.fep.es/league/3150"

# Noms oficials exactes com apareixen a la font (Jornada 26, 01/05),
# amb ciutat, província i comarca (Catalunya).
EQUIPS_2025_26 = [
    ("BARÇA", "Barcelona", "Barcelona", "Barcelonès"),
    ("ADISS HOCKEY RIVAS", "Rivas-Vaciamadrid", "Madrid", None),
    ("IGUALADA RIGAT HC", "Igualada", "Barcelona", "Anoia"),
    ("CP VOLTREGA MOVIMENTO STERN", "Vic", "Barcelona", "Osona"),
    ("INNOAESTHETICS HC SANT JUST", "Sant Just Desvern", "Barcelona", "Baix Llobregat"),
    ("SHUM FRIT RAVICH", "Manresa", "Barcelona", "Bages"),
    ("HOCKEY CLUB LICEO", "la Corunya", "la Corunya", None),
    ("AITEX PAS ALCOI", "Alcoi", "Alacant", None),
    ("REUS DEPORTIU BRASILIA", "Reus", "Tarragona", "Baix Camp"),
    ("PONS LLEIDA", "Lleida", "Lleida", "Segrià"),
    ("CE NOIA FREIXENET", "Sant Sadurní d'Anoia", "Barcelona", "Alt Penedès"),
    ("CH CALDES RECAM LÀSER", "Caldes de Montbui", "Barcelona", "Vallès Oriental"),
    ("CALAFELL LA MENORQUINA", "Calafell", "Tarragona", "Baix Penedès"),
    ("CERDANYOLA CLUB D'HOQUEI", "Cerdanyola del Vallès", "Barcelona", "Vallès Occidental"),
]

# Únic resultat de classificació confirmat per la font (notícia 04/05/2026)
LIDER_FASE_REGULAR = "HOCKEY CLUB LICEO"

VIGENCIA_2025_26 = ("2025-07-01", "2026-06-30")


def upsert_source(db: OkLligaDB) -> int:
    c = db.conn.cursor()
    c.execute(
        """
        INSERT INTO source (name, url, notes)
        VALUES (%(name)s, %(url)s, %(notes)s)
        ON CONFLICT (name) DO UPDATE SET url = EXCLUDED.url
        RETURNING source_id
        """,
        {
            "name": SOURCE_NAME,
            "url": LEAGUE_URL,
            "notes": "Càrrega inicial manual de la temporada 2025/26",
        },
    )
    return c.fetchone()["source_id"]


def main(dsn: str) -> None:
    with OkLligaDB(dsn) as db:
        with db.transaction():
            source_id = upsert_source(db)

            # Competició màxima categoria i historial de noms
            ok = db.upsert_competition(1, notes="Màxima categoria estatal")
            db.add_competition_name(ok, "Divisió d'Honor", 1965, 2001)
            db.add_competition_name(ok, "OK Lliga", 2002, 2024)
            db.add_competition_name(ok, "Parlem OK Lliga", 2025, None)

            # Temporada 2025/26
            season = db.upsert_season(2025)
            sc = db.season_competition_id(
                ok, season, "Parlem OK Lliga", fmt="lliga regular a doble volta"
            )

            club_ids = {}
            for nom, ciutat, provincia, comarca in EQUIPS_2025_26:
                club_id = db.upsert_club(
                    nom, city=ciutat, province=provincia, comarca=comarca
                )
                db.add_club_name(
                    club_id,
                    nom,
                    valid_from=VIGENCIA_2025_26[0],
                    valid_until=VIGENCIA_2025_26[1],
                    is_sponsor_name=True,
                )
                club_ids[nom] = club_id

                c = db.conn.cursor()
                c.execute(
                    """
                    INSERT INTO participation (season_competition_id, club_id, notes)
                    VALUES (%(sc)s, %(club)s, %(notes)s)
                    ON CONFLICT (season_competition_id, club_id) DO NOTHING
                    """,
                    {
                        "sc": sc,
                        "club": club_id,
                        "notes": "Càrrega inicial manual 2025/26",
                    },
                )

            # Posició confirmada per la font: Liceo 1r de la fase regular
            c = db.conn.cursor()
            c.execute(
                """
                UPDATE participation
                SET final_position = 1,
                    notes = '1r de la fase regular (notícia RFEP 04/05/2026)'
                WHERE season_competition_id = %s AND club_id = %s
                """,
                (sc, club_ids[LIDER_FASE_REGULAR]),
            )

        # Resum
        c = db.conn.cursor()
        c.execute(
            """
            SELECT count(*) AS clubs FROM participation p
            JOIN season_competition s ON s.season_competition_id = p.season_competition_id
            JOIN season sn ON sn.season_id = s.season_id
            WHERE sn.start_year = 2025
            """
        )
        print(f"OK: {c.fetchone()['clubs']} equips registrats a la Parlem OK Lliga 2025/26")
        print(f"Líder fase regular: {LIDER_FASE_REGULAR} (posició 1)")


if __name__ == "__main__":
    dsn = sys.argv[1] if len(sys.argv) > 1 else get_dsn()
    main(dsn)
