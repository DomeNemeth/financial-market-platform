import json
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from src.ingestion.adapters.yahoo import YahooAdapter, is_incomplete_session_bar


def load_fixture(filename: str) -> dict:
    return json.loads((Path("tests/fixtures") / filename).read_text())


#: A real capture of KLAC across its 2026-06-12 10-for-1 split: three bars
#: before the ex-date, the ex-date bar, and one after. The split boundary is
#: INSIDE the fixture on purpose — it is what makes the basis assertions below
#: non-vacuous. A fixture from a quiet week would let a de-adjusting adapter
#: pass every one of them.
FIXTURE = "yahoo_chart_response.json"


class TestYahooAdapterFetch:

    def test_parses_standard_response_correctly(self):
        adapter = YahooAdapter()

        with patch.object(adapter, "_get", return_value=load_fixture(FIXTURE)):
            df = adapter.fetch("KLAC", date(2026, 6, 9), date(2026, 6, 15))

        assert len(df) == 5
        assert set(df["ticker"]) == {"KLAC"}
        assert set(df["source"]) == {"yahoo"}

    def test_trading_dates_come_from_the_exchange_timezone(self):
        """
        Yahoo stamps a daily bar at the session OPEN in exchange-local time
        (13:30Z under EDT), not at midnight UTC like Polygon. Reading the UTC
        date happens to work for US equities and would silently break for any
        exchange east of Greenwich; the adapter converts to the timezone the
        response itself declares.
        """
        adapter = YahooAdapter()

        with patch.object(adapter, "_get", return_value=load_fixture(FIXTURE)):
            df = adapter.fetch("KLAC", date(2026, 6, 9), date(2026, 6, 15))

        assert list(df["trading_date"]) == [
            date(2026, 6, 9),
            date(2026, 6, 10),
            date(2026, 6, 11),
            date(2026, 6, 12),
            date(2026, 6, 15),
        ]

    def test_vwap_and_trade_count_are_null_never_fabricated(self):
        """
        ADR-0006. Yahoo's chart endpoint supplies neither field. Both could be
        faked convincingly — vwap as (h+l+c)/3, trade_count as 0 — and nothing
        downstream could tell. They are NULL, and a NULL is a value a consumer
        can handle correctly where a plausible wrong number is not.
        """
        adapter = YahooAdapter()

        with patch.object(adapter, "_get", return_value=load_fixture(FIXTURE)):
            df = adapter.fetch("KLAC", date(2026, 6, 9), date(2026, 6, 15))

        assert df["vwap"].isna().all()
        assert df["trade_count"].isna().all()

    def test_bars_are_landed_on_yahoos_own_basis_not_de_adjusted(self):
        """
        Raw stays raw (ADR-0008). Yahoo back-adjusts for splits and offers no
        flag to turn it off, so its pre-split KLAC bars are a tenth of the
        prints Polygon reports. The adapter must NOT correct that — the
        de-adjustment belongs in dbt's int_prices_merged, where it can be
        reconciled against Polygon and where raw.prices stays faithful to what
        the vendor actually said.

        Non-vacuity: the two closes asserted below straddle the ex-date, and the
        guard checks the fixture really does contain the ~10x basis step. An
        adapter that de-adjusted would land 2411.64 on 2026-06-11 and fail.
        """
        adapter = YahooAdapter()

        with patch.object(adapter, "_get", return_value=load_fixture(FIXTURE)):
            df = adapter.fetch("KLAC", date(2026, 6, 9), date(2026, 6, 15))

        by_date = df.set_index("trading_date")["close"]
        pre_split = by_date[date(2026, 6, 11)]
        ex_date = by_date[date(2026, 6, 12)]

        # Guard: the fixture straddles a real ~10x basis change. Polygon's raw
        # close for 2026-06-11 is 2411.64, ten times what Yahoo reports here.
        assert pre_split == pytest.approx(241.164, abs=1e-3)
        assert ex_date == pytest.approx(254.54, abs=1e-3)
        # ...and the step is absent from Yahoo's series precisely because Yahoo
        # already removed it. Adjacent closes differ by ~5%, not by ~90%.
        assert abs(ex_date / pre_split - 1) < 0.10

    def test_null_closes_are_skipped(self):
        """
        Yahoo pads its parallel arrays with nulls for sessions it has no print
        for — halts, and the in-progress session on an intraday fetch. Landing
        those would put a NULL close in raw and make the row count disagree with
        the session count for no reason.
        """
        fixture = load_fixture(FIXTURE)
        quote = fixture["chart"]["result"][0]["indicators"]["quote"][0]
        quote["close"][1] = None

        adapter = YahooAdapter()
        with patch.object(adapter, "_get", return_value=fixture):
            df = adapter.fetch("KLAC", date(2026, 6, 9), date(2026, 6, 15))

        assert len(df) == 4
        assert date(2026, 6, 10) not in set(df["trading_date"])

    def test_short_parallel_array_does_not_lose_earlier_bars(self):
        """
        The quote arrays are documented to match `timestamp` in length. When one
        does not, the alternative to the adapter's guard is an IndexError
        part-way through a ticker, which ADR-0011 turns into a failed ticker —
        discarding bars that were fetched perfectly well.
        """
        fixture = load_fixture(FIXTURE)
        fixture["chart"]["result"][0]["indicators"]["quote"][0]["open"] = [217.0, 216.6]

        adapter = YahooAdapter()
        with patch.object(adapter, "_get", return_value=fixture):
            df = adapter.fetch("KLAC", date(2026, 6, 9), date(2026, 6, 15))

        assert len(df) == 5
        assert df["open"].isna().sum() == 3
        assert df["close"].notna().all()

    def test_empty_result_returns_empty_dataframe(self):
        adapter = YahooAdapter()

        with patch.object(adapter, "_get", return_value={"chart": {"result": None}}):
            df = adapter.fetch("ZZZZ", date(2026, 6, 9), date(2026, 6, 15))

        assert df.empty
        assert "ticker" in df.columns
        assert "trading_date" in df.columns

    def test_missing_chart_key_returns_empty_dataframe(self):
        adapter = YahooAdapter()

        with patch.object(adapter, "_get", return_value={}):
            df = adapter.fetch("ZZZZ", date(2026, 6, 9), date(2026, 6, 15))

        assert df.empty

    def test_period2_is_widened_so_the_end_date_bar_is_included(self):
        """
        Yahoo's period2 bounds an INSTANT, not a day. A bar stamped 13:30Z on
        the end date is excluded by a bound at 00:00Z that same day, so the
        range is widened by a whole day rather than by a few hours — hours would
        be correct under EDT and wrong under EST.
        """
        adapter = YahooAdapter()

        with patch.object(adapter, "_get", return_value={}) as mock_get:
            adapter.fetch("KLAC", date(2026, 6, 9), date(2026, 6, 15))

        _, params = mock_get.call_args[0]
        assert params["period1"] == 1780963200   # 2026-06-09T00:00:00Z
        assert params["period2"] == 1781568000   # 2026-06-16T00:00:00Z, i.e. end + 1 day
        # The bound the naive implementation would send, and the bug it causes:
        # 2026-06-15T00:00:00Z is BEFORE that session's 13:30Z bar, so the last
        # requested day would be missing from every fetch.
        assert params["period2"] - params["period1"] == 7 * 86400


