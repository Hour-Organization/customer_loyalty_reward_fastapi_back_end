"""Value formats that match what the Laravel API sent, so existing clients read them unchanged."""

from contextvars import ContextVar
from datetime import date, datetime
from decimal import Decimal

from app.config import get_settings

# The public root of the current request, for URLs of uploaded files.
base_url: ContextVar[str | None] = ContextVar("base_url", default=None)


def iso(value: datetime | None) -> str | None:
    """Carbon's toIso8601String: 2026-11-04T09:30:00+00:00."""
    return value.strftime("%Y-%m-%dT%H:%M:%S+00:00") if value else None


def stamp(value: datetime | None) -> str | None:
    """How Eloquent serializes timestamps in toArray(): 2026-11-04T09:30:00.000000Z."""
    return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ") if value else None


def day(value: date | datetime | None) -> str | None:
    if value is None:
        return None
    return (value.date() if isinstance(value, datetime) else value).isoformat()


def dec(value: Decimal | float | int | None, places: int = 2) -> str | None:
    """Eloquent's decimal:N cast, which serializes as a string such as "1.50"."""
    if value is None:
        return None
    return f"{Decimal(value):.{places}f}"


def num(value: Decimal | float | int | None) -> float:
    """A (float) cast."""
    return float(value or 0)


def storage_url(path: str) -> str:
    root = base_url.get() or get_settings().app_url
    return f"{root.rstrip('/')}/storage/{path}"


def is_external(path: str | None) -> bool:
    return bool(path) and path.startswith(("http://", "https://"))


def file_url(path: str | None) -> str | None:
    """An uploaded file's public URL; external URLs are returned as they are."""
    if path is None:
        return None
    return path if is_external(path) else storage_url(path)
