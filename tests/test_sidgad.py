"""Tests del parser SIDGAD i de la ingesta, amb fixtures sintètiques.

Les fixtures imiten l'estructura observada al fragment real del
calendari rfep_cal_idc_2816_1.php (atributs semàntics al <tr>).
Quan tinguem mostres reals, es substitueixen per fixtures exactes.
"""

import pytest

from oklliga.sidgad import (
    SidgadClient,
    SidgadTeam,
    body_hash,
    parse_calendar,
    parse_classification,
    parse_teams_array,
)
from oklliga.ingest import SidgadIngest, PARSER_VERSION


class StubSidgadClient(SidgadClient):
    """Client sense xarxa: retorna fixtures locals."""

    def __init__(self):
        super().__init__(interval=0.0)

    def fetch_calendar(self, idc: int) -> str:
        return CALENDAR_HTML

    def fetch_match_sheet(self, idp: int, idm: int = 1) -> str:
        raise NotImplementedError


TEAMS_ARRAY_VALUE = (
    "calafell.png,3058,CAL,PARLEM CALAFELL;"
    "caldes.png,3036,CHC,CH CALDES RECAM LÀSER;"
    "fcbarcelona.png,3050,FCB,BARÇA;"
    "reusdep.png,3044,REUS,REUS DEPORTIU VIRGINIAS;"
    "71.png,3103,NOIA,CLUB ESPORTIU NOIA FREIXENET;"
    "78_2.png,3048,CPV,CP VOLTREGA MOVENTO STERN;"
    "vilafranca.png,3078,CPV,DIGITTECNIC CPV CAPITAL DEL VI"
)

# Cas real 2024/25: CPV és ambigu (Voltregà 3048 i Vilafranca 3078).
# La resolució correcta és per team_{id} de la classe del <tr>.


CALENDAR_HTML = """
<table>
<tr><th>JORNADA 1</th></tr>
<tr class="team_3050 team_3044" gamedate="20241012" hora="19:00" idp="28101" idc="2816">
  <td>12/10/2024</td><td>19:00</td>
  <td>BARÇA</td><td>REUS DEPORTIU VIRGINIAS</td><td>5 : 2</td>
</tr>
<tr class="team_3058 team_3103" gamedate="20241013" hora="18:30" idp="28102" idc="2816">
  <td>13/10/2024</td><td>18:30</td>
  <td>PARLEM CALAFELL</td><td>CLUB ESPORTIU NOIA FREIXENET</td><td>3:3</td>
</tr>
<tr class="team_3048 team_3078" gamedate="20241109" hora="19:00" idp="28106" idc="2816">
  <td>09/11/2024</td><td>19:00</td>
  <td>CPV</td><td>CPV</td><td>2 : 1</td>
</tr>
<tr class="team_3036" gamedate="20241014">
  <td>14/10/2024</td><td>CH CALDES RECAM LÀSER</td><td>9 - SUSPENDIDO POR COPA INTERCONTINENTAL</td>
</tr>
<tr class="fila_empty"><td>resultado 0-10 por resolución disciplinaria</td></tr>
<tr class="team_3058 team_3036" gamedate="20241102" hora="19:30" idp="28105" idc="2816">
  <td>02/11/2024</td><td>19:30</td>
  <td>PARLEM CALAFELL</td><td>CH CALDES RECAM LÀSER</td><td>4 : 2</td>
</tr>
</table>
"""


class TestParsers:
    def test_teams_array(self):
        teams = parse_teams_array(TEAMS_ARRAY_VALUE)
        assert len(teams) == 7
        barca = [t for t in teams if t.abbr == "FCB"][0]
        assert barca.team_entry_id == "3050"
        assert barca.name == "BARÇA"
        # sigles ambigües: dos equips diferents comparteixen CPV
        cpv = [t for t in teams if t.abbr == "CPV"]
        assert len(cpv) == 2
        assert {t.team_entry_id for t in cpv} == {"3048", "3078"}

    def test_calendar_rows(self):
        matches = parse_calendar(CALENDAR_HTML)
        assert len(matches) == 4
        m = matches[0]
        assert m.gamedate == "20241012"
        assert m.home_name == "BARÇA"
        assert m.away_name == "REUS DEPORTIU VIRGINIAS"
        assert m.home_goals == 5
        assert m.away_goals == 2
        assert m.idp == "28101"
        assert m.home_team_id == "3050"
        assert m.away_team_id == "3044"
        # cas CPV ambigu: IDs correctes des de les classes team_{id},
        # encara que el text visible sigui 'CPV' per als dos costats
        cpv = matches[2]
        assert cpv.home_team_id == "3048"
        assert cpv.away_team_id == "3078"
        assert cpv.home_goals == 2 and cpv.away_goals == 1
        # el quart partit involucra CH CALDES, que no té club registrat
        assert matches[3].away_name == "CH CALDES RECAM LÀSER"

    def test_body_hash_stable(self):
        assert body_hash("abc") == body_hash("abc")
        assert body_hash("abc") != body_hash("abd")


