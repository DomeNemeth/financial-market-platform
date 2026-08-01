# ADR-0011: Partial-batch failure policy

**Date:** 2026-08-02
**Status:** Accepted

## Context

An ingestion run covers many tickers. Any one of them can fail on its own —
a delisted symbol, a vendor 500, a rate limit that outlasts the retry budget, a
malformed payload. The batch as a whole is then neither a success nor a total
failure, and the platform has to decide what that means.

Two conventional answers, each wrong in a specific way:

**Fail fast.** Raise on the first ticker that fails. Simple and unambiguous, but
it throws away every ticker that would have succeeded *after* the failure. On a
five-requests-per-minute tier, a ten-ticker backfill takes two minutes of pure
rate-limit waiting; discarding that because ticker three was delisted means
re-paying it on every retry, and the retry hits the same delisted ticker.

**Log and continue.** Swallow the failure, keep going, exit 0. Every ticker that
can land, lands. But the run ledger then records `SUCCESS` for a batch that is
missing data, and that is the failure mode this project cares most about
avoiding: a silent gap in a price series produces confident wrong numbers rather
than an error. Anything reading `pipeline_runs` to answer "is the data complete?"
gets a wrong answer.

There is a third consideration specific to this codebase. `RunLedger` is
explicitly documented never to swallow exceptions, and each ticker's
`load_to_postgres` commits in its own transaction. So work already done is
already durable regardless of what happens next — which means the choice is
purely about *reporting*, not about data safety.

## Decision

**Collect and continue, then fail the run.**

Per-ticker failures are caught, recorded with the ticker name and exception type,
and the loop proceeds to the next ticker. After the loop, if any ticker failed,
a `PartialIngestionError` is raised **inside** the `RunLedger` context. The
ledger therefore records `FAILED`, with `rows_ingested` set to what actually
landed, and an `error_message` naming every failed ticker.

The result is both properties at once: successful tickers are committed and not
re-fetched, and the run's recorded status is honest about being incomplete.

**An empty result is not a failure.** A weekend, a holiday, or a security not yet
listed legitimately returns no rows. Adapters return an empty DataFrame rather
than raising for these, and the CLI treats it as a *gap*, not an error.

**Gaps are reported separately from failures.** Every ticker's observed trading
dates are diffed against the exchange calendar
(`src.common.calendar.missing_sessions`) and missing sessions are logged with
their count and range. A gap is a data observation — normal for a security not
listed across the whole window, and a real problem otherwise — so it warns rather
than failing. Only the exchange calendar makes this distinction possible at all;
counting calendar days would overstate expected sessions by roughly 30% and the
check would never fire meaningfully.

## Consequences

Good:

- Re-running after a partial failure is cheap. Loads are idempotent
  (`INSERT ... ON CONFLICT`), so already-ingested tickers upsert to the same
  rows and only the failures cost new API budget.
- `pipeline_runs` never reports `SUCCESS` for an incomplete batch, so it remains
  usable as a completeness signal.
- The error message names every failed ticker at once, rather than surfacing them
  one re-run at a time as fail-fast would.
- Exit code is non-zero, so a scheduler or CI step still sees a failure.

Bad:

- A run that failed can still have written data, which is initially surprising.
  This is stated in the error message itself ("N rows from M successful tickers
  were committed") rather than left for someone to discover.
- A systemic failure — expired API key, network down — burns the full rate-limit
  delay across every ticker before reporting, where fail-fast would report in
  seconds. Accepted: systemic failures are rare and obvious; single-ticker
  failures are common and the expensive case to get wrong.
- `rows_ingested` on a FAILED run is a partial count. Its meaning is "what
  landed", not "what was expected".

Neutral:

- A batch where *every* ticker fails is reported identically to one where a
  single ticker failed, just with a longer message. The distinction is available
  from the message and from `rows_ingested = 0`.

## Alternatives Considered

**Fail fast on the first error.** Rejected on the wasted-work argument above.
Would be the right call if loads were not idempotent, since partial writes would
then be dangerous rather than merely incomplete.

**Log and continue, exit 0.** Rejected: it makes the run ledger lie, which
defeats the reason the ledger exists.

**A `PARTIAL` status in `pipeline_runs`.** Genuinely appealing — it is the most
precise description of what happened. Rejected for now because every consumer
would need to learn a third state, and the common query ("did this run produce
complete data?") is binary. `FAILED` with a row count and a ticker list carries
the same information without widening the contract. Worth revisiting if a
consumer appears that needs to distinguish partial from total failure
programmatically.

**A configurable `--fail-fast` flag.** Rejected: it moves a correctness decision
into a runtime option, and the wrong setting produces silently incomplete data.
