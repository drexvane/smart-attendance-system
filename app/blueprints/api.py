"""JSON API.

Same paths as before so the existing dashboard JavaScript keeps working, but each
endpoint now requires a session and the analytics queries are aggregates rather than
per-row loops.
"""

from __future__ import annotations

from datetime import timedelta

from flask import Blueprint, jsonify, request
from flask_login import login_required

from app.extensions import db
from app.models import AttendanceRecord, LeaveRequest, Student
from app.services import analytics_service
from app.services.analytics_service import DateRange
from app.utils.time import today

api_bp = Blueprint("api", __name__, url_prefix="/api")

MAX_DAYS = 365
MAX_LIMIT = 200


def _window(default_days: int = 30) -> DateRange:
    raw = request.args.get("days", default_days, type=int) or default_days
    days = max(1, min(raw, MAX_DAYS))
    reference = today()
    return DateRange(start=reference - timedelta(days=days - 1), end=reference)


def _limit(default: int = 10) -> int:
    raw = request.args.get("limit", default, type=int) or default
    return max(1, min(raw, MAX_LIMIT))


# --- students ------------------------------------------------------------------
@api_bp.route("/student/<int:student_id>")
@login_required
def student_detail(student_id: int):
    student = db.session.get(Student, student_id)
    if student is None:
        return jsonify({"success": False, "error": "Student not found"}), 404
    return jsonify(student.to_dict())


# --- attendance ----------------------------------------------------------------
@api_bp.route("/attendance_summary")
@login_required
def attendance_summary():
    reference = today()
    return jsonify(analytics_service.summarise(DateRange(start=reference, end=reference)))


@api_bp.route("/today_attendance")
@login_required
def today_attendance():
    reference = today()
    records = (
        db.session.execute(
            db.select(AttendanceRecord)
            .options(db.joinedload(AttendanceRecord.student))
            .where(AttendanceRecord.date == reference)
            .order_by(AttendanceRecord.created_at.desc())
            .limit(_limit(10))
        )
        .unique()
        .scalars()
        .all()
    )
    return jsonify(
        {
            "date": reference.isoformat(),
            "total_present": sum(1 for r in records if r.status in ("Present", "Late")),
            "records": [
                {
                    "student_name": record.student.name if record.student else "Unknown",
                    "student_id": record.student.student_id if record.student else "N/A",
                    "time": record.time_in.isoformat() if record.time_in else None,
                    "status": record.status,
                }
                for record in records
            ],
        }
    )


# --- leave ---------------------------------------------------------------------
@api_bp.route("/leave/<int:leave_id>")
@login_required
def leave_detail(leave_id: int):
    request_row = db.session.get(LeaveRequest, leave_id)
    if request_row is None:
        return jsonify({"success": False, "error": "Leave request not found"}), 404
    return jsonify(request_row.to_dict())


@api_bp.route("/students_on_leave")
@login_required
def students_on_leave():
    from app.services import leave_service

    rows = leave_service.on_leave_today()
    return jsonify({"count": len(rows), "students": [row.to_dict() for row in rows]})


# --- recognition ---------------------------------------------------------------
@api_bp.route("/face_recognition_status")
@login_required
def face_recognition_status():
    from app.recognition import get_pipeline

    pipeline = get_pipeline()
    status = pipeline.status()
    # Keys the old endpoint returned, kept so existing callers do not break.
    status.update(
        {
            "available": pipeline.detector.available,
            "active": status["recognizing"],
            "camera_active": status["running"],
        }
    )
    return jsonify(status)


@api_bp.route("/recognition/status")
@login_required
def recognition_status():
    from app.recognition import get_pipeline

    return jsonify(get_pipeline().status())


# --- analytics -----------------------------------------------------------------
@api_bp.route("/analytics/trend")
@login_required
def analytics_trend():
    return jsonify(analytics_service.trend(_window()))


@api_bp.route("/analytics/department")
@login_required
def analytics_department():
    return jsonify(analytics_service.department_stats(_window()))


@api_bp.route("/analytics/status_distribution")
@login_required
def analytics_status_distribution():
    return jsonify(analytics_service.status_distribution(_window()))


@api_bp.route("/analytics/top_students")
@login_required
def analytics_top_students():
    return jsonify(analytics_service.top_students(_window(), limit=_limit(10)))


@api_bp.route("/analytics/at_risk")
@login_required
def analytics_at_risk():
    threshold = request.args.get("threshold", 75.0, type=float) or 75.0
    threshold = max(0.0, min(threshold, 100.0))
    return jsonify(
        analytics_service.at_risk_students(_window(), threshold=threshold, limit=_limit(10))
    )


@api_bp.route("/analytics/weekly_heatmap")
@login_required
def analytics_weekly_heatmap():
    weeks = request.args.get("weeks", 4, type=int) or 4
    return jsonify(analytics_service.weekly_heatmap(max(1, min(weeks, 26)), today()))


@api_bp.route("/analytics/recent_activity")
@login_required
def analytics_recent_activity():
    return jsonify(analytics_service.recent_activity(limit=_limit(20)))
