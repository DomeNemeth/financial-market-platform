{{
    config(
        materialized='view'
    )
}}

-- Corporate actions reshaped into per-event adjustment factors.
--
-- Grain: one row per (security_id, ex_date). The staging model's grain is
-- (security_id, action_type, ex_date), so a security that pays a dividend and
-- splits on the same ex-date collapses to one row here with both factors set.
-- That is rare but real, and getting it wrong would double-count the date in the
-- cumulative product downstream.
--
-- Every event contributes exactly two multiplicands, both defaulting to 1 (the
-- multiplicative identity) when that action type is absent. A dividend row
-- therefore carries split_ratio = 1, contributing ln(1) = 0 to the split product
-- and leaving it untouched. This is what keeps the two factor chains independent
-- while still living on one row.
--
-- THE ORDERING PROBLEM. ADR-0003's Consequences section flags this model:
-- "the dividend factor needs the prior close, which makes the adjustment depend
-- on the price series as well as the action series — a circular-looking
-- dependency that has to be resolved in a specific model order." That order is
-- resolved here and it is not circular:
--
--     stg_polygon__prices ─→ int_prices_with_calendar ─→ int_corporate_actions__factors
--                                       │                            │
--                                       └────────────────────────────┴─→ int_prices_with_adjustments
--
-- The factors model reads RAW closes from the calendar model. It never reads
-- adjusted ones, which is what would actually make it circular.
--
-- See the addendum to ADR-0003 for why a non-positive dividend factor becomes
-- NULL here rather than being clamped or dropped.

with actions as (

    select * from {{ ref('stg_polygon__corporate_actions') }}

),

prices as (

    select * from {{ ref('int_prices_with_calendar') }}

),

-- One row per (security_id, ex_date), with each action type in its own column.
-- max() is safe rather than arbitrary: raw.corporate_actions is UNIQUE on
-- (security_id, action_type, ex_date, source) and this model reads one source,
-- so each case expression sees at most one non-null value per group. The counts
-- are carried out so assert_one_action_per_type_per_ex_date can prove that
-- rather than leaving it as an assumption in a comment.
pivoted as (

    select
        security_id,
        ex_date,

        max(case when action_type = 'split'    then split_ratio  end) as split_ratio,
        max(case when action_type = 'dividend' then cash_amount  end) as dividend_amount,
        max(case when action_type = 'dividend' then currency     end) as dividend_currency,

        count(*) filter (where action_type = 'split')    as split_row_count,
        count(*) filter (where action_type = 'dividend') as dividend_row_count,

        -- When this event was last observed. Aggregated up the DAG into the
        -- mart's actions_observed_through, which is the `as_of` ADR-0003 asks
        -- the mart to carry: a back-adjusted series is not a fixed object, and
        -- a factor is only meaningful alongside the observation cutoff it was
        -- built from.
        max(ingested_at) as action_ingested_at

    from actions
    group by 1, 2

),

-- The last session STRICTLY BEFORE the ex-date, from the calendar seed.
--
-- Not `ex_date - interval '1 day'`. ADR-0003 is explicit that the reference
-- close is the previous *session*, and this dataset contains a live example of
-- why: JPM's 2026-07-06 ex-date has a previous session of 2026-07-02, because
-- 2026-07-03 is the observed Independence Day holiday and 2026-07-04/05 are the
-- weekend. Date arithmetic would read a bar dated Sunday 2026-07-05, find
-- nothing, and silently drop the dividend from the factor product.
--
-- A correlated max() rather than lag() over the sessions, because it stays
-- correct when the ex_date is not itself a session — which mirrors
-- src.common.calendar.previous_trading_day, whose docstring makes the same
-- promise ("`d` itself need not be a session"). The action counts here are in
-- the dozens, so the correlated scan costs nothing.
with_reference_session as (

    select
        p.*,
        (
            select max(c.session_date)
            from {{ ref('trading_calendar') }} c
            where c.calendar = 'XNYS'
              and c.session_date < p.ex_date
        ) as reference_session_date

    from pivoted p

),

with_reference_close as (

    select
        r.*,
        px.close_price as reference_close

    from with_reference_session r
    left join prices px
        on  px.security_id  = r.security_id
        and px.trading_date = r.reference_session_date

),

final as (

    select
        security_id,
        ex_date,

        -- Split leg. NULL means "no split on this date", which is a factor of 1.
        -- Guaranteed > 0 by raw.corporate_actions' CHECK on split_to/split_from,
        -- which is what makes ln() on it total. See the ADR-0003 addendum.
        coalesce(split_ratio, 1) as split_ratio,

        -- Dividend leg, kept as inputs as well as a factor so the arithmetic is
        -- auditable from the model output alone.
        dividend_amount,
        dividend_currency,
        reference_session_date,
        reference_close,

        case
            -- No dividend on this date: identity.
            when dividend_amount is null
                then 1

            -- A dividend whose reference close we do not hold. ADR-0003 skips it
            -- with no factor applied, "since the alternative is fabricating a
            -- denominator", and the Python reference does exactly that. SQL
            -- matches, or the two implementations disagree by construction.
            -- Made visible rather than silent by
            -- assert_dividend_factors_have_a_reference_close.
            when reference_close is null
                then 1

            -- A dividend at or above the previous close. Not a data error — it
            -- is what a liquidating distribution looks like — but the factor is
            -- non-positive and ln() cannot take it. NULL, never clamped: see the
            -- ADR-0003 addendum for why a wrong-but-plausible number is the worse
            -- outcome. assert_dividend_factors_are_positive fails on this.
            when 1 - (dividend_amount / reference_close) <= 0
                then null

            else 1 - (dividend_amount / reference_close)
        end as dividend_factor,

        -- Carried so the downstream cumulative product can tell "this dividend
        -- contributed a factor of 1" apart from "this dividend could not be
        -- computed and sum() silently ignored its NULL". Those two look identical
        -- inside an aggregate and mean opposite things.
        (dividend_amount is not null and reference_close is null)
            as is_reference_close_missing,

        (
            dividend_amount is not null
            and reference_close is not null
            and 1 - (dividend_amount / reference_close) <= 0
        ) as is_dividend_factor_uncomputable,

        split_row_count,
        dividend_row_count,
        action_ingested_at

    from with_reference_close

)

select * from final
