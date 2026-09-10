"""Delivery/edit state must reach disk so a restart rebuilds captions/text."""
from __future__ import annotations

from pathlib import Path

import pytest

from verdigris.message_store import STATE_KINDS, MessageStore
from verdigris.sinks.sqlite import SqliteSink


@pytest.fixture
def sink(tmp_path: Path, monkeypatch):
    from verdigris import config

    db = tmp_path / "messages.sqlite"
    monkeypatch.setattr(config, "MESSAGES_DB", db)
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "EVENTS_JSONL", tmp_path / "events.jsonl")
    monkeypatch.setattr(
        config, "BACKUP_EVENTS_JSONL", tmp_path / "backup_events.jsonl"
    )
    return SqliteSink(path=db)


def test_delivery_state_is_written(sink: SqliteSink):
    sink.handle_state({
        "guid": "G1",
        "state": "delivered",
        "handle": "tel:+1555",
        "timestamp": "2026-07-30T12:00:00+00:00",
    })
    rows = sink.store.read_events(kinds=set(STATE_KINDS))
    assert len(rows) == 1
    assert rows[0]["kind"] == "message_state"
    assert rows[0]["state"] == "delivered"
    assert rows[0]["guid"] == "G1"
    assert rows[0]["peer_handle"] == "tel:+1555"
    assert rows[0]["handle"].startswith("state:")


def test_edit_state_carries_body(sink: SqliteSink):
    sink.handle_state({
        "guid": "G1",
        "state": "edited",
        "handle": "tel:+1555",
        "timestamp": "2026-07-30T12:00:00+00:00",
        "body": "see you at 6",
    })
    row = sink.store.read_events(kinds=set(STATE_KINDS))[0]
    assert row["state"] == "edited"
    assert row["body"] == "see you at 6"


def test_typing_is_not_persisted(sink: SqliteSink):
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
    assert sink.store.count(kinds=set(STATE_KINDS)) == 0


@pytest.mark.skipif(
    __import__("importlib").util.find_spec("PySide6") is None,
    reason="PySide6 not installed",
)
def test_ui_reloads_edit_and_delivery_from_disk(tmp_path: Path, monkeypatch):
    """The whole reason these lines exist."""
    from verdigris import config
    from verdigris.qtui.models import ThreadStore

    db = tmp_path / "messages.sqlite"
    monkeypatch.setattr(config, "MESSAGES_DB", db)
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "EVENTS_JSONL", tmp_path / "events.jsonl")
    monkeypatch.setattr(
        config, "BACKUP_EVENTS_JSONL", tmp_path / "backup_events.jsonl"
    )

    store = MessageStore(db)
    store.open()
    for row in (
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
    ):
        store.upsert_event(row)
    store.close()

    class FakeClient:
        def read_events(self, kinds=None, limit=None, after_id=0):
            from verdigris.qtui.client import DaemonClient
            return DaemonClient.read_events(
                kinds=kinds, limit=limit, after_id=after_id
            )

        def read_events_with_ids(self, kinds=None, limit=None, after_id=0):
            from verdigris.qtui.client import DaemonClient
            return DaemonClient.read_events_with_ids(
                kinds=kinds, limit=limit, after_id=after_id
            )

        messageReceived = type("S", (), {"connect": lambda *a, **k: None})()
        messageSent = messageReceived
        messageStateChanged = messageReceived

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
    s._last_event_id = 0
    s._client = FakeClient()

    for rid, ev in s._client.read_events_with_ids(
        kinds={"sms_received", "sms_sent"}
    ):
        s._ingest(ev, outgoing=(ev.get("kind") == "sms_sent"), refresh=False)
        s._last_event_id = max(s._last_event_id, rid)
    for rid, ev in s._client.read_events_with_ids(kinds={"message_state"}):
        s._ingest_state(ev, refresh=False)
        s._last_event_id = max(s._last_event_id, rid)

    msg = s._by_guid["AAAA"]
    assert msg["body"] == "see you at 6"
    assert msg["edits"] == ["see you at 5"]
    assert msg["state"] == "read"
