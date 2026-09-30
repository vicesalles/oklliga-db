"""Ingesta de fitxes de partit i plantilles d'una temporada de l'OK Lliga.

Flux per temporada:
  1. Catàleg -> fases OK Lliga -> per cada fase (idc): teams_array
     (mapa team_entry_id -> team_id intern i nom visible -> team_entry_id).
  2. Plantilles de cada edició (stats_1_{idc}.php): jugadors + squad_member.
  3. Per cada partit de la BD amb idp (external_id 'match' o source_ref):
     fitxa -> pavelló, àrbitres, alineacions i esdeveniments.

Ús:
    python scripts/ingest_sheets.py --season 2025
    python scripts/ingest_sheets.py --season 2025 --limit 3   # prova

Idempotent: reexecutar no duplica (upserts + ON CONFLICT).
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from oklliga import OkLligaDB
from oklliga.config import get_dsn
from oklliga.sheets import SheetIngest
from oklliga.sidgad import (
    SidgadClient,
    parse_catalog_teams,
)

MIN_SEASON = 2021

KNOWN_RFEP_TEMP = {
    2026: 41,
    2025: 39,
    2024: 37,
    2023: 35,
    2022: 33,
    2021: 31,
}


def classify_phase(name: str) -> str:
    upper = name.upper()
    if "PLAY" not in upper:
        return "regular"
    if "9 Y 10" in upper or "9Y10" in upper.replace(" ", ""):
        return "playoff910"
    if "OUT" in upper:
        return "playout"
    return "playoff"


def find_okliga_phases(catalog_html: str, rfep_temp: int) -> list[tuple[int, str]]:
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
        raise SystemExit(f"No hi ha fases OK Lliga per temp_{rfep_temp}")
    return phases


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--season", type=int, required=True)
    ap.add_argument("--dsn", default=None)
    ap.add_argument("--limit", type=int, default=None,
                    help="màxim de fitxes a ingerir (proves)")
    args = ap.parse_args()

    if args.season < MIN_SEASON:
        raise SystemExit(f"Temporada anterior al límit editorial ({MIN_SEASON})")
    if args.season not in KNOWN_RFEP_TEMP:
        raise SystemExit("Temporada sense rfep_season_id conegut")

    rfep_temp = KNOWN_RFEP_TEMP[args.season]
    dsn = args.dsn or get_dsn()
    client = SidgadClient()

    catalog = client.fetch_catalog()
    phases = find_okliga_phases(catalog, rfep_temp)
    print(f"Temporada {args.season}/{(args.season + 1) % 100:02d}: "
          f"{len(phases)} fases OK Lliga")

    with OkLligaDB(dsn) as db:
        ing = SheetIngest(db, client=client)
        src = ing.ensure_source()
        c = db.conn.cursor()

        c.execute(
            "SELECT internal_id FROM external_id WHERE source_id=%s"
            " AND entity_type='season' AND external_id=%s",
            (src, str(rfep_temp)),
        )
        row = c.fetchone()
        if not row:
            raise SystemExit(
                "Temporada no registrada; executa primer ingest_season.py")
        season_id = row["internal_id"]

        ingested = 0
        for idc, comp_name in phases:
            print(f"\n=== {comp_name} (idc={idc}) ===")
            teams = parse_catalog_teams(catalog, idc)
            if not teams:
                print("  Sense teams_array; s'omet")
                continue

            c.execute(
                "SELECT internal_id FROM external_id WHERE source_id=%s"
                " AND entity_type='competition_edition' AND external_id=%s",
                (src, str(idc)),
            )
            row = c.fetchone()
            if not row:
                print("  Edició no registrada (ingest_season.py pend); s'omet")
                continue
            sc_id = row["internal_id"]

            # team_entry -> club (external_id 'club') -> team 'first'
            team_by_entry: dict[str, int] = {}
            entry_by_name: dict[str, str] = {}
            for t in teams:
                c.execute(
                    """
                    SELECT internal_id FROM external_id
                    WHERE source_id=%s AND entity_type='club'
                      AND external_id=%s
                    """,
                    (src, t.team_entry_id),
                )
                r2 = c.fetchone()
                if r2:
                    tid = db.team_id_for_club(r2["internal_id"])
                    if tid:
                        team_by_entry[t.team_entry_id] = tid
                entry_by_name[t.name.strip()] = t.team_entry_id

            n_squad = ing.ingest_squads(idc)
            print(f"  Plantilla: {n_squad} jugadors registrats")

            # Partits d'aquesta edició amb idp conegut
            c.execute(
                """
                SELECT m.match_id, m.source_ref
                FROM match m
                WHERE m.season_competition_id = %s
                  AND m.source_ref IS NOT NULL
                ORDER BY m.matchday_date NULLS LAST, m.match_id
                """,
                (sc_id,),
            )
            matches = c.fetchall()
            done = 0
            for mr in matches:
                if args.limit and ingested >= args.limit:
                    print(f"  Límit de {args.limit} fitxes assolit")
                    return
                ok = ing.ingest_match_sheet(
                    mr["match_id"], mr["source_ref"],
                    team_by_entry, entry_by_name,
                )
                if ok:
                    done += 1
                    ingested += 1
                    print(f"  Fitxa {mr['source_ref']}: OK")
                else:
                    print(f"  Fitxa {mr['source_ref']}: sense dades")
            print(f"  Fitxes: {done}/{len(matches)}")

        print(f"\nTotal: {ingested} fitxes ingestades")


if __name__ == "__main__":
    main()
