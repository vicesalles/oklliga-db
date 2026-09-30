"""Càrrega inicial de clubs de l'OK Lliga 2022/23 (Parlem OK Lliga).

Font: hockeypatines.fep.es (lliga 2477) i article "OK Liga 2022-23" de la
Viquipèdia amb dades de la RFEP. La classificació final completa de la
fase regular es completarà amb l'scraping; aquí només es registra el
campió de la fase regular (FC Barcelona, 68 punts).

Diferències vs 2024/25: baixen HC Alpicat, CP Vic i CP Vilafranca;
pujen CH Mataró i Girona CH.

Idempotent: es pot reexecutar sense duplicats.

Ús (llegint OKLLIGA_DSN del fitxer .env de l'arrel):
    python scripts/seed_2022_23.py

O amb DSN explícit (override):
    python scripts/seed_2022_23.py postgresql://user:pass@localhost/oklliga
"""
import sys
sys.path.insert(0, ".")

from oklliga import OkLligaDB
from oklliga.config import get_dsn

SOURCE_NAME = "RFEP - hockeypatines.fep.es"
SOURCE_URL = "https://www.hockeypatines.fep.es/league/2092"

# Clubs de la Parlem OK Lliga 2022/23.
# Convenció: canonical_name = nom del club SENSE patrocinador;
# el nom amb patrocinador (el que apareix a la font) es registra a
# club_name amb is_sponsor_name=true i vigència de la temporada.
# Tuple: (canonical_name, nom_a_la_font_2023, ciutat, província, comarca)
EQUIPS_2022_23 = [
    ("FC BARCELONA", "BARÇA", "Barcelona", "Barcelona", "Barcelonès"),
    ("HOCKEY CLUB LICEO", "DEPORTIVO LICEO", "la Corunya", "la Corunya", None),
    ("REUS DEPORTIU", "REUS DEPORTIU VIRGINIAS", "Reus", "Tarragona", "Baix Camp"),
    ("IGUALADA HC", "IGUALADA RIGAT HC", "Igualada", "Barcelona", "Anoia"),
    ("CP CALAFELL", "PARLEM CALAFELL", "Calafell", "Tarragona", "Baix Penedès"),
    ("CE NOIA FREIXENET", "CE NOIA FREIXENET", "Sant Sadurní d'Anoia", "Barcelona", "Alt Penedès"),
    ("LLEIDA LLISTA BLAVA", "FINQUES PRATS LLEIDA", "Lleida", "Lleida", "Segrià"),
    ("AITEX PAS ALCOI", "PAS ALCOI", "Alcoi", "Alacant", None),
    ("CH CALDES", "RECAM LÀSER CH CALDES", "Caldes de Montbui", "Barcelona", "Vallès Oriental"),
    ("CP VOLTREGA", "CP VOLTREGA STERN MOTOR", "Vic", "Barcelona", "Osona"),
    ("CP VILAFRANCA", "DIGITTECNIC CPV CAPITAL DEL VI", "Vilafranca del Penedès", "Barcelona", "Alt Penedès"),
    ("GIRONA CH", "GARATGE PLANA GIRONA CH", "Girona", "Girona", "Gironès"),
    ("CE ARENYS DE MUNT", "CE ARENYS DE MUNT SOLARTRADEX", "Arenys de Munt", "Barcelona", "Maresme"),
    ("PHC SANT CUGAT", "SOLIDEO PHC SANT CUGAT", "Sant Cugat del Vallès", "Barcelona", "Vallès Occidental"),
]

# Campió de la fase regular (classificació RFEP, 68 punts)
LIDER_FASE_REGULAR = "FC BARCELONA"

VIGENCIA_2022_23 = ("2022-07-01", "2023-06-30")


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
            "url": SOURCE_URL,
            "notes": "Càrrega inicial manual de la temporada 2022/23",
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

            # Temporada 2022/23
            season = db.upsert_season(2022)
            sc = db.season_competition_id(
                ok, season, "Parlem OK Lliga", fmt="lliga regular a doble volta"
            )
            club_ids = {}
            for canonic, nom_font, ciutat, provincia, comarca in EQUIPS_2022_23:
                club_id = db.upsert_club(
                    canonic, city=ciutat, province=provincia, comarca=comarca
                )
                db.add_club_name(
                    club_id,
                    nom_font,
                    valid_from=VIGENCIA_2022_23[0],
                    valid_until=VIGENCIA_2022_23[1],
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
                        "notes": "Càrrega inicial manual 2022/23",
                    },
                )

            # Posició confirmada per la font: FC Barcelona 1r de la fase regular
            c = db.conn.cursor()
            c.execute(
                """
                UPDATE participation
                SET final_position = 1,
                    notes = '1r de la fase regular (classificació RFEP 2022/23)'
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
            WHERE sn.start_year = 2022
            """
        )
        print(f"OK: {c.fetchone()['clubs']} equips registrats a la Parlem OK Lliga 2022/23")
        print(f"Líder fase regular: {LIDER_FASE_REGULAR} (posició 1)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else get_dsn())
