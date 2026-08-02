-- Fails on any adjustment factor that is zero, negative, or unexpectedly NULL.
--
-- These are the values that make the adjustment arithmetic meaningless rather
-- than merely wrong, and each fails in a different and quiet way:
--
--   NULL      Prices divide by the factor, so a NULL factor NULLs the entire
--             adjusted series for that bar. The likeliest cause is the one the
--             ADR-0003 addendum calls failure mode 2: sum() over zero rows is
--             NULL, so an unguarded exp(sum(ln(...))) returns NULL for every
--             security with no corporate actions — the MAJORITY case, and one
--             that leaves the interesting securities looking perfectly correct.
--
--   zero      Division by zero. Loud, but only at query time, in whatever
--             consumer happens to hit that row first.
--
--   negative  A sign flip on every price in the series. Nothing raises. A chart
--             renders. This is the one that ships.
--
-- dividend_factor is checked with an explicit carve-out rather than a blanket
-- not-null, because NULL there is a documented and deliberate outcome: a
-- dividend at or above the previous close cannot enter the product, and
-- int_prices_with_adjustments NULLs the affected bars on purpose. That carve-out
-- is exactly as wide as the condition that justifies it — a NULL dividend_factor
-- on a security with no uncomputable dividend is still a failure here, so the
-- exemption cannot be used to hide an unrelated NULL.

with uncomputable_securities as (

    select distinct security_id
    from {{ ref('int_corporate_actions__factors') }}
    where is_dividend_factor_uncomputable

),

violations as (

    select
        f.security_id,
        f.trading_date,
        f.split_factor,
        f.dividend_factor,
        case
            when f.split_factor is null      then 'split_factor is null'
            when f.split_factor <= 0         then 'split_factor is not positive'
            when f.dividend_factor is null   then 'dividend_factor is null with no uncomputable dividend'
            when f.dividend_factor <= 0      then 'dividend_factor is not positive'
        end as reason

    from {{ ref('fct_security_price_daily') }} f
    left join uncomputable_securities u
        on u.security_id = f.security_id

    where f.split_factor is null
       or f.split_factor <= 0
       or f.dividend_factor <= 0
       or (f.dividend_factor is null and u.security_id is null)

)

select * from violations
