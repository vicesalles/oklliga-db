-- =====================================================================
-- Migració 0007: fases de play-off amb identitat pròpia
--
-- Una temporada de l'OK Lliga pot tenir diverses fases post-temporada
-- amb idc SIDGAD propis: play-off pel títol, play-off 9-10 i play-out
-- (permanència). Fins ara totes coïncidien a stage='playoff' i es
-- fusionaven en un únic season_competition (UNIQUE competition_id,
-- season_id, stage), perdent partits dins la fase equivocada.
--
-- Nou domini de stage a season_competition:
--   'regular'     fase de lliga
--   'playoff'     play-off pel títol
--   'playoff910'  play-off pel 9è/10è lloc
--   'playout'     play-out de permanència
--   'other'       altres
--
-- Idempotent i tolerant amb BD existents.
-- =====================================================================
BEGIN;

-- 1. Relaxa el CHECK de stage
ALTER TABLE season_competition
    DROP CONSTRAINT IF EXISTS season_competition_stage_chk;

ALTER TABLE season_competition
    ADD CONSTRAINT season_competition_stage_chk
    CHECK (stage IN ('regular', 'playoff', 'playoff910', 'playout', 'other'));

-- 2. Reanomena la fase fusionada si existeix: les fases que ja
--    comparteixen stage='playoff' amb diverses idc no es poden
--    separar automàticament (els partits ja barrejats), però les
--    noves ingestes crearan season_competitions separats.
--    (El nom_used distingeix; la dada antiga es pot corregir amb
--    la cua de resolució o reingestant la temporada.)

COMMIT;
