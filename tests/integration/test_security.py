"""CSRF, rate limiting, security headers and error handling."""

from __future__ import annotations

import pytest

from tests.conftest import ADMIN_PASSWORD


@pytest.fixture
def csrf_app(app):
    """The app with CSRF turned back on.

    TestingConfig disables it so most tests can post plain forms; these tests are
    specifically about the protection working.
    """
    app.config["WTF_CSRF_ENABLED"] = True
    return app


@pytest.fixture
def csrf_client(csrf_app, admin):  # noqa: ARG001
    client = csrf_app.test_client()
    page = client.get("/login")
    token = _extract_token(page.data)
    response = client.post(
        "/login",
        data={"username": "admin", "password": ADMIN_PASSWORD, "csrf_token": token},
    )
    assert response.status_code == 302
    return client


def _extract_token(html: bytes) -> str:
    import re

    match = re.search(rb'name="csrf_token" value="([^"]+)"', html)
    assert match, "no csrf_token field found in the rendered form"
    return match.group(1).decode()


class TestCsrfProtection:
    def test_post_without_a_token_is_rejected(self, csrf_client):
        response = csrf_client.post("/mark_manual_attendance", data={"student_id": "S000"})
        assert response.status_code == 400

    def test_json_post_without_a_token_is_rejected(self, csrf_client):
        response = csrf_client.post("/mark_student_present", json={"student_id": 1})
        assert response.status_code == 400
        assert response.get_json()["success"] is False

    @pytest.mark.parametrize(
        "path",
        [
            "/start_detection",
            "/stop_detection",
            "/start_face_recognition",
            "/stop_face_recognition",
            "/auto_mark_attendance",
        ],
    )
    def test_camera_and_marking_routes_are_no_longer_exempt(self, csrf_client, path):
        """These were all @csrf_exempt, so any site could drive this machine's camera."""
        assert csrf_client.post(path).status_code == 400

    def test_a_valid_token_is_accepted(self, csrf_client, students):  # noqa: ARG002
        page = csrf_client.get("/mark_attendance")
        token = _extract_token(page.data)

        response = csrf_client.post(
            "/mark_manual_attendance",
            data={"student_id": "S000", "csrf_token": token},
        )
        assert response.status_code == 302

    def test_token_in_the_header_is_accepted(self, csrf_app, csrf_client, students):  # noqa: ARG002
        """This is the path static/js/csrf.js uses for fetch calls."""
        with csrf_app.test_request_context():
            from flask_wtf.csrf import generate_csrf

            page = csrf_client.get("/mark_attendance")
            token = _extract_token(page.data)
            assert generate_csrf  # imported symbol exists (the old code called a method)

        response = csrf_client.post(
            "/mark_student_present",
            json={"student_id": students[0].id},
            headers={"X-CSRFToken": token},
        )
        assert response.status_code == 200

    def test_csrf_token_renders_in_templates(self, csrf_client):
        """csrf.generate_csrf() did not exist, so this raised AttributeError."""
        page = csrf_client.get("/register_student")
        assert page.status_code == 200
        assert b'name="csrf-token"' in page.data


class TestSecurityHeaders:
    @pytest.mark.parametrize(
        "header",
        [
            "X-Content-Type-Options",
            "X-Frame-Options",
            "Referrer-Policy",
            "Content-Security-Policy",
            "Cross-Origin-Opener-Policy",
        ],
    )
    def test_header_is_present(self, client, header):
        assert header in client.get("/login").headers

    def test_frames_are_denied(self, client):
        assert client.get("/login").headers["X-Frame-Options"] == "DENY"

    def test_csp_blocks_framing(self, client):
        assert "frame-ancestors 'none'" in client.get("/login").headers["Content-Security-Policy"]


class TestRateLimiting:
    @staticmethod
    def _seed_admin(app):
        from app.extensions import db
        from app.models import Role, User

        user = User(username="admin", email="admin@example.test", role=Role.ADMIN.value)
        user.set_password(ADMIN_PASSWORD)
        db.session.add(user)
        db.session.commit()

    def test_login_attempts_are_throttled(self, limited_app):
        """Unlimited login attempts make offline-quality password guessing online."""
        self._seed_admin(limited_app)
        client = limited_app.test_client()

        statuses = [
            client.post("/login", data={"username": "admin", "password": "wrong"}).status_code
            for _ in range(15)
        ]
        assert 429 in statuses

    def test_a_429_reports_json_for_api_callers(self, limited_app):
        self._seed_admin(limited_app)
        client = limited_app.test_client()

        last = None
        for _ in range(15):
            last = client.post(
                "/login",
                data={"username": "admin", "password": "wrong"},
                headers={"Accept": "application/json"},
            )
        assert last.status_code == 429
        assert last.get_json()["status"] == 429

    def test_healthz_is_exempt(self, limited_app):
        """The probe runs every 30s and must never be throttled."""
        client = limited_app.test_client()
        statuses = {client.get("/healthz").status_code for _ in range(40)}
        assert statuses == {200}


class TestErrorHandling:
    def test_unknown_page_renders_a_404(self, admin_client):
        response = admin_client.get("/no-such-page")
        assert response.status_code == 404
        assert b"not found" in response.data.lower()

    def test_unknown_api_path_returns_json(self, admin_client):
        response = admin_client.get("/api/no-such-endpoint")
        assert response.status_code == 404
        assert response.get_json()["success"] is False

    def test_missing_student_api_returns_json_404(self, admin_client):
        response = admin_client.get("/api/student/999")
        assert response.status_code == 404
        assert "error" in response.get_json()

    def test_internal_errors_do_not_leak_details(self, app, admin_client, monkeypatch):
        """The old handlers returned str(exception) straight to the client."""
        from app.services import analytics_service

        def explode(*_args, **_kwargs):
            raise RuntimeError("connection string postgres://user:hunter2@db/prod")

        monkeypatch.setattr(analytics_service, "trend", explode)
        app.config["PROPAGATE_EXCEPTIONS"] = False

        response = admin_client.get("/api/analytics/trend", headers={"Accept": "application/json"})

        assert response.status_code == 500
        body = response.get_data(as_text=True)
        assert "hunter2" not in body
        assert "postgres://" not in body
        assert response.get_json()["reference"]

    def test_internal_error_reference_appears_in_the_log(
        self, app, admin_client, monkeypatch, caplog
    ):
        """The reference shown to the user must be findable in the server log."""
        from app.services import analytics_service

        monkeypatch.setattr(
            analytics_service, "trend", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
        )
        app.config["PROPAGATE_EXCEPTIONS"] = False
        # TestingConfig pins the log level to CRITICAL to keep test output quiet, and
        # configure_logging() sets propagate=False so records are not emitted twice.
        # caplog listens on the root logger, so both have to be relaxed here.
        monkeypatch.setattr(app.logger, "propagate", True)
        caplog.set_level("ERROR", logger="app")

        with caplog.at_level("ERROR"):
            response = admin_client.get(
                "/api/analytics/trend", headers={"Accept": "application/json"}
            )

        reference = response.get_json()["reference"]
        assert reference in caplog.text

    def test_oversized_upload_is_rejected(self, app, admin_client):
        app.config["MAX_CONTENT_LENGTH"] = 1024
        response = admin_client.post(
            "/register_student",
            data={"image": (__import__("io").BytesIO(b"x" * 5000), "big.jpg")},
            content_type="multipart/form-data",
        )
        assert response.status_code == 413
