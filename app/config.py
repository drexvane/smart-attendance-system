"""Application configuration.

One class per environment, selected by name in :func:`app.create_app`.  Everything that
differs between a laptop and a server lives here so that no module has to inspect
``FLASK_ENV`` at import time.
"""

from __future__ import annotations

import os
import secrets
from datetime import timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


class BaseConfig:
    """Settings shared by every environment."""

    # --- Flask core -------------------------------------------------------------
    SECRET_KEY: str = os.environ.get("SECRET_KEY", "")
    PERMANENT_SESSION_LIFETIME = timedelta(hours=12)

    # --- Database ---------------------------------------------------------------
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL") or "sqlite:///attendance.db"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS: dict = {"pool_pre_ping": True}

    # --- Session / cookie hardening --------------------------------------------
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = False  # flipped on in ProductionConfig
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = "Lax"

    # --- Uploads ----------------------------------------------------------------
    UPLOAD_FOLDER = str(BASE_DIR / "static" / "uploads")
    STUDENT_IMAGES_FOLDER = str(BASE_DIR / "student_images")
    EXPORT_FOLDER = str(BASE_DIR / "exports")
    FACE_DATA_FOLDER = str(BASE_DIR / "face_data")
    LOG_FOLDER = str(BASE_DIR / "logs")
    MODEL_FOLDER = str(BASE_DIR / "models")
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16 MB
    ALLOWED_IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".bmp"})
    MAX_IMAGE_PIXELS = 40_000_000  # decompression-bomb ceiling

    # --- Time -------------------------------------------------------------------
    # Records are stored in UTC.  This is the zone used to decide what "today"
    # means and to render timestamps, so a late-evening mark does not land on the
    # wrong day.  Any IANA name, e.g. "Asia/Kolkata".
    APP_TIMEZONE = os.environ.get("APP_TIMEZONE", "UTC")

    # --- Attendance rules -------------------------------------------------------
    LATE_THRESHOLD_TIME = os.environ.get("LATE_THRESHOLD_TIME", "09:30")
    ATTENDANCE_STATUSES = ("Present", "Absent", "Late", "Excused", "On Leave")
    LEAVE_TYPES = ("Sick", "Personal", "Family", "Academic", "Other")

    # --- Face detection ---------------------------------------------------------
    # DNN detector (res10 SSD).  Both files are tracked in models/; if either is
    # missing the detector falls back to a Haar cascade automatically.
    FACE_DETECTOR_PROTO = str(BASE_DIR / "models" / "deploy.prototxt")
    FACE_DETECTOR_WEIGHTS = str(BASE_DIR / "models" / "res10_300x300_ssd_iter_140000.caffemodel")
    FACE_DETECTION_CONFIDENCE = _env_float("FACE_DETECTION_CONFIDENCE", 0.6)
    FACE_CROP_SIZE = 200  # LBPH training samples are square grayscale crops
    FACE_CROP_MARGIN = 0.15

    # --- Face recognition (LBPH) ------------------------------------------------
    # LBPH returns a *distance*: lower is a better match.  Predictions above this
    # are rejected as unknown.  Typical usable range is roughly 30-90.
    FACE_MATCH_MAX_DISTANCE = _env_float("FACE_MATCH_MAX_DISTANCE", 70.0)
    # The runner-up must be at least this much worse than the winner, otherwise the
    # match is ambiguous and gets rejected.  This is the check the old histogram
    # matcher lacked entirely.
    FACE_MATCH_MIN_MARGIN = _env_float("FACE_MATCH_MIN_MARGIN", 8.0)
    FACE_MODEL_PATH = str(BASE_DIR / "face_data" / "lbph_model.yml")
    FACE_LABEL_MAP_PATH = str(BASE_DIR / "face_data" / "label_map.json")

    # --- Anti-proxy confirmation ------------------------------------------------
    # A student must be recognised in this many consecutive processed frames before
    # they become eligible for auto-marking.  A single-frame false positive can no
    # longer write an attendance record.
    RECOGNITION_CONFIRM_FRAMES = _env_int("RECOGNITION_CONFIRM_FRAMES", 5)
    # Once marked (or rejected), ignore the same student for this long.
    RECOGNITION_COOLDOWN_SECONDS = _env_int("RECOGNITION_COOLDOWN_SECONDS", 60)
    # Auto-marking may never be more permissive than recognition itself, so there is
    # deliberately no separate, lower auto-mark threshold here.

    # --- Camera -----------------------------------------------------------------
    CAMERA_INDEX = _env_int("CAMERA_INDEX", 0)
    CAMERA_WIDTH = _env_int("CAMERA_WIDTH", 640)
    CAMERA_HEIGHT = _env_int("CAMERA_HEIGHT", 480)
    CAMERA_FPS = _env_int("CAMERA_FPS", 30)
    CAMERA_READ_TIMEOUT = 5.0

    # --- Pagination -------------------------------------------------------------
    STUDENTS_PER_PAGE = 50
    ATTENDANCE_PER_PAGE = 100
    MAX_PER_PAGE = 500
    EXPORT_MAX_ROWS = _env_int("EXPORT_MAX_ROWS", 50_000)

    # --- Rate limiting ----------------------------------------------------------
    # memory:// is per-process and therefore ineffective behind multiple workers.
    # Point this at Redis in production, e.g. redis://localhost:6379/0
    RATELIMIT_STORAGE_URI = os.environ.get("RATELIMIT_STORAGE_URI", "memory://")
    RATELIMIT_STRATEGY = "fixed-window"
    RATELIMIT_DEFAULT = "600 per hour"
    RATELIMIT_HEADERS_ENABLED = True
    RATELIMIT_ENABLED = True

    # --- Logging ----------------------------------------------------------------
    LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
    LOG_MAX_BYTES = 5 * 1024 * 1024
    LOG_BACKUP_COUNT = 5

    TESTING = False
    DEBUG = False


