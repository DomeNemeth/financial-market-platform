{{
    config(
        materialized='view'
    )
}}

-- The cumulative split factor at every bar date, for undoing a VENDOR's
-- back-adjustment.
--
-- Grain: one row per (security_id, trading_date), over the union of every
-- vendor's bar dates.
--
--     split_factor(d) = Π ratio   for every split with ex_date > d
--
-- ------------------------------------------------------------------------
-- THIS DUPLICATES int_prices_with_adjustments. DELIBERATELY. DO NOT MERGE THEM.
--
-- The obvious cleanup — have int_prices_with_adjustments read this model and
-- delete its own copy of the product — would be wrong, because the two are not
-- the same claim:
--
--   int_prices_with_adjustments applies OUR back-adjustment, from the actions
--   this platform holds. It is the answer to "what is this price on a
--   split-adjusted basis".
--
--   This model UNDOES YAHOO'S back-adjustment, which Yahoo applied from the
--   actions YAHOO holds. It is the answer to "what did this price look like
--   before the vendor adjusted it".
--
-- Those two products are equal only if the two vendors' split histories agree.
-- Sharing one implementation would hard-wire that assumption into the DAG,
-- where it could never fail. Keeping them separate makes it a checkable claim,
-- and assert_deadjusted_yahoo_reconciles_to_polygon_raw is what checks it: if
-- Yahoo ever knows a split this platform does not, a de-adjusted Yahoo bar
-- stops matching Polygon's raw bar on a date both cover, and the test names the
-- security and the date. That is the same "implement it twice, reconcile by
-- test" pattern ADR-0003 uses for the Python and SQL adjustment code, adopted
-- for the same reason. See ADR-0006.
--
-- It also cannot read the factors model even if we wanted it to: this feeds
-- int_prices_merged, which feeds int_corporate_actions__factors. Reading the
-- factors here would close the loop and dbt would refuse to build.
--
-- ------------------------------------------------------------------------
-- Reads splits straight from staging rather than from the pivoted factors
-- model, so it depends on no price data at all and can sit upstream of the
-- merge. The exp(sum(ln(...))) form and its mandatory coalesce are ADR-0003's;
-- the addendum explains why the coalesce is load-bearing rather than defensive
-- (sum() over zero rows is NULL, and the majority of securities have no split).

with bars as (

    -- Every (security_id, trading_date) any vendor reported, not just the ones
    -- that survive the merge. The merge has not happened yet, and a Yahoo bar
    -- needs its factor computed BEFORE the priority rule can compare it to
    -- anything.
    select distinct
        security_id,
        trading_date
    from {{ ref('int_prices_with_calendar') }}
    where security_id is not null

),

splits as (

    select
        security_id,
        ex_date,
        split_ratio
    from {{ ref('stg_polygon__corporate_actions') }}
    where action_type = 'split'

),

cumulative as (

    select
        b.security_id,
        b.trading_date,

        -- `> trading_date`, STRICTLY. A bar ON the ex-date already trades on the
        -- post-split basis, so including its own split would correct it twice.
        -- This is the same boundary ADR-0003 calls the single easiest thing in
        -- the module to get wrong, and it is wrong here in the opposite
        -- direction: an off-by-one would leave a Yahoo ex-date bar multiplied by
        -- a factor of 10 while looking entirely plausible beside its neighbours.
        --
        -- Rounded to 12 dp for the same presentational reason as
        -- int_prices_with_adjustments, so a two-split chain reads 40.000000000000
        -- rather than 39.999999999999996.
        round(exp(coalesce(sum(ln(s.split_ratio)), 0)), 12) as split_factor,

        count(s.ex_date) as splits_after_bar

    from bars b
    left join splits s
        on  s.security_id = b.security_id
        and s.ex_date     > b.trading_date

    group by 1, 2

)

select * from cumulative
