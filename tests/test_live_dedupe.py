"""Live ingest must collapse MAP ↔ iMessage twins without a restart.

Reload already dedupes in `_materialize_events` (guid + same-body window).
Live `_ingest` used to only dedupe by event handle, so a group message that
arrived once over MAP and once over iMessage drew two bubbles until the app
was restarted and the open thread re-paged from SQLite.
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from iphonebridge.qtui.models import MessageListModel, ThreadStore

GROUP_KEY = "imessage-group:tel:+15551110001,tel:+15551110002"


def _store(*, group: bool = True) -> ThreadStore:
    s = ThreadStore.__new__(ThreadStore)
    key = GROUP_KEY if group else "tel:+15551110001"
    s._threads = {
        key: {
            "key": key,
            "name": "the fucky" if group else "alex",
            "messages": [],
            "messages_loaded": True,
            "is_group": group,
            "last_ts": "",
            "last_preview": "",
            "phone": None if group else "+15551110001",
            "unread": 0,
        }
    }
    s._by_phone = {}
    if not group:
        s._by_phone["15551110001"] = key
        s._by_phone["+15551110001"] = key
    s._seen_handles = set()
    s._deleted = {}
    s._by_guid = {}
    s._unsorted = set()
    s._current = key
    s._pending_receipts = {}
    s._read_marks = {}
    s._highlight_event_id = 0
    s._highlight_guid = ""
    s._message_model = MessageListModel()
    s._contacts = type("C", (), {
        "resolve_photo": lambda self, p: None,
        "resolve": lambda self, p: None,
    })()
    s._pinned = []
    s._refresh_pending = type("T", (), {"start": lambda self: None})()
    return s


def _ev(
    *,
    handle: str,
    body: str,
    guid: str = "",
    raw_type: str = "iMessage",
    kind: str = "sms_received",
    sender: str = "+15551110001",
    ts: str = "2026-07-31T12:00:00+00:00",
    chat_guid: str = GROUP_KEY,
) -> dict:
    return {
        "kind": kind,
        "handle": handle,
        "body": body,
        "guid": guid or None,
        "im_guid": guid or None,
        "raw_type": raw_type,
        "timestamp": ts,
        "sender_phone": sender,
        "sender_phone_norm": sender.lstrip("+"),
        "contact_name": "alex",
        "chat_guid": chat_guid,
        "chat_name": "the fucky ",
    }


def test_same_guid_second_handle_does_not_double():
    """Apple can deliver one guid on more than one of our registered handles."""
    s = _store()
    s._ingest(_ev(handle="guid:AAAA", body="yo", guid="AAAA"),
              outgoing=False, refresh=False)
    s._ingest(_ev(handle="guid:AAAA#other", body="yo", guid="AAAA"),
              outgoing=False, refresh=False)
    assert len(s._threads[s._current]["messages"]) == 1


def test_map_then_imessage_upgrades_guid_in_place():
    """Daemon still emits the guid-bearing twin so receipts can bind."""
    s = _store()
    s._ingest(_ev(
        handle="message123", body="on my way", guid="",
        raw_type="SMS_GSM",
    ), outgoing=False, refresh=False)
    s._ingest(_ev(
        handle="guid:BBBB", body="on my way", guid="BBBB",
        raw_type="iMessage",
    ), outgoing=False, refresh=False)
    msgs = s._threads[s._current]["messages"]
    assert len(msgs) == 1
    assert msgs[0]["guid"] == "BBBB"
    assert "BBBB" in s._by_guid


def test_imessage_then_map_drops_guidless_echo():
    s = _store()
    s._ingest(_ev(
        handle="guid:CCCC", body="here", guid="CCCC",
        raw_type="iMessage",
    ), outgoing=False, refresh=False)
    s._ingest(_ev(
        handle="message999", body="here", guid="",
        raw_type="SMS_GSM",
    ), outgoing=False, refresh=False)
    assert len(s._threads[s._current]["messages"]) == 1


def test_two_people_same_text_in_group_are_not_collapsed():
    s = _store()
    s._ingest(_ev(
        handle="guid:D1", body="ok", guid="D1",
        sender="+15551110001",
    ), outgoing=False, refresh=False)
    s._ingest(_ev(
        handle="guid:D2", body="ok", guid="D2",
        sender="+15551110002",
        ts="2026-07-31T12:00:05+00:00",
    ), outgoing=False, refresh=False)
    assert len(s._threads[s._current]["messages"]) == 2


def test_our_own_repeated_sends_still_keep_each_bubble():
    """sms_sent must not content-dedupe — see test_send_echo."""
    # thread_key_for normalizes to digits-only tel: form.
    key = "tel:15551110001"
    s = _store(group=False)
    s._threads = {
        key: {
            "key": key,
            "name": "alex",
            "messages": [],
            "messages_loaded": True,
            "is_group": False,
            "last_ts": "",
            "last_preview": "",
            "phone": "15551110001",
            "unread": 0,
        }
    }
    s._current = key
    s._by_phone = {"15551110001": key, "+15551110001": key}
    for i, h in enumerate(("sent-a", "sent-b", "sent-c")):
        s._ingest({
            "kind": "sms_sent",
            "handle": h,
            "body": "ok",
            "guid": "",
            "raw_type": "sms_sent",
            "timestamp": f"2026-07-31T12:00:{i:02d}+00:00",
            "sender_phone": "+15551110001",
            "sender_phone_norm": "15551110001",
            "chat_guid": None,
        }, outgoing=True, refresh=False)
    assert len(s._threads[key]["messages"]) == 3
