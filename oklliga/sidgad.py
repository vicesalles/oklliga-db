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

    round: Optional[int]
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
        round=rnd if rnd is not None else _int(attrs.get("jornada")),
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
        idp=attrs.get("idp") or None,
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
