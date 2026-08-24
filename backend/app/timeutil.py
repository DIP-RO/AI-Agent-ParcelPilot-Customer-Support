"""Time handling: dataset snapshot clock and business-hours arithmetic.

All timestamps in the data pack are naive local times in Asia/Kolkata. The
dataset snapshot declared in the workbook README is the reference "now" for
every time-based computation, so answers are reproducible.

Business-hours assumption (documented): Monday-Friday, 09:00-18:00 IST.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import config

IST = ZoneInfo("Asia/Kolkata")


def parse_ts(value: str) -> datetime:
    """Parse a data-pack timestamp ('2026-08-16 09:00' or ISO) as IST."""
    v = value.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(v, fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    raise ValueError(f"Unrecognized timestamp: {value!r}")


def fmt_ts(dt: datetime) -> str:
    return dt.astimezone(IST).strftime("%Y-%m-%d %H:%M IST (%A)")


def is_business_day(dt: datetime) -> bool:
    return dt.weekday() < 5  # Mon=0 .. Fri=4


def next_business_open(dt: datetime) -> datetime:
    """The earliest business-hours instant at or after dt."""
    cur = dt
    while True:
        if is_business_day(cur):
            start = cur.replace(hour=config.BUSINESS_DAY_START_HOUR, minute=0, second=0, microsecond=0)
            end = cur.replace(hour=config.BUSINESS_DAY_END_HOUR, minute=0, second=0, microsecond=0)
            if cur < start:
                return start
            if cur < end:
                return cur
        cur = (cur + timedelta(days=1)).replace(
            hour=config.BUSINESS_DAY_START_HOUR, minute=0, second=0, microsecond=0
        )


def add_business_hours(start: datetime, hours: float) -> datetime:
    """Advance a timestamp by N business hours (clock only runs Mon-Fri 09:00-18:00)."""
    remaining = timedelta(hours=hours)
    cur = next_business_open(start)
    while remaining > timedelta(0):
        day_end = cur.replace(hour=config.BUSINESS_DAY_END_HOUR, minute=0, second=0, microsecond=0)
        available = day_end - cur
        if remaining <= available:
            return cur + remaining
        remaining -= available
        cur = next_business_open(day_end + timedelta(minutes=1))
    return cur


def business_time_between(start: datetime, end: datetime) -> timedelta:
    """Business time elapsed between two instants."""
    if end <= start:
        return timedelta(0)
    total = timedelta(0)
    cur = next_business_open(start)
    while cur < end:
        day_end = cur.replace(hour=config.BUSINESS_DAY_END_HOUR, minute=0, second=0, microsecond=0)
        segment_end = min(day_end, end)
        if segment_end > cur:
            total += segment_end - cur
        cur = next_business_open(day_end + timedelta(minutes=1))
    return total


def compute_due_at(start: datetime, target: dict) -> datetime:
    """Due timestamp for an SLA target like {"amount": 4, "unit": "business_hours"}.

    Units: minutes / hours (wall clock, i.e. 24x7) and business_hours /
    business_days (clock runs only during business hours).
    """
    amount = target["amount"]
    unit = target["unit"]
    if unit == "minutes":
        return start + timedelta(minutes=amount)
    if unit == "hours":
        return start + timedelta(hours=amount)
    if unit == "business_hours":
        return add_business_hours(start, amount)
    if unit == "business_days":
        return add_business_hours(start, amount * config.BUSINESS_HOURS_PER_DAY)
    raise ValueError(f"Unknown SLA unit: {unit!r}")


def describe_target(target: dict) -> str:
    amount = target["amount"]
    unit = target["unit"].replace("_", " ")
    if amount == 1 and unit.endswith("s"):
        unit = unit[:-1]
    coverage = target.get("coverage")
    return f"{amount} {unit}" + (f" ({coverage})" if coverage else "")


def humanize_delta(delta: timedelta) -> str:
    total_min = int(delta.total_seconds() // 60)
    sign = "-" if total_min < 0 else ""
    total_min = abs(total_min)
    hours, minutes = divmod(total_min, 60)
    if hours >= 24:
        days, hours = divmod(hours, 24)
        return f"{sign}{days}d {hours}h {minutes}m"
    if hours:
        return f"{sign}{hours}h {minutes}m"
    return f"{sign}{minutes}m"