class DevelopmentConfig(BaseConfig):
    DEBUG = True
    # Regenerated on each restart when unset, which logs everyone out.  Set
    # SECRET_KEY in .env to keep sessions across reloads.
    SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
    LOG_LEVEL = os.environ.get("LOG_LEVEL", "DEBUG")


class TestingConfig(BaseConfig):
    TESTING = True
    DEBUG = False
    # Fixed so a session survives between requests inside one test. This class is only
    # selected by FLASK_ENV=testing, and ProductionConfig.validate() requires a real
    # SECRET_KEY from the environment, so this string cannot reach a deployment.
    SECRET_KEY = "testing-only-not-a-real-secret"  # nosec B105 # noqa: S105
    SQLALCHEMY_DATABASE_URI = os.environ.get("TEST_DATABASE_URL", "sqlite://")
    SQLALCHEMY_ENGINE_OPTIONS: dict = {}
    WTF_CSRF_ENABLED = False  # individual tests re-enable this to assert 400s
    RATELIMIT_ENABLED = False
    LOG_LEVEL = "CRITICAL"
    RECOGNITION_CONFIRM_FRAMES = 2
    RECOGNITION_COOLDOWN_SECONDS = 0


class ProductionConfig(BaseConfig):
    SESSION_COOKIE_SECURE = True
    REMEMBER_COOKIE_SECURE = True
    PREFERRED_URL_SCHEME = "https"

    @staticmethod
    def validate() -> None:
        """Fail fast on a misconfigured production deploy.

        Called from :func:`app.create_app` -- Flask reads config classes via
        ``from_object`` without instantiating them, so this cannot live in
        ``__init__``.
        """
        if not os.environ.get("SECRET_KEY"):
            raise RuntimeError(
                "SECRET_KEY must be set in production. Generate one with:\n"
                '  python -c "import secrets; print(secrets.token_hex(32))"'
            )
        if BaseConfig.RATELIMIT_STORAGE_URI.startswith("memory://"):
            # Not fatal, but it silently disables rate limiting across workers.
            import warnings

            warnings.warn(
                "RATELIMIT_STORAGE_URI is memory://, which is per-process and will "
                "not limit correctly behind multiple workers. Use Redis in production.",
                RuntimeWarning,
                stacklevel=2,
            )


CONFIGS: dict[str, type[BaseConfig]] = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
}


def resolve_config(name: str | None = None) -> type[BaseConfig]:
    """Pick a config class by name, falling back to ``FLASK_ENV``."""
    key = (name or os.environ.get("FLASK_ENV") or "development").strip().lower()
    if key not in CONFIGS:
        raise ValueError(f"Unknown config {key!r}; expected one of {sorted(CONFIGS)}")
    return CONFIGS[key]
