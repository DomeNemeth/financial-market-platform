"""
Streamlit entrypoint.

Three pages, deliberately — narrowed from the five originally planned. The
backend accumulated enough substance over five phases that a wide, shallow UI
would have undersold it: three pages that each say something true and specific
are worth more than five that each show a table. What was cut (a macro-context
page and a source-conflict page) was cut because both are better served by the
dbt tests that already assert on them than by a chart nobody would interrogate.

Run it:
    streamlit run src/dashboard/app.py

It needs the API, not the database:
    API_BASE_URL=http://localhost:8000   (default)
"""

from __future__ import annotations

import streamlit as st

from src.dashboard.api_client import API_BASE_URL
from src.dashboard.theme import CSS, INK_MUTED

st.set_page_config(
    page_title="Financial Market Platform",
    page_icon="◪",
    layout="wide",
    initial_sidebar_state="expanded",
)
st.markdown(CSS, unsafe_allow_html=True)

# Imported after set_page_config: Streamlit requires it to be the first command.
from src.dashboard.views import data_health, pipeline_status, price_chart  # noqa: E402

PAGES = {
    "Price series": price_chart.render,
    "Pipeline status": pipeline_status.render,
    "Data health": data_health.render,
}

with st.sidebar:
    st.markdown(
        "<div style='font-size:15px;font-weight:650;letter-spacing:-0.01em'>"
        "◪&nbsp;&nbsp;Financial Market Platform</div>"
        f"<div style='color:{INK_MUTED};font-size:11px;margin-top:2px'>"
        "Point-in-time market data warehouse</div>",
        unsafe_allow_html=True,
    )
    st.divider()

    selection = st.radio("View", list(PAGES), label_visibility="collapsed")

    st.divider()
    st.markdown(
        f"<div style='color:{INK_MUTED};font-size:11px;line-height:1.6'>"
        f"<strong>Thin client.</strong> This UI holds no database credentials. "
        f"Everything is read over HTTP from the project's own API, so ticker "
        f"resolution has exactly one implementation."
        f"<br><br>"
        f"<strong>Daily bars.</strong> Not live, and not presented as live — "
        f"the warehouse is fed by a nightly flow."
        f"<br><br>"
        f"API: <code>{API_BASE_URL}</code>"
        f"</div>",
        unsafe_allow_html=True,
    )

PAGES[selection]()
