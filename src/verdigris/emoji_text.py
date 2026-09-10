"""Emoji-aware markup for message bodies.

Two problems this solves, both of them cosmetic but glaring:

* Noto Color Emoji draws noticeably smaller than the Latin text it sits
  next to at the same pixel size, so inline emoji look shrunken. Qt has no
  per-font size adjustment we can reach from QML, so the fix is to mark the
  emoji runs up as rich text and give them their own font-size.
* A message that is *only* emoji gets drawn large and bare in Messages.app,
  with no bubble behind it. That needs the same run detection.

Detecting "is this character an emoji" properly needs the Unicode
Emoji_Presentation property, which the stdlib doesn't expose. The rule used
here is the practical half of it: astral pictographs are always emoji, BMP
characters are only emoji when they either carry a VS16 (U+FE0F) or appear
in the small hand-listed set that defaults to emoji presentation. That is
what iOS actually sends, so in practice it matches.
"""

from __future__ import annotations

import re
from html import escape

# Astral pictographs: emoji by default, no variation selector needed.
_ASTRAL = "\U0001F000-\U0001FAFF"
# Regional indicators, which pair up into flags.
_FLAG = "\U0001F1E6-\U0001F1FF"
# Skin-tone modifiers (inside _ASTRAL, named for the sequence pattern below).
_TONE = "\U0001F3FB-\U0001F3FF"

# The BMP characters whose Emoji_Presentation is Yes — they render as emoji
# with no VS16. Everything else in the BMP needs one, which keeps text-y
# characters like → and ™ from being blown up.
_BMP_ALWAYS = (
    "⌚-⌛⏩-⏬⏰⏳◽-◾"
    "☔-☕♈-♓♿⚓⚡⚪-⚫"
    "⚽-⚾⛄-⛅⛎⛔⛪⛲-⛳"
    "⛵⛺⛽✅✊-✋✨❌❎"
    "❓-❕❗➕-➗➰➿"
    "⬛-⬜⭐⭕"
)

_VS16 = "\ufe0f"
_ZWJ = "\u200d"
_KEYCAP = "\u20e3"
# BMP characters that *may* be emoji, but only when a VS16 says so.
_BMP_OPTIONAL = "\u00a9\u00ae\u2000-\u32ff"

# One emoji "character": an always-emoji base, or anything carrying a VS16.
_BASE = f"(?:[{_ASTRAL}{_BMP_ALWAYS}]|[{_BMP_OPTIONAL}]{_VS16})"
# ...plus its modifiers, and any ZWJ-joined continuation (family, rainbow flag).
_MODS = f"(?:{_VS16}|[{_TONE}])*"
_CLUSTER = (
    f"(?:[0-9#*]{_VS16}?{_KEYCAP}"       # keycaps
    f"|[{_FLAG}]{{2}}"                   # flags
    f"|{_BASE}{_MODS}(?:{_ZWJ}{_BASE}{_MODS})*"
    f")"
)

_RUN = re.compile(f"(?:{_CLUSTER})+")
_CLUSTER_RE = re.compile(_CLUSTER)

# Inline emoji get bumped above the surrounding text to compensate for the
# colour font's smaller drawing size.
INLINE_SCALE = 1.35
# An emoji-only message is drawn big and bare, but only up to a few of them —
# past that they'd wrap into a wall, so they stay closer to normal size.
JUMBO_MAX = 3
JUMBO_SIZE = 56
JUMBO_SIZE_MANY = 32


def emoji_count(text: str) -> int:
    """How many emoji clusters `text` contains."""
    return len(_CLUSTER_RE.findall(text))


def is_emoji_only(text: str) -> bool:
    """True if `text` is emoji and whitespace, and has at least one emoji."""
    stripped = text.strip()
    if not stripped:
        return False
    return not _RUN.sub("", stripped).strip()


def has_emoji(text: str) -> bool:
    return bool(_RUN.search(text))


# Same shape as link_preview.extract_urls — kept local so this module stays
# free of a cycle (link_preview imports config, emoji_text must not).
_URL_RE = re.compile(
    r"(https?://[^\s<>\"'\]\)\}>,]+)",
    re.IGNORECASE,
)
_URL_TRAIL = ".,;:!?)]}'\"…"


def _split_urls(text: str) -> list[tuple[str, bool]]:
    """Segment `text` into [(piece, is_url), ...] preserving order."""
    if not text:
        return []
    out: list[tuple[str, bool]] = []
    pos = 0
    for m in _URL_RE.finditer(text):
        raw = m.group(1)
        url = raw.rstrip(_URL_TRAIL)
        if not url:
            continue
        if m.start() > pos:
            out.append((text[pos:m.start()], False))
        out.append((url, True))
        # Put stripped trailing punctuation back as plain text.
        trail = raw[len(url):]
        end = m.end()
        if trail:
            out.append((trail, False))
        pos = end
    if pos < len(text):
        out.append((text[pos:], False))
    return out if out else [(text, False)]


def _markup_plain(text: str, size: int) -> str:
    """Escape `text` and size any emoji runs; always returns a string."""
    if not text:
        return ""
    if not has_emoji(text):
        return escape(text).replace("\n", "<br>")

    def wrap(m: re.Match[str]) -> str:
        return f'<span style="font-size:{size}px">{escape(m.group(0))}</span>'

    out: list[str] = []
    pos = 0
    for m in _RUN.finditer(text):
        out.append(escape(text[pos:m.start()]))
        out.append(wrap(m))
        pos = m.end()
    out.append(escape(text[pos:]))
    return "".join(out).replace("\n", "<br>")


def markup(text: str, size: int, *, link_color: str | None = None) -> str:
    """`text` as Qt rich text with emoji runs sized at `size` px.

    Returns "" when there's nothing to mark up (no emoji, no URLs), so
    callers can fall back to plain text and skip the rich-text layout.
    When `link_color` is set, http(s) URLs become tappable anchors.
    """
    has_links = bool(_URL_RE.search(text or ""))
    if not has_emoji(text) and not has_links:
        return ""

    if not has_links:
        return _markup_plain(text, size)

    # Link colour defaults to Messages-style blue; the caller overrides for
    # outgoing bubbles where the body is white.
    color = link_color or "#2f8fff"
    chunks: list[str] = []
    for piece, is_url in _split_urls(text):
        if is_url:
            href = escape(piece, quote=True)
            label = escape(piece)
            chunks.append(
                f'<a href="{href}" style="color:{color};'
                f'text-decoration:underline;">{label}</a>'
            )
        else:
            chunks.append(_markup_plain(piece, size))
    return "".join(chunks)


def body_markup(
    text: str, base_size: int, *, link_color: str | None = None
) -> tuple[str, bool]:
    """Rich text and jumbo flag for a message body.

    The jumbo flag means "draw this without a bubble" — the caller still
    needs to honour it, the markup only carries the size. URLs are always
    linkified when present so the bubble itself is tappable.
    """
    if is_emoji_only(text):
        n = emoji_count(text)
        size = JUMBO_SIZE if n <= JUMBO_MAX else JUMBO_SIZE_MANY
        # Emoji-only never has a URL worth linkifying.
        return markup(text, size), True
    return markup(
        text, round(base_size * INLINE_SCALE), link_color=link_color
    ), False
