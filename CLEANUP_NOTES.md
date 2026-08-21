# Cleanup Notes

The previous version of this file recorded the removal of six duplicate face-recognition
modules from `src/core/`. That cleanup is long superseded: the whole `src/` tree, and the
three Flask apps that imported it, no longer exist. This file now records what the
backend-and-pipeline refactor removed and consolidated.

## Three Flask apps collapsed into one factory

`app.py`, `app_simple.py`, and `app_minimal.py` — ~3,400 lines of overlapping,
diverging routes — are gone. In their place is a single application factory, `create_app()`
in `app/__init__.py`, entered through `wsgi.py` (production) and `run.py` (development).
Routes are split across blueprints, business logic into services, persistence into models.
See [STRUCTURE.md](docs/STRUCTURE.md).

`app.py` could not actually start: it referenced a `rate_limit` decorator defined nowhere
(`NameError` at import), called `csrf.generate_csrf()` which is not a method on
`CSRFProtect`, and constructed `AttendanceRecord(marked_by=...)` against a column the model
did not have. Those were not fixed in place; the module was replaced.

## `src/` folded into `app/`

| Old | New |
|---|---|
| `src/core/simple_camera.py` (+ the removed duplicates) | `app/recognition/camera.py` |
| `src/face_recognition/face_detector.py` | `app/recognition/detector.py` |
| `src/face_recognition/face_encoder.py` (histogram matcher) | `app/recognition/recognizer.py` (LBPH) |
| `src/database/models.py` | `app/models/` (one module per model) |
| `src/utils/helpers.py` | `app/utils/` + `app/services/` |

`get_attendance_status()` in the old helpers was a stub that returned the literal
`"Unknown"` and was never called — dropped rather than ported.

## The histogram "encoding" removed

The old descriptor was a 256-bin grayscale intensity histogram of the face crop. It carried
no spatial information and matched near-anything under similar lighting. The `face_encoding`
column that stored it is nulled and abandoned by migration `0003`; recognition is now LBPH
with a trained model in `face_data/`. Existing databases must re-enrol students.

## Scripts pruned

Eleven scripts were removed, keeping only `scripts/download_models.py` (which was rewritten
to verify SHA-256 digests). The deleted ones either imported modules that no longer exist,
or instructed users to `python app.py` / `python app_simple.py` / `pip install dlib`:

`capture_and_train.py`, `debug_recognition.py`, `quick_test.py`, `check_students.py`,
`setup.py`, `setup_face_recognition.py`, `install_requirements.py`,
`install_enhanced_requirements.py`, `migrate_db.py`, `migrate_leave_management.py`,
`migrate_to_enhanced.py`. Database migrations are now Alembic revisions under `migrations/`.

## Build artifacts un-tracked

`__pycache__/*.pyc`, `attendance_system.log`, `logs/app.log`, and the SQLite databases under
`instance/` were committed before `.gitignore` covered them. They were removed from the index
(`git rm --cached`) and left on disk. `models/*.caffemodel` **stays tracked** on purpose — the
DNN detector needs it in a fresh clone.

## Still deliberately not done

Template and CSS consolidation. `templates/` holds three parallel design systems and
`static/css` many overlapping stylesheets; only the `*_clean.html` set is reachable from
routes. That is frontend work for a separate pass. [LAYOUT_GUIDE.md](docs/LAYOUT_GUIDE.md)
still describes the old non-`_clean` templates and has not been updated for that reason.
