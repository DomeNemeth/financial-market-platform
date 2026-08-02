-- Fails if the split factor changes between two bars with no split in between.
--
-- The unconditional companion to assert_adjustment_factors_are_monotonic, which
-- can only check direction and has to exempt reverse splits to stay true. This
-- one holds for every security without exception, including reverse splits, and
-- it is the stronger statement: the cumulative factor is a step function whose
-- breakpoints are EXACTLY the ex-dates.
--
-- Monotonicity alone would not catch a factor that drifts a little on every bar
-- — the signature of the cumulative product being recomputed per row against a
-- moving window rather than a fixed security total. That drift is monotonic, so
-- the direction test passes, and the values look plausible right up until a
-- return series computed off them is quietly wrong everywhere instead of
-- obviously wrong somewhere.
--
-- Reads the actions from the factors model rather than from raw, so it also
-- fails if the pivot in int_corporate_actions__factors ever loses an event: an
-- action that vanished there would still move the factor, and the interval
-- lookup below would find nothing to justify the move.

with ordered as (

    select
        security_id,
        trading_date,
        split_factor,
        lag(trading_date) over w as previous_date,
        lag(split_factor) over w as previous_split_factor
    from {{ ref('fct_security_price_daily') }}
    window w as (partition by security_id order by trading_date)

),

changes as (

    select *
    from ordered
    where previous_date is not null
      and split_factor is distinct from previous_split_factor

),

unexplained as (

    select
        c.security_id,
        c.previous_date,
        c.trading_date,
        c.previous_split_factor,
        c.split_factor
    from changes c
    where not exists (
        select 1
        from {{ ref('int_corporate_actions__factors') }} f
        where f.security_id = c.security_id
          -- The factor for bar d excludes splits with ex_date <= d, so the
          -- factor changes between prev and d exactly when a split has
          -- prev < ex_date <= d. Both bounds matter: `>=` on the left would
          -- excuse a change by a split already excluded from both factors.
          and f.ex_date >  c.previous_date
          and f.ex_date <= c.trading_date
          and f.split_ratio <> 1
    )

)

select * from unexplained
