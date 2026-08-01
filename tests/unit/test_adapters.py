import json
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from src.ingestion.adapters.polygon import PolygonAdapter


def load_fixture(filename: str) -> dict:
    return json.loads((Path("tests/fixtures") / filename).read_text())


def make_adapter() -> PolygonAdapter:
    """Create adapter without needing a real API key in tests."""
    with patch("src.common.config.settings") as mock_settings:
        mock_settings.polygon_api_key = "test_key"
        return PolygonAdapter()


class TestPolygonAdapterFetch:

    def test_parses_standard_response_correctly(self):
        fixture = load_fixture("polygon_ohlcv_response.json")
        adapter = make_adapter()

        with patch.object(adapter, "_get", return_value=fixture):
            df = adapter.fetch("AAPL", date(2024, 1, 15), date(2024, 1, 17))

        assert len(df) == 3
        assert list(df["ticker"]) == ["AAPL", "AAPL", "AAPL"]
        assert list(df["source"]) == ["polygon", "polygon", "polygon"]

    def test_trading_dates_are_correct_utc(self):
        """Timestamps are midnight UTC — UTC date should match trading date."""
        fixture = load_fixture("polygon_ohlcv_response.json")
        adapter = make_adapter()

        with patch.object(adapter, "_get", return_value=fixture):
            df = adapter.fetch("AAPL", date(2024, 1, 15), date(2024, 1, 17))

        assert df["trading_date"].iloc[0] == date(2024, 1, 15)
        assert df["trading_date"].iloc[1] == date(2024, 1, 16)
        assert df["trading_date"].iloc[2] == date(2024, 1, 17)

    def test_empty_results_returns_empty_dataframe(self):
        """Non-trading days return empty results — should not crash."""
        adapter = make_adapter()
        empty_response = {"results": None, "status": "OK", "count": 0}

        with patch.object(adapter, "_get", return_value=empty_response):
            df = adapter.fetch("AAPL", date(2024, 1, 13), date(2024, 1, 13))

        assert df.empty
        # Should still have the correct columns even when empty
        assert "ticker" in df.columns
        assert "trading_date" in df.columns

    def test_missing_results_key_returns_empty_dataframe(self):
        """API sometimes omits 'results' key entirely."""
        adapter = make_adapter()
        response_without_results = {"status": "OK"}

        with patch.object(adapter, "_get", return_value=response_without_results):
            df = adapter.fetch("AAPL", date(2024, 1, 13), date(2024, 1, 13))

        assert df.empty


class TestPolygonAdapterValidate:

    def test_raises_on_missing_required_column(self):
        adapter = make_adapter()
        df = pd.DataFrame({"ticker": ["AAPL"], "close": [150.0]})

        with pytest.raises(ValueError, match="missing required columns"):
            adapter.validate(df)

    def test_coerces_numeric_types(self):
        adapter = make_adapter()
        df = pd.DataFrame({
            "ticker": ["AAPL"],
            "trading_date": ["2024-01-15"],
            "open": ["184.15"],    # string — should become float
            "high": ["187.28"],
            "low": ["183.88"],
            "close": ["186.86"],
            "volume": ["74282900"],
            "source": ["polygon"],
        })
        result = adapter.validate(df)

        assert result["close"].dtype == float
        assert result["open"].dtype == float

    def test_fractional_volume_survives_validation(self):
        """
        Regression: Polygon reports fractional volume (fractional-share trades
        aggregated), e.g. v=56090840.685498. validate() used to cast volume to
        Int64, which raises TypeError on a non-integral value and killed the
        first real ingestion run. Volume stays floating point through the raw
        layer; dbt staging rounds it to a whole share count.
        """
        adapter = make_adapter()
        df = pd.DataFrame({
            "ticker": ["AAPL"],
            "trading_date": ["2026-07-29"],
            "open": [339.73],
            "high": [344.5699],
            "low": [337.3501],
            "close": [338.19],
            "volume": [56090840.685498],
            "source": ["polygon"],
        })

        result = adapter.validate(df)

        assert result["volume"].iloc[0] == pytest.approx(56090840.685498)

    def test_empty_dataframe_passes_validation(self):
        """validate() should return empty DF, not raise, when input is empty."""
        adapter = make_adapter()
        df = adapter._empty_dataframe()
        result = adapter.validate(df)
        assert result.empty