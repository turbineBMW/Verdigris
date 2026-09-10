"""Incoming iMessage attachments arrive by reference and must be fetched.

The message carries an MMCS locator and a key, not the bytes. Nothing fetched
them, so a photo or sticker rendered as the bridge's `[name.png]` fallback
text — the placeholder a received image showed up as.

The fetched result rides the state channel, like an edit, because it modifies
a message that already exists. That also gets persistence for free, so the
image survives a restart.
"""
from __future__ import annotations

import json

from verdigris.imessage.bridge import translate
from verdigris.qtui.models import ThreadStore

MY = ["tel:+12155550150"]


def _event(parts: list) -> dict:
    return {"event": "message", "inst": {
        "id": "CC9AE249", "sender": "tel:+12155550101",
        "conversation": {"participants": ["tel:+12155550150",
                                          "tel:+12155550101"]},
        "message": {"Message": {"parts": parts}},
    }}


def test_the_locator_never_crosses_into_python():
    """Attachments are downloaded by (guid, index), not by handing the object
    back to the helper.

    It cannot make the round trip: the MMCS locator and key are `Vec<u8>`
    fields serialized as plist `Data`, and JSON renders them as a sequence of
    numbers, which rustpush's `bin_deserialize` rejects — the real failure was
    `invalid type: sequence, expected a byte array`. Keeping it out of Python
    also keeps key material off disk.
    """
    source = {"name": "x.png", "mime": "image/png", "uti_type": "public.png",
              "a_type": {"MMCS": {"size": 1234, "key": [1, 2, 3]}}}
    tr = translate(_event([{"part": {"Attachment": source}}]), MY)
    att = tr.extras.attachments[0]
    assert "_source" not in att
    assert not any(isinstance(v, (list, dict)) for v in att.values())
    # Still describes itself for the placeholder before the bytes land.
    assert att["name"] == "x.png"


def test_attachment_order_is_stable():
    """`index` on the download request is a position in this list, so it has
    to match the order the helper collects parts in."""
    tr = translate(_event([
        {"part": {"Attachment": {"name": "a.png"}}},
        {"part": {"Text": ["between", {}]}},
        {"part": {"Attachment": {"name": "b.png"}}},
    ]), MY)
    assert [a["name"] for a in tr.extras.attachments] == ["a.png", "b.png"]


def test_attachment_only_message_still_gets_placeholder_text():
    """Shown until the download completes, so the bubble isn't blank."""
    tr = translate(_event([{"part": {"Attachment": {"name": "x.png"}}}]), MY)
    assert tr.event.body == "[x.png]"


def _store_with_message(body: str = "[x.png]") -> tuple[ThreadStore, dict]:
    store = ThreadStore.__new__(ThreadStore)
    store._threads = {"k": {"key": "k", "messages": [], "name": "e"}}
    store._current = None
    msg = {"body": body, "ts": "2026-07-30T21:50:07+00:00", "outgoing": False,
           "attachments": [], "guid": "CC9AE249"}
    store._threads["k"]["messages"].append(msg)
    store._by_guid = {"CC9AE249": msg}
    store._seen_handles = set()
    return store, msg


def _apply(store, body, guid="CC9AE249"):
    store._apply_state(state="attachments", guid=guid, peer="",
                       body=body, timestamp="", refresh=False)


def test_downloaded_attachment_reaches_the_message():
    store, msg = _store_with_message()
    _apply(store, json.dumps([{"name": "x.png", "mime": "image/png",
                               "path": "/tmp/x.png", "w": 100, "h": 80}]))
    assert msg["attachments"][0]["path"] == "/tmp/x.png"


def test_placeholder_text_is_cleared_once_the_image_lands():
    """Otherwise every photo keeps a filename caption under it."""
    store, msg = _store_with_message("[x.png]")
    _apply(store, json.dumps([{"name": "x.png", "path": "/tmp/x.png"}]))
    assert msg["body"] == ""


def test_a_real_caption_is_kept():
    """Only the synthesized placeholder is dropped — text the sender actually
    typed alongside the photo has to survive."""
    store, msg = _store_with_message("look at this")
    _apply(store, json.dumps([{"name": "x.png", "path": "/tmp/x.png"}]))
    assert msg["body"] == "look at this"


def test_failed_download_leaves_the_message_alone():
    """A descriptor with no path never made it to disk; drawing it would give
    a broken image where the placeholder at least says something."""
    store, msg = _store_with_message()
    _apply(store, json.dumps([{"name": "x.png"}]))
    assert msg["attachments"] == []
    assert msg["body"] == "[x.png]"


