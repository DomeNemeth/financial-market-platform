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

---

# Addendum: computing the factor products in SQL

**Date:** 2026-08-02
**Status:** Accepted
**Extends:** the "Decision" section above. Changes none of it.

The decision above defines the factors as *products*. Python has `*`. Postgres
has no `PRODUCT()` aggregate, and none of the SQL standard's window functions
produce one. This addendum records how the product is actually computed, because
the technique has three failure modes that are individually easy to miss and
collectively capable of producing a silently wrong price series — which is the
one outcome ADR-0003 exists to prevent.

Written before the SQL, so the guards below are requirements on the models rather
than a description of whatever they happened to do.

## The technique

A product becomes a sum in log space:

```
Π xᵢ  =  exp( Σ ln xᵢ )
```

so `exp(sum(ln(x)))` is a product aggregate, and it composes with `GROUP BY` and
with window frames exactly as `sum` does.

**Direction.** ADR-0003 needs the product over actions *strictly after* the bar.
An aggregate naturally accumulates over rows *up to and including* the bar. So
the model computes the inclusive running sum forward and inverts it:

```
split_factor(d) = exp( Σ_{all actions} ln r  −  Σ_{ex_date ≤ d} ln r )
                = Π r  for ex_date > d
```

**Subtract in log space, exponentiate once.** The equivalent-looking
`exp(total) / exp(running)` is worse in two ways: it rounds twice instead of
once, and — the reason it actually matters — it loses an exact identity.
`exp(0::numeric)` is exactly `1` in Postgres (verified). For the most recent bar
the two sums are the same value, so their difference is exactly zero and the
factor is exactly `1`. The division form gives `39.9999999999999999 /
39.9999999999999999`, which is *probably* 1 but is not 1 by construction. The
back-adjustment convention above promises that the latest bar equals the raw bar;
this formulation makes that promise hold by arithmetic rather than by luck.

## Failure mode 1 — `ln` of a non-positive number aborts the run

Measured on this Postgres 16:

```
ln(0::numeric)   ERROR:  cannot take logarithm of zero
ln(-2::numeric)  ERROR:  cannot take logarithm of a negative number
```

Loud, not silent — but it aborts the whole `dbt build` with a message naming no
security, no date, and no row. The two arguments are not equally exposed:

**Split ratios are structurally safe.** `raw.corporate_actions` already CHECKs
`split_to > 0 AND split_from > 0`, so `split_to / split_from` cannot be zero or
negative. That constraint was written to stop a split vanishing from the factor
product; it turns out to also be what makes `ln` on the ratio total. Deleting it
would now break the transform layer too.

**Dividend factors are not.** `1 − amount / close_prev_ex` is ≤ 0 exactly when a
dividend meets or exceeds the previous close. That is rare but real — liquidating
distributions and large special dividends do it — and no constraint can forbid it,
because it is a true fact about the world rather than a data error.

**Decision: a non-positive dividend factor is NULL, not clamped, and the
total-return series is NULL for every bar it would have applied to.**

Clamping to a small positive number, or dropping the row from the product, would
both let the build finish with a `total_return_adjusted_close` that is wrong by
roughly the size of the dividend and indistinguishable from a right one. Aborting
would take out the entire warehouse for one security. NULL is the honest value:
it is the same answer ADR-0007 gives for CUSIP, it cannot be mistaken for a
price, and it is confined to the security and the bars actually affected.

Two things make it non-silent: the model carries a running count of
uncomputable dividends after each bar, and `assert_dividend_factors_are_positive`
fails the build listing every offending `(security_id, ex_date, amount,
reference_close)`. The failure is loud, attributable, and does not stop the other
five securities from building.

Note that `sum()` ignores NULLs. That is precisely why the count exists: without
it, a NULL factor would drop out of the product and be replaced by an implicit
1.0 — the silent-wrong-answer path, arrived at by doing nothing.

## Failure mode 2 — the empty product is NULL, not 1

`sum()` over zero rows is NULL, so `exp(sum(ln(x)))` over a security with no
corporate actions is NULL, and every adjusted price derived from it is NULL.

