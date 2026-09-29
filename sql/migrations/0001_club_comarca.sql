-- Migració: afegeix la columna comarca a club.
-- Per a bases de dades ja poblades amb l'esquema anterior.
-- Idempotent: es pot executar més d'un cop sense error.

ALTER TABLE club ADD COLUMN IF NOT EXISTS comarca text;

COMMENT ON COLUMN club.comarca IS 'Comarca (Catalunya); NULL fora de Catalunya';
