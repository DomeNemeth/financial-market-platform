"""
The price_type -> column mapping, checked without a database.

This map is the only place in the API where a SQL fragment is built by string
interpolation, and the only place where ADR-0003's naming discipline could be
quietly undone by choosing a different column. Both are cheap to assert
statically, so they are asserted here rather than only through the integration
tests that need a live warehouse.

No DB, no network: importing the router constructs a SQLAlchemy Engine, which
does not connect until a query runs.
"""

import pytest

from src.api.routers.prices import _SERIES_COLUMNS, MAX_BARS, _projection
from src.api.schemas.prices import PriceType

BAR_FIELDS = {"open", "high", "low", "close", "volume", "vwap"}


def test_every_price_type_has_a_mapping():
    """
    Exhaustive over the enum.

    A new PriceType member added without a mapping would be accepted by
    validation and then raise KeyError at request time — a 500 on a request the
    API had already agreed to serve. The router asserts this at import too; this
    test says so in a place that fails during CI rather than at startup.
    """
    assert set(_SERIES_COLUMNS) == set(PriceType)


@pytest.mark.parametrize("price_type", list(PriceType))
def test_every_mapping_covers_exactly_the_bar_fields(price_type):
    """
    A missing key would silently drop a column from the projection, and the bar
    would come back null for a series that does have that data.
    """
    assert set(_SERIES_COLUMNS[price_type]) == BAR_FIELDS


def test_raw_serves_the_unadjusted_columns():
    """`raw` must stay raw — the guarantee the Parquet archive is compared against."""
    assert _SERIES_COLUMNS[PriceType.RAW] == {
        "open": "open_price",
        "high": "high_price",
        "low": "low_price",
        "close": "close_price",
        "volume": "volume",
        "vwap": "vwap",
    }


def test_split_adjusted_serves_only_split_adjusted_columns():
    mapping = _SERIES_COLUMNS[PriceType.SPLIT_ADJUSTED]
    assert all(
        column and column.startswith("split_adjusted_") for column in mapping.values()
    ), mapping


def test_total_return_has_no_intraday_columns():
    """
    ADR-0003 derives only the total-return close.

    A dividend factor is defined against the previous session's close, and there
    is no defensible analogue for an intraday high — so these are None, and the
    endpoint serves explicit nulls. The failure this prevents is someone
    "completing" the mapping by pointing open/high/low at the split-adjusted
    columns, which would produce a bar mixing two different series and looking
    perfectly well-formed.
    """
    mapping = _SERIES_COLUMNS[PriceType.TOTAL_RETURN_ADJUSTED]

    assert mapping["close"] == "total_return_adjusted_close"
    for field in ("open", "high", "low", "vwap"):
        assert mapping[field] is None, f"there is no total-return {field}"


def test_total_return_volume_is_the_split_adjusted_volume():
    """
    Correct, not a fallback: a dividend does not change the share count, so the
    split-adjusted volume already is the right volume for a total-return series.
    """
    assert _SERIES_COLUMNS[PriceType.TOTAL_RETURN_ADJUSTED]["volume"] == "split_adjusted_volume"


def test_nothing_is_called_adjusted_close():
    """
    The naming rule ADR-0003 exists to enforce.

    `adjusted_close` is the industry's standard name and it is ambiguous between
    two series that must not be confused. It does not appear in the warehouse and
    it must not reappear at the API boundary, which is the layer most likely to
    reintroduce it for familiarity's sake.
    """
    columns = [c for mapping in _SERIES_COLUMNS.values() for c in mapping.values() if c]

    assert "adjusted_close" not in columns
    assert not any(c.startswith("adjusted_") for c in columns), columns


@pytest.mark.parametrize("price_type", list(PriceType))
def test_projection_aliases_every_field_including_the_absent_ones(price_type):
    """
    A field with no backing column becomes `NULL AS <field>`, never disappears.

    The bar shape has to be identical across price_types — a response whose keys
    changed with the parameter would push the asymmetry onto every consumer.
    """
    projection = _projection(price_type)

    for field in BAR_FIELDS:
        assert f" AS {field}" in projection, f"{field} missing from projection for {price_type}"

    expected_nulls = sum(1 for column in _SERIES_COLUMNS[price_type].values() if column is None)
    assert projection.count("NULL AS ") == expected_nulls


def test_projection_interpolates_only_known_column_names():
    """
    The projection is built with an f-string, so this asserts the property that
    makes that safe: every interpolated token is an identifier from the closed
    map above, never anything derived from a request.
    """
    known = {c for mapping in _SERIES_COLUMNS.values() for c in mapping.values() if c}

    for price_type in PriceType:
        for fragment in _projection(price_type).split(", "):
            source, _, alias = fragment.partition(" AS ")
            assert source == "NULL" or source in known, f"unexpected column {source!r}"
            assert alias in BAR_FIELDS


def test_the_row_cap_is_a_real_bound():
    """A cap of 0 or None would disable the guard without failing anything else."""
    assert isinstance(MAX_BARS, int)
    # Comfortably more than a decade of daily sessions, so the cap protects
    # against runaway responses rather than truncating ordinary requests.
    assert 2_500 <= MAX_BARS <= 100_000