class TestIncompleteSessionBars:
    """
    Yahoo returns a bar for the CURRENT session while it is still trading;
    Polygon does not. During market hours that partial bar is therefore the only
    source for today and wins the fallback slot unopposed under ADR-0006.

    The constants below are a real capture from 2026-08-04, when this was caught
    in the warehouse: AAPL's bar for that date had a volume of 25.2M against the
    previous session's 74.8M, because the session was about a third done.
    """

    # NYSE regular session on 2026-08-04, from Yahoo's currentTradingPeriod.
    SESSION_START = 1785850200   # 13:30Z  09:30 EDT
    SESSION_END = 1785873600     # 20:00Z  16:00 EDT
    MID_SESSION = 1785861015     # 16:30Z  the live regularMarketTime observed
    META = {"currentTradingPeriod": {"regular": {
        "start": SESSION_START, "end": SESSION_END,
    }}}

    def test_live_bar_during_the_session_is_incomplete(self):
        assert is_incomplete_session_bar(
            self.MID_SESSION, self.META, now_timestamp=self.MID_SESSION + 1
        )

    def test_same_bar_is_complete_once_the_session_has_closed(self):
        """
        The half of the condition that stops this test being a blanket 'drop
        today'. After the close the bar is settled and wanted; a rule keyed only
        on the date would discard it until midnight.
        """
        assert not is_incomplete_session_bar(
            self.MID_SESSION, self.META, now_timestamp=self.SESSION_END + 1
        )

    def test_completed_earlier_session_is_never_dropped(self):
        """
        A finished bar is stamped at its own session OPEN, which is outside the
        CURRENT trading period, so the window test excludes it regardless of the
        time of day.
        """
        yesterday_open = self.SESSION_START - 86400
        assert not is_incomplete_session_bar(
            yesterday_open, self.META, now_timestamp=self.MID_SESSION
        )

    def test_missing_metadata_keeps_the_bar(self):
        """
        Safe direction. Keeping a possibly-partial bar is recoverable — the
        trailing window re-fetches and the idempotent load overwrites it with
        settled values — whereas dropping bars because a field was absent would
        silently shorten every series.
        """
        assert not is_incomplete_session_bar(self.MID_SESSION, {}, now_timestamp=self.MID_SESSION)
        assert not is_incomplete_session_bar(
            self.MID_SESSION, {"currentTradingPeriod": {}}, now_timestamp=self.MID_SESSION
        )

    def test_fetch_drops_the_in_progress_bar_and_keeps_the_rest(self):
        """
        End to end through fetch(), with the fixture's last bar rewritten to look
        like a live one. Non-vacuity: the four earlier bars must survive, so a
        rule that simply dropped everything would fail this.
        """
        fixture = load_fixture(FIXTURE)
        result = fixture["chart"]["result"][0]
        result["meta"]["currentTradingPeriod"] = {"regular": {
            "start": result["timestamp"][-1] - 3600,
            # Far enough ahead that the session is open whenever this test runs.
            "end": 4102444800,   # 2100-01-01
        }}

        adapter = YahooAdapter()
        with patch.object(adapter, "_get", return_value=fixture):
            df = adapter.fetch("KLAC", date(2026, 6, 9), date(2026, 6, 15))

        assert len(df) == 4
        assert date(2026, 6, 15) not in set(df["trading_date"])
        assert date(2026, 6, 12) in set(df["trading_date"])