def test_unknown_guid_is_ignored():
    store, msg = _store_with_message()
    _apply(store, json.dumps([{"name": "x.png", "path": "/tmp/x.png"}]),
           guid="NOPE")
    assert msg["attachments"] == []


def test_malformed_payload_does_not_raise():
    store, msg = _store_with_message()
    _apply(store, "{not json")
    assert msg["attachments"] == []


def _rows_store() -> ThreadStore:
    s = ThreadStore.__new__(ThreadStore)
    s._threads = {}
    s._current = None
    s._by_guid = {}
    s._highlight_event_id = 0
    return s


def _row_msg(body: str, atts: list[dict]) -> dict:
    return {
        "body": body,
        "ts": "2026-07-30T21:50:07+00:00",
        "outgoing": False,
        "reactions": {},
        "attachments": atts,
        "guid": "g1",
        "reply_to": "",
        "sender_name": "",
        "sender_phone": "",
        "edits": [],
        "event_id": 0,
    }


def test_file_url_percent_encodes_spaces():
    """Spaces in attachment paths must not produce a broken Image source."""
    from verdigris.qtui.models import _file_url

    assert _file_url("/tmp/my photo.png") == "file:///tmp/my%20photo.png"
    assert _file_url("") == ""
    assert _file_url(None) == ""
    assert _file_url("/tmp/x.png") == "file:///tmp/x.png"


def test_image_row_without_mime_uses_extension():
    """Empty mime + .jpg path still draws as an image, not a paperclip chip."""
    store = _rows_store()
    rows = store._rows_for(_row_msg("", [{
        "path": "/tmp/photo.jpg",
        "name": "photo.jpg",
        "mime": "",
        "is_sticker": False,
        "w": 100,
        "h": 80,
    }]), None)
    assert [r["kind"] for r in rows] == ["image"]
    assert rows[0]["image"] == "file:///tmp/photo.jpg"
    # Name kept so QML can fall back to a file chip if decode fails.
    assert rows[0]["fileLabel"] == "photo.jpg"
    assert rows[0]["animated"] is False


def test_gif_row_is_marked_animated():
    """QML needs `animated` so free-standing media uses AnimatedImage."""
    from verdigris.qtui.models import _is_animated_att

    assert _is_animated_att({"mime": "image/gif", "name": "x.bin", "path": ""})
    assert _is_animated_att({"mime": "", "name": "loop.gif", "path": ""})
    assert _is_animated_att({
        "mime": "", "name": "payload", "path": "/tmp/clip.gif",
    })
    assert not _is_animated_att({
        "mime": "image/png", "name": "x.png", "path": "/tmp/x.png",
    })

    store = _rows_store()
    rows = store._rows_for(_row_msg("", [{
        "path": "/tmp/funny.gif",
        "name": "funny.gif",
        "mime": "image/gif",
        "is_sticker": False,
        "w": 200,
        "h": 150,
    }]), None)
    assert [r["kind"] for r in rows] == ["image"]
    assert rows[0]["animated"] is True
    assert rows[0]["image"] == "file:///tmp/funny.gif"


def test_animated_gif_sticker_is_marked_too():
    """Stickers can be GIF loops; they share the media path in QML."""
    store = _rows_store()
    rows = store._rows_for(_row_msg("", [{
        "path": "/tmp/sticker.gif",
        "name": "sticker.gif",
        "mime": "image/gif",
        "is_sticker": True,
        "w": 100,
        "h": 100,
    }]), None)
    assert [r["kind"] for r in rows] == ["sticker"]
    assert rows[0]["animated"] is True


def test_image_without_path_is_a_file_chip_not_invisible_media():
    """kind=image with an empty source used to vanish in QML (no chip)."""
    store = _rows_store()
    rows = store._rows_for(_row_msg("", [{
        "path": "",
        "name": "x.png",
        "mime": "image/png",
        "is_sticker": False,
    }]), None)
    assert [r["kind"] for r in rows] == ["file"]
    assert rows[0]["fileLabel"] == "x.png"
    assert rows[0]["image"] == ""


def test_placeholder_body_cleared_when_building_rows():
    """Even if state left body as [name], don't caption under the photo."""
    store = _rows_store()
    rows = store._rows_for(_row_msg("[x.png]", [{
        "path": "/tmp/x.png",
        "name": "x.png",
        "mime": "image/png",
        "is_sticker": False,
        "w": 10,
        "h": 10,
    }]), None)
    assert [r["kind"] for r in rows] == ["image"]
    assert all(r.get("body") != "[x.png]" for r in rows)
