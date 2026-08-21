"""Anti-proxy confirmation logic.

The behaviour under test is the guarantee the project claimed and did not have: a single
frame must never be enough to mark attendance.  The old ``auto_mark_attendance`` wrote a
record for any face the histogram matcher scored above 0.3 on one frame.

Camera, detector and recognizer are replaced with deterministic fakes so this runs
without hardware.
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from app.recognition.camera import Frame
from app.recognition.detector import FaceBox
from app.recognition.pipeline import KnownStudent, RecognitionPipeline
from app.recognition.recognizer import Match


class FakeStream:
    """Replays a scripted list of "who is in frame" states, one per frame."""

    index = 0
    last_error = None

    def __init__(self, script: list[int | None]):
        self.script = script
        self.position = 0
        self.acquired = 0
        self.released = 0
        self.is_running = True
        self._image = np.zeros((100, 100, 3), dtype=np.uint8)
        self._exhausted = threading.Event()

    def acquire(self):
        self.acquired += 1

    def release(self):
        self.released += 1
        self.is_running = False

    def force_stop(self):
        self.is_running = False

    def latest(self):
        return Frame(self._image, self.position, time.monotonic())

    def wait_for_frame(self, after=0, timeout=None):  # noqa: ARG002
        if self.position >= len(self.script):
            self._exhausted.set()
            # Keep the loop alive but idle so stop() drives termination.
            time.sleep(0.01)
            return None
        self.position += 1
        return Frame(self._image, self.position, time.monotonic())

    @property
    def current_subject(self) -> int | None:
        return self.script[self.position - 1] if 0 < self.position <= len(self.script) else None

    def wait_until_exhausted(self, timeout=5.0):
        assert self._exhausted.wait(timeout), "pipeline did not consume the whole script"


class FakeDetector:
    backend = "fake"
    available = True
    min_confidence = 0.9

    def __init__(self, stream: FakeStream):
        self.stream = stream

    def detect(self, image):  # noqa: ARG002
        return [] if self.stream.current_subject is None else [FaceBox(10, 10, 40, 40, 0.99)]

    def detect_largest(self, image):
        faces = self.detect(image)
        return faces[0] if faces else None


class FakeRecognizer:
    """Returns whichever student the script says is in frame."""

    enrolled_count = 2
    is_trained = True
    max_distance = 70.0
    min_margin = 8.0

    def __init__(self, stream: FakeStream):
        self.stream = stream

    def predict(self, crop):  # noqa: ARG002
        subject = self.stream.current_subject
        if subject is None:
            return Match(None, float("inf"), 0.0, rejection="no_prediction")
        return Match(subject, 20.0, 0.71)


def build_pipeline(script, *, confirm_frames=3, cooldown=0):
    stream = FakeStream(script)
    pipeline = RecognitionPipeline(
        stream=stream,
        detector=FakeDetector(stream),
        recognizer=FakeRecognizer(stream),
        crop_size=50,
        crop_margin=0.1,
        confirm_frames=confirm_frames,
        cooldown_seconds=cooldown,
    )
    pipeline.load_known(
        [KnownStudent(pk=1, name="Alice", roll="S001"), KnownStudent(pk=2, name="Bob", roll="S002")]
    )
    return pipeline, stream


def run(pipeline, stream):
    pipeline.start()
    stream.wait_until_exhausted()
    pipeline.stop()


class TestConsecutiveFrameConfirmation:
    def test_one_frame_never_confirms(self):
        """The exact scenario the old code marked attendance on."""
        pipeline, stream = build_pipeline([1], confirm_frames=3)
        run(pipeline, stream)

        assert pipeline.take_confirmed() == {}

    def test_two_of_three_frames_does_not_confirm(self):
        pipeline, stream = build_pipeline([1, 1], confirm_frames=3)
        run(pipeline, stream)

        assert pipeline.take_confirmed() == {}

    def test_three_consecutive_frames_confirms(self):
        pipeline, stream = build_pipeline([1, 1, 1], confirm_frames=3)
        run(pipeline, stream)

        confirmed = pipeline.take_confirmed()
        assert set(confirmed) == {1}
        assert confirmed[1] == pytest.approx(0.71)

    def test_streak_must_be_consecutive(self):
        """Intermittent hits must not accumulate into a confirmation.

        Three sightings spread across a gap is precisely the pattern a flaky false
        positive produces, and it must not be treated as three consecutive frames.
        """
        pipeline, stream = build_pipeline([1, None, 1, None, 1], confirm_frames=3)
        run(pipeline, stream)

        assert pipeline.take_confirmed() == {}

    def test_a_different_face_breaks_the_streak(self):
        pipeline, stream = build_pipeline([1, 1, 2, 1, 1], confirm_frames=3)
        run(pipeline, stream)

        assert pipeline.take_confirmed() == {}

    def test_each_student_is_tracked_separately(self):
        pipeline, stream = build_pipeline([1, 1, 1, 2, 2, 2], confirm_frames=3)
        run(pipeline, stream)

        assert set(pipeline.take_confirmed()) == {1, 2}


class TestCooldown:
    def test_confirmation_is_published_once_per_cooldown(self):
        """Nine straight frames at a 3-frame threshold must not mark three times."""
        pipeline, stream = build_pipeline([1] * 9, confirm_frames=3, cooldown=3600)
        run(pipeline, stream)

        confirmed = pipeline.take_confirmed()
        assert list(confirmed) == [1]

    def test_zero_cooldown_allows_repeat_confirmation(self):
        pipeline, stream = build_pipeline([1] * 9, confirm_frames=3, cooldown=0)
        run(pipeline, stream)

        # Still deduplicated into one pending entry per student, because the pending map
        # is keyed by student -- the guarantee is "at most one write", not "one event".
        assert list(pipeline.take_confirmed()) == [1]


class TestTakeConfirmedDrains:
    def test_second_caller_gets_nothing(self):
        """Two polling clients must not both be handed the same confirmation."""
        pipeline, stream = build_pipeline([1, 1, 1], confirm_frames=3)
        run(pipeline, stream)

        assert set(pipeline.take_confirmed()) == {1}
        assert pipeline.take_confirmed() == {}


class TestReportedState:
    def test_unknown_face_is_reported_not_matched(self):
        stream = FakeStream([1])
        pipeline = RecognitionPipeline(
            stream=stream,
            detector=FakeDetector(stream),
            recognizer=FakeRecognizer(stream),
            confirm_frames=3,
            cooldown_seconds=0,
        )
        # Deliberately do not load student 1, so the match resolves to nobody known.
        pipeline.load_known([])
        run(pipeline, stream)

        assert pipeline.take_confirmed() == {}

    def test_preview_mode_does_not_recognise(self):
        pipeline, stream = build_pipeline([1] * 9, confirm_frames=3)
        pipeline.start(preview_only=True)
        stream.wait_until_exhausted()
        assert pipeline.is_running
        assert not pipeline.is_recognizing
        pipeline.stop()

        assert pipeline.take_confirmed() == {}

    def test_status_reports_the_thresholds_in_force(self):
        pipeline, _ = build_pipeline([], confirm_frames=4, cooldown=30)
        status = pipeline.status()

        assert status["confirm_frames"] == 4
        assert status["cooldown_seconds"] == 30
        assert status["detector_backend"] == "fake"
        assert status["running"] is False

    def test_camera_is_released_on_stop(self):
        pipeline, stream = build_pipeline([1, 1], confirm_frames=3)
        run(pipeline, stream)

        assert stream.acquired == 1
        assert stream.released == 1

    def test_stop_is_idempotent(self):
        pipeline, stream = build_pipeline([1], confirm_frames=3)
        run(pipeline, stream)
        pipeline.stop()

        assert stream.released == 1
