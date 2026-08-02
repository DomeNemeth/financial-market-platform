-- Fails if a price bar did not land inside its security's valid-time window.
--
-- fct_security_price_daily joins dim_security on security_id AND the valid-time
-- window. Because security_id is a durable surrogate that is never reused, the
-- security_id half of that join can never fail — so if the valid-time half is
-- ever dropped, or the window is wrong, the join still returns a row and nothing
-- looks broken. The dimension columns simply describe a period the bar is not
-- from.
--
-- This test is what gives that join something to fail on. A row here means one
-- of two things, both worth knowing:
--   - the bar is dated outside the security's listed life, which usually means a
--     wrong list_date from the vendor rather than a wrong bar, or
--   - the LEFT JOIN found no dimension row at all, which means the valid-time
--     predicate excluded every candidate.
--
-- Reported together because they have the same consequence — a fact row with no
-- usable reference data — and the columns below distinguish them.

select
    f.security_id,
    f.vendor_ticker,
    f.trading_date,
    f.current_ticker,
    s.valid_from,
    s.valid_to,
    case
        when s.security_id is null then 'no dimension row matched the valid-time window'
        else 'bar falls outside the valid-time window'
    end as reason
from {{ ref('fct_security_price_daily') }} f
left join {{ ref('dim_security') }} s
    on s.security_id = f.security_id
where f.current_ticker is null
   or f.trading_date < coalesce(s.valid_from, '-infinity'::date)
   or f.trading_date > coalesce(s.valid_to,   'infinity'::date)
