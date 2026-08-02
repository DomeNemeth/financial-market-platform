-- Fails if (security_id, trading_date) is not unique in the price fact.
--
-- The mart's stated grain, checked rather than asserted in a docstring. A
-- duplicate here is not a cosmetic problem: every downstream aggregate
-- double-counts, a return series gets a zero-day inserted into it, and the API
-- returns two bars for one date.
--
-- The realistic cause is a fan-out in one of the two valid-time joins — the
-- ticker resolution in int_prices_with_calendar, or the dimension join here —
-- caused by overlapping security master windows. Those have their own dedicated
-- tests upstream, which name the cause; this one is the backstop that catches
-- any fan-out, including one introduced by a join nobody has thought to write a
-- test for yet.
--
-- Deliberately not the built-in `unique` test on a concatenated key: that would
-- need a surrogate column existing purely to be tested, and the failure message
-- would name the hash rather than the security and date.

select
    security_id,
    trading_date,
    count(*) as row_count
from {{ ref('fct_security_price_daily') }}
group by 1, 2
having count(*) > 1
