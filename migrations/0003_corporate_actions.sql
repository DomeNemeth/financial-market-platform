-- 0003_corporate_actions
-- Splits and cash dividends — the inputs to the adjustment factors described in
-- docs/adr/0003-adjusted-price-methodology.md.

-- ============================================================
-- raw.corporate_actions
--
-- One row per (security_id, action_type, ex_date, source).
--
-- ex_date is the grain, not the announcement or pay date: the ex-date is when
-- the price basis actually changes, and it is the only date the adjustment
-- maths uses. A bar ON the ex-date already trades on the new basis, which is
-- why the factor products in ADR-0003 use `ex_date > d`, strictly.
--
-- The action-specific columns are deliberately separate rather than a single
-- polymorphic `value`: a split ratio and a dividend amount are different units
-- (dimensionless vs currency) and mixing them in one column invites exactly the
-- kind of silent unit error this schema exists to prevent. The CHECK constraint
-- enforces that the right one is populated for the right action_type.
-- ============================================================
CREATE TABLE IF NOT EXISTS raw.corporate_actions (
    id              BIGSERIAL    PRIMARY KEY,
    security_id     BIGINT       NOT NULL
        REFERENCES raw.security_identity (security_id),
    ticker          VARCHAR(20)  NOT NULL,     -- as reported; denormalised for traceability
    action_type     VARCHAR(20)  NOT NULL,     -- 'split' | 'dividend'
    ex_date         DATE         NOT NULL,

    -- Splits. Stored as the vendor's two-sided ratio rather than a single float
    -- so the source values survive verbatim (ADR-0002). NVDA 2024-06-10 was
    -- split_to = 10, split_from = 1. The adjustment ratio is split_to/split_from.
    split_to        NUMERIC(18, 6),
    split_from      NUMERIC(18, 6),

    -- Cash dividends, per share, in `currency`.
    cash_amount     NUMERIC(18, 6),
    currency        CHAR(3),
    dividend_type   VARCHAR(20),               -- CD (regular) | SC (special) | LT | ST

    -- Context dates. Not used by the adjustment maths, kept for auditability.
    declaration_date DATE,
    record_date      DATE,
    pay_date         DATE,

    source          VARCHAR(50)  NOT NULL,
    ingested_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_corporate_actions
        UNIQUE (security_id, action_type, ex_date, source),

    CONSTRAINT ck_corporate_actions_type
        CHECK (action_type IN ('split', 'dividend')),

    -- A split with a zero or absent ratio would silently produce a division by
    -- zero or a no-op factor; a dividend with no amount would vanish from the
    -- factor product. Reject both at the door rather than debugging a wrong
    -- price series later.
    CONSTRAINT ck_corporate_actions_payload CHECK (
        (action_type = 'split'
            AND split_to  IS NOT NULL AND split_to  > 0
            AND split_from IS NOT NULL AND split_from > 0)
        OR
        (action_type = 'dividend'
            AND cash_amount IS NOT NULL AND cash_amount > 0)
    )
);

CREATE INDEX IF NOT EXISTS idx_corporate_actions_security
    ON raw.corporate_actions (security_id, ex_date DESC);
CREATE INDEX IF NOT EXISTS idx_corporate_actions_ticker
    ON raw.corporate_actions (ticker, ex_date DESC);
CREATE INDEX IF NOT EXISTS idx_corporate_actions_type
    ON raw.corporate_actions (action_type);
