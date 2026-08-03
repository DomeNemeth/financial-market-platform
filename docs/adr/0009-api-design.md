# ADR-0009: API design

**Date:** 2026-08-03
**Status:** Accepted

## Context

Phases 1–3 built an ingestion path, a bitemporal security master, and a marts
layer whose central claim is point-in-time correctness: `fct_security_price_daily`
joins `dim_security` on `security_id` **and** the valid-time window, never on
`security_id` alone, because the durable surrogate makes an unbounded join
succeed silently with reference data from the wrong period.

Phase 4 puts an HTTP surface on that. The API is where the platform's claims stop
being internal properties of a dbt DAG and become a contract a stranger can call
with a URL. Every decision below exists because the naive version of it would
quietly discard something the previous three phases paid for.

Five things needed deciding:

1. **Concurrency model.** Sync SQLAlchemy or async `AsyncSession` + asyncpg.
2. **How a caller asks for adjusted prices.** ADR-0003 produces *two* adjusted
   series on purpose. The conventional `?adjusted=true` cannot express that.
3. **What `as_of` actually resolves.** The platform has two time axes and the
   word "point-in-time" is used loosely enough to mean either.
4. **The error contract.** What a failure looks like on the wire.
5. **The wire representation of money.** ADR-0003 says Decimal, never float, and
   JSON has exactly one number type — a float.

## Decision

### 1. Sync SQLAlchemy. The API runs `def` endpoints in FastAPI's threadpool.

No `AsyncSession`, no asyncpg, no second engine. `src/common/database.py` keeps
its single sync `engine` and the API borrows connections from the same pool the
ingestion path uses.

This is a decision about *matching the concurrency model to the workload*, not an
admission that async was too hard. The workload here is a handful of
short, indexed reads against a local Postgres. FastAPI runs a plain `def`
endpoint in an `anyio` worker thread, so a blocking `psycopg2` call does not
block the event loop; the ceiling is the threadpool size (40 by default) and the
pool size (5 + 10 overflow), and neither is anywhere near being the binding
constraint at this scale.

The cost of async here is not the endpoint code, which is nearly identical. It is
that the repository would then own **two** engines, two session factories, and
two idioms for the same query — because `upsert_dataframe()`, the run ledger, the
migration runner, and every integration test are sync and have no reason to
change. A codebase where half the data access is async for no measured reason is
harder to defend than one that is uniformly sync with a written rationale.

**Revisit trigger, stated so this is falsifiable rather than a preference:** move
to async when a load test shows requests queuing on pool checkout — specifically,
when `pool_timeout` exceptions appear or p99 latency rises while Postgres-side
query time stays flat. That is the signature of thread/pool starvation, and it is
the only symptom async actually fixes. Slow queries are fixed by indexes, and
external HTTP fan-out — the workload async is genuinely good at — is not
something this API does.

### 2. `price_type` is a required enum, not a boolean.

```
price_type = raw | split_adjusted | total_return_adjusted
```

**Required.** There is no default. Omitting it is a 422.

`?adjusted=true` is the industry-standard spelling and it is unrepresentable
here, because ADR-0003's entire thesis is that there is no such thing as "the"
adjusted price. `split_adjusted_close` is for charting and price levels;
`total_return_adjusted_close` is for returns; using one where the other belongs
produces a plausible wrong number rather than an error. A boolean forces the API
to pick one and call it "adjusted", which is precisely the ambiguity ADR-0003
exists to eliminate — and it would be reintroduced at the only layer anyone
outside the project ever sees.

Making it *required* rather than defaulted follows the same reasoning one step
further. Any default is the API guessing which series the caller meant. A
required parameter costs one query-string token and converts a class of silent
misinterpretation into a 422 at the boundary.

**Column mapping**, which is asymmetric because the marts layer is asymmetric:

| `price_type` | open/high/low | close | volume | vwap |
|---|---|---|---|---|
| `raw` | `open_price` … | `close_price` | `volume` | `vwap` |
| `split_adjusted` | `split_adjusted_open` … | `split_adjusted_close` | `split_adjusted_volume` | `split_adjusted_vwap` |
| `total_return_adjusted` | **null** | `total_return_adjusted_close` | `split_adjusted_volume` | **null** |

Two entries need justifying:

- **`total_return_adjusted` has no open/high/low/vwap.** ADR-0003 derives only
  the total-return *close*, because a dividend factor is defined against the
  previous session's close and there is no defensible analogue for an intraday
  high. They are served as explicit `null`, not omitted and not silently filled
  from the split-adjusted series. A null says "this does not exist"; a
  substituted value would say "here is the total-return high", which is false.
- **`total_return_adjusted` volume is `split_adjusted_volume`.** Not a shortcut.
  A dividend does not change the share count, so the split-adjusted volume *is*
  the correct volume for a total-return series. Reusing the column is the
  arithmetically right answer, not a convenience.

