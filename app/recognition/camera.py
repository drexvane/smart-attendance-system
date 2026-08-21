"""Camera capture.

The old design ran two independent camera objects -- ``SimpleCamera`` for the plain
preview and ``FaceDetector`` for recognition -- each calling
``cv2.VideoCapture(0)`` and each with its own capture thread.  Starting both fought over
one physical device, and ``SimpleCamera.start_camera`` made it worse by silently probing
indices ``[1, 2, 0]`` on failure, so it could end up streaming a different camera than
the one that was asked for.

There is exactly one stream here, reference-counted across subscribers, and consumers
block on a condition variable until a new frame exists rather than polling with
``time.sleep(0.033)``.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import cv2
import numpy as np

logger = logging.getLogger(__name__)


class CameraError(RuntimeError):
    """Raised when the capture device cannot be opened or read."""


@dataclass
class Frame:
    """A captured frame plus the monotonically increasing sequence it arrived on."""

    image: np.ndarray
    sequence: int
    timestamp: float


class CameraStream:
    """A single shared capture device.

    ``acquire()``/``release()`` are reference-counted so the preview and the recognition
    pipeline can both use the camera and the device closes when the last one lets go.
    """

    def __init__(
        self,
        index: int = 0,
        width: int = 640,
        height: int = 480,
        fps: int = 30,
        read_timeout: float = 5.0,
    ) -> None:
        self.index = index
        self.width = width
        self.height = height
        self.fps = fps
        self.read_timeout = read_timeout

        self._capture: cv2.VideoCapture | None = None
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self._frame: Frame | None = None
        self._sequence = 0
        self._subscribers = 0
        self._condition = threading.Condition()
        self._state_lock = threading.RLock()
        self._last_error: str | None = None

    # --- lifecycle ------------------------------------------------------------
    @property
    def is_running(self) -> bool:
        return self._running.is_set()

    @property
    def subscribers(self) -> int:
        with self._state_lock:
            return self._subscribers

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def acquire(self) -> None:
        """Register a consumer, starting the device if it is not already open."""
        with self._state_lock:
            self._subscribers += 1
            if self._running.is_set():
                return
            try:
                self._open()
            except CameraError:
                self._subscribers -= 1
                raise

    def release(self) -> None:
        """Deregister a consumer, closing the device when none are left."""
        with self._state_lock:
            self._subscribers = max(0, self._subscribers - 1)
            if self._subscribers == 0:
                self._close()

    def force_stop(self) -> None:
        """Close the device regardless of subscriber count (shutdown path)."""
        with self._state_lock:
            self._subscribers = 0
            self._close()

    def _open(self) -> None:
        capture = cv2.VideoCapture(self.index)
        if not capture.isOpened():
            capture.release()
            self._last_error = f"Camera {self.index} could not be opened"
            # Deliberately no fallback to another index: silently switching devices is
            # how the old code ended up streaming the wrong camera.
            raise CameraError(
                f"{self._last_error}. Check that it is connected and not in use by "
                f"another application, or set CAMERA_INDEX."
            )

        capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        capture.set(cv2.CAP_PROP_FPS, self.fps)

        ok, probe = capture.read()
        if not ok or probe is None:
            capture.release()
            self._last_error = f"Camera {self.index} opened but returned no frames"
            raise CameraError(self._last_error)

        self._capture = capture
        self._last_error = None
        self._running.set()
        self._thread = threading.Thread(
            target=self._capture_loop, name=f"camera-{self.index}", daemon=True
        )
        self._thread.start()
        logger.info("Camera %d started (%dx%d)", self.index, self.width, self.height)

    def _close(self) -> None:
        self._running.clear()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3)
            if thread.is_alive():
                logger.warning("Camera thread did not stop within 3s")

        if self._capture is not None:
            try:
                self._capture.release()
            except cv2.error as exc:
                logger.warning("Error releasing camera: %s", exc)
            self._capture = None

        with self._condition:
            self._frame = None
            self._condition.notify_all()
        logger.info("Camera %d stopped", self.index)

    # --- capture --------------------------------------------------------------
    def _capture_loop(self) -> None:
        failures = 0
        max_failures = 30
        interval = 1.0 / max(self.fps, 1)

        while self._running.is_set() and self._capture is not None:
            ok, image = self._capture.read()
            if not ok or image is None:
                failures += 1
                if failures >= max_failures:
                    self._last_error = "Camera stopped returning frames"
                    logger.error("%s after %d attempts", self._last_error, failures)
                    break
                time.sleep(0.05)
                continue

            failures = 0
            with self._condition:
                self._sequence += 1
                self._frame = Frame(image, self._sequence, time.monotonic())
                self._condition.notify_all()
            time.sleep(interval)

        self._running.clear()
        with self._condition:
            self._condition.notify_all()
        logger.debug("Camera capture loop exited")

    # --- consumption ----------------------------------------------------------
    def latest(self) -> Frame | None:
        with self._condition:
            return self._frame

    def wait_for_frame(self, after: int = 0, timeout: float | None = None) -> Frame | None:
        """Block until a frame newer than ``after`` exists.

        Returns ``None`` on timeout or once the stream has stopped, which lets an MJPEG
        generator terminate cleanly instead of spinning on a global flag.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while True:
                if self._frame is not None and self._frame.sequence > after:
                    return self._frame
                if not self._running.is_set():
                    return None
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return None
                self._condition.wait(timeout=remaining if remaining is not None else 1.0)


def annotate(
    image: np.ndarray,
    labels: list[tuple[tuple[int, int, int, int], str, tuple[int, int, int]]],
    footer: str = "",
) -> np.ndarray:
    """Draw boxes and captions onto a copy of ``image``."""
    canvas = image.copy()
    for (x, y, width, height), caption, colour in labels:
        cv2.rectangle(canvas, (x, y), (x + width, y + height), colour, 2)
        if not caption:
            continue
        (text_width, text_height), _ = cv2.getTextSize(caption, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
        top = max(0, y - text_height - 8)
        cv2.rectangle(canvas, (x, top), (x + text_width + 6, y), colour, -1)
        cv2.putText(
            canvas,
            caption,
            (x + 3, y - 5),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (255, 255, 255),
            2,
        )

    if footer:
        cv2.putText(
            canvas,
            footer,
            (10, canvas.shape[0] - 15),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (0, 255, 255),
            2,
        )
    return canvas


def encode_jpeg(image: np.ndarray, quality: int = 80) -> bytes | None:
    ok, buffer = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    return buffer.tobytes() if ok else None
