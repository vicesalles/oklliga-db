-- 0009: àrbitres com a entitats + designacions per partit.
-- Pavelló/localitat: camps simples a match (venue ja existeix).

CREATE TABLE IF NOT EXISTS referee (
    referee_id  serial PRIMARY KEY,
    full_name   text NOT NULL UNIQUE,
    notes       text
);

CREATE TABLE IF NOT EXISTS match_referee (
    match_id    integer NOT NULL REFERENCES match(match_id) ON DELETE CASCADE,
    referee_id  integer NOT NULL REFERENCES referee(referee_id),
    role        text NOT NULL DEFAULT 'main',
    PRIMARY KEY (match_id, referee_id, role)
);

-- Targeta groga: existeix a l'acta (avís previ a la blava)
ALTER TYPE match_event_type ADD VALUE IF NOT EXISTS 'yellow_card';

-- Idempotència d'esdeveniments: un event (partit, equip, tipus, minut,
-- jugador, contingut) només pot existir un cop.
DELETE FROM match_event a USING match_event b
WHERE a.match_event_id > b.match_event_id
  AND a.match_id = b.match_id AND a.team_id = b.team_id
  AND a.player_id IS NOT DISTINCT FROM b.player_id
  AND a.event_type = b.event_type
  AND a.minute IS NOT DISTINCT FROM b.minute
  AND a.value_json = b.value_json;
CREATE UNIQUE INDEX IF NOT EXISTS uq_match_event_natural
    ON match_event (match_id, team_id, event_type, minute,
                    COALESCE(player_id, 0), value_json);
