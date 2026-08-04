"""DBus service the daemon exposes on the session bus for CLI clients.

Bus name:    com.gabriel.iphonebridge
Object path: /com/gabriel/iphonebridge
Interfaces:  com.gabriel.iphonebridge.Messages1   — messaging
             com.gabriel.iphonebridge.Calls1      — HFP call control

Messages1:
  • Send(string recipient, string body) → string transfer_path
      Send an SMS/iMessage from the iPhone via MAP PushMessage. iOS routes
      as iMessage automatically when the recipient is iMessage-capable.
  • ListRecent(string folder, uint32 limit) → string json
  • IsHealthy() → bool

Calls1 (HFP, via oFono):
  • Dial(string number) → string call_path
  • AnswerCall(string call_path)
  • HangupCall(string call_path)
  • HangupAll()
  • ListCalls() → string json
  • CallStateChanged(dict)  [signal] — emitted on every call lifecycle change

Events1 (live event feed for separate UIs):
  • MessageReceived(dict)   [signal] — a new SMS/iMessage arrived
  • MessageSent(dict)       [signal] — a message we sent via Send()
  • MessageSeen(dict)       [signal] — a message's read-state changed
  • AncsNotification(dict)  [signal] — a per-app ANCS notification
  • MessageStateChanged(dict) [signal] — delivery/read/typing, keyed by guid

Message payloads carry iMessage-only metadata under `im_*` keys when the
message came in over the native transport; MAP messages simply lack them.
Structured values (participants, attachments) are JSON strings, since a{sv}
cannot nest.

Designed to be simple/synchronous. PushMessage typically completes in
<2s on iOS 26.5 over the existing daemon session.
"""
from __future__ import annotations

import json
import logging

import dbus
import dbus.exceptions
import dbus.service

from iphonebridge import config
from iphonebridge.bus import session_bus
from iphonebridge.hfp.ofono_client import HfpError, HfpManager
from iphonebridge.obex.map_query import list_recent_messages
from iphonebridge.obex.map_send import send_message
from iphonebridge.obex.sessions import SessionManager

log = logging.getLogger(__name__)

BUS_NAME = "com.gabriel.iphonebridge"
OBJECT_PATH = "/com/gabriel/iphonebridge"
IFACE = "com.gabriel.iphonebridge.Messages1"
CALLS_IFACE = "com.gabriel.iphonebridge.Calls1"
EVENTS_IFACE = "com.gabriel.iphonebridge.Events1"


def _variant_dict(d: dict) -> dbus.Dictionary:
    """Coerce a plain dict into a D-Bus a{sv}. None → empty string."""
    out = dbus.Dictionary({}, signature="sv")
    for k, v in d.items():
        if v is None:
            out[k] = dbus.String("")
        elif isinstance(v, bool):
            out[k] = dbus.Boolean(v)
        elif isinstance(v, int):
            out[k] = dbus.Int64(v)
        elif isinstance(v, float):
            out[k] = dbus.Double(v)
        elif isinstance(v, (list, dict, tuple)):
            # JSON rather than str(): the iMessage extras carry real
            # structure (participant lists, attachment records), and
            # str() would render them as unparseable Python reprs on
            # the far side. a{sv} can't express nesting, so the value
            # travels as a string the client json.loads back.
            out[k] = dbus.String(json.dumps(v, ensure_ascii=False, default=str))
        else:
            out[k] = dbus.String(str(v))
    return out


def _reply_part(target_text: str) -> str:
    """Apple's `threadOriginatorPart` for a reply to `target_text`.

    The format is `<part>:<start>:<end>` — a text range inside the target,
    which for an ordinary one-part message means the whole of it: `0:0:<len>`.
    Verified against real replies sent by iOS itself, where the third field is
    always exactly the character count of the message being replied to.

    Sending a bare `"0"` (the old default) is what made replies arrive as
    ordinary messages: rustpush builds `tg` as `r:<part>:<guid>`, so a
    malformed part yields a `tg` Apple won't thread on, and it fails silently
    — the message is delivered, just not as a reply.

    Measured in UTF-16 code units, which is what Apple counts: NSString is
    UTF-16, so an emoji in the target is two, not one.
    """
    units = len(target_text.encode("utf-16-le")) // 2 if target_text else 0
    return f"0:0:{units}"


