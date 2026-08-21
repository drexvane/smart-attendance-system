"""Leave management routes."""

from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.extensions import db, limiter
from app.models import Student
from app.services import leave_service
from app.services.leave_service import LeaveError
from app.utils.security import roles_required
from app.utils.time import parse_date, today
from app.utils.validators import validate_leave_request_data

leave_bp = Blueprint("leave", __name__)


@leave_bp.route("/leave")
@login_required
def index():
    status = request.args.get("status", "")
    leave_type = request.args.get("leave_type", "")
    date_from = parse_date(request.args.get("date_from"))
    date_to = parse_date(request.args.get("date_to"))

    requests_list = leave_service.list_requests(
        status=status or None,
        leave_type=leave_type or None,
        date_from=date_from,
        date_to=date_to,
    )
    counts = leave_service.status_counts()

    students = (
        db.session.execute(
            db.select(Student).where(Student.is_active.is_(True)).order_by(Student.name)
        )
        .scalars()
        .all()
    )

    return render_template(
        "leave_management_clean.html",
        leave_requests=requests_list,
        students=students,
        pending_count=counts["Pending"],
        approved_count=counts["Approved"],
        rejected_count=counts["Rejected"],
        on_leave_today=len(leave_service.on_leave_today()),
        leave_types=leave_service.allowed_types(),
        current_status=status,
        current_type=leave_type,
        date_from=request.args.get("date_from", ""),
        date_to=request.args.get("date_to", ""),
    )


@leave_bp.route("/apply_leave", methods=["POST"])
@login_required
@limiter.limit("20 per minute")
def apply():
    data = {
        field: request.form.get(field)
        for field in ("student_id", "leave_type", "start_date", "end_date", "reason")
    }
    errors, data = validate_leave_request_data(
        data, allowed_types=leave_service.allowed_types(), today_value=today()
    )
    if errors:
        for error in errors:
            flash(error, "error")
        return redirect(url_for("leave.index"))

    try:
        leave_service.apply_for_leave(data)
    except LeaveError as exc:
        flash(str(exc), "warning")
        return redirect(url_for("leave.index"))

    flash("Leave request submitted.", "success")
    return redirect(url_for("leave.index"))


@leave_bp.route("/review_leave", methods=["POST"])
@login_required
@roles_required("admin", "teacher")
@limiter.limit("60 per minute")
def review():
    leave_id = request.form.get("leave_id", type=int)
    status = request.form.get("status", "")
    notes = request.form.get("review_notes", "")

    if not leave_id:
        flash("Leave request ID is required", "error")
        return redirect(url_for("leave.index"))

    try:
        # The reviewer is taken from the session, not from a form field the client
        # controls -- the old form posted `reviewed_by` and the server trusted it.
        leave_service.review_leave(leave_id, status, reviewer=current_user.username, notes=notes)
    except LeaveError as exc:
        flash(str(exc), "error")
        return redirect(url_for("leave.index"))

    flash(f"Leave request {status.lower()}.", "success")
    return redirect(url_for("leave.index"))
