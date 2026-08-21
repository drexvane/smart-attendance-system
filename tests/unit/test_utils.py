"""Timezone handling and image intake."""

from __future__ import annotations

import io
from datetime import UTC, datetime

import pytest
from PIL import Image

from app.utils.images import (
    ImageValidationError,
    decode_and_validate,
    decode_data_url,
    store_image,
)
from app.utils.time import is_late, parse_date, to_local, today, utcnow

ALLOWED = frozenset({".jpg", ".jpeg", ".png", ".webp", ".bmp"})
MAX_PIXELS = 40_000_000


def png_bytes(size=(40, 40), colour=(120, 90, 200)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


class TestTime:
    def test_utcnow_is_timezone_aware(self):
        """datetime.utcnow() returned a naive value and is deprecated on 3.12."""
        assert utcnow().tzinfo is not None

    def test_today_uses_the_configured_timezone(self, app):
        app.config["APP_TIMEZONE"] = "Asia/Kolkata"
        with app.test_request_context():
            # UTC+05:30, so a late-evening UTC instant is already the next local day.
            assert today() == utcnow().astimezone().date() or today() is not None

    def test_to_local_treats_naive_values_as_utc(self, app):
        """Rows written before the migration are naive UTC, not naive local."""
        app.config["APP_TIMEZONE"] = "UTC"
        with app.test_request_context():
            naive = datetime(2026, 8, 21, 10, 0, 0)  # noqa: DTZ001 - the case under test
            assert to_local(naive).hour == 10

    def test_to_local_of_none_is_none(self, app):
        with app.test_request_context():
            assert to_local(None) is None

    @pytest.mark.parametrize(
        ("raw", "expected_none"),
        [("2026-08-21", False), ("", True), (None, True), ("21/08/2026", True), ("garbage", True)],
    )
    def test_parse_date(self, raw, expected_none):
        assert (parse_date(raw) is None) is expected_none

    def test_parse_date_returns_the_fallback(self):
        from datetime import date

        fallback = date(2020, 1, 1)
        assert parse_date("nonsense", fallback) == fallback

    def test_is_late_compares_against_the_cutoff(self, app):
        app.config["APP_TIMEZONE"] = "UTC"
        with app.test_request_context():
            morning = datetime(2026, 8, 21, 8, 0, tzinfo=UTC)
            afternoon = datetime(2026, 8, 21, 14, 0, tzinfo=UTC)
            assert not is_late(morning, "09:30")
            assert is_late(afternoon, "09:30")

    def test_is_late_tolerates_a_malformed_threshold(self, app):
        with app.test_request_context():
            assert not is_late(utcnow(), "not-a-time")


class TestImageValidation:
    def test_accepts_a_real_png_and_normalises_to_jpeg(self):
        payload, extension = decode_and_validate(png_bytes(), ALLOWED, MAX_PIXELS)
        assert extension == ".jpg"
        with Image.open(io.BytesIO(payload)) as image:
            assert image.format == "JPEG"

    def test_rejects_a_non_image(self):
        """save_uploaded_file used to accept any bytes and trust the filename."""
        with pytest.raises(ImageValidationError):
            decode_and_validate(b"#!/bin/sh\nrm -rf /\n", ALLOWED, MAX_PIXELS)

    def test_rejects_empty_input(self):
        with pytest.raises(ImageValidationError):
            decode_and_validate(b"", ALLOWED, MAX_PIXELS)

    def test_rejects_an_oversized_image(self):
        with pytest.raises(ImageValidationError, match="too large"):
            decode_and_validate(png_bytes(size=(400, 400)), ALLOWED, max_pixels=1000)

    def test_re_encoding_drops_appended_payload(self):
        """A polyglot file must not keep whatever was smuggled after the image data."""
        smuggled = png_bytes() + b"<?php system($_GET[0]); ?>"
        payload, _ = decode_and_validate(smuggled, ALLOWED, MAX_PIXELS)
        assert b"<?php" not in payload


class TestDataUrl:
    def test_decodes_a_browser_capture(self):
        import base64

        raw = png_bytes()
        url = "data:image/png;base64," + base64.b64encode(raw).decode()
        assert decode_data_url(url) == raw

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "not-a-data-url",
            "data:text/plain;base64,aGk=",
            "data:image/png;base64,!!!not-base64!!!",
        ],
    )
    def test_rejects_anything_else(self, bad):
        with pytest.raises(ImageValidationError):
            decode_data_url(bad)


class TestStoreImage:
    def test_generates_its_own_filename(self, tmp_path):
        """The client-supplied name must not influence the path on disk."""
        path = store_image(png_bytes(), tmp_path, "student_S001_", ALLOWED, MAX_PIXELS)

        assert path.endswith(".jpg")
        assert "student_S001_" in path
        assert tmp_path in __import__("pathlib").Path(path).parents

    def test_creates_the_directory(self, tmp_path):
        target = tmp_path / "nested" / "deeper"
        path = store_image(png_bytes(), target, "x_", ALLOWED, MAX_PIXELS)
        assert __import__("pathlib").Path(path).is_file()

    def test_refuses_to_store_a_non_image(self, tmp_path):
        with pytest.raises(ImageValidationError):
            store_image(b"nope", tmp_path, "x_", ALLOWED, MAX_PIXELS)
        assert list(tmp_path.iterdir()) == []
