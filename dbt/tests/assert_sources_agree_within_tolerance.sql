{{ config(severity = 'warn') }}

-- Warns when two vendors covered the same bar and disagreed about it.
--
-- The whole of the logic lives in int_source_conflicts, for the reason ADR-0006
-- gives: a warn-test alone emits a count that nothing survives, so the rows are
-- gone the moment the build finishes and the disagreement can never be looked
-- at afterwards. The model keeps them; this test is the build-time signal over
-- the top of it. Selecting from the model rather than re-deriving the
-- comparison also means the numbers reported here and the numbers a human can
-- query are the same numbers by construction.
--
-- WARN, not ERROR, on the same reasoning as
-- assert_dividend_factors_have_a_reference_close. Two vendors disagreeing about
-- volume is a fact about the world — consolidated tape against primary
-- exchange, whole shares against fractional, a late correction one has applied
-- and the other has not — and a test that fails every build on an accepted
-- condition gets deleted within a week.
--
-- A CLOSE conflict is a different matter and is worth investigating. Both
-- vendors take the closing print from the primary exchange, so they agree to
-- 5.4e-8 across all 258 overlapping bars here — pure float32 residue — against
-- a 1e-6 tolerance. Nothing normal reaches it. It stays a warning for now only
-- because the count is zero and there is nothing yet to escalate; ADR-0006
-- records that if a close conflict ever appears, the right response is to split
-- this into two tests and error on that leg. int_source_conflicts already
-- exposes has_close_conflict separately so that split costs one line.
--
-- The count is the signal. It should be stable and attributable — every row
-- here should have a known cause — and a sudden change means a vendor changed
-- something.

select
    security_id,
    ticker,
    trading_date,
    source_a,
    source_b,
    has_close_conflict,
    has_intraday_conflict,
    has_volume_conflict,
    max_price_rel_diff,
    volume_rel_diff,
    close_a,
    close_b
from {{ ref('int_source_conflicts') }}
