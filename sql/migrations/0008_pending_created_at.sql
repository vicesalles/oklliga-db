-- =====================================================================
-- Migració 0008: created_at a pending_name_resolution
--
-- La cua de resolució registrava només quan es resolvia un cas
-- (resolved_at), no quan hi entrava. created_at permet saber quan va
-- aparèixer cada pendent i ordenar/auditar la cua per antiguitat.
-- =====================================================================
BEGIN;

ALTER TABLE pending_name_resolution
    ADD COLUMN IF NOT EXISTS created_at timestamp NOT NULL DEFAULT now();

COMMIT;
