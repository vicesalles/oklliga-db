-- =====================================================================
-- OK Lliga — Model de dades per a mineria de dades
-- Motor: PostgreSQL 14+ (validat amb 17)
-- Disseny orientat a:
--   1. Escassetat de dades en temporades antigues (source, confidence,
--      camps opcionals, granularitat per estat de partit).
--   2. Renombrat d'equips per sponsors (entity persistent + historial de
--      noms amb rang de vigència).
--   3. Canvis de nom de cada competició (competició persistent + historial
--      de noms per temporada). Una entitat per categoria: OK Lliga (màxima)
--      i OK Lliga Plata (segona) són competicions DIFERENTS.
--   4. Extensibilitat: statistics en JSONB tipat amb check constraints.
--   5. Jugadors i esdeveniments de partit (gols, targetes) amb el mateix
--      patró: identitat persistent + historial de noms + confidence.
-- =====================================================================

BEGIN;

CREATE EXTENSION IF NOT EXISTS btree_gist;

-- ---------------------------------------------------------------------
-- 1. DOMINIS I ENUMERACIONS
-- ---------------------------------------------------------------------

CREATE TYPE stat_scope AS ENUM ('match', 'team_match', 'player_match');
CREATE TYPE match_status AS ENUM (
    'scheduled',   -- programat
    'played',      -- jugat
    'walkover',    -- no presentat
    'postponed',   -- ajornat
    'cancelled'    -- cancel·lat / anul·lat
);
CREATE TYPE confidence_level AS ENUM ('unknown', 'low', 'medium', 'high');
CREATE TYPE match_event_type AS ENUM (
    'goal',            -- gol normal
    'penalty_goal',    -- gol de penalti
    'free_kick_goal',  -- gol de falta directa
    'own_goal',        -- gol en pròpia porta
    'blue_card',      -- targeta blava (2 min, específic hoquei patins)
    'red_card',       -- targeta vermella
    'injury',         -- lesió
    'goalkeeper_change' -- canvi de porter
);
-- Descripció de la font: web federació, premsa, hemeroteca, manual...
CREATE TABLE source (
    source_id     serial PRIMARY KEY,
    name          text NOT NULL UNIQUE,
    url           text,
    notes         text
);

-- ---------------------------------------------------------------------
-- 2. CLUB: entitat persistent, independent del nom
-- ---------------------------------------------------------------------

CREATE TABLE club (
    club_id     serial PRIMARY KEY,
    canonical_name text NOT NULL UNIQUE,    -- nom de referència actual (ex: 'Igualada Rigat HC')
    city       text,
    province   text,
    comarca    text,                        -- comarca (Catalunya); NULL fora de Catalunya
    founded_on date,
    dissolved_on date,
    notes      text,
    CONSTRAINT club_dates CHECK (dissolved_on IS NULL OR dissolved_on >= founded_on)
);

-- Historial de noms: cada club pot tenir múltiples noms amb vigència
-- temporal. Hormipresa Igualada HC, Igualada Rigat HC... són files
-- d'aquesta taula apuntant al mateix club_id.
CREATE TABLE club_name (
    club_name_id serial PRIMARY KEY,
    club_id      integer NOT NULL REFERENCES club(club_id) ON DELETE CASCADE,
    name         text NOT NULL,
    -- Normalització determinista: minúscules, sense accents, espais col·lapsats.
    -- La BD garanteix que no hi hagi dos noms idèntics per normalització.
    name_normalized text GENERATED ALWAYS AS (
        lower(regexp_replace(
            regexp_replace(name, '[^[:alnum:] ]', '', 'g'), '\\s+', ' ', 'g'
        ))
    ) STORED,
    valid_from   date,        -- NULL = origen desconegut
    valid_until  date,        -- NULL = vigent
    is_sponsor_name boolean NOT NULL DEFAULT false,
    CONSTRAINT club_name_dates CHECK (valid_until IS NULL OR valid_from IS NULL OR valid_until >= valid_from),
    -- Un club no pot tenir dos noms amb vigència coneguda solapada.
    -- Les dates NULL = "desconegut", no "infinit": no participa en el check.
    CONSTRAINT club_name_no_overlap EXCLUDE USING gist (
        club_id WITH =,
        daterange(valid_from, valid_until, '[]') WITH &&
    ) WHERE (valid_from IS NOT NULL),
    -- Un mateix nom normalitzat no pot pertànyer a dos clubs mai
    -- (rangs desconeguts compten com a infinit: és el cas ambigu, el
    -- més perillós, i queda prohibit).
    CONSTRAINT club_name_unique_norm EXCLUDE USING gist (
        name_normalized WITH =,
        daterange(
            COALESCE(valid_from, '-infinity'::date),
            COALESCE(valid_until, 'infinity'::date), '[]'
        ) WITH &&
    )
);

