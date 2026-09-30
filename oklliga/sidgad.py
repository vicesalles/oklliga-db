"""Adaptador del portal RFEP/SIDGAD (hockeypatines.fep.es).

Arquitectura observada (vegeu informe tècnic del 30/09/2026):
  - La web pública és un shell HTML + jQuery que carrega fragments
    text/html de www.server2.sidgad.es.
  - Els fragments requereixen la capçalera Origin; sense ella el
    backend respon 200 OK amb cos buit.
  - El calendari modern és rfep_cal_idc_{idc}_1.php (POST) i retorna
    una única taula HTML amb totes les jornades, sense paginació.
  - El catàleg de competicions és rfep_ls_1.php (GET): conté el
    mapa temp_id → competicions → teams_array de cada edició.

Aquest mòdul només fa HTTP i parsing: cap dependència de la BD.
La ingesta (oklliga.ingest) connecta adaptador + connector.
"""

from __future__ import annotations

import hashlib
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

BASE_URL = "https://www.server2.sidgad.es"
PORTAL_ORIGIN = "https://www.hockeypatines.fep.es"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Origin": PORTAL_ORIGIN,
    "Referer": PORTAL_ORIGIN + "/",
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "text/html, */*; q=0.01",
}

REQUEST_INTERVAL = 1.0  # segons entre peticions; ritme conservador


class SidgadError(RuntimeError):
    """Error genèric de l'adaptador."""


class EmptyResponseError(SidgadError):
    """200 OK amb cos buit: el SIDGAD no ha rebut Origin o endpoint erroni."""


@dataclass
class SidgadTeam:
    """Entrada de teams_array: logo,sigles,nom d'una inscripció d'edició."""

    team_entry_id: str
    abbr: str
    name: str


@dataclass
class SidgadMatch:
    """Un partit del calendari, parsejat del fragment HTML.

    La identificació fiable dels equips és home_team_id/away_team_id,
    extrets de les classes team_{id} del <tr> (ordre: local, visitant).
    Les sigles i el nom visible són només informatius: NO són clau
    (CPV pot ser Voltregà, Vilafranca, Vic, Vilanova... entre edicions).
    """

    round: Optional[int]      # jornada (regular) o partit de la sèrie (play-off)
    gamedate: str          # 'YYYYMMDD'
    time: Optional[str]     # 'HH:MM' local, sense zona confirmada
    home_name: str
    away_name: str
    home_abbr: str
    away_abbr: str
    home_team_id: Optional[str]   # team_entry_id de la classe team_{id}
    away_team_id: Optional[str]
    home_goals: Optional[int]
    away_goals: Optional[int]
    idp: Optional[str]      # id de fitxa de partit
    round_label: Optional[str] = None  # 'CUARTOS', 'SEMIFINALES', 'FINAL'...
    raw_attrs: dict[str, str] = field(default_factory=dict)


