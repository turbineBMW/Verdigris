"""Delivery/edit state must reach disk so a restart rebuilds captions/text."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from iphonebridge.sinks.jsonl import JsonlSink


@pytest.fixture
def sink(tmp_path: Path):
    return JsonlSink(path=tmp_path / "events.jsonl")


def test_delivery_state_is_written(sink: JsonlSink, tmp_path: Path):
    sink.handle_state({
        "guid": "G1",
        "state": "delivered",
        "handle": "tel:+1555",
        "timestamp": "2026-07-30T12:00:00+00:00",
    })
    rows = [json.loads(line) for line in sink.path.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["kind"] == "message_state"
    assert rows[0]["state"] == "delivered"
    assert rows[0]["guid"] == "G1"
    assert rows[0]["peer_handle"] == "tel:+1555"
    assert rows[0]["handle"].startswith("state:")


def test_edit_state_carries_body(sink: JsonlSink):
    sink.handle_state({
        "guid": "G1",
        "state": "edited",
        "handle": "tel:+1555",
        "timestamp": "2026-07-30T12:00:00+00:00",
        "body": "see you at 6",
    })
    row = json.loads(sink.path.read_text().strip())
    assert row["state"] == "edited"
    assert row["body"] == "see you at 6"


def test_typing_is_not_persisted(sink: JsonlSink):
    sink.handle_state({
        "guid": "",
        "state": "typing",
        "handle": "tel:+1555",
        "timestamp": "2026-07-30T12:00:00+00:00",
    })
    sink.handle_state({
        "guid": "",
        "state": "typing_stopped",
        "handle": "tel:+1555",
        "timestamp": "2026-07-30T12:00:01+00:00",
    })
    # File is only created on a real write — typing must not create it.
    assert not sink.path.exists()


@pytest.mark.skipif(
    __import__("importlib").util.find_spec("PySide6") is None,
    reason="PySide6 not installed",
)
def test_ui_reloads_edit_and_delivery_from_disk(tmp_path: Path, monkeypatch):
    """The whole reason these lines exist."""
    from iphonebridge import config
    from iphonebridge.qtui.models import ThreadStore

    log = tmp_path / "events.jsonl"
    rows = [
        {
            "kind": "sms_sent",
            "handle": "h1",
            "sender_phone": "+12155550150",
            "sender_phone_norm": "12155550150",
            "body": "see you at 5",
            "timestamp": "2026-07-30T12:00:00+00:00",
            "guid": "AAAA",
            "raw_type": "sms_sent",
        },
        {
            "kind": "message_state",
            "handle": "state:AAAA:edited:t1",
            "guid": "AAAA",
            "state": "edited",
            "peer_handle": "+12155550150",
            "timestamp": "2026-07-30T12:01:00+00:00",
            "body": "see you at 6",
        },
        {
            "kind": "message_state",
            "handle": "state:AAAA:read:t2",
            "guid": "AAAA",
            "state": "read",
            "peer_handle": "+12155550150",
            "timestamp": "2026-07-30T12:02:00+00:00",
            "body": "",
        },
    ]
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    monkeypatch.setattr(config, "EVENTS_JSONL", log)
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)

    class FakeClient:
        def read_events(self, kinds=None, limit=None):
            from iphonebridge.qtui.client import DaemonClient
            return DaemonClient.read_events(kinds=kinds, limit=limit)

        # ThreadStore connects these; unused here.
        messageReceived = type("S", (), {"connect": lambda *a, **k: None})()
        messageSent = messageReceived
        messageStateChanged = messageReceived

    # Minimal construction: bypass __init__ side effects where possible.
    s = ThreadStore.__new__(ThreadStore)
    s._threads = {}
    s._by_phone = {}
    s._by_guid = {}
    s._seen_handles = set()
    s._deleted = {}
    s._unsorted = set()
    s._current = ""
    s._read_marks = {}
    s._pending_receipts = {}
    s._client = FakeClient()

    # Exercise the same two-pass load history uses.
    for ev in s._client.read_events(kinds={"sms_received", "sms_sent"}):
        s._ingest(ev, outgoing=(ev.get("kind") == "sms_sent"), refresh=False)
    for ev in s._client.read_events(kinds={"message_state"}):
        s._ingest_state(ev, refresh=False)

    msg = s._by_guid["AAAA"]
    assert msg["body"] == "see you at 6"
    assert msg["edits"] == ["see you at 5"]
    assert msg["state"] == "read"
