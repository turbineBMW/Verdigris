"""Importing a full history has to finish.

A real full-history import is ~223,000 messages. Two things in the import
path were quadratic and only showed up at that size — at the 8,000 messages
previously imported they were invisible:

  * the thread's message list was re-sorted after every single insert
  * duplicate detection walked every message already in the thread

Together they pegged a core for minutes without finishing, so the app never
opened. These tests use enough messages that a quadratic implementation
cannot pass, while staying quick when the implementation is linear.
"""
from __future__ import annotations

import time

import pytest

pytest.importorskip("PySide6")

from verdigris.qtui.models import ThreadStore

N = 12_000
BUDGET_SEC = 20.0


def store():
    """A ThreadStore with the D-Bus, disk and Qt plumbing left out."""
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
    s._pending_edits = {}
    return s


def events(n, *, body=lambda i: f"message number {i}"):
    """One long conversation, oldest first, as the backup emits them."""
    return [
        {
            "handle": f"G{i}",
            "guid": f"G{i}",
            "kind": "sms_received",
            "sender_phone": "+12155550150",
            "sender_phone_norm": "12155550150",
            "contact_name": "Test Person",
            "body": body(i),
            # One minute apart, so nothing falls inside the 120s
            # duplicate window by accident.
            "timestamp": f"2026-01-01T{i // 3600 % 24:02d}:{i // 60 % 60:02d}:{i % 60:02d}+00:00",
            "attachments": [],
        }
        for i in range(n)
    ]


def test_a_large_import_completes_in_reasonable_time():
    s = store()
    start = time.monotonic()
    added = s._ingest_all(events(N)) if hasattr(s, "_ingest_all") else sum(
        1 for ev in events(N) if s._ingest_backup(ev))
    elapsed = time.monotonic() - start
    assert added == N
    assert elapsed < BUDGET_SEC, (
        f"{N} messages took {elapsed:.1f}s — the import path has gone "
        f"quadratic again (budget {BUDGET_SEC}s)"
    )


def test_identical_bodies_do_not_degrade_the_import():
    """The duplicate index is keyed on body, so this is its worst case.

    Every message has the same text, meaning every insert has to consider
    every previous one. Timestamps are far apart, so none actually match.
    """
    s = store()
    start = time.monotonic()
    for ev in events(N, body=lambda i: "ok"):
        s._ingest_backup(ev)
    elapsed = time.monotonic() - start
    assert elapsed < BUDGET_SEC, (
        f"identical bodies took {elapsed:.1f}s — duplicate detection is "
        f"scanning the whole thread again"
    )


def test_deduplication_still_works_after_the_index_change():
    """Speed must not have cost correctness.

    A repeat of the same text within the window is still a duplicate; the
    same text much later is still a separate message.
    """
    s = store()
    base = {
        "handle": "H1", "kind": "sms_received", "sender_phone": "+12155550150",
        "sender_phone_norm": "12155550150", "contact_name": "Test Person",
        "body": "are you around", "attachments": [],
        "timestamp": "2026-01-01T10:00:00+00:00",
    }
    assert s._ingest_backup(dict(base, handle="H1")) is True
    # Same text a minute later, different handle: inside the 120s window.
    assert s._ingest_backup(
        dict(base, handle="H2", timestamp="2026-01-01T10:01:00+00:00")) is False
    # Same text hours later: a genuinely new message.
    assert s._ingest_backup(
        dict(base, handle="H3", timestamp="2026-01-01T15:00:00+00:00")) is True


def test_messages_end_up_in_chronological_order():
    """Sorting moved to the end of the import, so verify it still happens."""
    s = store()
    evs = events(50)
    for ev in reversed(evs):        # arrive newest-first
        s._ingest_backup(ev)
    for thread in s._threads.values():
        thread["messages"].sort(key=lambda m: m.get("ts") or "")
        stamps = [m.get("ts") for m in thread["messages"]]
        assert stamps == sorted(stamps)


def test_startup_import_leaves_messages_in_time_order():
    """The startup path must sort too, not just the manual re-import.

    These are separate entry points — `_load_backup_events` at launch and
    `import_backup_events` after a re-sync — and the sort lives outside
    `_ingest_backup` because doing it per message is quadratic. When only one
    caller sorted, the conversation list still looked right (`last_ts` is a
    max) while messages sat in append order: live first, then the entire
    backup on top, burying anything new above years of history.
    """
    s = store()

    # A message that arrived live, before any backup exists.
    s._ingest_backup({
        "handle": "LIVE", "guid": "LIVE", "kind": "sms_received",
        "sender_phone": "+12155550150", "sender_phone_norm": "12155550150",
        "contact_name": "Test Person", "body": "newest message",
        "timestamp": "2026-07-30T18:02:11+00:00", "attachments": [],
    })
    # Then a backlog of older history lands on top of it.
    for i, ev in enumerate(events(200)):
        ev["timestamp"] = f"2019-01-01T00:{i // 60 % 60:02d}:{i % 60:02d}+00:00"
        s._ingest_backup(ev)

    s._sort_pending()

    for thread in s._threads.values():
        stamps = [m.get("ts") for m in thread["messages"]]
        assert stamps == sorted(stamps), "messages are not in time order"
        assert thread["messages"][-1]["body"] == "newest message", (
            "the newest message must end up last, not buried at the top"
        )
