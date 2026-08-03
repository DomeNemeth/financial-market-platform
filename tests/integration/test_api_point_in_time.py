"""
The point-in-time contract of the API, tested on a reused ticker.

This is the test Phase 4 exists to make possible. Everything else in the API
layer is plumbing; this asserts the one claim that would be silently false
without it: that `as_of` decides WHICH SECURITY a ticker resolves to, and that
two unrelated companies which held the same symbol at different times never
appear in one series.

WHY THE FIXTURE IS SYNTHETIC. Ticker reuse is real and common — exchanges lease
symbols and reassign them after a delisting — but none of the six securities
actually ingested here has ever been reused, and no free data source will
retroactively supply one. A test that could only run against a reused ticker
would therefore never run, which is the same as not having it. The hazard is
constructed instead, and `test_the_hazard_is_actually_present` proves the
construction worked before anything else is asserted.

WHY IT WRITES DIRECTLY INTO THE MARTS. These rows go into marts.dim_security and
marts.fct_security_price_daily, which dbt materialises. That is deliberate: the
API reads those tables, so that is where the fixture has to be for the API's
resolution query to be under test. Seeding raw.security_master instead would
require a full `dbt build` inside a test, which is minutes of runtime to exercise
a code path dbt already has its own tests for. The cost is that `dbt build` drops
these rows — which is fine, because the fixture recreates them and nothing else
depends on them.

Per the project's fixture convention the ticker is ZZ-prefixed and the
security_ids are far outside the real sequence, so these rows can never be
mistaken for ingested data, and the fixture deletes them on the way out.

Requires the stack up and `dbt build` already run (so the mart tables exist).
"""

import datetime as dt
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from src.api.main import app

pytestmark = pytest.mark.integration

# Two unrelated companies, one symbol, disjoint listing windows, and a year in
# the middle when nobody held it. The gap is not decoration: it is the only way
# to distinguish "resolved correctly" from "resolved to whichever row sorted
# first", because a broken implementation still returns *something* for a date
# inside either window.
TICKER = "ZZPIT"

ALPHA_ID = 9_000_001
ALPHA_LISTED = dt.date(2015, 1, 1)
ALPHA_DELISTED = dt.date(2018, 12, 31)
ALPHA_BAR = dt.date(2018, 6, 1)
ALPHA_CLOSE = Decimal("100.000000")

BETA_ID = 9_000_002
BETA_LISTED = dt.date(2020, 1, 1)
BETA_BAR = dt.date(2021, 6, 1)
BETA_CLOSE = Decimal("500.000000")

# In the gap between the two listings. Nobody held the symbol on this date.
ORPHAN_DATE = dt.date(2019, 6, 1)

# A second ticker whose two securities OVERLAP in valid time. That is a data
# defect, and the API's contract is to refuse it rather than pick one.
AMBIGUOUS_TICKER = "ZZAMBIG"
AMBIGUOUS_IDS = (9_000_011, 9_000_012)

SYNTHETIC_IDS = [ALPHA_ID, BETA_ID, *AMBIGUOUS_IDS]
SOURCE = "zz_synthetic_api_test"


def _delete(conn):
    conn.execute(
        text("DELETE FROM marts.fct_security_price_daily WHERE security_id = ANY(:ids)"),
        {"ids": SYNTHETIC_IDS},
    )
    conn.execute(
        text("DELETE FROM marts.dim_security WHERE security_id = ANY(:ids)"),
        {"ids": SYNTHETIC_IDS},
    )


