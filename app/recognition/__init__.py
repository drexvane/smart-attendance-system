"""Recognition subsystem.

The camera, detector, recognizer and pipeline are built lazily on first use and cached
on ``app.extensions``, so importing this package does not touch hardware -- the old code
opened a camera object at import time in ``app.py``.
"""

from __future__ import annotations

import logging

from flask import Flask, current_app

from app.recognition.camera import CameraError, CameraStream
from app.recognition.detector import FaceBox, FaceDetector, crop_face
from app.recognition.pipeline import KnownStudent, RecognitionPipeline
from app.recognition.recognizer import FaceRecognizer, Match

logger = logging.getLogger(__name__)

_DETECTOR_KEY = "recognition_detector"
_RECOGNIZER_KEY = "recognition_recognizer"
_STREAM_KEY = "recognition_stream"
_PIPELINE_KEY = "recognition_pipeline"

__all__ = [
    "CameraError",
    "CameraStream",
    "FaceBox",
    "FaceDetector",
    "FaceRecognizer",
    "KnownStudent",
    "Match",
    "RecognitionPipeline",
    "crop_face",
    "get_detector",
    "get_pipeline",
    "get_recognizer",
    "shutdown_recognition",
]


def get_detector(app: Flask | None = None) -> FaceDetector:
    app = app or current_app
    detector = app.extensions.get(_DETECTOR_KEY)
    if detector is None:
        detector = FaceDetector(
            proto_path=app.config["FACE_DETECTOR_PROTO"],
            weights_path=app.config["FACE_DETECTOR_WEIGHTS"],
            min_confidence=app.config["FACE_DETECTION_CONFIDENCE"],
        )
        app.extensions[_DETECTOR_KEY] = detector
    return detector


def get_recognizer(app: Flask | None = None) -> FaceRecognizer:
    app = app or current_app
    recognizer = app.extensions.get(_RECOGNIZER_KEY)
    if recognizer is None:
        recognizer = FaceRecognizer(
            model_path=app.config["FACE_MODEL_PATH"],
            label_map_path=app.config["FACE_LABEL_MAP_PATH"],
            max_distance=app.config["FACE_MATCH_MAX_DISTANCE"],
            min_margin=app.config["FACE_MATCH_MIN_MARGIN"],
        )
        recognizer.load()  # no-op when nothing has been trained yet
        app.extensions[_RECOGNIZER_KEY] = recognizer
    return recognizer


def get_stream(app: Flask | None = None) -> CameraStream:
    app = app or current_app
    stream = app.extensions.get(_STREAM_KEY)
    if stream is None:
        stream = CameraStream(
            index=app.config["CAMERA_INDEX"],
            width=app.config["CAMERA_WIDTH"],
            height=app.config["CAMERA_HEIGHT"],
            fps=app.config["CAMERA_FPS"],
            read_timeout=app.config["CAMERA_READ_TIMEOUT"],
        )
        app.extensions[_STREAM_KEY] = stream
    return stream


def get_pipeline(app: Flask | None = None) -> RecognitionPipeline:
    app = app or current_app
    pipeline = app.extensions.get(_PIPELINE_KEY)
    if pipeline is None:
        pipeline = RecognitionPipeline(
            stream=get_stream(app),
            detector=get_detector(app),
            recognizer=get_recognizer(app),
            crop_size=app.config["FACE_CROP_SIZE"],
            crop_margin=app.config["FACE_CROP_MARGIN"],
            confirm_frames=app.config["RECOGNITION_CONFIRM_FRAMES"],
            cooldown_seconds=app.config["RECOGNITION_COOLDOWN_SECONDS"],
        )
        app.extensions[_PIPELINE_KEY] = pipeline
    return pipeline


def shutdown_recognition(app: Flask) -> None:
    """Release the camera on process exit."""
    pipeline = app.extensions.get(_PIPELINE_KEY)
    if pipeline is not None:
        pipeline.stop()
    stream = app.extensions.get(_STREAM_KEY)
    if stream is not None:
        stream.force_stop()
