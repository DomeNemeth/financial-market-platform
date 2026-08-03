-- Fails if (security_id, trading_date) is not unique in the merged series.
--
-- The grain int_prices_merged exists to restore. Its inputs deliberately carry
-- `source` in their key, so this model is the one place in the DAG where two
-- rows collapse to one, and a bug there — a window partition missing a column,
-- a rank filter dropped — reintroduces the duplicate immediately.
--
-- assert_price_fact_grain_is_unique already checks the same property at the
-- mart. This is not redundant with it: the mart is four models downstream, and
-- a duplicate that first appears here would be reported there as a fact-table
-- problem, pointing at the dimension join rather than at the merge. Failing at
-- the point of collapse names the actual cause.
--
-- It also fires for a reason the mart's test cannot see. Every fallback bar
-- reaching the mart has already passed through the merge, so a merge that
-- silently kept both vendors' rows would double every aggregate downstream of
-- it — including the dividend reference-close join in
-- int_corporate_actions__factors, which is the failure ADR-0006 moved the merge
-- ahead of identity resolution specifically to prevent.

select
    security_id,
    trading_date,
    count(*)                          as row_count,
    string_agg(distinct source, ', ') as sources
from {{ ref('int_prices_merged') }}
group by 1, 2
having count(*) > 1
