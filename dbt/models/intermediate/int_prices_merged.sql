{{
    config(
        materialized='view'
    )
}}

-- The single price series, chosen from every vendor that reported a bar.
--
-- Grain: one row per (security_id, trading_date). This is the model that
-- RESTORES that grain — int_prices_with_calendar and int_prices_on_raw_basis
-- both carry `source` in their key, and everything downstream of here does not.
--
-- ADR-0006 is the decision this model implements: Polygon wins wherever it has
-- a bar, Yahoo fills gaps and nothing else, and a merged bar comes from exactly
-- ONE vendor and says which.
--
-- The de-adjustment onto a common basis has already happened upstream, in
-- int_prices_on_raw_basis. That ordering is not stylistic: choosing first and
-- de-adjusting after would compare a Polygon print against a Yahoo
-- split-adjusted price, which for KLAC on 2026-06-11 is 2411.64 against
-- 241.164 — a comparison with no meaning.
--
-- ------------------------------------------------------------------------
-- WHY THERE IS NO AVERAGING AND NO FIELD-LEVEL BEST-OF
--
-- A merged bar is one vendor's bar, whole. Blending would produce a row no
-- vendor ever published: unreconcilable against either vendor's own API, and no
-- longer byte-comparable with the Parquet archive ADR-0002 exists to provide.
-- It also has no defensible answer for a bar where the vendors differ by 900%,
-- because the mean of two prices on different bases is not a price.
--
-- The discarded bar is not thrown away. int_source_conflicts keeps every
-- disagreement above tolerance, with both vendors' values side by side.
--
-- ------------------------------------------------------------------------
-- WHY vwap AND trade_count GO SPARSE
--
-- A Yahoo-only bar has NULL vwap and NULL trade_count, because Yahoo supplies
-- neither and ADR-0006 refuses to fabricate them. Any consumer aggregating vwap
-- must now handle NULLs. That is the visible cost of having a fallback, and it
-- is strictly preferable to a fabricated number nothing downstream could catch.

with candidates as (

    select * from {{ ref('int_prices_on_raw_basis') }}

),

-- The priority rule.
--
-- A window function rather than a left join of one source onto another. The
-- join form needs a new branch and a new coalesce per column for every vendor
-- added; this needs one line in the case expression below, which is the
-- "adding a vendor is additive" promise ADR-0008 makes.
--
-- The ordering key is total and deterministic. `source` is the final tiebreak
-- so that two vendors sharing a priority — impossible today, and a
-- configuration error if it ever happened — still yield one stable row rather
-- than an arbitrary one that changes between builds.
ranked as (

    select
        *,
        row_number() over (
            partition by security_id, trading_date
            order by
                case source
                    when 'polygon' then 1
                    when 'yahoo'   then 2
                    -- An unrecognised vendor never outranks a known one. It
                    -- still reaches the mart if it is the only bar for a date,
                    -- which is the correct outcome: better a bar from a vendor
                    -- nobody prioritised than no bar at all, and
                    -- accepted_values on `source` fails the build anyway.
                    else 99
                end,
                source
        ) as source_rank,

        count(*) over (partition by security_id, trading_date) as sources_available

    from candidates

)

select
    security_id,
    ticker,
    trading_date,

    open_price,
    high_price,
    low_price,
    close_price,
    volume,
    vwap,
    trade_count,

    is_exchange_session,

    -- Provenance survives to the mart, so a consumer can always ask which
    -- vendor a bar came from without re-deriving it.
    source,
    sources_available,
    (source <> 'polygon')       as is_fallback_bar,
    (deadjustment_factor <> 1)  as was_de_adjusted,
    deadjustment_factor,
    vendor_reported_close,
    ingested_at

from ranked
where source_rank = 1
