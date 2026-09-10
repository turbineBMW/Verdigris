from __future__ import annotations

import dbus

from verdigris import config
from verdigris.ancs.client import AncsClient
from verdigris.ancs.constants import CategoryID, EventFlag
from verdigris.ancs.events import AncsEvent
from verdigris.ancs.icons import _desktop_icon_index, app_icon
from verdigris.ancs.parsers import Notification, NotificationAttributes
from verdigris.sinks import libnotify


def _event(**changes) -> AncsEvent:
    values = {
        "notification_id": 7,
        "device_path": "/org/bluez/hci0/dev_PHONE",
        "app_id": "com.apple.reminders",
        "app_name": "Reminders",
        "title": "Record my weight",
        "subtitle": "Today",
        "body": "7:30 AM",
        "category": "Schedule",
        "is_silent": False,
        "is_preexisting": False,
        "positive_action": None,
        "negative_action": None,
    }
    values.update(changes)
    return AncsEvent(**values)


class _Notifications:
    def __init__(self) -> None:
        self.calls = []

    def Notify(self, *args):
        self.calls.append(args)
        return dbus.UInt32(1)


def test_ancs_notify_uses_source_app_identity_and_headline(monkeypatch):
    sink = libnotify.LibnotifySink.__new__(libnotify.LibnotifySink)
    sink._notif = _Notifications()
    monkeypatch.setattr(libnotify, "ancs_app_icon", lambda *_: "/tmp/reminders.png")

    sink.handle_ancs(_event())

    call = sink._notif.calls[0]
    assert call[0] == "Reminders"
    assert call[2] == "/tmp/reminders.png"
    assert call[3] == "Record my weight"
    assert call[4] == "Today — 7:30 AM"
    assert "desktop-entry" not in call[6]
    assert call[6]["category"] == "iphone.schedule"


def test_ancs_client_preserves_notification_source_metadata():
    emitted = []
    client = AncsClient("/org/bluez/hci0/dev_PHONE", emitted.append)
    client._pending_notifications[7] = Notification(
        id=7,
        type=0,
        flags=EventFlag.Silent,
        category=CategoryID.Schedule,
        category_count=1,
    )
    attrs = NotificationAttributes(
        id=7,
        app_id="com.apple.reminders",
        title="Record my weight",
        subtitle="",
        message="Today",
        positive_action=None,
        negative_action=None,
    )

    client._emit(attrs, "Reminders")

    assert emitted[0].category == "Schedule"
    assert emitted[0].is_silent is True
    assert 7 not in client._pending_notifications


def test_custom_bundle_icon_wins(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    icon_dir = tmp_path / "ancs_app_icons"
    icon_dir.mkdir()
    expected = icon_dir / "com.example.todo.png"
    expected.write_bytes(b"not decoded by the resolver")

    assert app_icon("com.example.todo", "Todo", "Schedule") == str(expected)


def test_matching_desktop_app_icon_is_used(monkeypatch, tmp_path):
    applications = tmp_path / "applications"
    applications.mkdir()
    (applications / "chat.desktop").write_text(
        "[Desktop Entry]\nName=Signal\nIcon=signal-desktop\n"
    )
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_DIRS", "")
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    _desktop_icon_index.cache_clear()
    try:
        assert app_icon("org.whispersystems.signal", "Signal", "Social") == "signal-desktop"
    finally:
        _desktop_icon_index.cache_clear()


def test_known_apple_app_uses_category_appropriate_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    assert app_icon("com.apple.reminders", "Reminders", "Schedule") == "view-task"
