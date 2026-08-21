"""Recognition pipeline.

Camera -> detect -> crop -> identify -> confirm.  The confirmation stage is the part
the project claimed to have and did not: previously a single frame in which the
histogram matcher returned any correlation above 0.3 was enough to write an attendance
record, so one bad frame marked the wrong student present and there was no way to
notice.

Three guards apply before a candidate becomes eligible for marking:

1. **Consecutive frames.** The same student must be identified in N consecutive
   processed frames (``RECOGNITION_CONFIRM_FRAMES``).  Any frame that loses them resets
   the streak.
2. **Margin.** Enforced inside :class:`~app.recognition.recognizer.FaceRecognizer` --
   an ambiguous frame counts as a miss and breaks the streak.
3. **Cooldown.** After a student is confirmed they are ignored for
   ``RECOGNITION_COOLDOWN_SECONDS``, so lingering in front of the camera cannot churn
   the database.

The pipeline never writes to the database.  It publishes confirmed candidates and the
blueprint hands them to ``attendance_service``, which keeps the camera thread out of the
Flask/SQLAlchemy session entirely.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

import numpy as np

from app.recognition.camera import CameraError, CameraStream, annotate, encode_jpeg
from app.recognition.detector import FaceBox, FaceDetector, crop_face
from app.recognition.recognizer import FaceRecognizer

logger = logging.getLogger(__name__)

_GREEN = (0, 200, 0)
_AMBER = (0, 170, 255)
_RED = (0, 0, 220)


@dataclass(frozen=True)
class KnownStudent:
    pk: int
    name: str
    roll: str


@dataclass
class DetectedFace:
    """What the UI polls for: one entry per face currently visible."""

    box: FaceBox
    student_pk: int | None
    name: str
    roll: str | None
    confidence: float
    streak: int
    confirmed: bool
    rejection: str | None = None

    def to_dict(self) -> dict:
        return {
            "student_id": self.student_pk,
            "student_roll": self.roll,
            "name": self.name,
            "confidence": round(self.confidence, 3),
            "location": self.box.as_list(),
            "detector_confidence": round(self.box.confidence, 3),
            "streak": self.streak,
            "confirmed": self.confirmed,
            "rejection": self.rejection,
        }


@dataclass
class _Candidate:
    streak: int = 0
    last_confirmed_at: float | None = None
    best_confidence: float = 0.0


@dataclass
class PipelineStats:
    frames_processed: int = 0
    faces_seen: int = 0
    matches: int = 0
    rejections: dict[str, int] = field(default_factory=dict)


class RecognitionPipeline:
    """Owns the recognition loop for this process.

    A webcam is a single physical device and cannot be shared across gunicorn workers,
    so this is deliberately a per-process singleton and the server is pinned to one
    worker.  (The old module-level ``detection_active`` / ``face_recognition_active``
    globals had the same constraint but reported stale state instead of acknowledging
    it.)
    """

    def __init__(
        self,
        stream: CameraStream,
        detector: FaceDetector,
        recognizer: FaceRecognizer,
        *,
        crop_size: int = 200,
        crop_margin: float = 0.15,
        confirm_frames: int = 5,
        cooldown_seconds: int = 60,
    ) -> None:
        self.stream = stream
        self.detector = detector
        self.recognizer = recognizer
        self.crop_size = crop_size
        self.crop_margin = crop_margin
        self.confirm_frames = max(1, confirm_frames)
        self.cooldown_seconds = max(0, cooldown_seconds)

        self._lock = threading.RLock()
        self._running = threading.Event()
        self._thread: threading.Thread | None = None
        self._known: dict[int, KnownStudent] = {}
        self._faces: list[DetectedFace] = []
        self._candidates: dict[int, _Candidate] = {}
        self._pending: dict[int, float] = {}  # student_pk -> confidence at confirmation
        self._preview_only = False
        self.stats = PipelineStats()

    # --- state ----------------------------------------------------------------
    @property
    def is_running(self) -> bool:
        return self._running.is_set()

    @property
    def is_recognizing(self) -> bool:
        return self._running.is_set() and not self._preview_only

    def status(self) -> dict:
        with self._lock:
            return {
                "running": self.is_running,
                "recognizing": self.is_recognizing,
                "preview_only": self._preview_only,
                "camera_index": self.stream.index,
                "camera_error": self.stream.last_error,
                "detector_backend": self.detector.backend,
                "enrolled_students": self.recognizer.enrolled_count,
                "known_loaded": len(self._known),
                "confirm_frames": self.confirm_frames,
                "cooldown_seconds": self.cooldown_seconds,
                "faces_visible": len(self._faces),
                "frames_processed": self.stats.frames_processed,
                "matches": self.stats.matches,
                "rejections": dict(self.stats.rejections),
            }

    # --- lifecycle ------------------------------------------------------------
    def load_known(self, students: list[KnownStudent]) -> None:
        with self._lock:
            self._known = {student.pk: student for student in students}
            self._candidates.clear()
        logger.info("Recognition pipeline knows %d students", len(students))

    def start(self, *, preview_only: bool = False) -> None:
        """Open the camera and begin processing. Idempotent."""
        with self._lock:
            if self._running.is_set():
                # Promoting preview to recognition does not need a restart.
                if not preview_only:
                    self._preview_only = False
                return

            self.stream.acquire()  # raises CameraError
            self._preview_only = preview_only
            self._faces = []
            self._candidates.clear()
            self._pending.clear()
            self.stats = PipelineStats()
            self._running.set()
            self._thread = threading.Thread(
                target=self._loop, name="recognition-pipeline", daemon=True
            )
            self._thread.start()
        logger.info("Recognition pipeline started (preview_only=%s)", preview_only)

    def stop(self) -> None:
        with self._lock:
            if not self._running.is_set():
                return
            self._running.clear()
            thread, self._thread = self._thread, None

        if thread is not None and thread.is_alive():
            thread.join(timeout=3)
            if thread.is_alive():
                logger.warning("Recognition thread did not stop within 3s")

        self.stream.release()
        with self._lock:
            self._faces = []
            self._candidates.clear()
        logger.info("Recognition pipeline stopped")

    # --- processing -----------------------------------------------------------
    def _loop(self) -> None:
        last_sequence = 0
        try:
            while self._running.is_set():
                frame = self.stream.wait_for_frame(after=last_sequence, timeout=1.0)
                if frame is None:
                    if not self.stream.is_running:
                        logger.error("Camera stopped; ending recognition loop")
                        break
                    continue
                last_sequence = frame.sequence

                if self._preview_only:
                    with self._lock:
                        self._faces = []
                        self.stats.frames_processed += 1
                    continue

                try:
                    self._process(frame.image)
                except Exception:  # noqa: BLE001 - a bad frame must not kill the loop
                    logger.exception("Error processing frame")
        finally:
            self._running.clear()
            logger.debug("Recognition loop exited")

    def _process(self, image: np.ndarray) -> None:
        boxes = self.detector.detect(image)
        now = time.monotonic()
        faces: list[DetectedFace] = []
        seen_this_frame: set[int] = set()

        for box in boxes:
            try:
                crop = crop_face(image, box, self.crop_size, self.crop_margin)
            except (ValueError, OSError) as exc:
                logger.debug("Could not crop face: %s", exc)
                continue

            match = self.recognizer.predict(crop)
            if not match.is_match or match.student_pk not in self._known:
                if match.rejection:
                    self.stats.rejections[match.rejection] = (
                        self.stats.rejections.get(match.rejection, 0) + 1
                    )
                faces.append(
                    DetectedFace(
                        box=box,
                        student_pk=None,
                        name="Unknown",
                        roll=None,
                        confidence=0.0,
                        streak=0,
                        confirmed=False,
                        rejection=match.rejection,
                    )
                )
                continue

            student = self._known[match.student_pk]
            seen_this_frame.add(student.pk)
            candidate = self._candidates.setdefault(student.pk, _Candidate())
            candidate.streak += 1
            candidate.best_confidence = max(candidate.best_confidence, match.confidence)

            confirmed = self._maybe_confirm(student.pk, candidate, now)
            faces.append(
                DetectedFace(
                    box=box,
                    student_pk=student.pk,
                    name=student.name,
                    roll=student.roll,
                    confidence=match.confidence,
                    streak=candidate.streak,
                    confirmed=confirmed,
                )
            )

        # Anyone who dropped out of view loses their streak: the confirmation window
        # must be *consecutive*, otherwise intermittent false positives accumulate.
        for pk in list(self._candidates):
            if pk not in seen_this_frame:
                self._candidates[pk].streak = 0

        with self._lock:
            self._faces = faces
            self.stats.frames_processed += 1
            self.stats.faces_seen += len(boxes)
            self.stats.matches += len(seen_this_frame)

    def _maybe_confirm(self, student_pk: int, candidate: _Candidate, now: float) -> bool:
        if candidate.streak < self.confirm_frames:
            return False
        if (
            candidate.last_confirmed_at is not None
            and now - candidate.last_confirmed_at < self.cooldown_seconds
        ):
            return True  # still confirmed, just not re-published
        candidate.last_confirmed_at = now
        candidate.streak = 0
        with self._lock:
            self._pending[student_pk] = candidate.best_confidence
        candidate.best_confidence = 0.0
        logger.info(
            "Recognition confirmed student pk=%s after %d frames", student_pk, self.confirm_frames
        )
        return True

    # --- consumption ----------------------------------------------------------
    def detected_faces(self) -> list[dict]:
        with self._lock:
            return [face.to_dict() for face in self._faces]

    def take_confirmed(self) -> dict[int, float]:
        """Return and clear the confirmed candidates awaiting a database write."""
        with self._lock:
            pending, self._pending = self._pending, {}
            return pending

    def annotated_frame(self) -> np.ndarray | None:
        frame = self.stream.latest()
        if frame is None:
            return None
        with self._lock:
            faces = list(self._faces)
            preview_only = self._preview_only

        if preview_only:
            return annotate(frame.image, [], footer="Preview - recognition off")

        labels = []
        for face in faces:
            if face.confirmed:
                colour, caption = _GREEN, f"{face.name} (confirmed)"
            elif face.student_pk is not None:
                colour = _AMBER
                caption = f"{face.name} {face.streak}/{self.confirm_frames}"
            else:
                colour, caption = _RED, "Unknown"
            labels.append((face.box.as_tuple(), caption, colour))

        footer = (
            f"Faces: {len(faces)} | Enrolled: {self.recognizer.enrolled_count} | "
            f"Detector: {self.detector.backend.upper()}"
        )
        return annotate(frame.image, labels, footer=footer)

    def mjpeg_frames(self, max_fps: int = 15):
        """Generator of multipart JPEG chunks for the live view.

        Bounded by ``max_fps`` and terminated by the camera stopping or the client
        disconnecting, unlike the old ``while detection_active`` loop which spun on a
        module-level flag and never noticed a closed connection.
        """
        interval = 1.0 / max(max_fps, 1)
        last_sequence = 0
        while self._running.is_set():
            frame = self.stream.wait_for_frame(after=last_sequence, timeout=2.0)
            if frame is None:
                if not self.stream.is_running:
                    return
                continue
            last_sequence = frame.sequence

            image = self.annotated_frame()
            if image is None:
                continue
            payload = encode_jpeg(image)
            if payload is None:
                continue
            yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + payload + b"\r\n"
            time.sleep(interval)


__all__ = [
    "CameraError",
    "DetectedFace",
    "KnownStudent",
    "RecognitionPipeline",
]
