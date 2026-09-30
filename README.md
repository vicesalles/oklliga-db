# OK Lliga — Base de dades per a mineria de dades

Model de dades PostgreSQL per emmagatzemar resultats i estadístiques de tots els
partits de l'OK Lliga des de la temporada 2021/22 (vegeu «Abast» a continuació),
noms de la competició), pensat per a futures operacions de mineria de dades.

## Abast: amnèsia deliberada

La temporada d'inici de la base de dades és la **2021/22**. No s'ingereix ni
s'accepta cap dada anterior, per a cap competició. És una decisió editorial
assumida i deliberada: s'aprèn la pèrdua d'històric a canvi de concentrar
l'esforç en créixer endavant (temporades futures) i en amplada (més
competicions, categories i dimensions de dada dins de l'abast). El límit és
operatiu, no només documental: `ingest_season.py` defineix `MIN_SEASON = 2021`
i rebutja amb error explícit qualsevol temporada anterior.

## Les tres dificultats i com les resol el model

| Dificultat | Solució al model |
|---|---|
| **Dades escasses en el passat** | Camps opcionals (`matchday_date`, gols per temps...), `source_id` + `source_url` per traçabilitat, i `confidence` (`unknown/low/medium/high`) a cada fila per ponderar les dades a la mineria. Les restriccions només exigeixen marcador si l'estat és `played`. |
| **Equips que canvien de nom per sponsors** | `club` és l'entitat persistent; `club_name` guarda l'historial de noms amb `valid_from`/`valid_until` i flag `is_sponsor_name`. Hormipresa Igualada HC i Igualada Rigat HC són dues files de `club_name` amb el mateix `club_id`. La vista `v_match_with_names` resol automàticament el nom vigent en la data de cada partit. |
| **La competició canvia de nom** | `competition` és l'entitat persistent, **una per categoria** (OK Lliga i OK Lliga Plata són competicions diferents, mai noms d'una mateixa); `competition_name` guarda els noms per rang de temporades (Divisió d'Honor → OK Lliga; Primera Divisió → OK Lliga Plata) i `season_competition` fixa el nom usat a cada temporada concreta. |

## Model (resum)

```
club ──< club_name                    (entitat + historial de noms)
competition ──< competition_name      (entitat + historial de noms)
competition ──> season_competition <── season
season_competition ──< participation >── club
season_competition ──< match >── club (local/visitant)
match ──< match_stat          (JSONB extensibles, àmbit partit)
match ──< team_match_stat    (JSONB extensibles, àmbit equip-partit)

player ──< player_name               (entitat + historial de noms)
player ──< squad_membership >── club  (trajectòria: fitxatges amb dates)
player ──< match_player >── match    (alineació: titularitat, minuts)
match ──< match_event >── player     (gols, targetes blaves/vermelles, lesions...)
```

## Robustesa de la identitat de club (invariant crítica)

La invariant "una identitat, noms variables" es defensa en quatre capes:

1. **La BD impedeix la inconsistència** (`sql/01_schema.sql`):
   - `club_name.name_normalized` (columna generada): normalització determinista
     (minúscules, sense accents/puntuació, espais col·lapsats).
   - `club_name_no_overlap` (EXCLUDE GiST): un club no pot tenir dos noms amb
     vigència coneguda solapada. Dates NULL = "desconegut", no "infinit".
   - `club_name_unique_norm` (EXCLUDE GiST): un mateix nom normalitzat no pot
     pertànyer a dos clubs en rangs solapats (rangs desconeguts compten com a
     infinit: el cas ambigu queda prohibit).
2. **Resolució assistida, mai a cegues** (`oklliga/resolution.py`,
   `NameResolver`): `resolve_club` retorna `None` → el nom va a
   `pending_name_resolution` amb context i candidats proposats per
   similaritat (prefixos creixents + LIKE). L'humà decideix entre opcions
   (`resolve_as_existing` / `resolve_as_new_club` / `reject`) i la decisió
   queda persistida com a àlies: cada nom ambigu es decideix UNA sola vegada.
3. **Auditoria** (`oklliga/audit.py`, `ClubAuditor.detect_suspect_clubs`):
   clubs sense noms, clubs sense partits, noms canònics duplicats entre
   clubs. Cap càrrega massiva es dóna per bona sense passar l'auditoria.
4. **Correció auditada** (`ClubAuditor.merge_clubs`): fusió atòmica que
   reassigna totes les FK, elimina els partits entre les dues identitats
   (simulacres), neteja noms duplicats i registra tot a `club_merge_log`.

## Jugadors i esdeveniments

- `player` és l'entitat persistent amb el mateix patró que els clubs: `player_name`
  guarda àlies i transliteracions per resoldre noms durant l'scraping.
- `squad_membership` registra la trajectòria completa (quin club, des de/quan fins),
  també útil per saber quin nom d'equip cal mostrar en cada època.
- `match_player` és l'alineació d'un partit concret (titular, minuts, porter).
- `match_event` emmagatzema els esdeveniments ordenats temporalment amb un
  `match_event_type` (gol, penalti, falta directa, pròpia porta, targeta blava —
  específica de l'hoquei patins —, vermella, lesió, canvi de porter) i camps
  extensibles `value_num`/`value_json` per detalls futurs (minuts de sanció,
  assistències...). `player_id` pot ser `NULL` quan la font antiga no el identifica.

## Fitxes de partit (jugadors, àrbitres i esdeveniments)

La ingesta de fitxes (`scripts/ingest_sheets.py --season 2025`) completa cada
partit amb:

- **Jugadors** (`player` + `player_name`): creats des de les plantilles de
  l'edició (`stats_1_{idc}.php`, `tipo_stats=plantillas`), que són la font
  autoritativa d'`id_player` de la RFEP. La pertinença a l'edició es registra
  com a `external_id` amb `entity_type='squad_member'`.
- **Alineacions** (`match_player`): les files de l'acta no porten id de
  jugador; es resolen per nom normalitzat dins la plantilla del mateix
  equip. Si un nom no coincideix, va a `pending_name_resolution`
  (`kind='match_sheet_lineup'`) — mai es crea res a cegues.
- **Esdeveniments** (`match_event`): gols (normals, de falta directa i de
  penalti), targetes blaves, grogues i vermelles, amb període, minut del
  període i detall JSON cru per auditoria. Els esdeveniments sense jugador
  identificat queden amb `player_id` NULL (equipatius). Les faltes, els
  temps morts i les faltres directes/penals no consumats no són esdeveniments
  del model: queden al JSON cru.
- **Àrbitres**: entitats (`referee`) amb designació per partit
  (`match_referee`).
- **Pavelló** (`match.venue`): camp de text simple amb el nom del recinte
  tal com el publica la font.

Idempotència: l'índex únic `uq_match_event_natural` (migració 0009)
impedeix duplicar events en reexecutar.

## Estadístiques extensibles

Les mètriques es guarden com a parelles `stat_key` + `value_num/value_text/value_json`
en `match_stat` i `team_match_stat`. Això permet afegir qualsevol mètrica futura
(llançaments, targetes, aturades del porter, possessió, alineacions...) sense
migracions d'esquema, i que les dades antigues simplement tinguin menys claus.

## Fitxers

- `sql/01_schema.sql` — DDL complet (tipus, taules, restriccions, índexs, vista)
- `sql/02_test.sql` — Test amb el cas real Igualada (Hormipresa ↔ Rigat) i canvi de nom de la competició

## Ús

```bash
cp .env.example .env            # i ajusta OKLLIGA_DSN (mai el commitegis)
createdb oklliga
psql -d oklliga -f sql/01_schema.sql
python scripts/seed_2025_26.py  # llegirà OKLLIGA_DSN del .env

# Per actualitzar una base de dades ja existent a noves versions de
# l'esquema (aplica només les migracions pendents, en ordre):
python scripts/migrate.py          # llegirà OKLLIGA_DSN del .env
python scripts/migrate.py --status  # mostra què hi ha aplicat i què falta
```

Les credencials van totes al fitxer `.env` (vegeu `.env.example`):
`OKLLIGA_DSN` per a l'ús normal i `OKLLIGA_ADMIN_DSN` per als tests
d'integració. L'entorn (`export OKLLIGA_DSN=...`) té prioritat sobre
el fitxer, i un argument explícit a l'script té prioritat sobre tot.

## Connector Python

El package `oklliga` és la capa d'accés estable a la base de dades: l'scraper
només parla amb aquest connector, mai amb SQL directe. Si l'esquema evoluciona,
només cal adaptar el connector.

```python
from oklliga import OkLligaDB

db = OkLligaDB("postgresql://user:pass@localhost/oklliga")
with db:
    # clubs i noms històrics
    club = db.upsert_club("Igualada Rigat HC", city="Igualada")
    db.add_club_name(club, "Hormipresa Igualada HC",
                     valid_from="2000-07-01", valid_until="2003-06-30",
                     is_sponsor_name=True)

    # resoldre un nom de la font al club_id (retorna None si no es pot)
    club_id = db.resolve_club("Hormipresa Igualada HC", on_date="2002-11-09")

    # competició, temporada i partit (upserts idempotents)
    comp = db.upsert_competition(1)               # tier 1 = OK Lliga
    season = db.upsert_season(2002)               # 2002 = 2002/03
    sc = db.season_competition_id(comp, season, "OK Lliga")
    m = db.upsert_match(sc, home, away, round=5,
                        matchday_date="2002-11-09",
                        home_goals=3, away_goals=1)

    # esdeveniments
    db.add_match_event(m, home, "goal", player_id=..., minute=12, half=1)

    # consultes de mineria
    for match in db.matches_by_season(2002):
        print(match["home_name_used"], match["home_goals"])
```

Instal·lació i tests:

```bash
pip install -e ".[test]"
python -m pytest tests/ -v   # llegirà OKLLIGA_ADMIN_DSN del .env
```

Els tests creen i destrueixen una base de dades temporal (`oklliga_connector_test`)
i hi carreguen l'esquema complet des de zero.

## Passos següents (extracció)

1. Identificar els endpoints/HTML de la web de la RFEP per temporada i jornada.
2. Scraper desacoblat: parseja HTML → diccionaris Python → crides al connector
   `OkLligaDB`. Cap SQL a l'scraper; les reexecucions són segures (upserts).
3. Ingesta per temporades, marcant `confidence` segons la qualitat de la font.
4. Validació creuada de marcador i classificacions (`participation.points`).
