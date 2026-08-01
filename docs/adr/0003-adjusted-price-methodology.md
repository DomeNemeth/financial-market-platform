# ADR-0003: Adjusted price methodology

**Date:** 2026-08-01
**Status:** Accepted

## Context

A raw price series is discontinuous across corporate actions. NVDA closed at
$1,208.88 on 2024-06-07 and opened around $120 on 2024-06-10 after its 10-for-1
split. Nothing happened to anyone's wealth; the series is simply not comparable
across that boundary. Computing a return over it gives -90%, which is not a
number anyone should ship.

The platform requests `adjusted=false` from Polygon deliberately (ADR-0008: raw
stays raw), so producing a comparable series is our job. That requires deciding
four things, each of which vendors quietly answer differently:

- **Which actions adjust.** Splits only, or splits and dividends?
- **Which direction.** Rewrite history to today's basis, or the reverse?
- **What happens to volume.** Prices halving on a 2:1 split without volume
  doubling makes traded notional wrong.
- **What "adjusted" means at a point in time.** A back-adjusted series is not a
  fixed object: it changes retroactively every time a new corporate action
  occurs. The adjusted close for 2020-01-02 is a different number before and
  after the 2024 split.

That last point is the one that turns into look-ahead bias, and it is almost
never made explicit.

## Decision

**Two named series, never one ambiguous "adjusted".**

- `split_adjusted_*` — splits only. The correct basis for charting, technical
  work, and anything comparing price levels.
- `total_return_adjusted_*` — splits and cash dividends. The correct basis for
  performance and return calculations.

Nothing in the platform is called simply `adjusted_close`. The ambiguity in that
name is the entire problem.

**Back-adjustment.** History is restated onto the most recent basis, so the
latest bar always equals the raw bar. This is the near-universal convention and
means the newest prices — the ones a reader recognises — need no explanation.

**Cumulative factors are the product of all actions strictly after the bar.**
For a bar on date `d`:

```
split_factor(d)  = Π  ratio            for every split with ex_date > d
div_factor(d)    = Π (1 - amount / close_prev_ex)   for every dividend with ex_date > d

split_adjusted_price(d)        = raw_price(d)  / split_factor(d)
split_adjusted_volume(d)       = raw_volume(d) * split_factor(d)
total_return_adjusted_price(d) = raw_price(d)  / split_factor(d) * div_factor(d)
```

`> d`, not `>= d`: a bar *on* the ex-date already trades on the new basis.

`close_prev_ex` is the close on the last trading day before the ex-date, taken
from the trading calendar rather than by subtracting one day — the day before an
ex-date is frequently a weekend or holiday.

**Volume is adjusted inversely to price**, so price × volume is preserved.

**Adjusted volume is not rounded to an integer.** Post-adjustment share counts are
genuinely fractional, and rounding compounds across a long factor chain.

**Point-in-time is handled by storing factors, not adjusted prices.** The mart
stores raw OHLCV alongside the cumulative factors and the `as_of` date the
factors were computed for. An adjusted series is always derived, and always
carries the date its basis was computed on. A consumer asking for a series as
known on some past date gets factors built only from actions observed by then —
which composes with the system-time axis in ADR-0004.

**The maths is implemented twice, on purpose.** `src/transforms/adjusted_prices.py`
is a pure-Python reference implementation, unit-tested against hand-computed
fixtures and one real split. dbt SQL implements it again for the pipeline. The
two are reconciled by a test. This is a deliberate exception to ADR-0008's
"transform in dbt" rule: the maths is subtle enough that a readable, directly
unit-testable version is worth the duplication, and disagreement between the two
is a real signal.

## Consequences

Good:

- No consumer can accidentally use a dividend-adjusted series for charting or a
  split-only series for return calculation, because neither is available under a
  name that would let them.
- Storing factors rather than adjusted prices means a new corporate action does
  not require rewriting historical rows — only recomputing factors.
- Preserving raw alongside adjusted keeps the mart reconcilable against the
  Parquet archive.
- The dual implementation gives a genuine cross-check on the subtlest logic in
  the project.

Bad:

- More columns, and consumers must choose. That choice is the point, but it is
  a real ergonomic cost over a single `adjusted_close`.
- The dividend factor needs the prior close, which makes the adjustment depend
  on the price series as well as the action series — a circular-looking
  dependency that has to be resolved in a specific model order.
- Two implementations can drift. Mitigated by a reconciliation test, but the
  cost is real and is why this is scoped as an exception rather than a pattern.
- Correctness is bounded by corporate-action completeness. A missing split
  produces a silently wrong series. The reconciliation test against Polygon's
  own adjusted close is the guard, and it only covers tickers actually tested.

Neutral:

- Return-of-capital, spin-offs, mergers, and rights issues are **not** handled.
  Only splits and cash dividends are. This is a stated scope limit, not an
  oversight; spin-offs in particular need the child security's price to adjust
  correctly and so need the security master to model the relationship first.

## Alternatives Considered

**Use Polygon's `adjusted=true` and store what they send.** Far less work and
probably more accurate for edge cases. Rejected because the vendor will not say
which actions they applied or on what basis, the adjustment silently changes
under us as new actions occur, cross-vendor reconciliation becomes impossible,
and there is nothing to test. It is retained in one place with real value: as the
*oracle* for the reconciliation test, which is a much better use of it.

**Forward-adjustment.** Fix the earliest bar and scale later prices up. History
never changes, which is genuinely attractive for reproducibility. Rejected
because recent prices then bear no resemblance to quoted prices — a $10,000 AAPL
in 2026 — and every consumer would have to un-adjust to display anything.

**Store adjusted prices materialised, recomputing on each action.** Simpler to
query. Rejected because it destroys point-in-time reproducibility: after a
rewrite there is no record of what the series looked like before, which is the
same failure ADR-0004 exists to prevent.

**Single `adjusted_close` with a configurable convention.** Rejected — it moves
the ambiguity into configuration, where it is even easier to get wrong and much
harder to see in a query result.
