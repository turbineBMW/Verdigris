from __future__ import annotations

import dbus
import pytest

from verdigris import config
from verdigris.ancs.client import AncsClient
from verdigris.ancs.constants import CategoryID, EventFlag
from verdigris.ancs.events import AncsEvent
from verdigris.ancs.icons import _desktop_icon_index, app_icon
from verdigris.ancs.parsers import Notification, NotificationAttributes
from verdigris.sinks import libnotify


@pytest.fixture(autouse=True)
def isolated_notification_preferences(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))


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


def test_rules_reload_and_keep_last_good_values(monkeypatch, tmp_path):
    import json

    from verdigris.ancs.preferences import Preferences, Rule

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    root = tmp_path / "verdigris"
    root.mkdir()
    path = root / "notification-rules.json"
    prefs = Preferences()
    assert prefs.rule("com.example.chat") == Rule()
    path.write_text(json.dumps({"version": 1, "apps": {"com.example.chat": {
        "enabled": False, "desktop_id": "org.example.Chat.desktop", "icon": "custom.png",
    }}}))
    assert not prefs.rule("com.example.chat").enabled
    assert prefs.rule("com.example.chat").desktop_id == "org.example.Chat.desktop"
    path.write_text('{"version":1,"apps":{"com.example.chat":{"enabled":"false"}}}')
    assert not prefs.rule("com.example.chat").enabled
    path.write_text('{"version":2,"apps":{}}')
    assert not prefs.rule("com.example.chat").enabled
    path.write_text('{"version":1,"apps":{}}')
    assert prefs.rule("com.example.chat").enabled


def test_disabled_notification_never_reaches_sinks_or_dbus(monkeypatch, tmp_path):
    import json
    from unittest.mock import Mock

    from verdigris import daemon
    from verdigris.ancs.preferences import Rule

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(daemon.preferences, "rule", lambda _: Rule(enabled=False))
    instance = daemon.Daemon.__new__(daemon.Daemon)
    sink = Mock()
    instance.sinks = [sink]
    instance._dbus_service = Mock()
    instance._fanout_ancs(_event())
    sink.handle_ancs.assert_not_called()
    instance._dbus_service.emit_ancs.assert_not_called()
    registry = tmp_path / "verdigris/notification-apps.json"
    assert json.loads(registry.read_text()) == {"com.apple.reminders": "Reminders"}
    assert "weight" not in registry.read_text()
    assert registry.stat().st_mode & 0o777 == 0o600
    monkeypatch.setattr(daemon.preferences, "rule", lambda _: Rule())
    instance._fanout_ancs(_event())
    sink.handle_ancs.assert_called_once()
    instance._dbus_service.emit_ancs.assert_called_once()


def test_notification_custom_icon_and_click_target(monkeypatch, tmp_path):
    from unittest.mock import Mock

    from verdigris.ancs.preferences import Rule

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    icon = tmp_path / "verdigris/notification-icons/custom.png"
    icon.parent.mkdir(parents=True)
    icon.write_bytes(b"fixture")
    rule = Rule(desktop_id="org.example.Chat.desktop", icon="custom.png")
    monkeypatch.setattr(libnotify.preferences, "rule", lambda _: rule)
    app = Mock()
    desktop = Mock()
    desktop.new.return_value = app
    monkeypatch.setattr(libnotify, "Gio", Mock(DesktopAppInfo=desktop))
    sink = libnotify.LibnotifySink.__new__(libnotify.LibnotifySink)
    sink._notif = _Notifications()
    sink.handle_ancs(_event())
    call = sink._notif.calls[0]
    assert call[2] == str(icon)
    assert list(call[5]) == ["default", "Open app"]
    sink._on_action(1, "unexpected")
    app.launch.assert_not_called()
    sink._on_action(1, "default")
    desktop.new.assert_called_once_with("org.example.Chat.desktop")
    app.launch.assert_called_once_with([], None)
    rule = Rule(enabled=False)
    sink._on_action(1, "default")
    app.launch.assert_called_once()
    sink.handle_ancs(_event())
    assert len(sink._notif.calls) == 1


def test_notification_target_cleanup_and_missing_app(monkeypatch):
    from unittest.mock import Mock

    from verdigris.ancs.preferences import Rule

    monkeypatch.setattr(libnotify.preferences, "rule", lambda _: Rule(desktop_id="gone.desktop"))
    monkeypatch.setattr(libnotify, "Gio", Mock(DesktopAppInfo=Mock(new=Mock(return_value=None))))
    sink = libnotify.LibnotifySink.__new__(libnotify.LibnotifySink)
    sink._notif = _Notifications()
    sink.handle_ancs(_event())
    sink._on_action(1, "default")  # An uninstalled app must not crash the daemon.
    for field in ("_pending", "_reply_targets", "_peer_notifs", "_notif_calls", "_call_notifs", "_msg_subs"):
        setattr(sink, field, {})
    sink._on_closed(1, 2)
    assert sink._ancs_targets == {}


def test_custom_icon_cannot_escape_directory(monkeypatch, tmp_path):
    from verdigris.ancs.preferences import Rule

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    for name in ("..", "../outside.png", "/tmp/icon.png", "nested/icon.png"):
        assert Rule(icon=name).icon_path() is None
    assert Rule(icon="missing.png").icon_path() is None
