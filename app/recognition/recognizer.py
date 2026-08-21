"""Face recognition.

What was here before: a "face encoding" was ``cv2.calcHist`` over the grayscale face
crop -- a 256-bin count of how many pixels held each intensity.  That descriptor throws
away *where* the pixels are, so it cannot distinguish two people; it only measures
overall brightness distribution.  Matching then accepted any correlation above
``1.0 - tolerance`` (0.4 at the default tolerance of 0.6), and auto-marking accepted
0.3, below the recognition threshold itself.  Two different students photographed in the
same room reliably matched.

This module uses LBPH (Local Binary Patterns Histograms) instead, which builds
histograms per spatial cell and therefore does encode facial structure.  Two additional
guards make a false match much harder:

* a maximum distance, above which the face is reported Unknown rather than forced to
  the nearest neighbour, and
* a minimum margin between the best and second-best candidate, so an ambiguous frame is
  rejected instead of resolved arbitrarily.

LBPH is a large step up from an intensity histogram.  It is not a modern deep embedding
and it is not liveness detection -- see the README for what this does and does not
defend against.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

logger = logging.getLogger(__name__)

MIN_SAMPLES_PER_STUDENT = 1


@dataclass(frozen=True)
class Match:
    """A recognition result. ``student_pk`` is ``None`` when nothing matched."""

    student_pk: int | None
    distance: float
    confidence: float
    runner_up_distance: float | None = None
    rejection: str | None = None

    @property
    def is_match(self) -> bool:
        return self.student_pk is not None


class FaceRecognizer:
    """LBPH recognizer with on-disk persistence.

    Thread-safe: the recognition loop reads while a registration request may retrain.
    """

    def __init__(
        self,
        model_path: str | Path,
        label_map_path: str | Path,
        max_distance: float = 70.0,
        min_margin: float = 8.0,
    ) -> None:
        self.model_path = Path(model_path)
        self.label_map_path = Path(label_map_path)
        self.max_distance = max_distance
        self.min_margin = min_margin

        self._lock = threading.RLock()
        self._model: cv2.face.LBPHFaceRecognizer | None = None
        # label (int used by OpenCV) -> student primary key
        self._labels: dict[int, int] = {}
        self._trained = False

    # --- state ----------------------------------------------------------------
    @property
    def is_trained(self) -> bool:
        with self._lock:
            return self._trained

    @property
    def enrolled_count(self) -> int:
        with self._lock:
            return len(self._labels)

    @property
    def label_map(self) -> dict[int, int]:
        """``{opencv_label: student_pk}`` for the currently trained model."""
        with self._lock:
            return dict(self._labels)

    @property
    def student_labels(self) -> dict[int, int]:
        """``{student_pk: opencv_label}`` -- the inverse, for persisting onto rows."""
        with self._lock:
            return {pk: label for label, pk in self._labels.items()}

    def _new_model(self) -> cv2.face.LBPHFaceRecognizer:
        # radius/neighbors/grid are the OpenCV defaults; grid_x*grid_y cells is what
        # gives the descriptor its spatial awareness, which is exactly what the old
        # whole-image histogram lacked.
        return cv2.face.LBPHFaceRecognizer_create(  # type: ignore[attr-defined]
            radius=1, neighbors=8, grid_x=8, grid_y=8
        )

    # --- training -------------------------------------------------------------
    def train(self, samples: dict[int, list[np.ndarray]]) -> int:
        """Train from scratch on ``{student_pk: [grayscale crops]}``.

        Returns the number of students enrolled.  A full retrain (rather than
        ``update``) keeps the label map and the model file consistent after a student
        is deleted.
        """
        images: list[np.ndarray] = []
        labels: list[int] = []
        label_map: dict[int, int] = {}

        for label, (student_pk, crops) in enumerate(sorted(samples.items())):
            usable = [crop for crop in crops if crop is not None and crop.size]
            if len(usable) < MIN_SAMPLES_PER_STUDENT:
                logger.warning("Student %s has no usable face samples; skipped", student_pk)
                continue
            label_map[label] = student_pk
            for crop in usable:
                images.append(crop)
                labels.append(label)

        with self._lock:
            if not images:
                self._model = None
                self._labels = {}
                self._trained = False
                self._delete_persisted()
                logger.info("Face recognizer reset: no enrolled students")
                return 0

            model = self._new_model()
            model.train(images, np.array(labels, dtype=np.int32))
            self._model = model
            self._labels = label_map
            self._trained = True
            self._persist()

        logger.info(
            "Face recognizer trained on %d students / %d samples", len(label_map), len(images)
        )
        return len(label_map)

    # --- prediction -----------------------------------------------------------
    def predict(self, crop: np.ndarray) -> Match:
        """Identify a normalised grayscale face crop.

        LBPH returns a distance where lower is better, so the two nearest candidates
        are compared to enforce the margin check.
        """
        with self._lock:
            model, labels = self._model, dict(self._labels)

        if model is None or not labels:
            return Match(None, float("inf"), 0.0, rejection="no_enrolled_faces")
        if crop is None or crop.size == 0:
            return Match(None, float("inf"), 0.0, rejection="empty_crop")

        # collect() gives every candidate rather than only the winner, which is what
        # the margin test needs.  Note it returns one entry per *training sample*, so
        # the results have to be reduced to a best distance per label before the
        # runner-up means anything -- otherwise the second-best entry is usually just
        # another photo of the same person and the margin check never fires.
        collector = cv2.face.StandardCollector_create()  # type: ignore[attr-defined]
        with self._lock:
            model.predict_collect(crop, collector)
        results = collector.getResults()

        if not results:
            return Match(None, float("inf"), 0.0, rejection="no_prediction")

        best_per_label: dict[int, float] = {}
        for label, distance in results:
            label = int(label)
            if distance < best_per_label.get(label, float("inf")):
                best_per_label[label] = float(distance)

        ranked = sorted(best_per_label.items(), key=lambda item: item[1])
        best_label, best_distance = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else None

        if best_distance > self.max_distance:
            return Match(
                None,
                best_distance,
                0.0,
                runner_up_distance=runner_up,
                rejection="distance_above_threshold",
            )

        if runner_up is not None and (runner_up - best_distance) < self.min_margin:
            # Two enrolled students score almost identically: refuse rather than guess.
            return Match(
                None,
                best_distance,
                self._to_confidence(best_distance),
                runner_up_distance=runner_up,
                rejection="ambiguous_match",
            )

        student_pk = labels.get(int(best_label))
        if student_pk is None:
            return Match(None, best_distance, 0.0, rejection="unknown_label")

        return Match(
            student_pk,
            best_distance,
            self._to_confidence(best_distance),
            runner_up_distance=runner_up,
        )

    def _to_confidence(self, distance: float) -> float:
        """Map an LBPH distance onto 0..1 so the UI has one consistent scale."""
        if self.max_distance <= 0:
            return 0.0
        return round(max(0.0, min(1.0, 1.0 - distance / self.max_distance)), 4)

    # --- persistence ----------------------------------------------------------
    def _persist(self) -> None:
        model = self._model
        if model is None:
            logger.debug("Nothing to persist: no trained model in memory")
            return
        try:
            self.model_path.parent.mkdir(parents=True, exist_ok=True)
            model.write(str(self.model_path))
            self.label_map_path.write_text(
                json.dumps({str(k): v for k, v in self._labels.items()}, indent=2),
                encoding="utf-8",
            )
        except (OSError, cv2.error) as exc:
            logger.error("Could not persist face model: %s", exc)

    def _delete_persisted(self) -> None:
        for path in (self.model_path, self.label_map_path):
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("Could not remove %s: %s", path, exc)

    def load(self) -> bool:
        """Restore a previously trained model. Returns whether one was loaded."""
        if not self.model_path.is_file() or not self.label_map_path.is_file():
            return False
        try:
            raw = json.loads(self.label_map_path.read_text(encoding="utf-8"))
            labels = {int(key): int(value) for key, value in raw.items()}
            model = self._new_model()
            model.read(str(self.model_path))
        except (OSError, ValueError, cv2.error) as exc:
            logger.error("Could not load persisted face model: %s", exc)
            return False

        with self._lock:
            self._model = model
            self._labels = labels
            self._trained = True
        logger.info("Loaded face model with %d enrolled students", len(labels))
        return True
