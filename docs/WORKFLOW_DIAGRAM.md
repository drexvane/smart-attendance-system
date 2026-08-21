# Workflow Diagrams

Rewritten for the current architecture. The previous version diagrammed a dlib/HOG pipeline
generating 128-d encodings; the system does not and never did work that way. Detection is the
res10 SSD DNN, recognition is LBPH, and there is now an authentication layer, an anti-proxy
confirmation stage, and Alembic migrations — all reflected below.

---

## 1. System architecture

```
┌───────────────┐   HTTPS    ┌──────────────────────────────────────────────┐
│    Browser    │ ─────────► │                 nginx (TLS)                    │
│  templates/   │ ◄───────── │  proxies /  →  gunicorn, buffering off on the  │
│  *_clean.html │            │  MJPEG feed (/get_video_feed)                  │
└───────────────┘            └───────────────────────┬────────────────────────┘
                                                      │  1 worker, gthread
                                                      ▼
                        ┌──────────────────────────────────────────────┐
                        │        gunicorn -c gunicorn.conf.py           │
                        │  ┌────────────────────────────────────────┐  │
                        │  │   create_app()  (app/__init__.py)       │  │
                        │  │   extensions: db, migrate, csrf,        │  │
                        │  │               limiter, login_manager    │  │
                        │  ├────────────────────────────────────────┤  │
                        │  │  blueprints/   HTTP: parse, 1 service   │  │
                        │  │      auth dashboard students attendance │  │
                        │  │      leave recognition api              │  │
                        │  ├────────────────────────────────────────┤  │
                        │  │  services/     business rules, txns,    │  │
                        │  │                aggregate SQL            │  │
                        │  ├────────────────────────────────────────┤  │
                        │  │  models/       schema + constraints     │  │
                        │  └───────────────────┬────────────────────┘  │
                        │                      │ SQLAlchemy             │
                        │  ┌───────────────────▼────────────────────┐  │
                        │  │  app/recognition/  (per-process singleton)│ │
                        │  │  CameraStream → Detector → Recognizer →  │  │
                        │  │  Pipeline (confirmation state machine)   │  │
                        │  └───────────────────┬────────────────────┘  │
                        └──────────────────────┼───────────────────────┘
                                   ┌───────────┴───────────┐
                                   ▼                       ▼
                        ┌──────────────────┐    ┌──────────────────────┐
                        │  SQLite / Postgres│    │  one webcam (V4L2)   │
                        │  instance/ or DB  │    │  CAMERA_INDEX        │
                        └──────────────────┘    └──────────────────────┘
                                   ▲
                        ┌──────────┴──────────┐   ┌──────────────────┐
                        │  Redis (rate limits)│   │  face_data/       │
                        │  RATELIMIT_STORAGE  │   │  lbph_model.yml + │
                        └─────────────────────┘   │  samples/<pk>/    │
                                                   └──────────────────┘
```

The camera and the recognition pipeline are a **per-process singleton**, which is why the
server runs exactly one worker. Concurrency comes from threads, not processes.

---

## 2. Student registration and face enrolment

```
Teacher/Admin (logged in)
        │  POST /register_student  (+ CSRF token)
        ▼
┌────────────────────┐   invalid    ┌───────────────────────────┐
│ validate_student_  │ ───────────► │ 400 with field errors      │
│ data()             │              │ (errors.py, content-negot.)│
└─────────┬──────────┘              └───────────────────────────┘
          │ valid
          ▼
┌────────────────────┐   no image / not an image
│ decode photo with  │ ─────────────────────────► reject upload
│ Pillow, cap dims + │
│ MAX_CONTENT_LENGTH │
└─────────┬──────────┘
          │ ok
          ▼
┌────────────────────┐   no face   ┌───────────────────────────┐
│ detector.detect_   │ ──────────► │ "No face detected. Use a   │
│ largest()          │             │  clear, front-facing photo"│
└─────────┬──────────┘             └───────────────────────────┘
          │ one face
          ▼
┌───────────────────────────────────────────────┐
│ crop (margin) → grayscale → equalise → 200×200 │
│ store  face_data/samples/<student_pk>/NNN.png  │
└─────────┬──────────────────────────────────────┘
          ▼
┌───────────────────────────────────────────────┐
│ FaceRecognizer.train(all samples)              │
│   persist lbph_model.yml + label_map.json      │
│   set Student.face_model_label, samples_count, │
│       face_enrolled_at                          │
└─────────┬──────────────────────────────────────┘
          ▼
      committed; student now recognisable
```

