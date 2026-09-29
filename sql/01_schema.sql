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
-- =====================================================================

BEGIN;

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
    canonical_name text NOT NULL,           -- nom de referència actual (ex: 'Igualada Rigat HC')
    city       text,
    province   text,
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
    valid_from   date,        -- NULL = origen desconegut
    valid_until  date,        -- NULL = vigent
    is_sponsor_name boolean NOT NULL DEFAULT false,
    CONSTRAINT club_name_dates CHECK (valid_until IS NULL OR valid_from IS NULL OR valid_until >= valid_from)
);

-- Índex per resoldre "quin club és aquest nom?" durant l'scraping
CREATE INDEX idx_club_name_name ON club_name (name);
CREATE INDEX idx_club_name_club ON club_name (club_id);

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
CREATE TABLE season_competition (
    season_competition_id serial PRIMARY KEY,
    competition_id        integer NOT NULL REFERENCES competition(competition_id),
    season_id             integer NOT NULL REFERENCES season(season_id),
    name_used             text NOT NULL,   -- nom oficial usat aquella temporada
    format                text,            -- 'lliga regular', 'lliga + playoff', etc.
    UNIQUE (competition_id, season_id)
);

-- ---------------------------------------------------------------------
-- 5. PARTICIPACIONS: quin club juga quina temporada amb quin nom
-- ---------------------------------------------------------------------

CREATE TABLE participation (
    participation_id     serial PRIMARY KEY,
    season_competition_id integer NOT NULL REFERENCES season_competition(season_competition_id),
    club_id              integer NOT NULL REFERENCES club(club_id),
    club_name_used_id    integer REFERENCES club_name(club_name_id), -- nom que apareixia a la font
    final_position       smallint,
    points               smallint,
    notes                text,
    UNIQUE (season_competition_id, club_id)
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
    home_club_id         integer NOT NULL REFERENCES club(club_id),
    away_club_id         integer NOT NULL REFERENCES club(club_id),
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
    CONSTRAINT match_home_away_diff CHECK (home_club_id <> away_club_id),
    CONSTRAINT match_score_status CHECK (
        (status = 'played' AND home_goals IS NOT NULL AND away_goals IS NOT NULL)
        OR (status <> 'played')
    )
);

CREATE INDEX idx_match_sc_date ON match (season_competition_id, matchday_date);
CREATE INDEX idx_match_home ON match (home_club_id);
CREATE INDEX idx_match_away ON match (away_club_id);

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
    club_id            integer NOT NULL REFERENCES club(club_id),
    is_home            boolean NOT NULL,
    stat_key           text NOT NULL,      -- 'shots', 'penalties', 'free_kicks', ...
    value_num          numeric,
    value_text         text,
    value_json         jsonb,
    source_id          integer REFERENCES source(source_id),
    confidence         confidence_level NOT NULL DEFAULT 'high',
    UNIQUE (match_id, club_id, stat_key)
);

-- ---------------------------------------------------------------------
-- 8. VISTA: partits amb noms històrics correctes per temporada
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
    hc.canonical_name AS home_club,
    COALESCE(hn.name, hc.canonical_name) AS home_name_used,
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
JOIN club hc ON hc.club_id = m.home_club_id
JOIN club ac ON ac.club_id = m.away_club_id
LEFT JOIN club_name hn ON hn.club_id = m.home_club_id
     AND (hn.valid_from IS NULL OR hn.valid_from <= m.matchday_date)
     AND (hn.valid_until IS NULL OR hn.valid_until >= m.matchday_date)
LEFT JOIN club_name an ON an.club_id = m.away_club_id
     AND (an.valid_from IS NULL OR an.valid_from <= m.matchday_date)
     AND (an.valid_until IS NULL OR an.valid_until >= m.matchday_date);

COMMIT;
