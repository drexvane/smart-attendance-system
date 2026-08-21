# Face Recognition Guide

This replaces a guide describing two backends — the `face_recognition`/dlib library as the
"advanced" system and OpenCV LBPH as a "fallback." Neither is how it works now. **Nothing
in the codebase imports dlib or `face_recognition`, and there is no automatic backend
selection.** There is one pipeline:

> **res10 SSD (DNN) detects faces → LBPH recognises them → a confirmation stage decides
> whether to mark.**

Both stages are `opencv-contrib-python-headless`, which is a wheel — no CMake, no compiler,
no `dlib`.

## Why it was rewritten

The old "face encoding" was `cv2.calcHist` over the grayscale crop: a 256-bin count of how
many pixels held each brightness. That descriptor throws away *where* the pixels are, so it
cannot tell two people apart — it measures overall brightness. Matching then accepted any
correlation above `1.0 - tolerance` (0.4 at the default), and auto-marking accepted 0.3,
*below* the recognition threshold. Two different students in the same room reliably matched.

LBPH builds a histogram per spatial cell (an 8×8 grid), so it encodes structure, not just
brightness. Two extra guards, described below, make a false match materially harder.

## What it does not do — read this before deploying

**No liveness / presentation-attack detection.** LBPH plus consecutive-frame confirmation
resists a student casually marking a friend present. It does **not** detect a printed
photo, a face on a phone screen, or a video replay. If attendance carries academic
consequences, keep a human in the loop and tell the people being recorded what the system
can and cannot verify.

LBPH is also not a modern deep face embedding. It is a large step up from an intensity
histogram and an honest mid-tier classical recogniser; it is not FaceNet. Liveness and a
stronger embedding are the top items on the roadmap, listed as gaps rather than features.

## The three anti-proxy guards

A candidate must clear all three before it becomes eligible for marking. They live in
`app/recognition/recognizer.py` and `pipeline.py`.

1. **Maximum distance** — LBPH returns a *distance* (lower is a better match). Above
   `FACE_MATCH_MAX_DISTANCE` (default 70) the face is reported **Unknown** rather than
   forced to its nearest neighbour. This is the check the old matcher lacked: it always
   took the nearest.
2. **Runner-up margin** — the best candidate must beat the second-best by at least
   `FACE_MATCH_MIN_MARGIN` (default 8). If two enrolled students score almost the same, the
   frame is rejected as ambiguous instead of guessed. (The recogniser reduces LBPH's
   per-*sample* results to a best-distance-per-*student* first, or the runner-up would
   usually just be another photo of the same person and the margin would never fire.)
3. **Consecutive-frame confirmation** — the same student must be identified in
   `RECOGNITION_CONFIRM_FRAMES` (default 5) **consecutive** processed frames. Any frame that
   loses them resets the streak, so an intermittent false positive can never accumulate to a
   mark. After confirmation, a per-student cooldown (`RECOGNITION_COOLDOWN_SECONDS`, default
   60) stops someone lingering in frame from churning the database.

The pipeline never writes to the database. It publishes confirmed candidates; the
recognition blueprint hands them to `attendance_service`, keeping the camera thread out of
the Flask/SQLAlchemy session entirely.

## One confidence scale

Distance is mapped to a `0..1` confidence (`1 - distance/max_distance`) purely so the UI has
one number to show. Auto-marking uses the **same** recognition threshold — there is
deliberately no separate, looser auto-mark cutoff. The old split (recognise at 0.4, mark at
0.3) is exactly the bug that let it write records it did not consider matches.

## Enrolment

Register a student, then attach a photo — upload a file or capture from the camera.

1. The DNN detector finds the **largest** face in the photo.
2. It is cropped with margin, converted to grayscale, and stored as a lossless PNG under
   `face_data/samples/<student_id>/NNN.png`.
3. The LBPH model is retrained from every stored sample and persisted to
   `face_data/lbph_model.yml`, with an OpenCV-label → student-id map in
   `face_data/label_map.json`.

