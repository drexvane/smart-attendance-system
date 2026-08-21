"""WSGI entrypoint.

Used by gunicorn (``gunicorn -c gunicorn.conf.py wsgi:app``) and by
``flask --app wsgi``.  The previous production command was ``python app.py``, which
started the Werkzeug development server with ``debug=True`` bound to ``0.0.0.0`` --
the debugger console would have been reachable from the network.
"""

from __future__ import annotations

import atexit

from app import create_app
from app.recognition import shutdown_recognition

app = create_app()

# Release the capture device when the worker exits, so the camera is not left held.
atexit.register(shutdown_recognition, app)
