{% snapshot security_master_snapshot %}

{#
    SCD2 history for the security master — the SYSTEM-TIME axis of ADR-0004.

    dbt_valid_from / dbt_valid_to mean "when this platform believed this row",
    NOT "when it was true in the world". Valid time lives in list_date /
    delist_date, which come from the vendor and pass through unchanged. The two
    axes must never be conflated; an as-of query pins both.

    strategy='check', not 'timestamp': Polygon's last_updated_utc advances on
    fields this platform does not track, which would manufacture history rows
    that look like real attribute changes. check_cols lists exactly the
    attributes whose change is worth recording.

    Deliberately NOT in check_cols:
      - ingested_at  — changes on every run; would make each run a new version.
      - security_id  — the unique_key; by definition it never changes.
      - cusip/isin   — always NULL by design (ADR-0007); nothing to track.

    unique_key is security_id alone rather than (security_id, source) because the
    select is filtered to one source. Per ADR-0008, cross-source reconciliation
    belongs in the intermediate layer, so a second vendor gets its own snapshot
    rather than being unioned in here.
#}

{{
    config(
        target_schema='snapshots',
        unique_key='security_id',
        strategy='check',
        check_cols=[
            'ticker',
            'name',
            'figi',
            'share_class_figi',
            'exchange',
            'currency',
            'security_type',
            'active',
            'list_date',
            'delist_date',
        ],
        invalidate_hard_deletes=True,
    )
}}

select
    security_id,
    ticker,
    name,
    figi,
    share_class_figi,
    cusip,
    isin,
    exchange,
    currency,
    security_type,
    active,
    list_date,
    delist_date,
    source,
    ingested_at
from {{ source('raw', 'security_master') }}
where source = 'polygon'

{% endsnapshot %}
