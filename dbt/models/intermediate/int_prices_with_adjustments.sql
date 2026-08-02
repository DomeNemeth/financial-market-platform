{{
    config(
        materialized='view'
    )
}}

-- ADR-0003's adjustment maths, in SQL. The subtlest model in the project.
--
-- Grain: one row per (security_id, trading_date).
--
-- The definition being implemented (ADR-0003):
--
--     split_factor(d)          = Π ratio   for every split with ex_date > d
--     div_factor(d)            = Π (1 - amount / close_prev_ex)  for ex_date > d
--     split_adjusted_price(d)  = raw_price(d)  / split_factor(d)
--     split_adjusted_volume(d) = raw_volume(d) * split_factor(d)
--     total_return_price(d)    = raw_price(d)  / split_factor(d) * div_factor(d)
--
-- The split leg was built and reconciled against the Python reference on its own
-- before the dividend leg was added, so that a disagreement between the two
-- implementations could only be about one of them at a time.
--
-- `> d`, STRICTLY. A bar on the ex-date already trades on the post-split basis,
-- so including it would adjust that day twice. The Python reference calls this
-- "the single easiest thing to get wrong in the whole module" and it is the one
-- boundary the reconciliation test is really checking.
--
-- ------------------------------------------------------------------------
-- How the product is computed. Postgres has no PRODUCT() aggregate, so:
--
--     Π x  =  exp( Σ ln x )
--
-- and a product over "every split AFTER d" is expressed as a total minus an
-- inclusive running sum, both in log space:
--
--     split_factor(d) = exp( Σ_all ln r  −  Σ_{ex_date ≤ d} ln r )
--
-- Subtracting the logs and exponentiating ONCE, rather than dividing two
-- exponentials, is load-bearing: for the most recent bar the two sums are the
-- same value, so the difference is exactly 0 and exp(0) is exactly 1 in
-- Postgres. That is what makes ADR-0003's "the latest bar always equals the raw
-- bar" true by construction instead of true to within rounding.
--
-- Both sums are coalesce'd to 0 — the ADDITIVE identity, which exponentiates to
-- the multiplicative one. Without it a security with no corporate actions gets
-- sum() over zero rows = NULL, and every adjusted price it has becomes NULL.
-- That is the majority case, not an edge case: three of the six securities
-- loaded here have no action inside the price window.
--
-- Full analysis, measured precision, and the failure modes in the addendum to
-- docs/adr/0003-adjusted-price-methodology.md.

with bars as (

    select * from {{ ref('int_prices_with_calendar') }}

),

factors as (

    select * from {{ ref('int_corporate_actions__factors') }}

),

-- The complete product over every action the platform holds for the security,
-- with no date bound.
--
-- Deliberately NOT "the running total at the most recent bar". Those differ
-- when a split has been announced with an ex_date beyond the last bar we hold,
-- and in that case ADR-0003's definition — the product over all splits strictly
-- after the bar — includes it. The Python reference includes it too, so this is
-- also what keeps the two implementations reconcilable. See the closing section
-- of the ADR-0003 addendum.
security_totals as (

    select
        security_id,
        sum(ln(split_ratio)) as total_split_ln,

        -- sum() SKIPS NULLs. A dividend whose factor could not be computed
        -- therefore contributes nothing here and is indistinguishable from a
        -- dividend that contributed a factor of exactly 1 — which is the silent
        -- wrong answer the count beside it exists to prevent. Never remove one
        -- without the other.
        sum(ln(dividend_factor)) as total_dividend_ln,
        count(*) filter (where is_dividend_factor_uncomputable)
            as total_uncomputable_dividends,

        -- The observation cutoff these factors were built from. NULL for a
        -- security with no corporate actions at all, which is the honest value:
        -- its factor of 1 rests on no observation and would not change if one
        -- arrived tomorrow, whereas a factor of 10 is only true as of the moment
        -- the split was last seen.
        max(action_ingested_at) as actions_observed_through

    from factors
    group by 1

),

