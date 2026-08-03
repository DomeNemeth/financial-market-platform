{{
    config(
        materialized='table'
    )
}}

-- A TABLE, where every other intermediate model is a view. ADR-0008's layer
-- table says `intermediate | view`, and also says view-on-view chains push all
-- computation to query time and will eventually need materialising. This is
-- that point arriving, for one specific model, and it was measured rather than
-- assumed.
--
-- Since ADR-0006 this model is read TWICE on every path through the DAG — once
-- by int_splits__cumulative and once by int_prices_on_raw_basis, which also
-- reads int_splits__cumulative. As a view it is therefore re-planned and
-- re-executed four times over, and it is the most expensive model in the layer
-- to execute: the identity resolution is an INEQUALITY join against the
-- security master's valid-time window, which no index helps and which the
-- planner inlines badly into a chain above it.
--
-- Measured on the 636-bar warehouse: int_prices_on_raw_basis took 5.5 seconds
-- as a pure view chain and 0.00 seconds against a materialised base. The
-- reconciliation test self-joins that model, which turned 5.5 seconds into a
-- query that had not finished after seven minutes.
--
-- The alternative — leaving it a view and tolerating the cost — was rejected
-- because the cost is superlinear in the number of vendors, and the whole point
-- of ADR-0006's DAG is that a third vendor should be cheap.
--
-- ------------------------------------------------------------------------
-- Staged bars from EVERY price vendor, resolved to a durable security_id and
-- checked against the exchange calendar.
--
-- Grain: one row per (security_id, trading_date, SOURCE).
--
-- The source is part of the grain because this model runs BEFORE the merge.
-- Nothing here decides which vendor wins — that is int_prices_merged's job and
-- ADR-0006's decision. Consequently nothing downstream may join to this model
-- on (security_id, trading_date) alone: with two vendors loaded, such a join
-- fans out and silently doubles whatever it is aggregating. The one model that
-- needed exactly that join (int_corporate_actions__factors, resolving a
-- dividend's reference close) reads int_prices_merged instead, and that
-- requirement is what forced the merge to sit after identity resolution rather
-- than before it.
--
-- The union is a UNION ALL over the per-source staging models, never a merge:
-- ADR-0008 forbids a merged stg_prices precisely so that this layer, and only
-- this layer, gets to resolve vendor disagreement.
--
-- Two jobs, both of which have to happen before any adjustment maths can run,
-- and both of which are now written once for all vendors rather than once per
-- vendor. Adding a third source is a single extra branch in the union below.
--
-- 1. IDENTITY. raw.prices is keyed on (ticker, trading_date, source) and carries
--    no security_id — the price ingestion predates the security master. Every
--    downstream model joins on security_id (ADR-0004, ADR-0007), so the ticker →
--    security_id resolution has to happen somewhere, and the intermediate layer
--    is where ADR-0008 puts it.
--
--    The join is NOT `on sm.ticker = p.ticker` alone. That is the exact splice
--    ADR-0007 exists to prevent: tickers are leased by exchanges and reassigned
--    to unrelated companies, so a naive ticker join staples one company's bars
--    onto another company's identity, silently and permanently. The bar date
--    must also fall inside the security's VALID-time window — the vendor's
--    list_date/delist_date, not the snapshot's dbt_valid_from/to, which are
--    system time and answer a different question entirely.
--
--    A NULL list_date is common for older securities and means "unknown", not
--    "never listed", so it widens to -infinity rather than excluding the row.
--    Overstating the window is the safer error here: it produces a resolvable
--    row that the fan-out test can check, where understating it produces an
--    unresolved bar that vanishes from the mart.
--
-- 2. CALENDAR. A bar dated on a day the exchange was shut is a vendor defect.
--    It is FLAGGED, never filtered: ADR-0008 confines staging filters to cheap
--    invariants and says anything subtler must fail loudly instead of quietly
--    dropping rows a human would want to know about. The flag feeds
--    assert_all_bars_fall_on_exchange_sessions.
--
--    This is the complement of assert_no_missing_trading_days, which catches
--    sessions with no bar. Together they bound the price series from both sides.

with prices as (

    select
        ticker, trading_date, open_price, high_price, low_price, close_price,
        volume, vwap, trade_count, source, ingested_at
    from {{ ref('stg_polygon__prices') }}

    union all

    -- Yahoo bars arrive on a DIFFERENT PRICE BASIS — already split-adjusted by
    -- the vendor. They are unioned in unconverted on purpose: the correction
    -- needs the split history, and applying it here would mean every consumer of
    -- this model paid for a join it does not use. int_prices_merged applies it
    -- to exactly the Yahoo rows that survive the priority rule. See ADR-0006.
    select
        ticker, trading_date, open_price, high_price, low_price, close_price,
        volume, vwap, trade_count, source, ingested_at
    from {{ ref('stg_yahoo__prices') }}

),

security_master as (

    select * from {{ ref('stg_polygon__security_master') }}

),

exchange_sessions as (

    select session_date
    from {{ ref('trading_calendar') }}
    where calendar = 'XNYS'

),

identified as (

    select
        sm.security_id,
        p.ticker,
        p.trading_date,
        p.open_price,
        p.high_price,
        p.low_price,
        p.close_price,
        p.volume,
        p.vwap,
        p.trade_count,
        p.source,
        p.ingested_at

    from prices p

    -- LEFT, not INNER: a bar whose ticker has no security master row must stay
    -- visible with a NULL security_id so assert_every_price_bar_resolves_to_a_security
    -- can fail on it. An INNER join would make the same defect look like an
    -- absence of data, which is the failure this project keeps trying not to ship.
    left join security_master sm
        on  sm.ticker = p.ticker
        and p.trading_date >= coalesce(sm.valid_from, '-infinity'::date)
        and p.trading_date <= coalesce(sm.valid_to,   'infinity'::date)

),

with_calendar as (

    select
        i.*,
        (s.session_date is not null) as is_exchange_session

    from identified i
    left join exchange_sessions s
        on s.session_date = i.trading_date

)

select * from with_calendar
