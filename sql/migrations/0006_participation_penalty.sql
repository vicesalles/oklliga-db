-- =====================================================================
-- Migració 0006: penalitzacions oficials de classificació
--
-- La classificació oficial (rfep_clasif_idc_*.php) pot portar una
-- columna addicional PEN amb punts retirats per resolucions
-- disciplinàries (ex: CH CALDES 2025/26, -3 punts). Els punts
-- oficials de la font ja inclouen la penalització; aquest camp la
-- fa explícita i auditabile per a la vista de discrepàncies.
--
-- La vista v_standing_discrepancies s'elimina i es recrea (no es
-- pot fer CREATE OR REPLACE afegint-hi columnes).
-- =====================================================================
BEGIN;

ALTER TABLE participation
    ADD COLUMN IF NOT EXISTS penalty_points smallint NOT NULL DEFAULT 0;

DROP VIEW IF EXISTS v_standing_discrepancies;

CREATE VIEW v_standing_discrepancies AS
SELECT
    vs.season,
    vs.competition,
    vs.canonical_name,
    vs.played        AS calc_played,
    p.points         AS official_points,
    vs.points_calc   AS calc_points,
    p.penalty_points AS penalty_points,
    vs.wins          AS calc_wins,
    p.wins           AS official_wins,
    vs.draws         AS calc_draws,
    p.draws          AS official_draws,
    vs.losses        AS calc_losses,
    p.losses         AS official_losses,
    vs.goals_for     AS calc_gf,
    p.goals_for      AS official_gf,
    vs.goals_against AS calc_gc,
    p.goals_against  AS official_gc
FROM v_standings vs
JOIN participation p
  ON p.season_competition_id = vs.season_competition_id
 AND p.team_id = vs.team_id
WHERE p.points IS DISTINCT FROM vs.points_calc
   OR p.wins IS DISTINCT FROM vs.wins
   OR p.draws IS DISTINCT FROM vs.draws
   OR p.losses IS DISTINCT FROM vs.losses
   OR p.goals_for IS DISTINCT FROM vs.goals_for
   OR p.goals_against IS DISTINCT FROM vs.goals_against;

COMMIT;
