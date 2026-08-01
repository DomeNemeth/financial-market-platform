"""
Trading calendar helpers, wrapping `exchange_calendars`.

Everything in this project that reasons about "the previous trading day" or
"which days should have bars" goes through here rather than doing date
arithmetic, because date arithmetic is wrong in ways that do not announce
themselves:

- `ex_date - 1 day` lands on a Sunday roughly a fifth of the time, and on a
  holiday several times a year. ADR-0003's dividend factor needs the close on
  the *last trading day* before the ex-date, and silently reading a
  non-existent bar there yields a NULL factor, which drops the dividend from
  the cumulative product entirely.
- Counting expected rows as `(end - start).days` overstates coverage by ~30%,
  so a completeness check built on it never fires.

The default calendar is XNYS (NYSE). NASDAQ (XNAS) shares the NYSE session
schedule for regular trading days, so US equities across both venues can use it;
anything else must pass its own MIC explicitly.
"""

import datetime as dt
import logging
from functools import lru_cache

import exchange_calendars as xcals
import pandas as pd

logger = logging.getLogger(__name__)

DEFAULT_CALENDAR = "XNYS"


@lru_cache(maxsize=8)
def get_calendar(name: str = DEFAULT_CALENDAR) -> xcals.ExchangeCalendar:
    """
    Return a cached calendar. Construction parses decades of holiday rules and
    costs ~1s, so the cache is what makes per-row calendar lookups viable.
    """
    return xcals.get_calendar(name)


def _to_ts(d: dt.date) -> pd.Timestamp:
    """exchange_calendars works in tz-naive midnight Timestamps."""
    return pd.Timestamp(d)


def is_trading_day(d: dt.date, calendar: str = DEFAULT_CALENDAR) -> bool:
    """True if `d` is a full or half session on `calendar`."""
    return bool(get_calendar(calendar).is_session(_to_ts(d)))


def trading_days(
    start: dt.date, end: dt.date, calendar: str = DEFAULT_CALENDAR
) -> list[dt.date]:
    """
    Every session between `start` and `end`, both inclusive.

    Returns [] rather than raising when the range contains no sessions — a
    Saturday-to-Sunday range is a legitimate empty result, not an error.
    """
    if start > end:
        raise ValueError(f"start {start} is after end {end}")
    sessions = get_calendar(calendar).sessions_in_range(_to_ts(start), _to_ts(end))
    return [s.date() for s in sessions]


def session_count(start: dt.date, end: dt.date, calendar: str = DEFAULT_CALENDAR) -> int:
    """Number of sessions in [start, end]. The denominator for completeness checks."""
    return len(trading_days(start, end, calendar))


def previous_trading_day(d: dt.date, calendar: str = DEFAULT_CALENDAR) -> dt.date:
    """
    The last session strictly before `d`.

    `d` itself need not be a session. This is the function ADR-0003's dividend
    factor depends on: the close used to normalise a dividend is the close on
    the session before the ex-date, which is a Friday for any Monday ex-date and
    is not `ex_date - 1`.
    """
    return get_calendar(calendar).previous_session(_to_ts(d)).date()


def next_trading_day(d: dt.date, calendar: str = DEFAULT_CALENDAR) -> dt.date:
    """The first session strictly after `d`. `d` itself need not be a session."""
    return get_calendar(calendar).next_session(_to_ts(d)).date()


def missing_sessions(
    observed: set[dt.date],
    start: dt.date,
    end: dt.date,
    calendar: str = DEFAULT_CALENDAR,
) -> list[dt.date]:
    """
    Sessions in [start, end] with no corresponding entry in `observed`.

    The completeness primitive: pass the distinct trading_dates present for a
    ticker and get back the gaps. An empty list is the only acceptable result
    for a ticker that was listed across the whole range.
    """
    return [d for d in trading_days(start, end, calendar) if d not in observed]


def calendar_bounds(calendar: str = DEFAULT_CALENDAR) -> tuple[dt.date, dt.date]:
    """
    The first and last session the calendar can answer for.

    Not unbounded: exchange_calendars generates a finite window (roughly one year
    ahead of the package release). Asking beyond it raises, so a long backfill
    should clamp to this rather than discovering the limit mid-run.
    """
    cal = get_calendar(calendar)
    return cal.first_session.date(), cal.last_session.date()