@pytest.mark.usefixtures("dsn")
class TestIngest:
    def test_full_ingest(self, dsn):
        from oklliga import OkLligaDB

        with OkLligaDB(dsn) as db:
            ing = SidgadIngest(db, client=StubSidgadClient())
            src = ing.ensure_source()
            assert src > 0

            # snapshot idempotent
            s1 = ing.store_snapshot("rfep/rfep_cal_idc_2816_1.php", CALENDAR_HTML, {"idc": "2816"})
            s2 = ing.store_snapshot("rfep/rfep_cal_idc_2816_1.php", CALENDAR_HTML, {"idc": "2816"})
            assert s1 == s2

            # clubs + external ids
            ok = db.upsert_competition(1)
            season = db.upsert_season(2024)
            sc = db.season_competition_id(ok, season, "Parlem OK Lliga")

            teams = parse_teams_array(TEAMS_ARRAY_VALUE)
            club_by_entry = {}
            for entry_id, canonic in [
                ("3050", "FC BARCELONA"),
                ("3044", "REUS DEPORTIU"),
                ("3058", "CP CALAFELL"),
                ("3103", "CE NOIA FREIXENET"),
                ("3048", "CP VOLTREGA"),
                ("3078", "CP VILAFRANCA"),
            ]:
                club_id = db.upsert_club(canonic)
                ing.upsert_external_id("club", club_id, entry_id)
                club_by_entry[entry_id] = club_id

            counts = ing.ingest_calendar(
                idc=2816,
                season_competition_id=sc,
                teams=teams,
                club_by_team_entry=club_by_entry,
                season_start_year=2024,
            )
            # 4 partits al calendari; el CH CALDES no té club → a la cua.
            # El partit CPV vs CPV s'ingereix amb els clubs CORRECTES per ID
            # tot i que les sigles són idèntiques i ambigües.
            assert counts["matches"] == 3
            assert counts["queued"] == 1

            # reingesta idempotent
            counts2 = ing.ingest_calendar(
                idc=2816,
                season_competition_id=sc,
                teams=teams,
                club_by_team_entry=club_by_entry,
                season_start_year=2024,
            )
            assert counts2["matches"] == 3

            c = db.conn.cursor()
            c.execute(
                "SELECT count(*) AS n FROM match WHERE source_id = %s",
                (src,),
            )
            assert c.fetchone()["n"] == 3
            c.execute(
                """SELECT count(*) AS n FROM pending_name_resolution
                   WHERE status='pending' AND source_id = %s""",
                (src,),
            )
            assert c.fetchone()["n"] == 1
            c.execute("SELECT count(*) AS n FROM raw_snapshot")
            assert c.fetchone()["n"] == 1
            c.execute("SELECT count(*) AS n FROM external_id WHERE entity_type='match'")
            assert c.fetchone()["n"] == 3
            # La garantia clau: el partit CPV-CPV té Voltregà com a local i
            # Vilafranca com a visitant, resolts per team_entry_id (no sigles)
            c.execute(
                """SELECT hc.canonical_name AS home, ac.canonical_name AS away
                   FROM match mt
                   JOIN team ht ON ht.team_id = mt.home_team_id
                   JOIN club hc ON hc.club_id = ht.club_id
                   JOIN team at2 ON at2.team_id = mt.away_team_id
                   JOIN club ac ON ac.club_id = at2.club_id
                   WHERE mt.matchday_date = '2024-11-09' AND mt.source_id = %s""",
                (src,),
            )
            row = c.fetchone()
            assert row["home"] == "CP VOLTREGA"
            assert row["away"] == "CP VILAFRANCA"