-- Índex per resoldre "quin club és aquest nom?" durant l'scraping
CREATE INDEX idx_club_name_name ON club_name (name);
CREATE INDEX idx_club_name_norm ON club_name (name_normalized);
CREATE INDEX idx_club_name_club ON club_name (club_id);

-- ---------------------------------------------------------------------
-- 2b. EQUIPS: l'entitat que competeix. Un club pot tenir diversos equips
--     (primer equip, filials B, C...). La identitat i els noms són del
--     CLUB; qui participa en competicions i juga partits és l'EQUIP.
--     'first' = primer equip; 'B', 'C'... = filials.
-- ---------------------------------------------------------------------
CREATE TABLE team (
    team_id     serial PRIMARY KEY,
    club_id     integer NOT NULL REFERENCES club(club_id),
    label       text NOT NULL DEFAULT 'first',
    notes       text,
    UNIQUE (club_id, label),
    CONSTRAINT team_label_ck CHECK (label = 'first' OR label ~ '^[B-Z]$')
);

-- Cua de resolució de noms: cap insert de club des de scraping es fa a cegues.
-- Els noms que el connector no sap resoldre van aquí i un humà els classifica.
CREATE TABLE pending_name_resolution (
    pending_id    serial PRIMARY KEY,
    raw_name      text NOT NULL,
    context       jsonb,          -- temporada, competició, enfrontaments...
    created_at    timestamp NOT NULL DEFAULT now(),
    status        text NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'resolved', 'rejected')),
    proposed_club_id integer REFERENCES club(club_id),
    resolved_club_id integer REFERENCES club(club_id),
    created_new_club  boolean,
    resolved_by   text,
    resolved_at   timestamp,
    source_id     integer REFERENCES source(source_id),
    notes         text
);

CREATE INDEX idx_pending_status ON pending_name_resolution (status);

-- Log de fusions de clubs: correcció auditada i reversible en auditoria
CREATE TABLE club_merge_log (
    merge_id         serial PRIMARY KEY,
    merged_club_id   integer NOT NULL,   -- club erroni (desactivat)
    kept_club_id     integer NOT NULL,   -- club correcte (subsisteix)
    merged_at        timestamp NOT NULL DEFAULT now(),
    merged_by        text,
    reason           text
);

-- ---------------------------------------------------------------------
-- 3. COMPETICIÓ: entitat persistent amb historial de noms
-- ---------------------------------------------------------------------

CREATE TABLE competition (
    competition_id  serial PRIMARY KEY,
    tier            smallint NOT NULL,         -- 1 = OK Lliga (màxima), 2 = OK Lliga Plata (segona)...
    sport           text NOT NULL DEFAULT 'hoquei patins',
    country         text NOT NULL DEFAULT 'Espanya',
    notes           text,
    UNIQUE (tier, sport, country)             -- una entitat per categoria
);

-- Historial de noms de CADA competició:
--   tier 1: Divisió d'Honor -> OK Lliga
--   tier 2: Primera Divisió -> OK Lliga Plata
CREATE TABLE competition_name (
    competition_name_id serial PRIMARY KEY,
    competition_id      integer NOT NULL REFERENCES competition(competition_id) ON DELETE CASCADE,
    name                text NOT NULL,
    valid_from_season   integer NOT NULL,   -- any d'inici de la temporada (2026 = 2026/27)
    valid_until_season  integer,           -- NULL = vigent
    CONSTRAINT competition_name_dates CHECK (valid_until_season IS NULL OR valid_until_season >= valid_from_season)
);

