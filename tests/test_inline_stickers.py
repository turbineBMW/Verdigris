"""True inline media uses U+F00A; free-standing stickers use U+FFFC.

"3-0 on my return" (live, the fucky group) is the inline case: the image
attachment belongs *inside* the text bubble at the F00A slot. Bitmoji /
peels marked with U+FFFC stay free-standing rows — inlining those was wrong
and turned "￼This mf so cute dawg" into a caption with a glued-on image.
"""
from __future__ import annotations

from verdigris.qtui.models import (
    _INLINE_MEDIA,
    _OBJ_REPLACEMENT,
    ThreadStore,
    _body_with_inline_media,
)


def _store() -> ThreadStore:
    s = ThreadStore.__new__(ThreadStore)
    s._threads = {}
    s._current = None
    s._by_guid = {}
    return s


def _msg(body: str, atts: list[dict], **kw) -> dict:
    m = {
        "body": body,
        "ts": "2026-07-30T12:00:00+00:00",
        "outgoing": False,
        "reaction": None,
        "attachments": atts,
        "guid": "g1",
        "reply_to": "",
        "sender_name": "aiden",
        "sender_phone": "+12155550102",
        "edits": [],
    }
    m.update(kw)
    return m


def _image(path="/tmp/inline.png", **kw) -> dict:
    """Live inline media often arrives with is_sticker=false."""
    att = {
        "path": path,
        "mime": "image/png",
        "name": "inline.png",
        "is_sticker": False,
        "w": 320,
        "h": 320,
    }
    att.update(kw)
    return att


def _sticker(path="/tmp/bitmoji.png", **kw) -> dict:
    att = {
        "path": path,
        "mime": "image/png",
        "name": "bitmoji.png",
        "is_sticker": True,
        "w": 400,
        "h": 400,
    }
    att.update(kw)
    return att


def test_f00a_inline_media_lives_in_the_text_bubble():
    """The real "3-0 on my return" shape: one bubble, image at the slot."""
    store = _store()
    body = f"3-0 on my return{_INLINE_MEDIA}"
    rows = store._rows_for(_msg(body, [_image()]), None)
    assert len(rows) == 1
    assert rows[0]["kind"] == "text"
    assert rows[0]["body"] == "3-0 on my return"
    assert _INLINE_MEDIA not in rows[0]["body"]
    assert '<img src="file:///tmp/inline.png"' in rows[0]["richBody"]
    assert "3-0 on my return" in rows[0]["richBody"]
    assert rows[0]["jumbo"] is False
    # As tall as the bubble text (13px), not a free-standing tile.
    assert 'height="13"' in rows[0]["richBody"]
    assert 'width="13"' in rows[0]["richBody"]  # square 320x320


def test_f00a_accepts_non_sticker_images():
    """Daemon marks many inline stickers is_sticker=false; still inline them."""
    plain, rich, jumbo, free = _body_with_inline_media(
        f"hi{_INLINE_MEDIA}", [_image(is_sticker=False)]
    )
    assert plain == "hi"
    assert free == []
    assert "<img" in rich
    assert jumbo is False


def test_bitmoji_with_fffc_stays_free_standing():
    """￼This mf so cute dawg — bare sticker row + clean caption, not inline."""
    store = _store()
    body = f"{_OBJ_REPLACEMENT}This mf so cute dawg"
    rows = store._rows_for(_msg(body, [_sticker()]), None)
    assert [r["kind"] for r in rows] == ["sticker", "text"]
    assert rows[0]["image"] == "file:///tmp/bitmoji.png"
    assert rows[1]["body"] == "This mf so cute dawg"
    assert _OBJ_REPLACEMENT not in rows[1]["body"]
    assert "<img" not in (rows[1]["richBody"] or "")


def test_fffc_only_body_is_bare_sticker():
    store = _store()
    rows = store._rows_for(_msg(_OBJ_REPLACEMENT, [_sticker()]), None)
    assert len(rows) == 1
    assert rows[0]["kind"] == "sticker"


def test_standalone_sticker_stays_bare():
    store = _store()
    rows = store._rows_for(_msg("", [_sticker()]), None)
    assert len(rows) == 1
    assert rows[0]["kind"] == "sticker"


def test_caption_without_marker_keeps_sticker_above_text():
    store = _store()
    rows = store._rows_for(_msg("look at this", [_sticker()]), None)
    assert [r["kind"] for r in rows] == ["sticker", "text"]
    assert rows[1]["body"] == "look at this"
    assert "<img" not in (rows[1]["richBody"] or "")


def test_f00a_before_download_drops_glyph_not_attachment_row():
    """No path yet → no free-standing broken image; just the clean text."""
    store = _store()
    body = f"3-0 on my return{_INLINE_MEDIA}"
    att = {"name": "x.png", "mime": "image/png", "is_sticker": False}
    rows = store._rows_for(_msg(body, [att]), None)
    assert len(rows) == 1
    assert rows[0]["kind"] == "text"
    assert rows[0]["body"] == "3-0 on my return"
    assert "<img" not in (rows[0]["richBody"] or "")


def test_photo_without_f00a_stays_its_own_row():
    store = _store()
    photo = _image(path="/tmp/photo.jpg", name="photo.jpg", is_sticker=False,
                   w=800, h=600)
    rows = store._rows_for(_msg("caption", [photo]), None)
    assert [r["kind"] for r in rows] == ["image", "text"]
    assert rows[1]["body"] == "caption"


def test_reply_snippet_shows_inline_sticker_at_quote_height():
    """A reply to "3-0 on my return" must quote the sticker, text-tall."""
    store = _store()
    parent = _msg(f"3-0 on my return{_INLINE_MEDIA}", [_image()], guid="PARENT")
    store._by_guid = {"PARENT": parent}
    reply = _msg("nice", [], guid="CHILD", reply_to="PARENT")
    snippet = store._reply_snippet(reply)
    assert "3-0 on my return" in snippet
    assert '<img src="file:///tmp/inline.png"' in snippet
    # Quote font is 11px in ConversationsPage — not the bubble's 13.
    assert 'height="11"' in snippet
    assert _INLINE_MEDIA not in snippet


def test_reply_snippet_plain_target_stays_plain():
    store = _store()
    store._by_guid = {"PARENT": _msg("are you free at 6", [], guid="PARENT")}
    reply = _msg("yes", [], reply_to="PARENT")
    assert store._reply_snippet(reply) == "are you free at 6"