`trade_count` is never adjusted under any `price_type` — it is a count of
executions, and no corporate action retroactively changes how many trades
happened.

### 3. `as_of` resolves **valid time only**. It does not rewind system time.

`as_of` answers exactly one question: **which security was trading under this
ticker on that date?** It is compared against `dim_security.valid_from` /
`valid_to` — the vendor's list/delist dates — to pick a `security_id`.

It explicitly does **not**:

- rewind the SCD2 snapshot to what the platform believed on that date
  (`known_from` / `known_to`, i.e. system time), or
- recompute adjustment factors from only the corporate actions known on that
  date.

This is the decision most at risk of being overclaimed, so it is stated as a
limitation rather than buried. A response's adjusted prices reflect **every**
corporate action currently in the warehouse, regardless of `as_of`. To make that
impossible to miss, every price response carries
`actions_observed_through` — ADR-0003's `as_of` for the factors — as a separate
field. Two different "as of" concepts appear in one payload because two genuinely
different things are being said, and collapsing them into one field is how a
point-in-time claim turns out to be false.

**Default:** `as_of` defaults to `end` when `end` is supplied, and to the current
date otherwise. That default is what makes the common case correct without the
caller thinking about it: asking for a delisted security's 2019 prices with
`end=2019-12-31` resolves identity as of 2019, when that ticker belonged to that
company — not as of today, when it may belong to someone else or to nobody.

The resolution is deliberately allowed to **fail loudly in both directions**:
zero matches is a 404, and more than one match is a 409. A one-line
`WHERE ticker = :ticker` would never produce either, which is the problem — it
would pick an arbitrary row. This mirrors the two dbt tests that bracket the same
resolution inside `int_prices_with_calendar` (one fails on a bar resolving to no
security, one on a bar resolving to several).

### 4. One error envelope, including for validation errors.

Every non-2xx response our code produces has the same body:

```json
{
  "error": "security_not_found",
  "message": "No security was listed under ticker 'ZZZZ' as of 2026-07-31.",
  "details": null
}
```

`error` is a stable machine-readable slug; `message` is human-facing and may be
reworded; `details` is an optional structured payload.

| Code | Slug | Meaning |
|---|---|---|
| 404 | `security_not_found` | Ticker resolved to no security as of `as_of`. |
| 409 | `ambiguous_ticker` | Ticker resolved to more than one security as of `as_of`. A data defect; the API refuses to guess. `details` names the candidates. |
| 400 | `invalid_range` | `start` is after `end`. |
| 400 | `range_too_large` | The window would return more than `MAX_BARS` rows. |
| 422 | `validation_error` | Request failed schema validation. Pydantic's per-field errors are preserved verbatim under `details`. |
| 404/405 | `not_found` | Framework-level rejection: an unrouted path, a disallowed method. |

FastAPI's native 422 body is re-wrapped rather than left alone. Losing
field-level detail would be a bad trade, so it is nested under `details` intact —
the envelope is uniform *and* nothing is discarded. An API where a caller must
implement two error parsers depending on which layer rejected them has a worse
contract than one that pays a small wrapping cost.

Starlette's routing errors are wrapped for the same reason: a caller who typos a
URL should not get a different body shape from one who typos a ticker. `not_found`
is a separate slug from `security_not_found` deliberately — "this endpoint does
not exist" and "no security traded under that ticker" are different answers, and
a consumer branching on the code should not have to distinguish them by reading
the message.

**An empty result is a 200 with an empty `bars` array, never a 404.** The
security exists and the request was well-formed; the window simply contains no
sessions. Conflating "this resource does not exist" with "this resource has
nothing in that range" would make a weekend indistinguishable from a bad ticker.

### 5. Money crosses the wire as a JSON **string**, not a number.

`Decimal` fields serialise to strings — verified, not assumed:
`Decimal("123.456789012345678901")` renders as `"123.456789012345678901"`, all
21 significant digits intact. Pydantic v2 does this by default and it is exactly
what this project wants.

JSON's only numeric type is an IEEE-754 double. ADR-0003 keeps money in `Decimal`
through the entire Python path and in `numeric` through the entire SQL path
specifically because adjustment factors *multiply*, so float error compounds
along the chain. Emitting a float at the last hop would discard that guarantee at
the one point where it becomes someone else's problem.

Consumers must parse these as decimals. That is a real cost — `JSON.parse` in a
browser yields strings, not numbers — and it is the correct one for a financial
API.

### 6. `/pipeline/runs` is deliberately untyped.

`/securities` and `/prices` have Pydantic response models. `/pipeline/runs` has
`response_model=None` and returns raw dicts, on purpose:

