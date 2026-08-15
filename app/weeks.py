"""Week arithmetic for the planner.

The board shows Monday-Friday. Weekends are deliberately out of scope; adding
them later is a change to WEEKDAY_COUNT and a template, not to the schema,
because plan_days is keyed on real dates.
"""
from datetime import date, timedelta

WEEKDAY_COUNT = 5

WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                 "Saturday", "Sunday"]


def monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


def current_monday() -> date:
    return monday_of(date.today())


def parse_monday(value: str | None) -> date:
    """Resolve the ?week= query parameter to the Monday of that week.

    Anything unparseable falls back to the current week rather than erroring --
    a mangled URL should show you this week, not a stack trace.
    """
    if not value:
        return current_monday()
    try:
        return monday_of(date.fromisoformat(value))
    except ValueError:
        return current_monday()


def weekdays(monday: date) -> list[date]:
    return [monday + timedelta(days=offset) for offset in range(WEEKDAY_COUNT)]
