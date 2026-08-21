# 🎓 Smart Attendance System

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue.svg)](https://www.python.org/)

An open-source **face-recognition attendance system** built as a B.Tech major project:
Flask application factory, a service layer, Alembic migrations, real authentication, and a
detect-then-recognise camera pipeline with confirmation logic that makes a single-frame
false positive unable to write a record.

> Built the old-school way (solid fundamentals) with a future-facing mindset.

**What this README will not do is oversell it.** Read
[Accuracy and anti-proxy: what is and is not true](#-accuracy-and-anti-proxy-what-is-and-is-not-true)
before deploying this anywhere that matters. The short version: it recognises faces, it
resists casual proxy attendance, and it has **no liveness detection** — a printed photo
held up to the camera can still pass.

---

## 📌 Table of Contents

* [Overview](#-overview)
* [Features](#-features)
* [Tech Stack](#-tech-stack)
* [Project Structure](#️-project-structure)
* [Installation](#️-installation)
* [First Run](#-first-run)
* [Configuration](#-configuration)
* [Usage](#-usage)
* [Accuracy and anti-proxy](#-accuracy-and-anti-proxy-what-is-and-is-not-true)
* [Running with Docker](#-running-with-docker)
* [Development](#-development)
* [CI/CD](#-cicd)
* [Upgrading an existing database](#-upgrading-an-existing-database)
* [Security & Privacy](#-security--privacy)
* [Roadmap](#️-roadmap)
* [Contributing](#-contributing)
* [License](#-license)
* [Author](#-author)

---

## 🚀 Overview

Students enrol a few face samples. A camera stream is sampled, faces are located with an
SSD detector, each crop is matched against the enrolled set, and a match that survives
several consecutive frames marks that student present for the day — once, because the
database enforces one record per student per date.

Core goals:

* Accuracy over shortcuts
* Transparency over confusion — including about what the recognition can't do
* Open-source over gatekeeping

---

## ✨ Features

* ✅ Student registration with face enrolment (webcam capture or file upload)
* ✅ Real-time detection and recognition over an MJPEG stream
* ✅ Automatic marking behind consecutive-frame confirmation, a match-margin test and a
  per-student cooldown
* ✅ Unknown faces reported as unknown instead of matched to the nearest student
* ✅ Admin / teacher accounts, hashed passwords, role-restricted destructive actions
* ✅ Manual marking, bulk marking, and leave requests that feed back into attendance
* ✅ Analytics dashboard served by aggregate SQL (constant queries, not one per student)
* ✅ CSV / Excel export, streamed from memory and row-capped
* ✅ `/healthz` probe that actually touches the database

---

## 🧠 Tech Stack

**Backend** — Python 3.12 (3.11 supported), Flask 3, Flask-SQLAlchemy 3.1 / SQLAlchemy 2.0,
Flask-Migrate (Alembic), Flask-Login, Flask-WTF (CSRF), Flask-Limiter, gunicorn.

**Computer vision** — OpenCV **contrib** headless: `cv2.dnn` res10 SSD face detector plus
`cv2.face.LBPHFaceRecognizer`. NumPy, Pillow.

> There is no `dlib` and no `face_recognition` package. Earlier versions of this README
> devoted two sections to installing dlib; nothing in the codebase has ever imported it,
> and the contrib OpenCV wheel installs without a compiler on every supported platform.

**Frontend** — Jinja2 templates, vanilla HTML/CSS/JS (no build step).

**Database** — SQLite by default; PostgreSQL supported through `DATABASE_URL` and
exercised in CI.

---

## 🏗️ Project Structure

```
attendance-system/
├── app/
│   ├── __init__.py           create_app() factory, /healthz, error handlers
│   ├── config.py             Base / Development / Testing / Production
│   ├── extensions.py         db, migrate, csrf, limiter, login_manager
│   ├── errors.py             content-negotiated error responses
│   ├── cli.py                create-admin, create-user, rebuild-faces, ...
│   ├── models/               user, student, attendance, leave
│   ├── blueprints/           auth, dashboard, students, attendance, leave,
│   │                         recognition, api
│   ├── services/             student, attendance, leave, analytics, export
│   ├── recognition/          camera, detector, recognizer, enrolment, pipeline
│   └── utils/                validators, security, images, logging, time
├── migrations/               Alembic revisions (baseline → dedupe → auth)
├── models/                   res10 SSD detector files (tracked in git)
├── templates/  static/
├── tests/                    unit/ and integration/, plus conftest fixtures
├── scripts/download_models.py
├── wsgi.py                   app = create_app()          ← the entrypoint
├── run.py                    development convenience shim
├── gunicorn.conf.py          one worker, on purpose (see the file)
├── Dockerfile  .dockerignore
└── pyproject.toml            deps + ruff + pytest + coverage + mypy + bandit
```

---

## ⚙️ Installation

### Prerequisites

* Python **3.11 or 3.12**
* A webcam (only for enrolment and live recognition; the rest of the app runs without one)
* ~2 GB RAM free
* Linux additionally needs the OpenCV runtime libraries:
  `sudo apt-get install -y libgl1 libglib2.0-0`

### Clone and install

```bash
git clone https://github.com/your-username/attendance-system.git
cd attendance-system

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -e ".[dev]"          # runtime + test/lint tooling
# or, runtime only:
pip install -r requirements.txt
```

The detector's model files are tracked in `models/`, so a normal clone already has them.
If they are missing (shallow export, sparse checkout):

```bash
python scripts/download_models.py     # verifies SHA-256, exits non-zero if it can't
```

Without them the detector silently falls back to a Haar cascade, which is noticeably worse.

---

## 🏁 First Run

```bash
cp .env.example .env
python -c "import secrets; print(secrets.token_hex(32))"   # paste into SECRET_KEY

export FLASK_APP=wsgi.py                 # Windows: set FLASK_APP=wsgi.py
flask db upgrade                         # create/upgrade the schema
flask create-admin                       # prompts for username, email, password
flask run
```

Then open `http://localhost:5000` — it redirects to `/login`.

**There are no default credentials.** A fresh install has no usable account until
`flask create-admin` is run, and the password is prompted for rather than passed as an
argument so it does not land in shell history.

Other management commands:

| Command | Purpose |
| --- | --- |
| `flask create-admin` | Create the first admin account |
| `flask create-user --role teacher` | Add a teacher (or another admin) |
| `flask list-users` | List accounts and their roles |
| `flask deactivate-user <username>` | Disable an account without deleting its history |
| `flask rebuild-faces` | Retrain the LBPH model from the samples in `face_data/` |
| `flask recognition-info` | Report which detector/recognizer is actually loaded |

---

## 🔧 Configuration

Everything is environment-driven; see `.env.example` for the full list and
`app/config.py` for the defaults. The ones worth knowing:

| Variable | Default | Notes |
| --- | --- | --- |
| `SECRET_KEY` | random per boot in dev | **Required in production** — the app refuses to start without it |
| `FLASK_ENV` | `development` | `development` / `testing` / `production` |
| `DATABASE_URL` | `sqlite:///attendance.db` → `instance/attendance.db` | e.g. `postgresql+psycopg://user:pw@host/db` |
| `APP_TIMEZONE` | `UTC` | IANA name, e.g. `Asia/Kolkata`. Decides what "today" means |
| `LATE_THRESHOLD_TIME` | `09:30` | Marks after this are `Late` |
| `FACE_DETECTION_CONFIDENCE` | `0.6` | SSD detector threshold |
| `FACE_MATCH_MAX_DISTANCE` | `70.0` | LBPH **distance**; lower is a better match. Above this → unknown |
| `FACE_MATCH_MIN_MARGIN` | `8.0` | The runner-up must be this much worse, or the match is ambiguous and rejected |
| `RECOGNITION_CONFIRM_FRAMES` | `5` | Consecutive frames required before a student is eligible for marking |
| `RECOGNITION_COOLDOWN_SECONDS` | `60` | Per-student cooldown after a decision |
| `CAMERA_INDEX` | `0` | Which capture device to open |
| `EXPORT_MAX_ROWS` | `50000` | Hard cap on an export |
| `RATELIMIT_STORAGE_URI` | `memory://` | **Use Redis in production**; `memory://` is per-process |
| `LOG_LEVEL` | `INFO` | Rotating logs under `logs/` |

Timestamps are stored in UTC and rendered through `APP_TIMEZONE`. That is one clock, on
purpose: the previous version mixed UTC column defaults with local `date.today()` calls in
routes, which put late-evening marks on the wrong day.

---

## 🧪 Usage

### Automatic mode

1. Register a student and enrol several face samples (varied angles, even lighting).
2. Start the camera, then start recognition.
3. Stand in frame. Recognition has to agree with itself for
   `RECOGNITION_CONFIRM_FRAMES` consecutive frames, and beat the runner-up by
   `FACE_MATCH_MIN_MARGIN`, before anything is written.
4. The record is created once; a second attempt the same day is a no-op, enforced by a
   `UNIQUE (student_id, date)` constraint rather than by a read-then-write check.

### Manual mode

Mark a single student, mark a whole class absent, or record approved leave — which then
writes `On Leave` rather than leaving a gap that looks like truancy.

---

## 🎯 Accuracy and anti-proxy: what is and is not true

Being precise about this matters more than the feature list.

**What the pipeline does**

* Locates faces with the res10 SSD DNN detector (Haar cascade fallback).
* Matches each crop with LBPH against the enrolled samples.
* Rejects a match whose distance exceeds `FACE_MATCH_MAX_DISTANCE` → reported *Unknown*.
* Rejects a match that does not beat the runner-up by `FACE_MATCH_MIN_MARGIN` → ambiguous
  faces are not guessed at.
* Requires agreement across consecutive frames before marking, so one bad frame cannot
  write a record.
* Applies a per-student cooldown, so someone standing in frame cannot churn the database.

**What it does not do**

* **No liveness / presentation-attack detection.** A printed photograph or a face on a
  phone screen can pass. This is the honest limit of the current implementation and the
  top item on the roadmap.
* **No identity proof beyond the face.** No second factor, no device binding.
* Accuracy degrades with poor enrolment (one sample, one lighting condition), harsh
  backlighting, heavy occlusion, and low-resolution cameras. LBPH is a classical
  descriptor, not a deep embedding.

For a graded exam, treat the output as an aid to a human, not as an authority.

> For context on why the thresholds are set the way they are: the version this replaced
> used a 256-bin grayscale intensity histogram of the face crop as its "encoding". A
> histogram discards all spatial structure, so two different people under similar lighting
> correlate strongly — and auto-marking accepted a lower confidence than recognition
> itself. `tests/unit/test_recognizer.py` keeps a regression test that separates two faces
> the old scheme scored as a match.

---

## 🐳 Running with Docker

```bash
docker build -t attendance-system:local .

docker run --rm -p 5000:5000 \
  -e SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')" \
  -e FLASK_ENV=production \
  -e APP_TIMEZONE=Asia/Kolkata \
  -v "$PWD/instance:/app/instance" \
  -v "$PWD/face_data:/app/face_data" \
  -v "$PWD/student_images:/app/student_images" \
  -v "$PWD/logs:/app/logs" \
  attendance-system:local
```

Then `flask db upgrade` and `flask create-admin` inside the container
(`docker exec -it <id> flask db upgrade`).

Notes:

* The image runs **gunicorn**, not the Flask development server, as a non-root user.
* `HEALTHCHECK` polls `/healthz`, which executes `SELECT 1` — a healthy container has
  really reached its database.
* The four volumes above are state that must outlive the container.
* A webcam needs `--device /dev/video0` on Linux; on Docker Desktop for Windows/macOS
  there is no host camera passthrough, so run natively for camera work.
* `workers` stays at **1**. A webcam is one physical device and cannot be sharded across
  processes; `gunicorn.conf.py` explains it at length. Scale by adding hosts and moving
  the camera into its own service, not by raising `workers`.

---

## 💻 Development

```bash
pip install -e ".[dev]"
pre-commit install          # same ruff/mypy/bandit versions the pipeline uses

ruff check .                # lint
ruff format .               # format
mypy app/                   # types
pytest -q                   # tests (camera tests deselected by default)
pytest -q -m hardware       # camera tests, with a camera attached
pytest -q --cov=app         # coverage; floor is 75%, currently ~79%
```

`tests/conftest.py` provides `app`, `client`, `admin_client`, `db`, `students` and
`attendance_history` fixtures on in-memory SQLite. Point `TEST_DATABASE_URL` at a
PostgreSQL server to run the same suite there.

Tests use `app_today()` (the application clock) rather than `date.today()`. They are not
interchangeable, and mixing them produces failures that depend on the hour the suite runs.

After changing a model:

```bash
flask db migrate -m "what changed"
flask db upgrade
flask db check      # must report no drift between models and migrations
```

---

## 🔄 CI/CD

Five workflows, and **every gate in them can fail the build** — which was the point of the
last overhaul. The previous pipeline carried `continue-on-error: true` or `|| true` on
every quality step, including the smoke test, and reported green over an application that
raised `NameError` on import.

| Workflow | What it gates on |
| --- | --- |
| `ci.yml` | `ruff check`, `ruff format --check`, `mypy`, pytest on 3.11 + 3.12 with a coverage floor, the same suite against PostgreSQL, `flask db upgrade` + `db check` on an empty database, `bandit`, `pip-audit`, and a real smoke test that boots gunicorn and asserts `/healthz` |
| `docker.yml` | Image builds, Trivy scan on HIGH/CRITICAL, container serves `/healthz` and its `HEALTHCHECK` goes healthy — **then** it is pushed |
| `cd.yml` | Lint + types + tests before an artifact exists. Deployment steps are labelled dry runs, because that is what they are |
| `codeql.yml` | CodeQL `security-extended` |
| `performance.yml`, `code-review.yml` | Reporting only — PR comments, allowed to fail |

Every action is pinned to a commit SHA (a mutable `@v4` tag means whoever controls the tag
controls what runs against this repository), and Dependabot keeps those SHAs current.

---

## 🔁 Upgrading an existing database

If you have an `instance/attendance.db` from before the 2.0 refactor, its schema matches
revision `0001_baseline`, so tell Alembic that before upgrading:

```bash
cp instance/attendance.db instance/attendance.db.backup   # do this first
flask db stamp 0001_baseline
flask db upgrade
```

`0002_dedupe_attendance` collapses each `(student_id, date)` group to its earliest record —
the unique constraint cannot be added while duplicates exist — and `0003_auth_and_constraints`
adds the `users` table, the constraint and its indexes, `attendance_records.marked_by`, and
the face-enrolment columns.

It also **nulls out the legacy `students.face_encoding` values**: those are the old
intensity histograms and mean nothing to the LBPH recognizer. Students must be re-enrolled
(`flask rebuild-faces` retrains from any samples already in `face_data/`). Attendance
history is untouched.

---

## 🔐 Security & Privacy

Implemented:

* Session authentication, password hashing (Werkzeug PBKDF2), `admin` / `teacher` roles,
  and role checks on destructive actions such as permanent deletion.
* `@login_required` on every mutating route, both export routes and the student-PII APIs.
* CSRF protection on all state-changing requests, including the recognition control
  endpoints — those used to be `@csrf_exempt`. Browser JS sends the token as
  `X-CSRFToken`.
* Rate limiting via Flask-Limiter (use Redis in production; `memory://` is per-process).
* `HttpOnly` / `SameSite` / `Secure` session cookies, security response headers,
  `MAX_CONTENT_LENGTH`, and upload validation that decodes the bytes with Pillow and caps
  dimensions rather than trusting the extension.
* Error responses carry a correlation id; tracebacks go to the log, not to the client.
* Biometric data stays local — face samples in `face_data/`, no cloud service, no
  third-party API.

Known limitations, stated rather than buried:

* **No liveness detection** (see [above](#-accuracy-and-anti-proxy-what-is-and-is-not-true)).
* The Content-Security-Policy still allows `'unsafe-inline'` for styles and scripts,
  because the templates carry inline handlers and `<style>` blocks. Removing it is a
  frontend refactor, not a header change.
* Face samples and the LBPH model are stored unencrypted on disk. Protect the volume.
* No audit log of who changed which record beyond `marked_by`.

⚠️ **Ethical note**: biometric attendance needs informed consent, a retention policy, and
a way for a student to be removed. Deploy accordingly, and check your local data-protection
law first — in India, the DPDP Act treats this as personal data.

Found a vulnerability? Don't open a public issue — see `SECURITY.md`.

---

## 🛣️ Roadmap

* 🥇 **Liveness detection** (blink/motion challenge, or a passive anti-spoofing model) —
  the one gap that most limits how much this can be trusted
* 🧠 Deep face embeddings (ArcFace/FaceNet via ONNX) as an alternative to LBPH
* 🎥 Split the camera into its own service so the web tier can run more than one worker
* 📷 Multi-camera support
* 🧾 Full audit log
* 📱 Mobile / kiosk client
* 🎨 Template and CSS consolidation — `templates/` still holds three parallel design
  systems; only the `*_clean.html` set is reachable

---

## 🤝 Contributing

Contributions are welcome.

1. Fork and branch.
2. `pip install -e ".[dev]" && pre-commit install`.
3. Make the change, with a test that would fail without it.
4. `ruff check . && ruff format --check . && mypy app/ && pytest -q`.
5. Open a PR — CI runs the same gates, and they are allowed to fail.

Read `CONTRIBUTING.md`. This project follows the **Contributor Covenant**
(`CODE_OF_CONDUCT.md`): be respectful, no harassment, build — don't break people.

---

## 📄 License

MIT — see [LICENSE](LICENSE). Use it, modify it, distribute it. Just give credit where
it's due.

---

## 👨‍💻 Author

**Kunal Poonia** — B.Tech, open-source contributor and builder.

---

## 🌟 Final Note

This repo isn't just a project.

It's proof that fundamentals still matter — including the unglamorous ones: a pipeline
whose gates can fail, a schema that enforces its own invariants, and a README that says
what the code actually does.

Build it. Break it. Improve it. 🚀
