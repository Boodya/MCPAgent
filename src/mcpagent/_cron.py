"""Minimal cron expression parser — calculates seconds until next fire time.

Supports standard 5-field cron: minute hour day_of_month month day_of_week
Supports: numbers, ranges (1-5), lists (1,3,5), step values (*/5), and *.
"""

from __future__ import annotations

from datetime import datetime, timezone


def _parse_field(field: str, min_val: int, max_val: int) -> set[int]:
    """Parse a single cron field into a set of allowed integer values."""
    values: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if "/" in part:
            range_part, step_str = part.split("/", 1)
            step = int(step_str)
            if range_part == "*":
                start, end = min_val, max_val
            elif "-" in range_part:
                start, end = (int(x) for x in range_part.split("-", 1))
            else:
                start, end = int(range_part), max_val
            values.update(range(start, end + 1, step))
        elif part == "*":
            values.update(range(min_val, max_val + 1))
        elif "-" in part:
            start, end = (int(x) for x in part.split("-", 1))
            values.update(range(start, end + 1))
        else:
            values.add(int(part))
    return values


def next_cron_delay(expression: str) -> float:
    """Return seconds from now until the next matching time for *expression*.

    *expression* must be a standard 5-field cron string:
        ``minute hour day_of_month month day_of_week``

    Day-of-week: 0 = Monday .. 6 = Sunday (ISO), but 7 is also accepted as Sunday.
    """
    parts = expression.strip().split()
    if len(parts) != 5:
        raise ValueError(f"Expected 5 cron fields, got {len(parts)}: {expression!r}")

    minutes = _parse_field(parts[0], 0, 59)
    hours = _parse_field(parts[1], 0, 23)
    days_of_month = _parse_field(parts[2], 1, 31)
    months = _parse_field(parts[3], 1, 12)
    days_of_week = _parse_field(parts[4], 0, 6)
    # Accept 7 as Sunday → convert to 0
    if 7 in days_of_week:
        days_of_week.discard(7)
        days_of_week.add(6)

    now = datetime.now(timezone.utc)
    # Start searching from the next minute
    candidate = now.replace(second=0, microsecond=0)

    # Search up to ~1 year ahead
    for _ in range(525600):  # max minutes in a year
        from datetime import timedelta

        candidate += timedelta(minutes=1)
        if (
            candidate.month in months
            and candidate.day in days_of_month
            and candidate.weekday() in days_of_week
            and candidate.hour in hours
            and candidate.minute in minutes
        ):
            return (candidate - now).total_seconds()

    raise ValueError(f"No matching time found within 1 year for: {expression!r}")
