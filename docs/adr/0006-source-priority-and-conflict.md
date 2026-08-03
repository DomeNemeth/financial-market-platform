# ADR-0006: Source priority and conflict resolution

**Date:** 2026-08-03
**Status:** Accepted

## Context

Until Phase 5 the platform held one price vendor, so "which bar is the bar" was
not a question anyone had to answer. ADR-0008 anticipated it — staging is
strictly per-source, and cross-source merging is reserved for the intermediate
layer — but reserved a home without putting anything in it. The moment a second
vendor lands, `raw.prices` holds two rows for the same
`(ticker, trading_date)` and something downstream has to choose.

The choice cannot be deferred to the mart. `fct_security_price_daily` declares a
grain of one row per `(security_id, trading_date)` and
`assert_price_fact_grain_is_unique` enforces it, so an unmerged second source
does not degrade the mart gracefully — it fails the build. That is the correct
behaviour and it is also why the merge has to be designed rather than discovered.

### The two sources do not report the same quantity

This was measured before the rule was written, against the six securities and 43
sessions already in the warehouse, not assumed from the vendors' documentation.

Polygon is fetched with `adjusted=false` (ADR-0008, "raw stays raw"), so its bars
are the unadjusted prints. **Yahoo has no such flag.** Its
`/v8/finance/chart` endpoint returns a series that is already back-adjusted for
splits as of the moment of the fetch. KLAC's 10-for-1 split on 2026-06-12 makes
this exact rather than approximate:

| trading_date | polygon close | yahoo close | ratio | polygon volume | yahoo volume |
|---|---|---|---|---|---|
| 2026-06-11 | 2411.64 | 241.164001 | **10.0** | 1,769,402 | 17,694,000 |
| 2026-06-12 | 254.54 | 254.539993 | 1.0 | 10,069,200 | 10,056,600 |

AAPL and JPM — no splits in the window — agree at a ratio of 1.0 throughout.
The disagreement is not noise, and it is not Yahoo being wrong. Both series are
internally correct; they are stated on **different price bases**, and the basis
Yahoo uses is a function of when it was fetched.

Three further properties fell out of the same comparison and each bears on the
priority rule:

1. **Yahoo's `close` is split-adjusted but not dividend-adjusted.** Its separate
   `adjclose` array differs from `close` only for JPM, the one security with a
   dividend in the window. So Yahoo ships two of ADR-0003's three series and
   neither of them is the raw one this platform stores.
2. **Yahoo returns float32.** `306.309998` where Polygon returns `306.31`. That
   is ~7 significant decimal digits, against the `NUMERIC` Polygon supplies and
   the `Decimal`-throughout guarantee ADR-0003 makes.
3. **Yahoo's volume is not the same number.** It is whole-share where Polygon
   reports fractional (`7,359,000` against `736,101.1 × 10`), and the most recent
   bar in any fetch disagrees by ~0.1–0.2% — a late tape correction Polygon has
   applied and Yahoo has not yet, or vice versa.

### Why "just pick one" is not enough

A naive priority rule — Polygon where present, Yahoo otherwise — is wrong in a
specific and quiet way. A Yahoo bar filling a gap *before* a split enters
`int_prices_with_adjustments`, which divides by the cumulative split factor to
produce `split_adjusted_close`. But Yahoo already divided. The bar gets adjusted
twice, by a factor of 100 on KLAC, and every test in the suite still passes: the
grain is unique, the factor is a clean step function, the series is monotonic,
the bar resolves to exactly one security. Nothing in the project as it stands
would catch it.

## Decision

### 1. Polygon is primary. Yahoo is fallback only.

For every `(security_id, trading_date)`, the Polygon bar wins whenever one
exists. A Yahoo bar is used **only** where Polygon has none. There is no
averaging, no field-level cherry-picking, and no "best of both" — a merged bar
comes from exactly one vendor and says which.

Polygon is primary on three stated grounds, in order of weight:

- **It is the only source of the quantity this platform stores.** ADR-0002 and
  ADR-0008 make `raw` faithful to the vendor and make adjustment ours and
  auditable. Polygon's `adjusted=false` series is that quantity directly; Yahoo's
  has to be reconstructed.
- **Precision.** `NUMERIC` against float32, and ADR-0003's decimal guarantee
  holds along a factor chain only if the input is exact.
- **Field coverage.** Polygon supplies `vwap` and `trade_count`; Yahoo supplies
  neither.

