-- Fails if any macro value attached to a price bar had not been published yet.
--
-- The single invariant fct_security_price_macro_context exists to guarantee. If
-- it does not hold, every number in that model is a number the market did not
-- have, and any backtest built on it is wrong in the flattering direction.
--
-- Structurally this should be impossible — the LATERAL bounds on
-- first_published_date <= trading_date — which is exactly why it is worth
-- asserting. Changing that one predicate to observation_date is a plausible
-- "simplification" (it reads more naturally, and observation_date is the column
-- a reader thinks of as "the date"), it makes the model faster, and it produces
-- output that looks entirely correct: every row still has a sensible value, the
-- grain is unchanged, nothing is NULL that was not NULL before. The bias is
-- invisible in the data and shows up only as a backtest that works.
--
-- The reported columns are chosen so a failure is diagnosable without a second
-- query: the lag tells you how far into the future the leak reached, which
-- immediately distinguishes an off-by-one from a wholesale swap to the wrong
-- column.

select
    security_id,
    vendor_ticker,
    trading_date,
    series_id,
    macro_observation_date,
    macro_first_published_date,
    macro_value,
    (macro_first_published_date - trading_date) as days_of_lookahead,
    'macro value attached before it was published' as reason

from {{ ref('fct_security_price_macro_context') }}

where macro_first_published_date is not null
  and macro_first_published_date > trading_date
