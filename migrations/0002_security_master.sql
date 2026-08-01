-- 0002_security_master
-- Reference data: durable security identity + the current vendor snapshot.
-- Design rationale in docs/adr/0004-bitemporal-security-master.md and
-- docs/adr/0007-identifier-strategy.md.

-- ============================================================
-- raw.security_identity
--
-- The durable surrogate key. security_id is meaningless, never changes for a
-- given security, and is never reused. Every fact table joins on it rather than
-- on ticker, because tickers are leased by exchanges and get reassigned to
-- unrelated companies (ADR-0007).
--
-- identity_key is the natural key the surrogate is anchored to:
--   'figi:BBG000B9XRY4'      — resolved, durable across rebrands and renames
--   'vendor_ticker:polygon:AAPL' — provisional, used until OpenFIGI resolves
--
-- A provisional identity is promoted in place: identity_key and identity_kind
-- are rewritten, security_id is not, so every foreign key survives.
-- ============================================================
CREATE TABLE IF NOT EXISTS raw.security_identity (
    security_id     BIGSERIAL    PRIMARY KEY,
    identity_key    TEXT         NOT NULL,
    identity_kind   VARCHAR(20)  NOT NULL,
    first_seen_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    resolved_at     TIMESTAMPTZ,               -- when promoted to a FIGI anchor
    CONSTRAINT uq_security_identity_key  UNIQUE (identity_key),
    CONSTRAINT ck_security_identity_kind
        CHECK (identity_kind IN ('figi', 'vendor_ticker'))
);

-- Partial index: the promotion path and the "what is still unresolved?" query
-- both filter on provisional rows, which are the minority once backfilled.
CREATE INDEX IF NOT EXISTS idx_security_identity_provisional
    ON raw.security_identity (identity_key)
    WHERE identity_kind = 'vendor_ticker';

-- ============================================================
-- raw.security_master
--
-- CURRENT vendor snapshot only — one row per (security_id, source), upserted.
-- This table deliberately holds no history: the dbt snapshot owns the system-time
-- axis (ADR-0004), and keeping raw current-only is what keeps it faithful to what
-- the vendor most recently said (ADR-0002).
--
-- list_date / delist_date are the VALID-time axis: when the security itself was
-- tradeable. They are distinct from the snapshot's dbt_valid_from/to, which are
-- SYSTEM time — when this platform believed the row. Conflating the two is the
-- usual way a point-in-time claim turns out to be false.
-- ============================================================
CREATE TABLE IF NOT EXISTS raw.security_master (
    id              BIGSERIAL    PRIMARY KEY,
    security_id     BIGINT       NOT NULL
        REFERENCES raw.security_identity (security_id),
    ticker          VARCHAR(20)  NOT NULL,
    name            TEXT,

    -- Identifiers. FIGI is free and redistributable (OpenFIGI).
    -- CUSIP and ISIN are licensed: the columns exist so a licensed deployment
    -- needs no migration, but they are NEVER derived, inferred, or generated.
    -- NULL is the honest value here. See ADR-0007.
    figi                VARCHAR(12),
    share_class_figi    VARCHAR(12),
    cusip               VARCHAR(9),
    isin                VARCHAR(12),

    exchange        VARCHAR(20),               -- MIC, e.g. XNAS / XNYS
    currency        CHAR(3),
    security_type   VARCHAR(50),               -- CS | ETF | ADRC | ...
    active          BOOLEAN,

    -- Valid time, as reported by the vendor. NULL is common for older
    -- securities and is stored as NULL rather than defaulted to an open
    -- interval, which would overstate coverage.
    list_date       DATE,
    delist_date     DATE,

    source          VARCHAR(50)  NOT NULL,
    ingested_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_security_master UNIQUE (security_id, source),
    CONSTRAINT ck_security_master_valid_range
        CHECK (delist_date IS NULL OR list_date IS NULL OR delist_date >= list_date)
);

CREATE INDEX IF NOT EXISTS idx_security_master_ticker ON raw.security_master (ticker);
CREATE INDEX IF NOT EXISTS idx_security_master_figi   ON raw.security_master (figi);
CREATE INDEX IF NOT EXISTS idx_security_master_active ON raw.security_master (active);
