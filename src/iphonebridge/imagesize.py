"""Pixel dimensions of an image file, as it will actually be displayed.

The UI needs these *before* the image decodes. A message bubble whose height
only becomes correct once the photo has loaded makes the whole list resize
under the user mid-scroll, which reads as sticky, jumpy scrolling — so the
sizes are recorded at import time and the delegate reserves the right space
from the start.

"As displayed" matters: phone photos are routinely stored rotated with the
true orientation only in metadata, and Qt honours that when drawing. A
landscape-on-disk photo that displays portrait must report portrait here or
the reserved space is wrong in the most visible way possible.
"""

from __future__ import annotations

import logging
import struct
from pathlib import Path

log = logging.getLogger(__name__)

# EXIF orientations that swap the axes (90°/270° rotations, with or without
# a mirror).
_EXIF_SWAP = {5, 6, 7, 8}
_EXIF_ORIENTATION_TAG = 274

# HEIC metadata lives in the `meta` box at the head of the file, so there's
# no reason to read a 4 MB photo to find it.
_HEIF_HEAD_BYTES = 256 * 1024


def _pillow_size(path: Path) -> tuple[int, int] | None:
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(path) as im:
            w, h = im.size
            try:
                exif = im.getexif()
                if exif and exif.get(_EXIF_ORIENTATION_TAG) in _EXIF_SWAP:
                    w, h = h, w
            except Exception:
                pass
            return (w, h) if w > 0 and h > 0 else None
    except Exception:
        return None


def _heif_size(path: Path) -> tuple[int, int] | None:
    """Dimensions from a HEIC/HEIF, which Pillow can't open unaided.

    A HEIF holds several images — a thumbnail, sometimes a half-scale
    preview — each with its own `ispe` box. The primary one is the largest,
    so take the max rather than the first, which is usually the thumbnail.
    """
    try:
        head = path.open("rb").read(_HEIF_HEAD_BYTES)
    except OSError:
        return None

    best: tuple[int, int] | None = None
    i = head.find(b"ispe")
    while i != -1:
        # ispe: 4-byte version/flags, then width and height as big-endian
        # unsigned 32-bit.
        chunk = head[i + 8:i + 16]
        if len(chunk) == 8:
            w, h = struct.unpack(">II", chunk)
            if 0 < w <= 100_000 and 0 < h <= 100_000:
                if best is None or w * h > best[0] * best[1]:
                    best = (w, h)
        i = head.find(b"ispe", i + 4)

    if best is None:
        return None

    # `irot` gives a counter-clockwise rotation in 90° steps; odd counts
    # swap the axes.
    j = head.find(b"irot")
    if j != -1 and len(head) > j + 4 and (head[j + 4] & 3) % 2 == 1:
        best = (best[1], best[0])
    return best


def display_size(path: str | Path) -> tuple[int, int] | None:
    """(width, height) as rendered, or None if it can't be determined."""
    path = Path(path)
    if not path.exists():
        return None
    size = _pillow_size(path)
    if size is None:
        size = _heif_size(path)
    return size
