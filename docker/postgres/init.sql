-- ============================================================
-- Financial Market Platform — Postgres Bootstrap
--
-- The Postgres image's entrypoint runs this ONLY when the data directory is
-- empty. Editing it therefore has no effect on a database that already exists,
-- which is why it deliberately no longer contains any table DDL.
--
-- All tables live in migrations/ and are applied by:
--     .venv\Scripts\python.exe -m src.common.migrate
--
-- Anything below is limited to what must exist before the first migration can
-- run: the extension the run ledger's UUID default needs, and the dbt layer
-- schemas. Both are also repeated in migrations/0001_baseline.sql (idempotently)
-- so a database bootstrapped by an older copy of this file converges to the same
-- state. To rebuild from scratch:
--     docker compose down -v && docker compose up -d
--     .venv\Scripts\python.exe -m src.common.migrate
-- ============================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Schemas matching the dbt layers (ADR-0008).
CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS staging;
CREATE SCHEMA IF NOT EXISTS intermediate;
CREATE SCHEMA IF NOT EXISTS marts;

DO $$
BEGIN
    RAISE NOTICE 'Bootstrap complete. Schemas: raw, staging, intermediate, marts.';
    RAISE NOTICE 'Tables are NOT created here — run: python -m src.common.migrate';
END $$;
