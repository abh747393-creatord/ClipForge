"""First-run asset downloads: caption fonts + the YuNet face model.

These are binary files, so they are not committed to the repository.
On first start ClipForge downloads any missing ones from their official
sources (Google Fonts / OpenCV Zoo). If a download fails (offline), the
tool still starts; facecam auto-detection and styled captions degrade
gracefully until the files are present.
"""

from __future__ import annotations

import urllib.request
from pathlib import Path

from . import config

# (relative path under BASE_DIR, official download URL)
ASSETS: list[tuple[str, str]] = [
    ("fonts/Anton-Regular.ttf",
     "https://github.com/google/fonts/raw/main/ofl/anton/Anton-Regular.ttf"),
    ("fonts/ArchivoBlack-Regular.ttf",
     "https://github.com/google/fonts/raw/main/ofl/archivoblack/ArchivoBlack-Regular.ttf"),
    ("fonts/Poppins-ExtraBold.ttf",
     "https://github.com/google/fonts/raw/main/ofl/poppins/Poppins-ExtraBold.ttf"),
    ("models/face_yunet.onnx",
     "https://github.com/opencv/opencv_zoo/raw/main/models/"
     "face_detection_yunet/face_detection_yunet_2023mar.onnx"),
]


def ensure_assets(base_dir: Path | None = None) -> list[str]:
    """Download any missing asset files. Returns human-readable status lines."""
    base = Path(base_dir) if base_dir is not None else config.BASE_DIR
    lines: list[str] = []
    for rel, url in ASSETS:
        dest = base / rel
        if dest.exists() and dest.stat().st_size > 0:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            urllib.request.urlretrieve(url, dest)
            lines.append(f"downloaded {rel} ({dest.stat().st_size // 1024} KB)")
        except Exception as exc:  # noqa: BLE001 - never block startup on assets
            lines.append(f"WARNING: could not download {rel}: {exc}")
    return lines
