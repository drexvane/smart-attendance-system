"""Face detection.

The repository already tracks ``models/deploy.prototxt`` and
``models/res10_300x300_ssd_iter_140000.caffemodel`` -- a ResNet-10 SSD face detector --
and the old code never loaded them, using a Haar cascade instead.  The DNN detector is
markedly better on off-angle and poorly lit faces, so it is now the default and the
cascade is the fallback for installs where the weights are missing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# The res10 network was trained with these BGR channel means at 300x300.
_DNN_INPUT_SIZE = (300, 300)
_DNN_MEAN = (104.0, 177.0, 123.0)


@dataclass(frozen=True)
class FaceBox:
    """A detected face in pixel coordinates, with the detector's own confidence."""

    x: int
    y: int
    width: int
    height: int
    confidence: float

    @property
    def area(self) -> int:
        return self.width * self.height

    def as_list(self) -> list[int]:
        return [self.x, self.y, self.width, self.height]

    def as_tuple(self) -> tuple[int, int, int, int]:
        """Fixed-length form, for callers that need the shape and not just the values."""
        return (self.x, self.y, self.width, self.height)

    def clipped_to(self, frame_width: int, frame_height: int) -> FaceBox:
        x = max(0, min(self.x, frame_width - 1))
        y = max(0, min(self.y, frame_height - 1))
        width = max(1, min(self.width, frame_width - x))
        height = max(1, min(self.height, frame_height - y))
        return FaceBox(x, y, width, height, self.confidence)


class FaceDetector:
    """Detects faces in a BGR frame.

    Chooses the DNN backend when the model files are present, otherwise falls back to
    a Haar cascade.  ``backend`` reports which one is live so the UI and logs can say
    so honestly instead of implying DNN accuracy that is not there.
    """

    def __init__(
        self,
        proto_path: str | Path,
        weights_path: str | Path,
        min_confidence: float = 0.6,
    ) -> None:
        self.min_confidence = min_confidence
        self._net: cv2.dnn.Net | None = None
        self._cascade: cv2.CascadeClassifier | None = None
        self.backend = "none"

        proto, weights = Path(proto_path), Path(weights_path)
        if proto.is_file() and weights.is_file():
            try:
                self._net = cv2.dnn.readNetFromCaffe(str(proto), str(weights))
                self.backend = "dnn"
                logger.info("Face detector: res10 SSD DNN loaded from %s", weights.name)
            except cv2.error as exc:
                logger.error("Could not load DNN face model (%s); falling back to Haar", exc)
        else:
            logger.warning(
                "DNN face model not found at %s / %s -- falling back to Haar cascade. "
                "Run scripts/download_models.py to fetch it.",
                proto,
                weights,
            )

        if self._net is None:
            cascade_path = (
                Path(cv2.data.haarcascades)  # type: ignore[attr-defined]
                / "haarcascade_frontalface_default.xml"
            )
            cascade = cv2.CascadeClassifier(str(cascade_path))
            if cascade.empty():
                logger.error("Haar cascade could not be loaded; face detection is unavailable")
            else:
                self._cascade = cascade
                self.backend = "haar"
                logger.info("Face detector: Haar cascade fallback active")

    @property
    def available(self) -> bool:
        return self._net is not None or self._cascade is not None

    def detect(self, frame: np.ndarray) -> list[FaceBox]:
        """Return every face found in ``frame``, largest first."""
        if frame is None or frame.size == 0:
            return []
        boxes = self._detect_dnn(frame) if self._net is not None else self._detect_haar(frame)
        height, width = frame.shape[:2]
        clipped = [box.clipped_to(width, height) for box in boxes]
        return sorted(clipped, key=lambda box: box.area, reverse=True)

    def detect_largest(self, frame: np.ndarray) -> FaceBox | None:
        faces = self.detect(frame)
        return faces[0] if faces else None

    def _detect_dnn(self, frame: np.ndarray) -> list[FaceBox]:
        net = self._net
        if net is None:
            # The caller only routes here when the backend is "dnn", but an ``assert``
            # would vanish under ``python -O`` and leave an AttributeError in its place.
            return []
        height, width = frame.shape[:2]
        blob = cv2.dnn.blobFromImage(
            cv2.resize(frame, _DNN_INPUT_SIZE),
            scalefactor=1.0,
            size=_DNN_INPUT_SIZE,
            mean=_DNN_MEAN,
        )
        net.setInput(blob)
        detections = net.forward()

        results: list[FaceBox] = []
        for index in range(detections.shape[2]):
            confidence = float(detections[0, 0, index, 2])
            if confidence < self.min_confidence:
                continue
            # The network emits normalised corner coordinates.
            x1, y1, x2, y2 = (
                detections[0, 0, index, 3:7] * np.array([width, height, width, height])
            ).astype(int)
            box_width, box_height = x2 - x1, y2 - y1
            if box_width <= 0 or box_height <= 0:
                continue
            results.append(FaceBox(int(x1), int(y1), int(box_width), int(box_height), confidence))
        return results

    def _detect_haar(self, frame: np.ndarray) -> list[FaceBox]:
        if self._cascade is None:
            return []
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.equalizeHist(gray)
        faces = self._cascade.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=5, minSize=(50, 50)
        )
        # A cascade reports no score, so report the configured floor rather than
        # inventing a confidence number.
        return [
            FaceBox(int(x), int(y), int(w), int(h), self.min_confidence) for x, y, w, h in faces
        ]


def crop_face(
    frame: np.ndarray,
    box: FaceBox,
    size: int,
    margin: float = 0.15,
) -> np.ndarray:
    """Cut ``box`` out of ``frame`` as a normalised grayscale square.

    LBPH compares pixels positionally, so every sample -- at enrolment and at
    recognition time -- must go through this exact function or the comparison is
    meaningless.  Histogram equalisation removes most of the lighting difference
    between an enrolment photo and a live frame.
    """
    height, width = frame.shape[:2]
    pad_x = int(box.width * margin)
    pad_y = int(box.height * margin)

    x1 = max(0, box.x - pad_x)
    y1 = max(0, box.y - pad_y)
    x2 = min(width, box.x + box.width + pad_x)
    y2 = min(height, box.y + box.height + pad_y)

    region = frame[y1:y2, x1:x2]
    if region.size == 0:
        raise ValueError("Face crop is empty")

    if region.ndim == 3:
        region = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
    region = cv2.resize(region, (size, size), interpolation=cv2.INTER_AREA)
    return cv2.equalizeHist(region)
