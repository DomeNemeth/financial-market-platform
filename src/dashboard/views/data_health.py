"""
Coverage and freshness per security, by vendor.

WHAT THIS PAGE IS FOR. Every other view in this dashboard shows data that
exists. This one is about what is *missing* or *stale*, which is the harder
question and the one a warehouse actually gets judged on. A gap in a price
series does not raise; it just quietly makes a return calculation wrong.

The vendor split is the substance here rather than decoration. ADR-0006 makes
Polygon primary and Yahoo the fallback, and a bar that came from Yahoo is a
different object from a Polygon bar: it was de-adjusted back onto the raw basis
before it was chosen, and it carries no `vwap` and no `trade_count` because
Yahoo does not publish them and this platform will not fabricate them. Seeing
how much of a series is fallback tells you how much of it has those holes.

THE UNIVERSE IS CONFIGURATION, NOT DISCOVERY, and the page says so. The API has
no endpoint that lists securities, so this page iterates a configured ticker
list (see api_client.TICKERS). Drift between that list and the warehouse is
surfaced as an error row rather than hidden — a ticker that fails to resolve
appears with its reason instead of silently vanishing, which would make a
configuration mistake look like a clean bill of health.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from src.dashboard import api_client
from src.dashboard.api_client import ApiProblem, ApiUnreachable
from src.dashboard.errors import render_unreachable
from src.dashboard.theme import (
    INK_MUTED,
    PLOTLY_TEMPLATE,
    SOURCE_COLOURS,
    STATUS_CRITICAL,
    STATUS_GOOD,
    STATUS_WARNING,
    note,
    page_title,
)

#: Sessions of staleness before a security is called out. Three is a long
#: weekend plus a holiday — the point at which "the market was shut" stops
#: being the explanation.
STALE_AFTER_SESSIONS = 3


def render() -> None:
    st.markdown(
        page_title(
            "Data health",
            "Coverage, freshness, and vendor provenance per security.",
        ),
        unsafe_allow_html=True,
    )

    col_start, col_end, _ = st.columns([1, 1, 2])
    with col_start:
        # Defaults to the start of the Yahoo backfill, not the Polygon window.
        # Polygon covers every session from 2026-06-01, so a June default would
        # show a 100% Polygon warehouse and make the fallback layer look like
        # dead code — the one thing this page is best placed to disprove.
        start = st.date_input("From", value=dt.date(2026, 5, 1), format="YYYY-MM-DD")
    with col_end:
        end = st.date_input("To", value=dt.date(2026, 8, 3), format="YYYY-MM-DD")

    try:
        rows, failures = _collect(start, end)
    except ApiUnreachable as exc:
        render_unreachable(exc)
        return

    if failures:
        st.warning(
            f"{len(failures)} configured ticker(s) did not resolve. Shown below "
            f"rather than dropped — a silently shorter list looks like a clean "
            f"warehouse."
        )
        st.dataframe(
            pd.DataFrame(failures), hide_index=True, width="stretch"
        )

    if not rows:
        st.info("No securities resolved in this window.")
        return

    frame = pd.DataFrame(rows)
    _summary(frame, end)
    _coverage_chart(frame)
    _freshness_table(frame, end)


def _collect(start: dt.date, end: dt.date) -> tuple[list[dict], list[dict]]:
    """
    One `/prices` call per ticker, on the raw series.

    `raw` deliberately: this page is about what the warehouse HOLDS, and the
    adjusted series are derived. A gap is a gap in the raw bars; asking for an
    adjusted series would report the same gap through one more layer of
    arithmetic.
    """
    rows: list[dict] = []
    failures: list[dict] = []

    for ticker in api_client.TICKERS:
        try:
            series = api_client.get_prices(
                ticker, price_type="raw", start=start, end=end
            )
        except ApiProblem as problem:
            # A resolution failure is a finding about the data, not a crash.
            failures.append(
                {"Ticker": ticker, "Error": problem.error, "Detail": problem.message[:160]}
            )
            continue

        bars = series["bars"]
        dates = [b["trading_date"] for b in bars]

        # Yahoo bars are exactly the ones with no vwap: ADR-0006 forbids
        # fabricating it, and (h+l+c)/3 would have been plausible enough that
        # nothing downstream could have caught it. That refusal is what makes
        # this a reliable provenance signal rather than a guess.
        fallback = sum(1 for b in bars if b.get("vwap") is None)

        rows.append({
            "ticker": ticker,
            "security_id": series["security_id"],
            "bars": len(bars),
            "first_bar": min(dates) if dates else None,
            "last_bar": max(dates) if dates else None,
            "polygon_bars": len(bars) - fallback,
            "yahoo_bars": fallback,
        })

    return rows, failures


def _summary(frame: pd.DataFrame, end: dt.date) -> None:
    total = int(frame["bars"].sum())
    fallback = int(frame["yahoo_bars"].sum())
    share = (fallback / total * 100) if total else 0.0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Securities", len(frame))
    c2.metric("Bars", f"{total:,}")
    c3.metric("Fallback bars", f"{fallback:,}")
    c4.metric("Fallback share", f"{share:.1f}%")

    st.markdown(
        note(
            "A <em>fallback</em> bar came from Yahoo because Polygon had none "
            "for that session. It is a complete bar and it is on the correct "
            "raw basis — ADR-0006 de-adjusts Yahoo's split-adjusted prints "
            "before the merge chooses between vendors — but it carries no "
            "<code>vwap</code> and no <code>trade_count</code>, because Yahoo "
            "does not publish them and this platform does not invent them. "
            "A high fallback share means those fields are mostly absent."
        ),
        unsafe_allow_html=True,
    )


def build_coverage_figure(frame: pd.DataFrame) -> go.Figure:
    """Stacked bars of vendor provenance per security. No Streamlit."""
    ordered = frame.sort_values("bars", ascending=True)

    figure = go.Figure()
    # Colour is bound to the VENDOR, not to a position in the sort order, so
    # re-sorting or filtering never repaints a series out from under a reader
    # who has learned what blue means.
    figure.add_trace(go.Bar(
        y=ordered["ticker"], x=ordered["polygon_bars"], name="Polygon (primary)",
        orientation="h",
        marker=dict(color=SOURCE_COLOURS["polygon"], line=dict(width=0)),
        hovertemplate="Polygon: %{x} bars<extra></extra>",
    ))
    figure.add_trace(go.Bar(
        y=ordered["ticker"], x=ordered["yahoo_bars"], name="Yahoo (fallback)",
        orientation="h",
        marker=dict(color=SOURCE_COLOURS["yahoo"], line=dict(width=0)),
        hovertemplate="Yahoo: %{x} bars<extra></extra>",
    ))

    figure.update_layout(
        template=PLOTLY_TEMPLATE,
        barmode="stack",
        # A 2px surface gap between adjacent bars rather than a border drawn
        # around each mark.
        bargap=0.45,
        height=max(240, 46 * len(ordered) + 90),
        title=dict(text="Bars by vendor"),
        xaxis=dict(title=None),
        yaxis=dict(title=None, showgrid=False),
    )

    return figure


def _coverage_chart(frame: pd.DataFrame) -> None:
    st.plotly_chart(
        build_coverage_figure(frame), width="stretch", config={"displaylogo": False}
    )


def _freshness_table(frame: pd.DataFrame, end: dt.date) -> None:
    """
    The table twin, plus the staleness verdict.

    Staleness is stated as a glyph AND a word, never as a colour alone.
    """
    newest = frame["last_bar"].max()

    def verdict(row) -> str:
        if row["bars"] == 0:
            return "✕ no bars"
        if row["last_bar"] == newest:
            return "● current"
        gap = (dt.date.fromisoformat(newest) - dt.date.fromisoformat(row["last_bar"])).days
        if gap > STALE_AFTER_SESSIONS:
            return f"✕ stale ({gap}d behind)"
        return f"◐ {gap}d behind"

    display = frame.assign(Freshness=frame.apply(verdict, axis=1)).rename(columns={
        "ticker": "Ticker",
        "security_id": "Security ID",
        "bars": "Bars",
        "first_bar": "First",
        "last_bar": "Last",
        "polygon_bars": "Polygon",
        "yahoo_bars": "Yahoo",
    })[["Ticker", "Security ID", "Bars", "Polygon", "Yahoo", "First", "Last", "Freshness"]]

    def colour(value: str) -> str:
        if value.startswith("●"):
            return f"color: {STATUS_GOOD}"
        if value.startswith("◐"):
            return f"color: {STATUS_WARNING}"
        if value.startswith("✕"):
            return f"color: {STATUS_CRITICAL}"
        return f"color: {INK_MUTED}"

    st.dataframe(
        display.style.map(colour, subset=["Freshness"]),
        hide_index=True,
        width="stretch",
    )
    st.caption(
        f"'Behind' is measured against the newest bar in the warehouse "
        f"({newest}), not against today — a market holiday must not read as a "
        f"pipeline failure."
    )
