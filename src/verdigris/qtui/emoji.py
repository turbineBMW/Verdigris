"""Emoji shortcode lookup for the `:name` autocomplete in the composer.

Shortcodes are derived from the Unicode character names at import time
(GRINNING FACE → grinning_face), which gives full coverage of the emoji
blocks without shipping a data file or taking a dependency. Unicode names
don't always match what people actually type, so the handful of shortcodes
everyone knows from Discord/GitHub are aliased explicitly.
"""
from __future__ import annotations

import unicodedata
from functools import lru_cache

# Blocks worth scanning. Deliberately excludes the dingbat/arrow ranges that
# are technically emoji-presentable but that nobody wants in an emoji picker.
_RANGES = (
    (0x1F300, 0x1F5FF),   # symbols & pictographs
    (0x1F600, 0x1F64F),   # emoticons
    (0x1F680, 0x1F6FF),   # transport & map
    (0x1F900, 0x1F9FF),   # supplemental symbols (incl. most gestures)
    (0x1FA70, 0x1FAFF),   # extended-A (newer additions)
    (0x2600,  0x26FF),    # misc symbols
    (0x2700,  0x27BF),    # dingbats
)

# What people type vs. what Unicode calls it.
_ALIASES = {
    "joy": "😂", "smile": "😄", "grin": "😁", "sob": "😭", "cry": "😢",
    "wink": "😉", "heart": "❤️", "heart_eyes": "😍", "kiss": "😘",
    "thumbsup": "👍", "+1": "👍", "thumbsdown": "👎", "-1": "👎",
    "ok": "👌", "ok_hand": "👌", "pray": "🙏", "clap": "👏", "wave": "👋",
    "muscle": "💪", "fire": "🔥", "100": "💯", "tada": "🎉", "party": "🎉",
    "eyes": "👀", "thinking": "🤔", "shrug": "🤷", "facepalm": "🤦",
    "skull": "💀", "sunglasses": "😎", "sweat_smile": "😅", "yum": "😋",
    "smirk": "😏", "neutral": "😐", "confused": "😕", "angry": "😠",
    "rage": "😡", "poop": "💩", "ghost": "👻", "alien": "👽", "robot": "🤖",
    "cat": "🐱", "dog": "🐶", "star": "⭐", "sparkles": "✨", "zap": "⚡",
    "boom": "💥", "rocket": "🚀", "coffee": "☕", "beer": "🍺", "pizza": "🍕",
    "cake": "🎂", "gift": "🎁", "check": "✅", "x": "❌", "warning": "⚠️",
    "question": "❓", "exclamation": "❗", "sleeping": "😴", "cool": "🆒",
    "laughing": "😆", "upside_down": "🙃", "melting": "🫠", "salute": "🫡",
}


@lru_cache(maxsize=1)
def _table() -> list[tuple[str, str, int]]:
    """[(shortcode, emoji, rank)] — rank 0 for aliases, 1 for derived names."""
    seen: set[str] = set()
    out: list[tuple[str, str, int]] = []

    for code, char in _ALIASES.items():
        if code not in seen:
            seen.add(code)
            out.append((code, char, 0))

    for lo, hi in _RANGES:
        for cp in range(lo, hi + 1):
            ch = chr(cp)
            try:
                name = unicodedata.name(ch)
            except ValueError:
                continue          # unassigned codepoint
            code = name.lower().replace(" ", "_").replace("-", "_")
            if code in seen:
                continue
            seen.add(code)
            out.append((code, ch, 1))
    return out


def search(prefix: str, limit: int = 8) -> list[dict]:
    """Shortcodes starting with `prefix`, then ones merely containing it.

    Ranking within each group: aliases first, then shortest code. Sorting
    by length matters — in raw Unicode order `:fir` surfaced
    `first_quarter_moon_symbol` above `fireworks`, which is not what anyone
    means when they type `:fir`.
    """
    prefix = (prefix or "").lower().strip()
    if not prefix:
        return []
    starts: list[tuple] = []
    contains: list[tuple] = []
    for code, char, rank in _table():
        if code.startswith(prefix):
            starts.append((rank, len(code), code, char))
        elif prefix in code:
            contains.append((rank, len(code), code, char))

    starts.sort(key=lambda r: (r[0], r[1], r[2]))
    contains.sort(key=lambda r: (r[0], r[1], r[2]))
    return [{"code": c, "emoji": e}
            for _r, _l, c, e in (starts + contains)[:limit]]