Storing samples (not one descriptor per row) is what lets `flask rebuild-faces` retrain from
scratch — important when a student is deleted, so no stale label keeps matching.

---

## 3. Recognition and attendance marking

The confirmation stage is the part the project claimed but did not have.

```
Start recognition (POST /start_face_recognition, CSRF)
        │
        ▼
CameraStream.acquire() ──► recognition thread runs the loop below per frame
        │
        ▼
┌──────────────────────┐
│ detector.detect()    │  res10 SSD (DNN), Haar fallback if models/ absent
└─────────┬────────────┘
          │ for each face box
          ▼
┌──────────────────────┐
│ crop_face()          │  → grayscale 200×200
└─────────┬────────────┘
          ▼
┌──────────────────────┐
│ recognizer.predict() │  LBPH distance per candidate (lower = better)
└─────────┬────────────┘
          ▼
   ┌──────┴───────────────────────────────────────────┐
   │ GUARD 1  distance > FACE_MATCH_MAX_DISTANCE (70)? │──yes──► Unknown (red box)
   └──────┬───────────────────────────────────────────┘
          │ no
          ▼
   ┌──────────────────────────────────────────────────┐
   │ GUARD 2  (runner_up − best) < MIN_MARGIN (8)?      │──yes──► ambiguous → reject
   └──────┬───────────────────────────────────────────┘
          │ no  →  matched student
          ▼
   ┌──────────────────────────────────────────────────┐
   │ GUARD 3  streak += 1 (reset if student left view) │
   │          streak < CONFIRM_FRAMES (5)?              │──yes──► amber box "3/5"
   └──────┬───────────────────────────────────────────┘
          │ no  →  confirmed (green box)
          ▼
   ┌──────────────────────────────────────────────────┐
   │ within COOLDOWN_SECONDS (60) of last confirm?      │──yes──► hold, do not re-publish
   └──────┬───────────────────────────────────────────┘
          │ no
          ▼
   publish to _pending  (pipeline never touches the DB)
          │
          ▼
Blueprint POST /auto_mark_attendance  →  attendance_service.mark()
          │
          ▼
   INSERT (student_id, date) ──IntegrityError?──► rollback, return existing row
          │ ok                                      (idempotent "already marked")
          ▼
   record written: status, confidence_score, marked_by="Face Recognition"
```

Guards 1 and 2 are enforced in the recognizer; guard 3 and the cooldown in the pipeline. The
result is that no single frame, and no ambiguous or low-confidence match, can write a record.

---

## 4. Request lifecycle (auth and CSRF ordering)

```
Request
   │
   ▼
┌───────────────────────┐   over limit   ┌──────────────┐
│ Flask-Limiter          │ ─────────────► │ 429          │
└─────────┬─────────────┘                └──────────────┘
          ▼
┌───────────────────────┐   POST w/o valid token   ┌──────────────────────────┐
│ CSRF check (Flask-WTF) │ ───────────────────────► │ 400 CSRFError handler     │
└─────────┬─────────────┘                          └──────────────────────────┘
          │  ordering matters: CSRF runs BEFORE login_required, so an
          │  unauthenticated POST surfaces as 400, not a login redirect
          ▼
┌───────────────────────┐   anonymous    ┌──────────────────────────┐
│ @login_required        │ ─────────────► │ 302 → /login?next=…       │
└─────────┬─────────────┘                │ (or 401 for JSON callers) │
          ▼                              └──────────────────────────┘
┌───────────────────────┐   wrong role   ┌──────────────┐
│ @roles_required(admin) │ ─────────────► │ 403           │
└─────────┬─────────────┘                └──────────────┘
          ▼
   blueprint → service → model → response
          │
          ▼ (any unhandled exception)
   errors.py: log traceback + correlation id server-side,
              return generic message (no str(exception) to client)
```