class SidgadClient:
    """Client HTTP del backend SIDGAD amb control de ritme."""

    def __init__(self, base_url: str = BASE_URL, interval: float = REQUEST_INTERVAL):
        self.base_url = base_url.rstrip("/")
        self.interval = interval
        self._last_request = 0.0

    def _throttle(self) -> None:
        wait = self._last_request + self.interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def fetch(
        self,
        endpoint: str,
        params: Optional[dict[str, str]] = None,
        method: str = "POST",
        min_bytes: int = 1,
    ) -> str:
        """Fa la petició i retorna el cos.

        Llença EmptyResponseError si el cos és buit: un 200 buit del
        SIDGAD vol dir Origin perdut o endpoint inexistent, mai 'sense dades'.
        """
        url = self.base_url + "/" + endpoint.lstrip("/")
        data = None
        if method.upper() == "POST":
            data = urllib.parse.urlencode(params or {}).encode("utf-8")
        elif params:
            url += "?" + urllib.parse.urlencode(params)

        self._throttle()
        req = urllib.request.Request(url, data=data, headers=_HEADERS, method=method)
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            status = resp.status
        if status != 200:
            raise SidgadError(f"{endpoint}: HTTP {status}")
        if len(raw) < min_bytes:
            raise EmptyResponseError(
                f"{endpoint}: 200 OK amb cos buit — comprova Origin/endpoint"
            )
        for enc in ("utf-8", "cp1252", "latin-1"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")

    # ------------------------------------------------------------ catàleg

    def fetch_catalog(self) -> str:
        """Catàleg complet: competicions, temporades i teams_array."""
        return self.fetch("rfep/rfep_ls_1.php", method="GET")

    # ------------------------------------------------------------- calendari

    def fetch_calendar(self, idc: int) -> str:
        """Calendari complet d'una edició (totes les jornades, sense paginació)."""
        return self.fetch(
            f"rfep/rfep_cal_idc_{idc}_1.php",
            params={"idc": str(idc)},
            min_bytes=200,
        )

    def fetch_classification(self, idc: int) -> str:
        """Classificació de l'edició (tabla_clasif)."""
        return self.fetch(
            f"rfep/rfep_clasif_idc_{idc}_1.php",
            params={"idc": str(idc)},
            min_bytes=200,
        )

    def fetch_match_sheet(self, idp: int, idm: int = 1) -> str:
        """Fitxa de partit (acta) des del seu idp."""
        return self.fetch(
            f"rfep/rfep_gr_{idp}_{idm}.php",
            params={"idp": str(idp), "idm": str(idm)},
            min_bytes=200,
        )


# ---------------------------------------------------------------------------
# Parsers (HTML → dades). Basats en atributs semàntics, no en posició de columna
# ---------------------------------------------------------------------------

TEAMS_ARRAY_RE = re.compile(
    r'value="([^"]*)"', re.IGNORECASE
)

TEAMS_ENTRY_RE = re.compile(r"([^,;/]+),(\d+),([^,;]*),([^;]+);?")


def parse_teams_array(value: str) -> list[SidgadTeam]:
    """Parseja el valor de teams_array: 'logo,id,ABBR,NOM;logo,id,ABBR,NOM;...'"""
    teams = []
    for m in TEAMS_ENTRY_RE.finditer(value):
        logo, team_id, abbr, name = m.groups()
        teams.append(
            SidgadTeam(team_entry_id=team_id, abbr=abbr.strip(), name=name.strip())
        )
    return teams


def parse_match_row(tr_html: str) -> Optional[SidgadMatch]:
    """Extrau un partit d'una fila <tr> del calendari.

    La fila moderna inclou atributs semàntics (gamedate, idp, classes
    team_{id}). Retorna None si la fila no és un partit (capçaleres,
    files de resolució disciplinària sense enfrontament...).
    """
    # Atributs NOMÉS de l'etiqueta d'obertura del <tr>: els <td> interns
    # també tenen atributs (class="tabla_standard_less"...) que sobreescriurien
    # el class del <tr> i ens farien perdre les classes team_{id}.
    open_tag = re.search(r"<tr\b[^>]*>", tr_html, re.IGNORECASE)
    if open_tag is None or "gamedate" not in open_tag.group(0):
        return None
    attrs = dict(re.findall(r'(\w+)="([^"]*)"', open_tag.group(0)))
    # idp de la fitxa: viu a l'element fill <i class="game_report" idp="...">
    # (no a l'etiqueta <tr>), i només existeix si el partit té fitxa.
    idp_m = re.search(r'idp="(\d+)"', tr_html)
    idp = idp_m.group(1) if idp_m else None

    # IDs d'equip de les classes team_{id}: LA clau de resolució.
    # Ordre al class: primer local, després visitant.
    team_classes = re.findall(r"team_(\d+)", attrs.get("class", ""))
    home_team_id = team_classes[0] if len(team_classes) >= 1 else None
    away_team_id = team_classes[1] if len(team_classes) >= 2 else None

    def _int(v: Optional[str]) -> Optional[int]:
        if v is None:
            return None
        v = v.strip()
        if not v or not v.isdigit():
            return None
        return int(v)

    texts = [t.strip() for t in re.findall(r">([^<>]+)<", tr_html) if t.strip()]
    # Cel·la jor_in_games: 'JORNADA 5' (regular) o '1 CUARTOS' /
    # 'PARTIDO 2' (play-off). És la font autoritativa de ronda i NO
    # és un nom d'equip: cal excloure-la dels candidats a nom.
    jor_m = re.search(
        r"jor_in_games[^>]*>\s*([^<]*?)\s*<", tr_html, re.IGNORECASE
    )
    jor_text = jor_m.group(1).strip() if jor_m else ""
    round_label = None
    series_game = None
    if jor_text:
        jm = re.match(r"JORNADA\s*(\d+)", jor_text, re.IGNORECASE)
        pm = re.match(r"PARTIDO\s*(\d+)", jor_text, re.IGNORECASE)
        sm = re.match(r"(\d+)\s+(\S.*)", jor_text)
        if jm:
            rnd = int(jm.group(1))
        elif pm:
            series_game = int(pm.group(1))
        elif sm:
            series_game = int(sm.group(1))
            round_label = sm.group(2).strip().upper()
    # Descarta ràpidament files que no són partits: capçaleres de jornada,
    # avisos de suspensió sense enfrontament, notes disciplinàries...
    joined = " ".join(texts).lower()
    if not re.search(r"\d{2}/\d{2}/\d{4}|\d{1,2}:\d{2}", joined):
        if "resolución disciplinaria" in joined or "suspendido" in joined:
            return None
    # Un partit requereix exactament dos noms d'equips: textos amb lletres
    # que no són ni marcador, ni jornada, ni data/hora, ni notes d'estat.
    score_re = re.compile(r"^(\d+)\s*[:-]\s*(\d+)$")
    round_re = re.compile(r"JOR?NADA\s*(\d+)", re.IGNORECASE)
    noise_re = re.compile(
        r"suspendido|aplazado|cancelad|disciplinaria|descanso|anulad", re.IGNORECASE
    )
    name_like = []
    for t in texts:
        if t == jor_text:
            continue
        if score_re.match(t) or round_re.search(t):
            continue
        if re.fullmatch(r"\d{2}/\d{2}/\d{4}", t) or re.fullmatch(r"\d{1,2}:\d{2}", t):
            continue
        if noise_re.search(t) or re.fullmatch(r"\d+", t):
            continue
        if re.search(r"[A-Za-zÀ-ÿ]", t):
            name_like.append(t)
    if len(name_like) < 2:
        return None
    home, away = name_like[0], name_like[1]

    time_re = re.compile(r"^\d{1,2}:\d{2}$")
    hm, am = None, None
    rnd = None
    for t in texts:
        if time_re.match(t):
            continue
        m = score_re.match(t)
        if m and hm is None:
            hm, am = int(m.group(1)), int(m.group(2))
            continue
        rm = round_re.search(t)
        if rm and rnd is None:
            rnd = int(rm.group(1))
    return SidgadMatch(
        round=rnd if rnd is not None else series_game,
        round_label=round_label,
        gamedate=attrs["gamedate"],
        time=attrs.get("hora") or None,
        home_name=home,
        away_name=away,
        home_abbr=attrs.get("abbr1", ""),
        away_abbr=attrs.get("abbr2", ""),
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        home_goals=hm,
        away_goals=am,
        idp=idp or attrs.get("idp") or None,
        raw_attrs=attrs,
    )


TR_RE = re.compile(r"<tr\b[^>]*>.*?</tr>", re.DOTALL | re.IGNORECASE)


def parse_calendar(html: str) -> list[SidgadMatch]:
    """Parseja el fragment de calendari complet i retorna els partits."""
    matches = []
    for m in TR_RE.finditer(html):
        parsed = parse_match_row(m.group(0))
        if parsed is not None:
            matches.append(parsed)
    return matches


@dataclass
class SidgadStanding:
    """Una fila de la classificació: pos, nom, sigles i mètriques."""

    position: int
    name: str
    abbr: str
    points: int
    played: int
    wins: int
    draws: int
    losses: int
    goals_for: int
    goals_against: int
    diff: int
    penalty: int = 0


def parse_classification(html: str) -> list[SidgadStanding]:
    """Parseja la taula de classificació de l'edició.

    Cada fila té 11 textos: pos, nom, sigles, PT, PJ, PG, PE, PP,
    GF, GC, DIFF. El nom visible és el de l'edició (amb patrocinador)
    i cal resoldre'l contra el teams_array (no hi ha team_id a la
    classificació).
    """
    standings = []
    for m in TR_RE.finditer(html):
        texts = [t.strip() for t in re.findall(r">([^<>]+)<", m.group(0)) if t.strip()]
        # 11 valors: pos, nom, sigles, PT, PJ, PG, PE, PP, GF, GC, DIFF.
        # 12 valors: afegeix la penalització (PEN) al final; la PT ja
        # és la oficial amb la penalització aplicada (ex: CH CALDES
        # 2025/26: -46 de diferència però -3 mostrat, 15 pts).
        if len(texts) not in (11, 12):
            continue
        try:
            st = SidgadStanding(
                position=int(texts[0]),
                name=texts[1],
                abbr=texts[2],
                points=int(texts[3]),
                played=int(texts[4]),
                wins=int(texts[5]),
                draws=int(texts[6]),
                losses=int(texts[7]),
                goals_for=int(texts[8]),
                goals_against=int(texts[9]),
                diff=int(texts[10]),
                penalty=int(texts[11]) if len(texts) == 12 else 0,
            )
        except ValueError:
            continue
        standings.append(st)
    return standings


def parse_catalog_teams(html: str, idc: int) -> list[SidgadTeam]:
    """Del catàleg, extreu el teams_array d'una edició.

    ATENCIÓ: les sigles NO són úniques (ex: CPV = Voltregà i Vilafranca
    a la 2024/25). La clau estable és team_entry_id.
    """
    input_re = re.compile(
        r'<input[^>]*id="teams_array_%d"[^>]*value="([^"]*)"' % idc, re.IGNORECASE
    )
    m = input_re.search(html)
    if not m:
        return []
    return parse_teams_array(m.group(1))


def body_hash(body: str) -> str:
    """sha256 del cos, per a raw_snapshot i evitar reingestes."""
    return hashlib.sha256(body.encode("utf-8", errors="replace")).hexdigest()


# ---------------------------------------------------------------------------
# Fitxa de partit (rfep_gr_{idp}_{idm}.php): incidències + acta
# ---------------------------------------------------------------------------

@dataclass
class SheetPlayerRef:
    """Referència a jugador dins la fitxa (incidències o plantilla d'edició)."""
    id_player: Optional[str]
    team_entry_id: Optional[str]
    surname: str           # 'ALABART GONZALEZ'
    given_name: str        # 'IGNACIO'


@dataclass
class SheetEvent:
    """Una incidència de joc de la fitxa."""
    period: Optional[str]      # 'P1', 'P2', 'PR', ...
    clock: Optional[str]       # '03:56' minut del període
    team_entry_id: Optional[str]
    event_type: str            # normalitzat: 'goal', 'blue_card', ...
    detail: Optional[str]      # '- FALTA DIRECTA' en gols de falta
    dorsal: Optional[str]
    id_player: Optional[str]    # '0' o None si no identifica jugador
    surname: str
    given_name: str
    score_after: Optional[str]  # '1-4' per gols
    raw_text: str


@dataclass
class SheetLineupRow:
    """Fila de l'alineació a l'acta (sense id_player: es resol per nom)."""
    dorsal: Optional[str]
    license_code: Optional[str]  # 'OKM', 'AUT', 'OKP'...
    name: str                    # 'MARTINEZ BORRAS,ARNAU'
    is_goalkeeper: bool          # columna 'P'
    is_captain: bool             # columna 'C'


@dataclass
class SidgadMatchSheet:
    """Fitxa de partit parsejada."""
    venue: Optional[str]
    locality: Optional[str]
    date_str: Optional[str]      # '26/09/2025'
    time_str: Optional[str]      # '21:00'
    home_name: str
    away_name: str
    home_score: Optional[int]
    away_score: Optional[int]
    referees: list[tuple[str, str]]   # (nom complet, rol)
    events: list[SheetEvent]
    home_lineup: list[SheetLineupRow]
    away_lineup: list[SheetLineupRow]


def _norm_event(text: str) -> tuple[str, Optional[str]]:
    """Classifica el text d'una incidència -> (tipus normalitzat, detall).

    Tipus normalitzats (no coincideixen 1:1 amb l'enum match_event_type:
    alguns es guarden només com a metadades o s'ignoren):
      goal, blue_card, yellow_card, red_card, penalty_awarded,
      free_direct_awarded, foul, timeout, no_goal, other
    """
    t = text.upper().strip()
    if t.startswith("GOL"):
        detail = None
        if "FALTA DIRECTA" in t:
            detail = "falta directa"
        elif "PENAL" in t:
            detail = "penalti"
        return "goal", detail
    if "TARJETA AZUL" in t:
        return "blue_card", None
    if "TARJETA AMARILLA" in t:
        return "yellow_card", None
    if "TARJETA ROJA" in t or "ROJA" == t:
        return "red_card", None
    if "PENALTI" in t or "PENALTI PARA" in t:
        return "penalty_awarded", None
    if "FALTA DIRECTA PARA" in t:
        return "free_direct_awarded", None
    if t.startswith("FALTA"):
        return "foul", None
    if "NO HAY GOL" in t:
        return "no_goal", None
    if "TIEMPO MUERTO" in t:
        return "timeout", None
    return "other", text.strip()


def _strip_lang_labels(fragment: str) -> str:
    """Treu els <span class='lang_label ...'>.../</span> multillengua."""
    return re.sub(
        r"<span[^>]*class='[^']*lang_label[^']*'[^>]*>.*?</span>\s*",
        "",
        fragment,
        flags=re.DOTALL,
    )


def parse_match_sheet(html: str) -> Optional[SidgadMatchSheet]:
    """Parseja la fitxa de partit (resum + acta). None si no és una fitxa."""
    if 'id="game_report_inicidencias"' not in html and 'id="div_acta"' not in html:
        return None

    def _txt(re_pat: str, flags: int = 0) -> Optional[str]:
        m = re.search(re_pat, html, flags)
        return m.group(1).strip() if m else None

    venue = _txt(r"LUGAR DE CELEBRACI\u00d3N</span>.*?</td>\s*<td colspan=\"2\">\s*([^<]+)", re.S)
    locality = _txt(r"LOCALIDAD</span>.*?</td>\s*<td[^>]*>\s*([^<]+)", re.S)
    # Capçalera del resum (mateixa taula que el marcador)
    venue2 = _txt(r'<td width="40%" style="text-align: center[^"]*">\s*([^<]+?)\s*<br>')
    if venue is None:
        venue = venue2
    date_str = _txt(r"(\d{2}/\d{2}/\d{4})\s*-\s*\d{1,2}:\d{2}")
    time_str = _txt(r"\d{2}/\d{2}/\d{4}\s*-\s*(\d{1,2}:\d{2})")
    home = _txt(r'class="nombre1">\s*([^<]+?)\s*</div>')
    away = _txt(r'class="nombre2">\s*([^<]+?)\s*</div>')
    hs = _txt(r'id="home_score"[^>]*>\s*(\d+)')
    as_ = _txt(r'id="away_score"[^>]*>\s*(\d+)')

    # Àrbitres: bloc ARBITRAJE del resum
    referees: list[tuple[str, str]] = []
    arb = re.search(
        r"ARBITRATGE</span>\s*</span>\s*<br>\s*(.*?)</td>", html, re.S
    )
    if not arb:
        arb = re.search(r"ARBITRAJE</span>\s*<br>\s*(.*?)</td>", html, re.S)
    if arb:
        chunk = re.sub(r"<[^>]+>", "\n", arb.group(1))
        names = [n.strip() for n in chunk.split("\n")]
        names = [n for n in names if n and not n.upper().startswith("ARBITR")]
        for i, n in enumerate(names):
            referees.append((n, "main" if i < 2 else "aux"))

    # Incidències: NOMÉS la primera taula dins game_report_inicidencias
    # (després n'hi ha d'altres: golejadors, estadístiques...)
    events: list[SheetEvent] = []
    i = html.find('id="game_report_inicidencias"')
    if i >= 0:
        seg = html[i:html.find('id="div_acta"')]
        t_end = seg.find("</table>")
        inc = seg[:t_end] if t_end >= 0 else seg
        for r in re.findall(r"<tr>(.*?)</tr>", inc, re.S):
            period = re.search(
                r'game_view_indcidencias_period">\s*([^<]+)', r)
            clock = re.search(r'game_view_incidencias_time">\s*([^<]+)', r)
            tid = re.search(r'team_id="(\d+)"', r)
            dorsal = re.search(
                r'game_view_incidencias_dorsal[^>]*>\s*(\d+)', r)
            score = re.search(
                r'game_view_incidencias_result">\s*([^<]+?)\s*</div>', r)
            pl = re.search(r'id_player="(\d+)"[^>]*>(.*?)</a>', r, re.S)
            id_player = pl.group(1) if pl else None
            surname, given = "", ""
            if pl:
                nm = _strip_lang_labels(pl.group(2))
                texts = [t.strip() for t in re.split(r"<[^>]+>", nm)]
                texts = [t for t in texts if t]
                if texts:
                    surname = texts[0]
                    given = texts[1] if len(texts) > 1 else ""
            # text de l'event: tots els spans lang_es de la fila units
            # (ex: 'GOL' + ' - FALTA DIRECTA' en un segon span o fora
            # dels spans, segons la versió del fragment)
            ev_m = re.search(r"class=\"evento_\w+\"[^>]*>(.*?)</div>", r, re.S)
            ev_txt = ""
            if ev_m:
                cell = ev_m.group(1)
                # text fora dels spans (pot ser el detall o el nom d'equip)
                outside = re.sub(r"<span[^>]*>.*?</span>", " ", cell, flags=re.DOTALL)
                outside_txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", outside)).strip()
                es_texts = re.findall(r"lang_es'>([^<]+)</span>", cell)
                # uneix: parts lang_es + text exterior; el nom de l'equip
                # que apareix a l'exterior queda filtrat per _norm_event
                ev_txt = "".join(es_texts) + " " + outside_txt
            if ev_txt:
                etype, detail = _norm_event(ev_txt)
            else:
                etype, detail = "other", None
            # text brut per auditories
            raw = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", r)).strip()
            events.append(SheetEvent(
                period=period.group(1).strip() if period else None,
                clock=clock.group(1).strip() if clock else None,
                team_entry_id=tid.group(1) if tid else None,
                event_type=etype,
                detail=detail,
                dorsal=dorsal.group(1) if dorsal else None,
                id_player=id_player if id_player and id_player != "0" else None,
                surname=surname,
                given_name=given,
                score_after=score.group(1).strip() if score else None,
                raw_text=raw[:200],
            ))

    # Acta: alineacions. Capçaleres 'Local'/'Visitante' separen els blocs.
    home_lineup: list[SheetLineupRow] = []
    away_lineup: list[SheetLineupRow] = []
    i = html.find('id="div_acta"')
    if i >= 0:
        acta = html[i:]
        # Punts de tall: la capçalera d'equip és un <td> amb el nom
        # 'NOM (gols)' just després de la cel·la Local/Visitante.
        # Cerca literal per etapes: ràpida i sense backtracking.
        TEAM_HEADER_RE = re.compile(
            r"width=\"50%\"[^>]*>\s*([^<]+?)\s*\(\d+\)\s*</td>"
        )
        marks = list(TEAM_HEADER_RE.finditer(acta))

        def _parse_lineup_rows(block: str) -> list[SheetLineupRow]:
            rows: list[SheetLineupRow] = []
            for r in re.findall(r"<tr>(.*?)</tr>", block, re.S):
                cells = re.findall(r"<td[^>]*>(.*?)</td>", r, re.S)
                if len(cells) < 5:
                    continue
                flat = [re.sub(r"<[^>]+>", "", c).strip()
                        for c in cells]
                # estructura: dorsal | (5) | P | C | 'OKM - NOM' | gols | ...
                m = re.match(r"([A-Z]{2,4})\s*-\s*(.+)", flat[4])
                if not m:
                    continue
                rows.append(SheetLineupRow(
                    dorsal=flat[0] or None,
                    license_code=m.group(1),
                    name=m.group(2).strip(),
                    is_goalkeeper=flat[2].upper().startswith("P"),
                    is_captain=flat[3].upper().startswith("C"),
                ))
            return rows

        if len(marks) >= 2:
            home_lineup = _parse_lineup_rows(
                acta[marks[0].end():marks[1].start()])
            away_lineup = _parse_lineup_rows(acta[marks[1].end():])
        elif len(marks) == 1:
            home_lineup = _parse_lineup_rows(acta[marks[0].end():])

    return SidgadMatchSheet(
        venue=venue, locality=locality,
        date_str=date_str, time_str=time_str,
        home_name=home or "", away_name=away or "",
        home_score=int(hs) if hs else None,
        away_score=int(as_) if as_ else None,
        referees=referees,
        events=events,
        home_lineup=home_lineup,
        away_lineup=away_lineup,
    )


def parse_squads(html: str) -> list[SheetPlayerRef]:
    """Parseja plantilles d'una edició (stats_1_{idc}.php, tipo_stats=plantillas).

    Retorna la llista de (id_player, team_entry_id, cognom, nom) de l'edició:
    és el pont entre les alineacions de l'acta (noms) i els id_player.
    """
    out: list[SheetPlayerRef] = []
    seen: set[tuple[str, str]] = set()
    for m in re.finditer(
        r'id_player="(\d+)"[^>]*player_name\s*=\s*"([^"]*)"[^>]*team_id="(\d+)"',
        html,
    ):
        id_player, full, team_id = m.group(1), m.group(2), m.group(3)
        if (id_player, team_id) in seen:
            continue
        seen.add((id_player, team_id))
        full = full.replace("\t", " ").strip()
        if "," in full:
            surname, given = full.split(",", 1)
        else:
            surname, given = full, ""
        out.append(SheetPlayerRef(
            id_player=id_player, team_entry_id=team_id,
            surname=surname.strip(), given_name=given.strip(),
        ))
    return out
