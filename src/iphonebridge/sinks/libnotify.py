"""Desktop notification sink via org.freedesktop.Notifications.

Body format: title = display sender (contact name or phone number),
             body  = SMS text (truncated at ~280 chars to avoid huge popups).

Persistence model — notifications stay visible until ONE of:
  • The user dismisses the popup (clicks/swipes)        → we mark-read on iPhone
  • The iPhone marks the message read (user opens it)  → we auto-close popup
That way an unread message is never "missed" on the desktop side.

Read-state sync:
  Linux dismiss → MAP Message1.Properties.Set(Read=true)  → iPhone marks read
  iPhone reads  → MAP PropertiesChanged(Read=true)         → we close popup

Empirical on iOS 26.5: both directions work. iPhone propagates Read=true
back over MAP within a few seconds of opening the Messages app.
"""
from __future__ import annotations

import logging
from pathlib import Path

import dbus
import dbus.exceptions

from iphonebridge.ancs.events import AncsEvent
from iphonebridge.avatars import circular as circular_avatar
from iphonebridge.bus import session_bus
from iphonebridge.events import SmsEvent, normalize_phone

log = logging.getLogger(__name__)

_APP_NAME = "Messages"
_BODY_LIMIT = 280

# Basename of our .desktop file. Passing this as the `desktop-entry` hint is
# what makes Plasma show our icon and group the popups under "Messages" —
# a bare icon *name* only works if it exists in the active icon theme, and
# the GNOME-style names we used before don't exist in Breeze.
_DESKTOP_ENTRY = "com.gabriel.iphonebridge.Qt"
_ICON_FILE = (Path.home() / ".local/share/icons/hicolor/scalable/apps"
              / "com.gabriel.iphonebridge.UI.svg")


def _app_icon() -> str:
    """Absolute path if our icon is installed, else a theme name."""
    if _ICON_FILE.exists():
        return str(_ICON_FILE)
    return "mail-message-new"


