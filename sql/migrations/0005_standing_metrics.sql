-- =====================================================================
-- Migració 0005: mètriques oficials de classificació a participation
--
-- Guarda els valors OFICIALS de la font (rfep_clasif_idc_*.php): victòries,
-- empats, derrotes, gols a favor i en contra. Tots opcionals (NULL) per a
-- participacions carregades des de fonts sense classificació detallada
-- (dades antigues escasses).
--
-- La vista v_standings (afegida aquí) deriva les mateixes mètriques dels
-- partits: creuar official vs calculat detecta resolucions disciplinàries
-- i errors de la font (control de qualitat per a mineria).
-- =====================================================================

BEGIN;

ALTER TABLE participation
    ADD COLUMN IF NOT EXISTS wins smallint,
    ADD COLUMN IF NOT EXISTS draws smallint,
    ADD COLUMN IF NOT EXISTS losses smallint,
    ADD COLUMN IF NOT EXISTS goals_for smallint,
    ADD COLUMN IF NOT EXISTS goals_against smallint;

-- Classificació derivada dels partits ingestats (sempre actual)
CREATE OR REPLACE VIEW v_standings AS
SELECT
    sc.season_competition_id,
    s.start_year,
    s.label AS season,
    sc.name_used AS competition,
    t.team_id,
    t.label AS team_label,
    c.club_id,
    c.canonical_name,
    count(*) FILTER (WHERE m.status = 'played') AS played,
    count(*) FILTER (
        WHERE m.status = 'played'
          AND ((m.home_team_id = t.team_id AND m.home_goals > m.away_goals)
            OR (m.away_team_id = t.team_id AND m.away_goals > m.home_goals))
    ) AS wins,
    count(*) FILTER (
        WHERE m.status = 'played' AND m.home_goals = m.away_goals
    ) AS draws,
    count(*) FILTER (
        WHERE m.status = 'played'
          AND ((m.home_team_id = t.team_id AND m.home_goals < m.away_goals)
            OR (m.away_team_id = t.team_id AND m.away_goals < m.home_goals))
    ) AS losses,
    COALESCE(sum(m.home_goals) FILTER (WHERE m.home_team_id = t.team_id),
             0)
    + COALESCE(sum(m.away_goals) FILTER (WHERE m.away_team_id = t.team_id),
               0) AS goals_for,
    COALESCE(sum(m.away_goals) FILTER (WHERE m.home_team_id = t.team_id),
             0)
    + COALESCE(sum(m.home_goals) FILTER (WHERE m.away_team_id = t.team_id),
               0) AS goals_against,
    -- Punts recalculats (3/1/0) per comparar amb els oficials
    (count(*) FILTER (
        WHERE m.status = 'played'
          AND ((m.home_team_id = t.team_id AND m.home_goals > m.away_goals)
            OR (m.away_team_id = t.team_id AND m.away_goals > m.home_goals))
    ) * 3
    + count(*) FILTER (
        WHERE m.status = 'played' AND m.home_goals = m.away_goals
    )) AS points_calc
FROM participation p
JOIN team t ON t.team_id = p.team_id
JOIN club c ON c.club_id = t.club_id
JOIN season_competition sc ON sc.season_competition_id = p.season_competition_id
JOIN season s ON s.season_id = sc.season_id
LEFT JOIN match m
    ON m.season_competition_id = p.season_competition_id
   AND t.team_id IN (m.home_team_id, m.away_team_id)
GROUP BY sc.season_competition_id, s.start_year, s.label, sc.name_used,
         t.team_id, t.label, c.club_id, c.canonical_name;

-- Discrepàncies entre la classificació oficial i la calculada:
-- resolucions disciplinàries, walkovers o errors de font/ingesta.
-- Vista de control de qualitat: hauria de ser buida en una temporada
-- tancada i conciliada.
CREATE OR REPLACE VIEW v_standing_discrepancies AS
SELECT
    vs.season,
    vs.competition,
    vs.canonical_name,
    vs.played        AS calc_played,
    p.points         AS official_points,
    vs.points_calc   AS calc_points,
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