def _extras_dict(extras) -> dict:
    """Flatten IMessageExtras into keys for a message payload.

    Prefixed with `im_` so they can't collide with SmsEvent's own fields, and
    so a client can tell at a glance which affordances a message supports:
    anything with an `im_guid` can be reacted to, replied to, or unsent, and
    anything without arrived over MAP and can only be read.
    """
    return {
        "im_guid": extras.guid or "",
        "im_service": extras.service or "",
        "im_sender": extras.sender or "",
        "im_outgoing": bool(extras.outgoing),
        "im_is_group": bool(extras.is_group),
        "im_chat_name": extras.chat_name or "",
        "im_participants": list(extras.chat_participants or []),
        "im_reply_to_guid": extras.reply_to_guid or "",
        "im_reaction_target_guid": extras.reaction_target_guid or "",
        "im_reaction_verb": extras.reaction_verb or "",
        "im_edited_from_guid": extras.edited_from_guid or "",
        "im_unsent": bool(extras.unsent),
        "im_attachments": list(extras.attachments or []),
    }


class MessagesService(dbus.service.Object):
    def __init__(
        self,
        bus_name: dbus.service.BusName,
        sessions: SessionManager,
        hfp: HfpManager | None = None,
        on_sent=None,
        on_refresh_contacts=None,
        on_state=None,
        on_thread_read=None,
        on_active_thread=None,
        imessage=None,
    ):
        super().__init__(bus_name, OBJECT_PATH)
        self.sessions = sessions
        self.hfp = hfp
        # An `imessage.IMessageClient`, or None when the helper isn't running.
        # When present it's preferred over MAP for sending: MAP's PushMessage
        # can only carry plain text, while this path also does tapbacks,
        # replies, edits and unsends. Kept optional so the daemon still works
        # exactly as before with the helper stopped — which is the normal state
        # while OpenBubbles is open to renew the registration.
        self.imessage = imessage
        # on_sent(recipient, body, transfer_path) — daemon hook to record a
        # message we just sent (logs it to history + the event feed).
        self._on_sent = on_sent
        # on_refresh_contacts() — re-pull the phonebook over the
        # daemon's existing PBAP session. The CLI can't open its own:
        # the iPhone refuses a second concurrent OBEX session.
        self._on_refresh_contacts = on_refresh_contacts
        # on_state(guid, state, handle, timestamp, body) — persist delivery /
        # edit records that only travel on the state channel (not as SmsEvents).
        self._on_state = on_state
        # on_thread_read(peer) — the UI opened a conversation. Local only.
        self._on_thread_read = on_thread_read
        # on_active_thread(peer, focused) — which conversation is on screen and
        # whether the app window is focused. Used to suppress popups for a
        # thread the user is already looking at.
        self._on_active_thread = on_active_thread

    # ---- Messages1 ------------------------------------------------------

    @dbus.service.method(IFACE, in_signature="ss", out_signature="s")
    def Send(self, recipient: str, body: str) -> str:
        log.info("DBus Send called for %s (%d-byte body)", recipient, len(body))
        if not recipient.strip() or not body.strip():
            raise dbus.exceptions.DBusException(
                "recipient and body must both be non-empty",
                name="com.gabriel.iphonebridge.Error.InvalidArgs",
            )
        # Prefer the native path when it's up. Returns a message guid rather
        # than an obex transfer path; both are opaque handles to the caller,
        # and the guid is the more useful of the two because later events
        # (delivery, read, tapbacks) reference it.
        # Group recipient = comma-separated handles (and/or imessage-group:…).
        # MAP can only address one phone; never fall through to it for groups
        # or we "succeed" with a transfer that never reaches the chat.
        multi = "," in recipient or recipient.strip().startswith(
            "imessage-group:"
        )

        if self.imessage is not None:
            try:
                guid = self._imessage_send(recipient, body)
            except Exception as e:
                if multi:
                    log.exception("iMessage group send failed")
                    raise dbus.exceptions.DBusException(
                        str(e), name="com.gabriel.iphonebridge.Error.SendFailed"
                    )
                # Fall through to MAP rather than failing: a send that goes out
                # over Bluetooth is better than one that doesn't go out. The
                # common cause is an expired registration, and MAP is
                # unaffected by that.
                log.warning(
                    "iMessage send failed (%s); falling back to MAP", e
                )
            else:
                self._read_receipt_on_send(recipient)
                self._record(recipient, body, guid)
                return guid

        if multi:
            raise dbus.exceptions.DBusException(
                "group send needs the iMessage helper "
                "(iphonebridge-imessage.service)",
                name="com.gabriel.iphonebridge.Error.NotReady",
            )

        if self.sessions.map is None:
            raise dbus.exceptions.DBusException(
                "no transport available: iMessage helper down and MAP session "
                "not open (iPhone toggles probably off)",
                name="com.gabriel.iphonebridge.Error.NotReady",
            )
        try:
            transfer = send_message(self.sessions.map_path, recipient, body)
        except Exception as e:
            log.exception("Send failed")
            raise dbus.exceptions.DBusException(
                str(e), name="com.gabriel.iphonebridge.Error.SendFailed"
            )
        self._record(recipient, body, transfer)
        return transfer

    def _record(self, recipient: str, body: str, handle: str,
                reply_to_guid: str = "") -> None:
        """Add a message we just sent to history and the event feed.

        Every path that puts text in front of the other person has to call
        this. `SendReply` didn't, so a reply reached the recipient but never
        appeared in our own conversation — which from the sending side is
        indistinguishable from the reply not working at all.

        Never let a bookkeeping failure fail the send: the message has
        already gone out by the time we get here.
        """
        if self._on_sent is None:
            return
        try:
            self._on_sent(recipient, body, handle, reply_to_guid)
        except Exception:
            log.exception("on_sent hook failed (message was still sent)")

    # ---- iMessage-only verbs -------------------------------------------
    #
    # None of these are expressible over MAP, so they have no fallback: with
    # the helper stopped they raise NotReady rather than silently doing
    # nothing. Each takes the recipient in the same form as Send() and returns
    # the guid of the message it generated.

    def _imessage(self):
        if self.imessage is None:
            raise dbus.exceptions.DBusException(
                "iMessage helper is not running — start "
                "iphonebridge-imessage.service",
                name="com.gabriel.iphonebridge.Error.NotReady",
            )
        return self.imessage

    def _imessage_send(self, recipient: str, body: str) -> str:
        # Same multi-recipient split as SendReply/React: a group is a
        # comma-separated participant list. The old single to_handle() call
        # treated the whole list as one address and groups never sent.
        return self.imessage.send(self._participants(recipient), body)

    def _participants(self, recipient: str) -> list[str]:
        from iphonebridge.imessage.handles import GROUP_KEY_PREFIX, to_handle

        # A comma-separated recipient addresses a group; that's how the CLI
        # and UI already express multiple recipients. Also accept a full
        # `imessage-group:…` key (strip the prefix) so either form works.
        value = (recipient or "").strip()
        if value.startswith(GROUP_KEY_PREFIX):
            value = value[len(GROUP_KEY_PREFIX):]
        return [to_handle(r) for r in value.split(",") if r.strip()]

    def _read_receipt_on_send(self, recipient: str) -> None:
        """Tell the sender we've read the thread, because we just replied to it.

        Deliberately tied to sending rather than to viewing. Replying already
        reveals that the message was read, so the receipt discloses nothing
        the reply doesn't — whereas firing one when a conversation merely
        scrolls past would disclose something the user never chose to.

        Best-effort by construction: this runs *after* a message has already
        gone out, so raising here would report a successful send as a failure
        and invite the caller to send it twice.
        """
        if not config.SEND_READ_RECEIPTS or self.imessage is None:
            return
        try:
            self.imessage.mark_read(self._participants(recipient))
        except Exception:
            log.debug("read receipt for %s failed", recipient, exc_info=True)

    def _imessage_call(self, what: str, fn, *args, **kwargs) -> str:
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            log.exception("%s failed", what)
            raise dbus.exceptions.DBusException(
                str(e), name="com.gabriel.iphonebridge.Error.SendFailed"
            )

    @dbus.service.method(IFACE, in_signature="ssss", out_signature="s")
    def React(self, recipient: str, target_guid: str, kind: str, target_text: str) -> str:
        """Add a tapback. `kind` is a verb name or a literal emoji.

        MAP cannot do this at all — it can only observe iOS's synthesized
        'Loved "…"' text after the fact.
        """
        client = self._imessage()
        # A multi-character non-ASCII `kind` is an emoji tapback (iOS 18+);
        # the six classic ones are ASCII words.
        emoji = kind if kind and not kind.isascii() else None
        result = self._imessage_call(
            "React",
            client.react,
            self._participants(recipient),
            target_guid,
            kind=None if emoji else (kind or "heart"),
            emoji=emoji,
            target_text=target_text,
            # Part 0 is the text body. Without it rustpush writes a bare
            # target uuid and the reaction never attaches on the phone.
            target_part=0,
        )
        # A tapback is aimed at a specific message, so it's the clearest
        # possible admission of having read it. Edit and Unsend act on our
        # own messages and imply nothing about theirs, so they don't do this.
        self._read_receipt_on_send(recipient)
        return result

    @dbus.service.method(IFACE, in_signature="sss", out_signature="s")
    def Unreact(self, recipient: str, target_guid: str, kind: str) -> str:
        """Remove a previously sent tapback."""
        client = self._imessage()
        emoji = kind if kind and not kind.isascii() else None
        return self._imessage_call(
            "Unreact",
            client.react,
            self._participants(recipient),
            target_guid,
            kind=None if emoji else (kind or "heart"),
            emoji=emoji,
            enable=False,
            target_part=0,
        )

    @dbus.service.method(IFACE, in_signature="sss", out_signature="s")
    def Edit(self, recipient: str, target_guid: str, new_text: str) -> str:
        client = self._imessage()
        if not new_text.strip():
            raise dbus.exceptions.DBusException(
                "new_text must be non-empty",
                name="com.gabriel.iphonebridge.Error.InvalidArgs",
            )
        result = self._imessage_call(
            "Edit",
            client.edit,
            self._participants(recipient),
            target_guid,
            new_text,
        )
        self._echo_local_change(target_guid, "edited", recipient, new_text)
        return result

    @dbus.service.method(IFACE, in_signature="ss", out_signature="s")
    def Unsend(self, recipient: str, target_guid: str) -> str:
        client = self._imessage()
        result = self._imessage_call(
            "Unsend", client.unsend, self._participants(recipient), target_guid
        )
        self._echo_local_change(target_guid, "unsent", recipient,
                                "[message unsent]")
        return result

    def _echo_local_change(self, target_guid: str, state: str,
                           handle: str, body: str) -> None:
        """Tell our own UI about an edit/unsend we just made.

        Apple relays these to your *other* devices but not back to the one
        that sent them, so without this the desktop is the only place that
        never sees its own edit — the bubble keeps its old text while the
        correction shows up correctly on the phone. Which is exactly how it
        was reported.

        Rides the state channel rather than the message channel: this
        modifies an existing message rather than adding one, which is what
        that signal is for.
        """
        from datetime import datetime, timezone

        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            self.emit_message_state(target_guid, state, handle, ts, body)
        except Exception:
            log.exception("failed to echo local %s (it was still sent)", state)
        # The bus signal is live-only; without this, our own edit vanishes on
        # the next restart because nothing rewrote the message store.
        if self._on_state is not None:
            try:
                self._on_state(target_guid, state, handle, ts, body)
            except Exception:
                log.exception("failed to persist local %s", state)

    @dbus.service.method(IFACE, in_signature="ssss", out_signature="s")
    def SendReply(self, recipient: str, body: str, reply_to_guid: str,
                  target_text: str = "") -> str:
        """Send a threaded reply to a specific message.

        `target_text` is the text of the message being replied to. Like
        `React`, this needs it for the wire format rather than for display —
        see `_reply_part`.
        """
        client = self._imessage()
        from iphonebridge.imessage.handles import to_handle

        guid = self._imessage_call(
            "SendReply",
            client.send,
            [to_handle(r) for r in recipient.split(",") if r.strip()],
            body,
            reply_guid=reply_to_guid,
            reply_part=_reply_part(target_text),
        )
        self._read_receipt_on_send(recipient)
        # A reply is a message like any other — it has to land in our own
        # history too, or sending one looks like nothing happened. It also has
        # to keep pointing at what it replies to, or it lands as an ordinary
        # message, which looks the same again.
        self._record(recipient, body, guid, reply_to_guid)
        return guid

    @dbus.service.method(IFACE, in_signature="sb", out_signature="s")
    def SetTyping(self, recipient: str, typing: bool) -> str:
        client = self._imessage()
        # `dbus.Boolean` subclasses `int`, so json.dumps writes it as 1/0 and
        # the helper rejects it ("invalid type: integer, expected a boolean").
        # Coerce to a real bool at the boundary rather than teaching the wire
        # format to accept both.
        return self._imessage_call(
            "SetTyping", client.typing, self._participants(recipient), bool(typing)
        )

    @dbus.service.method(IFACE, in_signature="s", out_signature="s")
    def MarkRead(self, recipient: str) -> str:
        """Mark a thread read here and on the user's other Apple devices.

        Explicit form, for a caller that wants to disclose without sending —
        the UI does not call this. Normal read receipts ride along with an
        outgoing message instead; see `_read_receipt_on_send`.

        Still gated on the flag, so one switch turns off every path that can
        tell a sender we read something.
        """
        if not config.SEND_READ_RECEIPTS:
            log.debug("MarkRead suppressed: read receipts are off")
            return json.dumps({"ok": True, "suppressed": "read_receipts_disabled"})
        client = self._imessage()
        return self._imessage_call(
            "MarkRead", client.mark_read, self._participants(recipient)
        )

    @dbus.service.method(IFACE, in_signature="s", out_signature="i")
    def DismissNotifications(self, peer: str) -> int:
        """Close any desktop popups for `peer`. Returns how many were closed.

        Called when the UI opens a conversation: the message is on screen, so
        the notification for it has done its job.

        Deliberately *not* MarkRead. That discloses to the sender over Apple's
        network and is gated on a config flag; this only touches popups on this
        machine, so it stays available with read receipts off.
        """
        if self._on_thread_read is None:
            return 0
        try:
            return int(self._on_thread_read(peer) or 0)
        except Exception:
            log.exception("DismissNotifications(%s) failed", peer)
            return 0

    @dbus.service.method(IFACE, in_signature="sb", out_signature="")
    def SetActiveThread(self, peer: str, focused: bool) -> None:
        """Tell the daemon which conversation is open and whether we are focused.

        When `focused` is true and a message arrives for `peer`, desktop
        notifications for that conversation are suppressed — the UI already
        shows it. When the window loses focus (minimized, other workspace,
        covered), pass focused=false so new messages still notify even if
        the same thread remains selected.

        `peer` is a 1:1 handle, a comma-separated handle list, or a group
        thread key (`imessage-group:…`). Empty peer clears the active set.
        Local only; does not MarkRead.
        """
        if self._on_active_thread is None:
            return
        try:
            self._on_active_thread(str(peer or ""), bool(focused))
        except Exception:
            log.exception("SetActiveThread(%r, %s) failed", peer, focused)

    @dbus.service.method(IFACE, in_signature="", out_signature="s")
    def IMessageStatus(self) -> str:
        """JSON: whether the helper is up, our handles, and expiry warnings.

        The UI uses this to decide whether to offer tapbacks and edits at all,
        and to surface a registration that's about to lapse while there's
        still time to renew it.
        """
        # Reported even when the transport is down so the settings UI can
        # render the toggle without depending on the helper being up.
        receipts = {"send_read_receipts": config.SEND_READ_RECEIPTS}
        if self.imessage is None:
            return json.dumps(
                {"available": False, "handles": [], "warnings": [], **receipts}
            )
        try:
            status = self.imessage.status()
        except Exception as e:
            return json.dumps({"available": False, "error": str(e), **receipts})
        return json.dumps({"available": True, **status, **receipts})

    @dbus.service.method(IFACE, in_signature="", out_signature="i")
    def RefreshContacts(self) -> int:
        """Re-pull contacts via PBAP. Returns the cached contact count."""
        if self._on_refresh_contacts is None:
            raise dbus.exceptions.DBusException(
                "contacts refresh not available in this daemon build",
                name="com.gabriel.iphonebridge.Error.NotReady",
            )
        try:
            return int(self._on_refresh_contacts())
        except Exception as e:
            log.exception("contacts refresh failed")
            raise dbus.exceptions.DBusException(
                str(e), name="com.gabriel.iphonebridge.Error.RefreshFailed")

    @dbus.service.method(IFACE, in_signature="su", out_signature="s")
    def ListRecent(self, folder: str, limit: int) -> str:
        """Return up to `limit` recent messages from `folder` as a JSON array."""
        if self.sessions.map is None:
            raise dbus.exceptions.DBusException(
                "MAP session not open — iPhone toggles probably off",
                name="com.gabriel.iphonebridge.Error.NotReady",
            )
        folder = folder or "telecom/msg/INBOX"
        try:
            msgs = list_recent_messages(self.sessions.map_path,
                                        folder=folder,
                                        limit=max(1, min(int(limit), 200)))
        except Exception as e:
            log.exception("ListRecent failed")
            raise dbus.exceptions.DBusException(
                str(e), name="com.gabriel.iphonebridge.Error.QueryFailed"
            )
        return json.dumps(msgs, ensure_ascii=False)

    @dbus.service.method(IFACE, in_signature="", out_signature="b")
    def IsHealthy(self) -> bool:
        # Must ask obexd — a non-None session object survives the phone
        # disconnecting, so the old check reported healthy with no link.
        return self.sessions.is_alive()

    # ---- Calls1 (HFP) ---------------------------------------------------

    def _require_hfp(self) -> HfpManager:
        if self.hfp is None:
            raise dbus.exceptions.DBusException(
                "HFP not available in this daemon build",
                name="com.gabriel.iphonebridge.Error.NotReady",
            )
        return self.hfp

    @dbus.service.method(CALLS_IFACE, in_signature="s", out_signature="s")
    def Dial(self, number: str) -> str:
        """Place a call. Returns the new oFono VoiceCall object path."""
        log.info("DBus Dial called for %s", number)
        if not number.strip():
            raise dbus.exceptions.DBusException(
                "number must be non-empty",
                name="com.gabriel.iphonebridge.Error.InvalidArgs",
            )
        try:
            return self._require_hfp().dial(number)
        except HfpError as e:
            raise dbus.exceptions.DBusException(
                str(e), name="com.gabriel.iphonebridge.Error.NotReady"
            )
        except Exception as e:
            log.exception("Dial failed")
            raise dbus.exceptions.DBusException(
                str(e), name="com.gabriel.iphonebridge.Error.CallFailed"
            )

    @dbus.service.method(CALLS_IFACE, in_signature="s", out_signature="")
    def AnswerCall(self, call_path: str) -> None:
        log.info("DBus AnswerCall %s", call_path)
        try:
            self._require_hfp().answer(call_path)
        except HfpError as e:
            raise dbus.exceptions.DBusException(
                str(e), name="com.gabriel.iphonebridge.Error.NotReady"
            )
        except Exception as e:
            log.exception("AnswerCall failed")
            raise dbus.exceptions.DBusException(
                str(e), name="com.gabriel.iphonebridge.Error.CallFailed"
            )

    @dbus.service.method(CALLS_IFACE, in_signature="s", out_signature="")
    def HangupCall(self, call_path: str) -> None:
        log.info("DBus HangupCall %s", call_path)
        try:
            self._require_hfp().hangup(call_path)
        except HfpError as e:
            raise dbus.exceptions.DBusException(
                str(e), name="com.gabriel.iphonebridge.Error.NotReady"
            )
        except Exception as e:
            log.exception("HangupCall failed")
            raise dbus.exceptions.DBusException(
                str(e), name="com.gabriel.iphonebridge.Error.CallFailed"
            )

    @dbus.service.method(CALLS_IFACE, in_signature="", out_signature="")
    def HangupAll(self) -> None:
        log.info("DBus HangupAll")
        try:
            self._require_hfp().hangup_all()
        except HfpError as e:
            raise dbus.exceptions.DBusException(
                str(e), name="com.gabriel.iphonebridge.Error.NotReady"
            )
        except Exception as e:
            log.exception("HangupAll failed")
            raise dbus.exceptions.DBusException(
                str(e), name="com.gabriel.iphonebridge.Error.CallFailed"
            )

    @dbus.service.method(CALLS_IFACE, in_signature="", out_signature="s")
    def ListCalls(self) -> str:
        """Return the currently-tracked calls as a JSON array."""
        calls = self.hfp.list_calls() if self.hfp is not None else []
        return json.dumps(calls, ensure_ascii=False)

    @dbus.service.signal(CALLS_IFACE, signature="a{sv}")
    def CallStateChanged(self, props):
        """Emitted on every call lifecycle change. Payload is CallEvent.to_dict()."""

    def emit_call_state(self, event) -> None:
        """Daemon-side helper — push a CallEvent out as a CallStateChanged signal."""
        try:
            self.CallStateChanged(_variant_dict(event.to_dict()))
        except Exception:
            log.exception("CallStateChanged emit failed")

    # ---- Events1 (live event feed for separate UIs) ---------------------

    @dbus.service.signal(EVENTS_IFACE, signature="a{sv}")
    def MessageReceived(self, props):
        """Emitted when a new SMS/iMessage arrives. Payload: SmsEvent.to_dict()."""

    @dbus.service.signal(EVENTS_IFACE, signature="a{sv}")
    def MessageSent(self, props):
        """Emitted when we send a message via Send(). Payload: SmsEvent.to_dict()."""

    @dbus.service.signal(EVENTS_IFACE, signature="a{sv}")
    def MessageSeen(self, props):
        """Emitted on a message read-state change. Payload: SmsEvent.to_dict()."""

    @dbus.service.signal(EVENTS_IFACE, signature="a{sv}")
    def AncsNotification(self, props):
        """Emitted on a per-app ANCS notification. Payload: AncsEvent.to_dict()."""

    @dbus.service.signal(EVENTS_IFACE, signature="a{sv}")
    def MessageStateChanged(self, props):
        """Emitted when an existing message's state changes.

        Unlike the other Events1 signals this adds nothing to a conversation;
        it updates something already in it. Keyed by `guid`, which is why the
        message signals now carry one.

        Payload:
          guid       — the message this concerns (empty for typing)
          state      — "delivered" | "read" | "typing" | "typing_stopped"
          handle     — conversation participant, for routing typing indicators
                       (typing has no message to attach to)
          timestamp  — ISO-8601 UTC, when we learned of it
        """

    def emit_message_state(
        self, guid: str, state: str, handle: str = "", timestamp: str = "",
        body: str = "",
    ) -> None:
        """Daemon-side helper — push a delivery/read/typing update.

        `body` is only meaningful for the states that rewrite a message
        (edited, unsent); the delivery states carry no text.
        """
        try:
            self.MessageStateChanged(_variant_dict({
                "guid": guid,
                "state": state,
                "handle": handle,
                "timestamp": timestamp,
                "body": body,
            }))
        except Exception:
            log.exception("MessageStateChanged emit failed")

    def emit_message(self, event, extras=None) -> None:
        """Daemon-side helper — push an SmsEvent out as a D-Bus signal.

        `extras` is the iMessage-only metadata (guid, reply target, tapback
        target, attachments). Merged into the payload rather than sent
        alongside it so a client handles one dict per message; MAP events
        simply arrive without those keys.
        """
        try:
            props = event.to_dict()
            if extras is not None:
                props.update(_extras_dict(extras))
            payload = _variant_dict(props)
            kind = getattr(event, "kind", "")
            if kind == "sms_seen":
                self.MessageSeen(payload)
            elif kind == "sms_sent":
                self.MessageSent(payload)
            else:
                self.MessageReceived(payload)
        except Exception:
            log.exception("message signal emit failed")

    def emit_ancs(self, event) -> None:
        """Daemon-side helper — push an AncsEvent out as an AncsNotification signal."""
        try:
            self.AncsNotification(_variant_dict(event.to_dict()))
        except Exception:
            log.exception("AncsNotification emit failed")


def claim_bus_name() -> dbus.service.BusName:
    """Acquire com.gabriel.iphonebridge on the session bus.

    Raises if the name is already taken by another instance — caller
    should treat that as 'another daemon is already running'.
    """
    return dbus.service.BusName(
        BUS_NAME,
        bus=session_bus,
        do_not_queue=True,
        replace_existing=False,
    )
