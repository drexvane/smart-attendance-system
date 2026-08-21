# Deployment Guide

This guide replaces one that no longer described this application. The previous version
told you to `pip install dlib` (nothing here has ever imported it), to create the schema
with `db.create_all()` (there are Alembic migrations, and skipping them leaves the
database without the constraint that makes marking idempotent), to serve
`gunicorn app:app` with `workers = 4` (the module `app.py` is gone, and four workers
cannot share one webcam), and to keep `SECRET_KEY = os.environ.get("SECRET_KEY") or
"production-secret-key"` as a fallback.

Read [Before you deploy](#before-you-deploy) first. There is one hard constraint and one
honest limitation, and both change how you should size and pitch this system.

## Contents

1. [Before you deploy](#before-you-deploy)
2. [Requirements](#requirements)
3. [Local setup](#local-setup)
4. [Production: bare metal or VM](#production-bare-metal-or-vm)
5. [Production: Docker](#production-docker)
6. [PostgreSQL](#postgresql)
7. [Nginx and TLS](#nginx-and-tls)
8. [Configuration reference](#configuration-reference)
9. [Operations](#operations)
10. [CI/CD](#cicd)
11. [Troubleshooting](#troubleshooting)

---

## Before you deploy

**One worker, because a camera is one device.** The recognition pipeline holds a
`cv2.VideoCapture` handle. A webcam cannot be opened by four processes at once and cannot
be sharded across them, so `gunicorn.conf.py` pins `workers = 1` and gets concurrency from
threads. Raising it does not scale recognition; it produces workers that answer
`/api/recognition/status` about a camera they do not own. If you need to scale the web
tier, split the camera into its own service first.

**No liveness detection.** LBPH plus consecutive-frame confirmation resists casual proxy
attendance. It does not detect a printed photo or a face on a phone screen. If attendance
carries academic consequences, keep a human in the loop and say so to the people being
recorded.

**Biometric data has legal weight.** Face samples live on disk in `face_data/`,
unencrypted. Have a consent process, a retention period, and a deletion path before you
collect any. In India the DPDP Act treats this as personal data; the GDPR treats it as a
special category.

---

## Requirements

| | |
|---|---|
| Python | 3.12 primary, 3.11 floor. `runtime.txt` and `.python-version` both say 3.12; CI tests both. |
| OS | Linux for production (gunicorn does not run on Windows). Windows and macOS are fine for development. |
| Database | SQLite by default. PostgreSQL 14+ supported and exercised in CI. |
| Redis | Optional but wanted in production, for rate-limit state. |
| Camera | A V4L2 device for on-host recognition. Not required to run the web app. |
| Disk | ~1 GB for the image and dependencies, plus face samples (~150 KB per enrolled student). |

**No dlib, no CMake, no compiler for the recognition stack.** Detection is the bundled
res10 SSD via `cv2.dnn`; recognition is `cv2.face.LBPHFaceRecognizer`. Both ship in
`opencv-contrib-python-headless`, which is a wheel. The one system requirement is that
that wheel's shared libraries are present: `libgl1` and `libglib2.0-0`.

```bash
sudo apt-get install -y python3.12 python3.12-venv libgl1 libglib2.0-0
```

On a headless server you also want `v4l-utils` if a camera is attached, to confirm the
device index before you set `CAMERA_INDEX`.

---

## Local setup

```bash
git clone <repo-url> && cd attendance-system-master
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

cp .env.example .env
python -c "import secrets; print(secrets.token_hex(32))"   # paste into SECRET_KEY

python scripts/download_models.py      # only if models/ is missing or checksums fail
flask --app wsgi db upgrade            # creates instance/attendance.db
flask --app wsgi create-admin          # prompts; there are no default credentials
python run.py                          # http://127.0.0.1:5000
```

`run.py` binds 127.0.0.1 deliberately, so a debug-enabled server is not exposed to the
local network. Set `HOST` if you need otherwise, and do not do that in production.

---
## Production: bare metal or VM

### 1. User, code, virtualenv

```bash
sudo useradd --system --create-home --shell /bin/bash attendance
sudo -u attendance -H bash
cd ~ && git clone <repo-url> app && cd app
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt gunicorn
```

If the camera is attached, add the service user to the video group, or it will open no
device and the pipeline will report a camera error rather than crash:

```bash
sudo usermod -aG video attendance
```

### 2. Environment

Write `/home/attendance/app/.env` with mode `600`. Minimum viable production set:

```ini
FLASK_ENV=production
SECRET_KEY=<64 hex chars from secrets.token_hex(32)>
DATABASE_URL=postgresql+psycopg://attendance:<password>@localhost:5432/attendance
RATELIMIT_STORAGE_URI=redis://localhost:6379/0
APP_TIMEZONE=Asia/Kolkata
LOG_LEVEL=INFO
```

`ProductionConfig.validate()` raises at startup if `SECRET_KEY` is unset — there is no
fallback default any more, so a misconfigured deploy fails loudly instead of shipping a
known key. Leaving `RATELIMIT_STORAGE_URI` at `memory://` only warns.

### 3. Migrations, then the first account

```bash
flask --app wsgi db upgrade
flask --app wsgi create-admin
```

Run `db upgrade` on every deploy, before the new process starts. Never use
`db.create_all()`: it produces a schema without the `(student_id, date)` unique
constraint, and that constraint is what makes concurrent marking idempotent.

---
### 4. systemd

`/etc/systemd/system/attendance.service`:

```ini
[Unit]
Description=Smart Attendance System
After=network.target postgresql.service redis-server.service

[Service]
Type=notify
User=attendance
Group=attendance
WorkingDirectory=/home/attendance/app
EnvironmentFile=/home/attendance/app/.env
ExecStart=/home/attendance/app/.venv/bin/gunicorn -c gunicorn.conf.py wsgi:app
ExecReload=/bin/kill -s HUP $MAINPID
Restart=always
RestartSec=5

# Hardening. SupplementaryGroups is what lets the camera device stay readable while
# the rest of the filesystem is not writable.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
ReadWritePaths=/home/attendance/app/instance /home/attendance/app/logs /home/attendance/app/face_data /home/attendance/app/student_images /home/attendance/app/exports
SupplementaryGroups=video

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now attendance
curl -fsS http://127.0.0.1:5000/healthz
```

Do not add `--workers N` to `ExecStart`. `gunicorn.conf.py` sets `workers = 1` and
explains why in its docstring; the tunable that helps is `GUNICORN_THREADS`.

`Type=notify` requires gunicorn 20+, which is what `requirements.txt` pins. If you
substitute another server, drop back to `Type=simple`.

---
## Production: Docker

The `Dockerfile` in the repository root is a two-stage build: a builder that creates
`/opt/venv` from `requirements.txt`, and a `python:3.12-slim-bookworm` runtime that copies
that venv in, so the runtime never contacts PyPI and cannot resolve a different version
than was built. It runs as uid 1000, declares the four writable directories as volumes,
and its `HEALTHCHECK` hits `/healthz`.

```bash
docker build -t attendance-system:latest .

docker run -d --name attendance -p 5000:5000 \
  -e SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')" \
  -e FLASK_ENV=production \
  -e APP_TIMEZONE=Asia/Kolkata \
  -v attendance-instance:/app/instance \
  -v attendance-faces:/app/face_data \
  -v attendance-images:/app/student_images \
  -v attendance-logs:/app/logs \
  attendance-system:latest

docker exec attendance flask --app wsgi db upgrade
docker exec -it attendance flask --app wsgi create-admin
curl -fsS http://127.0.0.1:5000/healthz
docker inspect -f '{{.State.Health.Status}}' attendance
```

Mount all four volumes. `instance/` is the SQLite database, `face_data/` is the trained
LBPH model and its samples, `student_images/` is uploaded photos, `logs/` is the rotating
log. Anything not mounted is lost with the container.

**Camera passthrough** needs `--device /dev/video0:/dev/video0` and a Linux host. Docker
Desktop for Windows and macOS runs a Linux VM with no USB camera passthrough, so
recognition cannot work there — the web app will, and the pipeline will report a camera
error. Confirm the index on the host with `v4l2-ctl --list-devices` before mapping it.

There is no `docker-compose.yml` in the repository. If you want one, it needs the same
four volumes, the same single container, and `command: gunicorn -c gunicorn.conf.py
wsgi:app`; do not set `deploy.replicas` above 1 while the camera is in-process.

`docker.yml` in CI builds this image, scans it with Trivy (gating on HIGH/CRITICAL),
starts it, and asserts both `/healthz` and the container's own health status before it
pushes anything. A red Docker job means the image is broken, not that the scanner is
noisy.

---
## PostgreSQL

SQLite is the default and is adequate for a single institution with one camera; the write
volume is a few hundred rows a day. Move to PostgreSQL when you want concurrent writers,
network access, or real backups.

The driver is **not** in `requirements.txt` — install it alongside, which is what CI does:

```bash
pip install "psycopg[binary]==3.2.10"
```

```sql
CREATE USER attendance WITH PASSWORD '<password>';
CREATE DATABASE attendance OWNER attendance;
```

```ini
DATABASE_URL=postgresql+psycopg://attendance:<password>@localhost:5432/attendance
```

Then `flask --app wsgi db upgrade`. The migrations are written with
`render_as_batch=True` for SQLite's benefit and apply unchanged on PostgreSQL; the
`test-postgres` job in `ci.yml` runs the full suite plus `db upgrade` and `db check`
against `postgres:17-alpine`, so this path is exercised on every push.

### Migrating an existing SQLite database

If your `instance/attendance.db` predates this refactor, it has no Alembic version table.
Back it up, stamp the baseline, then upgrade:

```bash
cp instance/attendance.db instance/attendance.db.bak
flask --app wsgi db stamp 0001_baseline
flask --app wsgi db upgrade
```

Revision `0002` de-duplicates `(student_id, date)` keeping the earliest record — it has to,
because `0003` adds the unique constraint and cannot apply over duplicates. Revision
`0003` also nulls the legacy `students.face_encoding` column: those were 256-bin intensity
histograms, meaningless to the LBPH recognizer. **Every student must be re-enrolled after
this upgrade.** Plan for that before you run it on live data.

---
## Nginx and TLS

TLS is not optional here. `ProductionConfig` sets `SESSION_COOKIE_SECURE = True`, so the
session cookie is only sent over HTTPS — over plain HTTP nobody can stay logged in.

`/etc/nginx/sites-available/attendance`:

```nginx
server {
    listen 443 ssl http2;
    server_name attendance.example.edu;

    ssl_certificate     /etc/letsencrypt/live/attendance.example.edu/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/attendance.example.edu/privkey.pem;

    client_max_body_size 16m;   # must match MAX_CONTENT_LENGTH, or uploads 413 at the proxy

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;   # required, or Flask builds http:// URLs
    }

    # The MJPEG feed is one long-lived response. Buffering it shows a frozen frame;
    # the default proxy_read_timeout cuts it off after 60s.
    location /get_video_feed {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_buffering off;
        proxy_read_timeout 3600s;
        chunked_transfer_encoding off;
    }
}

server {
    listen 80;
    server_name attendance.example.edu;
    return 301 https://$host$request_uri;
}
```

```bash
sudo ln -s /etc/nginx/sites-available/attendance /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d attendance.example.edu
```

If you terminate TLS elsewhere and gunicorn sees a proxy it does not trust, set
`forwarded_allow_ips` in `gunicorn.conf.py` accordingly. The access log format already
prints `X-Forwarded-For`, so the real client IP shows up once the header arrives.

---
## Configuration reference

Every value is read in `app/config.py`; `.env.example` is the annotated template. Only
`SECRET_KEY` is mandatory, and only in production.

| Variable | Default | Notes |
|---|---|---|
| `SECRET_KEY` | — | **Required in production**; startup raises without it. |
| `FLASK_ENV` | `development` | `development` \| `testing` \| `production`. |
| `DATABASE_URL` | `sqlite:///attendance.db` | Relative SQLite paths resolve inside `instance/`. |
| `APP_TIMEZONE` | `UTC` | IANA name. Decides what "today" means; records are stored in UTC. |
| `LATE_THRESHOLD_TIME` | `09:30` | Local time after which a mark is `Late`. |
| `FACE_DETECTION_CONFIDENCE` | `0.6` | res10 SSD threshold. |
| `FACE_MATCH_MAX_DISTANCE` | `70.0` | LBPH **distance**: lower is a better match. Usable 30–90. |
| `FACE_MATCH_MIN_MARGIN` | `8.0` | Winner must beat runner-up by this, else rejected as ambiguous. |
| `RECOGNITION_CONFIRM_FRAMES` | `5` | Consecutive frames before auto-marking may write. Raise, don't lower. |
| `RECOGNITION_COOLDOWN_SECONDS` | `60` | Per-student ignore window after a decision. |
| `CAMERA_INDEX` / `_WIDTH` / `_HEIGHT` / `_FPS` | `0` / `640` / `480` / `30` | |
| `RATELIMIT_STORAGE_URI` | `memory://` | Per-process; use `redis://…` in production. |
| `EXPORT_MAX_ROWS` | `50000` | Hard cap per export. |
| `LOG_LEVEL` | `INFO` | Rotating files under `logs/`. |
| `GUNICORN_BIND` | `0.0.0.0:5000` | |
| `GUNICORN_THREADS` | `4` | The concurrency knob. There is no `GUNICORN_WORKERS`. |
| `GUNICORN_TIMEOUT` | `120` | Long, because the MJPEG stream is long-lived. |

### Tuning recognition

Both thresholds trade the two error types against each other, and the right values depend
on your camera and lighting, so measure rather than guess:

* Students reported `Unknown` who should match → raise `FACE_MATCH_MAX_DISTANCE` in steps
  of 5, or re-enrol with more samples under the lighting you actually use.
* Wrong student matched → lower `FACE_MATCH_MAX_DISTANCE`, raise `FACE_MATCH_MIN_MARGIN`.
* Marks appearing for people who only walked past → raise `RECOGNITION_CONFIRM_FRAMES`.

Auto-marking deliberately shares recognition's threshold. The previous implementation
recognised above `0.4` confidence and auto-marked above `0.3`, so it wrote records it did
not consider matches; do not reintroduce a separate, looser auto-mark threshold.

---
## Operations

### Deploy a new version

```bash
cd /home/attendance/app
git pull
. .venv/bin/activate && pip install -r requirements.txt
flask --app wsgi db upgrade        # before restart, always
sudo systemctl restart attendance
curl -fsS http://127.0.0.1:5000/healthz
```

`/healthz` returns `{"status":"healthy","database":"ok",…}` with 200, or 503 with
`"database":"unreachable"`. It is anonymous, CSRF-exempt and rate-limit-exempt, and it
executes `SELECT 1` — so a 200 means the process reached its database, which is what a load
balancer should check. Do not point a health check at `/`: that redirects to `/login`.

### User accounts

```bash
flask --app wsgi create-admin                 # prompts for username, email, password
flask --app wsgi create-user --role teacher
flask --app wsgi list-users
flask --app wsgi deactivate-user <username>   # keeps the row, revokes access
```

Roles are `admin` and `teacher`. Destructive routes (permanent student deletion, user
management) require `admin`; every mutating route and both export routes require a login.
Passwords shorter than 12 characters are rejected by the CLI.

### Face model

```bash
flask --app wsgi recognition-info    # enrolled labels, sample counts, model mtime
flask --app wsgi rebuild-faces       # retrain LBPH from face_data/ samples
```

Rebuild after bulk enrolment, after restoring `face_data/` from backup, or if
`lbph_model.yml` is deleted. It reads the stored crops, so it does not need the camera.

### Backups

Three things are state: the database, `face_data/`, and `student_images/`. A backup missing
`face_data/` restores an app that recognises nobody.

```bash
# SQLite -- .backup is consistent against a running writer; cp is not.
sqlite3 instance/attendance.db ".backup '/backup/attendance-$(date +%F).db'"
# PostgreSQL
pg_dump -Fc attendance > /backup/attendance-$(date +%F).dump
tar czf /backup/faces-$(date +%F).tar.gz face_data student_images
```

Test a restore into a scratch directory before you need one. Set a retention period that
matches your consent notice, and delete face data when it expires.

---
## CI/CD

Relevant to deployment because the pipeline now decides whether a commit is deployable.
Previously every gate carried `continue-on-error: true` or `|| true`, including the build
job's own import smoke test, so the badges were green over an application that could not
start.

| Workflow | Gates on |
|---|---|
| `ci.yml` | `ruff check`, `ruff format --check`, `mypy app/`, `pytest` on 3.11 + 3.12 with a 75% coverage floor, the same suite against `postgres:17-alpine`, `flask db upgrade` + `db check` (migration drift), `bandit`, `pip-audit`, then a real boot under gunicorn asserting `/healthz` is 200 and `/` is 302. |
| `docker.yml` | Image build, Trivy HIGH/CRITICAL, container `/healthz` and `HEALTHCHECK` status — all before any push. |
| `cd.yml` | The full quality gate, then artifact and image publication. **The deploy steps are explicit dry runs**: they print what a real deployment would do and deploy nothing. The comment block in each marks where a real target plugs in. |
| `codeql.yml`, `code-review.yml`, `performance.yml` | Reporting. `code-review.yml` was moved off `pull_request_target` — it combined a privileged token with an untrusted checkout and interpolated PR filenames into a shell. |

Reproduce the gate locally before pushing:

```bash
ruff check . && ruff format --check . && mypy app/ && pytest -q --cov=app
bandit -c pyproject.toml -r app/ wsgi.py run.py gunicorn.conf.py
pip-audit --strict --requirement requirements.txt
```

`pre-commit install` runs the first two on commit, with the same pinned versions CI uses.

---

## Troubleshooting

**Startup fails with `SECRET_KEY must be set in production`.** Intended. Generate one and
put it in the environment file the service actually reads — systemd's `EnvironmentFile`,
not your interactive shell.

**`RuntimeWarning: RATELIMIT_STORAGE_URI is memory://`.** Rate limiting is per-process and
will not hold across restarts. Point it at Redis.

**Login succeeds, then every page bounces back to `/login`.** `SESSION_COOKIE_SECURE` is on
in production, so the cookie needs HTTPS. Behind a proxy, also confirm
`X-Forwarded-Proto` is being set.

**A form or API POST returns 400.** Missing or stale CSRF token. The state-changing
recognition endpoints are no longer CSRF-exempt; `fetch` calls must send `X-CSRFToken`.
Note the ordering: CSRF validation runs before `login_required`, so an unauthenticated POST
surfaces as 400 rather than a redirect.

**429 on normal use.** `RATELIMIT_DEFAULT` is 600/hour per client. Behind a proxy that does
not forward the client IP, every request looks like one client — fix the headers before
raising the limit.

**`/healthz` returns 503.** The database is unreachable. Check `DATABASE_URL`, then that
`instance/` is writable by the service user (a container missing its `instance` volume
gives exactly this).

**Camera errors, or a black feed.** Confirm the device exists (`ls /dev/video*`), that the
service user is in the `video` group, that `CAMERA_INDEX` matches, and that nothing else
holds the device — the pipeline uses a single shared capture thread, but another process on
the host will still block it. In Docker you need `--device`, and on Docker Desktop for
Windows/macOS it cannot work at all.

**Everyone is `Unknown`.** Either the model was never trained (`flask --app wsgi
recognition-info` shows `Recognizer trained: False` → run `rebuild-faces`), or you upgraded
an old database, in which case the legacy histogram encodings were nulled by design and
students must be re-enrolled.

**`recognition-info` reports the Haar backend.** The DNN model files are missing, so the
detector fell back. Run `python scripts/download_models.py`; it verifies SHA-256 digests
and exits non-zero on mismatch.

**`ImportError: cv2.face`.** The plain `opencv-python-headless` wheel is installed. LBPH
lives in contrib: `pip install opencv-contrib-python-headless==4.14.0.94`, and check for a
stray `opencv-python` in the same environment.

**`ImportError: libGL.so.1`.** `apt-get install libgl1 libglib2.0-0`. Not
`libgl1-mesa-glx`, which does not exist on bookworm.
