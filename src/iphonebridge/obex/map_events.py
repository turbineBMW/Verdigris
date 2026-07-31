"""MAP MNS event subscription + bMessage fetch.

The push notification we receive from the iPhone (via the InterfacesAdded
signal on a new org.bluez.obex.Message1 object) is intentionally skinny —
it tells us a message exists but does NOT include sender or body. We
have to call Message1.Get(targetfile) to download the full bMessage and
parse it ourselves.

Flow:
  1. InterfacesAdded → new Message1 path appears
  2. Call Get(target, attachment=False) → returns transfer DBus path
  3. Subscribe to PropertyChanged on the transfer
  4. When Status flips to "complete" → read file, parse bMessage, fire callback
  5. Clean up temp file
"""
from __future__ import annotations

import logging
import tempfile
from collections.abc import Callable
from pathlib import Path

import dbus

from iphonebridge import config
from iphonebridge.bus import obex, session_bus
from iphonebridge.events import SmsEvent, normalize_phone, parse_map_timestamp
from iphonebridge.obex.bmessage import parse as parse_bmessage
from iphonebridge.obex.sessions import SessionManager

log = logging.getLogger(__name__)

EventCallback = Callable[[SmsEvent], None]


class MapEventListener:
    """Subscribes to MAP MNS push events and dispatches normalized SmsEvents."""

    def __init__(
        self,
        sessions: SessionManager,
        on_sms: EventCallback,
        *,
        resolve_contact: Callable[[str | None], str | None] = lambda _: None,
    ) -> None:
        self.sessions = sessions
        self.on_sms = on_sms
        self.resolve_contact = resolve_contact
        self._signal_match = None
        # Track pending transfers so we can correlate PropertyChanged signals
        # back to the message path that triggered them.
        self._pending: dict[str, _PendingFetch] = {}  # transfer_path -> ctx

    def start(self) -> None:
        om = dbus.Interface(
            session_bus.get_object("org.bluez.obex", "/"),
            "org.freedesktop.DBus.ObjectManager",
        )
        self._signal_match = om.connect_to_signal(
            "InterfacesAdded", self._on_interfaces_added
        )
        log.info("MAP MNS listener started (filtering on %s)",
                 self.sessions.map_path)

    def stop(self) -> None:
        if self._signal_match is not None:
            try:
                self._signal_match.remove()
            except Exception:
                pass
            self._signal_match = None
        # Cancel any pending fetches' signal subscriptions
        for p in list(self._pending.values()):
            p.cleanup()
        self._pending.clear()
        log.info("MAP MNS listener stopped")

    # ---- signal handlers -------------------------------------------------

    def _on_interfaces_added(self, path, ifaces):
        path_s = str(path)
        if not path_s.startswith(self.sessions.map_path):
            return
        if "org.bluez.obex.Message1" not in ifaces:
            return

        props = dict(ifaces["org.bluez.obex.Message1"])
        handle = path_s.rsplit("/", 1)[-1]
        log.info("new Message1 at %s (Status=%s Type=%s Size=%s) — fetching body",
                 handle, props.get("Status"), props.get("Type"),
                 props.get("Size"))

        # Kick off the bMessage download. We do this via the per-message
        # Message1.Get method, which returns a transfer object. We'll wait
        # for Status=complete via PropertyChanged on that transfer.
        target = Path(tempfile.mkstemp(prefix="ibridge_msg_", suffix=".bmsg")[1])
        try:
            msg_iface = obex(path_s, "org.bluez.obex.Message1")
            # Second arg is MAP's "Attachment" flag. It was hardcoded False,
            # which explicitly asks the phone to strip attachments from every
            # message — so we could never have seen one. Ask for them; for a
            # plain SMS with nothing attached this costs nothing.
            ret = msg_iface.Get(str(target), True)
            transfer_path = str(ret[0]) if isinstance(ret, (tuple, list)) else str(ret)
        except dbus.exceptions.DBusException as e:
            log.error("Message1.Get failed for %s: %s", handle,
                      e.get_dbus_name())
            target.unlink(missing_ok=True)
            return

        pending = _PendingFetch(
            listener=self,
            handle=handle,
            message_path=path_s,
            transfer_path=transfer_path,
            target=target,
            initial_props=props,
        )
        self._pending[transfer_path] = pending
        pending.subscribe()


# ---- per-transfer state machine ----------------------------------------

