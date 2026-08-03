-- 0005_macro_data
-- FRED macroeconomic series: the reference data a price series gets interpreted
-- against. Two tables, mirroring the split the rest of the schema already uses —
-- one for the thing's identity and attributes, one for its observations.

-- ============================================================
-- raw.macro_series
--
-- One row per (series_id, source).
--
-- series_id IS FRED'S OWN ID, USED DIRECTLY. There is no surrogate key here and
-- that is a deliberate departure from raw.security_identity, not an oversight.
--
-- ADR-0007 introduces a surrogate for securities because a ticker is a LEASE:
-- "AAPL" identifies whoever currently holds it, exchanges reassign it, and
-- joining on it splices unrelated companies together. None of that is true of a
-- FRED series ID. "UNRATE" is assigned once by a single authority, is globally
-- unique within FRED, is never reused for a different series, and has no
-- competing vendor issuing a conflicting "UNRATE". The three properties that
-- forced a surrogate for securities — reuse, multiple issuing authorities, and
-- vendor-specific spellings — are all absent.
--
-- A surrogate here would therefore buy nothing and cost the thing that makes
-- this data pleasant to work with: you can read a query and know what it is
-- about. `where series_id = 'UNRATE'` needs no lookup; `where security_id = 3`
-- does.
--
-- The revisit trigger is explicit. If a SECOND macro vendor is ever added — the
-- ECB, the OECD, the BLS directly — then "the unemployment rate" acquires
-- competing vendor-specific IDs and this decision must be revisited, exactly as
-- ADR-0007 describes for securities. `source` is already in the key so that the
-- collision surfaces as two rows rather than as an overwrite, and the mapping
-- table that would be needed can be added without changing this one.
-- ============================================================
CREATE TABLE IF NOT EXISTS raw.macro_series (
    series_id       VARCHAR(64)  NOT NULL,
    source          VARCHAR(50)  NOT NULL,

    title           TEXT         NOT NULL,

    -- FRED reports frequency both long ("Monthly") and short ("M"). Both are
    -- kept: the short form is what a query filters on, the long form is what a
    -- chart legend needs, and deriving one from the other means encoding FRED's
    -- vocabulary in our code.
    frequency       VARCHAR(32),
    frequency_short VARCHAR(8),

    -- Units matter more here than anywhere else in the schema. GDP is in
    -- billions of dollars, UNRATE is a percent, CPIAUCSL is an index with an
    -- arbitrary 1982-84=100 base. Nothing in the numbers themselves says so, and
    -- a chart that plots them on one axis is meaningless. Stored verbatim rather
    -- than normalised into an enum, because FRED's unit strings are the only
    -- authority on what a FRED number means.
    units           VARCHAR(128),
    units_short     VARCHAR(64),

    -- "Seasonally Adjusted" / "Not Seasonally Adjusted". A seasonally adjusted
    -- and an unadjusted series of the same quantity are different series with
    -- different IDs, and confusing them is the macro-data equivalent of
    -- confusing ADR-0003's two adjusted price series.
    seasonal_adjustment       VARCHAR(64),
    seasonal_adjustment_short VARCHAR(8),

    -- The period the series covers, as the vendor reports it. Valid time.
    observation_start DATE,
    observation_end   DATE,

    -- When FRED last revised ANY observation in this series. System time, and
    -- the macro analogue of actions_observed_through: a macro series is not a
    -- fixed historical object either.
    last_updated    TIMESTAMPTZ,

    notes           TEXT,

    ingested_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_macro_series PRIMARY KEY (series_id, source)
);

