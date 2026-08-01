# ADR-0001: Postgres as the transform substrate

**Date:** 2026-08-01
**Status:** Accepted

## Context

The platform ingests daily OHLCV bars and reference data from several vendors
(Polygon first, then Yahoo, Alpha Vantage, FRED), transforms them into
point-in-time-correct marts, and serves them over an API. Something has to be the
substrate the transform layer executes against.

The realistic working set is small. Ten tickers of daily bars over twenty years is
roughly 50,000 rows; a few hundred tickers over the same span is single-digit
millions. This is not a big-data problem, and choosing infrastructure as though it
were would be the wrong signal to send.

The constraints that actually matter:

- The transform layer is dbt. dbt needs a warehouse with a stable adapter.
- The API needs low-latency indexed point lookups (`ticker`, `as_of_date`) — a
  transactional access pattern, not an analytical scan.
- Corporate actions and the security master need real constraints: uniqueness on
  business keys, foreign keys, and exact decimal types for money.
- The whole stack must come up with `docker compose up -d` on a laptop, with no
  cloud account and no credential provisioning.

## Decision

**Postgres is the single transform substrate.** Ingestion writes each batch to
Parquet as an immutable archive *and* loads the same rows into a `raw` schema in
Postgres. dbt runs entirely against Postgres, building
`raw` → `staging` → `intermediate` → `marts`. The API reads from `marts`.

**DuckDB stays out of the critical path.** It remains available as a standalone
tool for ad-hoc querying of the Parquet archive, which it is genuinely good at,
but no pipeline stage depends on it.

## Consequences

Good:

- One engine to run, back up, and reason about. One connection string, one SQL
  dialect in the models.
- Real constraints are enforceable, and are enforced. `NUMERIC(18,6)` for prices
  means no binary-float drift in money columns. The unique constraint on
  `(ticker, trading_date, source)` is what makes `INSERT ... ON CONFLICT`
  idempotency possible at all — without it, "idempotent" would be a claim rather
  than a guarantee the database keeps.
- Postgres is the most common dbt target outside the cloud warehouses, so the
  models port to Snowflake/BigQuery/Redshift with mostly mechanical changes.
- The point-lookup pattern the API needs is what Postgres is best at.

Bad:

- Postgres is a row store. Wide analytical scans over full price history are
  slower than a columnar engine would be. Irrelevant at these volumes; at 10^8+
  rows it would not be, and this ADR would need revisiting.
- Storing the same rows in both Parquet and Postgres is deliberate duplication.
  ADR-0002 covers why that trade is worth making.

Neutral:

- `dbt-postgres` lags the cloud adapters on newer dbt features. Nothing planned
  depends on those. It also constrains the dbt and Python versions we can run —
  see ADR-0010.

## Alternatives Considered

**DuckDB as the transform engine.** Genuinely tempting: columnar, fast,
zero-server, reads the Parquet archive directly with no load step, and
`dbt-duckdb` is mature. Rejected on concurrency. DuckDB's single-writer model
means an ingestion run and a dbt run cannot safely overlap, and a long-lived API
process holding a connection conflicts with both. Working around that means
orchestrating a lock — more moving parts than just running a Postgres container.

**Snowflake / BigQuery.** The right answer at real scale and the environment most
employers actually run. Rejected because a portfolio project a reviewer should be
able to clone and run must not require a cloud account, a billing relationship, or
credential provisioning. The models are written to keep that port cheap.

**Parquet + DuckDB only, no Postgres.** Drops the duplication entirely, but leaves
the API serving point lookups by scanning files, and leaves the uniqueness and
referential constraints that make the reference-data layer trustworthy with
nowhere to live.
