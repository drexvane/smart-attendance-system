"""Application factory.

The previous entrypoint built the app at module import: it constructed the Flask
object, opened a camera, created database tables and made directories, all as a side
effect of ``import app``.  That made the configuration untestable and meant any import
of the module touched the filesystem and hardware.  Everything now happens inside
:func:`create_app`.
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, Response, jsonify
from flask_swagger_ui import get_swaggerui_blueprint
from flask_wtf.csrf import generate_csrf
from sqlalchemy import text

from app.config import ProductionConfig, resolve_config
from app.errors import register_error_handlers
from app.extensions import csrf, db, limiter, login_manager, migrate
from app.utils.logging import configure_logging
from app.utils.security import apply_security_headers
from app.utils.time import to_local, today, utcnow

__all__ = ["create_app"]

SWAGGER_URL = "/api/docs"
API_SPEC_URL = "/static/swagger.yaml"


def create_app(config_name: str | None = None) -> Flask:
    load_dotenv()

    config_class = resolve_config(config_name)
    if config_class is ProductionConfig:
        ProductionConfig.validate()

    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.config.from_object(config_class)

    configure_logging(app)
    _ensure_directories(app)
    _init_extensions(app)
    _register_blueprints(app)
    register_error_handlers(app)
    _register_template_globals(app)
    _register_hooks(app)
    _register_cli(app)
    _register_health(app)

    app.logger.info(
        "Application ready (config=%s, db=%s)",
        config_class.__name__,
        _redact_uri(app.config["SQLALCHEMY_DATABASE_URI"]),
    )
    return app


def _redact_uri(uri: str) -> str:
    """Hide any password in a database URI before it reaches the log."""
    if "@" not in uri or "://" not in uri:
        return uri
    scheme, rest = uri.split("://", 1)
    credentials, host = rest.rsplit("@", 1)
    user = credentials.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}"


def _ensure_directories(app: Flask) -> None:
    for key in (
        "UPLOAD_FOLDER",
        "STUDENT_IMAGES_FOLDER",
        "EXPORT_FOLDER",
        "FACE_DATA_FOLDER",
        "LOG_FOLDER",
    ):
        try:
            Path(app.config[key]).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            app.logger.warning("Could not create %s (%s): %s", key, app.config[key], exc)


def _init_extensions(app: Flask) -> None:
    db.init_app(app)
    # render_as_batch is mandatory for SQLite: it rebuilds the table so that adding a
    # unique constraint or dropping a column becomes possible.
    migrate.init_app(app, db, render_as_batch=True)
    csrf.init_app(app)
    login_manager.init_app(app)

    # Flask-Limiter reads RATELIMIT_ENABLED here and caches it per app; assigning
    # limiter.enabled afterwards has no effect, so the switch lives in config only.
    limiter.init_app(app)

    login_manager.login_view = "auth.login"
    login_manager.login_message = "Please sign in to continue."
    login_manager.login_message_category = "warning"
    login_manager.session_protection = "strong"

    from app.models import User

    @login_manager.user_loader
    def load_user(user_id: str):
        return db.session.get(User, int(user_id)) if user_id.isdigit() else None


def _register_blueprints(app: Flask) -> None:
    from app.blueprints.api import api_bp
    from app.blueprints.attendance import attendance_bp
    from app.blueprints.auth import auth_bp
    from app.blueprints.dashboard import dashboard_bp
    from app.blueprints.leave import leave_bp
    from app.blueprints.recognition import recognition_bp
    from app.blueprints.students import students_bp

    for blueprint in (
        auth_bp,
        dashboard_bp,
        students_bp,
        attendance_bp,
        leave_bp,
        recognition_bp,
        api_bp,
    ):
        app.register_blueprint(blueprint)

    swagger_bp = get_swaggerui_blueprint(
        SWAGGER_URL,
        API_SPEC_URL,
        config={"app_name": "Smart Attendance System API", "deepLinking": True},
    )
    app.register_blueprint(swagger_bp, url_prefix=SWAGGER_URL)


def _register_template_globals(app: Flask) -> None:
    @app.context_processor
    def inject_globals() -> dict:
        # generate_csrf is a module-level function in flask_wtf.csrf.  The old code
        # called csrf.generate_csrf() -- a method that does not exist on CSRFProtect --
        # so every template using csrf_token() raised AttributeError.
        return {
            "csrf_token": generate_csrf,
            "now": utcnow,
            "today": today,
            "to_local": to_local,
        }

    @app.template_filter("localtime")
    def localtime_filter(value, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
        local = to_local(value)
        return local.strftime(fmt) if local else ""


def _register_hooks(app: Flask) -> None:
    @app.after_request
    def secure_response(response: Response) -> Response:
        return apply_security_headers(response)


def _register_health(app: Flask) -> None:
    @app.route("/healthz")
    @csrf.exempt
    @limiter.exempt
    def healthz():
        """Liveness/readiness probe.

        The container HEALTHCHECK used to request ``/``, which now redirects to the
        login page; this endpoint stays anonymous and actually touches the database.
        """
        try:
            db.session.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001 - probe must not raise
            app.logger.error("Health check failed: %s", exc)
            return jsonify({"status": "unhealthy", "database": "unreachable"}), 503
        return jsonify(
            {
                "status": "healthy",
                "database": "ok",
                "version": app.config.get("APP_VERSION", "2.0.0"),
                "time": utcnow().isoformat(),
            }
        )


def _register_cli(app: Flask) -> None:
    from app.cli import register_commands

    register_commands(app)
