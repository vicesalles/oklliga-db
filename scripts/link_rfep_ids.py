"""Enllaça els clubs de la BD amb els team_entry_id de la RFEP/SIDGAD.

Abans de la primera ingesta d'una temporada cal registrar a external_id
la correspondència entre cada club canònic i el seu team_entry_id de
l'edició (l'ID de les classes team_{id} del calendari). Amb aquest
enllaç, la resolució d'equips és 100% per ID i les sigles queden
irrellevants (CPV = Voltregà? Vilafranca? no importa).

Semiautomàtic i auditat:
  1. Descarrega el teams_array de l'edició (idc) de la temporada.
  2. Proposa coincidències per nom exacte i per similitud contra els
     clubs de la BD (canonical_name + club_name).
  3. Mostra la taula i demana confirmació interactiva:
       - [ENTER] accepta totes les proposicions de confiança alta
       - 'y' accepta una per una
       - qualsevol altra cosa: no escriu res d'aquella fila
  4. Registra external_id (source rfep, entity_type 'club') i el nom
     visible de l'edició com a club_name històric de la temporada.

Ús:
    python scripts/link_rfep_ids.py --season 2024
    python scripts/link_rfep_ids.py --season 2024 --yes   # no interactiu
"""

import argparse
import difflib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from oklliga import OkLligaDB
from oklliga.config import get_dsn
from oklliga.ingest import SidgadIngest
from oklliga.sidgad import SidgadClient, parse_catalog_teams, parse_teams_array

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ingest_season import KNOWN_RFEP_TEMP, find_edition

from datetime import date


def season_range(start_year: int) -> tuple[date, date]:
    return (date(start_year, 7, 1), date(start_year + 1, 6, 30))


def normalize(s: str) -> str:
    import re
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]", "", s)
    return re.sub(r"\s+", " ", s).strip()


# Paraules genèriques del món del club que NO poden generar coincidència
# per si soles ('CLUB', 'HOQUEI', 'PATÍ', 'DEPORTIU'...). Una proposta per
# token compartit exigeix almenys un token NO genèric en comú.
GENERIC_WORDS = {
    "club", "hockey", "hoquei", "pati", "cp", "hc", "ch", "ce", "deportiu",
    "deportivo", "esportiu", "esportivo", "atletic", "atletico", "femeni",
    "basquet", "patins",
}


# Àlies coneguts que la similitut no pot resoldre (sigla -> canònic)
ALIAS_HINTS = {
    "bara": "FC BARCELONA",        # BARÇA (la ç desapareix en normalitzar)
    "barca": "FC BARCELONA",
}


