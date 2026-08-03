{{
    config(
        materialized='table'
    )
}}

-- Daily price bars joined to the macro data that was ACTUALLY PUBLISHED by that
-- trading date.
--
-- Grain: one row per (security_id, trading_date, series_id).
--
-- ========================================================================
-- THE PROBLEM THIS MODEL EXISTS TO SOLVE
--
-- Prices are daily. Macro series are monthly or quarterly. Joining them means
-- resolving a grain mismatch, and the obvious way to do it is wrong.
--
-- The obvious way is an ASOF join on the observation date: for each bar, take
-- the most recent macro observation dated on or before it. That is the standard
-- textbook pattern and it introduces look-ahead bias, because FRED dates an
-- observation to the START OF THE PERIOD IT DESCRIBES, not to when it was
-- published:
--
--     January 2026 unemployment       dated  2026-01-01
--                                 published  2026-02-11   (41 days later)
--
-- So an ASOF join on observation_date attaches January's unemployment rate to
-- every trading day from January 1st onward — including the whole of January,
-- during which nobody on earth knew the number. Measured over the series loaded
-- here, the lag it leaks is not marginal:
--
--     UMCSENT    ~26 days       CPIAUCSL   ~43 days
--     FEDFUNDS   ~31 days       INDPRO     ~46 days
--     UNRATE     ~35 days       GDP       ~121 days (max 175)
--
-- A strategy backtested on that join trades on data it could not have had, and
-- the error flatters it — which is the direction that gets a model deployed.
-- It is the same class of silent, plausible failure as joining a price bar to a
-- security by bare ticker equality (ADR-0007): nothing raises, every row looks
-- reasonable, and the result is confidently wrong.
--
-- ========================================================================
-- THE JOIN THIS MODEL ACTUALLY DOES
--
-- ASOF on first_published_date, not on observation_date. For each bar, the most
-- recent macro observation that HAD ALREADY BEEN PUBLISHED on that date.
--
-- Postgres has no ASOF JOIN, so it is a LATERAL subquery with ORDER BY ... LIMIT
-- 1, which is the canonical form: correlated on the outer row, ordered by the
-- axis being searched, taking one. It is O(bars x log observations) with the
-- idx_macro_observations_published index, which exists for exactly this and is
-- ordered on the publication date rather than the observation date because that
-- is the column the search actually bounds.
--
-- Note what is NOT used: a window function over a union of bars and
-- observations. That form is tempting and it is much harder to verify, because
-- the correctness lives in a frame clause rather than in a predicate anyone can
-- read. The same reasoning is written out in int_corporate_actions__factors,
-- which chose a correlated max() over lag() for the same reason.
--
-- ========================================================================
-- WHAT THIS STILL DOES NOT FIX — READ BEFORE BACKTESTING ON IT
--
-- `macro_value` is the LATEST REVISION of the number, not the number as first
-- published. FRED revises heavily; UMCSENT and GDP especially.
--
-- So this join removes look-ahead about a value's EXISTENCE and not about its
-- VALUE. On 2026-02-11 you would have known that January unemployment had been
-- reported, and you would have seen the FIRST estimate — which may since have
-- been revised. This model serves the revised figure against the original
-- publication date.
--
-- That boundary is deliberate, and it is exactly the one ADR-0009 draws for the
-- API's `as_of`, which rewinds valid time without replaying what the platform
-- believed at the time. Closing it needs every vintage stored rather than the
-- latest, which is an order of magnitude more data and a Phase 6 decision.
-- ADR-0012 states the limitation; macro_vintage_date is carried on every row so
-- that a consumer can see which revision they are holding rather than having to
-- assume.
--
-- ========================================================================
-- WHICH SERIES APPEAR
--
-- Only those with supports_point_in_time_join. FRED publishes no
-- initial-release history for calculated series (T10Y2Y, a spread) or for the
-- daily constant-maturity Treasury series (DGS10, DGS2), so there is no
-- publication date for them and there is no honest ASOF join to make.
--
-- They are EXCLUDED rather than joined with an assumed same-day lag. Assuming
-- one would mean inventing a publication date — the same category of error as
-- fabricating a CUSIP (ADR-0007) or a vwap (ADR-0006), and wrong in the
-- flattering direction again. Their absence is visible in dim_macro_series.

with prices as (

    select
        security_id,
        vendor_ticker,
        current_ticker,
        trading_date,
        close_price,
        split_adjusted_close,
        source as price_source
    from {{ ref('fct_security_price_daily') }}

),

eligible_series as (

    select
        series_id,
        title,
        frequency_short,
        units_short,
        seasonal_adjustment_short,
        is_annualised_rate
    from {{ ref('dim_macro_series') }}
    where supports_point_in_time_join

),

observations as (

    select
        series_id,
        observation_date,
        value,
        first_published_date,
        vintage_date,
        publication_lag_days
    from {{ ref('stg_fred__observations') }}
    -- A '.' row carries no number, so it cannot be the answer to "what was the
    -- latest known value". Excluding it here rather than downstream means the
    -- LATERAL returns the most recent REAL observation, skipping past holes,
    -- instead of returning a NULL that a consumer would have to re-search
    -- behind.
    where value is not null
      and first_published_date is not null

)

select
    p.security_id,
    p.vendor_ticker,
    p.current_ticker,
    p.trading_date,
    p.close_price,
    p.split_adjusted_close,
    p.price_source,

    s.series_id,
    s.title            as series_title,
    s.frequency_short,
    s.units_short,
    s.seasonal_adjustment_short,
    s.is_annualised_rate,

    m.value                as macro_value,
    m.observation_date     as macro_observation_date,
    m.first_published_date as macro_first_published_date,
    m.vintage_date         as macro_vintage_date,
    m.publication_lag_days as macro_publication_lag_days,

    -- How stale the attached number is, along both axes. They differ by the
    -- publication lag and a consumer needs both: the first says how old the
    -- economic period is, the second says how long the market has had to price
    -- the information in.
    (p.trading_date - m.observation_date)     as observation_age_days,
    (p.trading_date - m.first_published_date) as days_since_publication,

    {{ dbt.current_timestamp() }} as built_at

from prices p
cross join eligible_series s

-- The ASOF join. LEFT LATERAL, not INNER: a bar earlier than the series' first
-- publication legitimately has no macro value, and an inner join would silently
-- drop the bar entirely rather than showing it with a NULL. Dropping price rows
-- because a macro series was not yet published would be the macro layer
-- deleting price data, which no consumer would expect.
left join lateral (

    select o.*
    from observations o
    where o.series_id = s.series_id
      -- The whole point. `<=` on the PUBLICATION date, so a value is visible on
      -- the day it was released and not one day before. Using observation_date
      -- here instead is the look-ahead bug this model exists to avoid, and
      -- assert_macro_context_has_no_lookahead fails on it.
      and o.first_published_date <= p.trading_date
    order by
        o.first_published_date desc,
        -- Tiebreak: two observations can be published on the same day — a new
        -- print and a revision to an earlier period arrive together. The one
        -- describing the LATER period is the current state of the world.
        o.observation_date desc
    limit 1

) m on true
