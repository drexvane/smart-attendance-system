"""Student registration, enrolment and deletion."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from app.extensions import db
from app.models import AttendanceRecord, LeaveRequest, Student
from app.services import student_service
from app.services.student_service import StudentError
from tests.conftest import app_today, make_face


def photo_bytes_with_a_detectable_face() -> bytes:
    """A synthetic image the DNN detector will not find a face in.

    Used for the "photo has no face" path.  Registration must still succeed while
    reporting that the student is not enrolled -- the old code flashed a warning and
    saved a student who could never be recognised, with nothing on the record to say so.
    """
    buffer = io.BytesIO()
    Image.fromarray(make_face(seed=3)).convert("RGB").save(buffer, format="JPEG")
    return buffer.getvalue()


def valid_form(**overrides) -> dict:
    data = {
        "student_id": "S999",
        "name": "New Student",
        "email": "new.student@school.edu",
        "phone": "9876543210",
        "department": "CSE",
        "year": "2",
        "section": "B",
    }
    data.update(overrides)
    return data


class TestRegistration:
    def test_requires_a_photo(self, admin_client):
        response = admin_client.post("/register_student", data=valid_form())
        assert response.status_code == 400
        assert b"photo is required" in response.data

    def test_rejects_a_non_image_upload(self, admin_client):
        """save_uploaded_file used to write whatever bytes it was given."""
        response = admin_client.post(
            "/register_student",
            data={**valid_form(), "image": (io.BytesIO(b"#!/bin/sh\nrm -rf /"), "evil.jpg")},
            content_type="multipart/form-data",
        )
        assert response.status_code == 400
        assert db.session.execute(db.select(Student)).first() is None

    def test_creates_the_student_when_the_photo_has_no_face(self, admin_client):
        response = admin_client.post(
            "/register_student",
            data={
                **valid_form(),
                "image": (io.BytesIO(photo_bytes_with_a_detectable_face()), "x.jpg"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        assert response.status_code == 200

        student = db.session.execute(db.select(Student).filter_by(student_id="S999")).scalar_one()
        # The important part: enrolment state is recorded, not assumed.
        assert student.face_samples_count == 0
        assert student.has_face_enrolment is False

    def test_rejects_a_duplicate_student_id(self, admin_client, students):  # noqa: ARG002
        response = admin_client.post(
            "/register_student",
            data={
                **valid_form(student_id="S000", email="different@school.edu"),
                "image": (io.BytesIO(photo_bytes_with_a_detectable_face()), "x.jpg"),
            },
            content_type="multipart/form-data",
        )
        assert response.status_code == 400
        assert b"already exists" in response.data

    def test_rejects_an_invalid_email(self, admin_client):
        response = admin_client.post(
            "/register_student",
            data={
                **valid_form(email="@"),
                "image": (io.BytesIO(photo_bytes_with_a_detectable_face()), "x.jpg"),
            },
            content_type="multipart/form-data",
        )
        assert response.status_code == 400

    def test_accepts_a_base64_camera_capture(self, admin_client):
        import base64

        payload = base64.b64encode(photo_bytes_with_a_detectable_face()).decode()
        response = admin_client.post(
            "/register_student",
            data={**valid_form(), "captured_image": f"data:image/jpeg;base64,{payload}"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert db.session.execute(db.select(Student).filter_by(student_id="S999")).scalar_one()

    def test_rejects_a_malformed_data_url(self, admin_client):
        response = admin_client.post(
            "/register_student",
            data={**valid_form(), "captured_image": "data:image/jpeg;base64,!!!!"},
        )
        assert response.status_code == 400


class TestSoftDelete:
    def test_deactivates_without_losing_history(self, app, students, db):
        db.session.add(
            AttendanceRecord(
                student_id=students[0].id,
                date=app_today(),
                status="Present",
                marked_by="t",
            )
        )
        db.session.commit()

        student_service.deactivate_student(students[0].id)

        assert students[0].is_active is False
        assert db.session.execute(db.select(AttendanceRecord)).first() is not None

    def test_deactivated_student_leaves_the_roster(self, admin_client, students):
        admin_client.post(f"/delete_student/{students[0].id}")
        page = admin_client.get("/students")
        assert b"Student 0" not in page.data

    def test_deactivated_student_is_not_recognised(self, app, students):  # noqa: ARG002
        """The old code left the encoding in place, so a removed student kept matching."""
        student_service.deactivate_student(students[0].id)
        assert students[0].id not in {s.id for s in student_service.known_students()}

    def test_reactivation_restores_the_student(self, app, students):  # noqa: ARG002
        student_service.deactivate_student(students[0].id)
        student_service.reactivate_student(students[0].id)
        assert students[0].is_active is True

    def test_missing_student_raises(self, app):  # noqa: ARG002
        with pytest.raises(StudentError, match="not found"):
            student_service.deactivate_student(9999)


class TestPermanentDelete:
    def test_cascades_to_attendance_and_leave(self, app, students, db):
        today = app_today()
        db.session.add(
            AttendanceRecord(student_id=students[0].id, date=today, status="Present", marked_by="t")
        )
        db.session.add(
            LeaveRequest(
                student_id=students[0].id,
                leave_type="Sick",
                start_date=today,
                end_date=today,
                reason="Recovering from the flu.",
            )
        )
        db.session.commit()

        student_service.delete_student_permanently(students[0].id)

        # The old route deleted attendance by hand and never touched leave_requests,
        # leaving rows pointing at a student that no longer existed.
        assert db.session.execute(db.select(AttendanceRecord)).first() is None
        assert db.session.execute(db.select(LeaveRequest)).first() is None

    def test_removes_the_student_row(self, app, students):  # noqa: ARG002
        student_service.delete_student_permanently(students[0].id)
        assert db.session.get(Student, students[0].id) is None


class TestRoster:
    def test_search_matches_name_and_roll(self, admin_client, students):  # noqa: ARG002
        assert b"Student 1" in admin_client.get("/students?search=Student 1").data
        assert b"S002" in admin_client.get("/students?search=S002").data

    def test_department_filter(self, admin_client, students):  # noqa: ARG002
        page = admin_client.get("/students?department=ECE")
        assert b"Student 2" in page.data
        assert b"Student 0" not in page.data

    def test_pagination_bounds(self, admin_client, students):  # noqa: ARG002
        for query in ("per_page=0", "per_page=-1", "per_page=100000", "page=9999"):
            assert admin_client.get(f"/students?{query}").status_code == 200

    def test_student_api_returns_enrolment_state(self, admin_client, students):
        response = admin_client.get(f"/api/student/{students[0].id}")
        assert response.status_code == 200
        body = response.get_json()
        assert body["face_enrolled"] is False
        assert body["student_id"] == "S000"
