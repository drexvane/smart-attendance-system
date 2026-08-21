"""Input validation and sanitisation.

Ported from ``src/utils/helpers.py`` with the logic intact; the changes are that
``bleach`` is now a hard dependency rather than an optional import that silently fell
back to ``html.escape``, and that email/date checks are stricter.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

import bleach
from email_validator import EmailNotValidError, validate_email

# Tags permitted when a field explicitly opts into basic formatting.
_BASIC_TAGS = ["b", "i", "u", "em", "strong", "br", "p"]

STUDENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{2,19}$")
PHONE_RE = re.compile(r"^[0-9+()\s-]{7,15}$")

MAX_REASON_LENGTH = 500
MIN_REASON_LENGTH = 10


def sanitize_input(text: Any, allow_basic_html: bool = False) -> Any:
    """Strip HTML from user input.

    Returns the value unchanged when it is falsy so that ``None`` stays ``None``
    rather than becoming the string ``"None"``.
    """
    if not text:
        return text
    cleaned = bleach.clean(
        str(text).strip(),
        tags=_BASIC_TAGS if allow_basic_html else [],
        attributes={},
        strip=True,
    )
    return cleaned.strip()


def normalize_email(raw: str) -> tuple[str | None, str | None]:
    """Return ``(normalised_email, error)``.

    The old check was ``'@' not in email``, which accepted ``"@"`` itself.
    """
    try:
        result = validate_email(raw, check_deliverability=False)
    except EmailNotValidError as exc:
        return None, f"Invalid email address: {exc}"
    return result.normalized, None


def validate_student_data(data: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    """Validate and sanitise a student registration payload, in place.

    Returns ``(errors, sanitised_data)``.  The original returned only errors while
    mutating its argument; returning both makes the contract explicit and matches
    :func:`validate_leave_request_data`.
    """
    errors: list[str] = []

    for field in ("student_id", "name", "email", "department", "year", "section"):
        if not data.get(field):
            errors.append(f"{field.replace('_', ' ').title()} is required")

    for field in ("student_id", "name", "email", "phone", "department", "year", "section"):
        if data.get(field):
            data[field] = sanitize_input(data[field])

    student_id = data.get("student_id") or ""
    if student_id and not STUDENT_ID_RE.match(student_id):
        errors.append(
            "Student ID must be 3-20 characters using letters, digits, dot, dash, "
            "slash or underscore"
        )

    name = data.get("name") or ""
    if name and not 2 <= len(name) <= 100:
        errors.append("Name must be between 2 and 100 characters")

    if data.get("email"):
        normalised, error = normalize_email(data["email"])
        if error:
            errors.append(error)
        else:
            data["email"] = normalised

    phone = data.get("phone") or ""
    if phone and not PHONE_RE.match(phone):
        errors.append("Phone number must be 7-15 characters of digits and separators")

    return errors, data


def validate_leave_request_data(
    data: dict[str, Any],
    allowed_types: tuple[str, ...],
    today_value: date,
) -> tuple[list[str], dict[str, Any]]:
    """Validate and sanitise a leave request payload.

    ``allowed_types`` and ``today_value`` are injected rather than hardcoded so this
    stays a pure function and can be unit-tested without an app context.  ``today_value``
    is required on purpose: a ``date.today()`` fallback here would reintroduce the
    local-vs-configured clock split that the rest of the code went to some trouble to
    remove, and it would do so silently.
    """
    errors: list[str] = []

    for field in ("student_id", "leave_type", "start_date", "end_date", "reason"):
        if not data.get(field):
            errors.append(f"{field.replace('_', ' ').title()} is required")

    if data.get("reason"):
        data["reason"] = sanitize_input(data["reason"])
        length = len(data["reason"])
        if length < MIN_REASON_LENGTH:
            errors.append(f"Reason must be at least {MIN_REASON_LENGTH} characters long")
        elif length > MAX_REASON_LENGTH:
            errors.append(f"Reason must be less than {MAX_REASON_LENGTH} characters")

    if data.get("leave_type"):
        data["leave_type"] = sanitize_input(data["leave_type"])
        if data["leave_type"] not in allowed_types:
            errors.append(f"Leave type must be one of: {', '.join(allowed_types)}")

    start = _parse_iso(data.get("start_date"))
    end = _parse_iso(data.get("end_date"))
    if data.get("start_date") and start is None:
        errors.append("Invalid start date format (expected YYYY-MM-DD)")
    if data.get("end_date") and end is None:
        errors.append("Invalid end date format (expected YYYY-MM-DD)")

    if start and end:
        if end < start:
            errors.append("End date cannot be before start date")
        if start < today_value - timedelta(days=7):
            errors.append("Start date cannot be more than 7 days in the past")
        if end > today_value + timedelta(days=365):
            errors.append("End date cannot be more than 1 year in the future")
        data["start_date_parsed"] = start
        data["end_date_parsed"] = end

    return errors, data


def _parse_iso(raw: Any) -> date | None:
    if not raw:
        return None
    if isinstance(raw, date):
        return raw
    try:
        return date.fromisoformat(str(raw).strip())
    except ValueError:
        return None


def clamp_page_size(requested: int | None, default: int, maximum: int) -> int:
    """Bound a user-supplied ``per_page`` into ``1..maximum``.

    The old code did ``min(requested, MAX_PER_PAGE)`` with no lower bound, so
    ``?per_page=0`` or a negative value reached SQLAlchemy's paginate().
    """
    if not requested or requested < 1:
        return default
    return min(requested, maximum)
