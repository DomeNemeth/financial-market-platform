{{ config(severity = 'warn') }}

-- Warns on any dividend the platform holds no reference close for.
--
-- ADR-0003 decides that such a dividend is skipped with no factor applied,
-- "since the alternative is fabricating a denominator", and the Python reference
-- implements exactly that. The SQL matches it deliberately — the two
-- implementations must agree — so this is not a defect in either.
--
-- WARN, not ERROR, and that severity is the whole point of the test. The normal
-- cause is entirely benign: corporate actions are ingested from 2020 while
-- prices cover a few weeks, so most historical dividends have no bar behind
-- them. Erroring would fail every build on a condition the ADR already accepted,
-- and the test would be deleted within a week.
--
-- What it buys is that "skipped silently" becomes "skipped visibly". A dividend
-- inside the price window with no reference close is a different and real
-- problem — a missing bar next to an ex-date, which is precisely the case
-- assert_no_missing_trading_days is about — and it would otherwise reduce the
-- total-return factor by exactly the amount of the dropped dividend with nothing
-- anywhere reporting it. The warning count is the signal: it should be stable
-- and should only ever shrink as price history is backfilled.

select
    security_id,
    ex_date,
    dividend_amount,
    reference_session_date
from {{ ref('int_corporate_actions__factors') }}
where is_reference_close_missing
