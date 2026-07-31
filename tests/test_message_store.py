"""SQLite message store — write path, state, migration, incremental read."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from iphonebridge.events import SmsEvent
from iphonebridge.message_store import MESSAGE_KINDS, STATE_KINDS, MessageStore
from iphonebridge.sinks.sqlite import SqliteSink


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> MessageStore:
    from iphonebridge import config

    db = tmp_path / "messages.sqlite"
    monkeypatch.setattr(config, "MESSAGES_DB", db)
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "EVENTS_JSONL", tmp_path / "events.jsonl")
    monkeypatch.setattr(
        config, "BACKUP_EVENTS_JSONL", tmp_path / "backup_events.jsonl"
    )
    s = MessageStore(db)
    s.open()
    return s


def test_upsert_and_read_roundtrip(store: MessageStore):
    store.upsert_event({
        "kind": "sms_received",
        "handle": "h1",
        "guid": "G1",
        "body": "hello",
        "timestamp": "2026-07-30T12:00:00+00:00",
        "sender_phone": "+1555",
    })
    rows = store.read_events(kinds=set(MESSAGE_KINDS))
    assert len(rows) == 1
    assert rows[0]["body"] == "hello"
    assert rows[0]["guid"] == "G1"


def test_upsert_by_handle_is_idempotent(store: MessageStore):
    store.upsert_event({
        "kind": "sms_received",
        "handle": "guid:AAAA",
        "guid": "AAAA",
        "body": "v1",
        "timestamp": "2026-07-30T12:00:00+00:00",
    }, source="backup")
    store.upsert_event({
        "kind": "sms_received",
        "handle": "guid:AAAA",
        "guid": "AAAA",
        "body": "v2",
        "timestamp": "2026-07-30T12:00:00+00:00",
        "attachments": [{"path": "/tmp/x.jpg"}],
    }, source="backup")
    assert store.count() == 1
    assert store.read_events()[0]["body"] == "v2"
    assert store.read_events()[0]["attachments"]


def test_live_placeholder_does_not_wipe_backup_attachments(store: MessageStore):
    """The bug: backup lands the image, then live rewrites with [name.ext]."""
    store.upsert_event({
        "kind": "sms_received",
        "handle": "guid:8E546279-8F48-4640-B624-DA25B32AA5E6",
        "guid": "8E546279-8F48-4640-B624-DA25B32AA5E6",
        "body": "",
        "timestamp": "2026-07-30T16:31:08+00:00",
        "sender_phone": "+12672343106",
        "attachments": [{
            "path": "/tmp/IMG_0103.jpeg",
            "mime": "image/jpeg",
            "name": "IMG_0103.jpeg",
            "w": 1061, "h": 650,
        }],
    }, source="backup")
    store.upsert_event({
        "kind": "sms_received",
        "handle": "8E546279-8F48-4640-B624-DA25B32AA5E6",
        "guid": "8E546279-8F48-4640-B624-DA25B32AA5E6",
        "body": "[IMG_0103.jpeg]",
        "timestamp": "2026-07-30T16:34:15+00:00",
        "sender_phone": "+12672343106",
        "chat_name": "the fucky ",
    }, source="live")
    row = store.read_events()[0]
    assert row["attachments"][0]["path"] == "/tmp/IMG_0103.jpeg"
    # Placeholder caption is dropped once real media is present.
    assert row["body"] == ""


def test_attachment_state_folds_into_message_row(store: MessageStore):
    store.upsert_event({
        "kind": "sms_received",
        "handle": "guid:CC9AE249",
        "guid": "CC9AE249",
        "body": "[x.png]",
        "timestamp": "2026-07-30T12:00:00+00:00",
        "sender_phone": "+1555",
    })
    store.upsert_state({
        "guid": "CC9AE249",
        "state": "attachments",
        "handle": "tel:+1555",
        "timestamp": "2026-07-30T12:00:01+00:00",
        "body": json.dumps([{
            "name": "x.png", "mime": "image/png", "path": "/tmp/x.png",
            "w": 10, "h": 10,
        }]),
    })
    msgs = [e for e in store.read_events() if e.get("kind") == "sms_received"]
    assert msgs[0]["attachments"][0]["path"] == "/tmp/x.png"
    assert msgs[0]["body"] == ""


def test_state_persists_and_skips_typing(store: MessageStore):
    store.upsert_state({
        "guid": "G1",
        "state": "delivered",
        "handle": "tel:+1555",
        "timestamp": "2026-07-30T12:00:00+00:00",
    })
    store.upsert_state({
        "guid": "",
        "state": "typing",
        "handle": "tel:+1555",
        "timestamp": "2026-07-30T12:00:01+00:00",
    })
    rows = store.read_events(kinds=set(STATE_KINDS))
    assert len(rows) == 1
    assert rows[0]["state"] == "delivered"
    assert rows[0]["peer_handle"] == "tel:+1555"
    assert rows[0]["handle"].startswith("state:")


def test_sqlite_sink_writes_event_and_state(tmp_path: Path, monkeypatch):
    from iphonebridge import config
    from datetime import datetime, timezone

    db = tmp_path / "messages.sqlite"
    monkeypatch.setattr(config, "MESSAGES_DB", db)
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "EVENTS_JSONL", tmp_path / "events.jsonl")
    monkeypatch.setattr(
        config, "BACKUP_EVENTS_JSONL", tmp_path / "backup_events.jsonl"
    )

    sink = SqliteSink(path=db)
    sink.handle(SmsEvent(
        kind="sms_sent",
        handle="xfer1",
        sender_phone="+1555",
        sender_phone_norm="1555",
        contact_name=None,
        body="hi",
        timestamp=datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc),
        is_read=True,
        raw_status=None,
        raw_type="sms_sent",
        guid="G1",
    ))
    sink.handle_state({
        "guid": "G1",
        "state": "read",
        "handle": "+1555",
        "timestamp": "2026-07-30T12:01:00+00:00",
    })
    sink.handle_state({
        "guid": "",
        "state": "typing",
        "handle": "+1555",
        "timestamp": "2026-07-30T12:01:01+00:00",
    })

    msgs = sink.store.read_events(kinds=set(MESSAGE_KINDS))
    states = sink.store.read_events(kinds=set(STATE_KINDS))
    assert len(msgs) == 1
    assert msgs[0]["body"] == "hi"
    assert len(states) == 1
    assert states[0]["state"] == "read"


def test_search_finds_body_with_highlight(store: MessageStore):
    store.upsert_event({
        "kind": "sms_received",
        "handle": "s1",
        "body": "can you bring the lasagna later?",
        "timestamp": "2026-07-30T12:00:00+00:00",
        "sender_phone": "+15551212",
        "sender_phone_norm": "15551212",
        "contact_name": "Sam",
    })
    store.upsert_event({
        "kind": "sms_received",
        "handle": "s2",
        "body": "unrelated pizza night",
        "timestamp": "2026-07-30T12:01:00+00:00",
        "sender_phone": "+15551212",
        "sender_phone_norm": "15551212",
    })
    hits = store.search("lasagna")
    assert len(hits) == 1
    assert "lasagna" in hits[0]["previewPlain"].lower()
    assert "<b>" in hits[0]["preview"]
    assert hits[0]["threadKey"].startswith("tel:")


def test_messages_page_and_threads(store: MessageStore):
    for i in range(5):
        store.upsert_event({
            "kind": "sms_received",
            "handle": f"p{i}",
            "body": f"msg {i}",
            "timestamp": f"2026-07-30T12:0{i}:00+00:00",
            "sender_phone": "+15559999",
            "sender_phone_norm": "15559999",
        })
    threads = store.list_threads()
    assert len(threads) == 1
    key = threads[0]["key"]
    assert key == "tel:15559999"
    page = store.messages_page(key, limit=3)
    assert len(page) == 3
    # Oldest-first within the newest window (by time, not rowid).
    bodies = [ev["body"] for _, ev in page]
    assert bodies == ["msg 2", "msg 3", "msg 4"]
    edge_epoch = page[0][1].get("_ts_epoch") or 0
    older = store.messages_page(
        key, before_epoch=edge_epoch, before_id=page[0][0], limit=10
    )
    assert [ev["body"] for _, ev in older] == ["msg 0", "msg 1"]


def test_group_not_named_after_last_sender(store: MessageStore):
    store.upsert_event({
        "kind": "sms_received",
        "handle": "guid:G1",
        "guid": "G1",
        "body": "hi group",
        "timestamp": "2026-07-31T12:00:00+00:00",
        "sender_phone": "+15551111",
        "sender_phone_norm": "15551111",
        "contact_name": "aiden",
        "chat_name": "the fucky",
        "group_key": "imessage-group:tel:+15551111,tel:+15552222",
        "is_group": True,
    })
    t = store.list_threads()[0]
    assert t["is_group"]
    assert t["name"] == "the fucky"
    assert t["phone"] is None


def test_live_updates_thread_time_even_with_low_rowid(store: MessageStore):
    """Backup fills high ids; live upserts can reuse low ids — time must win."""
    store.upsert_event({
        "kind": "sms_received",
        "handle": "guid:OLD",
        "guid": "OLD",
        "body": "old backup",
        "timestamp": "2026-07-30T12:00:00+00:00",
        "sender_phone": "+15553333",
        "sender_phone_norm": "15553333",
        "contact_name": "sam",
    }, source="backup")
    # Simulate a later live message that somehow keeps a lower id path by
    # using a different handle but same thread — and a second message with
    # an earlier id via update.
    store.upsert_event({
        "kind": "sms_received",
        "handle": "guid:NEW",
        "guid": "NEW",
        "body": "brand new today",
        "timestamp": "2026-07-31T18:00:00+00:00",
        "sender_phone": "+15553333",
        "sender_phone_norm": "15553333",
        "contact_name": "sam",
    }, source="live")
    t = store.list_threads()[0]
    assert "brand new" in t["last_preview"]
    assert t["last_ts"].startswith("2026-07-31")
    page = store.messages_page(t["key"], limit=10)
    assert page[-1][1]["body"] == "brand new today"


def test_after_id_incremental(store: MessageStore):
    store.upsert_event({
        "kind": "sms_received", "handle": "a", "body": "1",
        "timestamp": "2026-07-30T12:00:00+00:00",
    })
    store.upsert_event({
        "kind": "sms_received", "handle": "b", "body": "2",
        "timestamp": "2026-07-30T12:01:00+00:00",
    })
    first = store.read_events_with_ids(kinds=set(MESSAGE_KINDS))
    assert len(first) == 2
    mid = first[0][0]
    rest = store.read_events_with_ids(kinds=set(MESSAGE_KINDS), after_id=mid)
    assert len(rest) == 1
    assert rest[0][1]["body"] == "2"


def test_migrate_from_jsonl(tmp_path: Path, monkeypatch):
    from iphonebridge import config

    db = tmp_path / "messages.sqlite"
    events = tmp_path / "events.jsonl"
    backup = tmp_path / "backup_events.jsonl"
    monkeypatch.setattr(config, "MESSAGES_DB", db)
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "EVENTS_JSONL", events)
    monkeypatch.setattr(config, "BACKUP_EVENTS_JSONL", backup)

    events.write_text(json.dumps({
        "kind": "sms_received",
        "handle": "map1",
        "body": "live",
        "timestamp": "2026-07-30T12:00:00+00:00",
    }) + "\n" + json.dumps({
        "kind": "ancs_notification",
        "handle": "ancs1",
        "body": "skip me",
    }) + "\n")
    backup.write_text(json.dumps({
        "kind": "sms_sent",
        "handle": "guid:BBBB",
        "guid": "BBBB",
        "body": "from phone",
        "source": "backup",
        "timestamp": "2026-07-30T11:00:00+00:00",
        "attachments": [],
    }) + "\n")

    store = MessageStore(db)
    store.open()
    rows = store.read_events(kinds=set(MESSAGE_KINDS))
    bodies = {r["body"] for r in rows}
    assert bodies == {"live", "from phone"}
    assert store.count() == 2  # ancs skipped

    # Second open does not double-import.
    store.close()
    store2 = MessageStore(db)
    store2.open()
    assert store2.count() == 2


@pytest.mark.skipif(
    __import__("importlib").util.find_spec("PySide6") is None,
    reason="PySide6 not installed",
)
def test_ui_reloads_edit_and_delivery_from_sqlite(tmp_path: Path, monkeypatch):
    """State rows in SQLite rebuild captions/text after a restart."""
    from iphonebridge import config
    from iphonebridge.qtui.models import ThreadStore

    db = tmp_path / "messages.sqlite"
    monkeypatch.setattr(config, "MESSAGES_DB", db)
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "EVENTS_JSONL", tmp_path / "events.jsonl")
    monkeypatch.setattr(
        config, "BACKUP_EVENTS_JSONL", tmp_path / "backup_events.jsonl"
    )

    store = MessageStore(db)
    store.open()
    store.upsert_event({
        "kind": "sms_sent",
        "handle": "h1",
        "sender_phone": "+12155550150",
        "sender_phone_norm": "12155550150",
        "body": "see you at 5",
        "timestamp": "2026-07-30T12:00:00+00:00",
        "guid": "AAAA",
        "raw_type": "sms_sent",
    })
    store.upsert_event({
        "kind": "message_state",
        "handle": "state:AAAA:edited:t1",
        "guid": "AAAA",
        "state": "edited",
        "peer_handle": "+12155550150",
        "timestamp": "2026-07-30T12:01:00+00:00",
        "body": "see you at 6",
    })
    store.upsert_event({
        "kind": "message_state",
        "handle": "state:AAAA:read:t2",
        "guid": "AAAA",
        "state": "read",
        "peer_handle": "+12155550150",
        "timestamp": "2026-07-30T12:02:00+00:00",
        "body": "",
    })
    store.close()

    class FakeClient:
        def read_events(self, kinds=None, limit=None, after_id=0):
            from iphonebridge.qtui.client import DaemonClient
            return DaemonClient.read_events(
                kinds=kinds, limit=limit, after_id=after_id
            )

        def read_events_with_ids(self, kinds=None, limit=None, after_id=0):
            from iphonebridge.qtui.client import DaemonClient
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


def test_count_unread_uses_epoch_not_string_order(store: MessageStore):
    """Mixed offsets must not resurrect a cleared badge after restart.

    A message at 15:41 EDT stored as `…T19:41…+00:00` sorts *after* a read
    mark at 16:07 EDT stored as `…T16:07…-04:00` under lexical ISO compare,
    so counting with `ts > ?` falsely re-lights unread on every cold start
    (the path pinned tiles take — their archive is not loaded).
    """
    key_phone = "15551234001"
    # Earlier wall-clock message, UTC spelling that sorts *later* as text.
    store.upsert_event({
        "kind": "sms_received",
        "handle": "u1",
        "body": "earlier",
        "timestamp": "2026-07-31T19:41:43.617000+00:00",  # 15:41 EDT
        "sender_phone": f"+{key_phone}",
        "sender_phone_norm": key_phone,
    })
    # Later wall-clock message — the one the user actually read.
    store.upsert_event({
        "kind": "sms_received",
        "handle": "u2",
        "body": "later",
        "timestamp": "2026-07-31T16:07:06.492493-04:00",  # 20:07 UTC
        "sender_phone": f"+{key_phone}",
        "sender_phone_norm": key_phone,
    })
    key = f"tel:{key_phone}"
    mark = "2026-07-31T16:07:06.492493-04:00"
    # Lexical `ts > mark` would count the 19:41 UTC row; epoch must not.
    assert store.count_unread(key, mark) == 0
    # Without a mark, both incoming messages are unread.
    assert store.count_unread(key, None) == 2
    # A mark before both leaves both unread.
    assert store.count_unread(key, "2026-07-31T12:00:00-04:00") == 2
    # Mark between them leaves only the later one.
    assert store.count_unread(key, "2026-07-31T16:00:00-04:00") == 1
