# ADR-0002: Parquet as an immutable landing zone

**Date:** 2026-08-01
**Status:** Accepted

## Context

ADR-0001 makes Postgres the transform substrate. That leaves an open question:
once a vendor response has been loaded into `raw.prices`, is the vendor response
itself still worth keeping?

Two properties of financial vendor data make this more than a theoretical
question:

1. **Vendors restate.** A price published today can be corrected next week —
   after a late trade report, an exchange correction, or a reclassified
   settlement. If the only copy of a bar is a row that gets overwritten by the
   next ingestion, the correction is undetectable and unauditable. "What did
   Polygon tell us on the day we made this decision?" becomes unanswerable.
2. **Vendor history is not free.** Polygon's free tier is rate-limited to 5
   requests/minute and its deep history is a paid entitlement. A `DROP` on the
   `raw` schema, a bad migration, or a botched backfill is not something we can
   simply re-request our way out of.

Separately, the `raw` schema is a *working copy* shaped for dbt — typed columns,
constraints, upserted in place. Those are the right properties for a transform
source and the wrong ones for an audit record.

## Decision

**Every ingestion writes Parquet before it writes Postgres**, to
`data/raw/prices/{source}/{ticker}/{YYYY-MM-DD}.parquet`.

The Parquet tree is an **immutable archive**. Files are never mutated in place.
They are overwritten only by an intentional, explicit re-ingestion of that exact
partition — never as a side effect of a normal run touching a neighbouring date.

Postgres `raw` is the **working copy**: upserted, constrained, and safe to
rebuild. If it were lost entirely, it is reconstructible from the Parquet tree
without going back to the vendor.

The write order is load-bearing. Parquet is written first, so a crash between the
two steps leaves an archived file with no database row — recoverable and
detectable — rather than a database row whose provenance was never captured.

## Consequences

Good:

- Vendor restatements become observable. The archived file and the current
  `raw.prices` row can be diffed, which is what makes a point-in-time claim
  defensible rather than aspirational.
- Postgres is disposable. `docker compose down -v` is not a data-loss event, which
  materially lowers the cost of schema changes during development.
- Rate-limited vendor history is captured once and never re-fetched.
- Parquet is columnar and self-describing, so the archive is directly queryable
  with DuckDB or pandas without a server, and compresses well.

Bad:

- The same rows are stored twice. At these volumes the archive is megabytes, so
  the storage cost is not a real concern, but the *conceptual* cost is: two
  representations can drift, and a reader has to know which one is authoritative
  for which question.
- Nothing yet enforces immutability. It is a convention held up by
  `write_parquet()` being the only writer, not a filesystem permission. A
  reconciliation check between archive and `raw` is not yet implemented.
- `data/` is gitignored, so the archive is local-only. It is not backed up and
  does not survive a machine loss. Acceptable for a portfolio project; it would
  be object storage with versioning enabled in production.

Neutral:

- One file per (source, ticker, date) yields many small files. Fine at this scale
  and it makes partition-level re-ingestion trivial; it would need compaction into
  larger partitions before it scaled.

## Alternatives Considered

**Postgres only, no archive.** Simplest, and the working copy is all the transform
layer strictly needs. Rejected because it makes restatements invisible and makes
the rate-limited vendor history unrecoverable — losing exactly the property that
distinguishes this from a toy pipeline.

**Archive the raw JSON responses instead of Parquet.** Higher fidelity: it
preserves vendor fields we do not currently parse, and it is the true wire
record. A reasonable choice, and arguably more correct for pure audit. Rejected
because it is not directly queryable without a parse step, compresses worse, and
loses the schema. The parsed-but-unmodified DataFrame is the better trade here,
and adapter `fetch()` deliberately does no transformation beyond field selection,
so little is discarded.

**Write Postgres first, Parquet second.** Rejected on the crash-window argument
above: it produces database rows with no archived provenance, which is the failure
mode the archive exists to prevent.
