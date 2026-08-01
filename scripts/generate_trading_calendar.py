"""
Generate dbt/seeds/trading_calendar.csv from `exchange_calendars`.

The seed is committed so dbt tests do not depend on a Python package at run
time, and so the calendar a given result was validated against is pinned in
version control rather than floating with a dependency upgrade.

Regenerate when extending the range or bumping exchange-calendars:
    .venv\\Scripts\\python.exe scripts\\generate_trading_calendar.py
"""

import argparse
import csv
from datetime import date
from pathlib import Path

from src.common.calendar import DEFAULT_CALENDAR, calendar_bounds, trading_days

SEED_PATH = Path(__file__).resolve().parents[1] / "dbt" / "seeds" / "trading_calendar.csv"

# 2015 comfortably predates any price history this project ingests, and the
# forward end is clamped to what the calendar can actually answer for.
DEFAULT_START = date(2015, 1, 1)
DEFAULT_END = date(2027, 12, 31)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the trading calendar seed")
    parser.add_argument("--calendar", default=DEFAULT_CALENDAR)
    parser.add_argument("--start", type=date.fromisoformat, default=DEFAULT_START)
    parser.add_argument("--end", type=date.fromisoformat, default=DEFAULT_END)
    args = parser.parse_args()

    first, last = calendar_bounds(args.calendar)
    start = max(args.start, first)
    end = min(args.end, last)
    if end < args.end:
        print(f"NOTE: clamped end {args.end} -> {end} ({args.calendar} generates no further)")

    sessions = trading_days(start, end, args.calendar)

    SEED_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SEED_PATH.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(["calendar", "session_date", "year", "month", "day_of_week"])
        for session in sessions:
            writer.writerow([
                args.calendar,
                session.isoformat(),
                session.year,
                session.month,
                session.isoweekday(),
            ])

    print(f"Wrote {len(sessions)} sessions ({start} → {end}) to {SEED_PATH}")


if __name__ == "__main__":
    main()
