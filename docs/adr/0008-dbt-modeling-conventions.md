# ADR-0008: dbt layering and modelling conventions

**Date:** 2026-08-01
**Status:** Accepted

## Context

The platform reads the same logical fact — a daily OHLCV bar — from multiple
vendors that disagree with each other. They disagree about volume (fractional vs
whole shares, consolidated vs primary-exchange tape), about which session a bar
covers, about symbol conventions, and about whether prices are already
split-adjusted.

Without a layering convention, that disagreement leaks. The two failure modes are
predictable: either every model reaches straight into `raw` and vendor quirks get
re-handled inconsistently in a dozen places, or a single `stg_prices` model
`UNION ALL`s the vendors together too early and the quirks get averaged into
something that is no vendor's actual data.

## Decision

**Four layers, each with one job.**

| Layer | Materialisation | Job |
|---|---|---|
| `raw` | Postgres tables, written by Python | Land vendor data. No transformation. |
| `staging` | view | One model per source table. Rename, cast, sanity-filter. |
| `intermediate` | view | Cross-source reconciliation and business logic. |
| `marts` | table | Query-ready facts and dimensions for the API. |

**Staging models are strictly per-source**, named `stg_{source}__{entity}` —
`stg_polygon__prices`, never a merged `stg_prices`. The double underscore
separates source from entity. A staging model reads exactly one source table and
never joins.

**Cross-source merging happens in `intermediate`, never earlier.** That is where
source priority and conflict resolution (ADR-0006) apply, and it is the only
layer allowed to decide which vendor wins.

**Raw stays raw.** Ingestion adapters do not transform. The clearest live example:
Polygon reports fractional volume (`v=56090840.685498`, an artifact of aggregating
fractional-share trades), so `raw.prices.volume` is `NUMERIC(20,6)` and stores it
verbatim. `round(volume)::bigint` happens in `stg_polygon__prices`. Rounding in
the adapter would destroy source detail that can never be recovered without
re-fetching.

**Renaming happens once, in staging.** Vendor field names (`o`, `h`, `l`, `c`,
`vw`, `n`) become platform names (`open_price`, `close_price`, `vwap`,
`trade_count`) exactly at the staging boundary. Nothing downstream sees a vendor
field name.

**Sanity filters live in staging and are cheap invariants only** — `close > 0`,
`volume >= 0`, `high >= low`. They protect downstream models from corrupt rows.
They are not business logic and must not silently drop rows a human would want to
know about; anything subtler belongs in a dbt test that fails loudly.

**Every model has a schema YAML entry** with a description and tests on at least
its grain columns. A model with no tests is treated as unfinished.

## Consequences

Good:

- Adding a vendor is additive: one new `stg_{source}__prices` model plus a branch
  in the intermediate reconciliation. No existing model changes.
- Vendor quirks are handled in exactly one place, and that place is named after
  the vendor, so the quirk is discoverable by anyone reading the DAG.
- Views for staging and intermediate mean no storage duplication and no staleness
  — the cost is recomputation, which is negligible at these volumes.
- `raw` is byte-comparable with the Parquet archive (ADR-0002), because neither
  has been transformed. That comparison is what makes archive reconciliation
  possible at all.

Bad:

- More models than a flat design needs. `stg_polygon__prices` is currently a thin
  rename over one table and looks like ceremony until the second vendor lands.
  This is accepted deliberately — the layering exists to make the *second* vendor
  cheap, and retrofitting it later is far more expensive than paying for it now.
- View-on-view chains push all computation to query time. At mart scale this will
  eventually need materialising; the layer config makes that a one-line change.

Neutral:

- Marts are tables, so they are stale between `dbt run`s. Correct for a
  batch daily-bar platform; it would be wrong for intraday data.

## Alternatives Considered

**A single merged `stg_prices` union-ing all sources.** Fewer models and a
simpler DAG. Rejected because it forces conflict resolution into the staging
layer, where there is no room for it — a `UNION ALL` either duplicates a bar
across vendors or silently picks one. Source priority is a business decision that
deserves its own explicit model.

**Transform in Python during ingestion, keep dbt thin.** Would put all logic in
one testable language. Rejected because it makes `raw` unfaithful to the vendor
and therefore unauditable against the Parquet archive, and because it discards the
lineage, documentation, and testing that are the actual reasons to use dbt. Note
that adjusted-price logic *is* deliberately implemented twice — pure Python as a
unit-testable reference implementation, dbt SQL for the pipeline (ADR-0003) —
which is a targeted exception, not a general pattern.

**dbt Python models for the adjustment maths.** Rejected: `dbt-postgres` has no
Python model support, so it would force the DuckDB or a cloud adapter that
ADR-0001 declined.
