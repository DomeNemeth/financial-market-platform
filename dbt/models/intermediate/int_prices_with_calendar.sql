{{
    config(
        materialized='view'
    )
}}

-- Staged Polygon bars, resolved to a durable security_id and checked against the
-- exchange calendar.
--
-- Grain: one row per (security_id, trading_date).
--
-- Two jobs, both of which have to happen before any adjustment maths can run.
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

    select * from {{ ref('stg_polygon__prices') }}

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
