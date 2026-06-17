# -*- coding: utf-8 -*-
"""Face-aware horizontal centering for 9:16 reframing (clip mode).

Given a few sample frames from a clip, find the horizontal center of the face so
the vertical crop keeps the speaker in view (the "фокус на лицо" requirement).

Windows + non-ASCII paths note: OpenCV's FileStorage and imread CANNOT open files
whose path contains non-ASCII characters (e.g. "контент завод"). So we (a) copy the
Haar cascade to an ASCII temp path before loading it, and (b) read frames via
np.fromfile + cv2.imdecode instead of cv2.imread. Without this, face detection
silently fails on a Cyrillic install path and every clip falls back to center crop.

Hard rule: BEST-EFFORT. If opencv is missing, the cascade can't load, or no face
is found, return None and the caller does a center crop. The clip always renders.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import List, Optional

_CASCADE = None          # cached cv2.CascadeClassifier
_CASCADE_TRIED = False    # so we only attempt the (possibly failing) load once


def opencv_available() -> bool:
    try:
        import cv2  # noqa: F401
        return True
    except Exception:
        return False


def _get_cascade():
    """Load the frontal-face Haar cascade, working around non-ASCII paths."""
    global _CASCADE, _CASCADE_TRIED
    if _CASCADE_TRIED:
        return _CASCADE
    _CASCADE_TRIED = True
    try:
        import cv2
        src = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"  # type: ignore[attr-defined]

        # 1) Direct load when the path is pure ASCII.
        if src.isascii():
            clf = cv2.CascadeClassifier(src)
            if not clf.empty():
                _CASCADE = clf
                return _CASCADE

        # 2) Copy to an ASCII temp path (OpenCV can't open non-ASCII paths on Windows).
        tmp = Path(tempfile.gettempdir()) / "trezzy_haar_frontalface.xml"
        if str(tmp).isascii():
            if not tmp.exists():
                shutil.copyfile(src, tmp)
            clf = cv2.CascadeClassifier(str(tmp))
            if not clf.empty():
                _CASCADE = clf
                return _CASCADE
    except Exception:
        pass
    return None


def _imread(path: Path):
    """Read an image in a way that tolerates non-ASCII paths on Windows."""
    import cv2
    import numpy as np
    try:
        data = np.fromfile(str(path), dtype=np.uint8)   # Python IO handles unicode paths
        if data.size:
            img = cv2.imdecode(data, cv2.IMREAD_COLOR)
            if img is not None:
                return img
    except Exception:
        pass
    try:
        return cv2.imread(str(path))
    except Exception:
        return None


def face_center_x(frame_paths: List[Path]) -> Optional[float]:
    """Return the median normalized X (0..1) of detected faces across frames.

    None if opencv is missing, the cascade can't load, or no face is found.
    """
    clf = _get_cascade()
    if clf is None:
        return None
    try:
        import cv2
    except Exception:
        return None

    centers: List[float] = []
    for p in frame_paths:
        try:
            img = _imread(p)
            if img is None:
                continue
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            faces = clf.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60))
            if len(faces) == 0:
                continue
            x, y, w, h = max(faces, key=lambda f: int(f[2]) * int(f[3]))   # largest face
            width = img.shape[1] or 1
            centers.append((float(x) + float(w) / 2.0) / float(width))
        except Exception:
            continue

    if not centers:
        return None
    centers.sort()
    return centers[len(centers) // 2]  # median = robust to a stray detection


__all__ = ["face_center_x", "opencv_available"]
