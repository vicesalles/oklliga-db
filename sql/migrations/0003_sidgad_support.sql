-- Migració 0003: suport a ingesta RFEP/SIDGAD
--
-- 1. Fases a season_competition: la fase regular i els play-offs de la
--    mateixa temporada són stages diferents (idc SIDGAD distints).
--    La columna ja existia a match.stage; ara la pujem a nivell d'edició.
-- 2. external_id: IDs externs amb namespace (source, entity, valor),
--    per guardar rfep_season_id, idc, club_*, team_id, idp, id_player.
-- 3. raw_snapshot: còpia auditada del fragment HTML original per poder
--    reprocessar quan el parser canviï.
-- 4. result_type a match: partits per resolució disciplinària, walkover...

BEGIN;

-- 1. Fases a season_competition -----------------------------------------
ALTER TABLE season_competition
    ADD COLUMN IF NOT EXISTS stage text NOT NULL DEFAULT 'regular';

-- Evitem duplicats existents: l'índex únic antic era sobre
-- (competition_id, season_id); ara ha de ser per fase.
ALTER TABLE season_competition
    DROP CONSTRAINT IF EXISTS season_competition_competition_id_season_id_key;
ALTER TABLE season_competition
    ADD CONSTRAINT season_competition_stage_chk
    CHECK (stage IN ('regular', 'playoff', 'playout', 'other'))
    NOT VALID;
ALTER TABLE season_competition
    ADD CONSTRAINT uq_season_competition_sc_stage
    UNIQUE (competition_id, season_id, stage);

-- 2. IDs externs amb namespace -------------------------------------------
CREATE TABLE IF NOT EXISTS external_id (
    external_id_id serial PRIMARY KEY,
    source_id      integer NOT NULL REFERENCES source(source_id),
    entity_type    text NOT NULL,   -- 'season', 'competition_edition', 'club', 'team_entry', 'match', 'player'
    internal_id    integer NOT NULL, -- PK de la taula interna (season_id, club_id, match_id...)
    external_id   text NOT NULL,    -- valor a la font ('37', '2816', 'club_72', '32743'...)
    notes         text,
    fetched_at    timestamp NOT NULL DEFAULT now(),
    UNIQUE (source_id, entity_type, external_id)
);
CREATE INDEX IF NOT EXISTS idx_external_id_internal ON external_id (entity_type, internal_id);

-- 3. Snapshots crus del SIDGAD ------------------------------------------
CREATE TABLE IF NOT EXISTS raw_snapshot (
    raw_snapshot_id serial PRIMARY KEY,
    source_id       integer NOT NULL REFERENCES source(source_id),
    endpoint        text NOT NULL,      -- ex: 'rfep_cal_idc_2816_1.php'
    params          jsonb,              -- paràmetres del POST
    body_hash       text NOT NULL,     -- sha256 hex del cos
    body            text NOT NULL,     -- fragment HTML original
    parser_version  text NOT NULL DEFAULT 'v0',
    fetched_at      timestamp NOT NULL DEFAULT now(),
    UNIQUE (endpoint, body_hash)
);
CREATE INDEX IF NOT EXISTS idx_raw_snapshot_fetched ON raw_snapshot (fetched_at);

-- 4. Tipus de resultat del partit ---------------------------------------
ALTER TABLE match
    ADD COLUMN IF NOT EXISTS result_type text
    CHECK (result_type IS NULL OR result_type IN ('on_field', 'disciplinary', 'walkover'));

COMMIT;
