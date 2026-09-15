"""verdigris daemon — orchestrates everything.

Startup order:
  1. bluez_setup.prepare — set adapter CoD, register BLE advert
  2. SessionManager.open_all — long-lived MAP + PBAP OBEX sessions
     (retries on Forbidden — see _try_open_sessions)
  3. ContactsResolver — warm SQLite cache; if empty, pull PBAP
  4. MapEventListener — subscribe to MAP MNS push events
  5. Sinks — register libnotify + sqlite
  6. DBus service (dev.turbinebmw.Verdigris.Bridge.Messages1)
  7. GLib.MainLoop().run()

Shutdown order is the reverse.

Degraded mode: if MAP/PBAP can't open (typically because the user hasn't
enabled iPhone toggles yet), the daemon stays alive, logs a clear
remediation hint, and retries every 60s. This avoids the systemd
crash-loop we hit in an earlier version.
"""
from __future__ import annotations

import json
import logging
import re
import signal
import threading
from datetime import datetime, timezone
from pathlib import Path

from gi.repository import GLib

from verdigris import bluez_setup, config
from verdigris.ancs.client import AncsClient
from verdigris.ancs.events import AncsEvent
from verdigris.ancs.preferences import preferences, remember_app
from verdigris.backup.runner import ATTACHMENTS_DIR
from verdigris.bus import main_loop
from verdigris.contacts import ContactsResolver, pull_phonebook
from verdigris.dbus_service import MessagesService, claim_bus_name
from verdigris.dedupe import CrossTransportDeduper
from verdigris.events import SmsEvent, sms_sent_event
from verdigris.hfp.events import CallEvent
from verdigris.hfp.ofono_client import HfpManager
from verdigris.imagesize import display_size
from verdigris.imessage import IMessageClient, IMessageUnavailable
from verdigris.imessage.bridge import translate as translate_imessage
from verdigris.obex.map_events import MapEventListener
from verdigris.obex.map_send import send_message as map_send_message
from verdigris.obex.sessions import SessionError, SessionManager
from verdigris.sinks import Sink
from verdigris.sinks.clipboard import ClipboardSink
from verdigris.sinks.libnotify import LibnotifySink
from verdigris.sinks.sqlite import SqliteSink

log = logging.getLogger(__name__)

# How often to re-pull the iPhone's phonebook (so the cache picks up new contacts)
CONTACTS_REFRESH_SEC = 24 * 60 * 60  # 24h
# How often to check the MAP session is still real. Short enough that a
# reconnect after the phone comes back in range feels immediate.
SESSION_WATCHDOG_SEC = 15

# How often to retry connecting to the iMessage helper. It's a separate
# systemd unit that may start after us, or be deliberately stopped while
# OpenBubbles renews the registration, so its absence is normal rather than an
# error and we just keep looking.
IMESSAGE_RETRY_SEC = 30

# 8-4-4-4-12 hex, as Apple formats message ids. Deliberately strict: the
# alternative it has to be told apart from is an obex object path, and
# mistaking one for the other silently breaks delivery receipts.
_GUID_RE = re.compile(
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
)


def _looks_like_guid(value: str | None) -> bool:
    return bool(value and _GUID_RE.match(value))

# How often to retry MAP/PBAP session open when blocked by the iPhone
# (toggles off, paired-but-not-connected, etc.)
SESSION_RETRY_SEC = 60