@pytest.fixture(scope="module", autouse=True)
def reused_ticker(db_engine):
    """Build the reused-ticker scenario, and remove it again afterwards."""
    with db_engine.begin() as conn:
        # Delete first: a previous run killed mid-test would otherwise leave rows
        # behind and turn the clean scenario into an ambiguous one.
        _delete(conn)

        conn.execute(
            text("""
                INSERT INTO marts.dim_security (
                    security_id, ticker, security_name, figi, primary_exchange_mic,
                    currency_code, security_type, is_active, valid_from, valid_to,
                    known_from, source, ingested_at
                ) VALUES
                    (:alpha_id, :ticker, 'ZZ Test Alpha Corp', 'BBGZZTESTAL1', 'XNYS',
                     'USD', 'CS', false, :alpha_listed, :alpha_delisted,
                     now(), :source, now()),
                    (:beta_id, :ticker, 'ZZ Test Beta Inc', 'BBGZZTESTBE2', 'XNYS',
                     'USD', 'CS', true, :beta_listed, NULL,
                     now(), :source, now()),
                    (:amb_a, :amb_ticker, 'ZZ Ambiguous A', 'BBGZZTESTAM1', 'XNYS',
                     'USD', 'CS', true, '2015-01-01', '2021-12-31',
                     now(), :source, now()),
                    (:amb_b, :amb_ticker, 'ZZ Ambiguous B', 'BBGZZTESTAM2', 'XNYS',
                     'USD', 'CS', true, '2018-01-01', NULL,
                     now(), :source, now())
            """),
            {
                "alpha_id": ALPHA_ID,
                "beta_id": BETA_ID,
                "amb_a": AMBIGUOUS_IDS[0],
                "amb_b": AMBIGUOUS_IDS[1],
                "ticker": TICKER,
                "amb_ticker": AMBIGUOUS_TICKER,
                "alpha_listed": ALPHA_LISTED,
                "alpha_delisted": ALPHA_DELISTED,
                "beta_listed": BETA_LISTED,
                "source": SOURCE,
            },
        )

        # One bar each. split_factor 1 and the adjusted columns equal to raw, so
        # this file tests resolution only — the adjustment maths is reconciled
        # in its own tests and has no business failing here.
        conn.execute(
            text("""
                INSERT INTO marts.fct_security_price_daily (
                    security_id, trading_date, vendor_ticker, current_ticker,
                    open_price, high_price, low_price, close_price, volume, vwap,
                    trade_count, split_factor, dividend_factor,
                    split_adjusted_open, split_adjusted_high, split_adjusted_low,
                    split_adjusted_close, split_adjusted_volume, split_adjusted_vwap,
                    total_return_adjusted_close, actions_observed_through, built_at,
                    is_exchange_session, source, ingested_at
                ) VALUES
                    (:alpha_id, :alpha_bar, :ticker, :ticker,
                     :alpha_close, :alpha_close, :alpha_close, :alpha_close, 1000, :alpha_close,
                     10, 1, 1,
                     :alpha_close, :alpha_close, :alpha_close, :alpha_close, 1000, :alpha_close,
                     :alpha_close, NULL, now(), true, :source, now()),
                    (:beta_id, :beta_bar, :ticker, :ticker,
                     :beta_close, :beta_close, :beta_close, :beta_close, 2000, :beta_close,
                     20, 1, 1,
                     :beta_close, :beta_close, :beta_close, :beta_close, 2000, :beta_close,
                     :beta_close, NULL, now(), true, :source, now())
            """),
            {
                "alpha_id": ALPHA_ID,
                "beta_id": BETA_ID,
                "ticker": TICKER,
                "alpha_bar": ALPHA_BAR,
                "beta_bar": BETA_BAR,
                "alpha_close": ALPHA_CLOSE,
                "beta_close": BETA_CLOSE,
                "source": SOURCE,
            },
        )

    yield

    with db_engine.begin() as conn:
        _delete(conn)


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


# --------------------------------------------------------------------------
# Non-vacuity. If the hazard is not present, every assertion below holds
# trivially and the file proves nothing.
# --------------------------------------------------------------------------


