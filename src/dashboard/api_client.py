"""
The dashboard's only way of reaching data: HTTP, against this project's own API.

THE DASHBOARD HAS NO DATABASE CREDENTIALS AND NO SQLALCHEMY IMPORT. That is the
central design decision of Phase 6's UI, and it is worth stating plainly because
the shortcut is so tempting: Streamlit runs Python, `src.common.database` is
right there, and a direct `SELECT * FROM marts.fct_security_price_daily WHERE
vendor_ticker = ...` would have been three lines and would have worked.

It would also have been a second, unresolved implementation of "which security
is this ticker" — the exact bare-ticker join that `src/api/resolution.py` exists
to prevent, reintroduced at the one layer a human actually looks at. Every
lesson this project has accumulated about ticker reuse, valid-time windows, and
the 404/409 contract lives behind the API. A client that goes around it inherits
none of them, and inherits them silently: the chart would render, the numbers
would look like prices, and nobody would see the splice.

So the dashboard is a consumer like any other, and it is the project's own
first real consumer — which makes it a test of the API's ergonomics as well as
a view of the data. Where the API is awkward here, the API is awkward.

DECIMALS. Money crosses the wire as a JSON string (ADR-0009 §5). This module
keeps it that way: `float()` is applied at the plotting boundary and nowhere
else, and every table view shows the original string. A chart is drawn in
float64 because that is what a screen is; a number a human reads is not.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from dataclasses import dataclass, field
from typing import Any

import requests

logger = logging.getLogger(__name__)

#: Inside Docker Compose the API is reached by service name; on a laptop it is
#: localhost. Same reason docker-compose.yml overrides POSTGRES_HOST for the app
#: service — containers reach each other by service name, never by localhost.
API_BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8000").rstrip("/")

#: The dashboard's ticker universe.
#:
#: A known gap, recorded rather than hidden: the API has no endpoint that LISTS
#: securities, so the data-health page cannot discover the universe the way the
#: Prefect flow does (`tracked_tickers()` reads raw.security_master). Adding a
#: list endpoint was out of scope for Phase 6, so the universe is configuration
#: here and can drift from the warehouse. The health page surfaces that drift
#: instead of hiding it: a ticker listed here that the API does not resolve is
#: shown as an error row, not silently dropped.
DEFAULT_TICKERS = ["AAPL", "JPM", "KLAC", "MSFT", "NVDA", "V"]
TICKERS = [
    t.strip().upper()
    for t in os.environ.get("DASHBOARD_TICKERS", ",".join(DEFAULT_TICKERS)).split(",")
    if t.strip()
]

REQUEST_TIMEOUT = 15


@dataclass
class ApiProblem(Exception):
    """
    A non-2xx response, carrying the API's own error envelope.

    Every non-2xx body from this API uses one envelope — including FastAPI's
    422s and routing 404s — so one parser is enough here. That is a property the
    API deliberately has (`src/api/errors.py`), and this class is what collecting
    on it looks like from the outside.
    """

    status_code: int
    error: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.message


class ApiUnreachable(Exception):
    """The API did not answer at all — down, starting, or the wrong URL."""


def _request(path: str, params: dict[str, Any] | None = None) -> Any:
    url = f"{API_BASE_URL}{path}"
    cleaned = {k: v for k, v in (params or {}).items() if v is not None}

    try:
        response = requests.get(url, params=cleaned, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        # Deliberately not re-raised as an ApiProblem. "The API refused this
        # request, and here is why" and "there is no API" are different
        # situations for a user, and collapsing them would present a stack of
        # connection errors as though the data were bad.
        raise ApiUnreachable(f"Could not reach the API at {API_BASE_URL}: {exc}") from exc

    if response.status_code >= 400:
        try:
            body = response.json()
        except ValueError:
            body = {}
        raise ApiProblem(
            status_code=response.status_code,
            error=body.get("error", "unknown_error"),
            message=body.get("message", response.text[:500]),
            details=body.get("details") or {},
        )

    return response.json()


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------


def health() -> dict[str, Any]:
    return _request("/health")


def get_security(ticker: str, as_of: dt.date | None = None) -> dict[str, Any]:
    return _request(
        f"/securities/{ticker}",
        {"as_of": as_of.isoformat() if as_of else None},
    )


def get_prices(
    ticker: str,
    price_type: str,
    start: dt.date | None = None,
    end: dt.date | None = None,
    as_of: dt.date | None = None,
) -> dict[str, Any]:
    """
    A price series.

    `price_type` is passed through with no default of its own. The API requires
    it and refuses to guess (ADR-0003), and a client that quietly supplied one
    would have re-created exactly the guess the API declined to make — the UI
    would then be choosing a series on the user's behalf while looking like it
    was reporting one.
    """
    return _request(
        f"/prices/{ticker}",
        {
            "price_type": price_type,
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
            "as_of": as_of.isoformat() if as_of else None,
        },
    )


def get_corporate_actions(
    ticker: str,
    start: dt.date | None = None,
    end: dt.date | None = None,
    as_of: dt.date | None = None,
) -> dict[str, Any]:
    return _request(
        f"/corporate-actions/{ticker}",
        {
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
            "as_of": as_of.isoformat() if as_of else None,
        },
    )


def get_pipeline_runs(
    limit: int = 50, status: str | None = None, flow_name: str | None = None
) -> list[dict[str, Any]]:
    return _request(
        "/pipeline/runs", {"limit": limit, "status": status, "flow_name": flow_name}
    )