CREATE INDEX idx_competition_name_name ON competition_name (name);

-- ---------------------------------------------------------------------
-- 4. TEMPORADA I SEASON COMPETITION (la competició d'una temporada)
-- ---------------------------------------------------------------------

CREATE TABLE season (
    season_id   serial PRIMARY KEY,
    start_year  integer NOT NULL UNIQUE,    -- 2026 representa 2026/27
    label       text NOT NULL,              -- '2026/27'
    CONSTRAINT season_label CHECK (label ~ '^[0-9]{4}/[0-9]{2}$')
);

-- La instància concreta d'una competició en una temporada
-- stage distingeix fase regular, play-off (títol), play-off 9-10 i
-- play-out: fases amb idc SIDGAD diferents dins la mateixa temporada.
CREATE TABLE season_competition (
    season_competition_id serial PRIMARY KEY,
    competition_id        integer NOT NULL REFERENCES competition(competition_id),
    season_id             integer NOT NULL REFERENCES season(season_id),
    name_used             text NOT NULL,   -- nom oficial usat aquella temporada
    format                text,            -- 'lliga regular', 'lliga + playoff', etc.
    stage                 text NOT NULL DEFAULT 'regular'
                           CHECK (stage IN ('regular', 'playoff', 'playoff910',
                                            'playout', 'other')),
    UNIQUE (competition_id, season_id, stage)
);

-- ---------------------------------------------------------------------
-- 5. PARTICIPACIONS: quin club juga quina temporada amb quin nom
-- ---------------------------------------------------------------------

CREATE TABLE participation (
    participation_id     serial PRIMARY KEY,
    season_competition_id integer NOT NULL REFERENCES season_competition(season_competition_id),
    team_id              integer NOT NULL REFERENCES team(team_id),
    club_name_used_id    integer REFERENCES club_name(club_name_id), -- nom que apareixia a la font
    final_position       smallint,
    points               smallint,
    -- Mètriques OFICIALS de la font (NULL si la font no les dóna);
    -- v_standings les deriva dels partits per creuament
    wins                 smallint,
    draws                smallint,
    losses               smallint,
    goals_for            smallint,
    goals_against        smallint,
    -- Punts retirats per resolució disciplinària (columna PEN de la
    -- classificació oficial); els punts oficials ja els inclouen
    penalty_points       smallint NOT NULL DEFAULT 0,
    notes                text,
    UNIQUE (season_competition_id, team_id)
);

-- ---------------------------------------------------------------------
-- 6. PARTITS
-- ---------------------------------------------------------------------

CREATE TABLE match (
    match_id             serial PRIMARY KEY,
    season_competition_id integer NOT NULL REFERENCES season_competition(season_competition_id),
    round                smallint,         -- jornada
    stage                text,              -- 'regular', 'playoff QF', 'final', ...
    matchday_date        date,             -- pot ser NULL si és desconeguda (dades antigues)
    status               match_status NOT NULL DEFAULT 'scheduled',
    home_team_id         integer NOT NULL REFERENCES team(team_id),
    away_team_id         integer NOT NULL REFERENCES team(team_id),
    home_goals           smallint,
    away_goals           smallint,
    home_goals_first_half smallint,         -- 1r temps (escàs en dades antigues)
    away_goals_first_half smallint,
    home_goals_second_half smallint,
    away_goals_second_half smallint,
    venue                text,
    attendance           integer,
    source_id            integer REFERENCES source(source_id),
    source_url           text,
    source_ref           text,              -- id a la font original
    confidence           confidence_level NOT NULL DEFAULT 'high',
    notes                text,
    CONSTRAINT match_home_away_diff CHECK (home_team_id <> away_team_id),
    CONSTRAINT match_score_status CHECK (
        (status = 'played' AND home_goals IS NOT NULL AND away_goals IS NOT NULL)
        OR (status <> 'played')
    )
);

-- Unicitat natural d'un partit dins d'una temporada de competició:
-- mateixa jornada (si coneguda) o, si no, mateixa data i enfrontament.
CREATE UNIQUE INDEX uq_match_sc_round ON match (season_competition_id, home_team_id, away_team_id, round)
    WHERE round IS NOT NULL;
CREATE UNIQUE INDEX uq_match_sc_date ON match (season_competition_id, home_team_id, away_team_id, matchday_date)
    WHERE round IS NULL AND matchday_date IS NOT NULL;

CREATE INDEX idx_match_sc_date ON match (season_competition_id, matchday_date);
CREATE INDEX idx_match_home ON match (home_team_id);
CREATE INDEX idx_match_away ON match (away_team_id);

-- ---------------------------------------------------------------------
-- 7. ESTADÍSTIQUES EXTENSIBLES (JSONB)
--    Permet guardar qualsevol mètrica actual o futura (targetes, llançaments,
--    aturades del porter, posseeïssió, alineacions...) sense canviar esquema.
--    Les dades antigues simplement tindran menys claus.
-- ---------------------------------------------------------------------

-- Estadístiques a nivell de partit (global)
CREATE TABLE match_stat (
    match_stat_id serial PRIMARY KEY,
    match_id      integer NOT NULL REFERENCES match(match_id) ON DELETE CASCADE,
    stat_key      text NOT NULL,           -- 'attendance', 'venue_alt', 'referee', ...
    value_num     numeric,
    value_text    text,
    value_json    jsonb,
    source_id     integer REFERENCES source(source_id),
    confidence    confidence_level NOT NULL DEFAULT 'high',
    UNIQUE (match_id, stat_key)
);

-- Estadístiques per equip i partit
CREATE TABLE team_match_stat (
    team_match_stat_id serial PRIMARY KEY,
    match_id           integer NOT NULL REFERENCES match(match_id) ON DELETE CASCADE,
    team_id            integer NOT NULL REFERENCES team(team_id),
    is_home            boolean NOT NULL,
    stat_key           text NOT NULL,      -- 'shots', 'penalties', 'free_kicks', ...
    value_num          numeric,
    value_text         text,
    value_json         jsonb,
    source_id          integer REFERENCES source(source_id),
    confidence         confidence_level NOT NULL DEFAULT 'high',
    UNIQUE (match_id, team_id, stat_key)
);

-- Àrbitres: entitats persistents + designació per partit
CREATE TABLE referee (
    referee_id  serial PRIMARY KEY,
    full_name   text NOT NULL UNIQUE,
    notes       text
);

CREATE TABLE match_referee (
    match_id    integer NOT NULL REFERENCES match(match_id) ON DELETE CASCADE,
    referee_id  integer NOT NULL REFERENCES referee(referee_id),
    role        text NOT NULL DEFAULT 'main',
    PRIMARY KEY (match_id, referee_id, role)
);

-- Auditoria d'intents de descàrrega de fitxes: distingeix 'no publicada
-- encara al SIDGAD' (404 reintentable) de 'sense dades'.
CREATE TABLE sheet_fetch_attempt (
    attempt_id  serial PRIMARY KEY,
    match_id    integer NOT NULL REFERENCES match(match_id) ON DELETE CASCADE,
    idp         text NOT NULL,
    endpoint    text NOT NULL,
    status      text NOT NULL CHECK (status IN ('ok', 'not_published_404', 'no_data')),
    http_status integer,
    source_id   integer REFERENCES source(source_id),
    attempted_at timestamp NOT NULL DEFAULT now(),
    notes       text
);
CREATE INDEX idx_sheet_fetch_attempt_match
    ON sheet_fetch_attempt (match_id, attempted_at);
CREATE INDEX idx_sheet_fetch_attempt_status
    ON sheet_fetch_attempt (status, attempted_at);

-- ---------------------------------------------------------------------
-- 8. JUGADORS: mateix patró que clubs (entitat persistent + historial
--    de noms) i vincle temporal club-jugador (plantilles/fitxatges)
-- ---------------------------------------------------------------------

CREATE TABLE player (
    player_id    serial PRIMARY KEY,
    full_name    text NOT NULL,             -- nom de referència actual
    birth_date   date,                       -- sovint desconegut en dades antigues
    position     text,                       -- 'porter', 'defensa', 'davanter'...
    handedness   text,                       -- 'esquerra', 'dreta'
    notes        text
);

-- Historial de noms del jugador (transliteracions, àlies, canvis documentats)
CREATE TABLE player_name (
    player_name_id serial PRIMARY KEY,
    player_id      integer NOT NULL REFERENCES player(player_id) ON DELETE CASCADE,
    name           text NOT NULL,
    valid_from     date,
    valid_until    date,
    CONSTRAINT player_name_dates CHECK (valid_until IS NULL OR valid_from IS NULL OR valid_until >= valid_from)
);

CREATE INDEX idx_player_name_name ON player_name (name);
CREATE INDEX idx_player_name_player ON player_name (player_id);

-- Fitxatges: quin jugador pertany a quin club i quan (trajectòria completa)
CREATE TABLE squad_membership (
    squad_membership_id serial PRIMARY KEY,
    player_id           integer NOT NULL REFERENCES player(player_id),
    club_id             integer NOT NULL REFERENCES club(club_id),
    valid_from          date,                -- NULL = origen desconegut
    valid_until         date,                -- NULL = vigent
    source_id           integer REFERENCES source(source_id),
    confidence          confidence_level NOT NULL DEFAULT 'high',
    CONSTRAINT squad_membership_dates CHECK (valid_until IS NULL OR valid_from IS NULL OR valid_until >= valid_from)
);

CREATE INDEX idx_squad_membership_player ON squad_membership (player_id);
CREATE INDEX idx_squad_membership_club ON squad_membership (club_id);

-- ---------------------------------------------------------------------
-- 8b. PARTICIPACIÓ EN PARTIT i ESDEVENIMENTS
-- ---------------------------------------------------------------------

-- Participació d'un jugador en un partit concret (alineació)
CREATE TABLE match_player (
    match_player_id serial PRIMARY KEY,
    match_id        integer NOT NULL REFERENCES match(match_id) ON DELETE CASCADE,
    team_id         integer NOT NULL REFERENCES team(team_id),
    player_id       integer NOT NULL REFERENCES player(player_id),
    is_starter      boolean,
    minutes_played  smallint,
    is_goalkeeper   boolean,
    source_id       integer REFERENCES source(source_id),
    confidence      confidence_level NOT NULL DEFAULT 'high',
    UNIQUE (match_id, player_id)
);

CREATE INDEX idx_match_player_player ON match_player (player_id);
CREATE INDEX idx_match_player_match ON match_player (match_id);

-- Esdeveniments del partit amb ordre temporal i detall extensible
CREATE TABLE match_event (
    match_event_id serial PRIMARY KEY,
    match_id       integer NOT NULL REFERENCES match(match_id) ON DELETE CASCADE,
    club_id        integer NOT NULL REFERENCES club(club_id),    -- equip que genera l'esdeveniment
    player_id      integer REFERENCES player(player_id),          -- NULL si no es coneix
    event_type     match_event_type NOT NULL,
    minute         smallint,                                     -- minut de joc (escàs en dades antigues)
    half           smallint CHECK (half IN (1, 2)),
    value_num      numeric,                                      -- ex: minuts de sanció (2, 5, 10)
    value_json     jsonb,                                        -- detalls extensors
    source_id      integer REFERENCES source(source_id),
    source_url     text,
    confidence     confidence_level NOT NULL DEFAULT 'high',
    notes          text
);

CREATE INDEX idx_match_event_match ON match_event (match_id, minute);
CREATE INDEX idx_match_event_player ON match_event (player_id);
CREATE INDEX idx_match_event_type ON match_event (event_type);

-- ---------------------------------------------------------------------
-- 9. VISTA: partits amb noms històrics correctes per temporada
--    Resol automàticament el nom que cada club usava en el moment del partit.
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

-- Classificació derivada dels partits i control de discrepàncies
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