The revisit trigger is stated so this does not become permanent by inertia: if
Polygon's free-tier 2-year aggregate cap (see Known Issues) becomes the binding
constraint on the platform's history, the correct response is a paid tier or a
third vendor, **not** a promotion of Yahoo — because promoting Yahoo would change
the basis of the primary series, not merely its provenance.

### 2. A Yahoo bar is de-adjusted onto the raw basis before it is merged.

Yahoo's series is multiplied back by the cumulative split factor that Yahoo
itself removed, so that the merged column means the same thing on every row
regardless of which vendor supplied it. Prices multiply and volume divides — the
inverse of the back-adjustment in ADR-0003:

```
raw_price(d)  = yahoo_price(d)  × split_factor(d)
raw_volume(d) = yahoo_volume(d) / split_factor(d)
```

where `split_factor(d)` is the product of every split ratio with `ex_date > d`,
exactly as ADR-0003 defines it.

**This is a second, independent implementation of that product, and that is
deliberate.** It lives in `int_splits__cumulative`, computed from
`raw.corporate_actions` alone; `int_prices_with_adjustments` keeps its own,
computed from `int_corporate_actions__factors`. They are not shared code, because
they are not the same claim:

- `int_prices_with_adjustments` applies **our** back-adjustment, from the actions
  we hold.
- `int_splits__cumulative` undoes **Yahoo's** back-adjustment, which Yahoo applied
  from the actions *Yahoo* holds.

Those two products are equal only if the two vendors' split histories agree.
Sharing one implementation would hard-wire that assumption; keeping them separate
makes it a checkable claim, and
`assert_deadjusted_yahoo_reconciles_to_polygon_raw` is what checks it. If Yahoo
ever knows a split the platform does not, the de-adjusted bar stops matching
Polygon's raw bar and the test names the security and date. This is the same
"implement it twice, reconcile by test" pattern ADR-0003 already uses for the
Python and SQL adjustment code, applied for the same reason.

### 3. The merge model is `int_prices_merged`, and it sits after identity resolution.

The DAG ADR-0008 reserved a home for, now filled:

```
stg_polygon__prices ─┐
stg_yahoo__prices   ─┴─→ int_prices_with_calendar   grain: (security_id, trading_date, source)
                                    │
stg_polygon__corporate_actions ─────┼──→ int_splits__cumulative
                                    │              │
                                    └──────────────┴──→ int_prices_merged   grain: (security_id, trading_date)
                                                              │
                                                              ├──→ int_source_conflicts
                                                              │
                                                              ├──→ int_corporate_actions__factors
                                                              │              │
                                                              └──────────────┴──→ int_prices_with_adjustments
```

**The merge happens *after* identity resolution, not before it**, and that
ordering is forced rather than aesthetic. `int_corporate_actions__factors`
resolves a dividend's reference close by joining prices on
`(security_id, trading_date)`. If it read a model still carrying one row per
source, that join would fan out and every dividend factor would be counted once
per vendor — silently doubling the dividend leg of the product. So the model the
factors read must already be merged, and merged models must therefore be
`security_id`-keyed.

The consequence is that `int_prices_with_calendar` becomes source-agnostic: it
unions the staging models, and its grain gains `source`. This is a net gain
beyond the merge, because the identity resolution and its two tests
(`assert_every_price_bar_resolves_to_a_security`,
`assert_price_bars_resolve_to_one_security`) are now written once and cover every
vendor, rather than being Polygon-shaped with a second copy owed for each new
source.

The alternative — merging on `(ticker, trading_date)` before resolution, which
would have left `int_prices_with_calendar` untouched — was rejected for a second
reason as well as the fan-out: the de-adjustment needs split ratios, which are
keyed on `security_id`, and reaching them by ticker is precisely the join
ADR-0007 exists to forbid.

### 4. Disagreement is a queryable model, not only a test.

Where both vendors supply a bar, the merge discards one of them. The discarded
bar is not thrown away silently: `int_source_conflicts` retains one row per
`(security_id, trading_date)` where the sources disagree by more than a stated
tolerance, carrying both values and the relative difference.

**A model, not just a `severity: warn` test.** A warn-test emits a count into
build output and nothing survives it — the rows are gone the moment the build
finishes, so "did Yahoo and Polygon disagree about AAPL last Tuesday" is not a
question anyone can answer afterwards. A model makes the disagreement a
first-class artefact that can be queried, trended, and eventually served. The
warn-test then simply selects from it, so the build still reports the count and
the count is still the signal.

Tolerances are set from the measurements above, and are deliberately different
per field because the two fields disagree for different reasons:

