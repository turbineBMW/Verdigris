"""Pre-rendered circular contact avatars.

Masking a photo into a circle in QML costs several layered render targets
per avatar (mask layer + effect layer, each multisampled). With a list of
forty conversations that's over a hundred framebuffers, which is enough to
make scrolling stutter. Cutting the circle once with Pillow and caching the
result means the UI just draws a plain image — no layers, no shaders — and
the edge is smoother than the GPU mask was anyway.

Shared by the Qt UI and the notification sink.
"""
from __future__ import annotations

import logging
from pathlib import Path

from iphonebridge import config

log = logging.getLogger(__name__)

CACHE_DIR = config.STATE_DIR / "avatars"
_SIZE = 128
# Mask is drawn at 4x and downscaled — a plain ellipse at final size has
# visibly stepped edges.
_SUPERSAMPLE = 4

# Process-local memo: (src, size, mtime) → path. Sidebar refresh hits every
# visible conversation dozens of times a minute; the disk PNG is already
# cheap, but the mtime/stat round-trips still add up.
_MEMO: dict[tuple[str, int], tuple[float, str | None]] = {}


def circular(src: str | Path | None, size: int = _SIZE) -> str | None:
    """Round-cropped PNG for `src`, cached. Returns None when unavailable.

    Falls back to the original path if Pillow isn't installed, so callers
    still get a (square) picture rather than nothing.
    """
    if not src:
        return None
    source = Path(src)
    try:
        st = source.stat()
    except OSError:
        return None
    if not source.is_file():
        return None

    key = (str(source), size)
    mtime = st.st_mtime
    hit = _MEMO.get(key)
    if hit is not None and hit[0] == mtime:
        return hit[1]

    try:
        from PIL import Image, ImageDraw
    except Exception:
        result = str(source)
        _MEMO[key] = (mtime, result)
        return result

    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        dest = CACHE_DIR / f"{source.stem}_{size}.png"
        if dest.exists() and dest.stat().st_mtime >= mtime:
            result = str(dest)
            _MEMO[key] = (mtime, result)
            return result

        img = Image.open(source).convert("RGBA")
        # Centre-crop to a square first, or the circle comes out an ellipse.
        w, h = img.size
        side = min(w, h)
        img = img.crop(((w - side) // 2, (h - side) // 2,
                        (w - side) // 2 + side, (h - side) // 2 + side))
        img = img.resize((size, size), Image.LANCZOS)

        big = size * _SUPERSAMPLE
        mask = Image.new("L", (big, big), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, big - 1, big - 1), fill=255)
        mask = mask.resize((size, size), Image.LANCZOS)

        out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        out.paste(img, (0, 0), mask)
        out.save(dest, "PNG")
        result = str(dest)
        _MEMO[key] = (mtime, result)
        return result
    except Exception:
        log.debug("could not round-crop %s", src, exc_info=True)
        result = str(source)
        _MEMO[key] = (mtime, result)
        return result
