"""Student lifecycle: registration, photo enrolment, deletion.

Registration used to happen inline in a 100-line route that mixed form parsing, base64
decoding, file writing, face encoding and database work, with the "no face detected"
case flashing a warning and saving the student anyway -- leaving rows that could never
be recognised and no way to tell them apart from enrolled ones.  Enrolment state is now
explicit on the model (``face_samples_count``, ``face_model_label``).
"""

from __future__ import annotations

import logging

from flask import current_app
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import Student
from app.recognition import get_detector, get_recognizer
from app.recognition.enrolment import (
    EnrolmentError,
    add_sample_from_image,
    count_samples,
    remove_samples,
    retrain,
)
from app.utils.images import delete_image, store_image
from app.utils.time import utcnow

logger = logging.getLogger(__name__)


class StudentError(Exception):
    """Raised for caller-fixable problems (duplicate id, unusable photo)."""


def _image_config() -> tuple[frozenset[str], int]:
    return (
        current_app.config["ALLOWED_IMAGE_EXTENSIONS"],
        current_app.config["MAX_IMAGE_PIXELS"],
    )


def find_by_roll(roll: str) -> Student | None:
    return db.session.execute(
        db.select(Student).filter_by(student_id=roll.strip())
    ).scalar_one_or_none()


def register_student(data: dict, photo_bytes: bytes | None) -> tuple[Student, str | None]:
    """Create a student and enrol their face.

    Returns ``(student, enrolment_warning)``.  The student is created either way, but a
    warning is returned -- and ``face_samples_count`` stays 0 -- when the photo could not
    be used, so the UI can say the student will not be auto-recognised yet.
    """
    if find_by_roll(data["student_id"]) is not None:
        raise StudentError(f"Student ID {data['student_id']} already exists")

    if not photo_bytes:
        raise StudentError("A student photo is required (upload or capture)")

    allowed, max_pixels = _image_config()
    image_path = store_image(
        photo_bytes,
        current_app.config["STUDENT_IMAGES_FOLDER"],
        prefix=f"student_{data['student_id']}_",
        allowed_extensions=allowed,
        max_pixels=max_pixels,
    )

    student = Student(
        student_id=data["student_id"],
        name=data["name"],
        email=data["email"],
        phone=data.get("phone"),
        department=data.get("department"),
        year=data.get("year"),
        section=data.get("section"),
        image_path=image_path,
    )
    db.session.add(student)
    try:
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        delete_image(image_path)
        # Unique violation on student_id or email -- report which, not the raw SQL.
        field = "email" if "email" in str(exc.orig).lower() else "student ID"
        raise StudentError(f"That {field} is already registered") from exc

    warning = enrol_face(student, image_path)
    return student, warning


def enrol_face(student: Student, image_path: str) -> str | None:
    """Add a training sample from ``image_path`` and retrain.

    Returns ``None`` on success or a human-readable warning on failure.
    """
    try:
        add_sample_from_image(
            image_path,
            student.id,
            face_root=current_app.config["FACE_DATA_FOLDER"],
            detector=get_detector(),
            crop_size=current_app.config["FACE_CROP_SIZE"],
            crop_margin=current_app.config["FACE_CROP_MARGIN"],
        )
    except EnrolmentError as exc:
        logger.warning("Face enrolment failed for %s: %s", student.student_id, exc)
        return str(exc)

    student.face_samples_count = count_samples(current_app.config["FACE_DATA_FOLDER"], student.id)
    student.face_enrolled_at = utcnow()
    db.session.commit()
    rebuild_face_model()
    return None


def rebuild_face_model() -> int:
    """Retrain LBPH from the samples of every active student.

    Called after any change to enrolment.  A full retrain (rather than an incremental
    update) is what guarantees a deleted student's label leaves the model.
    """
    active_pks = list(
        db.session.execute(db.select(Student.id).filter_by(is_active=True)).scalars().all()
    )
    recognizer = get_recognizer()
    count = retrain(recognizer, current_app.config["FACE_DATA_FOLDER"], active_pks)

    # Mirror the labels the recognizer actually assigned onto the rows, rather than
    # recomputing them here and risking a drift between model and database.
    assigned = recognizer.student_labels
    for student in db.session.execute(db.select(Student)).scalars():
        student.face_model_label = assigned.get(student.id)
    db.session.commit()
    return count


def deactivate_student(student_id: int) -> str:
    """Soft delete: keep history, stop recognising."""
    student = db.session.get(Student, student_id)
    if student is None:
        raise StudentError("Student not found")
    name = student.name
    student.is_active = False
    db.session.commit()
    # Without this the model keeps the label and a deactivated student is still matched.
    rebuild_face_model()
    logger.info("Student deactivated: %s (%s)", name, student.student_id)
    return name


def reactivate_student(student_id: int) -> str:
    student = db.session.get(Student, student_id)
    if student is None:
        raise StudentError("Student not found")
    student.is_active = True
    db.session.commit()
    rebuild_face_model()
    return student.name


def delete_student_permanently(student_id: int) -> str:
    """Hard delete, including photos and face samples.

    Attendance and leave rows go with it via ``ON DELETE CASCADE``; the old code deleted
    attendance manually and left leave requests orphaned with a dangling foreign key.
    """
    student = db.session.get(Student, student_id)
    if student is None:
        raise StudentError("Student not found")

    name, roll, image_path = student.name, student.student_id, student.image_path
    remove_samples(current_app.config["FACE_DATA_FOLDER"], student.id)
    db.session.delete(student)
    db.session.commit()
    delete_image(image_path)
    rebuild_face_model()
    logger.info("Student permanently deleted: %s (%s)", name, roll)
    return name


def update_student(student_id: int, data: dict) -> Student:
    student = db.session.get(Student, student_id)
    if student is None:
        raise StudentError("Student not found")
    for field in ("name", "email", "phone", "department", "year", "section"):
        if field in data and data[field] is not None:
            setattr(student, field, data[field])
    try:
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        raise StudentError("That email is already registered to another student") from exc
    return student


def replace_photo(student_id: int, photo_bytes: bytes) -> str | None:
    """Swap a student's photo and add it as an additional training sample."""
    student = db.session.get(Student, student_id)
    if student is None:
        raise StudentError("Student not found")

    allowed, max_pixels = _image_config()
    new_path = store_image(
        photo_bytes,
        current_app.config["STUDENT_IMAGES_FOLDER"],
        prefix=f"student_{student.student_id}_",
        allowed_extensions=allowed,
        max_pixels=max_pixels,
    )
    old_path, student.image_path = student.image_path, new_path
    db.session.commit()
    warning = enrol_face(student, new_path)
    if old_path != new_path:
        delete_image(old_path)
    return warning


def known_students() -> list[Student]:
    """Active students with a usable face enrolment."""
    return list(
        db.session.execute(
            db.select(Student)
            .filter(Student.is_active.is_(True), Student.face_samples_count > 0)
            .order_by(Student.name)
        )
        .scalars()
        .all()
    )