| field | tolerance | why |
|---|---|---|
| prices (OHLC) | 1e-6 relative | float32 headroom. A real disagreement is orders of magnitude larger. |
| volume | 5e-3 relative | absorbs the observed ~0.1–0.2% late-tape correction on the most recent bar, which is benign and would otherwise warn on every build forever. |

The severity is `warn`, on the same reasoning as
`assert_dividend_factors_have_a_reference_close`: two vendors disagreeing about
volume is a fact about the world, not a defect in this platform, and a test that
fails every build on an accepted condition is a test that gets deleted. A price
disagreement above 1e-6 *is* worth investigating, and the model is what makes
investigating it possible.

## Consequences

Good:

- The merged price column means one thing on every row. A consumer never has to
  ask which vendor a bar came from in order to know what basis it is on — though
  `fct_security_price_daily.source` tells them anyway, because provenance stays
  attached all the way to the mart.
- Adding a third vendor is now genuinely additive, as ADR-0008 promised: one
  staging model, one branch in the priority `case`, one row in the tolerance
  table. Identity resolution and the calendar check need no changes at all.
- The double-adjustment failure mode is closed by construction rather than by
  vigilance, and the reconciliation test fails loudly if the assumption it rests
  on (that the vendors agree about splits) ever stops holding.
- Vendor disagreement becomes data. `int_source_conflicts` is the first thing in
  the project that can answer a question about data *quality* historically rather
  than at build time.

Bad:

- The split product is now computed in two places in SQL, plus once in Python.
  Justified above, and reconciled by test, but it is more surface than a single
  implementation and a future reader will be tempted to "simplify" it. The
  comment in `int_splits__cumulative` says why not.
- `int_prices_with_calendar` changed grain, which touched four existing tests.
  A one-time cost, and the tests are stronger for it, but it is the kind of
  change that would have been cheaper if the union had been there from the start.
- A Yahoo-only bar reaches the mart with NULL `vwap` and NULL `trade_count`,
  so those columns are now sparse in a way they were not before. This is correct
  — see below — but any consumer aggregating `vwap` must now handle NULLs.

Neutral:

- Yahoo's `adjclose` is fetched and discarded. It is a third adjusted series with
  a methodology this platform has not audited, and ADR-0003 is explicit that a
  series called `adjusted_close` with unstated semantics is the thing the whole
  adjustment design exists to avoid. It is not stored.

## Alternatives Considered

**Averaging the two sources, or taking a field-level best-of.** Rejected on the
grounds ADR-0008 already gives for `UNION ALL` in staging: the result is a bar no
vendor ever published, cannot be reconciled against either vendor's own API, and
breaks the byte-comparability with the Parquet archive that ADR-0002 exists to
provide. It also has no defensible answer for a KLAC bar where the two values
differ by 900% — the mean of two prices on different bases is not a price.

**Fabricating `vwap` and `trade_count` for Yahoo bars.** Rejected, and the
reasoning is the one ADR-0007 gives for CUSIP/ISIN. `vwap` could be approximated
as `(high + low + close) / 3` and `trade_count` could be left at 0, and both
would be plausible enough that no test would catch them and no consumer would
know to distrust them. A fabricated `vwap` that is wrong by a fraction of a
percent is strictly worse than a NULL, because NULL is a value a consumer can
handle correctly and a wrong number is not. Yahoo's adapter NULLs both,
explicitly.

**A `source_priority` seed table instead of a `case` expression in SQL.** More
configurable, and genuinely better at a dozen vendors. Rejected at two: it moves
the decision out of the model that implements it and into a CSV, so the priority
rule stops being visible where the merge happens, and it buys flexibility this
platform has no use for yet. Revisit at the third vendor.

**Deferring the whole question by keeping Yahoo out of `raw.prices` and landing
it in a separate table.** Would have avoided the grain change entirely. Rejected
because it contradicts ADR-0008's per-source staging convention, which is built
on the assumption that all vendors of the same entity land in the same raw table
distinguished by `source` — the column already exists and is already part of the
uniqueness constraint. Sidestepping it would leave the platform with two
different conventions for multi-source data and no rule for which to use next.

**Making the conflict test `error` rather than `warn`.** Rejected for volume, on
the measured ~0.1–0.2% last-bar disagreement, which is benign and permanent.
Considered seriously for prices, where the tolerance is tight enough that any
breach is genuinely suspicious. Left at `warn` for now only because the price
and volume checks share one model and splitting them into two tests with
different severities buys little while the conflict count is zero; if a price
conflict ever appears, the right response is to split them and error on the
price leg.
