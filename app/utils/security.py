"""Authorisation helpers and response hardening."""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar, cast

from flask import Response, abort
from flask_login import current_user

F = TypeVar("F", bound=Callable[..., Any])


def roles_required(*roles: str) -> Callable[[F], F]:
    """Require an authenticated user holding one of ``roles``.

    Stacks under ``@login_required`` -- this only checks the role, so the login
    decorator still handles the unauthenticated redirect.
    """

    def decorator(view: F) -> F:
        @wraps(view)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not current_user.is_authenticated:
                abort(401)
            if not current_user.has_role(*roles):
                abort(403)
            return view(*args, **kwargs)

        return cast(F, wrapper)

    return decorator


admin_required = roles_required("admin")


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    # The templates load jQuery, Chart.js and Font Awesome from CDNs and use inline
    # event handlers throughout, so 'unsafe-inline' is required until the frontend is
    # reworked.  Documented as a known limitation rather than omitting the header.
    "Content-Security-Policy": (
        "default-src 'self'; "
        "img-src 'self' data: blob:; "
        "script-src 'self' 'unsafe-inline' "
        "https://cdn.jsdelivr.net https://cdnjs.cloudflare.com https://code.jquery.com; "
        "style-src 'self' 'unsafe-inline' "
        "https://cdn.jsdelivr.net https://cdnjs.cloudflare.com https://fonts.googleapis.com; "
        "font-src 'self' data: https://cdnjs.cloudflare.com https://fonts.gstatic.com; "
        "connect-src 'self'; "
        "frame-ancestors 'none'"
    ),
}


def apply_security_headers(response: Response) -> Response:
    """Attach hardening headers, without clobbering ones a view already set."""
    for header, value in SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    return response
