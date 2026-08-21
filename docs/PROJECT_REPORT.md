# Smart Attendance System — Project Report

> This report was rewritten to match the system as it is actually built. The earlier version
> described a dlib / `face_recognition` pipeline producing 128-dimensional embeddings and
> claimed face recognition "eliminates" proxy attendance. Neither was true: the code never
> used dlib, and no classical recogniser eliminates proxy attendance. The claims below are
> written to match the implementation and its real limits.

## 1. Problem statement

Manual attendance in classrooms is slow, error-prone, hard to analyse, and easy to falsify
by proxy. The goal is a web application that lets a teacher take attendance from a camera
feed, keeps auditable records, and makes casual proxy marking harder — without overstating
what automated recognition can guarantee.

## 2. Objectives

**Primary**
- Assist attendance capture from a live camera, with a manual path always available.
- Reduce *casual* proxy marking through recognition plus a multi-frame confirmation stage.
- Keep correct, idempotent, queryable records with real access control.

**Secondary**
- A web interface for teachers (day-to-day) and admins (destructive actions, user management).
- Analytics and CSV/Excel export.
- A codebase and CI/CD pipeline that can be maintained and trusted (gates that actually fail).

**Explicit non-goal:** liveness / anti-spoofing. See §7.

## 3. Methodology

### 3.1 Architecture

An application-factory Flask backend, layered blueprint → service → model, with the
recognition pipeline as a per-process singleton beside the services.

```
Browser ─► nginx (TLS) ─► gunicorn (1 worker) ─► create_app()
                                                   ├─ blueprints/  (HTTP)
                                                   ├─ services/    (rules, txns, SQL)
                                                   ├─ models/      (schema)
                                                   └─ recognition/ (camera→detect→recognise→confirm)
                                                        │                 │
                                                   SQLite/Postgres     one webcam
```

The single worker is a deliberate constraint, not a limitation to fix: a webcam is one
physical device and cannot be shared across processes.

### 3.2 Recognition pipeline

1. **Detection** — res10 SSD via `cv2.dnn.readNetFromCaffe` (bundled Caffe model), with a
   Haar cascade as an automatic fallback if the model files are absent.
2. **Recognition** — `cv2.face.LBPHFaceRecognizer`. LBPH builds a histogram per cell of an
   8×8 grid, so it encodes facial structure (unlike the whole-image intensity histogram this
   replaced). It returns a distance; lower is a better match.
3. **Confirmation (anti-proxy)** — three guards before any record is written:
   a maximum distance (else *Unknown*), a runner-up margin (else *ambiguous → rejected*), and
   recognition in N consecutive frames, followed by a per-student cooldown.

### 3.3 Development approach

Layered separation (no business logic in blueprints, no `request`/`session` in services);
UTC storage with a configurable display timezone; migrations over `create_all()`; and a CI
pipeline where lint, types, tests, security, and a live boot are all gating.

## 4. Technology stack

| Concern | Choice |
|---|---|
| Language | Python 3.12 (3.11 floor) |
| Web | Flask (app factory), Flask-Login, Flask-Migrate, Flask-WTF (CSRF), Flask-Limiter |
| ORM / DB | SQLAlchemy 2; SQLite default, PostgreSQL supported (psycopg 3) |
| Migrations | Alembic (`render_as_batch=True`) |
| Vision | `opencv-contrib-python-headless` (DNN detector + LBPH), NumPy, Pillow |
| Server | gunicorn (`gthread`, one worker); nginx + TLS in front |
| Rate limiting | Flask-Limiter, Redis storage in production |
| Frontend | Jinja2 templates, Bootstrap, Chart.js |
| Quality | ruff, mypy, pytest + coverage, bandit, pip-audit, pre-commit |
| CI/CD | GitHub Actions (SHA-pinned), Trivy image scan, Dependabot |

**No dlib, no CMake, no compiler** for the vision stack — it is a wheel.

## 5. Implementation

### 5.1 Database design (current schema)

```sql
CREATE TABLE users (              -- operator accounts; the system had none before
    id INTEGER PRIMARY KEY,
    username     VARCHAR(64)  UNIQUE NOT NULL,
    email        VARCHAR(120) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,      -- werkzeug hash, never plaintext
    role         VARCHAR(20)  NOT NULL DEFAULT 'teacher',   -- admin | teacher
    is_active    BOOLEAN      NOT NULL DEFAULT 1,
    last_login_at DATETIME, created_at DATETIME, updated_at DATETIME
);

CREATE TABLE students (
    id INTEGER PRIMARY KEY,
    student_id VARCHAR(20) UNIQUE NOT NULL,
    name VARCHAR(100) NOT NULL, email VARCHAR(120) UNIQUE NOT NULL,
    phone VARCHAR(15), department VARCHAR(50), year VARCHAR(10), section VARCHAR(5),
    image_path VARCHAR(255),
    face_model_label   INTEGER UNIQUE,        -- LBPH label; model lives in face_data/
    face_samples_count INTEGER NOT NULL DEFAULT 0,
    face_enrolled_at   DATETIME,
    is_active BOOLEAN NOT NULL DEFAULT 1, created_at DATETIME, updated_at DATETIME
    -- the old face_encoding TEXT column (a grayscale histogram) is dropped
);

CREATE TABLE attendance_records (
    id INTEGER PRIMARY KEY,
    student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    date DATE NOT NULL, time_in DATETIME NOT NULL, time_out DATETIME,
    status VARCHAR(20) NOT NULL DEFAULT 'Present',
    confidence_score FLOAT, marked_by VARCHAR(64) NOT NULL DEFAULT 'System',
    notes VARCHAR(255), created_at DATETIME, updated_at DATETIME,
    CONSTRAINT uq_attendance_student_date UNIQUE (student_id, date)  -- idempotency
);

CREATE TABLE leave_requests ( ... student_id FK ON DELETE CASCADE, leave_type,
    start_date, end_date, reason, status, reviewed_by, reviewed_at, review_notes ... );
```

