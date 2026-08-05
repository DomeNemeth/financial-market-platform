"""
Rendering the API's error envelope as something a human can act on.

THE ERROR PATHS ARE THE POINT OF THIS FILE, not an afterthought to it. Two of
them — the 404 and the 409 — are the visible surface of the single hardest thing
this platform does, and a dashboard that rendered them as a red "Error: 404"
would throw away the entire explanation.

  404 security_not_found  — nobody held this ticker on that date. The naive
      system returns a company here, and the company it returns is the wrong
      one. The UI has to say what `as_of` means, because the fix is almost
      always to move it, not to retype the ticker.

  409 ambiguous_ticker    — two securities claim the ticker over overlapping
      valid time. The API refuses to choose. This is the direction a broken
      implementation never even reaches: it would have returned one of them and
      looked perfectly healthy. Rendering it as a plain failure would waste the
      most interesting thing the API can tell you, so the candidates are laid
      out side by side and named as a data defect.

The rule the whole module follows: an error message here explains what the
system believes and what would change the answer. It never just restates the
status code.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from src.dashboard.api_client import ApiProblem, ApiUnreachable
from src.dashboard.theme import note


def render_problem(problem: ApiProblem, ticker: str = "") -> None:
    """Render one ApiProblem in the most useful form available for its kind."""
    handler = {
        "security_not_found": _security_not_found,
        "ambiguous_ticker": _ambiguous_ticker,
        "invalid_range": _plain,
        "range_too_large": _plain,
    }.get(problem.error, _plain)

    handler(problem, ticker)


def _plain(problem: ApiProblem, ticker: str) -> None:
    st.error(f"**{problem.error}** — {problem.message}")
    if problem.details:
        with st.expander("Error details"):
            st.json(problem.details)


def _security_not_found(problem: ApiProblem, ticker: str) -> None:
    st.warning(f"**No security resolved for `{ticker or '—'}`**")
    st.markdown(
        note(
            "This is a resolution result, not a missing row. Tickers are leased "
            "by exchanges and reassigned, so a lookup resolves against the "
            "security's list/delist window <em>as of a date</em> — and no "
            "security held this ticker on that date."
            "<br><br>"
            "If you are asking about a <strong>delisted</strong> company, move "
            "<code>as of</code> back inside the window it traded in. The default "
            "is today, which is the correct answer to &ldquo;who holds this "
            "ticker now&rdquo; and the wrong one for history."
        ),
        unsafe_allow_html=True,
    )
    with st.expander("What the API said"):
        st.code(problem.message, language=None)


def _ambiguous_ticker(problem: ApiProblem, ticker: str) -> None:
    st.error(f"**`{ticker or '—'}` resolved to more than one security**")
    st.markdown(
        note(
            "Two securities claim this ticker over <strong>overlapping</strong> "
            "valid time. That is a defect in the reference data, and the API "
            "will not choose between them — picking one would splice two "
            "unrelated companies into a single price history, which is the "
            "failure this platform exists to prevent and the one nobody notices."
            "<br><br>"
            "Both candidates are below. Fixing this means correcting a "
            "list/delist window in the security master, not retrying the query."
        ),
        unsafe_allow_html=True,
    )

    candidates = problem.details.get("candidates", [])
    if candidates:
        st.dataframe(
            pd.DataFrame(candidates).rename(
                columns={
                    "security_id": "Security ID",
                    "security_name": "Name",
                    "figi": "FIGI",
                    "valid_from": "Listed",
                    "valid_to": "Delisted",
                }
            ),
            hide_index=True,
            width="stretch",
        )


def render_unreachable(problem: ApiUnreachable) -> None:
    """
    The API is not answering.

    Kept distinct from every ApiProblem above: "the API refused this request,
    and here is why" and "there is no API" are different situations, and showing
    a connection failure in the shape of a data error sends the user looking for
    a problem in the warehouse.
    """
    st.error("**The API is not reachable**")
    st.markdown(
        note(
            "The dashboard holds no database credentials by design — it reads "
            "everything over HTTP from this project's own API, so that ticker "
            "resolution has exactly one implementation. With the API down there "
            "is nothing to fall back to, which is the intended trade."
            "<br><br>"
            "Start the stack with <code>docker compose up -d</code>, or set "
            "<code>API_BASE_URL</code> if the API is somewhere else."
        ),
        unsafe_allow_html=True,
    )
    st.code(str(problem), language=None)


class guarded:
    """
    Context manager that renders any API failure and suppresses it.

    Used to wrap each page body so one failing panel reports itself clearly
    instead of replacing the whole page with a Streamlit traceback.

    Usage:
        with guarded(ticker) as ok:
            ...
        if not ok:
            return
    """

    def __init__(self, ticker: str = ""):
        self.ticker = ticker
        self.ok = True

    def __enter__(self) -> guarded:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is None:
            return False
        if isinstance(exc, ApiProblem):
            render_problem(exc, self.ticker)
            self.ok = False
            return True
        if isinstance(exc, ApiUnreachable):
            render_unreachable(exc)
            self.ok = False
            return True
        return False

    def __bool__(self) -> bool:
        return self.ok
