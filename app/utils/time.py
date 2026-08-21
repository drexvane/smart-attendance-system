"""Time helpers.

The old code mixed two clocks: model defaults used ``datetime.utcnow()`` while routes
used ``date.today()`` (local).  Near midnight those disagree, so a mark could be
written with a UTC timestamp but filed under the local date, or looked up under a date
that had no records.  Everything now stores timezone-aware UTC and asks this module
what "today" means.

``datetime.utcnow()`` is also deprecated on Python 3.12, which the gating CI would
surface as a warning-turned-error.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import current_app


def app_timezone() -> ZoneInfo | timezone:
    """The configured display/business timezone, falling back to UTC."""
    name = "UTC"
    try:
        name = current_app.config.get("APP_TIMEZONE", "UTC")
    except RuntimeError:
        # No application context (CLI helpers, unit tests on pure functions).
        return UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        current_app.logger.warning("Unknown APP_TIMEZONE %r; using UTC", name)
        return UTC


def utcnow() -> datetime:
    """Timezone-aware current UTC time. Use this instead of ``datetime.utcnow()``."""
    return datetime.now(UTC)


def to_local(value: datetime | None) -> datetime | None:
    """Render a stored UTC timestamp in the configured timezone."""
    if value is None:
        return None
    if value.tzinfo is None:
        # Rows written by the pre-migration code are naive UTC.
        value = value.replace(tzinfo=UTC)
    return value.astimezone(app_timezone())


def today() -> date:
    """The current date *in the configured timezone*, not in UTC."""
    return utcnow().astimezone(app_timezone()).date()


def start_of_day_utc(day: date) -> datetime:
    """Midnight of ``day`` in the configured timezone, expressed in UTC."""
    return datetime.combine(day, time.min, tzinfo=app_timezone()).astimezone(UTC)


def parse_date(raw: str | None, default: date | None = None) -> date | None:
    """Parse an ISO ``YYYY-MM-DD`` string, returning ``default`` when unusable."""
    if not raw:
        return default
    try:
        return date.fromisoformat(raw.strip())
    except (ValueError, AttributeError):
        return default


def is_late(moment: datetime, threshold: str) -> bool:
    """Whether ``moment`` falls after the ``HH:MM`` cutoff, in local terms."""
    local = to_local(moment)
    if local is None:
        return False
    try:
        hour, minute = (int(part) for part in threshold.split(":", 1))
        cutoff = time(hour=hour, minute=minute)
    except (ValueError, TypeError):
        return False
    return local.time() > cutoff
