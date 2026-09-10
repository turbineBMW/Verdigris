"""Clipboard sink — auto-copies verification codes from incoming texts.

When an SMS/iMessage carries a one-time / 2FA code, lift it onto the system
clipboard and show a short confirmation, so the code can be pasted without
picking up the phone.
"""
from __future__ import annotations

import logging

import dbus
import dbus.exceptions

from verdigris.bus import session_bus
from verdigris.clipboard import copy_to_clipboard, extract_verification_code
from verdigris.events import SmsEvent

log = logging.getLogger(__name__)

_APP_NAME = "verdigris"


class ClipboardSink:
    name = "clipboard"

    def __init__(self) -> None:
        try:
            self._notif = dbus.Interface(
                session_bus.get_object(
                    "org.freedesktop.Notifications",
                    "/org/freedesktop/Notifications",
                ),
                "org.freedesktop.Notifications",
            )
        except dbus.exceptions.DBusException:
            self._notif = None
        log.info("clipboard sink ready (auto-copies verification codes)")

    def handle(self, event: SmsEvent) -> None:
        # Only incoming messages — never our own sent texts.
        if event.kind != "sms_received":
            return
        code = extract_verification_code(event.body)
        if code is None:
            return
        tool = copy_to_clipboard(code)
        if tool is None:
            log.warning(
                "verification code %s detected but no clipboard tool worked "
                "— install wl-clipboard (Wayland) or xclip (X11)", code)
            return
        log.info("verification code %s copied to clipboard (via %s)", code, tool)
        self._notify(code, event)

    def _notify(self, code: str, event: SmsEvent) -> None:
        if self._notif is None:
            return
        try:
            self._notif.Notify(
                _APP_NAME,
                dbus.UInt32(0),
                "edit-paste-symbolic",
                f"\U0001f4cb Code copied: {code}",
                f"from {event.display_sender} — paste with Ctrl+V",
                dbus.Array([], signature="s"),
                dbus.Dictionary({"urgency": dbus.Byte(1)}, signature="sv"),
                dbus.Int32(8000),  # transient — auto-expire after 8s
            )
        except dbus.exceptions.DBusException as e:
            log.debug("clipboard notify failed: %s", e.get_dbus_name())
