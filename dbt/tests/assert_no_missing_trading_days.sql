-- Fails if any ticker is missing a bar for a session the exchange was open.
--
-- A gap here is not cosmetic. A missing bar silently shortens a return series,
-- shifts a moving average, and — if it happens to sit next to a corporate action
-- — corrupts the adjustment factor chain. Nothing else in the pipeline would
-- surface it: raw.prices has no way to know a row *should* exist.
--
-- Bounded per ticker to its OWN observed range. Testing against the full
-- calendar would flag every session before a ticker was first ingested, which
-- is not a defect, and the test would never pass. This detects INTERIOR gaps —
-- a session the exchange was open, between two bars we do have.

with observed_range as (

    select
        ticker,
        min(trading_date) as first_observed,
        max(trading_date) as last_observed
    from {{ ref('stg_polygon__prices') }}
    group by ticker

),

expected as (

    select
        r.ticker,
        c.session_date
    from observed_range r
    inner join {{ ref('trading_calendar') }} c
        on  c.calendar = 'XNYS'
        and c.session_date between r.first_observed and r.last_observed

),

missing as (

    select
        e.ticker,
        e.session_date
    from expected e
    left join {{ ref('stg_polygon__prices') }} p
        on  p.ticker = e.ticker
        and p.trading_date = e.session_date
    where p.ticker is null

)

select * from missing
