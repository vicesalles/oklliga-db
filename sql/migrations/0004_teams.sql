-- =====================================================================
-- Migració 0004: equips (entitat competidora) i filials
--
-- Semàntica: el CLUB és la identitat persistent (noms, història);
-- l'EQUIP és qui competeix. Un club pot tenir diversos equips:
-- 'first' (primer equip) i filials 'B', 'C'...
-- participation, match, team_match_stat, match_player i match_event
-- passen de referenciar club a referenciar team.
--
-- Compatible amb les dues situacions d'arrencada:
--   a) BD creada amb l'esquema antic (columnes club_*): conversió
--      completa amb backfill (equip 'first' per club, referències
--      reconvertides, columnes antigues eliminades).
--   b) BD creada amb l'esquema nou ja actualitzat: no-op (les taules
--      ja tenen team_id); només assegura equip 'first' per club.
-- Cada pas és tolerant i es pot reexecutar.
-- =====================================================================

BEGIN;

-- La vista depèn de les columnes club de match: es reconstrueix al final
DROP VIEW IF EXISTS v_match_with_names;

CREATE TABLE IF NOT EXISTS team (
    team_id     serial PRIMARY KEY,
    club_id     integer NOT NULL REFERENCES club(club_id),
    label       text NOT NULL DEFAULT 'first',
    notes       text,
    UNIQUE (club_id, label),
    CONSTRAINT team_label_ck CHECK (label = 'first' OR label ~ '^[B-Z]$')
);

-- Un equip 'first' per cada club que no en tingui
INSERT INTO team (club_id, label, notes)
SELECT c.club_id, 'first', 'Creat per la migració 0004 (equip primer)'
FROM club c
WHERE NOT EXISTS (
    SELECT 1 FROM team t WHERE t.club_id = c.club_id AND t.label = 'first'
);

-- ---------------------------------------------------------------------
-- participation: club_id -> team_id
-- ---------------------------------------------------------------------
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'participation' AND column_name = 'club_id'
    ) THEN
        ALTER TABLE participation ADD COLUMN IF NOT EXISTS team_id integer;
        UPDATE participation p
        SET team_id = t.team_id
        FROM team t
        WHERE t.club_id = p.club_id AND t.label = 'first' AND p.team_id IS NULL;
        ALTER TABLE participation
            ALTER COLUMN team_id SET NOT NULL,
            DROP CONSTRAINT IF EXISTS participation_season_competition_id_club_id_key;
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'participation_team_id_fkey'
              AND conrelid = 'participation'::regclass
        ) THEN
            ALTER TABLE participation
                ADD CONSTRAINT participation_team_id_fkey
                FOREIGN KEY (team_id) REFERENCES team(team_id);
        END IF;
        ALTER TABLE participation DROP COLUMN IF EXISTS club_id;
    END IF;
END $$;

ALTER TABLE participation ALTER COLUMN team_id SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_participation_sc_team
    ON participation (season_competition_id, team_id);

-- ---------------------------------------------------------------------
-- match: home/away club -> team
-- ---------------------------------------------------------------------
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'match' AND column_name = 'home_club_id'
    ) THEN
        ALTER TABLE match ADD COLUMN IF NOT EXISTS home_team_id integer;
        ALTER TABLE match ADD COLUMN IF NOT EXISTS away_team_id integer;
        UPDATE match m
        SET home_team_id = t.team_id
        FROM team t
        WHERE t.club_id = m.home_club_id AND t.label = 'first'
          AND m.home_team_id IS NULL;
        UPDATE match m
        SET away_team_id = t.team_id
        FROM team t
        WHERE t.club_id = m.away_club_id AND t.label = 'first'
          AND m.away_team_id IS NULL;
        ALTER TABLE match DROP CONSTRAINT IF EXISTS match_home_away_diff;
        ALTER TABLE match
            ALTER COLUMN home_team_id SET NOT NULL,
            ALTER COLUMN away_team_id SET NOT NULL;
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'match_home_team_id_fkey'
              AND conrelid = 'match'::regclass
        ) THEN
            ALTER TABLE match
                ADD CONSTRAINT match_home_team_id_fkey
                FOREIGN KEY (home_team_id) REFERENCES team(team_id);
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'match_away_team_id_fkey'
              AND conrelid = 'match'::regclass
        ) THEN
            ALTER TABLE match
                ADD CONSTRAINT match_away_team_id_fkey
                FOREIGN KEY (away_team_id) REFERENCES team(team_id);
        END IF;
        DROP INDEX IF EXISTS uq_match_sc_round;
        DROP INDEX IF EXISTS uq_match_sc_date;
        DROP INDEX IF EXISTS idx_match_home;
        DROP INDEX IF EXISTS idx_match_away;
        ALTER TABLE match DROP COLUMN IF EXISTS home_club_id;
        ALTER TABLE match DROP COLUMN IF EXISTS away_club_id;
    END IF;
