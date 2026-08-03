-- Fails if the point-in-time macro join produces the same answer as the naive
-- one — that is, if all the machinery around publication dates makes no
-- difference to any row in the warehouse.
--
-- THE NON-VACUITY GUARD FOR THE ENTIRE MACRO LAYER.
--
-- assert_macro_context_has_no_lookahead checks that no value arrived early. It
-- cannot check the more embarrassing failure: that the correct join and the
-- naive one agree everywhere, so the publication-date column, the second FRED
-- request that fetches it, the index built for it and the LATERAL that uses it
-- are all elaborate ceremony producing a result a one-line join would have
-- produced.
--
-- If that were true it would still LOOK right. Every row would be defensible,
-- the no-lookahead test would pass, and the model would carry a claim about
-- rigour that its output did not support. This test is what turns "we handle
-- publication lag" from an assertion in a comment into a measured fact about
-- the data, and it fails if the fact stops being true — for instance if price
-- history were re-scoped to a window shorter than the shortest publication lag,
-- where the distinction genuinely stops mattering and the model should not
-- claim otherwise.
--
-- The naive join below is deliberately a faithful reconstruction of the WRONG
-- implementation — ASOF on observation_date — rather than an approximation of
-- it. Comparing against a strawman would prove nothing.

with prices as (

    select security_id, trading_date
    from {{ ref('fct_security_price_daily') }}

),

eligible_series as (

    select series_id
    from {{ ref('dim_macro_series') }}
    where supports_point_in_time_join

),

observations as (

    select series_id, observation_date, value, first_published_date
    from {{ ref('stg_fred__observations') }}
    where value is not null
      and first_published_date is not null

),

-- The WRONG join, reconstructed exactly: most recent observation whose PERIOD
-- began on or before the bar, ignoring whether it had been published.
naive as (

    select
        p.security_id,
        p.trading_date,
        s.series_id,
        m.observation_date as naive_observation_date,
        m.value            as naive_value

    from prices p
    cross join eligible_series s
    left join lateral (
        select o.*
        from observations o
        where o.series_id = s.series_id
          and o.observation_date <= p.trading_date
        order by o.observation_date desc
        limit 1
    ) m on true

),

compared as (

    select
        c.security_id,
        c.trading_date,
        c.series_id,
        c.macro_observation_date,
        c.macro_value,
        n.naive_observation_date,
        n.naive_value

    from {{ ref('fct_security_price_macro_context') }} c
    inner join naive n
        on  n.security_id  = c.security_id
        and n.trading_date = c.trading_date
        and n.series_id    = c.series_id

),

divergences as (

    select count(*) as rows_where_the_joins_disagree
    from compared
    where macro_observation_date is distinct from naive_observation_date

),

-- Reported alongside, so a failure says how badly the guard missed rather than
-- only that it did.
totals as (

    select count(*) as rows_compared from compared

)

select
    d.rows_where_the_joins_disagree,
    t.rows_compared,
    'VACUOUS: the point-in-time macro join never differs from the naive one, '
    || 'so the publication-lag handling changes no row and the model claims a '
    || 'rigour its output does not demonstrate' as reason

from divergences d
cross join totals t

where d.rows_where_the_joins_disagree = 0
  -- Guard on the guard: with no rows compared at all there is nothing to be
  -- vacuous about, and this test would otherwise fail on an empty warehouse for
  -- a reason that has nothing to do with look-ahead bias.
  and t.rows_compared > 0
