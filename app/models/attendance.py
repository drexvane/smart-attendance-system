"""Attendance records and sessions."""

from __future__ import annotations

from app.extensions import db
from app.utils.time import utcnow


class AttendanceRecord(db.Model):
    """One row per student per day.

    The unique constraint is the important part.  Every marking path used to do
    "SELECT then INSERT", which is a race: two concurrent requests (or the recognition
    loop firing twice) both see no row and both insert one.  With the constraint in
    place the second insert fails and the service layer converts that into an
    idempotent "already marked" result.
    """

    __tablename__ = "attendance_records"
    __table_args__ = (
        db.UniqueConstraint("student_id", "date", name="uq_attendance_student_date"),
        db.Index("ix_attendance_date_status", "date", "status"),
    )

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(
        db.Integer,
        db.ForeignKey("students.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    date = db.Column(db.Date, nullable=False, index=True)
    time_in = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    time_out = db.Column(db.DateTime(timezone=True))
    status = db.Column(db.String(20), nullable=False, default="Present", index=True)
    confidence_score = db.Column(db.Float)
    # Who or what wrote the row: "Face Recognition", "Manual", "Leave", a username.
    # helpers.py already read this via getattr(record, 'marked_by', 'System') but the
    # column never existed, so exports always said "System" and one route crashed
    # trying to set it.
    marked_by = db.Column(db.String(64), nullable=False, default="System")
    notes = db.Column(db.String(255))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    student = db.relationship("Student", back_populates="attendance_records")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "student_id": self.student_id,
            "student_name": self.student.name if self.student else None,
            "student_roll": self.student.student_id if self.student else None,
            "date": self.date.isoformat() if self.date else None,
            "time_in": self.time_in.isoformat() if self.time_in else None,
            "time_out": self.time_out.isoformat() if self.time_out else None,
            "status": self.status,
            "confidence_score": self.confidence_score,
            "marked_by": self.marked_by,
        }

    def __repr__(self) -> str:
        return f"<AttendanceRecord student={self.student_id} {self.date} {self.status}>"


class AttendanceSession(db.Model):
    """An optional class/period grouping. Retained from the original schema."""

    __tablename__ = "attendance_sessions"

    id = db.Column(db.Integer, primary_key=True)
    session_name = db.Column(db.String(100), nullable=False)
    subject = db.Column(db.String(100))
    teacher_name = db.Column(db.String(100))
    department = db.Column(db.String(50))
    year = db.Column(db.String(10))
    section = db.Column(db.String(5))
    start_time = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    end_time = db.Column(db.DateTime(timezone=True))
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "session_name": self.session_name,
            "subject": self.subject,
            "teacher_name": self.teacher_name,
            "department": self.department,
            "year": self.year,
            "section": self.section,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "is_active": self.is_active,
        }
