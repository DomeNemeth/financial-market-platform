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
--
-- ------------------------------------------------------------------------
-- Counts rows in int_prices_with_calendar itself rather than re-deriving the
-- join against the security master, which is what this test used to do.
--
-- Two reasons, both prompted by ADR-0006 unioning the vendors into that model.
-- First, the re-derivation was Polygon-shaped — it read stg_polygon__prices, so
-- a fan-out on a Yahoo bar would have gone unreported and every new vendor
-- would have owed a copy of this test. Second, a re-derivation only ever tests
-- the copy of the join written here; it passes happily while the real join in
-- the model does something else. Counting the model's own output tests the join
-- that actually runs.
--
-- `source` belongs in the grouping because it is part of the model's declared
-- grain. Two vendors reporting the same date is normal, and is precisely what
-- int_prices_merged exists to resolve — omitting it would make this test fire
-- on every date both vendors cover.

select
    p.source,
    p.ticker,
    p.trading_date,
    count(*) as resolved_security_count
from {{ ref('int_prices_with_calendar') }} p
where p.security_id is not null
group by 1, 2, 3
having count(*) > 1
