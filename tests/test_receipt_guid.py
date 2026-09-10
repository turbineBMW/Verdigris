"""Phone-sent messages must still accept Delivered/Read captions.

MAP often lands first without a guid; iMessage then brings the same text with
a guid. Dropping the second copy left a bubble that receipts could never
find. Receipts can also race ahead of the message entirely.
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from verdigris.qtui.models import ThreadStore


def store():
    s = ThreadStore.__new__(ThreadStore)
    s._threads = {}
    s._by_phone = {}
    s._by_guid = {}
    s._pending_receipts = {}
    s._seen_handles = set()
    s._deleted = {}
    s._unsorted = set()
    s._current = ""
    s._read_marks = {}
    return s


def map_sent(body, ts="2026-07-30T12:00:00+00:00"):
    return {
        "handle": f"map-{body}",
        "kind": "sms_sent",
        "sender_phone": "+12155559999",
        "sender_phone_norm": "12155559999",
        "body": body,
        "timestamp": ts,
        "raw_type": "SMS_GSM",
    }


def im_sent(body, guid, ts="2026-07-30T12:00:01+00:00"):
    return {
        "handle": guid,
        "guid": guid,
        "kind": "sms_sent",
        "sender_phone": "+12155559999",
        "sender_phone_norm": "12155559999",
        "body": body,
        "timestamp": ts,
        "raw_type": "iMessage",
    }


def test_map_first_then_imessage_attaches_guid():
    s = store()
    s._ingest(map_sent("from phone"), outgoing=True, refresh=False)
    s._ingest(im_sent("from phone", "GUID-1"), outgoing=True, refresh=False)

    thread = next(iter(s._threads.values()))
    assert len(thread["messages"]) == 1, "must not draw a second bubble"
    msg = thread["messages"][0]
    assert msg["guid"] == "GUID-1"
    assert s._by_guid["GUID-1"] is msg


def test_receipt_before_message_is_applied_when_guid_lands():
    s = store()
    s._apply_state(
        state="delivered", guid="GUID-2", peer="tel:+1",
        body="", timestamp="2026-07-30T12:00:05+00:00", refresh=False,
    )
    assert "GUID-2" in s._pending_receipts

    s._ingest(im_sent("hello", "GUID-2"), outgoing=True, refresh=False)
    msg = s._by_guid["GUID-2"]
    assert msg["state"] == "delivered"
    assert msg["state_ts"] == "2026-07-30T12:00:05+00:00"
    assert "GUID-2" not in s._pending_receipts


def test_receipt_after_map_upgrade_captions():
    """The full phone-send race: MAP → Delivered → iMessage guid."""
    s = store()
    s._ingest(map_sent("hey"), outgoing=True, refresh=False)
    s._apply_state(
        state="delivered", guid="GUID-3", peer="tel:+1",
        body="", timestamp="t", refresh=False,
    )
    # Receipt is pending — MAP row still has no guid.
    assert next(iter(s._threads.values()))["messages"][0].get("state") == ""

    s._ingest(im_sent("hey", "GUID-3"), outgoing=True, refresh=False)
    msg = next(iter(s._threads.values()))["messages"][0]
    assert msg["guid"] == "GUID-3"
    assert msg["state"] == "delivered"


def test_read_is_not_downgraded_by_late_delivered():
    s = store()
    s._ingest(im_sent("x", "G"), outgoing=True, refresh=False)
    s._apply_state(state="read", guid="G", peer="", body="", timestamp="t1",
                   refresh=False)
    s._apply_state(state="delivered", guid="G", peer="", body="",
                   timestamp="t2", refresh=False)
    assert s._by_guid["G"]["state"] == "read"