class TestFilials:
    """Semàntica d'equips: el club és la identitat; l'equip competeix.

    Un filial (label 'B') NO és un club nou: és un segon equip del mateix
    club, amb classificació i partits propis, en una altra competició.
    """

    def test_filial_es_equip_del_club(self, dsn):
        from oklliga import OkLligaDB

        with OkLligaDB(dsn) as db:
            club = db.upsert_club("CP VOLTREGA")
            first = db.upsert_team(club, "first")
            filial = db.upsert_team(club, "B")
            assert first != filial
            # idempotent
            assert db.upsert_team(club, "first") == first
            assert db.upsert_team(club, "B") == filial

            # El filial competeix a la mateixa temporada en altra competició
            ok = db.upsert_competition(1)
            plata = db.upsert_competition(2)
            season = db.upsert_season(2026)
            sc_ok = db.season_competition_id(ok, season, "OK Lliga")
            sc_plata = db.season_competition_id(plata, season, "OK Lliga Plata")

            c = db.conn.cursor()
            c.execute(
                """INSERT INTO participation (season_competition_id, team_id)
                   VALUES (%s, %s), (%s, %s)
                   ON CONFLICT DO NOTHING""",
                (sc_ok, first, sc_plata, filial),
            )
            # El filial juga partits propis contra equips d'altres clubs
            altre_club = db.upsert_club("CP MANLLEU")
            altre_b = db.upsert_team(altre_club, "B")
            m = db.upsert_match(
                sc_plata, filial, altre_b,
                round=1, matchday_date="2026-10-03", status="played",
                home_goals=3, away_goals=2,
            )
            assert m > 0

            # Mineria per club: el club té participacions via els dos equips
            hist = db.club_history(club)
            comps = {h["competition"] for h in hist}
            assert "OK Lliga" in comps and "OK Lliga Plata" in comps

            # La invariant d'identitat no es trenca: un sol club, dos equips
            c.execute(
                "SELECT count(DISTINCT club_id) AS n FROM team WHERE team_id IN (%s, %s)",
                (first, filial),
            )
            assert c.fetchone()["n"] == 1

    def test_label_filial_validacio(self, dsn):
        from oklliga import OkLligaDB
        import psycopg

        with OkLligaDB(dsn) as db:
            club = db.upsert_club("CP MANLLEU")
            # Labels permesos: 'first' i lletres majúscules
            db.upsert_team(club, "first")
            db.upsert_team(club, "B")
            db.upsert_team(club, "C")
            c = db.conn.cursor()
            with pytest.raises(psycopg.errors.CheckViolation):
                c.execute(
                    "INSERT INTO team (club_id, label) VALUES (%s, 'junior')",
                    (club,),
                )
            # Idempotència: reinsertar 'B' no duplica ni falla
            assert db.upsert_team(club, "B") > 0


CLASIF_HTML = """
<table class="tabla_standard tabla_clasif">
<tr><th colspan="3"></th><th>PT</th><th>PJ</th><th>PG</th><th>PE</th><th>PP</th><th>GF</th><th>GC</th><th>DIFF</th></tr>
<tr><td>1</td><td>BARÇA</td><td>FCB</td><td>71</td><td>26</td><td>23</td><td>2</td><td>1</td><td>123</td><td>44</td><td>79</td></tr>
<tr><td>2</td><td>DEPORTIVO LICEO</td><td>HCL</td><td>58</td><td>26</td><td>19</td><td>1</td><td>6</td><td>86</td><td>57</td><td>29</td></tr>
<tr><td>14</td><td>PATÍ VIC</td><td>VIC</td><td>15</td><td>26</td><td>4</td><td>3</td><td>19</td><td>50</td><td>103</td><td>-53</td></tr>
</table>
"""


class TestClassification:
    def test_parse_classification(self):
        sts = parse_classification(CLASIF_HTML)
        assert len(sts) == 3
        first = sts[0]
        assert first.position == 1
        assert first.name == "BARÇA"
        assert first.abbr == "FCB"
        assert first.points == 71
        assert first.played == 26
        assert (first.wins, first.draws, first.losses) == (23, 2, 1)
        assert (first.goals_for, first.goals_against, first.diff) == (123, 44, 79)
        last = sts[2]
        assert last.diff == -53
        # la capçalera (11 <th>) no genera fila: només 3 de vàlides
        assert [st.position for st in sts] == [1, 2, 14]

    def test_parse_classification_with_penalty(self):
        # Cas real 2025/26: CH CALDES amb columna PEN addicional.
        # La fila amb 12 valors conserva GF-GC com a diff i parseja PEN.
        html = """<table class="tabla_standard tabla_clasif">
<tr><th colspan="3"></th><th>PT</th><th>PJ</th><th>PG</th><th>PE</th><th>PP</th><th>GF</th><th>GC</th><th>DIFF</th><th>PEN</th></tr>
<tr><td>12</td><td>CH CALDES RECAM LÀSER</td><td>CHC</td><td>15</td><td>26</td><td>4</td><td>6</td><td>16</td><td>50</td><td>96</td><td>-46</td><td>-3</td></tr>
<tr><td>1</td><td>BARÇA</td><td>FCB</td><td>71</td><td>26</td><td>23</td><td>2</td><td>1</td><td>123</td><td>44</td><td>79</td><td>0</td></tr>
</table>
"""
        sts = parse_classification(html)
        assert len(sts) == 2
        caldes = sts[0]
        assert caldes.position == 12
        assert caldes.points == 15
        assert (caldes.goals_for, caldes.goals_against, caldes.diff) == (50, 96, -46)
        assert caldes.penalty == -3
        assert sts[1].penalty == 0
        assert sts[1].diff == 79


