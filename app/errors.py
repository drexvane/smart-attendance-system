"""Centralised error handling.

The old code repeated ``except Exception as e: return jsonify({'error': str(e)}), 500``
around forty times, which handed SQLAlchemy and filesystem internals to the client, and
registered ``@app.errorhandler(400)`` to sniff for the substring ``"csrf"`` -- so every
unrelated 400 in the application went through CSRF-specific handling.

Here each status has one handler, HTML and JSON are chosen by content negotiation, and
unexpected failures are logged with a traceback and a correlation id that is echoed to
the client so a user-reported error can be found in the log.
"""

from __future__ import annotations

import uuid

from flask import Flask, Response, current_app, flash, jsonify, redirect, render_template, request
from flask import url_for as flask_url_for
from flask_wtf.csrf import CSRFError
from werkzeug.exceptions import HTTPException

GENERIC_MESSAGE = "Something went wrong while processing your request."


def wants_json() -> bool:
    """True when the caller expects JSON rather than a rendered page."""
    if request.is_json or request.path.startswith("/api/"):
        return True
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return True
    accept = request.accept_mimetypes
    return accept["application/json"] > accept["text/html"]


def _json_error(status: int, message: str, *, reference: str | None = None) -> Response:
    payload: dict[str, object] = {"success": False, "error": message, "status": status}
    if reference:
        payload["reference"] = reference
    response = jsonify(payload)
    response.status_code = status
    return response


def _html_error(status: int, message: str, reference: str | None = None) -> tuple[str, int]:
    return (
        render_template("error.html", status=status, message=message, reference=reference),
        status,
    )


def register_error_handlers(app: Flask) -> None:
    @app.errorhandler(CSRFError)
    def handle_csrf_error(error: CSRFError):
        current_app.logger.warning(
            "CSRF validation failed on %s %s: %s", request.method, request.path, error.description
        )
        message = "Security token missing or expired. Refresh the page and try again."
        # 400 for both HTML and JSON.  Redirecting with a flash would hide a rejected
        # write behind a 302 and make the failure invisible to anything but a browser.
        if wants_json():
            return _json_error(400, message)
        flash(message, "error")
        return _html_error(400, message)

    @app.errorhandler(400)
    def handle_bad_request(error: HTTPException):
        message = error.description or "The request could not be understood."
        if wants_json():
            return _json_error(400, message)
        return _html_error(400, message)

    @app.errorhandler(401)
    def handle_unauthorized(error: HTTPException):  # noqa: ARG001
        if wants_json():
            return _json_error(401, "Authentication required.")
        flash("Please sign in to continue.", "warning")
        return redirect(flask_url_for("auth.login", next=request.path))

    @app.errorhandler(403)
    def handle_forbidden(error: HTTPException):  # noqa: ARG001
        message = "You do not have permission to perform this action."
        if wants_json():
            return _json_error(403, message)
        return _html_error(403, message)

    @app.errorhandler(404)
    def handle_not_found(error: HTTPException):  # noqa: ARG001
        message = "The requested resource was not found."
        if wants_json():
            return _json_error(404, message)
        return _html_error(404, message)

    @app.errorhandler(413)
    def handle_too_large(error: HTTPException):  # noqa: ARG001
        limit = app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024)
        message = f"Upload is too large. The limit is {limit} MB."
        if wants_json():
            return _json_error(413, message)
        return _html_error(413, message)

    @app.errorhandler(429)
    def handle_rate_limited(error: HTTPException):
        message = f"Too many requests. {error.description or 'Please slow down.'}"
        if wants_json():
            return _json_error(429, message)
        return _html_error(429, message)

    @app.errorhandler(500)
    @app.errorhandler(Exception)
    def handle_unexpected(error: Exception):
        # Let deliberate HTTP responses (abort(404) and friends) reach their own handler.
        if isinstance(error, HTTPException):
            return error

        reference = uuid.uuid4().hex[:12]
        current_app.logger.exception(
            "Unhandled error [ref=%s] on %s %s", reference, request.method, request.path
        )
        if wants_json():
            return _json_error(500, GENERIC_MESSAGE, reference=reference)
        return _html_error(500, GENERIC_MESSAGE, reference)
