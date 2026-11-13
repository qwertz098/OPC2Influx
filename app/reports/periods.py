"""
Report periods: boundaries of the last completed day / week / month and when
its report is due.

All calendar arithmetic is done on naive wall-clock datetimes. They are turned
into real instants only at the end, either as UTC or as the machine's local
time; the conversion of a naive datetime via ``astimezone()`` uses the
operating system's rules, so daylight saving changes are handled.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

PERIODS = ("daily", "weekly", "monthly")
PERIOD_LABELS = {"daily": "Daily", "weekly": "Weekly", "monthly": "Monthly"}


@dataclass(frozen=True)
class Period:
    kind: str          # daily / weekly / monthly
    key: str           # e.g. 2026-11-12, 2026-W46, 2026-11
    start: datetime    # aware, inclusive
    end: datetime      # aware, exclusive
    prev_start: datetime  # start of the period before (its end is ``start``)
    label: str         # human readable, e.g. "Thu 12.11.2026"


def parse_send_time(value: str) -> timedelta:
    """'HH:MM' → offset after midnight; invalid input falls back to 00:15."""
    try:
        h, m = (int(x) for x in value.strip().split(":"))
        if 0 <= h < 24 and 0 <= m < 60:
            return timedelta(hours=h, minutes=m)
    except (ValueError, AttributeError):
        pass
    return timedelta(minutes=15)


def wall_now(tz_mode: str) -> datetime:
    """Current wall-clock time (naive) in the chosen time base."""
    if tz_mode == "utc":
        return datetime.now(timezone.utc).replace(tzinfo=None)
    return datetime.now()


def to_instant(wall: datetime, tz_mode: str) -> datetime:
    """Naive wall-clock time → aware datetime."""
    if tz_mode == "utc":
        return wall.replace(tzinfo=timezone.utc)
    return wall.astimezone()   # machine's local rules, DST included


def _month_start(d: date, months_back: int = 0) -> date:
    y, m = d.year, d.month - months_back
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, 1)


def _period_containing_end(kind: str, end_day: date) -> tuple[date, date, date]:
    """(prev_start, start, end) dates of the period that ends at end_day 00:00."""
    if kind == "daily":
        start = end_day - timedelta(days=1)
        return start - timedelta(days=1), start, end_day
    if kind == "weekly":
        start = end_day - timedelta(days=7)
        return start - timedelta(days=7), start, end_day
    start = _month_start(end_day, 1)
    return _month_start(end_day, 2), start, end_day


def _latest_end(kind: str, today: date) -> date:
    """End (00:00) of the most recent completed period, as a date."""
    if kind == "daily":
        return today
    if kind == "weekly":
        return today - timedelta(days=today.weekday())   # this week's Monday
    return date(today.year, today.month, 1)


def _previous_end(kind: str, end: date) -> date:
    if kind == "daily":
        return end - timedelta(days=1)
    if kind == "weekly":
        return end - timedelta(days=7)
    return _month_start(end, 1)


def _key_and_label(kind: str, start: date) -> tuple[str, str]:
    if kind == "daily":
        return start.isoformat(), start.strftime("%a %d.%m.%Y")
    if kind == "weekly":
        y, w, _ = start.isocalendar()
        end = start + timedelta(days=6)
        return f"{y}-W{w:02d}", f"Week {w} ({start.strftime('%d.%m.')} – {end.strftime('%d.%m.%Y')})"
    return start.strftime("%Y-%m"), start.strftime("%B %Y")


def make_period(kind: str, end_day: date, tz_mode: str) -> Period:
    prev_start, start, end = _period_containing_end(kind, end_day)
    key, label = _key_and_label(kind, start)
    inst = lambda d: to_instant(datetime(d.year, d.month, d.day), tz_mode)  # noqa: E731
    return Period(kind, key, inst(start), inst(end), inst(prev_start), label)


def due_period(kind: str, now_wall: datetime, send_time: timedelta, tz_mode: str) -> Period:
    """The most recent period whose report time (end + send_time) has passed."""
    end = _latest_end(kind, now_wall.date())
    if now_wall < datetime(end.year, end.month, end.day) + send_time:
        end = _previous_end(kind, end)
    return make_period(kind, end, tz_mode)


def last_completed(kind: str, now_wall: datetime, tz_mode: str) -> Period:
    """The last period that has ended, regardless of the send time ("send now")."""
    return make_period(kind, _latest_end(kind, now_wall.date()), tz_mode)
