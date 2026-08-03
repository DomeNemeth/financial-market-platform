# ADR-0012: Macro data — publication lag and the vintage limitation

**Date:** 2026-08-03
**Status:** Accepted

## Context

Macro series differ from price series in a way that breaks the obvious join.

FRED dates an observation to the **start of the period it describes**, not to
when it was published. January 2026's unemployment rate is dated `2026-01-01`
and was first published on `2026-02-11`. So the standard ASOF join — for each
price bar, the most recent macro observation dated on or before it — attaches
January's number to every trading day in January, when nobody knew it.

Measured over the ten series loaded here, the lag is not marginal:

| series | avg lag (days) | max |
|---|---|---|
| UMCSENT | 26 | 57 |
| FEDFUNDS | 31 | 44 |
| UNRATE / PAYEMS | 35 | 80 |
| CPIAUCSL | 43 | 78 |
| INDPRO | 46 | 93 |
| GDP | 121 | 175 |

A backtest built on the naive join trades on data it could not have had, and the
error flatters it — the direction that gets a model deployed. It is the same
class of silent failure as joining a price bar to a security by bare ticker
equality (ADR-0007): nothing raises, every row looks reasonable.

There is a second, independent problem. FRED **revises**. The value served for a
period today is not the value first published for it, sometimes by a lot.

## Decision

**1. Store the true first-publication date, and join on it.**

`raw.macro_observations.first_published_date` comes from FRED's
`output_type=4` (initial release) endpoint with the realtime window widened to
FRED's full archive bounds. Both are required: the default response stamps every
`realtime_start` with the date of the fetch, which is true and useless.

`fct_security_price_macro_context` ASOF-joins on that column, never on
`observation_date`. `assert_macro_context_has_no_lookahead` is the invariant.

**2. Series with no publication history are excluded, not assumed.**

FRED publishes no initial-release history for calculated series (`T10Y2Y`, a
spread) or for the daily constant-maturity Treasuries (`DGS10`, `DGS2`) — 3 of
the 10 loaded. They are left out of the point-in-time join rather than joined
with an assumed same-day lag. Inventing a publication date is the same category
of error as fabricating a CUSIP (ADR-0007) or a `vwap` (ADR-0006): plausible,
uncheckable, and wrong in the flattering direction. `supports_point_in_time_join`
on `dim_macro_series` makes the exclusion visible.

**3. The limitation, stated in one sentence.**

> **`macro_value` is the latest revision, not the value as first published — so
> the point-in-time join removes look-ahead about a number's *existence* but not
> about its *value*.**

This is deliberately the same boundary ADR-0009 draws for the API's `as_of`,
which rewinds valid time without replaying what the platform believed at the
time. Closing it requires storing every vintage rather than the latest, which is
an order of magnitude more data and a Phase 6 decision. Until then
`macro_vintage_date` is carried on every row, so a consumer can see which
revision they hold rather than assume it is the original print — the same
discipline as surfacing `actions_observed_through` beside `as_of` rather than
leaving the gap silent.

## Consequences

Good:

- The ASOF join is genuinely point-in-time on the axis that matters most, and
  `assert_point_in_time_macro_differs_from_naive` proves the machinery changes
  real rows rather than being ceremony.
- `publication_lag_days` is materialised, so the size of the bias avoided is
  visible in the data instead of being a claim in a comment.

Bad:

- Two FRED requests per series instead of one.
- 3 of 10 series cannot take part in the point-in-time join, and they include
  the interest-rate series a reader would most expect to see.

Neutral:

- `raw.macro_observations` mixes a latest-vintage value with an
  initial-release date. That hybrid is stated above rather than hidden, and it
  is strictly more useful than either column alone.

## Alternatives Considered

**Naive ASOF on `observation_date`, with the bias documented.** Rejected: a
documented bias in a mart is still a bias in the mart, and the first consumer to
skim the docs gets a wrong answer with no warning at query time.

**Assume a fixed publication lag per frequency** (e.g. monthly = 30 days).
Rejected. It is fabrication, and the measured spread — UNRATE's lag ranges 31 to
80 days — shows a constant would be wrong by weeks in both directions.

**Store every vintage now.** The correct end state, and what makes the value
half of the problem solvable. Deferred to Phase 6 on size, and because the
existence half is where the large, systematic error is.
