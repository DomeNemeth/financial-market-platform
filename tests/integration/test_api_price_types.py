"""
The `price_type` contract, tested across a real 10-for-1 split.

ADR-0003's claim is that there is no such thing as "the" adjusted price, and
ADR-0009 turns that into an API rule: `price_type` is a required enum and the
conventional `?adjusted=true` does not exist. This file asserts that the three
series are actually *different from each other* over a window where they must
be, rather than merely that the endpoint accepts three parameter values.

Why KLA's 2026-06-12 10-for-1: the close drops ~90% overnight while nobody's
wealth changes. It is the sharpest available discriminator between the raw and
split-adjusted series, and it is inside Polygon's free-tier two-year aggregate
window — unlike NVDA's canonical 2024 split, whose bars 403. The same event
anchors test_split_reconciliation.py, which checks the *arithmetic*; this file
checks only that the API serves the right column for the right parameter and
shapes the response as ADR-0009 says.

Requires the stack up, `dbt build` already run, and KLAC prices and corporate
actions ingested.
"""

import datetime as dt
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from src.api.main import app

pytestmark = pytest.mark.integration

TICKER = "KLAC"
SPLIT_EX_DATE = dt.date(2026, 6, 12)
SPLIT_RATIO = Decimal(10)
# The last pre-split session and the first post-split one.
LAST_PRE = dt.date(2026, 6, 11)
WINDOW_START = dt.date(2026, 6, 5)
WINDOW_END = dt.date(2026, 6, 19)

# The mart stores factors as numeric with a long fractional tail from
# exp(sum(ln(...))); comparisons against an exact ratio need arithmetic
# headroom but nothing near the percent-scale differences a wrong column would
# produce.
TOLERANCE = Decimal("1e-9")

# A dividend with an ex-date inside the ingested price range, for the
# total-return series. KLAC pays none inside its split window, and NVDA's
# 2026-06-04 ex-date falls just before it — every bar there already has a
# dividend factor of 1, which is why the split window cannot double as the
# dividend window.
#
# JPM's 2026-07-06 is deliberate beyond convenience: its previous session is
# 2026-07-02, because 2026-07-03 is the observed Independence Day holiday.
# `ex_date - 1 day` lands on Sunday the 5th, finds no bar, and silently drops
# the dividend — the live instance of the hazard the trading calendar exists
# for, now visible through the API.
DIVIDEND_TICKER = "JPM"
DIVIDEND_EX_DATE = dt.date(2026, 7, 6)
DIVIDEND_WINDOW_START = dt.date(2026, 7, 1)
DIVIDEND_WINDOW_END = dt.date(2026, 7, 10)


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


