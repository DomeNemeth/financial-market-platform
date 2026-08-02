-- Fails if any price bar could not be resolved to a security_id.
--
-- raw.prices is keyed on ticker; every model downstream of the intermediate
-- layer is keyed on security_id (ADR-0004, ADR-0007). An unresolved bar is
-- therefore a bar that silently does not reach the mart — it does not error, it
-- just stops existing somewhere in the middle of the DAG, which is the hardest
-- kind of data loss to notice.
--
-- Two distinct causes, both real and both worth failing on:
--   - the ticker has no row in the security master at all (its reference data
--     was never ingested), or
--   - it has one, but the bar falls outside the security's valid-time window,
--     which usually means a wrong list_date rather than a wrong bar.

select
    ticker,
    min(trading_date) as first_unresolved,
    max(trading_date) as last_unresolved,
    count(*)          as unresolved_bars
from {{ ref('int_prices_with_calendar') }}
where security_id is null
group by ticker
