-- Fails if a de-adjusted Yahoo bar does not match Polygon's raw bar for the
-- same security and date.
--
-- THIS IS THE TEST THAT HOLDS UP ADR-0006. The merge rests on one claim — that
-- multiplying a Yahoo bar by the cumulative split factor recovers the
-- unadjusted print — and on the assumption underneath it, that the two vendors
-- agree about the split history. Neither is checkable anywhere else.
--
-- Every date both vendors cover is a free oracle for that claim: Polygon
-- supplies the answer, Yahoo supplies the input, and the correction either
-- lands on it or does not. It costs nothing, because the overlap exists anyway
-- as a by-product of the fallback having to be loaded before it can be used.
--
-- ------------------------------------------------------------------------
-- WHY int_splits__cumulative IS NOT SHARED WITH int_prices_with_adjustments
--
-- Because of this test. The two models compute the same product for different
-- reasons — one undoes Yahoo's back-adjustment, the other applies ours — and
-- they agree only if the two vendors' split histories agree. Sharing the code
-- would make that agreement true by construction and untestable. Keeping them
-- separate means a split Yahoo knows about and this platform does not shows up
-- here, on the overlap, as a bar that no longer reconciles. See ADR-0006.
--
-- ------------------------------------------------------------------------
-- TOLERANCES: 1e-6 on the close, 1e-4 on open/high/low. Both measured over the
-- 258 overlapping bars in this warehouse, not taken from vendor documentation.
--
-- The two numbers differ because the two quantities do. Measured maxima:
--
--     close             5.4e-8      open/high/low   up to 2.8e-5
--
-- CLOSE is the official closing print. Both vendors take it from the primary
-- exchange, so they agree exactly and the only residual is Yahoo's float32
-- representation — 306.309998 against Polygon's 306.31, about seven significant
-- decimal digits. 1e-6 clears the observed 5.4e-8 by more than an order of
-- magnitude.
--
-- OPEN, HIGH and LOW are not the same quantity across vendors. They depend on
-- which prints are consolidated — odd lots, off-exchange trades, opening
-- auction handling — and the vendors genuinely differ. KLAC's 2026-07-30 low is
-- 178.855 at Polygon and 178.86 at Yahoo: half a cent apart, far too large to
-- be float32 and not a defect in either. An earlier version of this test used
-- 1e-6 for all four fields, having measured only the close, and failed 79 of
-- 258 bars for exactly this reason.
--
-- Both tolerances remain absurdly tight against what this test exists to catch.
-- A de-adjustment error is a wrong SPLIT FACTOR, so it is a factor of 10 or
-- 100 — five to six orders of magnitude above even the loose bound. Widening
-- open/high/low to 1e-4 costs no real sensitivity; it just stops the test
-- reporting a vendor disagreement as a basis error.
--
-- Volume is deliberately not checked here at all. The two vendors measure
-- different tapes and disagree by up to 4.4e-3; int_source_conflicts reports
-- that at its own tolerance. Folding it in would make this test fail for a
-- reason that has nothing to do with the de-adjustment.

{% set close_tolerance = 0.000001 %}
{% set intraday_tolerance = 0.0001 %}

with polygon_bars as (

    select security_id, ticker, trading_date, close_price, open_price,
           high_price, low_price, deadjustment_factor
    from {{ ref('int_prices_on_raw_basis') }}
    where source = 'polygon'

),

yahoo_bars as (

    select security_id, trading_date, close_price, open_price,
           high_price, low_price, deadjustment_factor, vendor_reported_close
    from {{ ref('int_prices_on_raw_basis') }}
    where source = 'yahoo'

),

comparisons as (

    select
        p.security_id,
        p.ticker,
        p.trading_date,
        p.close_price as polygon_close,
        y.close_price as yahoo_deadjusted_close,
        y.vendor_reported_close as yahoo_reported_close,
        y.deadjustment_factor,

        abs(p.close_price - y.close_price) / nullif(abs(p.close_price), 0) as close_rel_diff,
        abs(p.open_price  - y.open_price)  / nullif(abs(p.open_price),  0) as open_rel_diff,
        abs(p.high_price  - y.high_price)  / nullif(abs(p.high_price),  0) as high_rel_diff,
        abs(p.low_price   - y.low_price)   / nullif(abs(p.low_price),   0) as low_rel_diff

    from polygon_bars p
    inner join yahoo_bars y
        on  y.security_id  = p.security_id
        and y.trading_date = p.trading_date

),

violations as (

    select
        security_id,
        ticker,
        trading_date,
        'de-adjusted yahoo bar does not match polygon raw' as reason,
        format(
            'factor=%s yahoo_reported=%s yahoo_deadjusted=%s polygon=%s '
            || 'close_rel_diff=%s open_rel_diff=%s high_rel_diff=%s low_rel_diff=%s',
            deadjustment_factor, yahoo_reported_close,
            yahoo_deadjusted_close, polygon_close,
            close_rel_diff, open_rel_diff, high_rel_diff, low_rel_diff
        ) as detail

    from comparisons
    where coalesce(close_rel_diff, 0) > {{ close_tolerance }}
       or coalesce(open_rel_diff,  0) > {{ intraday_tolerance }}
       or coalesce(high_rel_diff,  0) > {{ intraday_tolerance }}
       or coalesce(low_rel_diff,   0) > {{ intraday_tolerance }}

),

-- NON-VACUITY GUARD.
--
-- Every bar in this warehouse whose de-adjustment factor is 1 reconciles
-- trivially: the correction multiplied by one and changed nothing. If the
-- overlap ever contained only such bars, this test would pass while proving
-- nothing whatsoever about the de-adjustment — and it would keep passing if
-- int_prices_on_raw_basis dropped the multiplication entirely.
--
-- So the absence of a real correction to check is itself a failure. Today the
-- guard is satisfied by KLAC's nine pre-split sessions in the June overlap,
-- where the factor is 10. If price history is ever re-scoped so that no
-- de-adjusted bar overlaps both vendors, this fails and says so, rather than
-- going quietly green.
coverage as (

    select count(*) filter (where deadjustment_factor <> 1) as de_adjusted_comparisons
    from comparisons

)

select * from violations

union all

select
    cast(null as bigint) as security_id,
    cast(null as varchar) as ticker,
    cast(null as date) as trading_date,
    'VACUOUS: no de-adjusted bar was compared against polygon' as reason,
    format(
        'the two vendors overlap, but every overlapping bar had a split factor '
        || 'of 1, so this test proved nothing. de_adjusted_comparisons=%s',
        de_adjusted_comparisons
    ) as detail
from coverage
where de_adjusted_comparisons = 0
