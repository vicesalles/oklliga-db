"""Ingesta d'una temporada completa de l'OK Lliga des del portal RFEP/SIDGAD.

Flux (vegeu informe tècnic del 30/09/2026):
  1. Catàleg (rfep_ls_1.php) → troba l'edició (idc) de la fase regular
     de l'OK Lliga masculina de la temporada demanada
  2. teams_array de l'edició → inscripcions d'equip
  3. Calendari complet → partits amb resolució de club per ID extern,
     amb fallback a resolució per nom (cua d'humans, mai automàtica)
  4. Snapshots crus + external_ids per a reprocessat i traçabilitat

Ús:
    python scripts/ingest_season.py --season 2024
    python scripts/ingest_season.py --season 2024 --dry-run

Requereix:
    - Postgres amb esquema + migracions aplicats
    - Accés a www.server2.sidgad.es (la capçalera Origin l'envia el client)
    - Els clubs de la temporada ja carregats (seed_*.py) amb els seus
      IDs externs registrats, O es posposaran a la cua pending
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from oklliga import OkLligaDB
from oklliga.config import get_dsn
from oklliga.ingest import SidgadIngest
from oklliga.sidgad import (
    SidgadClient,
    parse_catalog_teams,
    parse_teams_array,
)

# Edicions (idc) de la fase regular masculina, segons l'informe tècnic.
# El catàleg les torna dinàmicament, però això serveix de mapa de control.
KNOWN_EDITIONS = {
    2026: 3568,
    2025: 3150,
    2024: 2816,
    2023: 2477,
    2022: 2092,
    2021: 1751,
    2020: 1474,
}

# rfep_season_id (opac, NO és l'any): segons el catàleg
KNOWN_RFEP_TEMP = {
    2026: 41,
    2025: 39,
    2024: 37,
    2023: 35,
    2022: 33,
    2021: 31,
    2020: 29,
}


def find_edition(catalog_html: str, rfep_temp: int) -> tuple[int, str]:
    """Troba l'idc de la fase regular masculina d'una temporada del catàleg."""
    import re

    # les competicions són <a ... class="... temp_{id} ..." id="{idc}"
    # idc_name="OK LIGA MASCULINA" ...>
    pattern = re.compile(
        r'class="([^"]*temp_%d[^"]*)"\s+id="(\d+)"' % rfep_temp
    )
    candidates = []
    for m in pattern.finditer(catalog_html):
        css, idc = m.groups()
        # el nom de la competició és a l'atribut idc_name del mateix <a>
        a_start = catalog_html.rfind("<a", 0, m.start())
        a_end = catalog_html.find(">", m.start())
        tag = catalog_html[a_start:a_end]
        name_m = re.search(r'idc_name="([^"]*)"', tag)
        name = name_m.group(1) if name_m else ""
        if "OK LIGA" in name.upper() and "PLATA" not in name.upper() \
                and "BRONCE" not in name.upper() and "IBERDROLA" not in name.upper():
            candidates.append((int(idc), name))
    if not candidates:
        raise SystemExit(
            f"No s'ha trobat l'edició de l'OK Lliga masculina per temp_{rfep_temp}"
        )
    if len(candidates) > 1:
        # Prefereix la que no sigui playoff: la fase regular acostuma a
        # anomenar-se exactament 'OK LIGA MASCULINA' o 'PARLEM OK LIGA'
        regular = [c for c in candidates if "PLAY" not in c[1].upper()]
        if regular:
            candidates = regular
    return candidates[0]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--season", type=int, required=True,
                    help="any d'inici de la temporada (2024 = 2024/25)")
    ap.add_argument("--dsn", default=None, help="DSN Postgres (per defecte .env)")
    ap.add_argument("--dry-run", action="store_true",
                    help="només analitza i mostra, no escriu a la BD")
    args = ap.parse_args()

    if args.season not in KNOWN_RFEP_TEMP:
        raise SystemExit(
            f"Temporada {args.season} sense rfep_season_id conegut; "
            "consulta el catàleg i amplia KNOWN_RFEP_TEMP"
        )
    rfep_temp = KNOWN_RFEP_TEMP[args.season]

    dsn = args.dsn or get_dsn()
    client = SidgadClient()

    print(f"Catàleg SIDGAD (temp_{rfep_temp} = temporada {args.season}/{(args.season+1)%100:02d})...")
    catalog = client.fetch_catalog()
    idc, comp_name = find_edition(catalog, rfep_temp)
    print(f"Edició trobada: idc={idc} ({comp_name})")

    teams = parse_catalog_teams(catalog, idc)
    if not teams:
        raise SystemExit("teams_array no trobat al catàleg per a aquesta idc")
    print(f"Equips a l'edició: {len(teams)}")

    if args.dry_run:
        for t in sorted(teams, key=lambda x: x.name):
            print(f"  {t.abbr:6s} {t.team_entry_id:6s} {t.name}")
        return

    with OkLligaDB(dsn) as db:
        ing = SidgadIngest(db, client=client)
        src = ing.ensure_source()

        # Temporada i edició al nostre model
        ok = db.upsert_competition(1, notes="Màxima categoria estatal")
        season = db.upsert_season(args.season)
        sc = db.season_competition_id(ok, season, comp_name,
                                       fmt="lliga regular a doble volta",
                                       stage="regular")
        ing.upsert_external_id("season", season, str(rfep_temp))
        ing.upsert_external_id("competition_edition", sc, str(idc))

        # Vincle team_entry → club per external_id registrat; els clubs
        # que no tinguin ID extern queden per resoldre per nom (cua).
        club_by_entry = {}
        for team in teams:
            club_id = ing.resolve_club_by_external_id(team.team_entry_id)
            if club_id:
                club_by_entry[team.team_entry_id] = club_id
        print(f"Clubs identificats per ID extern: {len(club_by_entry)}/{len(teams)}")

        counts = ing.ingest_calendar(
            idc=idc,
            season_competition_id=sc,
            teams=teams,
            club_by_team_entry=club_by_entry,
            season_start_year=args.season,
        )
        print(f"Partits ingestats: {counts['matches']}")
        print(f"Omesos (club sense identificar): {counts['queued']} → cua pending")

        # Classificació final de l'edició (posició i punts per equip)
        scounts = ing.ingest_classification(
            idc=idc,
            season_competition_id=sc,
            teams=teams,
            club_by_team_entry=club_by_entry,
        )
        print(f"Classificació: {scounts['resolved']}/{scounts['rows']} equips "
              f"(posició i punts actualitzats)")

        c = db.conn.cursor()
        c.execute(
            "SELECT count(*) AS n FROM pending_name_resolution "
            "WHERE status='pending' AND source_id=%s", (src,)
        )
        print(f"Pendings totals d'aquesta font: {c.fetchone()['n']}")


if __name__ == "__main__":
    main()