- `pipeline_runs.metadata` is free-form JSONB whose shape differs per flow. A
  response model would either type it as `dict[str, Any]`, which says nothing, or
  enumerate every flow's shape, which needs editing whenever a flow changes.
- It is an **operational** endpoint — for a human asking "did last night's run
  finish?" — not part of the data contract. Nothing should build against it.

The distinction is the point: typed where a consumer depends on the shape,
untyped where the value is in seeing whatever the ledger actually recorded. It is
tagged `operational` in the OpenAPI schema and its docstring says the shape is
unstable.

## Consequences

Good:

- The API cannot be asked an ambiguous question about adjusted prices. The
  parameter that would allow it does not exist.
- Ticker reuse is handled at the boundary, by the same valid-time rule the
  warehouse uses internally, rather than being a property only the dbt layer has.
- Both failure directions of identity resolution surface as distinct status codes,
  so a data defect (409) is never mistaken for a typo (404).
- The concurrency model is uniform across ingestion, migrations, tests, and the
  API. There is one way to talk to Postgres in this repository.
- Full decimal precision survives to the consumer.

Bad:

- `price_type` being required makes the simplest possible call more verbose, and
  is unusual enough that it will surprise anyone who has used a price API before.
  Accepted: the surprise is at the boundary and self-explains in the 422.
- Decimal-as-string requires consumer-side parsing and breaks naive
  `data.close * 2` arithmetic in JavaScript. Accepted for the reason above.
- `total_return_adjusted` responses carry four permanently-null fields. Uniform
  bar shape is worth more than a variant schema per `price_type`.
- `as_of` does *not* do the strongest thing the phrase "point-in-time" could
  mean. Mitigated by saying so here and by shipping `actions_observed_through`
  in every response rather than letting the omission pass unnoticed.
- Wrapping 422s means the error body differs from stock FastAPI, which may
  surprise someone reading the framework's docs rather than ours.

Neutral:

- `MAX_BARS` is 5,000 (~20 years of daily sessions), enforced by selecting
  `MAX_BARS + 1` rows and rejecting if the extra row appears. One query, no
  `COUNT(*)`, and no possibility of silent truncation. Pagination is deliberately
  not in Phase 4; when it arrives it will replace this cap, not sit beside it.
- `/securities/{ticker}` accepts `as_of` too, defaulting to today. It is a
  current-state lookup by default but shares the one resolver, so there is a
  single implementation of "which security is this ticker" in the codebase.
  A consequence worth knowing: a *delisted* ticker 404s by default and must be
  asked for with an `as_of` inside its listed window. That is the correct
  reading of "which security trades under this ticker today".

## Alternatives Considered

**Async SQLAlchemy + asyncpg.** Rejected on the two-idioms argument above, not on
difficulty — asyncpg and greenlet are already installed and the endpoints would
be a near-identical rewrite. Would become correct under the revisit trigger in
§1, or immediately if the API grew fan-out to slow external services, which is
the workload async actually wins on.

**`?adjusted=true`.** Rejected: structurally incapable of expressing ADR-0003's
two series. It is the single most common shape in the industry and that is part
of why the project's central methodological point is worth demonstrating in the
API rather than only in a SQL model.

**Returning all three series in every bar.** Genuinely tempting — nothing to get
wrong, and no required parameter. Rejected because it moves the choice from a
documented, validated request parameter to an undocumented downstream one: the
caller still has to pick a column, just without the API having told them the
picking matters. Roughly triples payload size as a secondary cost.

**Defaulting `price_type` to `raw`.** The runner-up. "Raw stays raw" is already a
project principle, and a caller who charts the default would see the genuine
–90% split artefact — a loud wrong answer rather than a silent one. Rejected
because a loud wrong answer is still an answer the API chose to give; the 422
happens earlier and needs no interpretation.

**`as_of` rewinding system time as well** — reconstructing what the platform
believed on a date by filtering `known_from`/`known_to`, and recomputing factors
from only the actions ingested by then. This is the strongest version of the
feature and the snapshot already carries the data for the dimension half of it.
Rejected for Phase 4 because the *factor* half does not exist:
`int_corporate_actions__factors` has no observation-time filter, so adding one is
a change to the transform layer with its own reconciliation tests, not an API
change. Doing the dimension half alone would be worse than not doing it — it
would make `as_of` look like full bitemporal replay while silently using
present-day factors. Deferred as a candidate for Phase 5, at which point
`as_of_known` would be a *second* parameter beside this one, never a redefinition
of it.

**Serialising money as JSON numbers.** Rejected: discards ADR-0003's decimal
guarantee at the final hop. `123.456789012345678901` is not representable as a
double, and the failure is silent.

**`PARTIAL`-style typed schema for `/pipeline/runs`.** Rejected for the reasons
in §6. The endpoint's value is showing what the ledger recorded, including fields
added since any schema was written.
