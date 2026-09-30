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

# Límit editorial: la temporada d'inici és la 2021/22. No s'ingereix
# res anterior per a cap competició (decisió documentada al README).
MIN_SEASON = 2021

# Edicions (idc) de la fase regular masculina, segons l'informe tècnic.
# El catàleg les torna dinàmicament, però això serveix de mapa de control.
KNOWN_EDITIONS = {
    2026: 3568,
    2025: 3150,
    2024: 2816,
    2023: 2477,
    2022: 2092,
    2021: 1751,
}

# rfep_season_id (opac, NO és l'any): segons el catàleg
KNOWN_RFEP_TEMP = {
    2026: 41,
    2025: 39,
    2024: 37,
    2023: 35,
    2022: 33,
    2021: 31,
}


def classify_phase(name: str) -> str:
    """Classifica una fase de l'OK Lliga segons el seu nom al catàleg."""
    upper = name.upper()
    if "PLAY" not in upper:
        return "regular"
    if "9 Y 10" in upper or "9Y10" in upper.replace(" ", ""):
        return "playoff910"
    if "OUT" in upper:
        return "playout"
    return "playoff"


def find_okliga_phases(
    catalog_html: str, rfep_temp: int
) -> list[tuple[int, str]]:
    """Torna totes les fases (idc, nom) de l'OK Lliga masculina d'una temporada.

    Inclou la fase regular i les fases post-temporada (play-off,
    play-off 9-10, play-out). Exclou Plata/Bronze/Iberdrola.
    """
    import re

    pattern = re.compile(r'<a[^>]*temp_%d[^>]*>' % rfep_temp)
    phases = []
    for m in pattern.finditer(catalog_html):
        tag = m.group(0)
        nm = re.search(r'idc_name="([^"]*)"', tag)
        idm = re.search(r'\bid="(\d+)"', tag)
        if not nm or not idm:
            continue
        name = nm.group(1)
        upper = name.upper()
        if ("OK LIGA" in upper and "PLATA" not in upper
                and "BRONCE" not in upper and "IBERDROLA" not in upper):
            phases.append((int(idm.group(1)), name))
    if not phases:
        raise SystemExit(
            f"No s'ha trobat cap fase de l'OK Lliga per temp_{rfep_temp}"
        )
    return phases


def find_edition(catalog_html: str, rfep_temp: int) -> tuple[int, str]:
    """Troba l'idc de la fase regular masculina d'una temporada del catàleg."""
    phases = find_okliga_phases(catalog_html, rfep_temp)
    regular = [p for p in phases if "PLAY" not in p[1].upper()]
    if not regular:
        raise SystemExit(
            f"No s'ha trobat la fase regular de l'OK Lliga per temp_{rfep_temp}"
        )
    return regular[0]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--season", type=int, required=True,
                    help="any d'inici de la temporada (2024 = 2024/25)")
    ap.add_argument("--phase", default="regular",
                    choices=["regular", "playoffs", "all"],
                    help="fases a ingerir: regular (per defecte), playoffs "
                         "(totes les fases PLAY*) o all")
    ap.add_argument("--dsn", default=None, help="DSN Postgres (per defecte .env)")
    ap.add_argument("--dry-run", action="store_true",
                    help="només analitza i mostra, no escriu a la BD")
    args = ap.parse_args()

    if args.season < MIN_SEASON:
        raise SystemExit(
            f"Temporada {args.season} anterior al l\u00edmit editorial ({MIN_SEASON}); "
            "veure 'Abast' al README"
        )
    if args.season not in KNOWN_RFEP_TEMP:
        raise SystemExit(
            f"Temporada {args.season} sense rfep_season_id conegut; "
            "consulta el cat\u00e0leg i amplia KNOWN_RFEP_TEMP"
        )
    rfep_temp = KNOWN_RFEP_TEMP[args.season]

    dsn = args.dsn or get_dsn()
    client = SidgadClient()

    print(f"Catàleg SIDGAD (temp_{rfep_temp} = temporada {args.season}/{(args.season+1)%100:02d})...")
    catalog = client.fetch_catalog()
    phases = find_okliga_phases(catalog, rfep_temp)
    print("Fases OK Lliga trobades al catàleg:")
    for pidc, pname in phases:
        print(f"  idc={pidc:5d}  {pname}")
    if args.phase == "regular":
        selected = [p for p in phases if classify_phase(p[1]) == "regular"]
    elif args.phase == "playoffs":
        selected = [p for p in phases if classify_phase(p[1]) != "regular"]
        if not selected:
            raise SystemExit("Aquesta temporada no té fases de play-off")
    else:
        selected = phases
    if not selected:
        raise SystemExit(f"Cap fase coincideix amb --phase {args.phase}")

    if args.dry_run:
        from oklliga.sidgad import parse_calendar
        for pidc, pname in selected:
            teams = parse_catalog_teams(catalog, pidc)
            ms = parse_calendar(client.fetch_calendar(pidc))
            print(f"\n{pname} (idc={pidc}): {len(teams)} equips, {len(ms)} partits")
            for t in sorted(teams, key=lambda x: x.name):
                print(f"  {t.abbr:6s} {t.team_entry_id:6s} {t.name}")
        return

    with OkLligaDB(dsn) as db:
        ing = SidgadIngest(db, client=client)
        src = ing.ensure_source()

        # Temporada i competició al nostre model
        ok = db.upsert_competition(1, notes="Màxima categoria estatal")
        season = db.upsert_season(args.season)
        ing.upsert_external_id("season", season, str(rfep_temp))

        for idc, comp_name in selected:
            stage = classify_phase(comp_name)
            is_playoff = stage != "regular"
            print(f"\n=== {comp_name} (idc={idc}, stage={stage}) ===")
            sc = db.season_competition_id(
                ok, season, comp_name,
                fmt="play-off" if is_playoff else "lliga regular a doble volta",
                stage=stage,
            )
            ing.upsert_external_id("competition_edition", sc, str(idc))

            teams = parse_catalog_teams(catalog, idc)
            if not teams:
                raise SystemExit(
                    f"teams_array no trobat al catàleg per a la idc {idc}"
                )

            # Vincle team_entry → club per external_id registrat; els clubs
            # que no tinguin ID extern queden per resoldre per nom (cua).
            club_by_entry = {}
            for team in teams:
                club_id = ing.resolve_club_by_external_id(team.team_entry_id)
                if club_id:
                    club_by_entry[team.team_entry_id] = club_id
            print(f"Equips a l'edició: {len(teams)}; identificats per ID extern: "
                  f"{len(club_by_entry)}/{len(teams)}")

            counts = ing.ingest_calendar(
                idc=idc,
                season_competition_id=sc,
                teams=teams,
                club_by_team_entry=club_by_entry,
                season_start_year=args.season,
                stage=stage,
            )
            print(f"Partits ingestats: {counts['matches']}")
            print(f"Omesos (club sense identificar): {counts['queued']} → cua pending")

            if not is_playoff:
                # Classificació final de l'edició (només fase regular)
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
        print(f"\nPendings totals d'aquesta font: {c.fetchone()['n']}")


if __name__ == "__main__":
    main()
