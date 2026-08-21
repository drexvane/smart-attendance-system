"""Gunicorn configuration.

**One worker, on purpose.**

The recognition pipeline owns a webcam.  A webcam is a single physical device: it cannot
be opened by four processes at once, and it cannot be sharded across them.  The old code
kept ``detection_active`` and ``face_recognition_active`` in module-level globals, so
with ``workers > 1`` a request that landed on worker B reported worker A's camera as
stopped -- it did not fail, it just answered wrongly, which is worse.

So the process count is pinned at one and concurrency comes from threads instead.  The
pipeline is a per-process singleton and ``/api/recognition/status`` reports the state of
the process that actually holds the device.

If you need to scale the web tier horizontally, split the camera out into its own
service first; raising ``workers`` here will not do it.
"""

from __future__ import annotations

import multiprocessing
import os

# --- socket ---------------------------------------------------------------------
bind = os.environ.get("GUNICORN_BIND", "0.0.0.0:5000")  # noqa: S104 - containers publish
backlog = 2048

# --- worker model ---------------------------------------------------------------
workers = 1  # see the module docstring; do not raise this while the camera is in-process
threads = int(os.environ.get("GUNICORN_THREADS", "4"))
worker_class = "gthread"
# /dev/shm is a tmpfs on Linux and is where gunicorn's own docs put the heartbeat file;
# the isdir() guard means a host without it falls back to the default instead of failing.
worker_tmp_dir = "/dev/shm" if os.path.isdir("/dev/shm") else None  # nosec B108 # noqa: S108

# The MJPEG endpoint is a long-lived streaming response, so the usual short request
# timeout would kill a working camera feed.
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "120"))
graceful_timeout = 30
keepalive = 5

# Recycle periodically: OpenCV allocations are not always returned to the OS.
max_requests = int(os.environ.get("GUNICORN_MAX_REQUESTS", "1000"))
max_requests_jitter = 100

# --- logging --------------------------------------------------------------------
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")
# %({x-forwarded-for}i)s so the real client shows up behind a reverse proxy.
access_log_format = '%({x-forwarded-for}i)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s %(D)s'

# --- misc -----------------------------------------------------------------------
preload_app = False  # the pipeline builds lazily per process; preloading would fork it
proc_name = "attendance-system"

# Reported for diagnostics only -- it is deliberately *not* used to set `workers`.
detected_cpus = multiprocessing.cpu_count()


def on_starting(server):
    server.log.info(
        "Starting with %d worker(s) x %d thread(s) on a %d-CPU host "
        "(single worker is intentional: the camera is one device)",
        workers,
        threads,
        detected_cpus,
    )


def worker_exit(server, worker):  # noqa: ARG001
    """Release the camera when a worker goes away."""
    try:
        from app.recognition import shutdown_recognition
        from wsgi import app

        shutdown_recognition(app)
    except Exception as exc:  # noqa: BLE001 - shutdown must not raise during exit
        server.log.warning("Recognition shutdown on worker exit failed: %s", exc)
