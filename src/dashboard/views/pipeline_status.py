"""
The run ledger, as an operator would read it.

This page answers two questions the ledger was built to answer separately: "did
last night work?" and "which source broke?". Migration 0006 made that possible
by giving a flow run one parent row and a child per step, so the second question
has an answer that does not require reading logs.

THE ONE THING THIS PAGE MUST NOT DO is flatter the result. A FAILED run with a
non-zero row count is not a contradiction and is not rendered as one: ADR-0011
commits each ticker's work as it lands and then fails the run inside the ledger,
so `rows_ingested` means "what landed", never "what was expected". A dashboard
that hid the failed status because rows arrived — or hid the rows because the
status was red — would be lying in one of the two directions the policy exists
to keep visible.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from src.dashboard import api_client
from src.dashboard.errors import guarded
from src.dashboard.theme import (
    RUN_STATUS,
    STATUS_CRITICAL,
    STATUS_GOOD,
    note,
    page_title,
    status_badge,
)


def render() -> None:
    st.markdown(
        page_title(
            "Pipeline status",
            "The run ledger. Every ingestion goes through it, and it never "
            "swallows an exception.",
        ),
        unsafe_allow_html=True,
    )

    col_limit, col_status, col_flow = st.columns([1, 1, 1.4])
    with col_limit:
        limit = st.slider("Runs to show", 10, 200, 50, step=10)
    with col_status:
        status = st.selectbox("Status", ["All", "SUCCESS", "FAILED", "RUNNING"])
    with col_flow:
        flow_filter = st.text_input("Flow name", placeholder="e.g. daily_ingest")

    guard = guarded()
    with guard:
        health = api_client.health()
        runs = api_client.get_pipeline_runs(
            limit=limit,
            status=None if status == "All" else status,
            flow_name=flow_filter.strip() or None,
        )
    if not guard:
        return

    _health_strip(health, runs)

    if not runs:
        st.info("No runs match this filter.")
        return

    _summary(runs)
    _runs_table(runs)
    _failures(runs)


def _health_strip(health: dict, runs: list[dict]) -> None:
    frame = pd.DataFrame(runs)
    latest = frame.iloc[0] if len(frame) else None

    c1, c2, c3, c4 = st.columns(4)

    db_ok = health.get("db") == "connected"
    c1.metric("API", health.get("status", "?").upper())
    c2.metric("Database", "CONNECTED" if db_ok else "UNREACHABLE")

    if latest is not None:
        c3.metric("Latest run", str(latest["flow_name"])[:22])
        c4.metric(
            "Started",
            str(latest["started_at"])[:16].replace("T", " ") if latest["started_at"] else "—",
        )
        st.markdown(
            f"Latest status: {status_badge(str(latest['status']))}",
            unsafe_allow_html=True,
        )


def _summary(runs: list[dict]) -> None:
    frame = pd.DataFrame(runs)
    counts = frame["status"].str.upper().value_counts()

    c1, c2, c3 = st.columns(3)
    c1.metric("Succeeded", int(counts.get("SUCCESS", 0)))
    c2.metric("Failed", int(counts.get("FAILED", 0)))
    c3.metric("Rows ingested", f"{int(frame['rows_ingested'].fillna(0).sum()):,}")

    st.markdown(
        note(
            "&ldquo;Rows ingested&rdquo; sums what actually <em>landed</em>, "
            "including on failed runs. Under ADR-0011 a partial batch commits "
            "its successful tickers and then fails the run, so a FAILED row "
            "with a non-zero count is the policy working, not a contradiction."
        ),
        unsafe_allow_html=True,
    )


def _runs_table(runs: list[dict]) -> None:
    frame = pd.DataFrame(runs)

    display = pd.DataFrame({
        "Status": frame["status"].map(lambda s: RUN_STATUS.get(str(s).upper(), ("", "○", s))[1]
                                      + " " + str(s)),
        "Flow": frame["flow_name"],
        "Started": frame["started_at"].astype(str).str[:19].str.replace("T", " "),
        "Rows": frame["rows_ingested"].fillna(0).astype(int),
        # NULL for CLI runs, set for the per-step children of a flow run. Shown
        # rather than hidden: it is what turns a flat list into "which source
        # broke".
        "Parent": frame.get("parent_run_id", pd.Series([None] * len(frame))).astype(str)
                       .replace("None", "—").str[:8],
        "Error": frame["error_message"].fillna("").astype(str).str[:80],
    })

    st.dataframe(
        display,
        hide_index=True,
        width="stretch",
        height=380,
        column_config={
            "Rows": st.column_config.NumberColumn(format="%d"),
        },
    )
    st.caption(
        "Parent is the flow run a step belongs to; '—' means a direct CLI run."
    )


def _failures(runs: list[dict]) -> None:
    failed = [r for r in runs if str(r.get("status", "")).upper() == "FAILED"]
    if not failed:
        st.markdown(
            f"<span style='color:{STATUS_GOOD}'>●</span> No failed runs in this "
            f"window.",
            unsafe_allow_html=True,
        )
        return

    with st.expander(f"Failures ({len(failed)}) — full error and metadata", expanded=False):
        for run in failed:
            st.markdown(
                f"<span style='color:{STATUS_CRITICAL}'>✕</span> "
                f"**{run['flow_name']}** · {str(run['started_at'])[:19].replace('T', ' ')}",
                unsafe_allow_html=True,
            )
            if run.get("error_message"):
                st.code(run["error_message"], language=None)
            if run.get("metadata"):
                st.json(run["metadata"], expanded=False)
            st.divider()
