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
                   JOIN club hc ON hc.club_id = mt.home_club_id
                   JOIN club ac ON ac.club_id = mt.away_club_id
                   WHERE mt.matchday_date = '2024-11-09' AND mt.source_id = %s""",
                (src,),
            )
            row = c.fetchone()
            assert row["home"] == "CP VOLTREGA"
            assert row["away"] == "CP VILAFRANCA"
