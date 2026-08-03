-- Fails if the merged series took a fallback bar on a date the primary source
-- also covered.
--
-- ADR-0006's priority rule, stated as an invariant rather than left as an
-- ORDER BY nobody re-reads: Polygon is primary, Yahoo fills gaps and nothing
-- else. Reversing the two entries in int_prices_merged's case expression is a
-- one-character edit that changes which vendor's numbers reach the API, and
-- nothing else in the suite would notice — the grain stays unique, the factors
-- stay monotonic, the series stays continuous. Every value would simply be
-- Yahoo's float32 instead of Polygon's decimal.
--
-- Note what this does NOT test. It says nothing about whether the chosen bar is
-- correct, only about which vendor supplied it; correctness on the overlap is
-- assert_deadjusted_yahoo_reconciles_to_polygon_raw's job. The two together are
-- what pin the merge.

with merged as (

    select * from {{ ref('int_prices_merged') }}

),

available as (

    select
        security_id,
        trading_date,
        count(*) filter (where source = 'polygon') as polygon_bars
    from {{ ref('int_prices_on_raw_basis') }}
    group by 1, 2

)

select
    m.security_id,
    m.ticker,
    m.trading_date,
    m.source as merged_source,
    a.polygon_bars,
    'a fallback bar was chosen on a date the primary source covered' as reason

from merged m
inner join available a
    on  a.security_id  = m.security_id
    and a.trading_date = m.trading_date

where a.polygon_bars > 0
  and m.source <> 'polygon'
