-- Fails if any bar is dated on a day the exchange was shut.
--
-- The complement of assert_no_missing_trading_days: that test finds sessions
-- with no bar, this one finds bars with no session. A price series can be wrong
-- in both directions and only checking one of them is how a timezone bug
-- survives.
--
-- The concrete hazard is the one CLAUDE.md records: Polygon's daily bars are
-- stamped midnight UTC of the trading date, and converting them to Eastern
-- shifts every date back one day, landing Monday's bar on Sunday. That defect
-- produces bars on non-sessions and nothing else in the pipeline notices —
-- raw.prices will store any date it is handed.
--
-- Since ADR-0006 this runs over every vendor at once, and the hazard is now
-- live in BOTH directions rather than hypothetical in one. The two vendors
-- stamp a daily bar differently — Polygon at midnight UTC, Yahoo at the session
-- open in exchange-local time — so the same naive conversion is wrong for each
-- of them in the opposite direction. `source` is reported so a failure names
-- which adapter's date handling drifted.

select
    source,
    security_id,
    ticker,
    trading_date
from {{ ref('int_prices_with_calendar') }}
where not is_exchange_session
