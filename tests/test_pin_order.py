"""Pinned conversations keep the order the user dragged them into.

Pins used to be a `set` written out sorted, so the grid rendered in the
sidebar's recency order and any arrangement the user made was lost on the
next message. These tests pin the ordering contract `movePin` relies on:
indices come from the rendered grid, which skips pins whose thread no longer
exists, so they cannot be applied to the stored list directly.
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from verdigris.qtui.models import ThreadStore


def make_store(pinned, rows=None, saves=None):
    """A ThreadStore with only the pin bookkeeping wired up.

    __new__ rather than __init__: the real one opens D-Bus, file watchers and
    Qt models, none of which this touches.
    """
    s = ThreadStore.__new__(ThreadStore)
    s._pinned = list(pinned)
    s._thread_rows = [{"threadKey": k} for k in (rows if rows is not None else pinned)]
    s._threads = {k: {} for k in (rows if rows is not None else pinned)}
    s._save_pinned = lambda: (saves.append(list(s._pinned)) if saves is not None else None)
    s.pinsChanged = type("Sig", (), {"emit": staticmethod(lambda: None)})()
    return s


def keys(store):
    return [r["threadKey"] for r in store._pinned_rows()]


def test_move_forward():
    s = make_store(["a", "b", "c", "d"])
    assert s.movePin(0, 2) is True
    assert keys(s) == ["b", "c", "a", "d"]


def test_move_backward():
    s = make_store(["a", "b", "c", "d"])
    assert s.movePin(3, 1) is True
    assert keys(s) == ["a", "d", "b", "c"]


def test_move_to_same_slot_is_a_no_op():
    s = make_store(["a", "b", "c"])
    assert s.movePin(1, 1) is False
    assert keys(s) == ["a", "b", "c"]


def test_out_of_range_is_rejected():
    """The grid clamps, but a stale index must never reorder anything."""
    s = make_store(["a", "b"])
    assert s.movePin(0, 5) is False
    assert s.movePin(-1, 0) is False
    assert keys(s) == ["a", "b"]


def test_indices_are_grid_relative_not_list_relative():
    """A pin whose thread has gone away is invisible in the grid.

    Index 1 in the grid is "c", not the stored list's "b" — applying the
    drop to `_pinned` directly would move the wrong conversation.
    """
    s = make_store(["a", "b", "c"], rows=["a", "c"])
    assert keys(s) == ["a", "c"]
    assert s.movePin(1, 0) is True
    assert keys(s) == ["c", "a"]
    # The orphan is kept, so re-appearing threads stay pinned.
    assert "b" in s._pinned


def test_reorder_is_persisted():
    saves = []
    s = make_store(["a", "b", "c"], saves=saves)
    s.movePin(2, 0)
    assert saves == [["c", "a", "b"]]


def test_load_tolerates_the_old_sorted_format(tmp_path, monkeypatch):
    """The file predates ordering; older builds wrote a plain sorted list."""
    import json

    from verdigris.qtui import models

    f = tmp_path / "pinned_threads.json"
    f.write_text(json.dumps(["b", "a", "a", 7, "c"]))
    monkeypatch.setattr(models, "_PINNED_FILE", f)
    # Order preserved as written, duplicates and non-strings dropped.
    assert ThreadStore._load_pinned() == ["b", "a", "c"]


def test_load_survives_a_corrupt_file(tmp_path, monkeypatch):
    from verdigris.qtui import models

    f = tmp_path / "pinned_threads.json"
    f.write_text("{not json")
    monkeypatch.setattr(models, "_PINNED_FILE", f)
    assert ThreadStore._load_pinned() == []
