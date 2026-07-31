"""BlueZ adapter prep — the toggle-dance from spike/RESULTS.md §1.

For MAP/PBAP to be reachable on iOS 26.5, three things must be true on
the Linux side:

1. Adapter Class-of-Device set to A/V Hands-Free (Major=4 Minor=8).
2. A BLE peripheral advert is active with SolicitUUIDs containing the
   ANCS UUID. Without this, the iPhone never surfaces the per-device
   "Show Message Notifications" / "Sync Contacts" toggles.
3. Adapter is powered.

This module owns those three concerns. Re-run safely on startup.

CoD setting requires CAP_NET_ADMIN (essentially root), so we shell out to
sudo btmgmt unless we detect we already have it. The BLE advert is
user-bus DBus and needs no privileges.
"""
from __future__ import annotations

import logging
import os
import subprocess
from typing import Any

import dbus
import dbus.exceptions
import dbus.service

from iphonebridge import config
from iphonebridge.bus import bluez, system_bus

log = logging.getLogger(__name__)


# ---- device link --------------------------------------------------------

def _device_path() -> str:
    """BlueZ object path for the paired iPhone."""
    return (f"/org/bluez/hci0/dev_"
            f"{config.IPHONE_MAC.replace(':', '_').upper()}")


def device_connected() -> bool:
    """Is the iPhone currently connected over Bluetooth?"""
    try:
        props = dbus.Interface(
            system_bus.get_object("org.bluez", _device_path()),
            "org.freedesktop.DBus.Properties")
        return bool(props.Get("org.bluez.Device1", "Connected"))
    except dbus.exceptions.DBusException:
        return False


def connect_device(timeout: float = 20.0) -> bool:
    """Ask BlueZ to reconnect the iPhone.

    iOS re-connects on its own most of the time, but not always — after a
    long absence it can sit there paired-but-idle indefinitely, which looks
    exactly like "messages stopped working".
    """
    if device_connected():
        return True
    try:
        dev = dbus.Interface(
            system_bus.get_object("org.bluez", _device_path()),
            "org.bluez.Device1")
        dev.Connect(timeout=timeout)
        log.info("reconnected to iPhone")
        return True
    except dbus.exceptions.DBusException as e:
        log.debug("Device1.Connect failed: %s", e.get_dbus_name())
        return False


# ---- Class-of-Device ----------------------------------------------------

def current_cod() -> int | None:
    """Return adapter Class field, or None if unavailable."""
    try:
        v = bluez(f"/org/bluez/{config.ADAPTER}",
                  "org.freedesktop.DBus.Properties").Get(
            "org.bluez.Adapter1", "Class")
        return int(v)
    except dbus.exceptions.DBusException:
        return None


def desired_cod_matches(cod: int | None) -> bool:
    """Major & Minor match what we want? Service-class bits are derived
    by BlueZ from registered profiles, so we only compare the low 16 bits
    of (Major<<8 | Minor<<2)."""
    if cod is None:
        return False
    major = (cod >> 8) & 0x1F
    minor = (cod >> 2) & 0x3F
    return major == config.COD_MAJOR and (minor << 2) == config.COD_MINOR


def set_cod(*, dry_run: bool = False) -> bool:
    """Apply A/V Hands-Free CoD via btmgmt. Returns True on success."""
    cmd = ["btmgmt", "class", str(config.COD_MAJOR), str(config.COD_MINOR)]
    if os.geteuid() != 0:
        cmd = ["sudo", "-n"] + cmd  # non-interactive sudo; user pre-grants
    log.info("setting adapter CoD via: %s", " ".join(cmd))
    if dry_run:
        return True
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log.error("btmgmt failed: %s", e)
        return False
    if r.returncode != 0:
        log.error("btmgmt class %d %d failed (rc=%d): %s",
                  config.COD_MAJOR, config.COD_MINOR, r.returncode,
                  r.stderr.strip() or r.stdout.strip())
        return False
    log.info("CoD set ok: %s", r.stdout.strip())
    return True


# ---- BLE advertisement (SolicitUUIDs = ANCS) ----------------------------

