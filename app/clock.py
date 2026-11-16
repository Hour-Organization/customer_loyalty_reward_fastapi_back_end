"""The application's clock. Times are naive UTC, and tests can move the clock."""

from datetime import UTC, date, datetime, timedelta

_offset = timedelta()


def now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None) + _offset


def today() -> date:
    return now().date()


def travel(delta: timedelta) -> None:
    """Move the clock forwards (or backwards with a negative delta)."""
    global _offset
    _offset += delta


def reset() -> None:
    global _offset
    _offset = timedelta()
