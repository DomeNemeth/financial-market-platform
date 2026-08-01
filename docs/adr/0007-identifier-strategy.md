# ADR-0007: Security identifier strategy

**Date:** 2026-08-01
**Status:** Accepted

## Context

Every table in the platform needs to answer "which security is this row about?"
The obvious answer — the ticker — is wrong, and wrong in ways that produce
silently incorrect results rather than errors.

**Tickers are reused.** They are leased by an exchange, not owned by a company.
When a company delists, its symbol returns to the pool and can be reassigned.
Joining price history to reference data on `ticker` alone will happily splice two
unrelated companies into one continuous series.

**Tickers change.** Rebrands and restructurings reassign symbols to the same
underlying company (FB → META, GOOG → GOOGL). Keyed on ticker, one company's
history fragments into two.

**Tickers are not unique across venues.** The same symbol can denote different
securities on different exchanges, and vendors disagree about suffix conventions
(`BRK.B` vs `BRK-B` vs `BRKB`).

The industry-standard identifiers that solve this have licensing constraints:

- **CUSIP** is owned by CUSIP Global Services (S&P/AMBS). Redistribution requires
  a paid licence.
- **ISIN** is built on CUSIP for US securities and inherits the constraint.
- **FIGI** (OpenFIGI, Bloomberg) is explicitly open, free, and redistributable
  under the OpenFIGI terms.

A portfolio project published to a public repository cannot redistribute licensed
identifiers. It also must not *fabricate* them — a plausibly-formatted fake CUSIP
is worse than an empty column, because it looks usable.

## Decision

**A surrogate key is the join key.** `security_id` is a platform-generated,
durable, meaningless integer. It never changes for a given security, it is never
reused, and it carries no business meaning. Every fact table references it.

**Tickers are attributes, not identities.** `ticker` lives in the security master
as a time-bounded attribute, versioned by the SCD2 snapshot (ADR-0004). Resolving
a ticker to a `security_id` is always an as-of-date lookup, never a direct join.

**FIGI is the external identifier of record.** Populated from the free OpenFIGI
API. It is the identifier the platform will actually use to reconcile across
vendors.

**CUSIP and ISIN columns exist but stay NULL unless a licence-free source
supplies them.** The columns are in the schema because omitting them would
misrepresent what a real security master looks like, and because a licensed
deployment should be able to populate them without a migration. They are
**never** derived, inferred, checksum-generated, or copied from an
unlicensed scrape. NULL is the honest value.

## Consequences

Good:

- Ticker reuse and ticker changes both become non-events. The surrogate key
  absorbs them, and the SCD2 history records what the ticker *was* on any given
  date.
- The repository is publishable. No licensed data is redistributed.
- Vendor cross-referencing has a real anchor in FIGI rather than fuzzy symbol
  matching.
- Swapping in a licensed CUSIP feed later is a data change, not a schema change.

Bad:

- An extra resolution hop on every query. Callers pass a ticker and an as-of
  date; the platform resolves to `security_id` before it can serve anything.
  This is a real ergonomic cost and it is the price of correctness.
- OpenFIGI is a third-party dependency with its own rate limits (25
  requests/minute unkeyed, 250 with a free key), so security-master enrichment
  is slow to backfill.
- `cusip` and `isin` being NULL in a public deployment means any consumer
  expecting them must handle their absence.

Neutral:

- FIGI is share-class-level (a `composite FIGI` per share class per country),
  which is the right grain here but is not identical to CUSIP's grain. Anyone
  reconciling against a CUSIP-keyed system needs to know that.

## Alternatives Considered

**Ticker as the primary key.** Simple, readable, and what most tutorial pipelines
do. Rejected outright: it is incorrect in the two specific ways described above,
and the failure is silent. A pipeline that produces confidently wrong numbers is
worse than one that fails.

**Composite natural key of (ticker, exchange, currency).** Better — it resolves
the cross-venue collision — but it still does not survive ticker reuse over time,
because the same triple genuinely refers to different companies in different
eras. It would need a date range to disambiguate, at which point it is a
versioned attribute set and the surrogate key is doing the real work anyway.

**Use CUSIP as the key and simply not publish the repository.** Rejected: the
whole point is a public portfolio artifact.

**Generate synthetic CUSIPs with valid check digits.** Rejected emphatically.
They would pass format validation, look real to any downstream consumer, and be
entirely fictional. This is the single worst option available and is called out
here specifically so the reasoning is on record.
