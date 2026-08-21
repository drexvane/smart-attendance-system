"""Shared pytest fixtures.

There was no conftest before this: the files in ``tests/`` were print-based scripts run
by hand, one of which opened a real camera (which would hang a CI runner), and none of
them could construct the application because it was built at import time.
"""

from __future__ import annotations

import shutil
import tempfile
from datetime import timedelta
from pathlib import Path

import numpy as np
import pytest

from app import create_app
from app.extensions import db as _db
from app.models import AttendanceRecord, Role, Student, User
from app.utils.time import today as app_today

__all__ = ["app_today", "make_face", "shuffle_preserving_histogram"]

ADMIN_PASSWORD = "admin-password-1234"  # noqa: S105 - fixture credential
TEACHER_PASSWORD = "teacher-password-1234"  # noqa: S105


@pytest.fixture(scope="session")
def tmp_data_root():
    """Isolated directories so tests never write into the repository tree."""
    root = Path(tempfile.mkdtemp(prefix="attendance-tests-"))
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def app(tmp_data_root, monkeypatch):
    monkeypatch.setenv("FLASK_ENV", "testing")
    application = create_app("testing")

    # Point every writable path at the temp root for this test.
    per_test = Path(tempfile.mkdtemp(dir=tmp_data_root))
    application.config.update(
        FACE_DATA_FOLDER=str(per_test / "face_data"),
        STUDENT_IMAGES_FOLDER=str(per_test / "student_images"),
        UPLOAD_FOLDER=str(per_test / "uploads"),
        EXPORT_FOLDER=str(per_test / "exports"),
        FACE_MODEL_PATH=str(per_test / "face_data" / "lbph_model.yml"),
        FACE_LABEL_MAP_PATH=str(per_test / "face_data" / "label_map.json"),
    )
    for key in ("FACE_DATA_FOLDER", "STUDENT_IMAGES_FOLDER", "UPLOAD_FOLDER", "EXPORT_FOLDER"):
        Path(application.config[key]).mkdir(parents=True, exist_ok=True)

    with application.app_context():
        _db.create_all()
        yield application
        _db.session.remove()
        _db.drop_all()


@pytest.fixture
def db(app):  # noqa: ARG001 - depends on app for the context
    return _db


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def admin(db):
    user = User(username="admin", email="admin@example.test", role=Role.ADMIN.value)
    user.set_password(ADMIN_PASSWORD)
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def teacher(db):
    user = User(username="teacher", email="teacher@example.test", role=Role.TEACHER.value)
    user.set_password(TEACHER_PASSWORD)
    db.session.add(user)
    db.session.commit()
    return user


def _login(client, username, password):
    response = client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert response.status_code == 302, f"login failed: {response.status_code}"
    return client


@pytest.fixture
def admin_client(client, admin):  # noqa: ARG001 - admin must exist first
    return _login(client, "admin", ADMIN_PASSWORD)


@pytest.fixture
def teacher_client(client, teacher):  # noqa: ARG001
    return _login(client, "teacher", TEACHER_PASSWORD)


@pytest.fixture
def students(db):
    """Three active students across two departments."""
    rows = [
        Student(
            student_id=f"S{index:03d}",
            name=f"Student {index}",
            email=f"student{index}@example.test",
            department="CSE" if index < 2 else "ECE",
            year="3",
            section="A",
        )
        for index in range(3)
    ]
    db.session.add_all(rows)
    db.session.commit()
    return rows


@pytest.fixture
def attendance_history(db, students):
    """Five days of records so analytics has something to aggregate.

    ``app_today`` rather than ``date.today()``: the application resolves "today" through
    ``APP_TIMEZONE``, and on a machine whose local zone is ahead of it the two disagree
    for part of every day.  Fixtures that seed data the services then query have to use
    the same clock the services do, or the tests fail depending on the hour they run.
    """
    today = app_today()
    created = []
    for offset in range(5):
        day = today - timedelta(days=offset)
        for index, student in enumerate(students):
            # Student 0 always present, 1 mixed, 2 mostly absent.
            if index == 0:
                status = "Present"
            elif index == 1:
                status = "Present" if offset % 2 == 0 else "Absent"
            else:
                status = "Absent"
            record = AttendanceRecord(
                student_id=student.id, date=day, status=status, marked_by="fixture"
            )
            created.append(record)
    db.session.add_all(created)
    db.session.commit()
    return created