class TestParseMatchSheet:
    """Parser de fitxes de partit (rfep_gr_{idp}_1.php)."""

    SHEET = """
<div id="partido_data_ppal">
<table><tr><td>OK LIGA MASCULINA 2025/2026 - JORNADA 1<br>26/09/2025 - 21:00</td>
<td width="40%" style="text-align: center; vertical-align: baseline;">COMPLEJO DEPORTIVO LES COMES - IGUALADA<br></td></tr></table>
<table><tr>
<td><div class="nombre1">IGUALADA RIGAT HC</div></td>
<td><div class="report_faltas">9</div></td>
<td><div id="game_score"><span id="home_score">1</span>-<span id="away_score">4</span></div></td>
<td><div class="report_faltas">6</div></td>
<td><div class="nombre2">BARÇA</div></td>
</tr></table>
<table><tr><td>
<span style="font-style: italic;"><span class='lang_label lang_es'>ARBITRAJE</span>
<span class="lang_ca lang_label">ARBITRATGE</span></span><br>
RIBO NAVARRO, JOSEP ANTONI<br>
CALONGE BARRIO, DAVID
</td></tr></table>
</div>
<div id="game_report_inicidencias">
<table><tbody>
<tr>
<td><div class="game_view_indcidencias_period">P2</div>
<div class="game_view_incidencias_time">03:56</div></td>
<td class="stats_table"><div class="texto_gris_11">BAR</div></td>
<td><img src="https://www.sidgad.com/shared/portales_files/images/icon_gol.png" width="20"></td>
<td><div class="game_view_incidencias_result">1-4</div></td>
<td><div class="game_view_incidencias_dorsal">55</div></td>
<td><div class="evento_destacado"><span class='lang_label lang_es'>GOL</span></div>
<a href="#" id_player="3956" class="nombre_ficha_jugador_plus" temp_name="2025/2026" team_id="3358">
ARAGONES FERRERES <span class='texto_gris_11'>SERGI</span></a></td>
</tr>
<tr>
<td><div class="game_view_indcidencias_period">P1</div>
<div class="game_view_incidencias_time">02:01</div></td>
<td class="stats_table"><div class="texto_gris_11">IHC</div></td>
<td></td>
<td></td>
<td><div class="game_view_incidencias_dorsal">22</div></td>
<td><div class="evento_negativo"><span class='lang_label lang_es'>TARJETA AMARILLA</span></div>
<a href="#" id_player="7886" class="nombre_ficha_jugador_plus" team_id="3408">
BARS CATALÀ <span class='texto_gris_11'>ROGER</span></a></td>
</tr>
<tr>
<td><div class="game_view_indcidencias_period">P2</div>
<div class="game_view_incidencias_time">09:46</div></td>
<td class="stats_table"><div class="texto_gris_11">RIV</div></td>
<td><img src="https://www.sidgad.com/shared/portales_files/images/icon_gol.png" width="20"></td>
<td><div class="game_view_incidencias_result">4-2</div></td>
<td><div class="game_view_incidencias_dorsal">77</div></td>
<td><div class="evento_destacado"><span class='lang_label lang_es'>GOL</span>
<span class='lang_label lang_en'>GOAL</span> - FALTA DIRECTA</div>
<a href="#" id_player="0" class="nombre_ficha_jugador_plus" team_id="3300">
<span class='texto_gris_11'></span></a></td>
</tr>
</tbody></table>
</div>
<div id="div_acta">
<table class="tabla_acta_print"><tr>
<td><span class='lang_label lang_es'>Local</span><span class='lang_label lang_ca'>Local</span></td>
<td width="50%" style="font-weight: bold;">IGUALADA RIGAT HC (1)</td>
</tr></table>
<table class="tabla_acta_print"><tr>
<td align="center">1</td><td align="center">5</td><td align="center">P</td><td align="center"></td>
<td>OKM - MARTINEZ BORRAS,ARNAU</td><td></td>
</tr><tr>
<td align="center">4</td><td align="center">5</td><td align="center"></td><td align="center">C</td>
<td>OKM - PASCUAL,MATÍAS JOSÉ</td><td></td>
</tr></table>
<table class="tabla_acta_print"><tr>
<td><span class='lang_label lang_es'>Visitante</span><span class='lang_label lang_ca'>Visitant</span></td>
<td width="50%" style="font-weight: bold;">BARÇA (4)</td>
</tr></table>
<table class="tabla_acta_print"><tr>
<td align="center">3</td><td align="center">5</td><td align="center"></td><td align="center"></td>
<td>OKM - ALABART GONZALEZ,IGNACIO</td><td></td>
</tr></table>
</div>
"""

    def test_parse_sheet_header(self):
        from oklliga.sidgad import parse_match_sheet
        s = parse_match_sheet(self.SHEET)
        assert s is not None
        assert s.venue == "COMPLEJO DEPORTIVO LES COMES - IGUALADA"
        assert s.home_name == "IGUALADA RIGAT HC"
        assert s.away_name == "BARÇA"
        assert (s.home_score, s.away_score) == (1, 4)
        assert s.date_str == "26/09/2025" and s.time_str == "21:00"
        assert ("RIBO NAVARRO, JOSEP ANTONI", "main") in s.referees
        assert ("CALONGE BARRIO, DAVID", "main") in s.referees

    def test_parse_sheet_events(self):
        from oklliga.sidgad import parse_match_sheet
        s = parse_match_sheet(self.SHEET)
        assert len(s.events) == 3
        gol = s.events[0]
        assert gol.event_type == "goal"
        assert gol.id_player == "3956"
        assert gol.team_entry_id == "3358"
        assert gol.surname == "ARAGONES FERRERES"
        assert gol.given_name == "SERGI"
        assert gol.score_after == "1-4"
        assert gol.dorsal == "55"
        tgroc = s.events[1]
        assert tgroc.event_type == "yellow_card"
        assert tgroc.id_player == "7886"
        # gol de falta directa amb id_player=0: player NULL, detall preservat
        gol_fd = s.events[2]
        assert gol_fd.event_type == "goal"
        assert gol_fd.detail == "falta directa"
        assert gol_fd.id_player is None

    def test_parse_sheet_lineups(self):
        from oklliga.sidgad import parse_match_sheet
        s = parse_match_sheet(self.SHEET)
        assert len(s.home_lineup) == 2
        assert len(s.away_lineup) == 1
        por = s.home_lineup[0]
        assert por.dorsal == "1"
        assert por.license_code == "OKM"
        assert por.name == "MARTINEZ BORRAS,ARNAU"
        assert por.is_goalkeeper is True
        cap = s.home_lineup[1]
        assert cap.is_captain is True
        assert cap.is_goalkeeper is False
        assert s.away_lineup[0].name == "ALABART GONZALEZ,IGNACIO"

    def test_parse_squads(self):
        from oklliga.sidgad import parse_squads
        html = """
<a href="#" id_player="4885" class="nombre_ficha_jugador_plus" player_name = "ALABART GONZALEZ, IGNACIO" temp_name = "" team_id="3358">
<a href="#" id_player="27432" class="nombre_ficha_jugador_plus" player_name = "ÁLVAREZ	BASSET, ADRIÀ" team_id="3358">
<a href="#" id_player="4885" class="nombre_ficha_jugador_plus" player_name = "ALABART GONZALEZ, IGNACIO" team_id="3358">
"""
        sq = parse_squads(html)
        # deduplicació per (id_player, team)
        assert len(sq) == 2
        assert sq[0].id_player == "4885"
        assert sq[0].surname == "ALABART GONZALEZ"
        assert sq[0].given_name == "IGNACIO"
        # tabulador dins el nom es neteja
        assert sq[1].surname == "ÁLVAREZ BASSET"

    def test_parse_sheet_not_a_sheet(self):
        from oklliga.sidgad import parse_match_sheet
        assert parse_match_sheet("<html><body>res</body></html>") is None