Storing the *samples*, not one descriptor per row, is what lets the model be rebuilt from
scratch — which matters when a student is deleted, because a stale label left in the model
would let a removed student keep matching. Registering more than one photo per student, in
the lighting you actually use, improves recognition more than any threshold change.

### Good enrolment photos

Front-facing, evenly lit, one person, neutral expression, no heavy occlusion. Recognition
works best when the live conditions resemble the enrolment conditions, so enrol under the
lighting the camera will really see.

## Tuning

Both thresholds trade the two error types against each other, and the right values depend on
your camera and lighting — measure, don't guess. See the configuration table in
[DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md#configuration-reference); the short version:

| Symptom | Change |
|---|---|
| Known students reported Unknown | Raise `FACE_MATCH_MAX_DISTANCE` by ~5, or re-enrol with more/better samples |
| Wrong student matched | Lower `FACE_MATCH_MAX_DISTANCE`; raise `FACE_MATCH_MIN_MARGIN` |
| Marks for people who only walked past | Raise `RECOGNITION_CONFIRM_FRAMES` |
| Repeated marks for a lingering student | Raise `RECOGNITION_COOLDOWN_SECONDS` |

## Operating it

**In the UI** — open *Mark Attendance*, start the camera, start recognition. Boxes are drawn
per face: red **Unknown**, amber recognised-but-not-yet-confirmed (with the streak, e.g.
`3/5`), green **confirmed**. Auto-marking writes a record only for green faces.

**From the CLI:**

```bash
flask --app wsgi recognition-info    # detector backend, thresholds, enrolled count, trained?
flask --app wsgi rebuild-faces       # retrain LBPH from face_data/ samples (no camera needed)
```

Run `rebuild-faces` after bulk enrolment, after restoring `face_data/` from backup, or if
`lbph_model.yml` is deleted.

**HTTP endpoints** (all state-changing ones require login and a CSRF token; `fetch` sends
`X-CSRFToken`): `POST /start_detection`, `POST /start_face_recognition`,
`POST /stop_detection`, `POST /stop_face_recognition`, `GET /get_video_feed` (MJPEG),
`GET /get_detected_faces` (JSON the UI polls).

## The detector fallback

`app/recognition/detector.py` loads `models/deploy.prototxt` +
`res10_300x300_ssd_iter_140000.caffemodel` via `cv2.dnn.readNetFromCaffe`. Both files are
tracked in git. If either is missing it falls back to a Haar cascade automatically and
`recognition-info` reports `Detector backend: haar`. To restore the DNN:

```bash
python scripts/download_models.py    # verifies SHA-256 and exits non-zero on mismatch
```

## Multi-worker reality

A webcam is one physical device; it cannot be sharded across gunicorn workers. The pipeline
is a per-process singleton and the server runs one worker (see `gunicorn.conf.py`). The old
module-level `detection_active` / `face_recognition_active` flags had the same constraint but
reported one worker's camera state to another instead of acknowledging it.

## Troubleshooting

| Problem | Cause / fix |
|---|---|
| `ImportError: cv2.face` | Plain `opencv-python-headless` installed. Need `opencv-contrib-python-headless==4.14.0.94`; remove any stray `opencv-python`. |
| Everyone Unknown | Model never trained (`recognition-info` → `trained: False`, run `rebuild-faces`), or you upgraded an old DB whose legacy histograms were nulled by design — re-enrol. |
| `recognition-info` shows Haar | DNN model files missing; run `scripts/download_models.py`. |
| Black feed / camera error | Check `ls /dev/video*`, `CAMERA_INDEX`, the `video` group, and that nothing else holds the device. Docker needs `--device`; Docker Desktop on Windows/macOS has no camera passthrough. |
| No face detected on enrolment | Use a clear, front-facing, well-lit photo; the largest detectable face is used. |

See [DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md) for install and production, and
[STRUCTURE.md](STRUCTURE.md) for where each piece lives.
