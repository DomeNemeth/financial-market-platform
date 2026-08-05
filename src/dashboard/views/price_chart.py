"""
Price series with corporate-action annotations.

The one page where this project's central claim becomes visible rather than
documented. A 10-for-1 split drops a close from 2411.64 to ~241 overnight while
nobody's wealth changes; the raw series shows the cliff, the split-adjusted
series does not, and the annotation says which event is responsible. Seeing
those three facts on one chart is the whole argument for ADR-0003 in a form that
does not require reading ADR-0003.

`price_type` is a control here, not a setting with a default, for the same
reason the API makes it required: there is no such thing as "the" adjusted
price, and a UI that silently picked one would be making the choice ADR-0003
says must be made deliberately.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from src.dashboard import api_client
from src.dashboard.errors import guarded
from src.dashboard.theme import (
    AXIS,
    INK_MUTED,
    INK_SECONDARY,
    PLOTLY_TEMPLATE,
    SERIES_1,
    note,
    page_title,
)

PRICE_TYPES = {
    "raw": "Raw",
    "split_adjusted": "Split-adjusted",
    "total_return_adjusted": "Total-return",
}

PRICE_TYPE_HELP = {
    "raw": "The vendor's unadjusted print. What actually traded that day.",
    "split_adjusted": (
        "For charting and price levels. Splits removed; dividends not. "
        "This is the series that makes a 10-for-1 look like nothing happened."
    ),
    "total_return_adjusted": (
        "For returns. Splits and dividends both removed. Only the CLOSE exists — "
        "a dividend factor is defined against the previous close and has no "
        "intraday analogue, so open/high/low are served as null rather than "
        "filled in from the split-adjusted series."
    ),
}


def _to_float(value: str | None) -> float | None:
    """
    The single conversion point from the API's decimal strings to float.

    A chart is drawn in float64 because that is what a screen is. Everything a
    human reads as a number — the table view below, the annotation labels —
    keeps the original string, so ADR-0009 §5's guarantee survives everywhere it
    can still be observed.
    """
    return float(value) if value is not None else None


def render() -> None:
    st.markdown(
        page_title(
            "Price series",
            "Daily bars with corporate actions annotated, resolved point-in-time.",
        ),
        unsafe_allow_html=True,
    )

    # One filter row above everything it scopes. Not per-chart controls: both
    # charts below re-render against the same slice, which is the only way the
    # volume panel can be trusted to line up with the price panel.
    col_ticker, col_type, col_start, col_end, col_asof = st.columns([1.1, 1.6, 1, 1, 1])

    with col_ticker:
        ticker = st.selectbox("Ticker", api_client.TICKERS, index=2)
    with col_type:
        price_type = st.radio(
            "Series",
            list(PRICE_TYPES),
            format_func=PRICE_TYPES.get,
            horizontal=True,
            help="Required — the API has no default and neither does this page.",
        )
    with col_start:
        # 2026-05-01 rather than 2026-06-01: May is Yahoo-only, so the default
        # window spans the vendor boundary as well as KLAC's 10-for-1 split.
        start = st.date_input("From", value=dt.date(2026, 5, 1), format="YYYY-MM-DD")
    with col_end:
        end = st.date_input("To", value=dt.date(2026, 8, 3), format="YYYY-MM-DD")
    with col_asof:
        as_of = st.date_input(
            "As of",
            value=None,
            format="YYYY-MM-DD",
            help=(
                "Which security the ticker resolves to. Defaults to the window's "
                "end date. Does NOT rewind the adjustment factors — see "
                "'actions observed through' below."
            ),
        )

    st.caption(PRICE_TYPE_HELP[price_type])

    guard = guarded(ticker)
    with guard:
        series = api_client.get_prices(
            ticker, price_type=price_type, start=start, end=end, as_of=as_of
        )
        actions = api_client.get_corporate_actions(
            ticker, start=start, end=end, as_of=as_of
        )
    if not guard:
        return

    if not series["bars"]:
        st.info(
            f"`{ticker}` resolved to security {series['security_id']}, but the "
            f"window {start} → {end} contains no sessions. That is an empty "
            f"range, not a missing security — which is why it is a 200 and not "
            f"a 404."
        )
        return

    _resolution_strip(series, actions)
    _chart(series, actions, price_type, ticker)
    _table_view(series, actions)


def _resolution_strip(series: dict, actions: dict) -> None:
    """What the ticker resolved to, and what the factors were built from."""
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Security ID", series["security_id"])
    c2.metric("Current ticker", series["current_ticker"] or "—")
    c3.metric("Resolved as of", series["as_of"])
    c4.metric("Bars", f"{series['bar_count']:,}")

    observed = series.get("actions_observed_through")
    if observed:
        st.markdown(
            note(
                f"Adjustment factors reflect every corporate action observed "
                f"through <strong>{observed[:19].replace('T', ' ')}</strong>. "
                f"<code>as of</code> above rewinds <em>which security</em> the "
                f"ticker resolves to — it does not rewind the factors. Two "
                f"different &ldquo;as of&rdquo; concepts, shown separately "
                f"because collapsing them is how a point-in-time claim turns "
                f"out to be false."
            ),
            unsafe_allow_html=True,
        )
    elif actions["action_count"] == 0:
        st.markdown(
            note(
                "No corporate actions in this window, so every adjustment "
                "factor is exactly 1 and all three series are identical. The "
                "'observed through' field is null rather than back-filled with "
                "a timestamp — a factor of 1 here rests on no observation, and "
                "saying otherwise would be an invented provenance."
            ),
            unsafe_allow_html=True,
        )


def _annotation_label(action: dict) -> str:
    """
    An event as text. Identity NEVER rests on colour here — the label is the
    encoding, which is also what keeps these readable in print and under any
    form of colour vision.
    """
    parts = []
    if Decimal(action["split_ratio"]) != 1:
        ratio = Decimal(action["split_ratio"]).normalize()
        parts.append(f"{ratio}:1 split")
    if action["dividend_amount"] is not None:
        parts.append(f"div {action['dividend_amount']}")
    return "  ".join(parts) or "action"


def build_figure(series: dict, actions: dict, price_type: str, ticker: str) -> go.Figure:
    """
    Build the price+volume figure from two API responses. No Streamlit.

    Split out from the rendering call deliberately: figure construction is the
    part with logic in it — the annotation layer, the panel split, the decimal
    conversion boundary — and keeping it a pure function of two JSON payloads
    means it can be exercised and looked at without a browser in the loop.
    """
    bars = pd.DataFrame(series["bars"])
    bars["trading_date"] = pd.to_datetime(bars["trading_date"])
    for column in ("open", "high", "low", "close", "volume"):
        bars[column] = bars[column].map(_to_float)

    # TWO PANELS, ONE SHARED X — never a second y-axis. Price and volume have
    # unrelated scales, and overlaying them on twin axes invents a correlation
    # that is not in the data: the alignment of the two scales would be
    # arbitrary and the reader would take it as meaning something.
    figure = make_subplots(
        rows=2, cols=1, shared_xaxes=True,
        row_heights=[0.74, 0.26], vertical_spacing=0.05,
    )

    figure.add_trace(
        go.Scatter(
            x=bars["trading_date"], y=bars["close"],
            mode="lines", name=PRICE_TYPES[price_type],
            line=dict(color=SERIES_1, width=2),
            hovertemplate="%{y:,.4f}<extra></extra>",
        ),
        row=1, col=1,
    )

    figure.add_trace(
        go.Bar(
            x=bars["trading_date"], y=bars["volume"], name="Volume",
            marker=dict(color=SERIES_1, opacity=0.32, line=dict(width=0)),
            hovertemplate="%{y:,.0f}<extra></extra>",
        ),
        row=2, col=1,
    )

    # Corporate actions as dated rules with text labels. Deliberately drawn in
    # secondary ink rather than a categorical hue: an annotation is chrome, not
    # a series, and giving it a fifth colour would put a hue on screen whose
    # CVD separation from the other four nobody has measured. Split and dividend
    # are told apart by their dash pattern and their label, never by colour.
    for action in actions["actions"]:
        is_split = Decimal(action["split_ratio"]) != 1
        figure.add_vline(
            x=action["ex_date"],
            line=dict(
                color=INK_SECONDARY if is_split else INK_MUTED,
                width=1.5 if is_split else 1,
                dash="dash" if is_split else "dot",
            ),
            annotation_text=_annotation_label(action),
            annotation_position="top left",
            annotation=dict(
                font=dict(size=10, color=INK_SECONDARY),
                bgcolor="rgba(13,13,13,0.85)",
                borderpad=3,
            ),
            row=1, col=1,
        )

    figure.update_layout(
        template=PLOTLY_TEMPLATE,
        height=520,
        showlegend=False,  # one series per panel; the titles name them
        hovermode="x unified",
        bargap=0.15,
        title=dict(
            text=f"{ticker} · {PRICE_TYPES[price_type].lower()} close and volume"
        ),
        # A range selector is the interaction that actually matters on a time
        # series: it re-frames without retyping dates.
        xaxis2=dict(
            rangeslider=dict(visible=True, thickness=0.06, bgcolor=AXIS),
        ),
    )
    figure.update_yaxes(title_text="Close", row=1, col=1, title_font=dict(size=11))
    figure.update_yaxes(title_text="Volume", row=2, col=1, title_font=dict(size=11))

    return figure


def _chart(series: dict, actions: dict, price_type: str, ticker: str) -> None:
    figure = build_figure(series, actions, price_type, ticker)
    st.plotly_chart(figure, width="stretch", config={"displaylogo": False})

    if actions["action_count"]:
        st.caption(
            f"{actions['action_count']} corporate action(s) annotated. "
            f"Dashed = split, dotted = dividend."
        )


def _table_view(series: dict, actions: dict) -> None:
    """
    The table twin of the chart, with the values exactly as the API served them.

    Every chart needs one: a tooltip must enhance a value, never be the only way
    to reach it. Here it does a second job — these are the decimal strings
    before any float conversion, so the precision ADR-0009 §5 protects is
    actually visible somewhere in the UI rather than merely preserved in transit.
    """
    with st.expander("Table view — values exactly as the API served them"):
        st.caption(
            "Prices are JSON strings, not numbers. JSON's only numeric type is "
            "an IEEE-754 double; these are decimals."
        )
        st.dataframe(
            pd.DataFrame(series["bars"]), hide_index=True, width="stretch", height=280
        )

        if actions["action_count"]:
            st.caption("Corporate actions in this window")
            st.dataframe(
                pd.DataFrame(actions["actions"]), hide_index=True, width="stretch"
            )
