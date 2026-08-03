{{
    config(
        materialized='view'
    )
}}

-- Every vendor's bars restated on the raw, unadjusted price basis.
--
-- Grain: one row per (security_id, trading_date, source) — unchanged from
-- int_prices_with_calendar. Nothing is chosen or discarded here; this model
-- only makes the vendors comparable, so that the two models downstream of it —
-- int_prices_merged, which picks a winner, and int_source_conflicts, which
-- reports the disagreement — are comparing like with like.
--
-- Separating this from the merge is what stops the de-adjustment being written
-- twice. It was, briefly, and two copies of an inverted factor is exactly the
-- kind of thing that drifts silently.
--
-- ------------------------------------------------------------------------
-- WHAT "RAW BASIS" MEANS AND WHY EACH VENDOR NEEDS DIFFERENT TREATMENT
--
-- Polygon is fetched with adjusted=false (ADR-0008), so its bars ARE the raw
-- basis. They pass through untouched — not multiplied by a factor of 1, but
-- genuinely untouched, so that a bug in the split factor cannot perturb the
-- primary source. That asymmetry is deliberate and should not be "tidied" into
-- a uniform multiplication.
--
-- Yahoo's chart endpoint back-adjusts for splits and offers no flag to turn it
-- off, so its bars are multiplied back by the factor the vendor removed:
--
--     raw_price(d)  = yahoo_price(d)  x split_factor(d)
--     raw_volume(d) = yahoo_volume(d) / split_factor(d)
--
-- Prices MULTIPLY and volume DIVIDES — the exact inverse of ADR-0003's
-- back-adjustment, which divides prices and multiplies volume. Getting the
-- inversion backwards puts a pre-split KLAC bar at 24.1164 instead of 2411.64:
-- wrong by a factor of 100, and still a number that looks like a price.
--
-- The inversion is also what makes the correction checkable, because it leaves
-- price x volume — the traded notional — invariant across it.

with bars as (

    select * from {{ ref('int_prices_with_calendar') }}

),

split_factors as (

    select * from {{ ref('int_splits__cumulative') }}

),

on_raw_basis as (

    select
        b.security_id,
        b.ticker,
        b.trading_date,
        b.source,
        b.is_exchange_session,
        b.ingested_at,

        case when b.source = 'yahoo' then b.open_price  * f.split_factor else b.open_price  end
            as open_price,
        case when b.source = 'yahoo' then b.high_price  * f.split_factor else b.high_price  end
            as high_price,
        case when b.source = 'yahoo' then b.low_price   * f.split_factor else b.low_price   end
            as low_price,
        case when b.source = 'yahoo' then b.close_price * f.split_factor else b.close_price end
            as close_price,

        case when b.source = 'yahoo' then b.volume / f.split_factor else b.volume end
            as volume,

        b.vwap,
        b.trade_count,

        -- The factor ACTUALLY APPLIED to this row — not the security's split
        -- factor on that date.
        --
        -- The distinction matters and got this wrong once. Polygon bars are
        -- passed through untouched, so nothing was applied to them and this is
        -- 1, even on a date where the security's split factor is 10. Reporting
        -- the split factor here regardless of source made was_de_adjusted true
        -- for nine Polygon bars that had not been de-adjusted at all, which
        -- would have made every consumer of that flag — including the
        -- non-vacuity guard in the reconciliation test — count the wrong rows.
        --
        -- With this definition the column reads as "how far this row moved from
        -- what the vendor said", which is what both the guard and
        -- int_source_conflicts actually want to know.
        case
            when b.source = 'yahoo' then coalesce(f.split_factor, 1)
            else 1
        end as deadjustment_factor,

        -- What the vendor actually said, before any correction. Kept so a
        -- disagreement can always be traced back to the vendor's own number
        -- without re-deriving the factor, and so raw.prices remains auditable
        -- from the model output alone.
        b.close_price  as vendor_reported_close,
        b.volume       as vendor_reported_volume

    from bars b
    left join split_factors f
        on  f.security_id  = b.security_id
        and f.trading_date = b.trading_date

    -- An unresolved bar cannot be merged or compared: the priority rule is per
    -- security, and a NULL security_id would collapse every unresolved ticker
    -- onto one another.
    --
    -- Such bars are NOT silently dropped. They stay visible in
    -- int_prices_with_calendar, where assert_every_price_bar_resolves_to_a_security
    -- fails on them by name. Excluding them here rather than there is what keeps
    -- that test able to report the defect, instead of the defect deleting
    -- itself from the model that would have reported it.
    where b.security_id is not null

)

select * from on_raw_basis
