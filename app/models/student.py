"""Student records and face-enrolment bookkeeping."""

from __future__ import annotations

from app.extensions import db
from app.utils.time import utcnow


class Student(db.Model):
    __tablename__ = "students"

    id = db.Column(db.Integer, primary_key=True)
    # UNIQUE already creates a backing index on both SQLite and Postgres, so no
    # separate index=True here -- that would build a second, redundant one.
    student_id = db.Column(db.String(20), unique=True, nullable=False)
    name = db.Column(db.String(100), nullable=False, index=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    phone = db.Column(db.String(15))
    department = db.Column(db.String(50), index=True)
    year = db.Column(db.String(10), index=True)
    section = db.Column(db.String(5))
    image_path = db.Column(db.String(255))

    # --- face enrolment ----------------------------------------------------------
    # The LBPH recognizer works on integer labels, not on blobs stored per row, so
    # the model itself lives in face_data/lbph_model.yml and these columns only track
    # enrolment state.  (The old face_encoding column held a 256-bin grayscale
    # histogram, which carried no spatial information and matched near-anything; it
    # is dropped by migration 0003.)
    face_model_label = db.Column(db.Integer, unique=True)
    face_samples_count = db.Column(db.Integer, nullable=False, default=0)
    face_enrolled_at = db.Column(db.DateTime(timezone=True))

    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    # Lazy on purpose.  With lazy="selectin" every page that lists students would also
    # fetch each of their attendance records and leave requests -- 50 students on the
    # roster page meant pulling their entire history with them.  Callers that need the
    # collections ask for them; callers that need the reverse direction use joinedload
    # on AttendanceRecord.student.
    attendance_records = db.relationship(
        "AttendanceRecord",
        back_populates="student",
        lazy="select",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    leave_requests = db.relationship(
        "LeaveRequest",
        back_populates="student",
        lazy="select",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    @property
    def has_face_enrolment(self) -> bool:
        return self.face_model_label is not None and self.face_samples_count > 0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "student_id": self.student_id,
            "name": self.name,
            "email": self.email,
            "phone": self.phone,
            "department": self.department,
            "year": self.year,
            "section": self.section,
            "image_path": self.image_path,
            "is_active": self.is_active,
            "face_enrolled": self.has_face_enrolment,
            "face_samples_count": self.face_samples_count,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:
        return f"<Student {self.student_id} {self.name}>"
