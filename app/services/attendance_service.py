"""Attendance business logic.

The old code marked attendance in four places (manual form, detected-face POST,
auto-mark loop, quick status toggle) and each one did the same read-then-write:

    existing = AttendanceRecord.query.filter_by(student_id=..., date=today).first()
    if existing: return "already marked"
    db.session.add(AttendanceRecord(...)); db.session.commit()

Two concurrent requests both see no row and both insert.  There was no unique
constraint to stop them, so the table accumulated duplicates that then skewed every
analytics percentage.  :func:`mark_attendance` is the single write path now, and it
relies on the database constraint instead of a prior read.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date as date_type
from typing import cast

from flask import current_app
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import AttendanceRecord, LeaveRequest, Student
from app.utils.time import is_late, today, utcnow

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MarkResult:
    """Outcome of a marking attempt."""

    record: AttendanceRecord
    created: bool
    updated: bool = False

    @property
    def message(self) -> str:
        name = self.record.student.name if self.record.student else "Student"
        if self.created:
            return f"{name} marked {self.record.status.lower()}"
        if self.updated:
            return f"{name} updated to {self.record.status.lower()}"
        return f"{name} was already marked {self.record.status.lower()} today"


class AttendanceError(Exception):
    """Raised for caller-fixable problems (unknown student, bad status)."""


def validate_status(status: str) -> str:
    allowed = current_app.config["ATTENDANCE_STATUSES"]
    if status not in allowed:
        raise AttendanceError(f"Invalid status {status!r}. Must be one of: {', '.join(allowed)}")
    return status


def resolve_status(explicit: str | None, moment=None) -> str:
    """Pick a status, deriving Present/Late from the clock when not given.

    The old marking paths hardcoded ``status = 'Present'`` with a comment saying late
    detection was intentional, while a LATE threshold sat unused in config; a student
    arriving at 11:00 was recorded identically to one arriving at 08:00.
    """
    if explicit:
        return validate_status(explicit)
    moment = moment or utcnow()
    threshold = current_app.config["LATE_THRESHOLD_TIME"]
    return "Late" if is_late(moment, threshold) else "Present"


def mark_attendance(
    student: Student,
    *,
    status: str | None = None,
    on_date: date_type | None = None,
    confidence: float | None = None,
    marked_by: str = "System",
    overwrite: bool = False,
) -> MarkResult:
    """Record attendance for ``student``, at most once per day.

    Returns a :class:`MarkResult` describing whether a row was created, updated or
    already present.  Never raises on a duplicate -- that is the normal, expected case
    when a face stays in front of the camera.
    """
    target_date = on_date or today()
    moment = utcnow()
    final_status = resolve_status(status, moment)

    record = AttendanceRecord(
        student_id=student.id,
        date=target_date,
        time_in=moment,
        status=final_status,
        confidence_score=confidence,
        marked_by=marked_by,
    )
    try:
        # A SAVEPOINT so that a constraint violation rolls back only this insert and
        # leaves the surrounding transaction usable.  The record is added *inside* the
        # savepoint so the rollback discards it cleanly.
        with db.session.begin_nested():
            db.session.add(record)
            db.session.flush()
    except IntegrityError:
        existing = _find_record(student.id, target_date)
        if existing is None:
            # The violation was something other than the daily uniqueness constraint.
            raise
        if overwrite and existing.status != final_status:
            previous = existing.status
            existing.status = final_status
            existing.marked_by = marked_by
            if confidence is not None:
                existing.confidence_score = confidence
            db.session.commit()
            logger.info(
                "Attendance updated: %s (%s) %s -> %s on %s",
                existing.student.name if existing.student else student.id,
                student.student_id,
                previous,
                final_status,
                target_date,
            )
            return MarkResult(record=existing, created=False, updated=True)
        logger.debug(
            "Duplicate attendance suppressed for %s on %s", student.student_id, target_date
        )
        return MarkResult(record=existing, created=False)

    db.session.commit()
    logger.info(
        "Attendance marked: %s (%s) %s on %s by %s",
        student.name,
        student.student_id,
        final_status,
        target_date,
        marked_by,
    )
    return MarkResult(record=record, created=True)


def _find_record(student_pk: int, on_date: date_type) -> AttendanceRecord | None:
    return db.session.execute(
        db.select(AttendanceRecord).filter_by(student_id=student_pk, date=on_date)
    ).scalar_one_or_none()


def mark_by_roll_number(roll: str, **kwargs) -> MarkResult:
    """Mark attendance given the human-facing student id (roll number)."""
    student = db.session.execute(
        db.select(Student).filter_by(student_id=roll.strip(), is_active=True)
    ).scalar_one_or_none()
    if student is None:
        raise AttendanceError(f"No active student found with ID {roll!r}")
    return mark_attendance(student, **kwargs)


def update_status(record_id: int, status: str, marked_by: str) -> AttendanceRecord:
    record = db.session.get(AttendanceRecord, record_id)
    if record is None:
        raise AttendanceError("Attendance record not found")
    record.status = validate_status(status)
    record.marked_by = marked_by
    db.session.commit()
    return record


def mark_time_out(record_id: int) -> AttendanceRecord:
    record = db.session.get(AttendanceRecord, record_id)
    if record is None:
        raise AttendanceError("Attendance record not found")
    if record.time_out is not None:
        raise AttendanceError("Time out is already recorded for this entry")
    record.time_out = utcnow()
    db.session.commit()
    return record


def delete_record(record_id: int) -> str:
    record = db.session.get(AttendanceRecord, record_id)
    if record is None:
        raise AttendanceError("Attendance record not found")
    name = record.student.name if record.student else "Unknown"
    db.session.delete(record)
    db.session.commit()
    return name


def apply_leave_to_attendance(leave: LeaveRequest, marked_by: str) -> int:
    """Write "On Leave" rows across an approved leave window.

    Uses the same idempotent path as everything else, with ``overwrite=True`` so an
    already-marked day is corrected rather than skipped.  The original looped with a
    read-then-write per day, one query per date.
    """
    student = cast("Student", leave.student)
    if student is None:
        raise AttendanceError("Leave request has no associated student")

    written = 0
    current = leave.start_date
    while current <= leave.end_date:
        result = mark_attendance(
            student,
            status="On Leave",
            on_date=current,
            confidence=None,
            marked_by=marked_by,
            overwrite=True,
        )
        if result.created or result.updated:
            written += 1
        current = current.fromordinal(current.toordinal() + 1)
    return written
