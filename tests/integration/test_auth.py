"""Authentication and authorisation.

Before this refactor there was no authentication of any kind: every route below was
reachable by an anonymous visitor, including permanent deletion and the full-roster
export.
"""

from __future__ import annotations

import pytest

from tests.conftest import ADMIN_PASSWORD

# Every route that changes data or discloses student records.
PROTECTED_GET = [
    "/",
    "/students",
    "/attendance",
    "/leave",
    "/reports",
    "/analytics",
    "/mark_attendance",
    "/export_attendance",
    "/get_detected_faces",
    "/api/student/1",
    "/api/today_attendance",
    "/api/attendance_summary",
    "/api/students_on_leave",
    "/api/analytics/trend",
    "/api/analytics/top_students",
    "/api/analytics/at_risk",
    "/api/face_recognition_status",
]

PROTECTED_POST = [
    "/register_student",
    "/mark_manual_attendance",
    "/mark_student_present",
    "/auto_mark_attendance",
    "/update_attendance_status",
    "/delete_student/1",
    "/permanently_delete_student/1",
    "/mark_student_status/1/Present",
    "/mark_time_out/1",
    "/delete_attendance/1",
    "/apply_leave",
    "/review_leave",
    "/start_detection",
    "/stop_detection",
    "/start_face_recognition",
    "/stop_face_recognition",
]


class TestAnonymousAccessIsRefused:
    @pytest.mark.parametrize("path", PROTECTED_GET)
    def test_get_requires_a_session(self, client, path):
        response = client.get(path)
        assert response.status_code in (302, 401), f"{path} was reachable anonymously"
        if response.status_code == 302:
            assert "/login" in response.headers["Location"]

    @pytest.mark.parametrize("path", PROTECTED_POST)
    def test_post_requires_a_session(self, client, path):
        response = client.post(path)
        assert response.status_code in (302, 401), f"{path} was writable anonymously"

    def test_healthz_stays_public(self, client):
        """The container probe must work without credentials."""
        assert client.get("/healthz").status_code == 200

    def test_login_page_is_public(self, client):
        assert client.get("/login").status_code == 200


class TestLogin:
    def test_correct_credentials_start_a_session(self, client, admin):  # noqa: ARG002
        response = client.post("/login", data={"username": "admin", "password": ADMIN_PASSWORD})
        assert response.status_code == 302
        assert client.get("/").status_code == 200

    def test_wrong_password_is_refused(self, client, admin):  # noqa: ARG002
        response = client.post("/login", data={"username": "admin", "password": "wrong"})
        assert response.status_code == 401

    def test_unknown_user_gets_the_same_message_as_a_wrong_password(self, client, admin):  # noqa: ARG002
        """The form must not be usable to enumerate accounts."""
        unknown = client.post("/login", data={"username": "nobody", "password": "x"})
        wrong = client.post("/login", data={"username": "admin", "password": "x"})

        assert unknown.status_code == wrong.status_code == 401
        assert b"Incorrect username or password" in unknown.data
        assert b"Incorrect username or password" in wrong.data

    def test_deactivated_account_cannot_sign_in(self, client, db, admin):
        admin.is_active_flag = False
        db.session.commit()

        response = client.post("/login", data={"username": "admin", "password": ADMIN_PASSWORD})
        assert response.status_code == 403

    def test_last_login_is_recorded(self, client, db, admin):
        assert admin.last_login_at is None
        client.post("/login", data={"username": "admin", "password": ADMIN_PASSWORD})
        db.session.refresh(admin)
        assert admin.last_login_at is not None

    def test_next_parameter_cannot_redirect_off_site(self, client, admin):  # noqa: ARG002
        """An open redirect here would make a phishing link look legitimate."""
        response = client.post(
            "/login?next=https://evil.example",
            data={"username": "admin", "password": ADMIN_PASSWORD},
        )
        assert response.status_code == 302
        assert "evil.example" not in response.headers["Location"]

    def test_relative_next_is_honoured(self, client, admin):  # noqa: ARG002
        response = client.post(
            "/login?next=/students",
            data={"username": "admin", "password": ADMIN_PASSWORD},
        )
        assert response.headers["Location"] == "/students"


class TestLogout:
    def test_logout_ends_the_session(self, admin_client):
        assert admin_client.post("/logout").status_code == 302
        assert admin_client.get("/").status_code == 302

    def test_logout_is_post_only(self, admin_client):
        """A GET logout is CSRF-able from any page that can embed a link."""
        assert admin_client.get("/logout").status_code == 405


class TestRoles:
    def test_teacher_cannot_permanently_delete(self, teacher_client, students):
        response = teacher_client.post(f"/permanently_delete_student/{students[0].id}")
        assert response.status_code == 403

    def test_admin_can_permanently_delete(self, admin_client, students):
        response = admin_client.post(f"/permanently_delete_student/{students[0].id}")
        assert response.status_code == 200
        assert response.get_json()["success"] is True

    def test_teacher_can_soft_delete(self, teacher_client, students):
        response = teacher_client.post(f"/delete_student/{students[0].id}")
        assert response.status_code == 200

    def test_teacher_cannot_rebuild_the_face_model(self, teacher_client):
        assert teacher_client.post("/rebuild_face_model").status_code == 403
