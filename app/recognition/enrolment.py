"""Face enrolment.

Sample images live on disk under ``face_data/<student_pk>/`` and the LBPH model is
retrained from them.  Storing samples rather than a single descriptor per row is what
LBPH needs, and it means the model can be rebuilt from scratch at any time -- including
after a student is deleted, where leaving a stale label in the model would let a removed
student keep matching.
"""

from __future__ import annotations

import logging
from pathlib import Path

import cv2
import numpy as np

from app.recognition.detector import FaceDetector, crop_face
from app.recognition.recognizer import FaceRecognizer

logger = logging.getLogger(__name__)

SAMPLE_SUFFIX = ".png"  # lossless, so repeated training is deterministic


class EnrolmentError(ValueError):
    """Raised when a photo cannot be used for enrolment."""


def student_sample_dir(root: str | Path, student_pk: int) -> Path:
    return Path(root) / "samples" / str(student_pk)


def add_sample_from_image(
    image_path: str | Path,
    student_pk: int,
    *,
    face_root: str | Path,
    detector: FaceDetector,
    crop_size: int,
    crop_margin: float,
) -> Path:
    """Detect the largest face in ``image_path`` and store it as a training sample."""
    image = cv2.imread(str(image_path))
    if image is None:
        raise EnrolmentError(f"Could not read image at {image_path}")

    box = detector.detect_largest(image)
    if box is None:
        raise EnrolmentError(
            "No face detected in the photo. Use a clear, front-facing, well-lit image."
        )

    crop = crop_face(image, box, crop_size, crop_margin)
    directory = student_sample_dir(face_root, student_pk)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{_next_index(directory):03d}{SAMPLE_SUFFIX}"
    if not cv2.imwrite(str(destination), crop):
        raise EnrolmentError(f"Could not write face sample to {destination}")

    logger.info("Stored face sample %s for student pk=%s", destination.name, student_pk)
    return destination


def _next_index(directory: Path) -> int:
    existing = [path.stem for path in directory.glob(f"*{SAMPLE_SUFFIX}")]
    indices = [int(stem) for stem in existing if stem.isdigit()]
    return max(indices, default=0) + 1


def count_samples(face_root: str | Path, student_pk: int) -> int:
    directory = student_sample_dir(face_root, student_pk)
    if not directory.is_dir():
        return 0
    return len(list(directory.glob(f"*{SAMPLE_SUFFIX}")))


def remove_samples(face_root: str | Path, student_pk: int) -> None:
    directory = student_sample_dir(face_root, student_pk)
    if not directory.is_dir():
        return
    for path in directory.glob(f"*{SAMPLE_SUFFIX}"):
        path.unlink(missing_ok=True)
    try:
        directory.rmdir()
    except OSError:
        pass  # not empty; harmless


def load_samples(face_root: str | Path, student_pks: list[int]) -> dict[int, list[np.ndarray]]:
    """Read every stored sample for the given students."""
    samples: dict[int, list[np.ndarray]] = {}
    for pk in student_pks:
        directory = student_sample_dir(face_root, pk)
        if not directory.is_dir():
            continue
        crops = []
        for path in sorted(directory.glob(f"*{SAMPLE_SUFFIX}")):
            image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
            if image is None:
                logger.warning("Skipping unreadable sample %s", path)
                continue
            crops.append(image)
        if crops:
            samples[pk] = crops
    return samples


def retrain(
    recognizer: FaceRecognizer,
    face_root: str | Path,
    student_pks: list[int],
) -> int:
    """Rebuild the model from the samples of ``student_pks``."""
    samples = load_samples(face_root, student_pks)
    return recognizer.train(samples)
