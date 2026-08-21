"""Attendance marking, including the duplicate-write race.

Marking used to be four separate copies of "SELECT then INSERT" with no constraint
behind them, so two overlapping requests each saw no row and each inserted one.
"""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from app.extensions import db
from app.models import AttendanceRecord, Student
from app.services import attendance_service
from app.services.attendance_service import AttendanceError
from tests.conftest import app_today


def record_count() -> int:
    return db.session.execute(db.select(db.func.count()).select_from(AttendanceRecord)).scalar_one()


class TestIdempotentMarking:
    def test_marking_twice_creates_one_row(self, app, students):  # noqa: ARG002
        first = attendance_service.mark_attendance(students[0], marked_by="test")
        second = attendance_service.mark_attendance(students[0], marked_by="test")

        assert first.created is True
        assert second.created is False
        assert record_count() == 1

    def test_the_second_call_returns_the_existing_row(self, app, students):  # noqa: ARG002
        first = attendance_service.mark_attendance(students[0], marked_by="test")
        second = attendance_service.mark_attendance(students[0], marked_by="test")

        assert second.record.id == first.record.id

    def test_overwrite_updates_instead_of_inserting(self, app, students):  # noqa: ARG002
        attendance_service.mark_attendance(students[0], status="Absent", marked_by="test")
        result = attendance_service.mark_attendance(
            students[0], status="Present", marked_by="test", overwrite=True
        )

        assert result.updated is True
        assert result.record.status == "Present"
        assert record_count() == 1

    def test_different_students_get_their_own_rows(self, app, students):  # noqa: ARG002
        for student in students:
            attendance_service.mark_attendance(student, marked_by="test")
        assert record_count() == len(students)

    def test_different_days_get_their_own_rows(self, app, students):  # noqa: ARG002
        today = app_today()
        attendance_service.mark_attendance(students[0], on_date=today, marked_by="test")
        attendance_service.mark_attendance(
            students[0], on_date=today - timedelta(days=1), marked_by="test"
        )
        assert record_count() == 2

    def test_the_database_rejects_a_duplicate_outright(self, app, students):  # noqa: ARG002
        """Proves the constraint exists rather than only the service-level check."""
        from sqlalchemy.exc import IntegrityError

        today = app_today()
        db.session.add(AttendanceRecord(student_id=students[0].id, date=today, status="Present"))
        db.session.commit()

        db.session.add(AttendanceRecord(student_id=students[0].id, date=today, status="Absent"))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()


class TestConcurrentMarking:
    def test_parallel_requests_produce_exactly_one_record(self, file_db_app):
        """The race the old read-then-write lost.

        Eight threads mark the same student on the same day through the real service
        against a real file-backed database; exactly one row must exist afterwards and
        exactly one caller may believe it inserted.
        """
        app = file_db_app
        with app.app_context():
            student = Student(
                student_id="R001", name="Racer", email="racer@school.edu", department="CSE"
            )
            db.session.add(student)
            db.session.commit()
            student_pk = student.id

        errors: list[Exception] = []
        created_flags: list[bool] = []
        barrier = threading.Barrier(8)

        def worker():
            try:
                barrier.wait(timeout=15)
                with app.app_context():
                    try:
                        row = db.session.get(Student, student_pk)
                        result = attendance_service.mark_attendance(row, marked_by="race")
                        created_flags.append(result.created)
                    finally:
                        # Inside the context: the scoped session is keyed on it.
                        db.session.remove()
            except Exception as exc:  # noqa: BLE001 - collected and asserted below
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=45)

        assert errors == [], f"marking raised under concurrency: {errors}"
        with app.app_context():
            total = db.session.execute(
                db.select(db.func.count()).select_from(AttendanceRecord)
            ).scalar_one()
        assert total == 1, f"concurrent marking created {total} rows"
        assert created_flags.count(True) == 1, "more than one caller believed it inserted"