END $$;

ALTER TABLE match ALTER COLUMN home_team_id SET NOT NULL;
ALTER TABLE match ALTER COLUMN away_team_id SET NOT NULL;

-- Constraint i índexs finals (idempotents en totes les situacions)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'match_home_away_diff'
          AND conrelid = 'match'::regclass
    ) THEN
        ALTER TABLE match
            ADD CONSTRAINT match_home_away_diff
            CHECK (home_team_id <> away_team_id);
    END IF;
END $$;

DROP INDEX IF EXISTS uq_match_sc_round;
CREATE UNIQUE INDEX uq_match_sc_round
    ON match (season_competition_id, home_team_id, away_team_id, round)
    WHERE round IS NOT NULL;
DROP INDEX IF EXISTS uq_match_sc_date;
CREATE UNIQUE INDEX uq_match_sc_date
    ON match (season_competition_id, home_team_id, away_team_id, matchday_date)
    WHERE round IS NULL AND matchday_date IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_match_home ON match (home_team_id);
CREATE INDEX IF NOT EXISTS idx_match_away ON match (away_team_id);

-- ---------------------------------------------------------------------
-- team_match_stat, match_player, match_event: club_id -> team_id
-- (taules encara buides a la pràctica, però el pas és segur)
-- ---------------------------------------------------------------------
DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['team_match_stat', 'match_player', 'match_event']
    LOOP
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = t AND column_name = 'club_id'
        ) THEN
            EXECUTE format(
                'ALTER TABLE %I ADD COLUMN IF NOT EXISTS team_id integer', t);
            EXECUTE format(
                'UPDATE %I s SET team_id = tt.team_id '
                'FROM team tt WHERE tt.club_id = s.club_id '
                'AND tt.label = ''first'' AND s.team_id IS NULL', t);
            EXECUTE format(
                'ALTER TABLE %I DROP CONSTRAINT IF EXISTS %I, '
                'ALTER COLUMN team_id SET NOT NULL', t, t || '_club_id_fkey');
            EXECUTE format(
                'ALTER TABLE %I DROP CONSTRAINT IF EXISTS %I', t,
                t || '_match_id_club_id_stat_key_key');
            EXECUTE format(
                'ALTER TABLE %I DROP COLUMN IF EXISTS club_id', t);
        END IF;
    END LOOP;
END $$;

-- ---------------------------------------------------------------------
-- Vista actualitzada a la nova estructura
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW v_match_with_names AS
SELECT
    m.match_id,
    s.label AS season,
    sc.name_used AS competition_name,
    m.round,
    m.stage,
    m.matchday_date,
    m.status,
    ht.label AS home_team_label,
    hc.canonical_name AS home_club,
    COALESCE(hn.name, hc.canonical_name) AS home_name_used,
    at.label AS away_team_label,
    ac.canonical_name AS away_club,
    COALESCE(an.name, ac.canonical_name) AS away_name_used,
    m.home_goals,
    m.away_goals,
    m.home_goals_first_half,
    m.away_goals_first_half,
    m.home_goals_second_half,
    m.away_goals_second_half,
    m.venue,
    m.confidence
FROM match m
JOIN season_competition sc ON sc.season_competition_id = m.season_competition_id
JOIN season s ON s.season_id = sc.season_id
JOIN team ht ON ht.team_id = m.home_team_id
JOIN club hc ON hc.club_id = ht.club_id
JOIN team at ON at.team_id = m.away_team_id
JOIN club ac ON ac.club_id = at.club_id
LEFT JOIN club_name hn ON hn.club_id = hc.club_id
     AND (hn.valid_from IS NULL OR hn.valid_from <= m.matchday_date)
     AND (hn.valid_until IS NULL OR hn.valid_until >= m.matchday_date)
LEFT JOIN club_name an ON an.club_id = ac.club_id
     AND (an.valid_from IS NULL OR an.valid_from <= m.matchday_date)
     AND (an.valid_until IS NULL OR an.valid_until >= m.matchday_date);

COMMIT;