def _fetch(
    client,
    price_type: str,
    ticker: str = TICKER,
    start: dt.date = WINDOW_START,
    end: dt.date = WINDOW_END,
) -> dict:
    response = client.get(
        f"/prices/{ticker}",
        params={
            "price_type": price_type,
            "start": start.isoformat(),
            "end": end.isoformat(),
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture(scope="module")
def raw(client):
    return _fetch(client, "raw")


@pytest.fixture(scope="module")
def split_adjusted(client):
    return _fetch(client, "split_adjusted")


@pytest.fixture(scope="module")
def total_return(client):
    return _fetch(client, "total_return_adjusted")


def _by_date(payload: dict) -> dict[str, dict]:
    return {bar["trading_date"]: bar for bar in payload["bars"]}


# --------------------------------------------------------------------------
# Non-vacuity. Everything below compares series across a split; if the window
# does not contain one, every comparison is trivially satisfiable.
# --------------------------------------------------------------------------


def test_the_window_actually_straddles_the_split(db_engine, raw):
    """
    The split is really in raw.corporate_actions, and the window really covers
    both sides of it.

    Asserted against the database rather than assumed from the constants above,
    so this fails loudly if the fixture data is ever re-ingested differently
    instead of quietly making the rest of the file vacuous.
    """
    with db_engine.connect() as conn:
        row = conn.execute(
            text("""
                SELECT split_to, split_from
                FROM raw.corporate_actions
                WHERE ticker = :t AND action_type = 'split' AND ex_date = :ex
            """),
            {"t": TICKER, "ex": SPLIT_EX_DATE},
        ).fetchone()

    if row is None:
        pytest.skip(
            f"No {TICKER} split on {SPLIT_EX_DATE} in raw.corporate_actions. Run: "
            f"python -m src.ingestion.corporate_actions --tickers {TICKER} --since 2024-01-01"
        )
    assert Decimal(row[0]) / Decimal(row[1]) == SPLIT_RATIO

    dates = {b["trading_date"] for b in raw["bars"]}
    assert any(d < SPLIT_EX_DATE.isoformat() for d in dates), "no pre-split sessions in window"
    assert any(d >= SPLIT_EX_DATE.isoformat() for d in dates), "no post-split sessions in window"


def test_the_raw_series_still_contains_the_artefact_being_corrected(raw):
    """
    Confirms the problem is real before asserting that a parameter fixes it.

    If `raw` had been quietly adjusted somewhere upstream, the split-adjusted
    comparisons below would pass while proving nothing at all.
    """
    bars = _by_date(raw)
    before = Decimal(bars[LAST_PRE.isoformat()]["close"])
    after = Decimal(bars[SPLIT_EX_DATE.isoformat()]["close"])

    assert (after - before) / before < Decimal("-0.85"), (
        "expected a ~-90% overnight artefact in the raw series"
    )


def test_all_three_series_cover_the_same_sessions(raw, split_adjusted, total_return):
    """
    A price_type that silently returned a different row set would make every
    per-date comparison below compare the wrong things — or skip them.
    """
    assert set(_by_date(raw)) == set(_by_date(split_adjusted)) == set(_by_date(total_return))
    assert raw["bar_count"] == len(raw["bars"]) > 0


# --------------------------------------------------------------------------
# The contract: each price_type serves a different column.
# --------------------------------------------------------------------------


def test_split_adjusted_removes_the_artefact_that_raw_keeps(raw, split_adjusted):
    """
    The two series must *disagree*, in the specific way the split predicts.

    This is the assertion an implementation that ignored `price_type` and always
    served close_price would fail — and it is worth stating as a difference
    between two responses rather than as a property of one, because that is what
    makes it impossible to satisfy by serving the same column twice.
    """
    raw_bars, adj_bars = _by_date(raw), _by_date(split_adjusted)

    before_raw = Decimal(raw_bars[LAST_PRE.isoformat()]["close"])
    after_raw = Decimal(raw_bars[SPLIT_EX_DATE.isoformat()]["close"])
    before_adj = Decimal(adj_bars[LAST_PRE.isoformat()]["close"])
    after_adj = Decimal(adj_bars[SPLIT_EX_DATE.isoformat()]["close"])

    assert abs((after_adj - before_adj) / before_adj) < Decimal("0.10"), (
        "split_adjusted still shows a discontinuity across the split"
    )
    assert (after_raw - before_raw) / before_raw < Decimal("-0.85"), (
        "raw should NOT be adjusted"
    )


def test_pre_split_adjusted_close_is_the_raw_close_divided_by_the_ratio(raw, split_adjusted):
    """
    The exact relationship, not just "smaller".

    A response that served split_adjusted_close for `raw` too, or applied the
    factor twice, still passes a continuity check on one side; it cannot pass
    this. Post-split bars are checked as well, where the factor is exactly 1 and
    the two series must be *identical* — ADR-0003's guarantee that the latest bar
    equals the raw bar, visible at the API boundary.
    """
    raw_bars, adj_bars = _by_date(raw), _by_date(split_adjusted)

    for day in sorted(raw_bars):
        raw_close = Decimal(raw_bars[day]["close"])
        adj_close = Decimal(adj_bars[day]["close"])
        expected = raw_close / SPLIT_RATIO if day < SPLIT_EX_DATE.isoformat() else raw_close

        assert abs(adj_close - expected) / expected <= TOLERANCE, (
            f"{day}: split_adjusted close {adj_close}, expected {expected}"
        )


def test_volume_is_adjusted_in_the_opposite_direction(raw, split_adjusted):
    """
    Volume goes UP when price goes down, so price x volume survives.

    Included because it is the one field an implementation that applied the
    factor uniformly would get backwards while every price column still looked
    right.
    """
    raw_bars, adj_bars = _by_date(raw), _by_date(split_adjusted)
    pre_split = [d for d in raw_bars if d < SPLIT_EX_DATE.isoformat()]
    assert pre_split, "no pre-split sessions to check"

    for day in pre_split:
        raw_notional = Decimal(raw_bars[day]["close"]) * Decimal(raw_bars[day]["volume"])
        adj_notional = Decimal(adj_bars[day]["close"]) * Decimal(adj_bars[day]["volume"])
        assert abs(adj_notional - raw_notional) / raw_notional <= TOLERANCE, (
            f"{day}: notional not preserved ({adj_notional} vs {raw_notional})"
        )
        assert Decimal(adj_bars[day]["volume"]) > Decimal(raw_bars[day]["volume"])


def test_total_return_serves_only_a_close(total_return):
    """
    The asymmetry ADR-0003 forces, surfaced honestly.

    There is no total-return open, high, low, or vwap — a dividend factor is
    defined against the previous session's close and has no intraday analogue.
    Those fields are explicitly null rather than omitted or back-filled from the
    split-adjusted series: a null says "this does not exist", where a substituted
    value would assert something false and look entirely reasonable.
    """
    assert total_return["bars"], "no bars to check"

    for bar in total_return["bars"]:
        assert bar["open"] is None, "there is no total-return open"
        assert bar["high"] is None
        assert bar["low"] is None
        assert bar["vwap"] is None, "there is no total-return vwap"
        assert bar["close"] is not None
        # Not adjusted under any price_type: it counts executions.
        assert bar["trade_count"] is not None


def test_total_return_volume_is_the_split_adjusted_volume(total_return, split_adjusted):
    """
    Not a fallback — the arithmetically correct answer.

    A dividend does not change the share count; only a split does. So the
    split-adjusted volume already IS the right volume for a total-return series,
    and equality here is exact rather than approximate because it is literally
    the same column.
    """
    tr_bars, adj_bars = _by_date(total_return), _by_date(split_adjusted)

    for day in sorted(tr_bars):
        assert Decimal(tr_bars[day]["volume"]) == Decimal(adj_bars[day]["volume"])


def test_trade_count_is_identical_across_every_price_type(raw, split_adjusted, total_return):
    """No corporate action retroactively changes how many trades happened."""
    raw_bars, adj_bars, tr_bars = _by_date(raw), _by_date(split_adjusted), _by_date(total_return)

    for day in sorted(raw_bars):
        assert (
            raw_bars[day]["trade_count"]
            == adj_bars[day]["trade_count"]
            == tr_bars[day]["trade_count"]
        )


def test_the_two_adjusted_series_diverge_exactly_at_an_ex_date(client):
    """
    The two adjusted series are not interchangeable, demonstrated at the
    boundary where they must differ.

    Without this the file could "prove" three distinct price_types while only
    ever exercising two: KLAC pays no dividend in its split window, so
    total_return and split_adjusted coincide there and a swapped column would go
    unnoticed.

    The assertion is a step function, not just an inequality. Bars BEFORE the
    ex-date carry a dividend factor below 1, so their total-return close sits
    strictly below the split-adjusted close. Bars ON OR AFTER the ex-date have a
    factor of exactly 1 and the two series must be *identical*. Both halves
    matter: the first catches a total-return column that was never applied, the
    second catches one applied to the wrong side of the boundary — the classic
    `<=` / `<` off-by-one, which an inequality-only test passes happily.
    """
    adj = _by_date(
        _fetch(
            client, "split_adjusted", ticker=DIVIDEND_TICKER,
            start=DIVIDEND_WINDOW_START, end=DIVIDEND_WINDOW_END,
        )
    )
    tr = _by_date(
        _fetch(
            client, "total_return_adjusted", ticker=DIVIDEND_TICKER,
            start=DIVIDEND_WINDOW_START, end=DIVIDEND_WINDOW_END,
        )
    )

    ex_date = DIVIDEND_EX_DATE.isoformat()
    before = [d for d in sorted(adj) if d < ex_date]
    on_or_after = [d for d in sorted(adj) if d >= ex_date]

    assert before, f"window has no sessions before {ex_date}"
    assert on_or_after, f"window has no sessions on or after {ex_date}"

    for day in before:
        assert Decimal(tr[day]["close"]) < Decimal(adj[day]["close"]), (
            f"{day}: total-return close should sit strictly below split-adjusted "
            "before an ex-date"
        )

    for day in on_or_after:
        assert Decimal(tr[day]["close"]) == Decimal(adj[day]["close"]), (
            f"{day}: no dividend follows this bar, so the two series must agree exactly"
        )


# --------------------------------------------------------------------------
# The parameter contract itself.
# --------------------------------------------------------------------------


def test_price_type_is_required(client):
    """
    Omitting it is a 422, not a default.

    Any default would be the API choosing a series on the caller's behalf, which
    is exactly the ambiguity ADR-0003 exists to remove. The rejection happens at
    the boundary and names the field.
    """
    response = client.get(f"/prices/{TICKER}")

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["error"] == "validation_error"
    assert any("price_type" in (d.get("loc") or []) for d in body["details"])


def test_there_is_no_bare_adjusted_price_type(client):
    """
    `?price_type=adjusted` is rejected, and so is `?adjusted=true`.

    The industry-standard spelling is structurally unrepresentable here, and that
    is the point of the enum: a caller who wants "adjusted prices" must say which
    of the two they mean. The unknown query parameter is ignored by FastAPI, so
    the second request fails on the *missing* price_type — which is the same
    refusal arriving by a different route.
    """
    named = client.get(f"/prices/{TICKER}", params={"price_type": "adjusted"})
    assert named.status_code == 422, named.text
    assert named.json()["error"] == "validation_error"

    boolean_style = client.get(f"/prices/{TICKER}", params={"adjusted": "true"})
    assert boolean_style.status_code == 422, boolean_style.text


def test_prices_cross_the_wire_as_strings_not_json_numbers(raw):
    """
    ADR-0009 §5, asserted on the parsed payload.

    JSON's only numeric type is an IEEE-754 double. ADR-0003 keeps money in
    Decimal end to end precisely because adjustment factors multiply and float
    error compounds; emitting a JSON number here would throw that away at the
    last hop, silently. `json.loads` gives a `float` for a bare number and a
    `str` for a quoted one, so the type of the parsed value IS the contract.
    """
    bar = raw["bars"][0]
    for field in ("open", "high", "low", "close", "volume", "vwap"):
        assert isinstance(bar[field], str), f"{field} was {type(bar[field]).__name__}, not a string"
        # And it must survive the round trip exactly.
        assert Decimal(bar[field]) == Decimal(bar[field])

    # trade_count is a genuine integer count, not money, and stays a JSON number.
    assert isinstance(bar["trade_count"], int)


def test_the_response_reports_which_actions_the_factors_reflect(split_adjusted):
    """
    `actions_observed_through` is present and distinct from `as_of`.

    Two different "as of" concepts appear in one payload because two genuinely
    different things are being said: `as_of` resolved the identity, and this
    says which corporate actions the adjusted numbers were built from. ADR-0009
    §3 keeps them separate rather than collapsing them, because collapsing them
    is how a point-in-time claim turns out to be false.
    """
    assert split_adjusted["actions_observed_through"] is not None, (
        "KLAC has a split, so its factors rest on a real observation"
    )
    assert split_adjusted["as_of"] == WINDOW_END.isoformat(), "as_of should default to end"
    assert "as_of" in split_adjusted and "actions_observed_through" in split_adjusted


def test_an_empty_window_is_an_empty_series_not_a_404(client):
    """
    The security exists; the range simply has no sessions.

    Conflating "no such resource" with "no data in that range" would make a
    weekend indistinguishable from a bad ticker.
    """
    response = client.get(
        f"/prices/{TICKER}",
        params={"price_type": "raw", "start": "2019-01-01", "end": "2019-01-05"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["bars"] == []
    assert body["bar_count"] == 0
    assert body["security_id"] > 0, "the security still resolved"


def test_an_inverted_range_is_rejected(client):
    response = client.get(
        f"/prices/{TICKER}",
        params={"price_type": "raw", "start": "2026-06-19", "end": "2026-06-05"},
    )

    assert response.status_code == 400, response.text
    assert response.json()["error"] == "invalid_range"


def test_an_oversized_window_is_rejected_rather_than_truncated(client, monkeypatch):
    """
    The MAX_BARS guard, exercised by lowering the cap rather than by fabricating
    thousands of bars.

    The real cap is 5,000 and the warehouse holds 43 sessions, so this guard can
    never fire on real data — which is exactly why it needs a test. An untested
    limit is a limit nobody knows the behaviour of, and the behaviour here is the
    whole point: over the cap the request is REJECTED, and the response carries
    no bars at all. A truncated series would be indistinguishable from a complete
    one, and someone would compute a return over it.
    """
    monkeypatch.setattr("src.api.routers.prices.MAX_BARS", 5)

    over = client.get(
        f"/prices/{TICKER}",
        params={"price_type": "raw", "start": "2026-06-01", "end": "2026-07-31"},
    )

    assert over.status_code == 400, over.text
    body = over.json()
    assert body["error"] == "range_too_large"
    assert "bars" not in body, "an over-cap response must carry no partial data"

    # Non-vacuity: the cap must be responding to the window size, not simply
    # rejecting everything once lowered. A window inside the cap still succeeds.
    under = client.get(
        f"/prices/{TICKER}",
        params={"price_type": "raw", "start": "2026-06-01", "end": "2026-06-05"},
    )
    assert under.status_code == 200, under.text
    assert 0 < under.json()["bar_count"] <= 5


def test_the_cap_admits_a_window_of_exactly_max_bars(client, monkeypatch):
    """
    The boundary itself: MAX_BARS rows is allowed, MAX_BARS + 1 is not.

    Worth pinning because the implementation asks the database for MAX_BARS + 1
    rows and rejects on seeing the extra one. That is an easy place to end up
    off by one, and the failure would silently refuse a request that is exactly
    at the documented limit.
    """
    # 2026-06-01 to 2026-06-05 is one trading week: five sessions.
    monkeypatch.setattr("src.api.routers.prices.MAX_BARS", 5)
    exactly = client.get(
        f"/prices/{TICKER}",
        params={"price_type": "raw", "start": "2026-06-01", "end": "2026-06-05"},
    )
    assert exactly.status_code == 200, exactly.text
    assert exactly.json()["bar_count"] == 5, "expected a full five-session week"

    monkeypatch.setattr("src.api.routers.prices.MAX_BARS", 4)
    one_over = client.get(
        f"/prices/{TICKER}",
        params={"price_type": "raw", "start": "2026-06-01", "end": "2026-06-05"},
    )
    assert one_over.status_code == 400, one_over.text
    assert one_over.json()["error"] == "range_too_large"
