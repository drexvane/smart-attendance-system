# Fixes Applied

The previous version of this file (dated October 10, 2025) recorded a numpy
array-truth-value fix in `face_recognition/face_encoder.py` and a fixed-layout CSS pass.
Both refer to files the backend rewrite deleted. This file now records the correctness
fixes that rewrite carried, with the before/after that motivated each.

Dated: 2026-08-21. Branch: `refactor/backend-and-pipeline`.

---

## 1. Three crashes that stopped the app from starting

`app.py` could not be imported.

| Bug | Before | After |
|---|---|---|
| Undefined decorator | `@rate_limit(...)` on six routes; `rate_limit` defined nowhere → `NameError` at import | `@limiter.limit(...)` from a properly initialised Flask-Limiter |
| Wrong CSRF call | `csrf.generate_csrf()` — not a method on `CSRFProtect` → `AttributeError` in every template | module-level `from flask_wtf.csrf import generate_csrf` |
| Missing column | `AttendanceRecord(marked_by=...)` against a column that did not exist | `marked_by` added to the model (migration `0003`) |

Ruff's `F821` (undefined name) now gates CI, so a missing decorator fails the build instead
of shipping.

## 2. Recognition that could not recognise

**Before:** a "face encoding" was a 256-bin grayscale intensity histogram (`cv2.calcHist`
over the crop). It discarded all spatial structure, so two different people under similar
lighting correlated highly. Matching accepted correlation `> 0.4`; auto-marking accepted
`> 0.3` — *below* the recognition threshold, so it wrote records it did not consider matches.

**After:** `cv2.face.LBPHFaceRecognizer` (8×8 spatial grid) with three guards — a maximum
distance, a runner-up margin, and N-consecutive-frame confirmation — plus a per-student
cooldown. Auto-marking shares the recognition threshold; there is no looser second cutoff.
A regression test scores two different enrolled people and asserts they no longer collide.
Details in [README_FACE_RECOGNITION.md](README_FACE_RECOGNITION.md).

## 3. Duplicate attendance under concurrency

**Before:** every marking path did `SELECT` then `INSERT`. Two concurrent requests — or the
recognition loop firing twice — both saw no row and both inserted one.

**After:** `UniqueConstraint("student_id", "date")` plus an index on `(date, status)`.
`attendance_service.mark()` inserts and catches `IntegrityError`, rolling back and returning
the existing row, so marking is idempotent. Migration `0002` de-duplicates existing rows
(keeping the earliest) before `0003` adds the constraint, because it cannot apply over
duplicates.

## 4. Off-by-one-day records

**Before:** model defaults used `datetime.utcnow()` while routes used local `date.today()`,
so an evening mark could land on the wrong day, and `utcnow()` is deprecated in 3.12.

**After:** one `app/utils/time.py`. Timestamps stored as timezone-aware UTC; "today" and all
rendering resolved through a configurable `APP_TIMEZONE`. Ruff's `DTZ` rules ban naive
datetimes.

## 5. Analytics N+1

**Before:** `/api/analytics/top_students` issued 1 + N queries (501 for 500 students); other
endpoints looped per-day or per-department.

**After:** aggregate SQL — `GROUP BY` with `func.sum(case(...))`. `top_students` and
`at_risk` are O(1) queries; `trend` is one query with absent dates filled in Python. An
integration test asserts the query count with a counter.

## 6. Internal errors leaked to clients

**Before:** ~40 copies of `except Exception as e: return jsonify({'error': str(e)}), 500`
returned stack-trace text to callers, and a blanket `@app.errorhandler(400)` hijacked every
400 to sniff for CSRF.

**After:** `app/errors.py` — content-negotiated 400/401/403/404/413/429/500 handlers that log
the traceback server-side and return a generic message with a correlation id. A real
`CSRFError` handler replaces the 400 hijack.

## 7. No authentication at all

**Before:** anonymous visitors could delete students, rewrite attendance, and export the full
roster. The state-changing recognition endpoints were `@csrf_exempt` POSTs.

**After:** a `User` model (hashed passwords, `admin`/`teacher` roles), Flask-Login,
`@login_required` on every mutating route and both exports, `@roles_required("admin")` on
destructive routes and user management. CSRF is enforced on the recognition endpoints (the JS
sends `X-CSRFToken`). **No default credentials** — `flask create-admin` bootstraps the first
account and a fresh install has no usable login until it is run. `ProductionConfig.validate()`
refuses to boot without `SECRET_KEY`; the old `or "production-secret-key"` fallback is gone.

## 8. Uploads accepted anything

**Before:** `save_uploaded_file` wrote any bytes to a `.jpg`; the base64 capture path decoded
arbitrary bytes to disk.

**After:** uploads are decoded with Pillow to confirm they are images, capped by dimension and
`MAX_CONTENT_LENGTH` (16 MB), and restricted by extension.

## 9. The camera fought itself

**Before:** `SimpleCamera` and `FaceDetector` each opened camera index 0 independently, and a
fallback tried indices `[1, 2, 0]`, so it could end up on a different camera than requested.
Module-level `detection_active` / `face_recognition_active` globals reported one worker's
state to another under `workers > 1`.

**After:** one shared, reference-counted `CameraStream` with a condition variable (the MJPEG
generator waits for a new frame and terminates on client disconnect). The pipeline is a
per-process singleton; `gunicorn.conf.py` pins one worker and documents why.

## 10. A pipeline that could not fail

**Before:** every gate in `ci.yml` carried `continue-on-error: true` and/or `|| true`,
including the build job's own import smoke test. Green over a dead app. `docker.yml` was
`continue-on-error: true` and hid a `libgl1-mesa-glx` package that does not exist on
bookworm. `code-review.yml` ran on `pull_request_target` with an untrusted checkout and
interpolated PR filenames into a shell — command injection in a privileged context. `cd.yml`
reported successful production deploys that were `echo` statements.

**After:** every quality gate can fail. `ci.yml` gates lint, format, types, tests on 3.11 +
3.12 with a coverage floor, the same suite against PostgreSQL, migration drift, bandit,
pip-audit, and a real gunicorn boot asserting `/healthz`. `docker.yml` builds, Trivy-scans,
and health-checks the container before any push. `code-review.yml` moved to `pull_request`
with env-passed, `xargs -0`-fed filenames. `cd.yml`'s deploy steps are explicit dry runs. All
actions are pinned to commit SHAs. See [DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md#cicd).

---

## Verified

`ruff check`, `ruff format --check`, `mypy app/`, 272 tests at 79% coverage (75% floor),
bandit and pip-audit clean, migrations apply to a fresh DB with no drift, `/healthz` returns
200, unauthenticated mutating requests are refused.

**Not verified in the dev environment:** Docker (not installed here), the gunicorn boot step
(gunicorn does not run on Windows), and the PostgreSQL CI leg. These run in CI on Linux.
