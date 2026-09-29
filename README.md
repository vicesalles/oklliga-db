# OK Lliga — Base de dades per a mineria de dades

Model de dades PostgreSQL per emmagatzemar resultats i estadístiques de tots els
partits de l'OK Lliga (històric complet, incloses les etapes anteriors amb altres
noms de la competició), pensat per a futures operacions de mineria de dades.

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
createdb oklliga
psql -d oklliga -f sql/01_schema.sql
psql -d oklliga -f sql/02_test.sql   # prova de validació
```

## Passos següents (extracció)

1. Identificar els endpoints/HTML de la web de la RFEP per temporada i jornada.
2. Scraper amb resolució de noms d'equip contra `club_name` (diccionari
   nom_històric → `club_id`, amb coincidència exacta i difusa).
3. Ingesta per temporades, marcant `confidence` segons la qualitat de la font.
4. Validació creuada de marcador i classificacions (`participation.points`).
