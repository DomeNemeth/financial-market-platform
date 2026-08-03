-- Fails if the two independent cumulative-split-factor implementations disagree.
--
-- ADR-0006 keeps int_splits__cumulative separate from the split leg of
-- int_prices_with_adjustments on purpose — they answer different questions
-- (undo Yahoo's back-adjustment vs. apply ours) and merging them would
-- hard-wire the assumption that the vendors' split histories agree. The price
-- of that decision is two implementations of the same product, and this test is
-- what stops them drifting apart unnoticed.
--
-- It is the same arrangement ADR-0003 already uses for the Python and SQL
-- adjustment code: implement twice, reconcile by test. The reconciliation is
-- what makes the duplication defensible rather than merely tolerated.
--
-- EXACT equality, not a tolerance. Both models compute exp(sum(ln(ratio)))
-- over the same numeric type and round to the same 12 decimal places, and
-- neither takes a vendor value as input — the ratios come from the same
-- raw.corporate_actions rows. There is no float32 anywhere in this comparison
-- and no source of legitimate drift, so any difference at all is a real
-- divergence between the two implementations. `is distinct from` rather than
-- `<>` so a NULL on one side and a value on the other is caught rather than
-- swallowed by three-valued logic.
--
-- The inner join is what scopes this correctly: int_splits__cumulative covers
-- every vendor's bar dates, int_prices_with_adjustments only the merged ones,
-- and the merged set is a subset. Comparing on the overlap is the whole
-- comparable population; a left join would report the surplus rows as failures.

select
    s.security_id,
    s.trading_date,
    s.split_factor as deadjustment_model_factor,
    a.split_factor as adjustment_model_factor,
    s.split_factor - a.split_factor as difference

from {{ ref('int_splits__cumulative') }} s
inner join {{ ref('int_prices_with_adjustments') }} a
    on  a.security_id  = s.security_id
    and a.trading_date = s.trading_date

where s.split_factor is distinct from a.split_factor
