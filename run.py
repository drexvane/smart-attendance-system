#!/usr/bin/env python3
"""Development launcher.

    python run.py

Serves on 127.0.0.1 by default -- not 0.0.0.0 -- so a debug-enabled server is not
exposed to the local network.  Set HOST explicitly if you need it reachable.

For production use gunicorn:  gunicorn -c gunicorn.conf.py wsgi:app
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

from app import create_app
from app.recognition import shutdown_recognition

load_dotenv()

app = create_app()

if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    debug = os.environ.get("FLASK_ENV", "development") == "development"

    # The warning below is *about* binding to all interfaces; the default is 127.0.0.1.
    if host == "0.0.0.0" and debug:  # nosec B104 # noqa: S104
        app.logger.warning(
            "Serving with debug=True on 0.0.0.0 exposes the Werkzeug debugger to the "
            "network. Use gunicorn for anything other than local development."
        )

    try:
        app.run(host=host, port=port, debug=debug, threaded=True)
    finally:
        shutdown_recognition(app)
