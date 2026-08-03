{{
    config(
        materialized='view'
    )
}}

-- Where two vendors covered the same bar and disagreed about it.
--
-- Grain: one row per (security_id, trading_date) on which at least one field
-- differs by more than its tolerance. Dates covered by a single vendor are not
-- here — there is nothing to disagree about.
--
-- ------------------------------------------------------------------------
-- WHY THIS IS A MODEL AND NOT ONLY A TEST
--
-- ADR-0006 considered a `severity: warn` test on its own and rejected it. A
-- warn-test emits a count into build output and nothing survives it: the rows
-- are gone the moment the build finishes, so "did the vendors disagree about
-- AAPL last Tuesday, and by how much" is a question nobody can answer
-- afterwards. Making it a model turns vendor disagreement into a first-class
-- artefact that can be queried, trended, and eventually served.
--
-- assert_sources_agree_within_tolerance then just selects from this, so the
-- build still reports the count and the count is still the signal.
--
-- ------------------------------------------------------------------------
-- COMPARED ON THE RAW BASIS, AFTER DE-ADJUSTMENT
--
-- Reads int_prices_on_raw_basis, not int_prices_with_calendar. Comparing the
-- vendors' reported numbers directly would flag every pre-split KLAC bar at a
-- 900% disagreement, which is not a disagreement about the price — it is the
-- two vendors stating the same price on different bases, which is exactly what
-- the de-adjustment exists to remove. A conflict here means the vendors
-- genuinely disagree about what traded.
--
-- That also makes this model a live check on the de-adjustment itself: if the
-- correction were wrong, the KLAC pre-split bars would reappear here as
-- enormous conflicts rather than staying silent.
--
-- ------------------------------------------------------------------------
-- THE TOLERANCES, AND WHY THERE ARE THREE OF THEM
--
-- All measured over the 258 overlapping bars in this warehouse, not taken from
-- vendor documentation. Observed maxima:
--
--     close  5.4e-8      open/high/low  2.8e-5      volume  4.4e-3
--
-- Three tolerances rather than one, because those are three different
-- quantities. A single number would have to be the loosest of them, throwing
-- away all sensitivity on the close — which is the one field the vendors
-- actually agree about and therefore the one worth watching closely.
--
--   CLOSE, 1e-6. The official closing print. Both vendors take it from the
--   primary exchange, so the only residual is Yahoo's float32 representation:
--   306.309998 against Polygon's 306.31, about 7 significant decimal digits.
--   The observed 5.4e-8 sits more than an order of magnitude inside this, so a
--   breach here is genuinely suspicious.
--
--   OPEN / HIGH / LOW, 1e-4. NOT the same quantity across vendors. Intraday
--   extremes depend on which prints get consolidated — odd lots, off-exchange
--   trades, opening auction handling — and the vendors genuinely differ. KLAC's
--   2026-07-30 low is 178.855 at Polygon and 178.86 at Yahoo: half a cent
--   apart, far too large for float32 and a defect in neither.
--
--   VOLUME, 1e-2. The field the vendors are least likely to ever agree on,
--   because consolidated-tape and primary-exchange volume are different
--   quantities by definition. Yahoo reports whole shares where Polygon reports
--   fractional, and a late tape correction moves the most recent bar of any
--   fetch. 4.4e-3 observed, so 1e-2 keeps the warn count a signal rather than
--   background noise.
--
-- Every one of these is still five or six orders of magnitude tighter than what
-- a basis error looks like — a wrong split factor is a factor of 10, not of
-- 1.0001 — so none of this loosening weakens the check that matters.

{% set close_tolerance    = 0.000001 %}
{% set intraday_tolerance = 0.0001 %}
{% set volume_tolerance   = 0.01 %}

with candidates as (

    select * from {{ ref('int_prices_on_raw_basis') }}

),

-- Self-join rather than a pivot on source, so the model does not need editing
-- when a third vendor lands: it would simply produce three pairs per date.
-- `a.source < b.source` gives each unordered pair exactly once and never pairs
-- a row with itself.
pairs as (

    select
        a.security_id,
        a.ticker,
        a.trading_date,

        a.source as source_a,
        b.source as source_b,

        a.close_price as close_a,
        b.close_price as close_b,
        a.open_price  as open_a,
        b.open_price  as open_b,
        a.high_price  as high_a,
        b.high_price  as high_b,
        a.low_price   as low_a,
        b.low_price   as low_b,
        a.volume      as volume_a,
        b.volume      as volume_b,

        -- What each vendor said before de-adjustment, so a conflict can be
        -- traced to the vendor's own number without re-deriving anything.
        a.vendor_reported_close as vendor_close_a,
        b.vendor_reported_close as vendor_close_b,

        -- Whether this comparison crossed a basis correction. The non-vacuity
        -- guard, as data: rows with a factor of 1 on both sides prove nothing
        -- about the de-adjustment.
        greatest(a.deadjustment_factor, b.deadjustment_factor) as max_deadjustment_factor

    from candidates a
    inner join candidates b
        on  b.security_id  = a.security_id
        and b.trading_date = a.trading_date
        and a.source < b.source

),

-- nullif on the denominator rather than a `where <> 0` guard: a zero close
-- cannot reach here (stg_*__prices filters close > 0), but a zero VOLUME is
-- entirely legal on a halted session, and dividing by it would abort the build
-- with a division-by-zero on a day nothing traded. NULL propagates through the
-- comparison below and the row simply does not qualify, which is correct — two
-- vendors both reporting zero volume are agreeing, not conflicting.
differences as (

    select
        *,
        abs(close_a - close_b) / nullif(abs(close_a), 0)   as close_rel_diff,
        abs(open_a  - open_b)  / nullif(abs(open_a),  0)   as open_rel_diff,
        abs(high_a  - high_b)  / nullif(abs(high_a),  0)   as high_rel_diff,
        abs(low_a   - low_b)   / nullif(abs(low_a),   0)   as low_rel_diff,
        abs(volume_a - volume_b) / nullif(abs(volume_a), 0) as volume_rel_diff

    from pairs

),

flagged as (

    select
        *,

        greatest(
            coalesce(close_rel_diff, 0),
            coalesce(open_rel_diff,  0),
            coalesce(high_rel_diff,  0),
            coalesce(low_rel_diff,   0)
        ) as max_price_rel_diff,

        -- Split into two flags rather than one. The close and the intraday
        -- extremes fail for different reasons and at different magnitudes, and
        -- collapsing them would make a conflict report say "a price differed"
        -- when the actionable question is always WHICH price. ADR-0006 also
        -- reserves the right to error on the close leg alone, which needs the
        -- legs separable.
        (coalesce(close_rel_diff, 0) > {{ close_tolerance }}) as has_close_conflict,

        (
            coalesce(open_rel_diff, 0) > {{ intraday_tolerance }}
            or coalesce(high_rel_diff, 0) > {{ intraday_tolerance }}
            or coalesce(low_rel_diff,  0) > {{ intraday_tolerance }}
        ) as has_intraday_conflict,

        (coalesce(volume_rel_diff, 0) > {{ volume_tolerance }}) as has_volume_conflict,

        {{ close_tolerance }}    as close_tolerance,
        {{ intraday_tolerance }} as intraday_tolerance,
        {{ volume_tolerance }}   as volume_tolerance

    from differences

)

select * from flagged
where has_close_conflict
   or has_intraday_conflict
   or has_volume_conflict
