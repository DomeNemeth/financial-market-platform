{{
    config(
        materialized='view'
    )
}}

-- Staged OHLCV daily bars from Yahoo Finance.
-- Grain: one row per (ticker, trading_date).
--
-- THESE BARS ARE NOT ON THE SAME PRICE BASIS AS stg_polygon__prices.
--
-- Polygon is fetched with adjusted=false, so its bars are the unadjusted
-- prints. Yahoo's chart endpoint has no such flag: its series is already
-- back-adjusted for splits as of the moment it was fetched, and no parameter
-- turns that off. Measured on KLAC's 10-for-1 split of 2026-06-12 — the
-- 2026-06-11 close here is 241.164, where Polygon reports 2411.64.
--
-- Correcting that is NOT this model's job. ADR-0008 confines staging to
-- renaming, casting, and cheap sanity filters, and the de-adjustment needs the
-- split history, which is a join. It happens one layer up, in
-- int_prices_merged, using int_splits__cumulative. See ADR-0006.
--
-- vwap and trade_count are selected explicitly as NULL rather than omitted, so
-- this model is union-compatible with stg_polygon__prices column-for-column and
-- a reader can see that the absence is deliberate. Yahoo supplies neither, and
-- ADR-0006 refuses to fabricate them: vwap could be approximated as (h+l+c)/3
-- convincingly enough that nothing downstream would catch it, which is exactly
-- what makes it worse than a NULL.

with source as (

    select * from {{ source('raw', 'prices') }}
    where source = 'yahoo'

),

renamed_and_filtered as (

    select
        ticker,
        trading_date,
        open        as open_price,
        high        as high_price,
        low         as low_price,
        close       as close_price,
        -- Yahoo already reports whole-share volume, so this round() is a no-op
        -- on real data. It is here so the column arrives as bigint exactly as
        -- stg_polygon__prices' does: a union of numeric and bigint would resolve
        -- to numeric and silently change the type of a mart column.
        round(volume)::bigint as volume,

        -- Not supplied by the vendor. See the header.
        cast(null as numeric) as vwap,
        cast(null as bigint)  as trade_count,

        source,
        ingested_at

    from source

    where
        -- The same cheap invariants stg_polygon__prices applies. They should
        -- never fire on real data; they exist so a corrupted row cannot reach
        -- the merge and win a fallback slot on a date Polygon has no bar for.
        close > 0
        and volume >= 0
        and high >= low

)

select * from renamed_and_filtered
