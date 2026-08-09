"""
The corporate-actions endpoint, which exists to annotate a price chart.

The endpoint is small; its contract is not. A chart annotation is a claim about
WHICH security had an action and WHEN, laid over a price series that made the
same claim independently. If the two disagree, the chart is worse than
unannotated — a 10-for-1 split drawn on the wrong company's series, or absent
from the right one, turns a smooth adjusted line into an unexplained cliff.

So the assertions here are mostly about agreement with `/prices` rather than
about this endpoint alone:

  - the same ticker and the same `as_of` resolve to the same security_id in both
    responses, and a ticker REUSE returns different actions per `as_of`;
  - the split this warehouse is built around is present, with the right ratio on
    the right date;
  - the two "identical numbers, opposite meanings" flags survive the trip.

Requires the stack up and `dbt build` already run. No network and no API key.
"""

import datetime as dt
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from src.api.main import app

pytestmark = pytest.mark.integration

# KLA's 10-for-1 on 2026-06-12 — the same event the split reconciliation and the
# Yahoo de-adjustment guard are both built on.
SPLIT_TICKER = "KLAC"
SPLIT_EX_DATE = dt.date(2026, 6, 12)
SPLIT_RATIO = Decimal(10)

# JPMorgan's 2026-07-06 dividend, whose reference session is 2026-07-02 because
# 2026-07-03 was the observed Independence Day holiday. This is the live instance
# of the hazard the trading calendar exists for, and the endpoint carries the
# resolved session so a consumer can see it rather than recompute it wrongly.
DIVIDEND_TICKER = "JPM"
DIVIDEND_EX_DATE = dt.date(2026, 7, 6)
DIVIDEND_REFERENCE_SESSION = dt.date(2026, 7, 2)


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