class Daemon:
    def __init__(self) -> None:
        self.sessions = SessionManager()
        self.contacts = ContactsResolver()
        self.sinks: list[Sink] = []
        self.listener: MapEventListener | None = None
        self.ancs: AncsClient | None = None
        self.hfp: HfpManager | None = None
        self._contacts_refresh_id: int | None = None
        self._session_retry_id: int | None = None
        self._session_watchdog_id: int | None = None
        # "up" / "waiting" — so the watchdog logs state changes, not every tick.
        self._link_state: str | None = None
        self._bus_name = None
        self._dbus_service: MessagesService | None = None
        self._post_sessions_done = False
        # Native iMessage transport. Connected opportunistically — see
        # _connect_imessage — because the helper is a separate unit whose
        # absence is a normal state, not a failure.
        self._imessage: IMessageClient | None = None
        self._imessage_retry_id: int | None = None
        # guid → IMessageExtras for the detail SmsEvent can't carry (real
        # tapback targets, reply threading, attachments). Bounded, because the
        # daemon is long-lived and this would otherwise grow without limit.
        self._imessage_extras: dict = {}
        self._imessage_handles: set[str] = set()
        # guids already fanned out, so the per-handle duplicates Apple sends
        # don't each become a history row. Insertion-ordered for trimming.
        self._imessage_seen: dict = {}
        # One message can reach us over MAP, over iMessage, and as our own
        # recorded send. See dedupe.py for why matching has to be heuristic.
        self._deduper = CrossTransportDeduper()

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> None:
        log.info("=== verdigris starting ===")
        config.ensure_dirs()

        if not bluez_setup.prepare():
            log.warning(
                "bluez_setup.prepare reported issues — continuing anyway, "
                "but MAP/PBAP may be refused. Re-pair on iPhone after the "
                "adapter is in A/V Hands-Free CoD if the toggles aren't there."
            )

        # ANCS — per-app notifications via BLE GATT. Independent of MAP/PBAP.
        # The client installs its subscriptions first, then we request the LE
        # bearer alongside Classic. On BlueZ 5.87 prepare() has already
        # restored the kernel address-resolution flag needed for reconnects.
        device_path = (
            f"/org/bluez/{config.ADAPTER}"
            f"/dev_{config.IPHONE_MAC.replace(':', '_')}"
        )
        self.ancs = AncsClient(device_path, on_event=self._fanout_ancs)
        self.ancs.start()
        bluez_setup.connect_le_bearer_async()

        # HFP — take/place calls via oFono. Also independent of MAP/PBAP; if
        # oFono isn't set up it logs a hint and stays dormant.
        self.hfp = HfpManager(
            on_event=self._fanout_call,
            resolve_contact=lambda raw: self.contacts.resolve(raw),
        )
        self.hfp.start()

        # Sinks don't need the OBEX sessions — set them up now so ANCS and
        # HFP events still reach the desktop while MAP/PBAP are degraded.
        self._setup_sinks()

        # Try to open MAP/PBAP. If blocked, stay alive and retry every minute.
        self._try_open_sessions(first_attempt=True)

        # Always set up the DBus service so a CLI can at least query
        # IsHealthy and learn the daemon's status. Send() will fail until
        # the MAP session is open, but the service surface itself is up.
        try:
            self._bus_name = claim_bus_name()
            self._dbus_service = MessagesService(
                self._bus_name, self.sessions, hfp=self.hfp,
                on_sent=self._record_sent,
                on_refresh_contacts=self._refresh_contacts_now,
                on_state=self._persist_state,
                on_thread_read=self._dismiss_notifications,
                on_active_thread=self._set_active_thread,
                imessage=self._imessage)
            log.info("DBus service ready: dev.turbinebmw.Verdigris.Bridge")
            # After the D-Bus service exists, so the client can be handed to
            # it directly rather than being picked up on the next retry.
            self._start_imessage()
        except Exception:
            log.exception("DBus service registration failed — continuing "
                          "without send capability")

        # Signal handlers
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, self._signal)

        if not self._post_sessions_done:
            log.warning("=== Verdigris running in DEGRADED mode ===")
            log.warning("    No MAP/PBAP session yet. Retrying every %ds.",
                        SESSION_RETRY_SEC)
        # The "ready" line in the happy path is emitted by
        # _post_sessions_setup, so we don't duplicate it here.

    def _try_open_sessions(self, *, first_attempt: bool) -> None:
        """Open MAP + PBAP. On Forbidden, schedule a periodic retry instead
        of crashing. Idempotent."""
        try:
            self.sessions.open_all()
        except SessionError as e:
            msg = str(e)
            log.warning("could not open MAP/PBAP sessions: %s", msg)
            if "Forbidden" in msg or "0x43" in msg:
                log.warning("")
                log.warning("  → This usually means the iPhone toggles aren't on.")
                log.warning("  → On the iPhone:")
                log.warning("       Settings → Bluetooth → tap (i) next to this device")
                log.warning("       Enable: Show Message Notifications")
                log.warning("       Enable: Sync Contacts")
                log.warning("")
            if first_attempt and self._session_retry_id is None:
                self._session_retry_id = GLib.timeout_add_seconds(
                    SESSION_RETRY_SEC, self._retry_sessions
                )
                log.warning("  → Daemon stays running. Will retry every %ds.",
                            SESSION_RETRY_SEC)
            return
        # Sessions opened — wire everything that depends on them.
        self._post_sessions_setup()

    def _retry_sessions(self) -> bool:
        """GLib timer callback. Return True to keep the timer firing."""
        log.info("retrying MAP/PBAP session open ...")
        try:
            self.sessions.open_all()
        except SessionError as e:
            # Still blocked — keep timer alive
            log.info("still blocked: %s", str(e)[:120])
            return True

        log.info("sessions opened on retry — promoting to ready state")
        self._post_sessions_setup()
        # Stop the retry timer
        self._session_retry_id = None
        return False

    def _setup_sinks(self) -> None:
        """Register the SQLite + libnotify sinks. Independent of the OBEX
        sessions, so ANCS/HFP events reach the desktop even in degraded mode."""
        if self.sinks:
            return
        try:
            self.sinks.append(SqliteSink())
        except Exception:
            log.exception("sqlite sink failed to init — history will not persist")
        try:
            self.sinks.append(LibnotifySink(
                hfp=self.hfp,
                send_message=self._send_reply,
                contacts=self.contacts,
            ))
        except Exception:
            log.exception("libnotify sink failed to init — continuing")
        try:
            self.sinks.append(ClipboardSink())
        except Exception:
            log.exception("clipboard sink failed to init — continuing")
        log.info("sinks ready: %s", [s.name for s in self.sinks])

    def _post_sessions_setup(self) -> None:
        """Everything that requires live MAP+PBAP sessions. Idempotent so
        we can call it either at first-attempt success or at retry success."""
        if self._post_sessions_done:
            return
        self._post_sessions_done = True

        # Warm contacts; if empty, do a one-time pull. PBAP pull is cheap.
        if self.contacts.count() == 0:
            log.info("contacts cache empty — pulling from iPhone via PBAP")
            self._refresh_contacts()

        # Schedule periodic contacts refresh
        if self._contacts_refresh_id is None:
            self._contacts_refresh_id = GLib.timeout_add_seconds(
                CONTACTS_REFRESH_SEC, self._periodic_refresh_contacts
            )

        # Wire up MAP MNS listener.
        # IMPORTANT: pass an indirect lambda so the resolver can be refreshed
        # in place via self.contacts.refresh() without breaking this binding.
        if self.listener is None:
            self.listener = MapEventListener(
                sessions=self.sessions,
                on_sms=self._fanout,
                resolve_contact=lambda raw: self.contacts.resolve(raw),
            )
            self.listener.start()

        # Watchdog: the iPhone drops the link whenever it wanders out of
        # range, sleeps, or switches networks, and obexd tears the OBEX
        # sessions down with it. Nothing used to notice — the daemon sat
        # with dead sessions reporting itself healthy and silently stopped
        # delivering messages until it was restarted by hand.
        if self._session_watchdog_id is None:
            self._session_watchdog_id = GLib.timeout_add_seconds(
                SESSION_WATCHDOG_SEC, self._check_sessions)

        log.info("=== verdigris ready (contacts=%d, sinks=%s) ===",
                 self.contacts.count(),
                 [s.name for s in self.sinks])

    def _check_sessions(self) -> bool:
        """Re-establish MAP/PBAP if they've died. GLib timeout callback."""
        try:
            if self.sessions.is_alive():
                if self._link_state != "up":
                    self._link_state = "up"
                return True
            if not bluez_setup.device_connected():
                # Nudge the link back up. iOS usually reconnects itself, but
                # after a long gap it can sit paired-but-idle forever.
                if not bluez_setup.connect_device():
                    # Log the transition once rather than every 15s — but do
                    # log it, so "messages stopped" is visible in the journal
                    # instead of silently sitting at debug level.
                    if self._link_state != "waiting":
                        self._link_state = "waiting"
                        log.warning(
                            "iPhone is not reachable over Bluetooth — "
                            "messages are paused until it reconnects")
                    return True

            log.warning("MAP session is dead but the iPhone is connected — "
                        "reopening")
            if self.listener is not None:
                self.listener.stop()
                self.listener = None
            if not self.sessions.reopen():
                return True
            self.listener = MapEventListener(
                sessions=self.sessions,
                on_sms=self._fanout,
                resolve_contact=lambda raw: self.contacts.resolve(raw),
            )
            self.listener.start()
            log.info("MAP session recovered")
        except Exception:
            log.exception("session watchdog failed")
        return True

    def _refresh_contacts_now(self) -> int:
        """D-Bus hook: re-pull contacts and return the new count."""
        self._refresh_contacts()
        return self.contacts.count()

    def _refresh_contacts(self) -> None:
        """Pull phonebook from iPhone + reload in-process cache. Idempotent."""
        try:
            pulled = pull_phonebook(self.sessions)
            count = self.contacts.refresh()
            log.info("contacts refresh: pulled %d, cached %d", pulled, count)
        except Exception:
            log.exception("contacts refresh failed — running with previous cache")

    def _periodic_refresh_contacts(self) -> bool:
        """GLib timeout callback. Return True to keep the timer running."""
        log.info("periodic contacts refresh tick")
        self._refresh_contacts()
        return True

    def stop(self) -> None:
        log.info("=== verdigris stopping ===")
        for tid_attr in ("_contacts_refresh_id", "_session_retry_id",
                         "_session_watchdog_id", "_imessage_retry_id"):
            tid = getattr(self, tid_attr, None)
            if tid is not None:
                try:
                    GLib.source_remove(tid)
                except Exception:
                    pass
                setattr(self, tid_attr, None)
        if self._imessage is not None:
            # Closes the socket and stops the reader thread. The helper itself
            # keeps running — it's a separate unit and other clients may be
            # attached.
            try:
                self._imessage.close()
            except Exception:
                log.exception("closing the iMessage client failed")
            self._imessage = None
        if self.listener is not None:
            self.listener.stop()
        if self.ancs is not None:
            self.ancs.stop()
        if self.hfp is not None:
            self.hfp.stop()
        self.sessions.close_all()
        bluez_setup.unregister_advert()
        main_loop.quit()

    def run(self) -> None:
        self.start()
        try:
            main_loop.run()
        finally:
            self.stop()

    # ---- internals -------------------------------------------------------

    # ---- iMessage --------------------------------------------------------

    # Cap on _imessage_extras. Roughly a day of heavy conversation; old
    # entries only matter for rendering messages still on screen.
    _EXTRAS_LIMIT = 5000
    # Only needs to span the gap between duplicate copies of one message,
    # which is milliseconds; sized generously anyway since it's just guids.
    _SEEN_LIMIT = 5000

    def _connect_imessage(self) -> bool:
        """Attach to the helper if it's listening. Returns True once attached.

        Safe to call repeatedly. Used both at startup and from the retry timer,
        so a helper that starts later — or restarts — gets picked up without
        restarting the daemon.
        """
        if self._imessage is not None and self._imessage.connected:
            return True
        client = IMessageClient(on_event=self._on_imessage_event_threaded)
        try:
            client.connect()
        except IMessageUnavailable as e:
            # Expected whenever the unit is stopped. Debug, not warning: this
            # fires every IMESSAGE_RETRY_SEC and would otherwise bury the log.
            log.debug("iMessage helper not available: %s", e)
            return False
        self._imessage = client
        try:
            self._imessage_handles = set(client.handles())
        except Exception:
            log.exception("could not read iMessage handles")
            self._imessage_handles = set()
        if self._dbus_service is not None:
            self._dbus_service.imessage = client
        log.info(
            "iMessage transport up (%d handle(s)): sending, tapbacks, "
            "replies, edits and unsends available",
            len(self._imessage_handles),
        )
        for warning in self._imessage_warnings():
            log.warning("iMessage: %s", warning)
        return True

    def _imessage_warnings(self) -> list[str]:
        if self._imessage is None:
            return []
        try:
            return list(self._imessage.status().get("warnings", []))
        except Exception:
            return []

    # How many trailing history rows to scan for guids at startup. Apple
    # replays a modest backlog, not the whole account, so this only has to
    # cover recent traffic.
    _SEED_SCAN_ROWS = 2000

    def _seed_seen_guids(self) -> None:
        """Remember guids already in history, so a restart doesn't re-add them.

        Apple replays recent messages whenever a new APNs connection is
        established, and `_imessage_seen` lives only in memory — so before
        this, every daemon restart appended a fresh copy of everything in the
        replay window. Four restarts in an afternoon produced four copies of
        the same message.

        Cheap and best-effort: a failure here costs deduplication, not
        correctness, so it must never stop the daemon from starting.
        """
        try:
            from verdigris.message_store import MESSAGE_KINDS, default_store

            # Newest N message rows — enough to cover Apple's replay window.
            rows = default_store().read_events(
                kinds=set(MESSAGE_KINDS), limit=self._SEED_SCAN_ROWS
            )
        except Exception:
            log.debug("could not read history to seed guid dedupe", exc_info=True)
            return

        seeded = 0
        for ev in rows:
            guid = ev.get("guid")
            if guid and guid not in self._imessage_seen:
                self._imessage_seen[guid] = None
                seeded += 1
        if seeded:
            log.info("seeded %d known message ids from history", seeded)

    def _start_imessage(self) -> None:
        # Before connecting, so a replay that arrives immediately is already
        # recognised as old.
        self._seed_seen_guids()
        if self._connect_imessage():
            return
        log.info(
            "iMessage helper not running; will keep trying every %ds "
            "(sending falls back to MAP until then)",
            IMESSAGE_RETRY_SEC,
        )
        if self._imessage_retry_id is None:
            self._imessage_retry_id = GLib.timeout_add_seconds(
                IMESSAGE_RETRY_SEC, self._imessage_retry_tick
            )

    def _imessage_retry_tick(self) -> bool:
        if self._connect_imessage():
            self._imessage_retry_id = None
            return False  # stop the timer
        return True

    def _on_imessage_event_threaded(self, event: dict) -> None:
        """Called on the client's reader thread — must not touch GLib state.

        Everything downstream (sinks, D-Bus emission, libnotify) assumes the
        main loop's thread, so hand the event over rather than processing it
        here. This is the entire reason the callback is split in two.
        """
        GLib.idle_add(self._on_imessage_event, event)

    def _on_imessage_event(self, event: dict) -> bool:
        # Returning False so idle_add runs this exactly once.
        try:
            # Two different losses, one response. "helper_disconnected" is
            # ours, raised when the unix socket dies. "disconnected" is the
            # helper's own, raised when *Apple's* connection drops while the
            # socket is still perfectly healthy — the case that used to leave
            # the transport looking available while nothing arrived on it.
            if event.get("event") in ("helper_disconnected", "disconnected"):
                log.warning(
                    "iMessage helper disconnected; falling back to MAP and "
                    "retrying every %ds", IMESSAGE_RETRY_SEC
                )
                self._imessage = None
                if self._dbus_service is not None:
                    self._dbus_service.imessage = None
                if self._imessage_retry_id is None:
                    self._imessage_retry_id = GLib.timeout_add_seconds(
                        IMESSAGE_RETRY_SEC, self._imessage_retry_tick
                    )
                return False

            inst = event.get("inst")
            log.debug(
                "iMessage event %s: %s",
                event.get("event"),
                next(iter(inst.get("message")))
                if isinstance(inst, dict) and isinstance(inst.get("message"), dict)
                else (inst or {}).get("message") if isinstance(inst, dict) else "-",
            )
            translated = translate_imessage(event, self._imessage_handles)
            if translated is None:
                return False

            if translated.extras is not None:
                self._remember_extras(translated.extras)

            # Apple delivers one copy of a message per handle of ours that the
            # conversation targets, so an account with three registered
            # handles sees the same message up to three times — same guid,
            # different `certified_context.target`. Deduplicate on the guid so
            # history and notifications get one entry.
            #
            # Only message-bearing events are filtered: a Delivered or Read
            # receipt reuses the guid of the message it refers to, and those
            # produce no SmsEvent anyway.
            #
            # Self-chat is the exception: the "duplicate" is the loopback —
            # your number texting you back. Keep one blue (the send) and turn
            # the second copy into a grey incoming bubble instead of dropping
            # it. Normal chats still drop same-guid copies.
            if translated.event is not None:
                guid = translated.extras.guid if translated.extras else None
                if guid:
                    if guid in self._imessage_seen:
                        if self._is_self_chat(translated):
                            self._as_self_echo(translated)
                            log.debug("self-chat echo of %s → incoming", guid)
                        else:
                            log.debug("dropping duplicate iMessage %s", guid)
                            return False
                    else:
                        # A dict, not a set: dicts preserve insertion order, so
                        # the head really is the oldest when trimming below.
                        self._remember_guid(guid)

            # Receipts and typing indicators update existing conversations
            # rather than adding to them; there's no SmsEvent to fan out, so
            # they go out on their own signal instead.
            if translated.event is None:
                self._emit_state(translated)
                return False

            # An edit or unsend by someone else rewrites a message we already
            # have; it is not a new one. Fanning it out as a message left the
            # original showing its old text while the correction landed as a
            # separate bubble — and, because Apple sends no participant list
            # on an Edit, that bubble had no chat_guid and so filed itself in
            # the sender's 1:1 thread instead of the group it belongs to.
            #
            # Routing it through the state channel is also what makes it
            # survive a restart: only `message_state` rows carry an edit to
            # disk, so before this an edit was lost on the next reload even
            # when it had applied correctly live.
            if self._emit_remote_edit(translated):
                return False

            sms = translated.event
            # MAP events get their contact name resolved the same way; doing it
            # here keeps both transports rendering identically.
            if sms.sender_phone and not sms.contact_name:
                sms.contact_name = self.contacts.resolve(sms.sender_phone)
            self._fanout(sms, translated.extras)
            # After the fanout, never before: the bubble should appear now and
            # fill in its image when the bytes arrive.
            self._fetch_attachments(sms, translated.extras)
        except Exception:
            # An exception here would otherwise vanish into GLib's idle
            # handler and take the event with it, silently.
            log.exception("failed to handle an iMessage event")
        return False

    # Apple's names for these, mapped to the states the UI renders.
    # MessageReadOnDevice means *we* read it elsewhere (iPhone, another Mac),
    # which is the cue to clear our own unread badge rather than to draw a
    # "Read" caption under an outgoing message — but both are "read" as far
    # as a given message's state goes, and the guid says which message.
    _RECEIPT_STATES = {
        "Delivered": "delivered",
        "Read": "read",
        "MessageReadOnDevice": "read",
    }

    def _remember_guid(self, guid: str) -> None:
        """Track a message id we've already fanned out (bounded)."""
        self._imessage_seen[guid] = None
        if len(self._imessage_seen) > self._SEEN_LIMIT:
            # Bounded like _imessage_extras. Duplicates arrive within
            # milliseconds of each other, so only recent guids matter; drop
            # the oldest quarter at once rather than trimming every message.
            for old in list(self._imessage_seen)[: self._SEEN_LIMIT // 4]:
                self._imessage_seen.pop(old, None)

    def _is_self_chat(self, translated) -> bool:
        """True when every participant is one of our own handles.

        Texting your own number (or email) has no other party: Apple still
        echoes the message back with the same guid, and without special
        handling that echo is dropped as a duplicate.
        """
        extras = translated.extras
        if extras is None:
            return False
        participants = list(extras.chat_participants or [])
        if not participants:
            # No roster — fall back to "the peer is us".
            sender = extras.sender or ""
            return bool(sender and sender in self._imessage_handles)
        return all(p in self._imessage_handles for p in participants)

    @staticmethod
    def _as_self_echo(translated) -> None:
        """Rewrite a same-guid self-chat copy into a grey incoming bubble.

        The real message (blue, actionable, keyed by guid) stays as the
        original send. The echo gets a distinct handle so UI handle-dedupe
        doesn't swallow it, and no guid so it can't steal the original's
        place in `_by_guid` (receipts/edits must keep targeting the send).
        """
        sms = translated.event
        if sms is None:
            return
        base = sms.guid or sms.handle or "self"
        sms.kind = "sms_received"
        sms.is_read = True  # self-loop shouldn't light an unread badge
        sms.guid = None
        sms.handle = f"{base}#self-echo"
        if translated.extras is not None:
            translated.extras.outgoing = False

    def _emit_state(self, translated) -> None:
        """Push a delivery/read/typing update out on the bus (and disk)."""
        extras = translated.extras
        guid = (extras.guid if extras else "") or ""
        # The sender, not participants[0]: our own handle is among the
        # participants, so the first entry is not reliably the other party —
        # and a typing indicator routed to ourselves would render in the
        # wrong conversation, or none.
        handle = ""
        if extras:
            handle = extras.sender or ""
            if not handle:
                others = [
                    p for p in extras.chat_participants
                    if p not in self._imessage_handles
                ]
                handle = others[0] if others else ""

        if translated.typing is not None:
            # Typing has no message to attach to, so it routes by handle.
            state = "typing" if translated.typing else "typing_stopped"
        elif translated.receipt:
            state = self._RECEIPT_STATES.get(translated.receipt)
            if state is None:
                log.debug("ignoring unknown receipt %r", translated.receipt)
                return
        else:
            return

        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        log.info("message state: %s %s (%s)", state, guid or "-", handle or "-")
        if self._dbus_service is not None:
            self._dbus_service.emit_message_state(guid, state, handle, ts)
        self._persist_state(guid, state, handle, ts)

    def _fetch_attachments(self, event, extras) -> None:
        """Download an incoming message's attachments, then announce them.

        iMessage delivers attachments by reference: the message carries an
        MMCS locator and a key, not the bytes. Nothing fetched them, so a
        photo or sticker arrived as the bridge's `[name.png]` fallback text —
        the "placeholder" a received image showed up as.

        Runs on a worker thread because the transfer is unbounded network I/O
        and the caller is the GLib main loop. The message itself has already
        been fanned out by then, so it appears immediately and gains its image
        when the bytes land, rather than the whole conversation stalling
        behind a video download.

        The result rides the state channel rather than the message channel for
        the same reason edits do: it modifies a message that already exists.
        That also gets persistence for free — `SqliteSink.handle_state` writes
        it to messages.sqlite, so the image is still there after a restart.
        """
        atts = list(extras.attachments or [])
        guid = extras.guid or ""
        if not atts or not guid:
            return

        client = self._imessage
        if client is None:
            return

        def work() -> None:
            out = []
            for i, att in enumerate(atts):
                uti = (att.get("uti") or "").lower()
                # Shaped like the backup importer's descriptors, because the
                # UI reads one vocabulary for both sources — note `mime`, not
                # the bridge's `mime_type`.
                name = att.get("name") or "attachment"
                mime = att.get("mime_type") or ""
                # Some parts arrive with only a filename; without a mime the
                # UI used to draw a paperclip chip for every photo. Infer the
                # common image types from the extension as a backstop.
                if not mime:
                    lower = name.lower()
                    if lower.endswith(".png"):
                        mime = "image/png"
                    elif lower.endswith((".jpg", ".jpeg")):
                        mime = "image/jpeg"
                    elif lower.endswith(".gif"):
                        mime = "image/gif"
                    elif lower.endswith((".heic", ".heif")):
                        mime = "image/heic"
                    elif lower.endswith(".webp"):
                        mime = "image/webp"
                meta = {
                    "name": name,
                    "mime": mime,
                    "is_sticker": "sticker" in uti,
                }
                try:
                    dest = self._attachment_path(guid, i, att)
                    # By position: the helper holds the attachment, because
                    # its locator and key are byte arrays JSON can't hand back.
                    reply = client.download_attachment(guid, i, str(dest))
                    meta["path"] = reply.get("path") or str(dest)
                    meta["bytes"] = reply.get("bytes") or 0
                    # Recorded once here so the UI can reserve space before
                    # the image decodes, as the backup path does.
                    size = display_size(dest)
                    if size:
                        meta["w"], meta["h"] = size
                except Exception as e:
                    # One unfetchable attachment must not lose the others,
                    # or the message's text.
                    log.warning("attachment %d of %s failed: %s", i, guid, e)
                out.append(meta)
            GLib.idle_add(self._announce_attachments, guid, extras.sender or "", out)

        threading.Thread(target=work, name=f"attach-{guid[:8]}",
                         daemon=True).start()

    @staticmethod
    def _attachment_path(guid: str, index: int, att: dict):
        """Where one attachment's bytes live.

        Named by guid and part index rather than by the sender's filename:
        those are routinely `IMG_0001.png` and would collide across senders,
        and a name arriving from the network must never choose a path.
        """
        name = att.get("name") or ""
        suffix = Path(name).suffix[:10] if name else ""
        if not suffix:
            mime = (att.get("mime_type") or "").lower()
            suffix = ".png" if "png" in mime else (
                ".jpg" if "jpe" in mime else "")
        safe = re.sub(r"[^A-Za-z0-9_-]", "", guid)[:40] or "attachment"
        ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
        return ATTACHMENTS_DIR / f"{safe}-{index}{suffix}"

    def _announce_attachments(self, guid: str, handle: str,
                              attachments: list[dict]) -> bool:
        """Publish downloaded attachments. Runs back on the main loop."""
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        body = json.dumps(attachments)
        log.info("attachments ready for %s (%d)", guid, len(attachments))
        if self._dbus_service is not None:
            self._dbus_service.emit_message_state(
                guid, "attachments", handle, ts, body)
        self._persist_state(guid, "attachments", handle, ts, body)
        return False  # idle_add: run once

    def _emit_remote_edit(self, translated) -> bool:
        """Send someone else's edit/unsend on the state channel. True if it was one.

        The mirror image of `MessagesService._echo_local_change`, which covers
        edits *we* make (Apple relays those to our other devices but never back
        to the sender). Between them every edit reaches the UI the same way,
        whichever end it came from.

        The guid on the wire is the *target* — the message being rewritten —
        not the edit event's own id, because that's what the UI looks a message
        up by.
        """
        extras = translated.extras
        target = (extras.edited_from_guid if extras else "") or ""
        if not target:
            return False

        state = "unsent" if extras.unsent else "edited"
        # For an unsend the bridge already put "[message unsent]" here, which
        # is the same placeholder `_echo_local_change` sends for our own — so
        # both ends render identically.
        body = translated.event.body or ""
        handle = extras.sender or ""
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        log.info("remote %s of %s (%s)", state, target, handle or "-")
        if self._dbus_service is not None:
            self._dbus_service.emit_message_state(target, state, handle, ts, body)
        self._persist_state(target, state, handle, ts, body)
        return True

    def _dismiss_notifications(self, peer: str) -> int:
        """The UI opened a conversation — take its popups off the screen.

        Looked up by name rather than held as a reference, because the
        libnotify sink is optional: it fails to construct on a machine with no
        notification server and the daemon carries on without it.
        """
        closed = 0
        for sink in self.sinks:
            dismiss = getattr(sink, "dismiss_for", None)
            if dismiss is None:
                continue
            try:
                closed += int(dismiss(peer) or 0)
            except Exception:
                log.exception("sink %s failed to dismiss popups", sink.name)
        return closed

    def _set_active_thread(self, peer: str, focused: bool) -> None:
        """UI focus: which conversation is open and whether the window is active.

        Libnotify uses this to skip popups for a thread the user is already
        looking at. Optional sinks without the method are ignored.
        """
        for sink in self.sinks:
            setter = getattr(sink, "set_active_thread", None)
            if setter is None:
                continue
            try:
                setter(peer, focused)
            except Exception:
                log.exception("sink %s failed to set active thread", sink.name)

    def _persist_state(
        self,
        guid: str,
        state: str,
        handle: str = "",
        timestamp: str = "",
        body: str = "",
    ) -> None:
        """Append a state record so captions/edits survive a restart."""
        props = {
            "guid": guid,
            "state": state,
            "handle": handle,
            "timestamp": timestamp,
            "body": body,
        }
        for sink in self.sinks:
            handler = getattr(sink, "handle_state", None)
            if handler is None:
                continue
            try:
                handler(props)
            except Exception:
                log.exception("sink %s failed on state %s", sink.name, state)

    def _remember_extras(self, extras) -> None:
        if len(self._imessage_extras) >= self._EXTRAS_LIMIT:
            # Drop the oldest quarter rather than one at a time, so this
            # doesn't run on nearly every message once the cap is reached.
            # dicts preserve insertion order, so the head is the oldest.
            for guid in list(self._imessage_extras)[: self._EXTRAS_LIMIT // 4]:
                self._imessage_extras.pop(guid, None)
        self._imessage_extras[extras.guid] = extras

    def _fanout(self, event: SmsEvent, extras=None) -> None:
        # Every transport funnels through here, which makes it the one place
        # cross-transport duplicates can be caught — including MAP echoing
        # back a message we just sent ourselves.
        #
        # Deliberately after the iMessage extras have been recorded (see
        # _on_imessage_event), so suppressing a row never discards the richer
        # metadata that came with it.
        #
        # Self-chat echoes deliberately restated the same body on a second
        # transport-ish path; don't let the heuristic eat the grey bubble.
        is_self_echo = (event.handle or "").endswith("#self-echo")
        if not is_self_echo and self._deduper.is_duplicate(event):
            # MAP often beats iMessage for phone-sent traffic and carries no
            # guid. Dropping the iMessage copy entirely left the UI with a
            # bubble that could never receive Delivered/Read (those are
            # keyed by guid). Still emit/persist a guid-bearing copy so the
            # UI can upgrade the MAP row in place; the UI merges on body
            # rather than drawing a second bubble.
            if event.guid:
                log.debug(
                    "transport-dupe of %s has guid %s — emitting for upgrade",
                    event.handle, event.guid,
                )
                for sink in self.sinks:
                    try:
                        sink.handle(event)
                    except Exception:
                        log.exception("sink %s failed on upgrade event %s",
                                      sink.name, event.handle)
                if self._dbus_service is not None:
                    self._dbus_service.emit_message(event, extras)
            else:
                log.debug("suppressed duplicate message %s", event.handle)
            return
        for sink in self.sinks:
            try:
                sink.handle(event)
            except Exception:
                log.exception("sink %s failed on event %s",
                              sink.name, event.handle)
        if self._dbus_service is not None:
            # Sinks take the bare event — they persist history, which is a
            # MAP-era format. Only the bus carries the iMessage extras.
            self._dbus_service.emit_message(event, extras)

    def _send_reply(
        self,
        recipient: str,
        body: str,
        reply_to_guid: str = "",
        target_text: str = "",
    ) -> str:
        """Send from outside the D-Bus API (inline notification reply).

        Prefers the native iMessage path so group recipients and threaded
        replies (reply_to_guid) work; MAP can only push plain 1:1 text.
        """
        if self._imessage is not None:
            try:
                from verdigris.dbus_service import _reply_part
                from verdigris.imessage.handles import to_handle

                participants = [
                    to_handle(r) for r in recipient.split(",") if r.strip()
                ]
                if not participants:
                    raise ValueError("empty recipient")
                if reply_to_guid:
                    guid = self._imessage.send(
                        participants, body,
                        reply_guid=reply_to_guid,
                        reply_part=_reply_part(target_text),
                    )
                else:
                    guid = self._imessage.send(participants, body)
                self._record_sent(recipient, body, guid, reply_to_guid)
                return guid
            except Exception as e:
                # Fall through to MAP for plain 1:1; groups / threaded replies
                # have no MAP equivalent, so log and re-raise those.
                if reply_to_guid or "," in recipient:
                    log.exception("inline reply via iMessage failed")
                    raise
                log.warning(
                    "iMessage inline reply failed (%s); falling back to MAP", e
                )

        if self.sessions.map is None:
            raise SessionError("MAP session not open")
        # MAP addresses one phone; for a multi-recipient string take the first.
        map_to = recipient.split(",")[0].strip()
        transfer = map_send_message(self.sessions.map_path, map_to, body)
        self._record_sent(recipient, body, transfer)
        return transfer

    def _is_self_recipient(self, recipient: str) -> bool:
        """True when `recipient` is one of our own iMessage addresses."""
        if not recipient or not self._imessage_handles:
            return False
        from verdigris.events import normalize_phone
        from verdigris.imessage.handles import handle_matches, to_handle

        try:
            handle = to_handle(recipient)
        except ValueError:
            return False
        if handle in self._imessage_handles:
            return True
        # Phone formats disagree (`+1…` vs digits-only); match the tail.
        norm = normalize_phone(recipient)
        if not norm:
            return False
        return any(
            h.startswith("tel:") and handle_matches(h, norm)
            for h in self._imessage_handles
        )

    def _record_sent(self, recipient: str, body: str, transfer_path: str,
                     reply_to_guid: str = "") -> None:
        """Hook for DBus Send() — log + broadcast a message we just sent so it
        shows up in conversation history alongside incoming messages.

        `transfer_path` is whatever the transport returned: an obex path for
        MAP, a message guid for iMessage. They're told apart by shape — a guid
        is a bare UUID, an obex handle is a slash-separated path. Keeping the
        guid matters because delivery receipts are keyed to it, and because
        every UI action needs it to name its target.
        """
        guid = transfer_path if _looks_like_guid(transfer_path) else None
        event = sms_sent_event(
            recipient, body,
            contact_name=self.contacts.resolve(recipient),
            transfer_path=transfer_path,
            guid=guid,
            reply_to_guid=reply_to_guid or None,
        )
        log.info("sms_sent to %s: %r", event.display_sender, (body or "")[:80])
        # So Apple's same-guid echo of this send is recognised as a duplicate
        # (dropped in normal chats, flipped to a grey self-echo in self-chat)
        # rather than a second blue bubble.
        if guid:
            self._remember_guid(guid)
        # The reply target rides on the event itself rather than in `extras`:
        # it has to reach disk, so that a reply reloaded from history still
        # reads as a reply instead of turning back into an ordinary message.
        self._fanout(event)
        # Self-chat: Apple often never delivers a second Message event for
        # the loopback (verified live: only MessageSent, no MessageReceived).
        # Synthesize the grey "texted me back" bubble here so the conversation
        # behaves the way the user asked for, without waiting on a copy that
        # may never arrive. A later Apple same-guid copy reuses the same
        # `#self-echo` handle and the UI handle-dedupe drops it.
        if guid and self._is_self_recipient(recipient):
            self._fanout(self._synthetic_self_echo(event, guid))
            log.info("self-chat: synthesized grey echo for %s", guid)

    @staticmethod
    def _synthetic_self_echo(sent: SmsEvent, guid: str) -> SmsEvent:
        """Grey incoming twin of a message we just sent to ourselves."""
        return SmsEvent(
            kind="sms_received",
            handle=f"{guid}#self-echo",
            sender_phone=sent.sender_phone,
            sender_phone_norm=sent.sender_phone_norm,
            contact_name=sent.contact_name,
            body=sent.body,
            timestamp=sent.timestamp,
            is_read=True,
            raw_status="self-echo",
            raw_type="iMessage",
            guid=None,
            reply_to_guid=sent.reply_to_guid,
        )

    def _fanout_ancs(self, event: AncsEvent) -> None:
        remember_app(event.app_id, event.app_name)
        if not preferences.rule(event.app_id).enabled:
            return
        for sink in self.sinks:
            try:
                handler = getattr(sink, "handle_ancs", None)
                if handler is None:
                    continue  # sink doesn't know about ANCS events
                handler(event)
            except Exception:
                log.exception("sink %s failed on ANCS event %d",
                              sink.name, event.notification_id)
        if self._dbus_service is not None:
            self._dbus_service.emit_ancs(event)

    def _fanout_call(self, event: CallEvent) -> None:
        for sink in self.sinks:
            try:
                handler = getattr(sink, "handle_call", None)
                if handler is None:
                    continue  # sink doesn't know about call events
                handler(event)
            except Exception:
                log.exception("sink %s failed on call event %s",
                              sink.name, event.call_path)
        if self._dbus_service is not None:
            self._dbus_service.emit_call_state(event)

    def _signal(self, signum, _frame):
        log.info("received signal %d, stopping", signum)
        main_loop.quit()
