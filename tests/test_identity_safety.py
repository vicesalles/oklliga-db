"""Tests de les defenses contra errors d'identitat de clubs.

Cobreix: constraints d'esquema (anti-solapament, normalització),
cua de resolució assistida i fusió auditada de clubs.
"""

import pytest

from oklliga import OkLligaDB
from oklliga.audit import ClubAuditor
from oklliga.resolution import NameResolver, normalize_name




def test_normalize_name():
    assert normalize_name("Reus  Deportiu") == "reus deportiu"
    assert normalize_name("Hormipresa Igualada HC!") == "hormipresa igualada hc"
    assert normalize_name("IGUALADA RIGAT") == "igualada rigat"


def test_bd_impedeix_noms_solapats(dsn):
    """La BD no deixa escriure dos noms vigents alhora per un club.

    Un nom sense valid_until és vigent indefinidament: solapa amb qualsevol
    nom posterior. Per convenció, el nom vigent es tanca (valid_until) quan
    se'n registra un de nou.
    """
    with OkLligaDB(dsn) as db:
        club = db.upsert_club("Club Test Solap")
        db.add_club_name(club, "Nom A", valid_from="2000-01-01",
                         valid_until="2002-12-31")
        # Solapament amb el rang de Nom A: prohibit
        with pytest.raises(Exception):
            db.add_club_name(club, "Nom B", valid_from="2001-01-01")
        # Rang posterior, no solapat: permès i queda vigent
        db.add_club_name(club, "Nom B", valid_from="2003-01-01")


def test_bd_impedeix_nom_duplicat_entre_clubs(dsn):
    """El mateix nom no pot pertànyer a dos clubs en rangs solapats."""
    with OkLligaDB(dsn) as db:
        c1 = db.upsert_club("Club Unic A")
        c2 = db.upsert_club("Club Unic B")
        db.add_club_name(c1, "Nom Compartit", valid_from="2000-01-01",
                         valid_until="2010-01-01")
        with pytest.raises(Exception):
            db.add_club_name(c2, "Nom Compartit", valid_from="2001-01-01")
        # Rangs coneguts no solapats: permès (el club va desaparèixer i el
        # nom va passar a una altra identitat)
        db.add_club_name(c2, "Nom Compartit", valid_from="2050-01-01",
                         valid_until="2060-01-01")


def test_resolver_cua_i_decisions(dsn):
    """Flux complet: nom no resolt → cua → decisió humana → àlies persistent."""
    with OkLligaDB(dsn) as db:
        igualada = db.upsert_club("Igualada Rigat HC", city="Igualada")
        db.add_club_name(igualada, "Hormipresa Igualada HC",
                         valid_from="2000-07-01", valid_until="2003-06-30")

    with NameResolver(dsn) as res:
        # Un nom nou vist per l'scraper no resol
        pid = res.enqueue("Igualada Rigat HC 2002", context={"season": "2002/03"})
        assert res.pending()[0]["pending_id"] == pid

        # Proposa candidats per similaritat (sense crear res)
        cands = res.propose_candidates("Igualada Rigat HC 2002")
        assert any(c["club_id"] == igualada for c in cands)

        # Decisió humana: és l'Igualada
        res.resolve_as_existing(pid, igualada, resolved_by="tester")
        assert res.pending() == []

    # L'àlies queda persistit i ara el connector resol automàticament
    with OkLligaDB(dsn) as db:
        assert db.resolve_club("Igualada Rigat HC 2002") == igualada


def test_resolver_crea_club_nou(dsn):
    with NameResolver(dsn) as res:
        pid = res.enqueue("CP Manresa", context={"season": "2025/26"})
        club_id = res.resolve_as_new_club(pid, "CP Manresa", city="Manresa")
        assert club_id > 0
    with OkLligaDB(dsn) as db:
        assert db.resolve_club("CP Manresa") == club_id


def test_merge_clubs_audia_i_corrigeix(dsn):
    """Error humà detectat: club duplicat es fusiona de forma auditada.

    Un partit legítim del duplicat contra un tercer club es reassigna al
    club correcte; un partit 'duplicat contra duplicat' (simulacre erroni)
    queda eliminat en lloc de convertir-se en un club contra si mateix.
    """
    with OkLligaDB(dsn) as db:
        bo = db.upsert_club("Igualada Rigat HC Fusion")
        dup = db.upsert_club("Igualada Rigat HC Fusion2")
        tercer = db.upsert_club("Reus Merge Test")
        db.add_club_name(dup, "Igualada Fusion Dup")

        comp = db.upsert_competition(1)
        season = db.upsert_season(2025)
        sc = db.season_competition_id(comp, season, "OK Lliga")
        t_dup = db.upsert_team(dup, "first")
        t_tercer = db.upsert_team(tercer, "first")
        t_bo = db.upsert_team(bo, "first")
        m_legitim = db.upsert_match(sc, t_dup, t_tercer, round=1,
                                    home_goals=2, away_goals=2)
        m_simulacre = db.upsert_match(sc, t_dup, t_bo, round=2,
                                      home_goals=1, away_goals=1)

    with ClubAuditor(dsn) as auditor:
        auditor.merge_clubs(dup, bo, merged_by="tester", reason="duplicat per error")

        row = auditor.conn.cursor()
        # El partit legítim queda reassignat al club correcte
        row.execute(
            """
            SELECT hc.club_id AS home_club_id, ac.club_id AS away_club_id
            FROM match m
            JOIN team ht ON ht.team_id = m.home_team_id
            JOIN club hc ON hc.club_id = ht.club_id
            JOIN team at ON at.team_id = m.away_team_id
            JOIN club ac ON ac.club_id = at.club_id
            WHERE m.match_id = %s
            """,
            (m_legitim,),
        )
        match_row = row.fetchone()
        assert match_row["home_club_id"] == bo
        assert match_row["away_club_id"] == tercer

        # El simulacre (dup contra bo, que ara seria bo contra bo) s'elimina
        row.execute(
            "SELECT count(*) AS n FROM match WHERE match_id = %s", (m_simulacre,)
        )
        assert row.fetchone()["n"] == 0

        # La fusió consta al log
        log = auditor.merge_log()
        assert any(
            l["merged_club_id"] == dup and l["kept_club_id"] == bo for l in log
        )

        # El club fusionat ja no existeix
        row.execute("SELECT count(*) AS n FROM club WHERE club_id = %s", (dup,))
        assert row.fetchone()["n"] == 0


def test_auditoria_detecta_clubs_sense_partits(dsn):
    with OkLligaDB(dsn) as db:
        fantasma = db.upsert_club("Club Fantasma Auditoria")
    with ClubAuditor(dsn) as auditor:
        suspects = auditor.detect_suspect_clubs()
        trobats = {
            r["club_id"] for rows in suspects.values() for r in rows
        }
        assert fantasma in trobats
