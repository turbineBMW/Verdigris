"""DaemonClient — the Qt UI's link to the running verdigris daemon.

D-Bus client for the daemon, built on QtDBus + Qt signals
instead of dbus-python + GLib, so it lives on the Qt event loop.

The daemon owns `dev.turbinebmw.Verdigris.Bridge` on the session bus. This client:
  • subscribes to its live signals (Events1 + Calls1) and re-emits them as
    Qt signals the QML layer binds to;
  • calls its methods (Messages1.Send, Calls1.Dial/Answer/Hangup, …)
    asynchronously so the UI never blocks;
  • reads message history from the SQLite message store (messages.sqlite).
"""
from __future__ import annotations

import json
import logging

from PySide6.QtCore import SLOT, QObject, QTimer, Signal, Slot
from PySide6.QtDBus import (
    QDBusConnection,
    QDBusInterface,
    QDBusMessage,
    QDBusPendingCallWatcher,
)

from verdigris.message_store import default_store

log = logging.getLogger(__name__)

BUS_NAME = "dev.turbinebmw.Verdigris.Bridge"
OBJECT_PATH = "/dev/turbinebmw/Verdigris/Bridge"
MESSAGES_IFACE = "dev.turbinebmw.Verdigris.Bridge.Messages1"
CALLS_IFACE = "dev.turbinebmw.Verdigris.Bridge.Calls1"
EVENTS_IFACE = "dev.turbinebmw.Verdigris.Bridge.Events1"

POLL_INTERVAL_MS = 5000
_CALL_TIMEOUT_MS = 60_000


def _plain(value):
    """Normalize QtDBus argument containers into plain Python values."""
    if hasattr(value, "toVariant"):  # QDBusArgument / QDBusVariant
        value = value.toVariant()
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


