#!/usr/bin/env python3
"""Fetch the DNN face-detector model files into ``models/``.

Both files are tracked in git, so a normal clone already has them and this script is a
no-op.  It exists for the case where they are missing -- a shallow export, a sparse
checkout, or a stale image built before the files were added -- because without them
:class:`app.recognition.detector.DnnFaceDetector` falls back to a Haar cascade, which
detects noticeably worse and does so *silently*.

Two things this deliberately does that the previous version did not:

* files are written next to the repository, not next to the current working directory,
  so ``python scripts/download_models.py`` from any directory puts them where the
  detector looks;
* every download is verified against a pinned SHA-256 and a bad digest is deleted rather
  than left on disk.  These URLs are plain ``raw.githubusercontent.com`` paths on
  branches, and a branch is mutable: without a digest this script would install whatever
  those paths happen to serve.

Exit status is non-zero when a file is missing at the end, so it can be used in a setup
script or a Dockerfile without `|| true` hiding a failed fetch.
"""

from __future__ import annotations

import hashlib
import shutil
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

MODELS_DIR = Path(__file__).resolve().parent.parent / "models"

CHUNK = 64 * 1024
TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class Model:
    name: str
    url: str
    sha256: str
    size: int
    description: str

    @property
    def path(self) -> Path:
        return MODELS_DIR / self.name


MODELS: tuple[Model, ...] = (
    Model(
        name="deploy.prototxt",
        url=(
            "https://raw.githubusercontent.com/opencv/opencv/master/"
            "samples/dnn/face_detector/deploy.prototxt"
        ),
        sha256="dcd661dc48fc9de0a341db1f666a2164ea63a67265c7f779bc12d6b3f2fa67e9",
        size=28104,
        description="network architecture",
    ),
    Model(
        name="res10_300x300_ssd_iter_140000.caffemodel",
        url=(
            "https://raw.githubusercontent.com/opencv/opencv_3rdparty/"
            "dnn_samples_face_detector_20170830/res10_300x300_ssd_iter_140000.caffemodel"
        ),
        sha256="2a56a11a57a4a295956b0660b4a3d76bbdca2206c4961cea8efe7d95c7cb2f2d",
        size=10666211,
        description="trained weights (10 MB)",
    ),
)


def digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            sha.update(block)
    return sha.hexdigest()


def download(model: Model) -> bool:
    """Fetch one model to a temporary path, verify it, then move it into place."""
    tmp = model.path.with_suffix(model.path.suffix + ".part")
    print(f"  fetching {model.name} ({model.description}) ...")
    try:
        # The URLs above are literal https constants; S310 is about dynamic schemes.
        with (
            urllib.request.urlopen(model.url, timeout=TIMEOUT_SECONDS) as response,  # noqa: S310
            tmp.open("wb") as handle,
        ):
            shutil.copyfileobj(response, handle, CHUNK)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"  FAILED: {exc}")
        tmp.unlink(missing_ok=True)
        return False

    actual = digest(tmp)
    if actual != model.sha256:
        print("  FAILED: checksum mismatch -- refusing to install this file")
        print(f"    expected {model.sha256}")
        print(f"    got      {actual}")
        tmp.unlink(missing_ok=True)
        return False

    tmp.replace(model.path)
    print(f"  ok ({model.path.stat().st_size} bytes, sha256 verified)")
    return True


def main() -> int:
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"DNN face detector models -> {MODELS_DIR}")

    failed: list[str] = []
    for model in MODELS:
        if model.path.exists():
            if digest(model.path) == model.sha256:
                print(f"  {model.name}: present and verified, skipping")
                continue
            print(f"  {model.name}: present but the checksum does not match, refetching")
        if not download(model):
            failed.append(model.name)

    if failed:
        print(f"\n{len(failed)} file(s) missing: {', '.join(failed)}")
        print("The detector will fall back to a Haar cascade, which is less accurate.")
        return 1

    print("\nAll model files present. The DNN detector will be used.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