class _PendingFetch:
    """One in-flight Message1.Get transfer."""

    def __init__(
        self,
        listener: MapEventListener,
        handle: str,
        message_path: str,
        transfer_path: str,
        target: Path,
        initial_props: dict,
    ) -> None:
        self.listener = listener
        self.handle = handle
        self.message_path = message_path
        self.transfer_path = transfer_path
        self.target = target
        self.initial_props = initial_props
        self._match = None

    def subscribe(self) -> None:
        self._match = session_bus.add_signal_receiver(
            self._on_props_changed,
            dbus_interface="org.freedesktop.DBus.Properties",
            signal_name="PropertiesChanged",
            path=self.transfer_path,
        )

    def cleanup(self) -> None:
        if self._match is not None:
            try:
                self._match.remove()
            except Exception:
                pass
            self._match = None
        try:
            self.target.unlink(missing_ok=True)
        except OSError:
            pass

    # ---- the actual handler ---------------------------------------------

    def _on_props_changed(self, iface, changed, _invalidated):
        if iface != "org.bluez.obex.Transfer1":
            return
        status = changed.get("Status")
        if status is None:
            return
        status_s = str(status)
        if status_s not in ("complete", "error"):
            return

        try:
            if status_s == "error":
                log.warning("transfer error for %s; firing event with minimal data",
                            self.handle)
                self._fire_minimal()
                return

            if not self.target.exists() or self.target.stat().st_size == 0:
                log.warning("transfer complete but no file for %s", self.handle)
                self._fire_minimal()
                return

            blob = self.target.read_text(errors="replace")
            self._maybe_dump(blob)
            parsed = parse_bmessage(blob)
            self._fire_full(parsed)
        finally:
            self.cleanup()
            self.listener._pending.pop(self.transfer_path, None)

    def _maybe_dump(self, blob: str) -> None:
        """Keep a copy of any bMessage that looks like it carries more than
        plain text, so its structure can be inspected.

        We've never seen an attachment come through — attachments were
        being suppressed at fetch time — so there's no parser for them yet.
        Capture real samples rather than guess at the format.
        """
        markers = ("BEGIN:BATT", "Content-Type:", "Content-Transfer-Encoding",
                   "base64", "MMS", "image/", "video/", "audio/")
        if not any(m.lower() in blob.lower() for m in markers):
            return
        try:
            config.ensure_dirs()
            dump_dir = config.STATE_DIR / "bmsg_samples"
            dump_dir.mkdir(parents=True, exist_ok=True)
            path = dump_dir / f"{self.handle}.bmsg"
            path.write_text(blob, errors="replace")
            log.warning("bMessage %s looks like it carries an attachment "
                        "(%d bytes) — saved sample to %s",
                        self.handle, len(blob), path)
        except OSError as e:
            log.debug("could not dump bMessage sample: %s", e)

    @staticmethod
    def _kind_for(folder: str | None) -> str:
        """Direction from the bMessage FOLDER field.

        iOS pushes messages you send from the phone as well as ones you
        receive; the only thing distinguishing them is the folder
        (telecom/msg/sent vs .../inbox). Without this everything was
        recorded as incoming, so your own sent messages showed up as if the
        other person had said them.
        """
        f = (folder or "").lower()
        if "sent" in f or "outbox" in f:
            return "sms_sent"
        return "sms_received"

    def _fire_full(self, parsed) -> None:
        sender_raw = parsed.sender_phone
        norm = normalize_phone(sender_raw)
        # Prefer the parsed phone for resolution; fall back to bMessage's FN
        contact = self.listener.resolve_contact(sender_raw) if sender_raw else None
        if contact is None and parsed.sender_name:
            contact = parsed.sender_name
        # Use timestamp from initial props (BlueZ tends to populate it on the
        # Message1 object) when available; otherwise None
        ts = parse_map_timestamp(self.initial_props.get("Timestamp"))
        kind = self._kind_for(
            parsed.folder or str(self.initial_props.get("Folder") or ""))
        event = SmsEvent(
            kind=kind,
            handle=self.handle,
            sender_phone=sender_raw,
            sender_phone_norm=norm,
            contact_name=contact,
            # A sent message is already read by definition.
            body=parsed.body,
            timestamp=ts,
            is_read=(kind == "sms_sent"
                     or str(parsed.status or "").upper() == "READ"),
            raw_status=str(self.initial_props.get("Status") or "") or None,
            raw_type=parsed.type or str(self.initial_props.get("Type") or "") or None,
            message_path=self.message_path,
        )
        log.info("%s %s %s: %r (folder=%s)",
                 kind, "to" if kind == "sms_sent" else "from",
                 event.display_sender, (event.body or "")[:80], parsed.folder)
        try:
            self.listener.on_sms(event)
        except Exception:
            log.exception("on_sms callback raised")

    def _fire_minimal(self) -> None:
        """Fallback: fire what little we know, so a notification still shows."""
        event = SmsEvent(
            kind="sms_received",
            handle=self.handle,
            sender_phone=None,
            sender_phone_norm=None,
            contact_name=None,
            body=None,
            timestamp=None,
            is_read=False,
            raw_status=str(self.initial_props.get("Status") or "") or None,
            raw_type=str(self.initial_props.get("Type") or "") or None,
            message_path=self.message_path,
        )
        try:
            self.listener.on_sms(event)
        except Exception:
            log.exception("on_sms callback raised")