class _AncsAdvert(dbus.service.Object):
    """Minimal LEAdvertisement1 object so iOS shows our toggles."""

    PATH = config.BLE_ADVERT_DBUS_PATH

    @dbus.service.method("org.bluez.LEAdvertisement1",
                         in_signature="", out_signature="")
    def Release(self) -> None:
        return None

    @dbus.service.method("org.freedesktop.DBus.Properties",
                         in_signature="s", out_signature="a{sv}")
    def GetAll(self, iface: str) -> dict[str, Any]:
        if iface != "org.bluez.LEAdvertisement1":
            raise dbus.exceptions.DBusException(
                f"Unknown interface {iface}",
                name="org.freedesktop.DBus.Error.InvalidArgs")
        return {
            "Type": dbus.String("peripheral"),
            "SolicitUUIDs": dbus.Array([config.ANCS_SOLICIT_UUID], signature="s"),
            "LocalName": dbus.String(config.BLE_ADVERT_LOCAL_NAME),
            "Includes": dbus.Array(["tx-power"], signature="s"),
        }

    @dbus.service.method("org.freedesktop.DBus.Properties",
                         in_signature="ss", out_signature="v")
    def Get(self, iface: str, prop: str):
        return self.GetAll(iface)[prop]


_advert_instance: _AncsAdvert | None = None


def register_advert() -> bool:
    """Register the BLE advertisement on the system bus.

    Idempotent — calling twice is harmless because BlueZ will reject the
    second registration and we treat that as success.
    """
    global _advert_instance
    if _advert_instance is None:
        _advert_instance = _AncsAdvert(system_bus, _AncsAdvert.PATH)

    ad_mgr = bluez(f"/org/bluez/{config.ADAPTER}",
                   "org.bluez.LEAdvertisingManager1")
    try:
        # BlueZ's RegisterAdvertisement frequently NoReply-timeouts even
        # though it actually registers. Pass a long timeout and treat
        # NoReply as a probable success — we verify via ActiveInstances.
        ad_mgr.RegisterAdvertisement(_AncsAdvert.PATH, {}, timeout=10.0)
        log.info("BLE advert registered: %s", _AncsAdvert.PATH)
        return True
    except dbus.exceptions.DBusException as e:
        name = e.get_dbus_name()
        if name == "org.bluez.Error.AlreadyExists":
            log.info("BLE advert already registered")
            return True
        if name == "org.freedesktop.DBus.Error.NoReply":
            # Probable success — check ActiveInstances to confirm
            try:
                v = dbus.Interface(
                    system_bus.get_object("org.bluez", f"/org/bluez/{config.ADAPTER}"),
                    "org.freedesktop.DBus.Properties",
                ).Get("org.bluez.LEAdvertisingManager1", "ActiveInstances")
                if int(v) > 0:
                    log.info("BLE advert registered despite NoReply "
                             "(ActiveInstances=%d)", int(v))
                    return True
            except dbus.exceptions.DBusException:
                pass
        log.error("RegisterAdvertisement failed: %s: %s",
                  name, e.get_dbus_message())
        return False


def unregister_advert() -> None:
    """Best-effort unregister; safe to call on shutdown."""
    try:
        ad_mgr = bluez(f"/org/bluez/{config.ADAPTER}",
                       "org.bluez.LEAdvertisingManager1")
        ad_mgr.UnregisterAdvertisement(_AncsAdvert.PATH)
    except dbus.exceptions.DBusException as e:
        log.debug("UnregisterAdvertisement: %s", e.get_dbus_name())


# ---- one-shot startup ---------------------------------------------------

def prepare(*, allow_sudo: bool = True) -> bool:
    """Run all the prerequisites. Returns False if anything critical failed.

    Idempotent. Safe to call on every daemon start.
    """
    ok = True
    cod = current_cod()
    log.info("current adapter Class = 0x%06x", cod or 0)
    if not desired_cod_matches(cod):
        if not allow_sudo and os.geteuid() != 0:
            log.warning("CoD wrong but sudo disabled — skipping CoD set")
        else:
            ok &= set_cod()
    else:
        log.info("CoD already matches A/V Hands-Free, leaving as-is")

    ok &= register_advert()
    return ok
