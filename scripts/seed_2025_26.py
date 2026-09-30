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

# Clubs de la Parlem OK Lliga 2025/26.
# Convenció: canonical_name = nom del club SENSE patrocinador;
# el nom amb patrocinador (el que apareix a la font) es registra a
# club_name amb is_sponsor_name=true i vigència de la temporada.
# Tuple: (canonical_name, nom_a_la_font_2025, ciutat, província, comarca)
EQUIPS_2025_26 = [
    ("FC BARCELONA", "BARÇA", "Barcelona", "Barcelona", "Barcelonès"),
    ("ADISS HOCKEY RIVAS", "ADISS HOCKEY RIVAS", "Rivas-Vaciamadrid", "Madrid", None),
    ("IGUALADA HC", "IGUALADA RIGAT HC", "Igualada", "Barcelona", "Anoia"),
    ("CP VOLTREGA", "CP VOLTREGA MOVIMENTO STERN", "Vic", "Barcelona", "Osona"),
    ("HC SANT JUST", "INNOAESTHETICS HC SANT JUST", "Sant Just Desvern", "Barcelona", "Baix Llobregat"),
    ("SHUM MAÇANET", "SHUM FRIT RAVICH", "Maçanet de la Selva", "Girona", "Selva"),
    ("HOCKEY CLUB LICEO", "HOCKEY CLUB LICEO", "la Corunya", "la Corunya", None),
    ("AITEX PAS ALCOI", "AITEX PAS ALCOI", "Alcoi", "Alacant", None),
    ("REUS DEPORTIU", "REUS DEPORTIU BRASILIA", "Reus", "Tarragona", "Baix Camp"),
    ("LLEIDA LLISTA BLAVA", "PONS LLEIDA", "Lleida", "Lleida", "Segrià"),
    ("CE NOIA FREIXENET", "CE NOIA FREIXENET", "Sant Sadurní d'Anoia", "Barcelona", "Alt Penedès"),
    ("CH CALDES", "CH CALDES RECAM LÀSER", "Caldes de Montbui", "Barcelona", "Vallès Oriental"),
    ("CP CALAFELL", "CALAFELL LA MENORQUINA", "Calafell", "Tarragona", "Baix Penedès"),
    ("CERDANYOLA CLUB D'HOQUEI", "CERDANYOLA CLUB D'HOQUEI", "Cerdanyola del Vallès", "Barcelona", "Vallès Occidental"),
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
            for canonic, nom_font, ciutat, provincia, comarca in EQUIPS_2025_26:
                club_id = db.upsert_club(
                    canonic, city=ciutat, province=provincia, comarca=comarca
                )
                # El nom amb patrocinador com a nom històric de la temporada;
                # si coincideix amb el canònic també es registra (harmless).
                db.add_club_name(
                    club_id,
                    nom_font,
                    valid_from=VIGENCIA_2025_26[0],
                    valid_until=VIGENCIA_2025_26[1],
                    is_sponsor_name=True,
                )
                club_ids[canonic] = club_id
                team_id = db.upsert_team(club_id, "first")
                c = db.conn.cursor()
                c.execute(
                    """
                    INSERT INTO participation (season_competition_id, team_id, notes)
                    VALUES (%(sc)s, %(team)s, %(notes)s)
                    ON CONFLICT (season_competition_id, team_id) DO NOTHING
                    """,
                    {
                        "sc": sc,
                        "team": team_id,
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
                WHERE season_competition_id = %s AND team_id = %s
                """,
                (sc, db.upsert_team(club_ids[LIDER_FASE_REGULAR], "first")),
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