---

## 5. Database schema

```
┌──────────────────┐          ┌────────────────────────────────────┐
│ users            │          │ students                            │
├──────────────────┤          ├────────────────────────────────────┤
│ id PK            │          │ id PK                               │
│ username UQ      │          │ student_id UQ    name(ix)  email UQ │
│ email UQ         │          │ department(ix)  year(ix)  section   │
│ password_hash    │          │ image_path                          │
│ role             │          │ face_model_label UQ                 │
│ is_active        │          │ face_samples_count  face_enrolled_at│
│ last_login_at    │          │ is_active(ix)  created/updated_at   │
└──────────────────┘          └───────────────┬─────────────────────┘
   (no FK to the rest;                         │ 1
    operator accounts)                         │
                              ┌─────────────────┴───────┬───────────────────┐
                              │ N                        │ N                 │
                   ┌──────────▼───────────────┐  ┌──────▼──────────────────┐
                   │ attendance_records        │  │ leave_requests           │
                   ├──────────────────────────┤  ├─────────────────────────┤
                   │ id PK                     │  │ id PK                    │
                   │ student_id FK ─► ON DELETE│  │ student_id FK ─► CASCADE │
                   │             CASCADE (ix)  │  │ leave_type               │
                   │ date (ix)                 │  │ start_date  end_date     │
                   │ time_in  time_out         │  │ reason                   │
                   │ status (ix)               │  │ status (ix)              │
                   │ confidence_score          │  │ reviewed_by reviewed_at  │
                   │ marked_by                 │  │ review_notes             │
                   │ notes                     │  │ created/updated_at       │
                   ├──────────────────────────┤  └─────────────────────────┘
                   │ UNIQUE(student_id, date)  │◄── the idempotency constraint
                   │ INDEX(date, status)       │
                   └──────────────────────────┘

   attendance_sessions — optional class/period grouping, retained from the
   original schema, not FK-linked.
```

---

## 6. Migration sequence

```
(fresh DB)              (existing DB predating the refactor)
    │                              │
    │                    flask db stamp 0001_baseline
    ▼                              ▼
0001_baseline ──────► 0002_dedupe_attendance ──────► 0003_auth_and_constraints
 today's schema        collapse duplicate            users table;
 (for stamping)        (student_id, date),           UNIQUE(student_id,date) + indexes;
                       keep earliest                 marked_by; face_* columns;
                                                      NULL legacy face_encoding
```

`0002` must run before `0003`: the unique constraint cannot be created while duplicates
exist. `0003` nulls the legacy histogram encodings, so **students must be re-enrolled** after
upgrading an old database. `flask db check` gates model/migration drift in CI.

---

## 7. CI/CD pipeline

```
push / PR
    │
    ▼
ci.yml ── lint (ruff check + format) ─► test (3.11, 3.12: pytest, cov ≥ 75%) ─┐
              │ mypy app/                test-postgres (postgres:17-alpine:     │
              │                            db upgrade + db check)               │
              │                          security (bandit, pip-audit)           │
              └──────────────────────────────────────────────────────┬────────┘
                                                                       ▼
                                              build ── import smoke ── gunicorn boot
                                                       assert /healthz 200, / → 302
docker.yml ── build image ─► Trivy (HIGH/CRITICAL, gating) ─► run container,
                              assert /healthz + HEALTHCHECK ─► THEN push
cd.yml ── full gate ─► build artifact + image ─► DRY-RUN deploy (deploys nothing)

Every gate can fail. No continue-on-error / || true on a quality gate.
All actions pinned to commit SHAs.
```

See [DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md) for running each stage locally and
[FIXES_APPLIED.md](FIXES_APPLIED.md) for what these gates were hiding before.