def propose_matches(
    teams: list, club_names: dict[int, list[str]]
) -> dict[str, tuple[int, str, float]]:
    """team_entry_id -> (club_id, canonical, score). Score 1.0 = nom exacte."""
    # Índex normalitzat: nom -> club_id (canònics i històrics)
    index: dict[str, int] = {}
    for club_id, names in club_names.items():
        for n in names:
            index.setdefault(normalize(n), club_id)

    proposals: dict[str, tuple[int, str, float]] = {}
    used_clubs: set[int] = set()
    # Primera passada: exactes (sense repetir club)
    for t in teams:
        key = normalize(t.name)
        if key in index and index[key] not in used_clubs:
            proposals[t.team_entry_id] = (index[key], key, 1.0)
            used_clubs.add(index[key])
    # Segona passada: àlies coneguts i similitud per als que queden
    all_norms = list(index.keys())
    for t in teams:
        if t.team_entry_id in proposals:
            continue
        key = normalize(t.name)
        hint = ALIAS_HINTS.get(key)
        if hint and normalize(hint) in index:
            proposals[t.team_entry_id] = (index[normalize(hint)], normalize(hint), 0.99)
            used_clubs.add(index[normalize(hint)])
            continue
        # Coincidència de tokens: tots els tokens significatius del nom
        # de l'edició apareixen al nom del club ('DEPORTIVO LICEO' dins
        # 'HOCKEY CLUB LICEO' -> 'LICEO'; 'SANT JUST' dins 'HC SANT JUST').
        # Els tokens genèrics (club, hockey...) no compten.
        toks = [w for w in key.split() if len(w) > 2 and w not in GENERIC_WORDS]
        if toks:
            for norm, cid in index.items():
                if cid in used_clubs:
                    continue
                if all(w in norm.split() for w in toks):
                    proposals[t.team_entry_id] = (cid, norm, 0.90)
                    used_clubs.add(cid)
                    break
            if t.team_entry_id in proposals:
                continue
        # Token distintiu compartit ('LICEO', 'JUST'): proposem amb
        # confiança mitjana; l'humà ho confirma (amb --yes no s'accepta)
        toks2 = [w for w in key.split()
                 if len(w) >= 4 and w not in GENERIC_WORDS]
        if toks2:
            for norm, cid in index.items():
                if cid in used_clubs:
                    continue
                shared = [w for w in toks2 if w in norm.split()]
                if shared:
                    proposals[t.team_entry_id] = (cid, norm, 0.75)
                    break
            if t.team_entry_id in proposals:
                continue
        matches = difflib.get_close_matches(key, all_norms, n=3, cutoff=0.6)
        if matches:
            best = matches[0]
            score = difflib.SequenceMatcher(None, key, best).ratio()
            if index[best] not in used_clubs or score > 0.85:
                proposals[t.team_entry_id] = (index[best], best, round(score, 2))
                if score > 0.85:
                    used_clubs.add(index[best])
    return proposals


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--season", type=int, required=True,
                    help="any d'inici (2024 = 2024/25)")
    ap.add_argument("--dsn", default=None)
    ap.add_argument("--yes", action="store_true",
                    help="accepta totes les proposicions d'alta confiança sense preguntar")
    args = ap.parse_args()

    if args.season not in KNOWN_RFEP_TEMP:
        raise SystemExit(f"Temporada {args.season} no és al mapa KNOWN_RFEP_TEMP")

    rfep_temp = KNOWN_RFEP_TEMP[args.season]
    dsn = args.dsn or get_dsn()
    client = SidgadClient()

    print(f"Catàleg SIDGAD (temp_{rfep_temp} = {args.season}/{(args.season+1)%100:02d})...")
    catalog = client.fetch_catalog()
    idc, comp_name = find_edition(catalog, rfep_temp)
    print(f"Edició: idc={idc} ({comp_name})")
    teams = parse_catalog_teams(catalog, idc)
    print(f"Equips a l'edició: {len(teams)}")

    with OkLligaDB(dsn) as db:
        ing = SidgadIngest(db, client=client)
        src = ing.ensure_source()

        c = db.conn.cursor()
        c.execute("""
            SELECT c.club_id, c.canonical_name,
                   array_agg(DISTINCT cn.name) AS names
            FROM club c
            LEFT JOIN club_name cn ON cn.club_id = c.club_id
            GROUP BY c.club_id, c.canonical_name
        """)
        club_names = {r["club_id"]: [r["canonical_name"], *(r["names"] or [])]
                      for r in c.fetchall()}
        canonical_by_id = {r: v[0] for r, v in club_names.items()}

        # Ja enllaçats?
        c.execute("""
            SELECT external_id, internal_id FROM external_id
            WHERE source_id = %s AND entity_type = 'club'
        """, (src,))
        already = {r["external_id"] for r in c.fetchall()}
        linked_clubs = {r["internal_id"] for r in c.fetchall()}

        proposals = {
            eid: p for eid, p in propose_matches(teams, club_names).items()
            if p[0] not in linked_clubs
        }

        print("\nProposta d'enllaç (team_entry_id -> club):")
        accepted: list[tuple[str, int]] = []
        for t in sorted(teams, key=lambda x: x.name):
            linked = t.team_entry_id in already
            p = proposals.get(t.team_entry_id)
            tag = "JA ENLLAÇAT" if linked else ""
            if p:
                club_id, matched_name, score = p
                canon = canonical_by_id[club_id]
                mark = " exacte " if score == 1.0 else f" sim={score}"
                print(f"  {t.team_entry_id:6s} {t.abbr:5s} {t.name:42s}"
                      f" -> {canon:28s}{mark} {tag}")
                if not linked and (args.yes or score == 1.0):
                    accepted.append((t.team_entry_id, club_id))
                elif not linked:
                    ans = input("    accepta? [y/N] ").strip().lower()
                    if ans == "y":
                        accepted.append((t.team_entry_id, club_id))
            else:
                print(f"  {t.team_entry_id:6s} {t.abbr:5s} {t.name:42s} -> ??? {tag}")

        vf, vu = season_range(args.season)
        with db.transaction():
            for entry_id, club_id in accepted:
                ing.upsert_external_id("club", club_id, entry_id)
                # Nom visible de l'edició com a nom històric del club
                team = next(t for t in teams if t.team_entry_id == entry_id)
                try:
                    db.add_club_name(club_id, team.name,
                                     valid_from=vf, valid_until=vu,
                                     is_sponsor_name=True)
                except Exception:
                    # Ja registrat pel seed amb altra grafia/vigència: no crític
                    pass
            # Tanca els pendents de la cua que aquesta edició resol:
            # el nom cru que la ingesta va enfilar coincideix amb el nom
            # visible d'un equip de l'edició enllaçat (nou o ja existent).
            c2 = db.conn.cursor()
            c2.execute(
                """
                SELECT external_id, internal_id FROM external_id
                WHERE source_id = %(s)s AND entity_type = 'club'
                  AND external_id = ANY(%(entries)s)
                """,
                {"s": src, "entries": [t.team_entry_id for t in teams]},
            )
            club_by_entry = {r["external_id"]: r["internal_id"]
                             for r in c2.fetchall()}
            norm_by_entry = {t.team_entry_id: normalize(t.name)
                             for t in teams
                             if t.team_entry_id in club_by_entry}
            c = db.conn.cursor()
            c.execute(
                "SELECT pending_id, raw_name FROM pending_name_resolution "
                "WHERE status = 'pending'"
            )
            closed = 0
            for row in c.fetchall():
                entry = next((e for e, n in norm_by_entry.items()
                              if n == normalize(row["raw_name"])), None)
                if entry is None:
                    continue
                c.execute(
                    """
                    UPDATE pending_name_resolution
                    SET status = 'resolved',
                        resolved_club_id = %(club)s,
                        resolved_by = 'link_rfep_ids (external_id)',
                        resolved_at = now()
                    WHERE pending_id = %(pid)s
                    """,
                    {"club": club_by_entry[entry], "pid": row["pending_id"]},
                )
                closed += c.rowcount
        print(f"Fet. {len(accepted)} enllaços escrits; {closed} pendents tancats.")
        print("Ara ingest_season.py resoldrà tots els equips per ID.")


if __name__ == "__main__":
    main()
