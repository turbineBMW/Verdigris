"""ANCS GATT client — subscribes to the three ANCS characteristics on a
paired iPhone, decodes Notification Source events, writes
GetNotificationAttributes commands to Control Point, parses Data Source
responses, and emits AncsEvents.

Design notes (from bmh129/ancs4linux + verdigris's Phase 0):

- We don't trust Device1.ServicesResolved as a readiness signal because
  BlueZ flips it true after BR/EDR SDP, before BLE GATT enumerates. Instead,
  we listen for ObjectManager.InterfacesAdded and wait for all three ANCS
  characteristic UUIDs to show up under the target iPhone's device path.

- StartNotify is called on Notification Source and Data Source as soon as
  they're present + we're paired. Control Point is write-only.

- For each NotificationAdded/Modified event we get on NS, we synthesize a
  GetNotificationAttributes packet asking for AppIdentifier+Title+Subtitle+
  Message (plus positive/negative action labels if the iPhone declared
  them) and write it to CP. The response comes back on DS.

- App display names are looked up lazily via GetAppAttributes and cached.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

import dbus
import dbus.exceptions

from verdigris.ancs.constants import (
    ANCS_CHAR_UUIDS,
    CATEGORY_NAMES,
    CONTROL_POINT_CHAR,
    DATA_SOURCE_CHAR,
    NOTIFICATION_SOURCE_CHAR,
    CommandID,
    EventID,
)
from verdigris.ancs.events import AncsEvent
from verdigris.ancs.parsers import (
    AppAttributes,
    DataSourceEvent,
    Notification,
    NotificationAttributes,
    build_get_app_attributes,
    build_get_notification_attributes,
)
from verdigris.bus import system_bus

log = logging.getLogger(__name__)


def _char_path_to_device_path(char_path: str) -> str:
    """Drop the last two segments: …/hciN/dev_XX/serviceYYYY/charZZZZ → …/dev_XX."""
    return "/".join(char_path.rsplit("/", 2)[:-2])


class AncsClient:
    """Tracks ANCS characteristics under one target device and subscribes
    when all three are present.

    Idempotent: calling start() multiple times is harmless; if chars are
    already present at start time, we hook them immediately.
    """

    def __init__(
        self,
        device_path: str,
        on_event: Callable[[AncsEvent], None],
    ) -> None:
        self.device_path = device_path
        self.on_event = on_event

        # Char path slots — set as InterfacesAdded fires
        self._ns_path: str | None = None
        self._ds_path: str | None = None
        self._cp_path: str | None = None
        self._notify_started = False

        # In-flight per-notification attribute requests + app-name cache
        self._app_name_cache: dict[str, str] = {}
        self._pending_app_lookups: dict[str, list[NotificationAttributes]] = {}
        # Preserve the Notification Source metadata until its asynchronous
        # Data Source attributes arrive.  Category/flags are also useful to
        # desktop sinks (including category-specific icon fallbacks).
        self._pending_notifications: dict[int, Notification] = {}

        # ObjectManager subscriptions — live for the client's lifetime.
        self._signal_matches: list = []
        # PropertiesChanged receivers on the current NS/DS chars. Torn down
        # whenever the chars vanish: BlueZ reuses handle paths across LE
        # reconnects, so a stale receiver left on an old path would wake up
        # again next time the same path comes back and every notification
        # would be delivered (NS receivers x DS receivers) times.
        self._char_matches: list = []

    # ---- lifecycle ------------------------------------------------------

    def start(self) -> None:
        log.info("ANCS client starting; watching %s", self.device_path)
        om = dbus.Interface(
            system_bus.get_object("org.bluez", "/"),
            "org.freedesktop.DBus.ObjectManager",
        )
        self._signal_matches.append(
            om.connect_to_signal("InterfacesAdded", self._on_iface_added)
        )
        self._signal_matches.append(
            om.connect_to_signal("InterfacesRemoved", self._on_iface_removed)
        )
        # Sweep current state — ANCS chars may already exist if we're
        # restarting against a live pair.
        managed = om.GetManagedObjects()
        for path, ifaces in managed.items():
            self._on_iface_added(path, ifaces)

    def stop(self) -> None:
        log.info("ANCS client stopping")
        for m in self._signal_matches:
            try:
                m.remove()
            except Exception:
                pass
        self._signal_matches = []
        self._unsubscribe_chars()
        # StopNotify on the chars if we'd started
        for path in (self._ns_path, self._ds_path):
            if path:
                try:
                    dbus.Interface(
                        system_bus.get_object("org.bluez", path),
                        "org.bluez.GattCharacteristic1",
                    ).StopNotify()
                except dbus.exceptions.DBusException:
                    pass
        self._ns_path = self._ds_path = self._cp_path = None
        self._notify_started = False
        self._pending_notifications.clear()
        self._pending_app_lookups.clear()

    # ---- ObjectManager event handlers -----------------------------------

    def _on_iface_added(self, path, ifaces):
        path_s = str(path)
        char = ifaces.get("org.bluez.GattCharacteristic1")
        if char is None:
            return
        uuid = str(char.get("UUID", "")).lower()
        if uuid not in ANCS_CHAR_UUIDS:
            return
        if _char_path_to_device_path(path_s) != self.device_path:
            return
        if uuid == NOTIFICATION_SOURCE_CHAR:
            self._ns_path = path_s
            log.info("ANCS Notification Source found: %s", path_s)
        elif uuid == DATA_SOURCE_CHAR:
            self._ds_path = path_s
            log.info("ANCS Data Source found:         %s", path_s)
        elif uuid == CONTROL_POINT_CHAR:
            self._cp_path = path_s
            log.info("ANCS Control Point found:       %s", path_s)
        self._try_subscribe()

    def _on_iface_removed(self, path, ifaces):
        path_s = str(path)
        for attr in ("_ns_path", "_ds_path", "_cp_path"):
            if getattr(self, attr) == path_s:
                setattr(self, attr, None)
                self._unsubscribe_chars()
                self._notify_started = False
                log.warning("ANCS char gone: %s", path_s)

    def _unsubscribe_chars(self) -> None:
        """Drop the PropertiesChanged receivers on the NS/DS chars."""
        for m in self._char_matches:
            try:
                m.remove()
            except Exception:
                pass
        self._char_matches = []

    def _try_subscribe(self) -> None:
        if self._notify_started:
            return
        if not (self._ns_path and self._ds_path and self._cp_path):
            return
        # Subscribe to NS + DS via StartNotify and PropertiesChanged.
        try:
            ns = dbus.Interface(
                system_bus.get_object("org.bluez", self._ns_path),
                "org.bluez.GattCharacteristic1",
            )
            ds = dbus.Interface(
                system_bus.get_object("org.bluez", self._ds_path),
                "org.bluez.GattCharacteristic1",
            )
            ns.StartNotify()
            ds.StartNotify()
        except dbus.exceptions.DBusException as e:
            log.warning("ANCS StartNotify failed: %s", e.get_dbus_name())
            return

        self._char_matches.append(
            system_bus.add_signal_receiver(
                self._on_ns_changed,
                dbus_interface="org.freedesktop.DBus.Properties",
                signal_name="PropertiesChanged",
                path=self._ns_path,
            )
        )
        self._char_matches.append(
            system_bus.add_signal_receiver(
                self._on_ds_changed,
                dbus_interface="org.freedesktop.DBus.Properties",
                signal_name="PropertiesChanged",
                path=self._ds_path,
            )
        )
        self._notify_started = True
        log.info("ANCS subscription active for %s", self.device_path)

    # ---- Notification Source: new/modified/removed events --------------

    def _on_ns_changed(self, iface, changed, _invalidated):
        if iface != "org.bluez.GattCharacteristic1":
            return
        value = changed.get("Value")
        if value is None:
            return
        try:
            n = Notification.parse(bytes(value))
        except Exception as e:
            log.error("NS parse failed: %s", e)
            return
        # Skip pre-existing (notifications that already existed on the
        # iPhone at our connect time — too noisy on initial subscribe).
        if n.is_preexisting:
            log.debug("ANCS preexisting event uid=%d cat=%d — skipping",
                      n.id, n.category)
            return
        if n.type == EventID.NotificationRemoved:
            self._pending_notifications.pop(n.id, None)
            log.debug("ANCS removed uid=%d", n.id)
            return
        # Added or Modified → request full attrs
        self._pending_notifications[n.id] = n
        self._request_attrs(n)

    def _request_attrs(self, n: Notification) -> None:
        if not self._cp_path:
            return
        pkt = build_get_notification_attributes(
            n.id,
            want_positive=n.has_positive_action,
            want_negative=n.has_negative_action,
        )
        try:
            dbus.Interface(
                system_bus.get_object("org.bluez", self._cp_path),
                "org.bluez.GattCharacteristic1",
            ).WriteValue([dbus.Byte(b) for b in pkt], {})
        except dbus.exceptions.DBusException as e:
            log.warning("CP WriteValue failed: %s", e.get_dbus_name())

    # ---- Data Source: responses to our CP writes ------------------------

    def _on_ds_changed(self, iface, changed, _invalidated):
        if iface != "org.bluez.GattCharacteristic1":
            return
        value = changed.get("Value")
        if value is None:
            return
        try:
            ev = DataSourceEvent.parse(bytes(value))
        except Exception as e:
            log.error("DS parse failed: %s", e)
            return
        if ev.type == CommandID.GetNotificationAttributes:
            try:
                attrs = NotificationAttributes.parse(ev.body)
            except Exception as e:
                log.error("NotificationAttributes parse failed: %s", e)
                return
            self._handle_notification_attrs(attrs)
        elif ev.type == CommandID.GetAppAttributes:
            try:
                app_attrs = AppAttributes.parse(ev.body)
            except Exception as e:
                log.error("AppAttributes parse failed: %s", e)
                return
            self._handle_app_attrs(app_attrs)

    def _handle_notification_attrs(self, attrs: NotificationAttributes) -> None:
        # If we don't have the app's display name yet, queue and ask the
        # iPhone for it. Otherwise emit immediately.
        if attrs.app_id in self._app_name_cache:
            self._emit(attrs, self._app_name_cache[attrs.app_id])
        else:
            self._pending_app_lookups.setdefault(attrs.app_id, []).append(attrs)
            self._request_app_name(attrs.app_id)

    def _request_app_name(self, app_id: str) -> None:
        if not self._cp_path:
            return
        try:
            dbus.Interface(
                system_bus.get_object("org.bluez", self._cp_path),
                "org.bluez.GattCharacteristic1",
            ).WriteValue(
                [dbus.Byte(b) for b in build_get_app_attributes(app_id)], {}
            )
        except dbus.exceptions.DBusException as e:
            log.warning("App-name lookup failed for %s: %s",
                        app_id, e.get_dbus_name())

    def _handle_app_attrs(self, app_attrs: AppAttributes) -> None:
        self._app_name_cache[app_attrs.app_id] = app_attrs.app_name
        pending = self._pending_app_lookups.pop(app_attrs.app_id, [])
        for attrs in pending:
            self._emit(attrs, app_attrs.app_name)

    def _emit(self, attrs: NotificationAttributes, app_name: str) -> None:
        source = self._pending_notifications.pop(attrs.id, None)
        event = AncsEvent(
            notification_id=attrs.id,
            device_path=self.device_path,
            app_id=attrs.app_id,
            app_name=app_name,
            title=attrs.title,
            subtitle=attrs.subtitle,
            body=attrs.message,
            category=CATEGORY_NAMES.get(source.category, "Other") if source else "Other",
            is_silent=source.is_silent if source else False,
            is_preexisting=source.is_preexisting if source else False,
            positive_action=attrs.positive_action,
            negative_action=attrs.negative_action,
        )
        try:
            self.on_event(event)
        except Exception:
            log.exception("on_event callback raised")