@pytest.fixture
def file_db_app(tmp_path, monkeypatch):
    """An app backed by a SQLite *file* rather than ``:memory:``.

    The in-memory database is served through a StaticPool, i.e. one shared connection,
    which sqlite3 will not tolerate being driven from several threads at once.  Tests
    that exercise real concurrency need a file so each thread gets its own connection.
    """
    from app.config import TestingConfig

    database = tmp_path / "concurrency.db"
    monkeypatch.setenv("FLASK_ENV", "testing")
    monkeypatch.setattr(
        TestingConfig, "SQLALCHEMY_DATABASE_URI", f"sqlite:///{database.as_posix()}"
    )
    monkeypatch.setattr(
        TestingConfig,
        "SQLALCHEMY_ENGINE_OPTIONS",
        # A generous busy timeout: SQLite serialises writers, and without it a
        # contended write fails immediately with "database is locked".
        {"connect_args": {"timeout": 30, "check_same_thread": False}},
    )
    application = create_app("testing")

    per_test = tmp_path / "data"
    application.config.update(
        FACE_DATA_FOLDER=str(per_test / "face_data"),
        STUDENT_IMAGES_FOLDER=str(per_test / "student_images"),
    )
    with application.app_context():
        _db.create_all()
        yield application
        _db.session.remove()
        _db.drop_all()


@pytest.fixture
def limited_app(tmp_data_root, monkeypatch):
    """A separate app instance with rate limiting switched on.

    Flask-Limiter reads ``RATELIMIT_ENABLED`` during ``init_app`` and caches it per
    application, so the flag has to be set before ``create_app`` rather than toggled
    afterwards.
    """
    from app.config import TestingConfig

    monkeypatch.setenv("FLASK_ENV", "testing")
    monkeypatch.setattr(TestingConfig, "RATELIMIT_ENABLED", True)
    application = create_app("testing")

    per_test = Path(tempfile.mkdtemp(dir=tmp_data_root))
    application.config.update(
        FACE_DATA_FOLDER=str(per_test / "face_data"),
        STUDENT_IMAGES_FOLDER=str(per_test / "student_images"),
    )
    with application.app_context():
        _db.create_all()
        yield application
        _db.session.remove()
        _db.drop_all()


@pytest.fixture
def query_counter(app):
    """Counts SQL statements issued inside the ``with`` block.

    Used to pin the analytics endpoints at a fixed number of queries: they previously
    issued one per student or one per day, so ``/api/analytics/top_students`` with 500
    students meant 501 round trips.
    """
    from sqlalchemy import event

    class Counter:
        def __init__(self):
            self.statements: list[str] = []

        @property
        def count(self) -> int:
            return len(self.statements)

        def __enter__(self):
            self.statements.clear()
            event.listen(_db.engine, "before_cursor_execute", self._record)
            return self

        def __exit__(self, *exc):
            event.remove(_db.engine, "before_cursor_execute", self._record)
            return False

        def _record(self, conn, cursor, statement, *args, **kwargs):  # noqa: ARG002
            self.statements.append(statement)

    return Counter()


# --- synthetic face data ---------------------------------------------------------
def make_face(seed: int, size: int = 200) -> np.ndarray:
    """A deterministic, *spatially structured* grayscale image.

    Structure matters: the point of several tests is that the old descriptor (a plain
    intensity histogram) cannot tell these apart while LBPH can, so the images must
    differ in layout rather than only in overall brightness.
    """
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:size, 0:size]
    base = 40 + 60 * np.sin((x / size) * np.pi * (1 + seed % 3))
    bands = 50 * ((y // (10 + seed % 7)) % 2)
    blobs = 60 * np.exp(-(((x - size * 0.3 * (1 + seed % 2)) ** 2 + (y - size * 0.4) ** 2) / 900))
    noise = rng.normal(0, 4, (size, size))
    return np.clip(base + bands + blobs + noise, 0, 255).astype(np.uint8)


def shuffle_preserving_histogram(image: np.ndarray, seed: int = 0) -> np.ndarray:
    """Randomly permute pixels, which keeps the intensity histogram identical.

    Used to demonstrate concretely that a whole-image histogram carries no spatial
    information: this output scores as a perfect histogram match to its input while
    looking nothing like it.
    """
    rng = np.random.default_rng(seed)
    flat = image.flatten()
    rng.shuffle(flat)
    return flat.reshape(image.shape)
