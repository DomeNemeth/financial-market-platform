{{
    config(
        materialized='view'
    )
}}

-- Staged corporate actions from Polygon.
-- Grain: one row per (security_id, action_type, ex_date).
--
-- The one piece of real work here is collapsing the vendor's two-sided split
-- ratio into the single dimensionless factor the adjustment maths consumes
-- (ADR-0003). Everything else is renaming and type coercion, per ADR-0008.

with source as (

    select * from {{ source('raw', 'corporate_actions') }}
    where source = 'polygon'

),

renamed as (

    select
        security_id,
        ticker,
        action_type,
        ex_date,

        split_to,
        split_from,
        -- The adjustment ratio. NVDA 2024-06-10: to=10, from=1 -> 10.0.
        -- NULL for dividends, which is what keeps the split and dividend factor
        -- products independent downstream.
        case
            when action_type = 'split'
            then split_to / nullif(split_from, 0)
        end as split_ratio,

        cash_amount,
        currency,
        dividend_type,

        declaration_date,
        record_date,
        pay_date,

        source,
        ingested_at

    from source

)

select * from renamed
