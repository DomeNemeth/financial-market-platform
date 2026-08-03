"""
Ticker -> security_id resolution. The one place in the API that does it.

This module is the point of Phase 4. Everything else in the API layer is
plumbing around this query.
"""

import datetime as dt
import logging
from collections.abc import Iterator

from sqlalchemy import Connection, RowMapping, text

from src.api.errors import AmbiguousTicker, SecurityNotFound
from src.common.database import engine

logger = logging.getLogger(__name__)


def get_connection() -> Iterator[Connection]:
    """
    FastAPI dependency yielding a pooled connection.

    Sync, and a plain `def` endpoint therefore runs in the threadpool, so the
    blocking psycopg2 call never touches the event loop. ADR-0009 §1 records why
    this is not an AsyncSession and states the measurement that would change it.

    `engine.connect()`, not `engine.begin()`: every endpoint here is a read, and
    an implicit transaction that nothing commits would hold a snapshot open for
    no reason.
    """
    with engine.connect() as conn:
        yield conn


# ---------------------------------------------------------------------------
# THE RESOLUTION QUERY
#
# `where ticker = :ticker` alone would be wrong here, and — this is the whole
# problem — it would never once raise an error while being wrong.
#
# Tickers are leased by exchanges and reassigned. When a company delists, its
# symbol goes back in the pool and can be handed to an unrelated company months
# later. A bare ticker equality staples both companies' histories into one
# series: a chart that runs continuously through a bankruptcy into someone
# else's IPO, or a "10-year return" spanning two businesses that never had
# anything to do with each other. Nothing about the result looks broken. There
# is no null to notice, no exception, no row count that seems off. It is exactly
# the failure mode this project is built around avoiding, at the one layer an
# outside consumer actually touches.
#
# So resolution is bounded by the security's VALID-time window, and `as_of` is
# the date the question is asked about:
#
#     "Which security was listed under this ticker ON THIS DATE?"
#
# VALID time (`valid_from`/`valid_to`, the vendor's list/delist dates), NOT the
# snapshot's system time (`known_from`/`known_to`). Filtering `as_of` against
# system time would ask "was this date inside the period we believed this row",
# which is a category error — a date in the market compared to a timestamp in
# our database — and would drop every row the moment the snapshot recorded a new
# version. dim_security exposes both axes under unambiguous names precisely so
# this choice has to be made deliberately. ADR-0009 §3 states the limitation
# that follows: `as_of` does not rewind system time, and it does not rewind the
# adjustment factors either.
#
# A NULL `valid_from` means "unknown", not "never listed", so it widens the
# window to -infinity rather than excluding the row. Same convention as
# int_prices_with_calendar and fct_security_price_daily — overstating the window
# yields a resolvable row that a test can check; understating it makes the
# security vanish.
#
# Both failure directions are errors, not fallbacks:
#
#   0 matches  -> 404. No security held this ticker then.
#   >1 matches -> 409. Two securities claim the ticker over overlapping valid
#                 time. That is a data defect, and picking one is precisely the
#                 splice above. The API refuses. These two are the runtime
#                 mirror of assert_every_price_bar_resolves_to_a_security and
#                 assert_price_bars_resolve_to_one_security, which bracket the
#                 same resolution at build time.
# ---------------------------------------------------------------------------
_RESOLVE_SQL = text("""
    SELECT *
    FROM marts.dim_security
    WHERE ticker = :ticker
      AND :as_of >= coalesce(valid_from, '-infinity'::date)
      AND :as_of <= coalesce(valid_to,   'infinity'::date)
    ORDER BY security_id
""")


def normalise_ticker(ticker: str) -> str:
    """
    Tickers are stored upper-case by ingestion; normalise so `/prices/klac`
    works. Done in Python rather than as `upper(ticker) = upper(:ticker)` in SQL
    so the comparison stays sargable against the ticker index.
    """
    return ticker.strip().upper()


def resolve_security(conn: Connection, ticker: str, as_of: dt.date) -> RowMapping:
    """
    Resolve `ticker` to exactly one security as of `as_of`, or raise.

    Returns the full dim_security row, so `/securities` can serve it directly
    and `/prices` can take the `security_id` from it — one implementation of
    "which security is this ticker" for the whole API.

    Raises SecurityNotFound (404) or AmbiguousTicker (409).
    """
    symbol = normalise_ticker(ticker)
    rows = conn.execute(_RESOLVE_SQL, {"ticker": symbol, "as_of": as_of}).mappings().all()

    if not rows:
        raise SecurityNotFound(
            f"No security was listed under ticker '{symbol}' as of {as_of.isoformat()}. "
            f"Tickers are reused, so this resolves against the security's "
            f"list/delist window; a delisted ticker needs an as_of inside it."
        )

    if len(rows) > 1:
        candidates = [
            {
                "security_id": r["security_id"],
                "security_name": r["security_name"],
                "figi": r["figi"],
                "valid_from": r["valid_from"].isoformat() if r["valid_from"] else None,
                "valid_to": r["valid_to"].isoformat() if r["valid_to"] else None,
            }
            for r in rows
        ]
        logger.error(f"Ambiguous ticker {symbol} as of {as_of}: {candidates}")
        raise AmbiguousTicker(
            f"Ticker '{symbol}' resolved to {len(rows)} securities as of {as_of.isoformat()}. "
            f"Their valid-time windows overlap, which is a data defect; the API will "
            f"not choose between them.",
            details={"candidates": candidates},
        )

    return rows[0]
