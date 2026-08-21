"""The regression test this whole refactor exists for.

The original ``FaceEncoder`` described a face as ``cv2.calcHist`` over the grayscale
crop: a 256-bin count of pixel intensities.  A histogram is invariant to where the
pixels are, so it cannot represent a face -- and matching accepted any correlation above
``1.0 - tolerance``, which at the default tolerance of 0.6 meant 0.4.

These tests demonstrate the failure concretely and show that the LBPH replacement does
not share it.
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from app.recognition.recognizer import FaceRecognizer
from tests.conftest import make_face, shuffle_preserving_histogram


def legacy_histogram_encoding(image: np.ndarray) -> np.ndarray:
    """The old descriptor, reproduced verbatim from src/face_recognition/face_encoder.py."""
    resized = cv2.resize(image, (100, 100))
    hist = cv2.calcHist([resized], [0], None, [256], [0, 256]).flatten()
    return hist / (np.sum(hist) + 1e-7)


def legacy_correlation(a: np.ndarray, b: np.ndarray) -> float:
    return float(cv2.compareHist(a.astype(np.float32), b.astype(np.float32), cv2.HISTCMP_CORREL))


LEGACY_ACCEPT_THRESHOLD = 1.0 - 0.6  # the shipped default: correlation > 0.4


class TestLegacyHistogramIsNotAFaceDescriptor:
    def test_scrambled_image_is_a_perfect_match_to_itself(self):
        """Destroying all spatial structure leaves the histogram unchanged.

        This is the clearest statement of the flaw: an image with every pixel moved to a
        random position is still a ~1.0 "match", because the descriptor only counts
        intensities.  Built at 100x100 so the encoder's internal resize is a no-op and
        the comparison isolates the descriptor rather than the resampling.
        """
        face = make_face(seed=1, size=100)
        scrambled = shuffle_preserving_histogram(face, seed=99)

        assert not np.array_equal(face, scrambled), "the scramble must actually move pixels"

        correlation = legacy_correlation(
            legacy_histogram_encoding(face), legacy_histogram_encoding(scrambled)
        )

        assert correlation > 0.99
        assert correlation > LEGACY_ACCEPT_THRESHOLD

    def test_different_people_pass_the_shipped_threshold(self):
        """Distinct faces cross the 0.4 bar the old code used to accept a match."""
        matches = 0
        pairs = 0
        for left in range(6):
            for right in range(left + 1, 6):
                pairs += 1
                correlation = legacy_correlation(
                    legacy_histogram_encoding(make_face(seed=left)),
                    legacy_histogram_encoding(make_face(seed=right)),
                )
                if correlation > LEGACY_ACCEPT_THRESHOLD:
                    matches += 1

        # Not a threshold to tune -- the point is that false matches happen at all, and
        # in practice they happened often.
        assert matches > 0, (
            f"expected the legacy matcher to confuse at least one of {pairs} distinct pairs"
        )


class TestLbphSeparatesPeople:
    @pytest.fixture
    def recognizer(self, tmp_path):
        return FaceRecognizer(
            model_path=tmp_path / "model.yml",
            label_map_path=tmp_path / "labels.json",
            max_distance=70.0,
            min_margin=8.0,
        )

    @staticmethod
    def _samples(seed: int, count: int = 6) -> list[np.ndarray]:
        """Several near-duplicate views of one "person"."""
        base = make_face(seed=seed)
        rng = np.random.default_rng(seed * 100)
        return [
            np.clip(base.astype(np.int16) + rng.normal(0, 3, base.shape), 0, 255).astype(np.uint8)
            for _ in range(count)
        ]

    def test_trains_and_identifies_the_right_person(self, recognizer):
        samples = {10: self._samples(1), 20: self._samples(2), 30: self._samples(3)}
        assert recognizer.train(samples) == 3
        assert recognizer.is_trained

        for student_pk, crops in samples.items():
            match = recognizer.predict(crops[0])
            assert match.student_pk == student_pk, f"misidentified student {student_pk}: {match}"
            assert 0.0 <= match.confidence <= 1.0

    def test_scrambled_image_is_rejected(self, recognizer):
        """The case the legacy descriptor called a perfect 1.0 match."""
        samples = {10: self._samples(1), 20: self._samples(2)}
        recognizer.train(samples)

        scrambled = shuffle_preserving_histogram(samples[10][0], seed=7)
        match = recognizer.predict(scrambled)

        assert not match.is_match
        assert match.rejection is not None

    def test_unenrolled_face_is_unknown_not_nearest_neighbour(self, recognizer):
        """An unknown face must be reported Unknown, not forced onto a label.

        Queried with unstructured noise, which is unlike either enrolled pattern -- the
        old matcher would still have returned whichever label scored highest.
        """
        recognizer.train({10: self._samples(1), 20: self._samples(2)})

        rng = np.random.default_rng(4242)
        unknown = rng.integers(0, 256, (200, 200), dtype=np.uint8)
        match = recognizer.predict(unknown)

        assert not match.is_match
        assert match.rejection is not None

    def test_ambiguous_match_is_rejected_by_the_margin_check(self, recognizer):
        """Two enrolled students scoring alike must not be resolved arbitrarily.

        The old code took whichever candidate happened to score highest with no notion
        of how close the runner-up was.
        """
        shared = make_face(seed=5)
        # Enrol the *same* appearance under two labels: any query is genuinely ambiguous.
        recognizer.train({10: [shared] * 4, 20: [shared] * 4})
        recognizer.min_margin = 50.0  # force the ambiguity to be detected

        match = recognizer.predict(shared)

        assert not match.is_match
        assert match.rejection == "ambiguous_match"

    def test_untrained_recognizer_reports_no_enrolled_faces(self, recognizer):
        match = recognizer.predict(make_face(seed=1))
        assert not match.is_match
        assert match.rejection == "no_enrolled_faces"

    def test_empty_training_set_resets_the_model(self, recognizer):
        recognizer.train({10: self._samples(1)})
        assert recognizer.is_trained

        assert recognizer.train({}) == 0
        assert not recognizer.is_trained
        assert recognizer.enrolled_count == 0

    def test_model_survives_a_save_and_reload(self, recognizer, tmp_path):
        samples = {10: self._samples(1), 20: self._samples(2)}
        recognizer.train(samples)

        reloaded = FaceRecognizer(
            model_path=tmp_path / "model.yml",
            label_map_path=tmp_path / "labels.json",
            max_distance=70.0,
            min_margin=8.0,
        )
        assert reloaded.load()
        assert reloaded.enrolled_count == 2
        assert reloaded.predict(samples[20][0]).student_pk == 20

    def test_label_map_round_trips(self, recognizer):
        recognizer.train({7: self._samples(1), 9: self._samples(2)})
        assert set(recognizer.label_map.values()) == {7, 9}
        assert set(recognizer.student_labels) == {7, 9}
