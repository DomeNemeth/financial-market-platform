-- 0001_baseline
-- The Phase 1 schema, exactly as docker/postgres/init.sql created it.
--
-- Every statement is IF NOT EXISTS so this applies cleanly to a database that
-- was already bootstrapped by the pre-migration init.sql. On such a database
-- this migration is a no-op that exists only to establish the baseline version.

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS staging;
CREATE SCHEMA IF NOT EXISTS intermediate;
CREATE SCHEMA IF NOT EXISTS marts;

-- ============================================================
-- Run ledger — every pipeline run, success or failure.
-- ============================================================
CREATE TABLE IF NOT EXISTS public.pipeline_runs (
    id              UUID         PRIMARY KEY DEFAULT uuid_generate_v4(),
    flow_name       VARCHAR(255) NOT NULL,
    status          VARCHAR(50)  NOT NULL,       -- RUNNING | SUCCESS | FAILED
    started_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,
    rows_ingested   INTEGER,
    error_message   TEXT,
    metadata        JSONB,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pipeline_runs_flow_name  ON public.pipeline_runs (flow_name);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_status     ON public.pipeline_runs (status);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_started_at ON public.pipeline_runs (started_at DESC);

-- ============================================================
-- raw.prices — OHLCV daily bars, one row per (ticker, trading_date, source).
-- ============================================================
CREATE TABLE IF NOT EXISTS raw.prices (
    id              BIGSERIAL    PRIMARY KEY,
    ticker          VARCHAR(20)  NOT NULL,
    trading_date    DATE         NOT NULL,
    open            NUMERIC(18, 6),
    high            NUMERIC(18, 6),
    low             NUMERIC(18, 6),
    close           NUMERIC(18, 6),
    -- NUMERIC, not BIGINT: vendors report fractional volume (Polygon aggregates
    -- fractional-share trades, e.g. 56090840.685498). The raw layer stores what
    -- the source sent; rounding to a whole share count happens in dbt staging.
    volume          NUMERIC(20, 6),
    vwap            NUMERIC(18, 6),
    trade_count     INTEGER,
    source          VARCHAR(50)  NOT NULL,
    ingested_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_raw_prices UNIQUE (ticker, trading_date, source)
);

CREATE INDEX IF NOT EXISTS idx_raw_prices_ticker       ON raw.prices (ticker);
CREATE INDEX IF NOT EXISTS idx_raw_prices_trading_date ON raw.prices (trading_date DESC);
CREATE INDEX IF NOT EXISTS idx_raw_prices_source       ON raw.prices (source);
