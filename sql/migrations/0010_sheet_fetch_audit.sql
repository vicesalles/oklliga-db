-- 0010: Auditoria d'intents de descàrrega de fitxes de partit.
-- Les fitxes dels playoffs poden respondre 404 (encara no publicades al
-- SIDGAD). Enregistrarem cada intent per poder reintentar només les
-- pendents i distingir 'no publicada' de 'sense dades'.

BEGIN;

CREATE TABLE IF NOT EXISTS sheet_fetch_attempt (
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

CREATE INDEX IF NOT EXISTS idx_sheet_fetch_attempt_match
    ON sheet_fetch_attempt (match_id, attempted_at);

CREATE INDEX IF NOT EXISTS idx_sheet_fetch_attempt_status
    ON sheet_fetch_attempt (status, attempted_at);

COMMIT;