The `UNIQUE(student_id, date)` constraint is what makes concurrent marking safe: the service
inserts and catches the resulting `IntegrityError` instead of doing a racy read-then-write.

### 5.2 Recognition (representative, not the old dlib code)

```python
# recognizer.predict(): distance per candidate, then two guards
best_label, best_distance = ranked[0]
runner_up = ranked[1][1] if len(ranked) > 1 else None
if best_distance > self.max_distance:
    return Match(None, best_distance, 0.0, rejection="distance_above_threshold")   # Unknown
if runner_up is not None and (runner_up - best_distance) < self.min_margin:
    return Match(None, ..., rejection="ambiguous_match")                            # refuse
return Match(student_pk, best_distance, self._to_confidence(best_distance))
```

```python
# pipeline: consecutive-frame confirmation before a candidate is publishable
candidate.streak += 1
if candidate.streak < self.confirm_frames:      # default 5
    return False                                # not yet eligible
# ... cooldown check, then publish to _pending for the service to mark
```

### 5.3 Package structure

```
app/ __init__.py config.py extensions.py errors.py cli.py
     models/ blueprints/ services/ recognition/ utils/
migrations/versions/ 0001_baseline 0002_dedupe_attendance 0003_auth_and_constraints
wsgi.py run.py gunicorn.conf.py   tests/unit tests/integration
```

See [STRUCTURE.md](STRUCTURE.md).

## 6. Results

Functional, verified in development:

- Auth: anonymous access to mutating routes and exports is refused; roles enforced; first
  account created by `flask create-admin` (no default credentials).
- Marking is idempotent under repeats/concurrency (one row per student per day).
- Analytics endpoints are O(1) queries (were 1 + N).
- Recognition separates two different enrolled people that the old histogram matcher scored
  as the same (regression test).
- `/healthz` reports database reachability for load balancers and the container HEALTHCHECK.
- Quality suite: `ruff`, `mypy`, **272 tests at ~79% coverage** (75% floor), bandit and
  pip-audit clean; migrations apply with no drift.

## 7. Performance and accuracy — honestly

**No accuracy benchmark has been run in this environment, so no accuracy figure is claimed.**
The earlier report's "95–98%" numbers were not measured. Recognition accuracy for LBPH
depends heavily on enrolment quality, lighting, and camera; it should be measured on the
deployment's own data (see the tuning table in
[DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md#configuration-reference)).

What can be stated:
- LBPH with an 8×8 spatial grid is a large, structural improvement over an intensity
  histogram, which could not distinguish two faces at all.
- The confirmation stage means no single frame, and no ambiguous or below-threshold match,
  can write a record — the dominant cause of casual false marks in the old design.
- Throughput is bounded by one camera and one worker; threads handle web concurrency. This
  is sized for a single institution with one entry point, not a campus of cameras.

**Limitations, stated plainly:**
- **No liveness detection.** A printed photo or a face on a phone screen can pass. This is
  the top roadmap item and the reason a human should stay in the loop where attendance
  carries consequences.
- **LBPH is classical**, not a deep embedding; it is sensitive to pose and lighting.
- **Biometric data is stored unencrypted** on disk (`face_data/`, `student_images/`). Consent,
  retention, and deletion are operational responsibilities; the DPDP Act (India) and GDPR
  (special category) apply.

## 8. Challenges and solutions

| Challenge | Solution |
|---|---|
| The app could not start (undefined `rate_limit`, wrong CSRF call, missing column) | Rebuilt on an app factory; ruff `F821` now gates undefined names |
| A matcher that could not tell people apart | LBPH + distance + margin + consecutive-frame confirmation |
| Duplicate rows from read-then-write races | `UNIQUE(student_id, date)` + `IntegrityError`-catching service |
| Off-by-one-day records | UTC storage, configurable display timezone, one time helper |
| Analytics 1 + N queries | Aggregate SQL (`GROUP BY`, `func.sum(case(...))`) |
| Anonymous destructive access | Flask-Login, roles, CSRF, no default credentials |
| A green CI over a dead app | Every gate made able to fail; live boot + health check |
| Camera contention across workers | One shared capture thread; per-process singleton; one worker |

## 9. Future scope

- **Liveness / anti-spoofing** (blink or challenge–response, or a passive PAD model) — the
  most important gap.
- A **deep face embedding** (e.g. an ONNX ArcFace) behind the same recognizer interface.
- **Splitting the camera into its own service** so the web tier can scale horizontally.
- **Encryption at rest** for biometric samples and an **audit log** of who marked what.
- Tightening the Content-Security-Policy (currently needs `'unsafe-inline'`).

## 10. Conclusion

The system automates attendance capture from a camera, enforces access control, keeps
correct idempotent records, and raises the bar against casual proxy marking through
recognition plus multi-frame confirmation. It does not, and does not claim to, eliminate
proxy attendance: without liveness detection a determined spoof with a photo can still
succeed, which is why the manual path and human oversight remain first-class. The refactor's
larger contribution is a codebase and pipeline whose correctness can be verified rather than
asserted.

---

**Project Team**: [Your Name]  
**Institution**: [Your College/University]  
**Course**: [Course]  
**Date**: [Date]
