# OK Lliga — Base de dades per a mineria de dades

Model de dades PostgreSQL per emmagatzemar resultats i estadístiques de tots els
partits de l'OK Lliga (històric complet, incloses les etapes anteriors amb altres
noms de la competició), pensat per a futures operacions de mineria de dades.

## Les tres dificultats i com les resol el model

| Dificultat | Solució al model |
|---|---|
| **Dades escasses en el passat** | Camps opcionals (`matchday_date`, gols per temps...), `source_id` + `source_url` per traçabilitat, i `confidence` (`unknown/low/medium/high`) a cada fila per ponderar les dades a la mineria. Les restriccions només exigeixen marcador si l'estat és `played`. |
| **Equips que canvien de nom per sponsors** | `club` és l'entitat persistent; `club_name` guarda l'historial de noms amb `valid_from`/`valid_until` i flag `is_sponsor_name`. Hormipresa Igualada HC i Igualada Rigat HC són dues files de `club_name` amb el mateix `club_id`. La vista `v_match_with_names` resol automàticament el nom vigent en la data de cada partit. |
| **La competició canvia de nom** | `competition` és l'entitat persistent; `competition_name` guarda els noms per rang de temporades (Divisió d'Honor → OK Lliga) i `season_competition` fixa el nom usat a cada temporada concreta. |

## Model (resum)

```
club ──< club_name                    (entitat + historial de noms)
competition ──< competition_name      (entitat + historial de noms)
competition ──> season_competition <── season
season_competition ──< participation >── club
season_competition ──< match >── club (local/visitant)
match ──< match_stat          (JSONB extensibles, àmbit partit)
match ──< team_match_stat    (JSONB extensibles, àmbit equip-partit)
```

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
