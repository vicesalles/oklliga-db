-- Test de validació: cas real Igualada (2 noms, 1 club) + canvi de nom competició
BEGIN;

INSERT INTO source (name) VALUES ('RFEP - hemeroteca web');

INSERT INTO club (canonical_name, city) VALUES
    ('Igualada Rigat HC', 'Igualada'),
    ('Reus Deportiu', 'Reus');

INSERT INTO club_name (club_id, name, valid_from, valid_until, is_sponsor_name) VALUES
    (1, 'Igualada HC', '1990-01-01', '2000-06-30', false),
    (1, 'Hormipresa Igualada HC', '2000-07-01', '2003-06-30', true),
    (1, 'Igualada Rigat HC', '2003-07-01', NULL, true),
    (2, 'Reus Deportiu', '1940-01-01', NULL, false);

-- Dues competicions DIFERENTS: màxima categoria i segona categoria
INSERT INTO competition (competition_id, tier, notes) VALUES
    (1, 1, 'Màxima categoria estatal'),
    (2, 2, 'Segona categoria estatal');
INSERT INTO competition_name (competition_id, name, valid_from_season, valid_until_season) VALUES
    (1, 'Divisió d''Honor', 1965, 2001),
    (1, 'OK Lliga', 2002, NULL),
    (2, 'Primera Divisió', 1965, 2001),
    (2, 'OK Lliga Plata', 2002, NULL);

INSERT INTO season (start_year, label) VALUES (2002, '2002/03'), (2026, '2026/27');

INSERT INTO season_competition (competition_id, season_id, name_used) VALUES
    (1, 1, 'OK Lliga'),
    (1, 2, 'OK Lliga'),
    (2, 2, 'OK Lliga Plata');      -- segona divisió, entitat diferent

INSERT INTO match (season_competition_id, round, stage, matchday_date, status,
                   home_club_id, away_club_id, home_goals, away_goals,
                   home_goals_first_half, away_goals_first_half,
                   home_goals_second_half, away_goals_second_half,
                   source_id, confidence)
VALUES
    (1, 5, 'regular', '2002-11-09', 'played', 1, 2, 3, 1, 2, 0, 1, 1, 1, 'high'),
    (2, 5, 'regular', NULL, 'played', 2, 1, 4, 2, NULL, NULL, NULL, NULL, 1, 'low');

INSERT INTO team_match_stat (match_id, club_id, is_home, stat_key, value_num, confidence) VALUES
    (1, 1, true, 'shots', 28, 'high'),
    (1, 2, false, 'shots', 15, 'medium');

-- Jugadors: trajectòria i participació en el partit de 2002
INSERT INTO player (full_name, position) VALUES
    ('Marc Torras', 'davanter'),
    ('Albert Casanova', 'porter');

INSERT INTO player_name (player_id, name) VALUES
    (1, 'Marc Torras Gras');

INSERT INTO squad_membership (player_id, club_id, valid_from, valid_until, confidence) VALUES
    (1, 1, '2000-07-01', '2005-06-30', 'high'),
    (1, 2, '2005-07-01', NULL, 'high'),
    (2, 1, '1998-07-01', NULL, 'medium');

INSERT INTO match_player (match_id, club_id, player_id, is_starter, minutes_played, is_goalkeeper, confidence) VALUES
    (1, 1, 1, true, 50, false, 'high'),
    (1, 1, 2, true, 50, true, 'medium');

INSERT INTO match_event (match_id, club_id, player_id, event_type, minute, half, value_num, confidence) VALUES
    (1, 1, 1, 'goal', 12, 1, NULL, 'high'),
    (1, 1, 1, 'penalty_goal', 34, 1, NULL, 'high'),
    (1, 2, NULL, 'blue_card', 41, 1, 2, 'low'),
    (1, 2, NULL, 'goal', 28, 1, NULL, 'medium');

COMMIT;

-- Comprovacions jugador/esdeveniments
SELECT 'player_count' AS check_name, count(*)::text AS value FROM player
UNION ALL
SELECT 'torras_goals_2002', count(*)::text FROM match_event
    WHERE player_id = 1 AND event_type IN ('goal','penalty_goal','free_kick_goal')
    AND match_id = 1
UNION ALL
SELECT 'torras_squad_changes', count(*)::text FROM squad_membership WHERE player_id = 1;

-- Màxims goleadors ficticis del test (consulta de mineria exemple)
SELECT p.full_name, count(*) AS goals
FROM match_event me
JOIN player p ON p.player_id = me.player_id
WHERE me.event_type IN ('goal','penalty_goal','free_kick_goal')
GROUP BY p.full_name ORDER BY goals DESC;

-- Comprovacions
SELECT 'match_count' AS check_name, count(*)::text AS value FROM match
UNION ALL
SELECT 'same_club_2_names', count(DISTINCT club_id)::text FROM club_name
UNION ALL
SELECT 'distinct_competitions', count(*)::text FROM competition;

-- La vista ha de mostrar Hormipresa el 2002 i Rigat el 2026
SELECT season, home_name_used, away_name_used, home_goals, away_goals, confidence
FROM v_match_with_names ORDER BY season;

-- Resolució inversa: trobar club a partir d'un nom històric
SELECT cn.name, c.canonical_name FROM club_name cn JOIN club c ON c.club_id = cn.club_id WHERE cn.name = 'Hormipresa Igualada HC';