class TestYahooAdapterNoDataStatus:

    def test_404_is_translated_to_no_data_not_raised(self):
        """
        Yahoo encodes "unknown symbol / no data in range" as a 404 with a JSON
        error body, where Polygon encodes the same condition as a 200 with an
        empty results list. BaseAdapter's contract says that condition returns
        an empty DataFrame rather than raising, so the two adapters must behave
        identically for identical input.
        """
        adapter = YahooAdapter()

        class FakeResponse:
            status_code = 404

            @staticmethod
            def json():
                return {"chart": {"result": None, "error": {
                    "code": "Not Found",
                    "description": "No data found, symbol may be delisted",
                }}}

        with patch.object(adapter.session, "get", return_value=FakeResponse()):
            assert adapter._get("/v8/finance/chart/ZZZZ") == {}

    def test_server_errors_still_raise(self):
        """
        The 404 carve-out is exactly as wide as the condition that justifies it.
        A 500 is a transport failure, not an absence of data, and must not be
        laundered into an empty DataFrame — that would report a successful run
        that ingested nothing.
        """
        import requests

        adapter = YahooAdapter()

        class FakeResponse:
            status_code = 500

            @staticmethod
            def raise_for_status():
                raise requests.HTTPError("500 Server Error")

        with patch.object(adapter.session, "get", return_value=FakeResponse()):
            with pytest.raises(requests.HTTPError):
                adapter._get("/v8/finance/chart/KLAC")


class TestYahooAdapterValidate:

    def test_raises_on_missing_required_column(self):
        adapter = YahooAdapter()
        df = pd.DataFrame({"ticker": ["KLAC"], "close": [254.54]})

        with pytest.raises(ValueError, match="missing required columns"):
            adapter.validate(df)

    def test_coerces_numeric_types(self):
        adapter = YahooAdapter()
        df = pd.DataFrame({
            "ticker": ["KLAC"],
            "trading_date": ["2026-06-12"],
            "open": ["237.60"],
            "high": ["254.93"],
            "low": ["236.00"],
            "close": ["254.54"],
            "volume": ["10056600"],
            "source": ["yahoo"],
        })
        result = adapter.validate(df)

        assert result["close"].dtype == float
        assert result["trading_date"].iloc[0] == date(2026, 6, 12)

    def test_null_vwap_survives_validation_as_null(self):
        """
        The coercion must not turn an explicit NULL into a NaN that psycopg2
        adapts to a literal Postgres NaN — NUMERIC accepts it silently and it
        poisons every aggregate over the column. to_records() is what converts
        it to None at the driver boundary; this asserts validate() leaves it in
        a state to_records() can still recognise as missing.
        """
        adapter = YahooAdapter()
        df = pd.DataFrame({
            "ticker": ["KLAC"],
            "trading_date": ["2026-06-12"],
            "open": [237.60], "high": [254.93], "low": [236.00], "close": [254.54],
            "volume": [10056600.0],
            "vwap": [None],
            "trade_count": [None],
            "source": ["yahoo"],
        })

        result = adapter.validate(df)

        assert pd.isna(result["vwap"].iloc[0])
        assert pd.isna(result["trade_count"].iloc[0])

    def test_empty_dataframe_passes_validation(self):
        adapter = YahooAdapter()
        assert adapter.validate(adapter._empty_dataframe()).empty