-- ============================================================
-- raw.macro_observations
--
-- One row per (series_id, observation_date, source).
--
-- ------------------------------------------------------------
-- observation_date IS THE START OF THE PERIOD, NOT THE PUBLICATION DATE.
--
-- FRED dates a monthly observation to the first of the month: January 2026's
-- unemployment rate is dated 2026-01-01. That value did not exist on
-- 2026-01-01. It was first published on 2026-02-11, forty-one days later.
--
-- This is the single sharpest hazard in this table. Joining a daily price on
-- 2026-01-15 to "the most recent macro observation on or before that date"
-- returns a number that nobody could have known for another month — textbook
-- look-ahead bias, and a backtest built on it prints money that does not exist.
-- It is exactly the class of silent, plausible-looking error the security
-- master's valid-time bounds exist to prevent, in a different guise.
--
-- Hence first_published_date, below, which is why this table stores a column
-- FRED's default response does not even return.
-- ------------------------------------------------------------
-- THE '.' SENTINEL.
--
-- FRED returns the STRING "." for a missing observation — a market holiday in a
-- daily series, a period a survey did not run. It is not zero and must never
-- become zero: DGS10 has "." on 2026-07-03, the observed Independence Day
-- holiday, and a 0.0 there is a ten-year Treasury yield of zero percent, which
-- would be absorbed silently by every moving average and correlation computed
-- over it.
--
-- `value` is therefore NULLABLE by design. src/ingestion/fred.py converts the
-- sentinel explicitly rather than relying on a numeric coercion to fail, so the
-- conversion is visible at the point it happens.
-- ============================================================
CREATE TABLE IF NOT EXISTS raw.macro_observations (
    series_id       VARCHAR(64)  NOT NULL,
    observation_date DATE        NOT NULL,
    source          VARCHAR(50)  NOT NULL,

    -- NULL means "FRED reported '.' for this period" — genuinely no observation.
    -- NUMERIC, never float: these are levels that get differenced and
    -- compounded, and the project's rule about float error applies here as much
    -- as it does to prices. The scale is wide because the series are: GDP is
    -- ~30,000 (billions), DGS10 is ~4.5 (percent).
    value           NUMERIC(20, 6),

    -- ------------------------------------------------------------
    -- The bitemporal columns. Both come from FRED, neither is derived.
    --
    -- first_published_date: when the FIRST estimate for this period became
    -- available. Fetched separately with output_type=4 (initial release), which
    -- is the only way FRED will report it — the default response stamps every
    -- realtime_start with today's date, which is true and useless.
    --
    -- This is what makes a point-in-time join possible at all. A macro series
    -- joined on `first_published_date <= trading_date` cannot leak a value that
    -- had not been published, which is the look-ahead bias described above.
    --
    -- vintage_date: which vintage `value` is from — the realtime date of the
    -- fetch that produced it. Today, for a default fetch. Kept so that
    -- "which revision is this number" has an answer stored beside the number,
    -- rather than being inferred from ingested_at.
    --
    -- WHAT THIS PAIR DOES NOT GIVE YOU, stated here because the columns look
    -- like they give more than they do: `value` is the LATEST revision, not the
    -- value as first published. So a join on first_published_date eliminates
    -- look-ahead about a number's EXISTENCE but not about its VALUE — the
    -- figure served is one that may have been revised several times since.
    --
    -- That is the same boundary ADR-0009 draws for the API's `as_of`, which
    -- rewinds valid time without replaying what the platform believed. Closing
    -- it properly needs every vintage stored, which is a table an order of
    -- magnitude larger and a Phase 6 decision.
    -- ------------------------------------------------------------
    first_published_date DATE,
    vintage_date         DATE,

    ingested_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_macro_observations
        PRIMARY KEY (series_id, observation_date, source),

    CONSTRAINT fk_macro_observations_series
        FOREIGN KEY (series_id, source)
        REFERENCES raw.macro_series (series_id, source),

    -- An observation cannot have been published before the period it describes
    -- began. A row violating this means the publication date was taken from the
    -- wrong field — most likely the default response's realtime_start, which is
    -- the date of the FETCH and would put a 1948 observation's publication in
    -- 2026. Cheap to check, and it fails at the door rather than silently
    -- widening every point-in-time join by seventy years.
    CONSTRAINT ck_macro_observations_publication_follows_period
        CHECK (first_published_date IS NULL
               OR first_published_date >= observation_date)
);

CREATE INDEX IF NOT EXISTS idx_macro_observations_series
    ON raw.macro_observations (series_id, observation_date DESC);

-- The index the point-in-time ASOF join actually uses. Ordered on the
-- PUBLICATION date, not the observation date, because the correct join asks
-- "what had been published by this trading date" and the index has to answer
-- that directly or the join degrades to a scan per bar.
CREATE INDEX IF NOT EXISTS idx_macro_observations_published
    ON raw.macro_observations (series_id, first_published_date DESC)
    WHERE first_published_date IS NOT NULL;