class DaemonClient(QObject):
    """Live link to the daemon. Emits Qt signals as D-Bus signals arrive."""

    messageReceived = Signal(dict)
    messageSent = Signal(dict)
    messageSeen = Signal(dict)
    ancsNotification = Signal(dict)
    # Delivery/read/typing. Updates an existing bubble rather than adding one,
    # so consumers match on `guid` (or `handle`, for typing).
    messageStateChanged = Signal(dict)
    callStateChanged = Signal(dict)
    availabilityChanged = Signal(bool)
    statusChanged = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._bus = QDBusConnection.sessionBus()
        self.available = False   # is the daemon reachable on D-Bus?
        self.healthy = False     # is the MAP session up?
        self._watchers: set[QDBusPendingCallWatcher] = set()

        self._subscribe()
        self.refresh_availability()

        # Poll so the UI reflects daemon/MAP state changes (restarts,
        # degraded-mode retries, ...) without the user having to hit Recheck.
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh_availability)
        self._timer.start(POLL_INTERVAL_MS)

    # ---- signal subscription -------------------------------------------

    def _subscribe(self) -> None:
        # Subscriptions work even before the daemon is up — delivery just
        # starts once it claims the bus name.
        # QtDBus wants a receiver + SLOT() signature here, not a bare callable.
        #
        # The signature must be QVariantMap, not QDBusMessage: with
        # QDBusMessage the a{sv} payload arrives as an unconvertible
        # QDBusArgument ("Cannot copy-convert ... to C++") and every signal
        # is delivered as an empty dict. Declaring QVariantMap makes Qt
        # demarshal the dictionary for us.
        for name, slot in (("MessageReceived", "_on_message_received"),
                           ("MessageSent", "_on_message_sent"),
                           ("MessageSeen", "_on_message_seen"),
                           ("MessageStateChanged", "_on_message_state"),
                           ("AncsNotification", "_on_ancs")):
            ok = self._bus.connect(BUS_NAME, OBJECT_PATH, EVENTS_IFACE, name,
                                   self, SLOT(f"{slot}(QVariantMap)"))
            if not ok:
                log.error("could not subscribe to %s", name)
        if not self._bus.connect(BUS_NAME, OBJECT_PATH, CALLS_IFACE,
                                 "CallStateChanged", self,
                                 SLOT("_on_call_state(QVariantMap)")):
            log.error("could not subscribe to CallStateChanged")

    @Slot("QVariantMap")
    def _on_message_received(self, props: dict) -> None:
        self.messageReceived.emit(_plain(dict(props)))

    @Slot("QVariantMap")
    def _on_message_sent(self, props: dict) -> None:
        self.messageSent.emit(_plain(dict(props)))

    @Slot("QVariantMap")
    def _on_message_seen(self, props: dict) -> None:
        self.messageSeen.emit(_plain(dict(props)))

    @Slot("QVariantMap")
    def _on_message_state(self, props: dict) -> None:
        self.messageStateChanged.emit(_plain(dict(props)))

    @Slot("QVariantMap")
    def _on_ancs(self, props: dict) -> None:
        self.ancsNotification.emit(_plain(dict(props)))

    @Slot("QVariantMap")
    def _on_call_state(self, props: dict) -> None:
        self.callStateChanged.emit(_plain(dict(props)))

    # ---- proxy helpers --------------------------------------------------

    def _iface(self, name: str) -> QDBusInterface:
        iface = QDBusInterface(BUS_NAME, OBJECT_PATH, name, self._bus, self)
        iface.setTimeout(_CALL_TIMEOUT_MS)
        return iface

    @Slot()
    def refresh_availability(self) -> bool:
        """Re-probe the daemon. Emits availabilityChanged on a reachability
        transition, and statusChanged on any change (reachability or MAP
        health) — called both on demand and from the background poll."""
        iface = self._iface(MESSAGES_IFACE)
        reachable, healthy = False, False
        if iface.isValid():
            reply = iface.call("IsHealthy")
            if reply.type() == QDBusMessage.MessageType.ReplyMessage:
                reachable = True
                args = reply.arguments()
                healthy = bool(args[0]) if args else False

        changed = reachable != self.available or healthy != self.healthy
        if reachable != self.available:
            self.available = reachable
            self.availabilityChanged.emit(reachable)
        self.healthy = healthy
        if changed:
            self.statusChanged.emit()
        return reachable

    def _call_async(self, iface_name: str, method: str, args: list,
                    on_ok, on_err) -> None:
        """Fire a method call without blocking the UI thread.

        QDBusPendingCallWatcher keeps the reply on the Qt event loop. We hold
        a reference to the watcher until it fires, otherwise Python may
        collect it mid-flight and the reply is lost.
        """
        iface = self._iface(iface_name)
        if not iface.isValid():
            on_err("daemon not reachable")
            return

        pending = iface.asyncCallWithArgumentList(method, args)
        watcher = QDBusPendingCallWatcher(pending, self)
        self._watchers.add(watcher)

        def _done(w: QDBusPendingCallWatcher) -> None:
            self._watchers.discard(w)
            reply = w.reply()
            if reply.type() == QDBusMessage.MessageType.ErrorMessage:
                on_err(reply.errorMessage() or "call failed")
            else:
                out = reply.arguments()
                on_ok(str(out[0]) if out else "")
            w.deleteLater()

        watcher.finished.connect(_done)

    # ---- Messages1 ------------------------------------------------------

    def send_message(self, recipient: str, body: str, on_ok, on_err) -> None:
        """Send asynchronously. on_ok(transfer_path) / on_err(text)."""
        self._call_async(MESSAGES_IFACE, "Send", [recipient, body],
                         on_ok, on_err)

    # ---- iMessage-only verbs --------------------------------------------
    #
    # Every one of these names its target by guid, which only messages that
    # arrived over the native transport have. Callers must check `im_guid` is
    # non-empty before offering the affordance — a MAP message can't be
    # reacted to, replied to, edited, or unsent, and the daemon will raise
    # rather than silently do nothing.

    def send_reply(self, recipient: str, body: str, reply_to_guid: str,
                   target_text: str, on_ok, on_err) -> None:
        """`target_text` is the replied-to message's text; Apple's reply
        format encodes a range over it. See `_reply_part` in dbus_service."""
        self._call_async(MESSAGES_IFACE, "SendReply",
                         [recipient, body, reply_to_guid, target_text],
                         on_ok, on_err)

    def react(self, recipient: str, target_guid: str, kind: str,
              target_text: str, on_ok, on_err) -> None:
        """Add a tapback. `kind` is a verb name ('Heart') or a literal emoji.

        `target_text` is the quoted snippet iOS renders in the synthesized
        'Loved "…"' line on devices too old for real tapbacks.
        """
        self._call_async(MESSAGES_IFACE, "React",
                         [recipient, target_guid, kind, target_text],
                         on_ok, on_err)

    def unreact(self, recipient: str, target_guid: str, kind: str,
                on_ok, on_err) -> None:
        self._call_async(MESSAGES_IFACE, "Unreact",
                         [recipient, target_guid, kind], on_ok, on_err)

    def edit_message(self, recipient: str, target_guid: str, new_text: str,
                     on_ok, on_err) -> None:
        self._call_async(MESSAGES_IFACE, "Edit",
                         [recipient, target_guid, new_text], on_ok, on_err)

    def unsend_message(self, recipient: str, target_guid: str,
                       on_ok, on_err) -> None:
        self._call_async(MESSAGES_IFACE, "Unsend",
                         [recipient, target_guid], on_ok, on_err)

    def set_typing(self, recipient: str, typing: bool) -> str | None:
        """Fire-and-forget: a lost typing indicator isn't worth reporting."""
        return self._call_sync(MESSAGES_IFACE, "SetTyping",
                               [recipient, bool(typing)])

    def dismiss_notifications(self, peers: str) -> None:
        """Close the desktop popups for a conversation we just opened.

        Fire-and-forget: this is cosmetic, and blocking the thread switch on a
        round trip to the daemon would be felt.
        """
        self._call_async(MESSAGES_IFACE, "DismissNotifications", [peers],
                         lambda *_: None, lambda *_: None)

    def set_active_thread(self, peer: str, focused: bool) -> None:
        """Tell the daemon which conversation is open and if we are focused.

        Fire-and-forget: used to suppress new-message popups for a thread that
        is already on screen. See Messages1.SetActiveThread.
        """
        self._call_async(MESSAGES_IFACE, "SetActiveThread",
                         [peer or "", bool(focused)],
                         lambda *_: None, lambda *_: None)

    def imessage_status(self) -> dict:
        """Whether the native transport is up, our handles, expiry warnings.

        Synchronous and cheap; the UI uses it to decide whether to offer the
        verbs above at all.
        """
        iface = self._iface(MESSAGES_IFACE)
        if not iface.isValid():
            return {"available": False}
        reply = iface.call("IMessageStatus")
        if reply.type() == QDBusMessage.MessageType.ErrorMessage:
            return {"available": False, "error": reply.errorMessage()}
        args = reply.arguments()
        try:
            return json.loads(str(args[0])) if args else {"available": False}
        except ValueError:
            return {"available": False}

    # ---- Calls1 ---------------------------------------------------------

    def dial(self, number: str, on_ok, on_err) -> None:
        self._call_async(CALLS_IFACE, "Dial", [number], on_ok, on_err)

    def answer_call(self, call_path: str) -> str | None:
        return self._call_sync(CALLS_IFACE, "AnswerCall", [call_path])

    def hangup_call(self, call_path: str) -> str | None:
        return self._call_sync(CALLS_IFACE, "HangupCall", [call_path])

    def hangup_all(self) -> str | None:
        return self._call_sync(CALLS_IFACE, "HangupAll", [])

    def _call_sync(self, iface_name: str, method: str,
                   args: list) -> str | None:
        """Blocking call for the quick ones. Returns an error string or None."""
        iface = self._iface(iface_name)
        if not iface.isValid():
            return "daemon not reachable"
        reply = iface.call(method, *args)
        if reply.type() == QDBusMessage.MessageType.ErrorMessage:
            text = reply.errorMessage() or "call failed"
            log.warning("%s failed: %s", method, text)
            return text
        return None

    def list_calls(self) -> list[dict]:
        iface = self._iface(CALLS_IFACE)
        if not iface.isValid():
            return []
        reply = iface.call("ListCalls")
        if reply.type() == QDBusMessage.MessageType.ErrorMessage:
            return []
        args = reply.arguments()
        try:
            return json.loads(str(args[0])) if args else []
        except ValueError:
            return []

    # ---- history (SQLite message store) ---------------------------------

    @staticmethod
    def read_events(kinds: set[str] | None = None,
                    limit: int | None = None,
                    after_id: int = 0) -> list[dict]:
        """Read message history from messages.sqlite, oldest-first.

        Optionally filter by `kind`. With `limit` and no `after_id`, returns
        the newest N events (still oldest-first). `after_id` is for the UI's
        incremental disk sync.
        """
        try:
            # Shared process-wide store — a fresh MessageStore() per call
            # opened a new sqlite connection (64 MiB page cache + 256 MiB
            # mmap) every 10s sync and never closed it until GC, which is
            # what pushed the UI's RSS into multi-GB territory.
            return default_store().read_events(
                kinds, after_id=after_id, limit=limit
            )
        except Exception as e:
            log.warning("could not read message store: %s", e)
            return []

    @staticmethod
    def read_events_with_ids(
        kinds: set[str] | None = None,
        limit: int | None = None,
        after_id: int = 0,
    ) -> list[tuple[int, dict]]:
        """Like read_events, but each item is `(row_id, event)` for cursors."""
        try:
            return default_store().read_events_with_ids(
                kinds, after_id=after_id, limit=limit
            )
        except Exception as e:
            log.warning("could not read message store: %s", e)
            return []
