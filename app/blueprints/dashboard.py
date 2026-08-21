"""Dashboard and analytics pages."""

from __future__ import annotations

from datetime import timedelta

from flask import Blueprint, current_app, render_template, request
from flask_login import login_required

from app.extensions import db
from app.models import AttendanceRecord, Student
from app.services import analytics_service
from app.services.analytics_service import DateRange
from app.utils.time import today

dashboard_bp = Blueprint("dashboard", __name__)

MAX_ANALYTICS_DAYS = 365


def _days_param(default: int = 30) -> int:
    """Bound the ``days`` query parameter.

    The old routes did ``int(request.args.get('days', 30))`` with no bound and no error
    handling: ``?days=abc`` raised a ValueError that surfaced as a 500, and
    ``?days=100000`` issued that many queries.
    """
    raw = request.args.get("days", default, type=int) or default
    return max(1, min(raw, MAX_ANALYTICS_DAYS))


@dashboard_bp.route("/")
@login_required
def index():
    reference = today()
    roster = analytics_service.active_student_count()

    today_counts = analytics_service.status_counts_by_date(
        DateRange(start=reference, end=reference)
    ).get(reference, {})
    present_today = today_counts.get("Present", 0) + today_counts.get("Late", 0)

    recent_records = (
        db.session.execute(
            db.select(AttendanceRecord)
            .options(db.joinedload(AttendanceRecord.student))
            .order_by(AttendanceRecord.created_at.desc())
            .limit(10)
        )
        .unique()
        .scalars()
        .all()
    )

    enrolled = db.session.execute(
        db.select(db.func.count())
        .select_from(Student)
        .where(Student.is_active.is_(True), Student.face_samples_count > 0)
    ).scalar_one()

    return render_template(
        "index_clean.html",
        total_students=roster,
        today_attendance=sum(today_counts.values()),
        today_present=present_today,
        enrolled_students=enrolled,
        recent_records=recent_records,
    )


@dashboard_bp.route("/analytics")
@login_required
def analytics():
    days = _days_param()
    reference = today()
    window = DateRange(start=reference - timedelta(days=days - 1), end=reference)
    summary = analytics_service.overview(reference, window)
    return render_template(
        "analytics.html",
        days=days,
        detector_backend=current_app.config.get("FACE_DETECTOR_WEIGHTS"),
        **summary,
    )
