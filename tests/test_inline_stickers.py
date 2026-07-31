"""Stickers that sit inside a message body must render in the bubble.

sms.db (and live sticker parts) mark those positions with U+FFFC. Before this
was wired up, the UI drew the sticker as a free-standing row above the text
and left the placeholder glyph in the caption — so every "sticker + words"
message looked broken.
"""
from __future__ import annotations

from iphonebridge.imessage.bridge import translate
from iphonebridge.qtui.models import (
    _OBJ_REPLACEMENT,
    _body_with_inline_stickers,
    ThreadStore,
)

MY = ["tel:+12155550150"]


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


def _sticker(path="/tmp/cute-sticker.png", **kw) -> dict:
    att = {
        "path": path,
        "mime": "image/png",
        "name": "cute-sticker.png",
        "is_sticker": True,
        "w": 400,
        "h": 400,
    }
    att.update(kw)
    return att


def test_inline_sticker_lives_in_the_text_bubble():
    """One row: the sticker is an <img> in richBody, not a bare sticker row."""
    store = _store()
    body = f"{_OBJ_REPLACEMENT}This mf so cute dawg"
    rows = store._rows_for(_msg(body, [_sticker()]), None)
    assert len(rows) == 1
    assert rows[0]["kind"] == "text"
    assert rows[0]["body"] == "This mf so cute dawg"
    assert _OBJ_REPLACEMENT not in rows[0]["body"]
    assert '<img src="file:///tmp/cute-sticker.png"' in rows[0]["richBody"]
    assert "This mf so cute dawg" in rows[0]["richBody"]
    assert rows[0]["jumbo"] is False


def test_standalone_sticker_stays_bare():
    """No surrounding text → free-standing transparent sticker, as before."""
    store = _store()
    rows = store._rows_for(_msg("", [_sticker()]), None)
    assert len(rows) == 1
    assert rows[0]["kind"] == "sticker"
    assert rows[0]["image"] == "file:///tmp/cute-sticker.png"


def test_marker_only_body_is_still_a_bare_sticker():
    """A body that is nothing but U+FFFC is an attachment-only message."""
    store = _store()
    rows = store._rows_for(_msg(_OBJ_REPLACEMENT, [_sticker()]), None)
    assert len(rows) == 1
    assert rows[0]["kind"] == "sticker"


def test_sticker_without_marker_stays_free_standing():
    """Caption with no U+FFFC keeps the sticker above the text bubble."""
    store = _store()
    rows = store._rows_for(
        _msg("look at this", [_sticker()]), None
    )
    assert [r["kind"] for r in rows] == ["sticker", "text"]
    assert rows[1]["body"] == "look at this"
    assert "<img" not in (rows[1]["richBody"] or "")


def test_two_inline_stickers_match_markers_in_order():
    body = f"this one mines{_OBJ_REPLACEMENT} and this one is ky’s {_OBJ_REPLACEMENT}"
    a = _sticker(path="/tmp/a.png", name="a.png")
    b = _sticker(path="/tmp/b.png", name="b.png")
    plain, rich, jumbo, free = _body_with_inline_stickers(body, [a, b])
    assert plain == "this one mines and this one is ky’s "
    assert free == []
    assert jumbo is False
    assert rich.index("file:///tmp/a.png") < rich.index("file:///tmp/b.png")
    assert rich.count("<img") == 2


def test_photo_is_not_pulled_inline():
    """Only stickers fill U+FFFC slots; a regular image stays its own row."""
    store = _store()
    photo = {
        "path": "/tmp/photo.jpg",
        "mime": "image/jpeg",
        "name": "photo.jpg",
        "is_sticker": False,
        "w": 800,
        "h": 600,
    }
    body = f"{_OBJ_REPLACEMENT}caption"
    # No sticker available to claim the marker — photo is free-standing,
    # marker is dropped so the caption is clean.
    rows = store._rows_for(_msg(body, [photo]), None)
    assert [r["kind"] for r in rows] == ["image", "text"]
    assert rows[1]["body"] == "caption"
    assert "<img" not in (rows[1]["richBody"] or "")


def test_live_sticker_part_writes_a_body_marker():
    """The bridge must leave U+FFFC so live traffic shares the backup path."""
    event = {
        "event": "message",
        "inst": {
            "id": "CC9AE249",
            "sender": "tel:+12155550101",
            "conversation": {
                "participants": ["tel:+12155550150", "tel:+12155550101"],
            },
            "message": {
                "Message": {
                    "parts": [
                        {
                            "part": {
                                "Attachment": {
                                    "name": "s.png",
                                    "mime": "image/png",
                                    "uti": "com.apple.sticker",
                                }
                            }
                        },
                        {"part": {"Text": ["hi there", {}]}},
                    ]
                }
            },
        },
    }
    tr = translate(event, MY)
    assert tr is not None
    assert tr.event is not None
    assert tr.event.body == f"{_OBJ_REPLACEMENT}hi there"
    assert tr.extras.attachments[0]["name"] == "s.png"


def test_live_photo_part_does_not_write_a_body_marker():
    """Photos remain free-standing; only stickers claim a body slot."""
    event = {
        "event": "message",
        "inst": {
            "id": "CC9AE249",
            "sender": "tel:+12155550101",
            "conversation": {
                "participants": ["tel:+12155550150", "tel:+12155550101"],
            },
            "message": {
                "Message": {
                    "parts": [
                        {
                            "part": {
                                "Attachment": {
                                    "name": "x.png",
                                    "mime": "image/png",
                                    "uti": "public.png",
                                }
                            }
                        },
                        {"part": {"Text": ["caption", {}]}},
                    ]
                }
            },
        },
    }
    tr = translate(event, MY)
    assert tr is not None
    assert tr.event.body == "caption"
    assert _OBJ_REPLACEMENT not in (tr.event.body or "")


def test_live_sticker_only_still_gets_filename_placeholder():
    """Until the bytes land, a sticker-only message needs something to show."""
    event = {
        "event": "message",
        "inst": {
            "id": "CC9AE249",
            "sender": "tel:+12155550101",
            "conversation": {
                "participants": ["tel:+12155550150", "tel:+12155550101"],
            },
            "message": {
                "Message": {
                    "parts": [
                        {
                            "part": {
                                "Attachment": {
                                    "name": "s.png",
                                    "mime": "image/png",
                                    "uti": "com.apple.sticker",
                                }
                            }
                        },
                    ]
                }
            },
        },
    }
    tr = translate(event, MY)
    assert tr is not None
    assert tr.event.body == "[s.png]"
    assert _OBJ_REPLACEMENT not in (tr.event.body or "")
