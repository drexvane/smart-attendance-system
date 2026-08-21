# Project Structure

The previous version of this file described `app.py`, `app_simple.py`, `app_minimal.py` and
a `src/` package. None of those exist any more: the three overlapping Flask applications
were replaced by one application factory, and `src/` moved into `app/`.

```
attendance-system-master/
│
├── wsgi.py                     # production entrypoint: app = create_app()
├── run.py                      # development launcher, binds 127.0.0.1
├── gunicorn.conf.py            # workers = 1 -- see its docstring
├── pyproject.toml              # metadata, deps, ruff / pytest / coverage / mypy / bandit
├── requirements.txt            # runtime pins, kept in step with pyproject
├── Dockerfile                  # two-stage build, venv copied into a slim runtime
│
├── app/
│   ├── __init__.py             # create_app(), blueprint/extension wiring, /healthz
│   ├── config.py               # Base / Development / Testing / Production
│   ├── extensions.py           # db, migrate, csrf, limiter, login_manager (unbound)
│   ├── errors.py               # content-negotiated 400/401/403/404/413/429/500
│   ├── cli.py                  # create-admin, create-user, list-users,
│   │                           #   deactivate-user, rebuild-faces, recognition-info
│   │
│   ├── models/                 # user.py, student.py, attendance.py, leave.py
│   ├── blueprints/             # auth, dashboard, students, attendance, leave,
│   │                           #   recognition, api  (HTTP only -- no business logic)
│   ├── services/               # student, attendance, leave, analytics, export
│   ├── recognition/            # camera, detector, recognizer, enrolment, pipeline
│   └── utils/                  # validators, security, images, logging, time
│
├── migrations/                 # Alembic, render_as_batch=True for SQLite
│   └── versions/
│       ├── 0001_baseline.py            # today's schema, for stamping old databases
│       ├── 0002_dedupe_attendance.py   # collapse duplicate (student_id, date)
│       └── 0003_auth_and_constraints.py# users, unique constraint, indexes, marked_by
│
├── tests/
│   ├── conftest.py             # app / client / authed_client / db fixtures
│   ├── unit/                   # validators, utils, recognizer, pipeline
│   └── integration/            # auth, students, attendance, leave, analytics,
│                               #   export, security (CSRF, headers, rate limits)
│
├── scripts/
│   └── download_models.py      # fetch + SHA-256 verify the res10 SSD detector
│
├── templates/                  # Jinja2; the *_clean.html set is what routes render
├── static/                     # css, js, images, swagger.yaml
├── models/                     # deploy.prototxt + res10 caffemodel (tracked in git)
│
├── instance/                   # SQLite database          ─┐
├── face_data/                  # LBPH model + samples      │ state: back these up,
├── student_images/             # uploaded photos           │ mount them in Docker
├── exports/                    # generated CSV / XLSX      │
└── logs/                       # rotating application log  ─┘
```

## Layering

Requests flow **blueprint → service → model**, and nothing skips a layer.

| Layer | Responsibility | Does not |
|---|---|---|
| `blueprints/` | Parse and validate input, call one service, render or serialise. | Contain query logic or business rules. |
| `services/` | Business rules, transactions, aggregate SQL. Return plain data or ORM objects. | Touch `request`, `session`, or return responses. |
| `models/` | Schema, constraints, relationships, small derived properties. | Perform I/O beyond the ORM. |

`app/recognition/` sits beside the services and is deliberately framework-free apart from
reading config: `camera.py` owns the single shared capture thread, `detector.py` wraps the
res10 DNN with a Haar fallback, `recognizer.py` wraps LBPH, `enrolment.py` builds training
crops, and `pipeline.py` holds the anti-proxy state machine (consecutive-frame
confirmation, runner-up margin, per-student cooldown).

## Conventions

* **No module-level mutable state.** The old `detection_active` / `face_recognition_active`
  globals reported one process's camera to another; the pipeline is now a per-process
  singleton reached through `app/recognition/__init__.py`.
* **UTC in the database, local for display.** Always `app/utils/time.py` — never
  `datetime.utcnow()` (deprecated in 3.12, and `DTZ` in ruff rejects it).
* **Errors go through `errors.py`.** No route returns `str(exception)` to a client.
* **New dependencies are pinned** in both `pyproject.toml` and `requirements.txt`.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the workflow and
[DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md) for running it.