This is the dangerous one, because it hits the *majority* case. Most securities
have no action in any given window: of the six loaded here, three (AAPL, MSFT, V)
have none inside the price window at all. An unguarded implementation produces a
correct-looking series for the interesting securities and an all-NULL one for the
boring ones, which is easy to skim past.

Every log sum is therefore `coalesce(..., 0)` — the additive identity, giving
`exp(0) = 1` exactly. Chosen over `coalesce(exp(...), 1)` so the identity lives
in log space with the rest of the arithmetic, and so a NULL that arrives from
somewhere unexpected still surfaces rather than being papered over at the end.

Guarded by the mart test that factors are never NULL.

## Failure mode 3 — the result is close to the product, not equal to it

`exp(sum(ln(x)))` is not exact. Measured, in `numeric`:

| chain | exact | computed | rel. error |
|---|---|---|---|
| 10:1 (KLAC 2026) | 10 | 10.0000000000000002 | 2e-17 |
| 4:1 then 10:1 (NVDA) | 40 | 39.9999999999999999 | 2e-18 |
| 2:1 × 20 | 1048576 | 1048575.9999999998025063 | 1.9e-16 |

Three consequences:

- **Stay in `numeric`.** `float8` was measured at `10.000000000000002` for the
  same single factor — an order of magnitude worse, and it discards the exactness
  of `exp(0)`. This is the same reason the Python reference is `Decimal`
  throughout; the SQL side must not quietly undo it by casting.
- **Compare with a tolerance, never with `=`.** The reconciliation against the
  Python reference uses a relative tolerance of 1e-9: eight orders of magnitude
  looser than the measured error, so it cannot fail on arithmetic noise, and
  still tight enough that any real disagreement in *method* — an off-by-one on
  `> d` vs `>= d`, a missed action, an inverted ratio — moves the result by
  percent, not by 1e-9, and fails immediately.
- **Stored factors are rounded to 12 dp.** Cosmetic honesty for the realistic
  case: a security with one or two splits then stores `10.000000000000` rather
  than a number that looks like a bug in a query result. It is explicitly *not*
  a correctness guarantee — the 20-split chain above is wrong at the 10th decimal
  and rounding to 12 does not repair it. Real securities have single-digit split
  counts, so this is a presentation choice made with its limit understood, not a
  fix.

The exactness that *is* guaranteed is the one that matters: the identity path.
No actions after a bar → empty sum → `coalesce(...,0)` → `exp(0)` = 1, exactly,
with no rounding anywhere on that path.

## Why not a real product aggregate

`CREATE AGGREGATE product(numeric)` over a `numeric` multiply would be exact,
with no logs and no failure modes 1–3. Rejected: it is DDL that lives outside
dbt's model graph, so it must be created by a migration and kept in sync with a
transform layer that has no reference to it — a dependency dbt cannot see, cannot
test, and cannot rebuild. A fresh clone would fail with "function product does
not exist" at `dbt run` rather than at `migrate`, which is a bad place to learn
about it. The precision cost measured above is 1e-16 relative on a quantity used
to divide prices carrying 6 decimal places; it is not the binding constraint.

Worth revisiting if the platform ever grows a security with a long enough action
chain for the error to reach the 6th decimal, which needs roughly 10⁹ actions.

## What this addendum does not change

The definitions in the Decision section remain authoritative. In particular the
strict `ex_date > d` boundary, the two-named-series rule, the inverse volume
adjustment, and storing factors rather than adjusted prices are all unchanged —
this is a note on *how the product is evaluated*, not on what it is.

One consequence of that authority is worth stating because it looks like a bug:
a split with an `ex_date` after the most recent bar **is** included, so the latest
bar's factor is not 1 during the window between a split's announcement and its
ex-date. That follows from "the product of all actions strictly after the bar",
it matches the Python reference exactly, and the reconciliation test would fail if
SQL diverged. The "latest bar equals the raw bar" property above therefore holds
whenever no action is pending, which is the ordinary case, and not otherwise.
