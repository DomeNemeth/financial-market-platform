{{
    config(
        materialized='table'
    )
}}

-- The daily price fact. The table the API will serve from.
--
-- Grain: one row per (security_id, trading_date). Enforced by
-- assert_price_fact_grain_is_unique — a mart whose grain is only asserted in a
-- comment is a mart whose grain is wrong eventually.
--
-- ------------------------------------------------------------------------
-- WHAT IS STORED, AND WHY IT IS NOT JUST THE ADJUSTED PRICES
--
-- Raw OHLCV, the cumulative factors, AND the derived series, together. ADR-0003
-- rejected "store adjusted prices materialised" because it destroys
-- point-in-time reproducibility — after a new corporate action rewrites the
-- series there is no record of what it looked like before. That rejection is
-- about storing adjusted prices INSTEAD of factors. Keeping raw and the factors
-- alongside the derived columns preserves everything the ADR wanted:
--
--   - raw OHLCV is untouched and stays byte-comparable with the Parquet archive,
--   - the factors are the stored artefact, so a new action recomputes factors
--     rather than rewriting history,
--   - actions_observed_through says what the factors were computed FROM, which
--     is the `as_of` ADR-0003 asks the mart to carry, and
--   - the adjusted columns are a materialised convenience that any consumer
--     could reproduce from the two beside them.
--
-- The one thing a consumer must not do is treat an adjusted column as a fixed
-- historical fact. It is a function of the action list as of
-- actions_observed_through, and it will change when that does.
--
-- ------------------------------------------------------------------------
-- THE DIMENSION JOIN
--
-- On security_id AND the VALID-time window, not on security_id alone.
--
-- security_id alone would be a bug hiding in plain sight. The surrogate is
-- durable and never reused (ADR-0007), so the join would always "work" — it
-- would simply attach reference data to bars from outside the period that data
-- describes, and nothing would ever fail. Bounding it on valid time makes the
-- claim the join is actually asserting — that this security was tradeable on
-- this date — checkable, and assert_price_fact_bars_lie_in_the_valid_window
-- checks it.
--
-- Valid time (list_date/delist_date), NOT the snapshot's system time
-- (dbt_valid_from/to). Joining a fact's trading_date against dbt_valid_from
-- would be asking "was this bar's date inside the period we believed this row",
-- which is a category error: it compares a date in the market to a timestamp in
-- our database, and it would silently drop every historical bar the moment the
-- snapshot recorded a new version. dim_security exposes both axes under
-- unambiguous names precisely so this choice has to be made on purpose.
--
-- A NULL list_date means "unknown", not "never listed", and widens the window
-- rather than excluding the row — consistent with int_prices_with_calendar.

with adjusted as (

    select * from {{ ref('int_prices_with_adjustments') }}

),

securities as (

    select * from {{ ref('dim_security') }}

),

joined as (

    select
        -- ---------- keys ----------
        a.security_id,
        a.trading_date,

        -- The ticker on the bar as the vendor reported it, kept beside the
        -- dimension's current ticker. They differ after a rename, and the
        -- difference is the audit trail for the identity resolution: a bar
        -- whose vendor ticker no longer matches the security's current one is
        -- exactly what a correctly-handled rename looks like.
        a.ticker            as vendor_ticker,
        s.ticker            as current_ticker,

        -- ---------- raw, exactly as landed ----------
        a.open_price,
        a.high_price,
        a.low_price,
        a.close_price,
        a.volume,
        a.vwap,
        a.trade_count,

        -- ---------- the stored artefact ----------
        a.split_factor,
        a.dividend_factor,

        -- ---------- derived, splits only: charting and price levels ----------
        a.split_adjusted_open,
        a.split_adjusted_high,
        a.split_adjusted_low,
        a.split_adjusted_close,
        a.split_adjusted_volume,
        a.split_adjusted_vwap,

        -- ---------- derived, splits and dividends: returns ----------
        a.total_return_adjusted_close,

        -- ---------- provenance ----------
        -- The observation cutoff the factors were built from (ADR-0003's
        -- as_of), and when this table was built. Both, because they answer
        -- different questions: the first says which actions are reflected, the
        -- second says how stale the table is.
        a.actions_observed_through,
        {{ dbt.current_timestamp() }} as built_at,

        a.is_exchange_session,
        a.source,
        a.ingested_at

    from adjusted a

    -- LEFT, so an unjoined bar is visible to its test rather than silently
    -- absent. An INNER join here would make a valid-time defect look like
    -- missing price data, which is the harder failure to diagnose.
    left join securities s
        on  s.security_id  = a.security_id
        and a.trading_date >= coalesce(s.valid_from, '-infinity'::date)
        and a.trading_date <= coalesce(s.valid_to,   'infinity'::date)

)

select * from joined
