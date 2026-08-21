"""Image intake.

``save_uploaded_file`` in the old helpers wrote whatever bytes it was handed to a path
built from the client-supplied filename, and the base64 capture path in ``app.py``
decoded a data URL straight to a ``.jpg``.  Neither confirmed the payload was an image.
Everything now goes through :func:`decode_and_validate`, which makes Pillow prove it.
"""

from __future__ import annotations

import base64
import binascii
import io
import logging
import uuid
from pathlib import Path

from PIL import Image, UnidentifiedImageError

logger = logging.getLogger(__name__)

# Pillow's own guard against decompression bombs; we set our own ceiling too.
Image.MAX_IMAGE_PIXELS = 50_000_000


class ImageValidationError(ValueError):
    """Raised when an upload is not a usable image."""


def decode_and_validate(
    raw: bytes,
    allowed_extensions: frozenset[str],
    max_pixels: int,
) -> tuple[bytes, str]:
    """Verify ``raw`` really is an image and return ``(normalised_bytes, extension)``.

    The file is re-encoded from the decoded pixels rather than passed through, which
    drops any trailing payload smuggled after the image data and strips EXIF (this
    system stores photos of students -- GPS tags in a phone snapshot are not wanted).
    """
    if not raw:
        raise ImageValidationError("Empty image payload")

    try:
        with Image.open(io.BytesIO(raw)) as probe:
            probe.verify()  # structural check; consumes the file object
        with Image.open(io.BytesIO(raw)) as image:
            width, height = image.size
            if width * height > max_pixels:
                raise ImageValidationError(
                    f"Image is too large: {width}x{height} exceeds {max_pixels} pixels"
                )
            fmt = (image.format or "").lower()
            extension = f".{'jpg' if fmt in {'jpeg', 'mpo'} else fmt}"
            if extension not in allowed_extensions:
                raise ImageValidationError(
                    f"Unsupported image format {fmt or 'unknown'}; "
                    f"allowed: {', '.join(sorted(allowed_extensions))}"
                )
            rgb = image.convert("RGB")
            buffer = io.BytesIO()
            rgb.save(buffer, format="JPEG", quality=90, optimize=True)
    except ImageValidationError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageValidationError(f"File is not a readable image: {exc}") from exc

    return buffer.getvalue(), ".jpg"


def decode_data_url(data_url: str) -> bytes:
    """Decode a ``data:image/...;base64,...`` capture from the browser."""
    if not data_url or not data_url.startswith("data:image"):
        raise ImageValidationError("Captured image is not a data URL")
    try:
        _, encoded = data_url.split(",", 1)
        return base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ImageValidationError(f"Captured image is not valid base64: {exc}") from exc


def store_image(
    payload: bytes,
    folder: str | Path,
    prefix: str,
    allowed_extensions: frozenset[str],
    max_pixels: int,
) -> str:
    """Validate then write an image, returning its path.

    The filename is generated, never taken from the client, so a crafted upload name
    cannot influence where the bytes land.
    """
    normalised, extension = decode_and_validate(payload, allowed_extensions, max_pixels)
    directory = Path(folder)
    directory.mkdir(parents=True, exist_ok=True)
    filename = f"{prefix}{uuid.uuid4().hex}{extension}"
    destination = directory / filename
    destination.write_bytes(normalised)
    logger.info("Stored image %s (%d bytes)", destination, len(normalised))
    return str(destination)


def delete_image(path: str | None) -> None:
    """Best-effort removal of a stored image."""
    if not path:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Could not delete image %s: %s", path, exc)
