"""
Operational view of the run ledger.

DELIBERATELY UNTYPED (ADR-0009 §6). `/securities` and `/prices` have Pydantic
response models because consumers depend on their shape. This one has
`response_model=None` and returns raw dicts, for two reasons:

  - `pipeline_runs.metadata` is free-form JSONB whose shape differs per flow. A
    response model would either type it `dict[str, Any]`, which says nothing, or
    enumerate every flow's shape, which needs editing whenever a flow changes.
  - It is an operational endpoint — for a human asking "did last night's run
    finish?" — not part of the data contract. Nothing should build against it,
    and its value is showing whatever the ledger actually recorded, including
    columns added since any schema was written.

The contrast is the point: typed where the shape is a promise, untyped where the
promise would be a lie.
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Connection, text

from src.api.resolution import get_connection

router = APIRouter(prefix="/pipeline", tags=["operational"])
logger = logging.getLogger(__name__)


@router.get(
    "/runs",
    response_model=None,
    summary="Recent pipeline runs (operational; response shape is not stable)",
)
def list_runs(
    limit: int = Query(default=50, ge=1, le=500, description="Most recent N runs."),
    status: str | None = Query(
        default=None,
        description="Filter by status: RUNNING, SUCCESS, or FAILED. Case-insensitive.",
    ),
    flow_name: str | None = Query(default=None, description="Filter by exact flow name."),
    conn: Connection = Depends(get_connection),
) -> list[dict[str, Any]]:
    """
    The run ledger, newest first.

    A FAILED run with a non-zero `rows_ingested` is normal, not a contradiction:
    ADR-0011 commits each ticker's work as it lands and then fails the run inside
    the ledger, so successful tickers survive while the run's recorded status
    stays honest about being incomplete. `rows_ingested` means "what landed",
    never "what was expected".
    """
    rows = (
        conn.execute(
            text("""
                SELECT
                    id, flow_name, status, started_at, completed_at,
                    rows_ingested, error_message, metadata, created_at,
                    -- Added in Phase 6 for the dashboard's pipeline page. This
                    -- is exactly the change the module docstring anticipates:
                    -- the endpoint's value is showing whatever the ledger
                    -- recorded, "including columns added since any schema was
                    -- written", and nothing may build against its shape. NULL
                    -- for CLI runs; set for the per-step children a Prefect
                    -- flow run writes (migration 0006).
                    parent_run_id
                FROM public.pipeline_runs
                WHERE (CAST(:status AS text) IS NULL
                       OR upper(status) = upper(CAST(:status AS text)))
                  AND (CAST(:flow_name AS text) IS NULL OR flow_name = CAST(:flow_name AS text))
                ORDER BY started_at DESC
                LIMIT :row_limit
            """),
            {"status": status, "flow_name": flow_name, "row_limit": limit},
        )
        .mappings()
        .all()
    )

    # jsonable_encoder handles the UUID and the timestamps; metadata arrives from
    # psycopg2 already deserialised from JSONB into Python objects.
    return [dict(row) for row in rows]