def _actions(client, ticker: str, **params) -> dict:
    response = client.get(f"/corporate-actions/{ticker}", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _by_ex_date(body: dict, ex_date: dt.date) -> dict | None:
    return next(
        (a for a in body["actions"] if a["ex_date"] == ex_date.isoformat()), None
    )


# --------------------------------------------------------------------------
# Non-vacuity. Without real actions in the window, everything below is trivial.
# --------------------------------------------------------------------------


def test_the_warehouse_actually_holds_the_events_under_test(db_engine):
    """
    Guard: the split and the dividend are really present, in the intermediate
    model this endpoint reads — not merely in raw.

    Without this, an endpoint that returned an empty list for everything would
    pass several tests below by never contradicting them.
    """
    with db_engine.connect() as conn:
        split = conn.execute(
            text("""
                SELECT f.split_ratio
                FROM intermediate.int_corporate_actions__factors f
                JOIN marts.dim_security d ON d.security_id = f.security_id
                WHERE d.ticker = :t AND f.ex_date = :ex
            """),
            {"t": SPLIT_TICKER, "ex": SPLIT_EX_DATE},
        ).scalar()

        dividend = conn.execute(
            text("""
                SELECT f.dividend_amount
                FROM intermediate.int_corporate_actions__factors f
                JOIN marts.dim_security d ON d.security_id = f.security_id
                WHERE d.ticker = :t AND f.ex_date = :ex
            """),
            {"t": DIVIDEND_TICKER, "ex": DIVIDEND_EX_DATE},
        ).scalar()

    assert split == SPLIT_RATIO, f"KLAC's 10:1 on {SPLIT_EX_DATE} is missing from the model"
    assert dividend is not None, f"JPM's {DIVIDEND_EX_DATE} dividend is missing from the model"


# --------------------------------------------------------------------------
# The events themselves.
# --------------------------------------------------------------------------


def test_the_split_is_served_with_its_ratio_and_date(client):
    """
    The annotation a chart actually draws: a 10-for-1 on 2026-06-12.

    The ratio is compared as a Decimal parsed from a string. An endpoint
    emitting a JSON number would still pass an `== 10` check here, which is why
    the string type is asserted separately below rather than trusted.
    """
    body = _actions(client, SPLIT_TICKER, start="2026-01-01", end="2026-12-31")
    action = _by_ex_date(body, SPLIT_EX_DATE)

    assert action is not None, f"no action on {SPLIT_EX_DATE}: {body['actions']}"
    assert Decimal(action["split_ratio"]) == SPLIT_RATIO
    # A split-only event carries the dividend leg as the identity, not as null.
    assert action["dividend_amount"] is None
    assert Decimal(action["dividend_factor"]) == 1


def test_the_dividend_carries_the_calendar_resolved_reference_session(client):
    """
    The whole reason the trading calendar exists, visible in the response.

    `ex_date - 1 day` for 2026-07-06 is Sunday 2026-07-05. The correct previous
    SESSION is 2026-07-02, because 2026-07-03 was the observed Independence Day
    holiday. An endpoint that served the naive answer — or served nothing and
    left the consumer to compute it — would push this exact bug into every
    client that annotates a chart.
    """
    body = _actions(client, DIVIDEND_TICKER, start="2026-01-01", end="2026-12-31")
    action = _by_ex_date(body, DIVIDEND_EX_DATE)

    assert action is not None, f"no action on {DIVIDEND_EX_DATE}: {body['actions']}"
    assert action["reference_session_date"] == DIVIDEND_REFERENCE_SESSION.isoformat()
    assert (DIVIDEND_EX_DATE - DIVIDEND_REFERENCE_SESSION).days == 4, (
        "the gap must be four days, or this test has stopped covering the holiday"
    )

    # And the factor really is derived from that session's close, not asserted.
    reference_close = Decimal(action["reference_close"])
    dividend = Decimal(action["dividend_amount"])
    expected = 1 - (dividend / reference_close)
    assert abs(Decimal(action["dividend_factor"]) - expected) < Decimal("1e-12")


def test_money_crosses_the_wire_as_a_string(client):
    """
    ADR-0009 §5, at this endpoint too.

    JSON's only numeric type is a double. These are the inputs the adjustment
    factors are built from, so emitting one as a float here while serving the
    resulting adjusted close as an exact decimal would lose precision at an
    especially odd place to lose it.
    """
    body = _actions(client, DIVIDEND_TICKER, start="2026-01-01", end="2026-12-31")
    action = _by_ex_date(body, DIVIDEND_EX_DATE)

    for field in ("split_ratio", "dividend_amount", "reference_close", "dividend_factor"):
        assert isinstance(action[field], str), f"{field} was serialised as {type(action[field])}"


def test_a_security_with_no_actions_is_an_empty_list_not_a_404(client):
    """
    Most securities have no actions in any given window. That is not an error,
    and an endpoint that 404'd here would make "this company never split" look
    identical to "this ticker does not exist".
    """
    body = _actions(client, SPLIT_TICKER, start="2020-01-01", end="2020-12-31")

    assert body["action_count"] == 0
    assert body["actions"] == []
    assert body["security_id"] > 0, "the security still resolved; only the window was empty"


# --------------------------------------------------------------------------
# Agreement with /prices. The reason this endpoint is on the API at all.
# --------------------------------------------------------------------------


def test_resolution_agrees_with_the_prices_endpoint(client):
    """
    Same ticker, same as_of, same security_id — in both responses.

    This is what makes overlaying the two sound. The dashboard draws a series
    from /prices and annotations from here; if the two resolved differently,
    the chart would silently attribute one company's split to another's prices.
    Sharing `resolve_security` is what guarantees it, and this asserts the
    guarantee rather than trusting the shared import.
    """
    params = {"as_of": "2026-08-03", "start": "2026-06-01", "end": "2026-08-03"}

    actions = _actions(client, SPLIT_TICKER, **params)
    prices = client.get(
        f"/prices/{SPLIT_TICKER}", params={**params, "price_type": "split_adjusted"}
    )
    assert prices.status_code == 200, prices.text

    assert actions["security_id"] == prices.json()["security_id"]
    assert actions["as_of"] == prices.json()["as_of"]


def test_the_split_falls_inside_the_price_window_it_annotates(client):
    """
    An annotation outside the series is invisible; one inside it must line up
    with a real session.

    Checks the ex-date is a date the price endpoint actually returns a bar for.
    A split annotated on a non-session date would be drawn at a point the chart
    has no x-value for, which clients handle by silently dropping it.
    """
    window = {"start": "2026-06-01", "end": "2026-08-03"}

    actions = _actions(client, SPLIT_TICKER, **window)
    prices = client.get(
        f"/prices/{SPLIT_TICKER}", params={**window, "price_type": "split_adjusted"}
    )
    bar_dates = {b["trading_date"] for b in prices.json()["bars"]}

    assert SPLIT_EX_DATE.isoformat() in bar_dates, "the split's ex-date is not a session"
    assert _by_ex_date(actions, SPLIT_EX_DATE) is not None


# --------------------------------------------------------------------------
# The resolution contracts, inherited unchanged.
# --------------------------------------------------------------------------


def test_an_unknown_ticker_is_a_404_with_the_shared_error_envelope(client):
    response = client.get("/corporate-actions/ZZNOSUCH")

    assert response.status_code == 404, response.text
    assert response.json()["error"] == "security_not_found"


def test_lookup_is_case_insensitive(client):
    lower = client.get(f"/corporate-actions/{SPLIT_TICKER.lower()}")
    upper = client.get(f"/corporate-actions/{SPLIT_TICKER}")

    assert lower.status_code == 200, lower.text
    assert lower.json()["security_id"] == upper.json()["security_id"]
    assert lower.json()["ticker"] == SPLIT_TICKER, "the echoed ticker is normalised"


def test_an_inverted_range_is_rejected(client):
    response = client.get(
        f"/corporate-actions/{SPLIT_TICKER}",
        params={"start": "2026-12-31", "end": "2026-01-01"},
    )

    assert response.status_code == 400, response.text
    assert response.json()["error"] == "invalid_range"


def test_as_of_selects_which_security_the_actions_belong_to(client, db_engine):
    """
    Ticker reuse, at this endpoint.

    Two unrelated securities share a ticker over disjoint windows and each has
    an action. Asking with an `as_of` inside either window must return that
    company's action and only that one — a bare ticker match would return both,
    and a chart would be annotated with a split that happened to a different
    company a decade earlier.

    The fixture is local to this test rather than shared with
    test_api_point_in_time.py: that module's rows go straight into the marts,
    while actions have to enter through `raw` so the intermediate view computes
    them, which is a different insertion point for a different layer.

    THE ACTIONS ARE WRITTEN UNDER source='polygon', not a synthetic source, and
    that is forced rather than sloppy. stg_polygon__corporate_actions filters
    `where source = 'polygon'`, so rows under any other source are invisible to
    the view this endpoint reads, and the test would assert against two empty
    lists and pass for the wrong reason. The project's fixture convention allows
    exactly this — "a distinct `source` value OR a ZZ-prefixed ticker" — and the
    ZZ ticker plus the far-out-of-sequence security_ids keep these rows
    unmistakable. The dim_security rows, which nothing filters by source, keep
    the synthetic marker.
    """
    ids = (9_100_001, 9_100_002)
    ticker = "ZZCACT"
    source = "zz_synthetic_ca_test"

    def _cleanup(conn):
        conn.execute(
            text("DELETE FROM raw.corporate_actions WHERE security_id = ANY(:ids)"),
            {"ids": list(ids)},
        )
        conn.execute(
            text("DELETE FROM marts.dim_security WHERE security_id = ANY(:ids)"),
            {"ids": list(ids)},
        )
        conn.execute(
            text("DELETE FROM raw.security_identity WHERE security_id = ANY(:ids)"),
            {"ids": list(ids)},
        )

    with db_engine.begin() as conn:
        _cleanup(conn)
        conn.execute(
            text("""
                INSERT INTO raw.security_identity
                    (security_id, identity_key, identity_kind)
                VALUES
                    (:a, 'figi:ZZCACTALPHA', 'figi'),
                    (:b, 'figi:ZZCACTBETA1', 'figi')
            """),
            {"a": ids[0], "b": ids[1]},
        )
        conn.execute(
            text("""
                INSERT INTO marts.dim_security (
                    security_id, ticker, security_name, figi, primary_exchange_mic,
                    currency_code, security_type, is_active, valid_from, valid_to,
                    known_from, source, ingested_at
                ) VALUES
                    (:a, :t, 'ZZ CA Alpha', 'ZZCACTALPHA', 'XNYS', 'USD', 'CS',
                     false, '2015-01-01', '2018-12-31', now(), :s, now()),
                    (:b, :t, 'ZZ CA Beta',  'ZZCACTBETA1', 'XNYS', 'USD', 'CS',
                     true,  '2020-01-01', NULL,         now(), :s, now())
            """),
            {"a": ids[0], "b": ids[1], "t": ticker, "s": source},
        )
        conn.execute(
            text("""
                INSERT INTO raw.corporate_actions
                    (security_id, ticker, action_type, ex_date, split_to, split_from,
                     source, ingested_at)
                VALUES
                    (:a, :t, 'split', '2016-06-01', 2, 1, 'polygon', now()),
                    (:b, :t, 'split', '2021-06-01', 3, 1, 'polygon', now())
            """),
            {"a": ids[0], "b": ids[1], "t": ticker},
        )

    try:
        alpha = _actions(client, ticker, as_of="2016-06-01")
        beta = _actions(client, ticker, as_of="2021-06-01")

        assert alpha["security_id"] == ids[0]
        assert beta["security_id"] == ids[1]
        assert [a["ex_date"] for a in alpha["actions"]] == ["2016-06-01"]
        assert [a["ex_date"] for a in beta["actions"]] == ["2021-06-01"]

        # Named explicitly: two responses, one ticker, nothing in common.
        assert alpha["security_id"] != beta["security_id"]
        assert not (
            {a["ex_date"] for a in alpha["actions"]}
            & {a["ex_date"] for a in beta["actions"]}
        )
    finally:
        with db_engine.begin() as conn:
            _cleanup(conn)
