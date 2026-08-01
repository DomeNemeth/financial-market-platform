import logging

import pandas as pd
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.orm import sessionmaker

from src.common.config import settings

logger = logging.getLogger(__name__)

# Sync engine — fine for ingestion and Phase 1 FastAPI.
# pool_pre_ping=True tests the connection before use, catching stale connections
# after Docker restarts without crashing.
engine = create_engine(
    settings.postgres_dsn,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def check_connection() -> bool:
    """Test DB connectivity. Used by the health endpoint."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def to_records(df: pd.DataFrame, columns: list[str]) -> list[dict]:
    """
    Project `df` onto `columns` and convert to executemany-ready dicts.

    The .astype(object).where(notna) step is load-bearing: psycopg2 cannot adapt
    pandas sentinels. pd.NA (from Int64 columns like trade_count) raises
    outright, and float('nan') is adapted to a literal Postgres NaN — which
    NUMERIC silently accepts, so a missing vwap would land as NaN rather than
    NULL and quietly poison every aggregate over that column. Both must become
    None before they reach the driver.
    """
    projected = df[columns]
    return projected.astype(object).where(pd.notna(projected), None).to_dict("records")


def upsert_dataframe(
    df: pd.DataFrame,
    *,
    target: str,
    columns: list[str],
    conflict_columns: list[str],
    update_columns: list[str] | None = None,
    conn: Connection | None = None,
) -> int:
    """
    Idempotently upsert a DataFrame into a table via a staging temp table.
    Returns the number of rows inserted or updated.

    Shared by every ingestion path — prices, security master, corporate actions —
    so the idempotency guarantee is implemented once rather than three times.

    `update_columns` defaults to every column that is not part of the conflict
    key. Pass it explicitly to make an upsert insert-only for some columns.

    Pass `conn` to enlist in a caller's transaction; otherwise one is opened and
    committed here.

    The staging table is a genuine session-scoped TEMP table with ON COMMIT DROP.
    An earlier version of the price loader wrote to a permanent `raw.prices_tmp`,
    which let two concurrent ingestions clobber each other's staging rows and
    left a stale copy of price data sitting in the schema dbt introspects.

    It is built as `CREATE TEMP TABLE ... AS SELECT <cols> FROM <target> WITH NO
    DATA` rather than `LIKE <target>`. LIKE copies every column plus its NOT NULL
    constraints but *not* its defaults, so a BIGSERIAL `id` would arrive as a
    NOT NULL bigint with no sequence behind it and every insert would fail. The
    WITH NO DATA projection yields exactly the columns being written, with the
    target's exact types and no constraints, defaults, or sequence consumption.
    """
    if df.empty:
        return 0

    if update_columns is None:
        update_columns = [c for c in columns if c not in conflict_columns]

    staging = f"_stg_{target.replace('.', '_')}"
    collist = ", ".join(columns)
    placeholders = ", ".join(f":{c}" for c in columns)
    conflict = ", ".join(conflict_columns)

    if update_columns:
        action = "DO UPDATE SET " + ", ".join(f"{c} = EXCLUDED.{c}" for c in update_columns)
    else:
        action = "DO NOTHING"

    def _run(c: Connection) -> int:
        c.execute(text(f"""
            CREATE TEMP TABLE {staging} ON COMMIT DROP AS
            SELECT {collist} FROM {target} WITH NO DATA
        """))
        c.execute(
            text(f"INSERT INTO {staging} ({collist}) VALUES ({placeholders})"),
            to_records(df, columns),
        )
        # DISTINCT ON is required, not defensive: Postgres raises "ON CONFLICT DO
        # UPDATE command cannot affect row a second time" if one statement
        # presents two rows with the same conflict key. Vendors do return
        # intra-batch duplicates — overlapping paginated ranges, a restated bar.
        # ctid DESC keeps the physically-last staged row: last-write-wins in
        # DataFrame order.
        result = c.execute(text(f"""
            INSERT INTO {target} ({collist})
            SELECT DISTINCT ON ({conflict}) {collist}
            FROM {staging}
            ORDER BY {conflict}, ctid DESC
            ON CONFLICT ({conflict}) {action}
        """))
        rows = result.rowcount
        logger.info(f"Upserted {rows} rows into {target}")
        return rows

    if conn is not None:
        return _run(conn)
    with engine.begin() as own_conn:
        return _run(own_conn)