-- The inclusive running sum: every action with ex_date <= the bar date.
--
-- A join-and-aggregate rather than a window over a union of bars and actions.
-- It is O(bars x actions), the same complexity the Python reference chose and
-- for the same reason: with a handful of actions per security it costs nothing,
-- and it reads as a direct transcription of the definition rather than something
-- you have to trust a frame clause to have got right.
bar_running_totals as (

    select
        b.security_id,
        b.trading_date,
        coalesce(sum(ln(f.split_ratio)), 0)     as running_split_ln,
        coalesce(sum(ln(f.dividend_factor)), 0) as running_dividend_ln,
        count(*) filter (where f.is_dividend_factor_uncomputable)
            as running_uncomputable_dividends

    from bars b
    left join factors f
        on  f.security_id = b.security_id
        and f.ex_date    <= b.trading_date

    group by 1, 2

),

with_factors as (

    select
        b.security_id,
        b.ticker,
        b.trading_date,

        b.open_price,
        b.high_price,
        b.low_price,
        b.close_price,
        b.volume,
        b.vwap,
        b.trade_count,
        b.is_exchange_session,
        b.source,
        b.ingested_at,

        -- Rounded to 12 dp so a two-split chain stores 40.000000000000 rather
        -- than 39.9999999999999999. Presentation, not correctness — see the
        -- addendum, which measures where this stops helping.
        round(
            exp(coalesce(t.total_split_ln, 0) - coalesce(r.running_split_ln, 0)),
            12
        ) as split_factor,

        -- The dividend leg, same construction. NULL when any dividend after this
        -- bar had a non-positive factor: that action's contribution is genuinely
        -- unknown, the chain through it is broken, and every earlier bar's
        -- total-return basis depends on it. Confined to the bars actually
        -- affected — bars after the bad ex-date keep a real factor, because no
        -- broken action lies ahead of them.
        --
        -- NULL rather than a clamped number, per the ADR-0003 addendum: a
        -- total-return price that is wrong by the size of a dividend looks
        -- exactly like one that is right.
        case
            when coalesce(t.total_uncomputable_dividends, 0)
                 - coalesce(r.running_uncomputable_dividends, 0) > 0
                then null
            else round(
                exp(
                    coalesce(t.total_dividend_ln, 0)
                    - coalesce(r.running_dividend_ln, 0)
                ),
                12
            )
        end as dividend_factor,

        t.actions_observed_through

    from bars b
    left join bar_running_totals r
        on  r.security_id  = b.security_id
        and r.trading_date = b.trading_date
    left join security_totals t
        on t.security_id = b.security_id

),

final as (

    select
        *,

        -- Prices divide, volume multiplies. The inversion is what preserves
        -- price x volume across a split, so traded notional stays comparable.
        open_price  / split_factor as split_adjusted_open,
        high_price  / split_factor as split_adjusted_high,
        low_price   / split_factor as split_adjusted_low,
        close_price / split_factor as split_adjusted_close,

        -- NOT rounded to a whole share count. A post-adjustment share count is
        -- genuinely fractional and rounding compounds along a factor chain
        -- (ADR-0003). Note the source column is already integral: staging rounds
        -- the vendor's fractional volume to whole shares, and the fractionality
        -- reintroduced here is the adjustment's, not Polygon's.
        volume * split_factor as split_adjusted_volume,

        -- vwap is a price and adjusts like one. NULL-safe by virtue of being a
        -- division: a NULL vwap stays NULL rather than becoming 0.
        vwap / split_factor as split_adjusted_vwap,

        -- The total-return basis: splits AND cash dividends. The correct input
        -- to any return or performance calculation, and the wrong one for
        -- charting — which is why ADR-0003 refuses to let either exist under the
        -- name `adjusted_close`.
        --
        -- MULTIPLIED by the dividend factor, where the split factor DIVIDES.
        -- Not a typo and not symmetry for its own sake: div_factor is already
        -- built as (1 - amount/close), a number just below 1, so multiplying
        -- lowers historical prices to reflect the cash that came out of them.
        -- Dividing would raise them and invert the sign of every dividend in the
        -- series while leaving the result looking entirely plausible.
        --
        -- NULL propagates through the multiplication on its own, so a broken
        -- dividend chain yields a NULL total-return price and an intact
        -- split-adjusted one beside it. That is the intended outcome: the two
        -- series answer different questions and only one of them depends on the
        -- dividend that could not be computed.
        close_price / split_factor * dividend_factor as total_return_adjusted_close

    from with_factors

)

select * from final
