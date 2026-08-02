-- Fails if a single raw bar resolved to MORE than one security.
--
-- The complement of assert_every_price_bar_resolves_to_a_security. That test
-- catches a bar reaching no security; this one catches a bar reaching several,
-- which is worse: the LEFT JOIN in int_prices_with_calendar fans the row out and
-- the same day's volume gets counted once per match. Nothing else would notice —
-- the mart's grain test would fail, but only after the duplicate had already
-- propagated, and the message would point at the mart rather than at the cause.
--
-- The trigger is two security master rows sharing a ticker with overlapping
-- valid-time windows. That is exactly the ticker-reuse case ADR-0007 is about,
-- except with the reuse recorded wrongly (windows that overlap rather than
-- abut), and it is the failure the valid-time bound is supposed to prevent.

select
    p.ticker,
    p.trading_date,
    count(*) as resolved_security_count
from {{ ref('stg_polygon__prices') }} p
inner join {{ ref('stg_polygon__security_master') }} sm
    on  sm.ticker = p.ticker
    and p.trading_date >= coalesce(sm.valid_from, '-infinity'::date)
    and p.trading_date <= coalesce(sm.valid_to,   'infinity'::date)
group by 1, 2
having count(*) > 1
