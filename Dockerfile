# Smart Attendance System -- production image.
#
# What changed from the previous Dockerfile, and why (the Docker job was
# `continue-on-error: true`, so none of this had ever been observed to fail):
#
#   * `libgl1-mesa-glx` does not exist on Debian bookworm, which python:*-slim now
#     resolves to.  The package is `libgl1`.  The old build could not have worked.
#   * `libopencv-dev` was installed in the *runtime* stage -- hundreds of MB of headers
#     that a headless wheel has no use for.  Dropped.
#   * `pip wheel --no-deps` built wheels *without* their dependencies, so the install
#     step re-resolved everything from PyPI anyway and the wheel cache bought nothing.
#     Replaced with a virtualenv built in the builder stage and copied wholesale.
#   * `mkdir -p` ran *after* `USER appuser`, so it wrote into a root-owned WORKDIR.
#     Directories are now created and chowned before the user switch.
#   * HEALTHCHECK hit `/`, which redirects to `/login` now that auth exists.  It uses
#     `/healthz`, which also checks database connectivity.
#   * CMD was `python app.py`, i.e. the Flask development server -- and that module set
#     `debug=True, host='0.0.0.0'`, which exposes the Werkzeug debugger console (remote
#     code execution) to anything that can reach the port.  It is gunicorn now.

# ---------------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# build-essential is needed only if a dependency has no wheel for this platform.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# A venv, rather than a wheelhouse: it is copied verbatim into the runtime stage, so
# the runtime never contacts PyPI and cannot resolve a different version than was built.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt ./
RUN pip install --upgrade pip setuptools wheel \
    && pip install -r requirements.txt

# ---------------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

# libgl1 + libglib2.0-0 are what opencv-contrib-python-headless actually links against.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FLASK_APP=wsgi.py \
    FLASK_ENV=production

WORKDIR /app

# Application code, then the writable data directories -- both owned by the runtime
# user *before* the USER switch, because a container cannot chown its own WORKDIR.
COPY . .
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p instance logs exports face_data student_images static/uploads \
    && chown -R appuser:appuser /app

# Declared so an operator gets a warning when they forget to mount them: everything
# below is state that must outlive the container.
VOLUME ["/app/instance", "/app/face_data", "/app/student_images", "/app/logs"]

USER appuser

EXPOSE 5000

# /healthz is unauthenticated, CSRF-exempt, rate-limit-exempt, and executes SELECT 1,
# so a container reporting healthy has actually reached its database.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:5000/healthz || exit 1

# Config lives in gunicorn.conf.py, which documents why workers is pinned at 1.
CMD ["gunicorn", "-c", "gunicorn.conf.py", "wsgi:app"]
