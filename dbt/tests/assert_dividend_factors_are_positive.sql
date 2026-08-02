-- Fails on any dividend whose adjustment factor is not positive.
--
-- The factor is 1 - amount / close_prev_ex, so this fires when a dividend meets
-- or exceeds the previous session's close. That is not necessarily a data error:
-- liquidating distributions and large special dividends genuinely do it, and no
-- CHECK constraint can forbid a true fact about the world.
--
-- It fires because ln() cannot take a non-positive argument, so such a dividend
-- cannot enter the cumulative product at all. int_prices_with_adjustments
-- responds by NULLing total_return_adjusted_close for the bars the broken action
-- would have applied to, which is honest but invisible — nothing in the mart
-- says WHY those rows are NULL. This test is what makes it attributable: it
-- names the security, the ex-date, the amount, and the close that could not
-- absorb it, so the question "why is this security's total-return series NULL"
-- has an answer at build time.
--
-- See the addendum to docs/adr/0003-adjusted-price-methodology.md for why the
-- factor is not clamped to something small and positive instead.

select
    security_id,
    ex_date,
    dividend_amount,
    reference_session_date,
    reference_close,
    1 - (dividend_amount / reference_close) as would_be_factor
from {{ ref('int_corporate_actions__factors') }}
where is_dividend_factor_uncomputable
