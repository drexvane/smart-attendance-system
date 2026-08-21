"""Leave request handling."""

from __future__ import annotations

import logging
from datetime import date as date_type

from flask import current_app
from sqlalchemy import func

from app.extensions import db
from app.models import LeaveRequest, Student
from app.services.attendance_service import apply_leave_to_attendance
from app.utils.time import today, utcnow

logger = logging.getLogger(__name__)

REVIEW_STATUSES = ("Approved", "Rejected")


class LeaveError(Exception):
    """Raised for caller-fixable problems (overlap, unknown student, bad status)."""


def apply_for_leave(data: dict) -> LeaveRequest:
    """Create a leave request, rejecting overlaps with a non-rejected one."""
    student = db.session.get(Student, int(data["student_id"]))
    if student is None or not student.is_active:
        raise LeaveError("Select an active student")

    start: date_type = data["start_date_parsed"]
    end: date_type = data["end_date_parsed"]

    overlapping = db.session.execute(
        db.select(LeaveRequest).where(
            LeaveRequest.student_id == student.id,
            LeaveRequest.status != "Rejected",
            LeaveRequest.start_date <= end,
            LeaveRequest.end_date >= start,
        )
    ).first()
    if overlapping is not None:
        raise LeaveError("A leave request already covers part of this period")

    request = LeaveRequest(
        student_id=student.id,
        leave_type=data["leave_type"],
        start_date=start,
        end_date=end,
        reason=data["reason"],
    )
    db.session.add(request)
    db.session.commit()
    logger.info("Leave request %s created for %s", request.id, student.student_id)
    return request


def review_leave(leave_id: int, status: str, reviewer: str, notes: str = "") -> LeaveRequest:
    """Approve or reject, writing "On Leave" attendance across an approved window."""
    if status not in REVIEW_STATUSES:
        raise LeaveError(f"Status must be one of: {', '.join(REVIEW_STATUSES)}")

    request = db.session.get(LeaveRequest, leave_id)
    if request is None:
        raise LeaveError("Leave request not found")
    if request.status != "Pending":
        raise LeaveError(f"This request was already {request.status.lower()}")

    request.status = status
    request.reviewed_by = reviewer
    request.reviewed_at = utcnow()
    request.review_notes = notes or None
    db.session.commit()

    if status == "Approved":
        # The old code inlined this loop with a read-then-write per day; it now goes
        # through the single idempotent marking path.
        written = apply_leave_to_attendance(request, marked_by=f"Leave/{reviewer}")
        logger.info(
            "Leave %s approved by %s; %d attendance days written", leave_id, reviewer, written
        )
    else:
        logger.info("Leave %s rejected by %s", leave_id, reviewer)
    return request


def list_requests(
    status: str | None = None,
    leave_type: str | None = None,
    date_from: date_type | None = None,
    date_to: date_type | None = None,
    limit: int = 500,
) -> list[LeaveRequest]:
    query = db.select(LeaveRequest).options(db.joinedload(LeaveRequest.student))
    if status:
        query = query.where(LeaveRequest.status == status)
    if leave_type:
        query = query.where(LeaveRequest.leave_type == leave_type)
    if date_from:
        query = query.where(LeaveRequest.start_date >= date_from)
    if date_to:
        query = query.where(LeaveRequest.end_date <= date_to)
    query = query.order_by(LeaveRequest.created_at.desc()).limit(limit)
    return list(db.session.execute(query).unique().scalars().all())


def status_counts() -> dict[str, int]:
    """All status counts in one query.

    The page previously issued four separate ``count()`` queries plus one for the
    on-leave-today figure.
    """
    rows = db.session.execute(
        db.select(LeaveRequest.status, func.count()).group_by(LeaveRequest.status)
    ).all()
    counts = {"Pending": 0, "Approved": 0, "Rejected": 0}
    for status, total in rows:
        counts[status] = total
    return counts


def on_leave_today(reference: date_type | None = None) -> list[LeaveRequest]:
    reference = reference or today()
    return list(
        db.session.execute(
            db.select(LeaveRequest)
            .options(db.joinedload(LeaveRequest.student))
            .where(
                LeaveRequest.status == "Approved",
                LeaveRequest.start_date <= reference,
                LeaveRequest.end_date >= reference,
            )
        )
        .unique()
        .scalars()
        .all()
    )


def allowed_types() -> tuple[str, ...]:
    return current_app.config["LEAVE_TYPES"]
