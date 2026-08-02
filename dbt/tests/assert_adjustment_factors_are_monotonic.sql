-- Fails if a cumulative adjustment factor moves the wrong way through time.
--
-- Both factors are products over actions STRICTLY AFTER the bar, so as the bar
-- date advances, actions leave the product and never join it. That gives each
-- factor a direction, and a factor moving against it means the cumulative
-- product is accumulating in the wrong direction — the classic symptom of
-- computing the running sum inclusive of the wrong side, or of subtracting the
-- running total from itself rather than from the security total.
--
-- The two legs are NOT equally unconditional, and collapsing them into one
-- blanket "factors decrease over time" test would be a false invariant that
-- fires on correct data:
--
-- DIVIDEND — unconditional, non-decreasing.
--   Every dividend factor is 1 - amount/close with amount > 0 (CHECK-enforced in
--   raw.corporate_actions) and amount < close (or the factor would be
--   non-positive, which is NULL'd and tested separately). So every multiplicand
--   is strictly between 0 and 1, and dropping one can only move the product UP,
--   toward 1. No exceptions exist.
--
-- SPLIT — conditional, non-increasing only for forward splits.
--   A forward split has ratio > 1, so dropping it moves the product DOWN. But a
--   REVERSE split (1-for-10: split_to = 1, split_from = 10) has a ratio of 0.1,
--   and dropping that moves the product UP. A reverse split is perfectly legal
--   data — it is what a company facing delisting does — and an unconditional
--   monotonicity test would fail on a correctly-adjusted series the first time
--   one landed. So this leg is scoped to securities whose splits are all
--   forward, and securities with a reverse split are excluded from THIS check
--   while remaining covered by assert_split_factor_changes_only_at_ex_dates,
--   which holds for them too.
--
-- The exclusion is deliberately narrow: it is keyed on the security actually
-- having a reverse split, not on a hardcoded ticker list, so it cannot quietly
-- widen to cover an unrelated bug.

with securities_with_reverse_splits as (

    select distinct security_id
    from {{ ref('int_corporate_actions__factors') }}
    where split_ratio < 1

),

ordered as (

    select
        f.security_id,
        f.trading_date,
        f.split_factor,
        f.dividend_factor,
        lag(f.trading_date)    over w as previous_date,
        lag(f.split_factor)    over w as previous_split_factor,
        lag(f.dividend_factor) over w as previous_dividend_factor,
        (r.security_id is not null)   as has_reverse_split

    from {{ ref('fct_security_price_daily') }} f
    left join securities_with_reverse_splits r
        on r.security_id = f.security_id
    window w as (partition by f.security_id order by f.trading_date)

),

violations as (

    select
        security_id,
        previous_date,
        trading_date,
        previous_split_factor,
        split_factor,
        previous_dividend_factor,
        dividend_factor,
        case
            when split_factor > previous_split_factor
                then 'split_factor increased over time'
            when dividend_factor < previous_dividend_factor
                then 'dividend_factor decreased over time'
        end as reason

    from ordered

    where previous_date is not null
      and (
            (not has_reverse_split and split_factor > previous_split_factor)
            or dividend_factor < previous_dividend_factor
          )

)

select * from violations
