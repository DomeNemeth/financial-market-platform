-- Fails if a security has more than one split, or more than one dividend, on a
-- single ex-date.
--
-- int_corporate_actions__factors pivots the action list with
-- `max(case when action_type = 'split' then split_ratio end)`. That is only
-- correct if at most one row can match per group. It currently is — raw is
-- UNIQUE on (security_id, action_type, ex_date, source) and the model reads a
-- single source — but max() over a group that grew a second row would silently
-- discard one and produce an adjustment factor that is wrong by the size of the
-- dropped action, with nothing anywhere raising.
--
-- This test is what turns that from an assumption in a comment into a checked
-- invariant, and it is what will fail first if a second vendor is ever unioned
-- into the staging model in violation of ADR-0008.

select
    security_id,
    ex_date,
    split_row_count,
    dividend_row_count
from {{ ref('int_corporate_actions__factors') }}
where split_row_count > 1
   or dividend_row_count > 1