def _escape_markup(text: str) -> str:
    """Escape for the body, which Plasma parses as markup."""
    return (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;"))

# NotificationClosed reason codes (org.freedesktop.Notifications spec):
#   1 = expired (timeout)
#   2 = dismissed by user
#   3 = CloseNotification() called programmatically (e.g. by us on iPhone-read)
#   4 = undefined / reserved
#
# We mark-read only on dismissed-by-user. Reason 3 = we're already closing
# because the iPhone marked it read (so we'd be in a write-self-write loop).
# Reason 1 = expired, but with timeout=0 this shouldn't happen for us.
_REASON_DISMISSED = 2


class LibnotifySink:
    name = "libnotify"

    def __init__(self, hfp=None, send_message=None, contacts=None) -> None:
        # Optional HfpManager — when present, incoming-call popups carry
        # Answer / Decline action buttons wired straight to it.
        self._hfp = hfp
        # send_message(recipient, body) — enables inline reply straight from
        # the notification, which Plasma supports natively.
        self._send_message = send_message
        # ContactsResolver — used to put the sender's photo on the popup.
        self._contacts = contacts
        # notification_id → phone number, so a reply knows who to answer.
        self._reply_targets: dict[int, str] = {}
        # normalized peer → the notification ids currently on screen for them.
        # Opening that conversation in the UI is a read, and reading is what
        # these popups are waiting for — see `dismiss_for`.
        self._peer_notifs: dict[str, set[int]] = {}
        self._notif = dbus.Interface(
            session_bus.get_object(
                "org.freedesktop.Notifications",
                "/org/freedesktop/Notifications",
            ),
            "org.freedesktop.Notifications",
        )
        # notification_id (uint32 from Notify) -> Message1 DBus path
        self._pending: dict[int, str] = {}
        # notification_id -> SignalMatch for the per-Message1 PropertiesChanged sub
        self._msg_subs: dict[int, object] = {}
        # Incoming-call popups: call_path <-> notification_id
        self._call_notifs: dict[str, int] = {}
        self._notif_calls: dict[int, str] = {}

        # Listen for any of our notifications closing (dismissed, expired,
        # or programmatically closed).
        self._match = self._notif.connect_to_signal(
            "NotificationClosed", self._on_closed,
        )
        # Listen for action-button clicks (Answer / Decline on call popups).
        self._action_match = self._notif.connect_to_signal(
            "ActionInvoked", self._on_action,
        )
        # Plasma-specific: fires when the user types into the inline reply
        # box and hits send. Not part of the freedesktop spec, so it simply
        # never fires on servers that don't implement it.
        try:
            self._reply_match = self._notif.connect_to_signal(
                "NotificationReplied", self._on_reply,
            )
        except Exception:
            self._reply_match = None
            log.debug("notification server has no NotificationReplied signal")
        log.info(
            "libnotify sink ready (persistent + bidirectional read-sync)")

    @staticmethod
    def _peer_key(handle: str | None) -> str | None:
        """Comparable form of a sender handle.

        The UI knows a thread by whatever the message carried, the popup by
        whatever the transport gave us, and for the same person those differ
        (`+1 215 555 0150` against `tel:+12155550150`). Digits-only for phone
        numbers, lowercased for email handles, so both sides land on the same
        string.
        """
        if not handle:
            return None
        text = handle.strip()
        if "@" in text:
            return text.lower()
        digits = normalize_phone(text)
        if not digits:
            return None
        # Last ten, so a bare 2155550150 matches a +12155550150 that came in
        # with its country code. Whether one is present depends on which
        # transport delivered the message, and the two halves of this are fed
        # by different transports.
        return digits[-10:] if len(digits) > 10 else digits

    def dismiss_for(self, peer: str) -> int:
        """Close the popups for a conversation the user has just read.

        Without this they never close on the iMessage path at all: the only
        automatic close is a BlueZ `Message1.Read` property change, which is a
        MAP object, and an iMessage arrives with no such object to watch. So
        every notification stayed on screen until dismissed by hand, which is
        what "they persist" was.

        Local only. Nothing here tells Apple anything — read receipts are a
        separate decision, gated on SEND_READ_RECEIPTS, and opening a thread
        must not silently disclose that you read it.
        """
        # Comma-separated, because a group conversation notifies under each
        # member who spoke and opening it has to clear all of them.
        keys = {k for k in (self._peer_key(p) for p in peer.split(",")) if k}
        closed = 0
        for key in keys:
            for nid in sorted(self._peer_notifs.get(key, ())):
                try:
                    self._notif.CloseNotification(dbus.UInt32(nid))
                    closed += 1
                except dbus.exceptions.DBusException as e:
                    log.debug("CloseNotification(%d): %s",
                              nid, e.get_dbus_name())
            # `_on_closed` prunes the bookkeeping, but only for ids the server
            # actually reports. Drop the bucket either way so a popup the
            # server has already forgotten can't leak.
            self._peer_notifs.pop(key, None)
        if closed:
            log.info("closed %d popup(s) for %s (thread opened)", closed, peer)
        return closed

    @staticmethod
    def _base_hints() -> dict:
        return {
            "urgency": dbus.Byte(1),
            "desktop-entry": dbus.String(_DESKTOP_ENTRY),
            # No x-kde-display-appname / x-kde-origin-name here: Plasma
            # already derives the app name from `desktop-entry`, and setting
            # either of them makes the header read "Messages · Messages".
        }

    def _photo_for(self, event: SmsEvent):
        """Circular contact photo for the sender, if we have one cached."""
        if self._contacts is None:
            return None
        try:
            src = self._contacts.resolve_photo(
                event.sender_phone or event.sender_phone_norm)
        except Exception:
            log.debug("photo lookup failed", exc_info=True)
            return None
        return circular_avatar(src) if src else None

    def handle(self, event: SmsEvent) -> None:
        # Don't pop a desktop notification for a message we ourselves sent.
        if event.kind == "sms_sent":
            return
        # No emoji prefix — the app icon already identifies these, and the
        # sender's name reads better on its own.
        title = event.display_sender
        body = (event.body or "").strip()
        if len(body) > _BODY_LIMIT:
            body = body[:_BODY_LIMIT - 1] + "…"
        # Body is rendered as markup by Plasma, so raw text must be escaped
        # or a stray '&' or '<' silently swallows the rest of the message.
        body = _escape_markup(body)

        hints = self._base_hints()
        photo = self._photo_for(event)
        if photo:
            hints["image-path"] = dbus.String(f"file://{photo}")

        # Plasma renders a real reply box when this action is present.
        actions: list[str] = []
        recipient = event.sender_phone or event.sender_phone_norm
        if self._send_message is not None and recipient:
            actions = ["inline-reply", "Reply"]
            hints["x-kde-reply-placeholder-text"] = dbus.String(
                f"Reply to {event.display_sender}…")

        try:
            # expire_timeout=0 → notification stays visible indefinitely.
            # We close it ourselves when the iPhone marks the message read,
            # or rely on the user to dismiss it manually (which we then
            # propagate back as mark-read).
            nid = int(self._notif.Notify(
                _APP_NAME,
                dbus.UInt32(0),
                _app_icon(),
                title,
                body,
                dbus.Array(actions, signature="s"),
                dbus.Dictionary(hints, signature="sv"),
                dbus.Int32(0),  # 0 = never expire
            ))
        except dbus.exceptions.DBusException as e:
            log.error("libnotify Notify failed: %s", e.get_dbus_name())
            return

        if recipient:
            self._reply_targets[nid] = recipient
        # Keyed on the sender, not the thread: a group message notifies as the
        # person who sent it, and that is the handle the UI will hand back.
        peer_key = self._peer_key(recipient) or self._peer_key(
            event.sender_phone_norm)
        if peer_key:
            self._peer_notifs.setdefault(peer_key, set()).add(nid)

        if event.message_path:
            self._pending[nid] = event.message_path
            # Subscribe to PropertiesChanged on this specific Message1 path
            # so we get notified if iOS marks it read.
            self._msg_subs[nid] = session_bus.add_signal_receiver(
                lambda iface, changed, _inv, nid=nid:
                    self._on_msg_props(nid, iface, changed),
                dbus_interface="org.freedesktop.DBus.Properties",
                signal_name="PropertiesChanged",
                path=event.message_path,
            )

    # ---- ANCS events (per-app notifications) ----------------------------

    def handle_ancs(self, event: AncsEvent) -> None:
        # Title: "📱 AppName" or "📱 com.bundle.id" if no name yet
        app = event.app_name or event.app_id or "Notification"
        title = f"\U0001f4f1 {app}"
        # Body: prefer Title field for headline, then Message
        body_parts = [p for p in (event.title, event.body) if p]
        body = " — ".join(body_parts) if body_parts else ""
        if len(body) > _BODY_LIMIT:
            body = body[:_BODY_LIMIT - 1] + "…"
        try:
            # ANCS notifications also persistent (timeout=0). User dismisses
            # or we close on demand. No mark-read sync for ANCS (the iPhone
            # doesn't expose a write-back path for app notification state).
            self._notif.Notify(
                _APP_NAME,
                dbus.UInt32(0),
                _app_icon(),
                title,
                _escape_markup(body),
                dbus.Array([], signature="s"),
                dbus.Dictionary(self._base_hints(), signature="sv"),
                dbus.Int32(0),
            )
        except dbus.exceptions.DBusException as e:
            log.error("libnotify Notify (ANCS) failed: %s", e.get_dbus_name())

    # ---- HFP call events (incoming-call popups with actions) ------------

    def handle_call(self, event) -> None:
        if event.kind == "call_incoming":
            self._show_incoming_call(event)
        elif event.kind in ("call_active", "call_ended"):
            # Answered (here or on the phone) or ended — drop the ringing popup.
            self._close_call_notif(event.call_path)

    def _show_incoming_call(self, event) -> None:
        if event.call_path in self._call_notifs:
            return  # already showing a popup for this call
        title = f"\U0001f4de {event.display_peer}"
        # Action buttons are only useful if we can actually act on them.
        if self._hfp is not None:
            actions = dbus.Array(
                ["answer", "Answer", "decline", "Decline"], signature="s")
        else:
            actions = dbus.Array([], signature="s")
        try:
            hints = self._base_hints()
            hints["urgency"] = dbus.Byte(2)   # critical: a ringing phone
            nid = int(self._notif.Notify(
                _APP_NAME,
                dbus.UInt32(0),
                "call-start",
                title,
                "Incoming call",
                actions,
                dbus.Dictionary(hints, signature="sv"),
                dbus.Int32(0),  # 0 = never expire (we close it ourselves)
            ))
        except dbus.exceptions.DBusException as e:
            log.error("libnotify Notify (call) failed: %s", e.get_dbus_name())
            return
        self._call_notifs[event.call_path] = nid
        self._notif_calls[nid] = event.call_path

    def _close_call_notif(self, call_path: str) -> None:
        nid = self._call_notifs.pop(call_path, None)
        if nid is None:
            return
        self._notif_calls.pop(nid, None)
        try:
            self._notif.CloseNotification(dbus.UInt32(nid))
        except dbus.exceptions.DBusException:
            pass

    def _on_reply(self, nid, text) -> None:
        """User typed a reply straight into the notification popup."""
        try:
            nid_i = int(nid)
        except (TypeError, ValueError):
            return
        recipient = self._reply_targets.get(nid_i)
        body = str(text).strip()
        if not recipient or not body or self._send_message is None:
            return
        try:
            self._send_message(recipient, body)
            log.info("sent inline reply to %s (%d chars)", recipient, len(body))
        except Exception:
            log.exception("inline reply failed")

    def _on_action(self, nid, action_key) -> None:
        try:
            nid_i = int(nid)
        except (TypeError, ValueError):
            return
        call_path = self._notif_calls.get(nid_i)
        if call_path is None or self._hfp is None:
            return
        action = str(action_key)
        try:
            if action == "answer":
                self._hfp.answer(call_path)
                log.info("answered call from notification: %s", call_path)
            elif action == "decline":
                self._hfp.hangup(call_path)
                log.info("declined call from notification: %s", call_path)
        except Exception as e:
            log.warning("call action %r failed: %s", action, e)

    # ---- iPhone marks read → close our popup ----------------------------

    def _on_msg_props(self, nid: int, iface: str, changed) -> None:
        if iface != "org.bluez.obex.Message1":
            return
        # Look for Read going True. Some BlueZ versions send Status instead.
        read_now = (
            bool(changed.get("Read", False))
            or str(changed.get("Status", "")).lower() in ("read", "complete")
        )
        if not read_now:
            return
        if nid not in self._pending:
            return  # already closed/handled
        try:
            self._notif.CloseNotification(dbus.UInt32(nid))
            log.info("iPhone marked message read — closed popup %d", nid)
        except dbus.exceptions.DBusException as e:
            log.debug("CloseNotification(%d): %s", nid, e.get_dbus_name())
        # _on_closed will clean up the dict + signal match (reason=3)

    # ---- Linux user dismisses → mark-read on iPhone ----------------------

    def _on_closed(self, nid, reason) -> None:
        try:
            nid_i = int(nid)
            reason_i = int(reason)
        except (TypeError, ValueError):
            return

        message_path = self._pending.pop(nid_i, None)
        target = self._reply_targets.pop(nid_i, None)
        key = self._peer_key(target)
        if key is not None:
            bucket = self._peer_notifs.get(key)
            if bucket is not None:
                bucket.discard(nid_i)
                if not bucket:
                    self._peer_notifs.pop(key, None)

        # Clean up call-popup bookkeeping if this was an incoming-call popup.
        call_path = self._notif_calls.pop(nid_i, None)
        if call_path is not None:
            self._call_notifs.pop(call_path, None)

        # Always remove the per-message subscription, no matter the reason
        sub = self._msg_subs.pop(nid_i, None)
        if sub is not None:
            try:
                sub.remove()
            except Exception:
                pass

        if message_path is None:
            return

        # Only propagate read-state to iPhone when the human actively
        # dismissed (reason=2). Don't loop on programmatic close (reason=3,
        # which is fired when we closed it ourselves because iPhone already
        # marked it read).
        if reason_i != _REASON_DISMISSED:
            return
        try:
            dbus.Interface(
                session_bus.get_object("org.bluez.obex", message_path),
                "org.freedesktop.DBus.Properties",
            ).Set("org.bluez.obex.Message1", "Read", dbus.Boolean(True))
            log.info("marked %s as read on iPhone (user dismissed popup)",
                     message_path.rsplit("/", 1)[-1])
        except dbus.exceptions.DBusException as e:
            log.debug("mark-read failed for %s: %s",
                      message_path, e.get_dbus_name())
