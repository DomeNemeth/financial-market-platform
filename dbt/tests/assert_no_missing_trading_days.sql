-- Fails if any ticker is missing a bar for a session the exchange was open.
--
-- A gap here is not cosmetic. A missing bar silently shortens a return series,
-- shifts a moving average, and — if it happens to sit next to a corporate action
-- — corrupts the adjustment factor chain. Nothing else in the pipeline would
-- surface it: raw.prices has no way to know a row *should* exist.
--
-- Bounded per security to its OWN observed range. Testing against the full
-- calendar would flag every session before a security was first ingested, which
-- is not a defect, and the test would never pass. This detects INTERIOR gaps —
-- a session the exchange was open, between two bars we do have.
--
-- ------------------------------------------------------------------------
-- Runs over int_prices_merged, i.e. AFTER the fallback has been applied, and
-- keyed on security_id rather than ticker.
--
-- Reading a single vendor's staging model would now report the wrong thing. A
-- session Polygon missed and Yahoo covered is not a gap in this platform's
-- price series — filling exactly that hole is what ADR-0006 added the fallback
-- for — and failing on it would make the test demand that every vendor be
-- complete, which is a stronger claim than the platform makes or needs.
--
-- What is genuinely lost is per-vendor gap detection, and it is not lost
-- anywhere: src/ingestion/__main__.py diffs each ticker's observed dates
-- against the exchange calendar at ingestion time and logs the gaps per source,
-- which is where a vendor-coverage problem is actionable. This test answers the
-- different and more important question — is the SERVED series continuous.

with observed_range as (

    select
        security_id,
        min(trading_date) as first_observed,
        max(trading_date) as last_observed
    from {{ ref('int_prices_merged') }}
    group by security_id

),

expected as (

    select
        r.security_id,
        c.session_date
    from observed_range r
    inner join {{ ref('trading_calendar') }} c
        on  c.calendar = 'XNYS'
        and c.session_date between r.first_observed and r.last_observed

),

missing as (

    select
        e.security_id,
        e.session_date
    from expected e
    left join {{ ref('int_prices_merged') }} p
        on  p.security_id  = e.security_id
        and p.trading_date = e.session_date
    where p.security_id is null

)

select * from missing