class TestStatusRules:
    def test_late_is_derived_from_the_clock(self, app, students):
        app.config["LATE_THRESHOLD_TIME"] = "00:00"  # everything is late
        result = attendance_service.mark_attendance(students[0], marked_by="test")
        assert result.record.status == "Late"

    def test_present_before_the_cutoff(self, app, students):
        app.config["LATE_THRESHOLD_TIME"] = "23:59"
        result = attendance_service.mark_attendance(students[0], marked_by="test")
        assert result.record.status == "Present"

    def test_an_explicit_status_wins(self, app, students):
        app.config["LATE_THRESHOLD_TIME"] = "00:00"
        result = attendance_service.mark_attendance(students[0], status="Excused", marked_by="test")
        assert result.record.status == "Excused"

    def test_an_unknown_status_is_refused(self, app, students):  # noqa: ARG002
        with pytest.raises(AttendanceError, match="Invalid status"):
            attendance_service.mark_attendance(students[0], status="Vacationing")

    def test_marked_by_is_recorded(self, app, students):  # noqa: ARG002
        """The column the old model was missing entirely."""
        result = attendance_service.mark_attendance(students[0], marked_by="Manual/alice")
        assert result.record.marked_by == "Manual/alice"


class TestRoutes:
    def test_manual_marking_by_roll_number(self, admin_client, students):  # noqa: ARG002
        response = admin_client.post(
            "/mark_manual_attendance", data={"student_id": "S000"}, follow_redirects=True
        )
        assert response.status_code == 200
        assert record_count() == 1

    def test_manual_marking_rejects_an_unknown_roll(self, admin_client, students):  # noqa: ARG002
        response = admin_client.post(
            "/mark_manual_attendance", data={"student_id": "NOPE"}, follow_redirects=True
        )
        assert response.status_code == 200
        assert record_count() == 0

    def test_quick_status_toggle_works(self, admin_client, students):
        """This route crashed with a TypeError: it set marked_by, which had no column."""
        response = admin_client.post(f"/mark_student_status/{students[0].id}/Present")

        assert response.status_code == 200
        assert response.get_json()["success"] is True
        assert record_count() == 1

    def test_quick_status_toggle_updates_an_existing_row(self, admin_client, students):
        admin_client.post(f"/mark_student_status/{students[0].id}/Present")
        response = admin_client.post(f"/mark_student_status/{students[0].id}/Absent")

        assert response.status_code == 200
        assert record_count() == 1
        assert db.session.get(AttendanceRecord, 1).status == "Absent"

    def test_quick_status_rejects_an_invalid_status(self, admin_client, students):
        response = admin_client.post(f"/mark_student_status/{students[0].id}/Elsewhere")
        assert response.status_code == 400

    def test_time_out_can_only_be_set_once(self, admin_client, students):
        admin_client.post(f"/mark_student_status/{students[0].id}/Present")

        assert admin_client.post("/mark_time_out/1").status_code == 200
        assert admin_client.post("/mark_time_out/1").status_code == 400

    def test_updating_status_by_record_id(self, admin_client, students):
        admin_client.post(f"/mark_student_status/{students[0].id}/Present")
        response = admin_client.post(
            "/update_attendance_status", json={"record_id": 1, "status": "Late"}
        )
        assert response.status_code == 200
        assert response.get_json()["status"] == "Late"

    def test_deleting_a_record(self, admin_client, students):
        admin_client.post(f"/mark_student_status/{students[0].id}/Present")
        assert admin_client.post("/delete_attendance/1").status_code == 200
        assert record_count() == 0

    def test_deleting_a_missing_record_is_a_404(self, admin_client):
        assert admin_client.post("/delete_attendance/999").status_code == 404

    def test_attendance_page_filters_combine(self, admin_client, attendance_history):  # noqa: ARG002
        """Three filters at once used to trigger a cartesian product from repeat joins."""
        response = admin_client.get(
            "/attendance?department=CSE&year=3&search=Student&status=Present"
        )
        assert response.status_code == 200

    def test_per_page_is_bounded(self, admin_client, attendance_history):  # noqa: ARG002
        assert admin_client.get("/attendance?per_page=0").status_code == 200
        assert admin_client.get("/attendance?per_page=-5").status_code == 200
        assert admin_client.get("/attendance?per_page=999999").status_code == 200
