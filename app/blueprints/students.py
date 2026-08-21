"""Student management routes."""

from __future__ import annotations

from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from app.extensions import db, limiter
from app.models import Student
from app.services import student_service
from app.services.student_service import StudentError
from app.utils.images import ImageValidationError, decode_data_url
from app.utils.security import admin_required
from app.utils.validators import clamp_page_size, validate_student_data

students_bp = Blueprint("students", __name__)


def _photo_from_request() -> bytes | None:
    """Read a photo from either the file field or the base64 camera capture."""
    captured = request.form.get("captured_image")
    if captured:
        return decode_data_url(captured)
    upload = request.files.get("image")
    if upload and upload.filename:
        return upload.read()
    return None


@students_bp.route("/students")
@login_required
def list_students():
    page = request.args.get("page", 1, type=int)
    per_page = clamp_page_size(
        request.args.get("per_page", type=int),
        current_app.config["STUDENTS_PER_PAGE"],
        current_app.config["MAX_PER_PAGE"],
    )
    search = (request.args.get("search") or "").strip()
    department = request.args.get("department", "")
    year = request.args.get("year", "")

    query = db.select(Student).where(Student.is_active.is_(True))
    if search:
        pattern = f"%{search}%"
        query = query.where(
            db.or_(
                Student.name.ilike(pattern),
                Student.student_id.ilike(pattern),
                Student.email.ilike(pattern),
            )
        )
    if department:
        query = query.where(Student.department == department)
    if year:
        query = query.where(Student.year == year)

    pagination = db.paginate(
        query.order_by(Student.name.asc()),
        page=page,
        per_page=per_page,
        error_out=False,
    )

    # One query for both filter dropdowns instead of two.
    facets = db.session.execute(
        db.select(Student.department, Student.year).where(Student.is_active.is_(True)).distinct()
    ).all()
    departments = sorted({row[0] for row in facets if row[0]})
    years = sorted({row[1] for row in facets if row[1]})

    return render_template(
        "students_clean.html",
        students=pagination.items,
        pagination=pagination,
        departments=departments,
        years=years,
        current_search=search,
        current_department=department,
        current_year=year,
        per_page=per_page,
    )


@students_bp.route("/register_student", methods=["GET", "POST"])
@login_required
@limiter.limit("20 per minute", methods=["POST"])
def register():
    if request.method == "GET":
        return render_template("register_student_clean.html")

    data = {
        field: request.form.get(field)
        for field in ("student_id", "name", "email", "phone", "department", "year", "section")
    }
    errors, data = validate_student_data(data)

    try:
        photo = _photo_from_request()
    except ImageValidationError as exc:
        errors.append(str(exc))
        photo = None

    if errors:
        for error in errors:
            flash(error, "error")
        return render_template("register_student_clean.html", data=data), 400

    try:
        student, warning = student_service.register_student(data, photo)
    except (StudentError, ImageValidationError) as exc:
        flash(str(exc), "error")
        return render_template("register_student_clean.html", data=data), 400

    if warning:
        flash(
            f"{student.name} was registered, but face enrolment failed: {warning} "
            "They will not be recognised automatically until a usable photo is added.",
            "warning",
        )
    else:
        flash(f"{student.name} registered and enrolled for face recognition.", "success")
    return redirect(url_for("students.list_students"))


@students_bp.route("/edit_student/<int:student_id>", methods=["GET", "POST"])
@login_required
@limiter.limit("30 per minute", methods=["POST"])
def edit(student_id: int):
    student = db.session.get(Student, student_id)
    if student is None:
        flash("Student not found", "error")
        return redirect(url_for("students.list_students"))

    if request.method == "GET":
        return render_template("edit_student.html", student=student)

    data = {
        field: request.form.get(field)
        for field in ("student_id", "name", "email", "phone", "department", "year", "section")
    }
    errors, data = validate_student_data(data)
    if errors:
        for error in errors:
            flash(error, "error")
        return render_template("edit_student.html", student=student), 400

    try:
        student_service.update_student(student_id, data)
        photo = _photo_from_request()
        if photo:
            warning = student_service.replace_photo(student_id, photo)
            if warning:
                flash(f"Photo updated but face enrolment failed: {warning}", "warning")
    except (StudentError, ImageValidationError) as exc:
        flash(str(exc), "error")
        return render_template("edit_student.html", student=student), 400

    flash(f"{student.name} updated.", "success")
    return redirect(url_for("students.list_students"))


@students_bp.route("/delete_student/<int:student_id>", methods=["POST"])
@login_required
@limiter.limit("30 per minute")
def delete(student_id: int):
    """Soft delete. Attendance history is preserved."""
    try:
        name = student_service.deactivate_student(student_id)
    except StudentError as exc:
        return jsonify({"success": False, "message": str(exc)}), 404
    return jsonify({"success": True, "message": f"{name} was deactivated"})


@students_bp.route("/permanently_delete_student/<int:student_id>", methods=["POST"])
@login_required
@admin_required
@limiter.limit("10 per minute")
def delete_permanent(student_id: int):
    """Hard delete, including attendance, leave, photos and face samples."""
    try:
        name = student_service.delete_student_permanently(student_id)
    except StudentError as exc:
        return jsonify({"success": False, "message": str(exc)}), 404
    current_app.logger.warning(
        "Permanent deletion of %s performed by %s", name, current_user.username
    )
    return jsonify({"success": True, "message": f"{name} was permanently deleted"})


@students_bp.route("/rebuild_face_model", methods=["POST"])
@login_required
@admin_required
@limiter.limit("5 per hour")
def rebuild_model():
    """Retrain the recognizer from stored samples."""
    count = student_service.rebuild_face_model()
    return jsonify({"success": True, "enrolled_students": count})
