"""Small shared helpers for the UI pages."""
from __future__ import annotations

from datetime import datetime


def format_ts(value: str | None, *, fmt: str = "%b %-d · %H:%M") -> str:
    """Format an ISO timestamp from an event dict, in local time."""
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return str(value)[:16]
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    try:
        return dt.strftime(fmt)
    except ValueError:  # %-d is glibc-only; fall back if unsupported
        return dt.strftime("%b %d · %H:%M")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return dt.astimezone() if dt.tzinfo is not None else dt


def _strftime(dt: datetime, fmt: str, fallback: str) -> str:
    try:
        return dt.strftime(fmt)
    except ValueError:  # %-d / %-I are glibc-only
        return dt.strftime(fallback)


def relative_ts(value: str | None) -> str:
    """Short thread-list stamp, the way Messages.app does it.

    Today → "9:41 AM"; yesterday → "Yesterday"; within the last week →
    weekday name; older → "4/1/25".
    """
    dt = _parse(value)
    if dt is None:
        return ""
    today = datetime.now().astimezone().date() if dt.tzinfo else datetime.now().date()
    delta = (today - dt.date()).days
    if delta <= 0:
        return _strftime(dt, "%-I:%M %p", "%I:%M %p")
    if delta == 1:
        return "Yesterday"
    if delta < 7:
        return dt.strftime("%a")
    return _strftime(dt, "%-m/%-d/%y", "%m/%d/%y")


def separator_ts(value: str | None) -> str:
    """In-conversation date/time divider, e.g. "Today 9:41 AM"."""
    dt = _parse(value)
    if dt is None:
        return ""
    today = datetime.now().astimezone().date() if dt.tzinfo else datetime.now().date()
    delta = (today - dt.date()).days
    time = _strftime(dt, "%-I:%M %p", "%I:%M %p")
    if delta <= 0:
        return f"Today {time}"
    if delta == 1:
        return f"Yesterday {time}"
    if delta < 7:
        return f"{dt.strftime('%a')} {time}"
    return f"{_strftime(dt, '%b %-d, %Y', '%b %d, %Y')} {time}"


def receipt_ts(value: str | None) -> str:
    """Time shown beside a "Read" caption, e.g. "12:52 PM".

    Same shape as `separator_ts` but with today's time left bare: a receipt
    almost always lands the same day it's read, and "Read Today 12:52 PM"
    reads worse than "Read 12:52 PM". Older ones keep the day, because
    "Read 12:52 PM" on a week-old message would be actively misleading.
    """
    dt = _parse(value)
    if dt is None:
        return ""
    today = datetime.now().astimezone().date() if dt.tzinfo else datetime.now().date()
    delta = (today - dt.date()).days
    time = _strftime(dt, "%-I:%M %p", "%I:%M %p")
    if delta <= 0:
        return time
    if delta == 1:
        return f"Yesterday {time}"
    if delta < 7:
        return f"{dt.strftime('%a')} {time}"
    return f"{_strftime(dt, '%b %-d', '%b %d')} {time}"


def clock_ts(value: str | None) -> str:
    """Bare time of day, e.g. "6:24 PM" — the Show Times gutter.

    Deliberately time-only whatever the date: the gutter sits beside a
    conversation that already carries date dividers, and repeating the date on
    every line is what the divider exists to avoid.
    """
    dt = _parse(value)
    if dt is None:
        return ""
    return _strftime(dt, "%-I:%M %p", "%I:%M %p")


def ts_epoch(value: str | None) -> float:
    """POSIX seconds for an ISO stamp, 0.0 when unparseable.

    Never compare these timestamps as strings: events carry a mix of
    '-04:00' and '+00:00' offsets, so lexical order is not chronological
    order — '…T17:22:13-04:00' sorts *before* '…T20:19:28+00:00' while
    actually being later.
    """
    dt = _parse(value)
    if dt is None:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.timestamp()


def ts_gap_seconds(a: str | None, b: str | None) -> float:
    """Seconds between two ISO stamps; +inf when either is unparseable."""
    da, db = _parse(a), _parse(b)
    if da is None or db is None:
        return float("inf")
    if (da.tzinfo is None) != (db.tzinfo is None):
        return float("inf")
    return abs((db - da).total_seconds())


def event_ts(ev: dict) -> str:
    """Best timestamp string for an event dict (message timestamp or seen_at)."""
    return ev.get("timestamp") or ev.get("seen_at") or ""