def test_the_hazard_is_actually_present(db_engine):
    """
    Two securities really do share this ticker, and a bare ticker match really
    would return both.

    This is the guard the whole file rests on. It asserts the *defect* — that
    `where ticker = 'ZZPIT'` is ambiguous — so that when the endpoint returns
    exactly one security below, that is a result rather than an accident of an
    incomplete fixture.
    """
    with db_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT security_id, valid_from, valid_to "
                "FROM marts.dim_security WHERE ticker = :t"
            ),
            {"t": TICKER},
        ).fetchall()

    assert len(rows) == 2, f"expected two securities sharing {TICKER}, got {len(rows)}"
    assert {r[0] for r in rows} == {ALPHA_ID, BETA_ID}

    # And their windows must not overlap, or the correct answer would itself be
    # ambiguous and the 404/one-match assertions would be testing the wrong thing.
    alpha = next(r for r in rows if r[0] == ALPHA_ID)
    beta = next(r for r in rows if r[0] == BETA_ID)
    assert alpha[2] < beta[1], "the two listing windows must be disjoint"


def test_both_securities_have_bars_that_a_naive_join_would_merge(db_engine):
    """
    Each security has price history under the shared ticker.

    Without this, `/prices` could return one security's bars simply because the
    other had none, and the resolution logic would never be exercised.
    """
    with db_engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT security_id, count(*)
                FROM marts.fct_security_price_daily
                WHERE vendor_ticker = :t
                GROUP BY security_id
            """),
            {"t": TICKER},
        ).fetchall()

    assert {r[0]: r[1] for r in rows} == {ALPHA_ID: 1, BETA_ID: 1}


# --------------------------------------------------------------------------
# The claim: as_of selects the security.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "as_of,expected_id,expected_name",
    [
        (dt.date(2016, 6, 1), ALPHA_ID, "ZZ Test Alpha Corp"),
        (dt.date(2018, 12, 31), ALPHA_ID, "ZZ Test Alpha Corp"),  # inclusive upper bound
        (dt.date(2020, 1, 1), BETA_ID, "ZZ Test Beta Inc"),  # inclusive lower bound
        (dt.date(2021, 6, 1), BETA_ID, "ZZ Test Beta Inc"),
    ],
)
def test_as_of_selects_which_security_the_ticker_resolves_to(
    client, as_of, expected_id, expected_name
):
    """
    THE test. One ticker, four dates, two different companies.

    This asserts the *outcome* of resolution, not merely that the endpoint
    returns 200. An implementation matching on ticker alone would return the
    same security_id for all four dates and fail here on two of them — which is
    exactly the bug that would otherwise ship silently, because a bare ticker
    match never errors, it just answers about the wrong company.

    The window boundaries are included because inclusivity is a real decision
    (`>=` / `<=`, matching int_prices_with_calendar) and an off-by-one there
    would move a whole day's history to the wrong company.
    """
    response = client.get(f"/securities/{TICKER}", params={"as_of": as_of.isoformat()})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["security_id"] == expected_id
    assert body["security_name"] == expected_name
    assert body["resolved_as_of"] == as_of.isoformat()


def test_a_date_between_the_two_listings_resolves_to_nothing(client):
    """
    404, not "whichever row we found first".

    The gap year is the sharpest discriminator in the file: a bare ticker match
    returns a security here, and returning one is *wrong* — nobody held the
    symbol in 2019. An API that answers this with a company is confidently
    answering a question about a security that did not exist.
    """
    response = client.get(f"/securities/{TICKER}", params={"as_of": ORPHAN_DATE.isoformat()})

    assert response.status_code == 404, response.text
    assert response.json()["error"] == "security_not_found"


def test_prices_return_only_the_resolved_security_s_bars(client):
    """
    The consequence that actually damages an analysis.

    Both companies' bars sit in the fact table under the same vendor_ticker, and
    a series joined on ticker would splice them into one history running from
    100 to 500 — a fictitious 5x return across a bankruptcy and an unrelated
    IPO. Asking with an as_of inside either window must return that company's
    bars and *only* that company's.
    """
    alpha = client.get(
        f"/prices/{TICKER}",
        params={
            "price_type": "raw", "as_of": "2016-06-01",
            "start": "2010-01-01", "end": "2030-01-01",
        },
    )
    assert alpha.status_code == 200, alpha.text
    alpha_body = alpha.json()
    assert alpha_body["security_id"] == ALPHA_ID
    assert [b["trading_date"] for b in alpha_body["bars"]] == [ALPHA_BAR.isoformat()]
    assert Decimal(alpha_body["bars"][0]["close"]) == ALPHA_CLOSE

    beta = client.get(
        f"/prices/{TICKER}",
        params={
            "price_type": "raw", "as_of": "2021-06-01",
            "start": "2010-01-01", "end": "2030-01-01",
        },
    )
    assert beta.status_code == 200, beta.text
    beta_body = beta.json()
    assert beta_body["security_id"] == BETA_ID
    assert [b["trading_date"] for b in beta_body["bars"]] == [BETA_BAR.isoformat()]
    assert Decimal(beta_body["bars"][0]["close"]) == BETA_CLOSE

    # The splice, named explicitly: the two responses share a ticker and a wide
    # date range, and still have nothing in common.
    assert alpha_body["security_id"] != beta_body["security_id"]
    assert not (
        {b["trading_date"] for b in alpha_body["bars"]}
        & {b["trading_date"] for b in beta_body["bars"]}
    )


def test_as_of_defaults_to_end_rather_than_today(client):
    """
    The default rule from ADR-0009 §3, which is load-bearing for delisted
    securities.

    Requesting Alpha's window with no `as_of` at all must resolve as of `end` —
    2018, when Alpha held the ticker — not as of today, when Beta does. If the
    default were `today`, every historical query about a delisted company would
    silently answer about whoever inherited its symbol, which is the failure
    this endpoint exists to prevent and the one a caller is least likely to
    notice.
    """
    response = client.get(
        f"/prices/{TICKER}",
        params={"price_type": "raw", "start": "2010-01-01", "end": "2018-12-31"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["as_of"] == "2018-12-31", "as_of should have defaulted to end"
    assert body["security_id"] == ALPHA_ID
    assert body["bar_count"] == 1


def test_an_open_ended_request_resolves_as_of_today(client):
    """The other half of the default: no `end` means the question is about now."""
    response = client.get(f"/prices/{TICKER}", params={"price_type": "raw"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["as_of"] == dt.date.today().isoformat()
    assert body["security_id"] == BETA_ID, "today's holder of the ticker is Beta"


# --------------------------------------------------------------------------
# The other failure direction.
# --------------------------------------------------------------------------


def test_overlapping_valid_windows_are_a_conflict_not_a_coin_flip(client):
    """
    409 when two securities claim the same ticker at once.

    This is the direction a naive implementation never even reaches: it would
    return one of them and look perfectly healthy. Refusing is the runtime
    counterpart of assert_price_bars_resolve_to_one_security, and the response
    names both candidates so the data defect is diagnosable from the error alone.
    """
    response = client.get(
        f"/securities/{AMBIGUOUS_TICKER}", params={"as_of": "2019-06-01"}
    )

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["error"] == "ambiguous_ticker"

    candidates = body["details"]["candidates"]
    assert {c["security_id"] for c in candidates} == set(AMBIGUOUS_IDS)


def test_the_ambiguous_ticker_resolves_cleanly_outside_the_overlap(client):
    """
    Non-vacuity for the 409: the fixture must not be *permanently* ambiguous.

    An implementation that returned 409 for every request would pass the test
    above. Here only one security's window covers 2016, so a correct resolver
    answers normally — which proves the 409 is a response to the overlap and not
    to the ticker.
    """
    response = client.get(f"/securities/{AMBIGUOUS_TICKER}", params={"as_of": "2016-06-01"})

    assert response.status_code == 200, response.text
    assert response.json()["security_id"] == AMBIGUOUS_IDS[0]


def test_lookup_is_case_insensitive(client):
    """Tickers are stored upper-case; a lower-case path must not 404."""
    response = client.get(f"/securities/{TICKER.lower()}", params={"as_of": "2021-06-01"})

    assert response.status_code == 200, response.text
    assert response.json()["security_id"] == BETA_ID
