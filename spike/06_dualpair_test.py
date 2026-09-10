#!/usr/bin/env python3
"""06_dualpair_test.py  —  Can a single iPhone bond BR/EDR + BLE simultaneously?

Phase 2 research question. Phase 0 showed that a BR/EDR-paired iPhone
does NOT auto-BLE-attach to our BLE advert with SolicitUUIDs=ANCS. The
ancs4linux project pairs purely over BLE. If we can persuade iOS to
maintain BOTH pairings (same MAC, two link keys), we get ANCS + MAP/PBAP.

This script sets up the environment iOS expects for a fresh pairing
(adapter CoD = A/V Hands-Free, BLE peripheral advert with ANCS solicit,
discoverable + pairable), then watches what shows up under
/org/bluez/hci0/dev_<MAC>/ once the user pairs from the iPhone side.

Preconditions (run these in the shell BEFORE this script):
  1. systemctl --user stop verdigris
  2. bluetoothctl remove AA:BB:CC:DD:EE:FF
  3. On iPhone: Settings → Bluetooth → tap (i) next to pop-os → Forget
  4. sudo systemctl restart bluetooth.service     # clears orphan adverts

Then:
  python3 spike/06_dualpair_test.py 2>&1 | tee results/06_dualpair_test.log

After "Ready, pair from iPhone now":
  - On iPhone: Settings → Bluetooth → tap 'pop-os' under OTHER DEVICES
  - Accept pairing prompt on iPhone AND on Linux (GNOME notification)
"""
from __future__ import annotations

import signal
import subprocess
import sys
import time

import dbus
import dbus.exceptions
import dbus.service
from gi.repository import GLib

IPHONE_MAC = "AA:BB:CC:DD:EE:FF"
ADAPTER    = "hci0"
ANCS_UUID  = "7905F431-B5CE-4E99-A40F-4B1E122D00D0"
ANCS_UUID_LOWER = ANCS_UUID.lower()

import dbus.mainloop.glib
dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
bus = dbus.SystemBus()
device_path = f"/org/bluez/{ADAPTER}/dev_{IPHONE_MAC.replace(':','_')}"


def bluez(path, iface):
    return dbus.Interface(bus.get_object("org.bluez", path), iface)


# ---- Class-of-Device (via passwordless sudo rule installed earlier) ------

def set_cod() -> None:
    # Check current CoD first — btmgmt class deadlocks when LE adverts are
    # active (Phase 0 spike result #9). If we're already in A/V Hands-Free,
    # don't call btmgmt at all.
    try:
        cur = int(bluez(f"/org/bluez/{ADAPTER}",
                        "org.freedesktop.DBus.Properties").Get(
            "org.bluez.Adapter1", "Class"))
    except dbus.exceptions.DBusException:
        cur = 0
    major = (cur >> 8) & 0x1F
    minor = (cur >> 2) & 0x3F
    if major == 4 and (minor << 2) == 8:
        print(f"[+] CoD already A/V Hands-Free (0x{cur:06x}), skipping btmgmt",
              flush=True)
        return
    print(f"[+] Adapter CoD = 0x{cur:06x}; setting to A/V Hands-Free ...",
          flush=True)
    try:
        r = subprocess.run(
            ["sudo", "-n", "btmgmt", "class", "4", "8"],
            capture_output=True, text=True, timeout=8,
        )
        if r.returncode == 0:
            print(f"    OK: {r.stdout.strip()}", flush=True)
        else:
            print(f"    [WARN] sudo -n btmgmt failed (rc={r.returncode}): "
                  f"{r.stderr.strip() or r.stdout.strip()}", flush=True)
    except subprocess.TimeoutExpired:
        print("    [WARN] btmgmt deadlocked. There's an active LE advert "
              "somewhere. Run `sudo systemctl restart bluetooth.service` "
              "to clear it, then re-run this script.", flush=True)


# ---- BLE advert (peripheral, solicits ANCS) ------------------------------

class AncsAdvert(dbus.service.Object):
    PATH = "/dualpair_test/advert"

    @dbus.service.method("org.bluez.LEAdvertisement1")
    def Release(self):  # noqa: N802
        return None

    @dbus.service.method("org.freedesktop.DBus.Properties",
                         in_signature="s", out_signature="a{sv}")
    def GetAll(self, iface):  # noqa: N802
        if iface != "org.bluez.LEAdvertisement1":
            raise dbus.exceptions.DBusException(
                f"Unknown interface {iface}",
                name="org.freedesktop.DBus.Error.InvalidArgs")
        return {
            "Type": dbus.String("peripheral"),
            "SolicitUUIDs": dbus.Array([ANCS_UUID], signature="s"),
            "LocalName": dbus.String("pop-os-dualpair"),
            "Includes": dbus.Array(["tx-power"], signature="s"),
        }

    @dbus.service.method("org.freedesktop.DBus.Properties",
                         in_signature="ss", out_signature="v")
    def Get(self, iface, prop):  # noqa: N802
        return self.GetAll(iface)[prop]


# ---- Main flow -----------------------------------------------------------

set_cod()

# Verify adapter is in a clean state
ad_mgr = bluez(f"/org/bluez/{ADAPTER}", "org.bluez.LEAdvertisingManager1")
props = bluez(f"/org/bluez/{ADAPTER}", "org.freedesktop.DBus.Properties")
ai = int(props.Get("org.bluez.LEAdvertisingManager1", "ActiveInstances"))
print(f"[+] LE Advertising ActiveInstances before register: {ai}", flush=True)
if ai > 0:
    print("    [WARN] non-zero ActiveInstances — leftover adverts. "
          "Did you `sudo systemctl restart bluetooth.service` first?",
          flush=True)

advert = AncsAdvert(bus, AncsAdvert.PATH)
print("[+] Registering BLE peripheral advert (SolicitUUIDs=ANCS) ...", flush=True)
try:
    ad_mgr.RegisterAdvertisement(AncsAdvert.PATH, {}, timeout=10.0)
    print("    OK", flush=True)
except dbus.exceptions.DBusException as e:
    name = e.get_dbus_name()
    if name == "org.freedesktop.DBus.Error.NoReply":
        # BlueZ quirk — usually registers anyway
        try:
            new_ai = int(props.Get("org.bluez.LEAdvertisingManager1",
                                    "ActiveInstances"))
            if new_ai > ai:
                print(f"    OK (despite NoReply; "
                      f"ActiveInstances={new_ai})", flush=True)
            else:
                raise RuntimeError(f"register did not take effect: {e}")
        except dbus.exceptions.DBusException:
            print(f"    FAILED: {name}: {e.get_dbus_message()}", flush=True)
            sys.exit(2)
    else:
        print(f"    FAILED: {name}: {e.get_dbus_message()}", flush=True)
        sys.exit(2)

# Discoverable + pairable
print("[+] Setting adapter pairable + discoverable ...", flush=True)
adapter_props = bluez(f"/org/bluez/{ADAPTER}",
                      "org.freedesktop.DBus.Properties")
for prop, val in [("Pairable", dbus.Boolean(True)),
                  ("Discoverable", dbus.Boolean(True))]:
    try:
        adapter_props.Set("org.bluez.Adapter1", prop, val)
    except dbus.exceptions.DBusException as e:
        print(f"    [WARN] Set({prop}): {e.get_dbus_name()}", flush=True)

print("""
=================================================================
READY — now pair from the iPhone side:

  1. iPhone Settings → Bluetooth
  2. Wait for 'pop-os' or 'pop-os-dualpair' to appear under OTHER DEVICES
  3. Tap it. Accept the 6-digit pairing code on both sides.
  4. After pairing succeeds, COME BACK HERE and watch the output.

This script will print the device's UUIDs every 5s. Look for:
  • 7905f431-...  ← ANCS service (BLE)         ⇐ this is the prize
  • 00001132-...  ← Message Access Server (BR/EDR MAP)
  • 0000112f-...  ← Phonebook Access Server (BR/EDR PBAP)

If ALL THREE show up, dual-pair works and Phase 2a is unblocked.
If only BR/EDR ones — iOS combined the pair into one mode (BR/EDR).
If only ANCS — iOS chose BLE-only, MAP/PBAP gone.

Ctrl+C to stop.
=================================================================
""", flush=True)


# ---- Watch for pair + GATT discovery --------------------------------------

def report():
    """Print device state. Called every 5 seconds via GLib timeout."""
    try:
        info = bluez(device_path, "org.freedesktop.DBus.Properties")
        paired = bool(info.Get("org.bluez.Device1", "Paired"))
        connected = bool(info.Get("org.bluez.Device1", "Connected"))
        services_resolved = bool(info.Get("org.bluez.Device1",
                                          "ServicesResolved"))
        uuids = list(info.Get("org.bluez.Device1", "UUIDs"))
    except dbus.exceptions.DBusException:
        print("[poll] device not yet known to BlueZ — waiting for pair",
              flush=True)
        return True

    has_ancs = any(str(u).lower() == ANCS_UUID_LOWER for u in uuids)
    has_map = any(str(u).lower().startswith("00001132") for u in uuids)
    has_pbap = any(str(u).lower().startswith("0000112f") for u in uuids)

    marker = lambda b: "✓" if b else "✗"
    print(f"[poll] Paired={paired}  Connected={connected}  "
          f"ServicesResolved={services_resolved}  "
          f"ANCS={marker(has_ancs)}  MAP={marker(has_map)}  "
          f"PBAP={marker(has_pbap)}  ({len(uuids)} UUIDs total)",
          flush=True)

    if has_ancs and (has_map or has_pbap):
        print("\n🎉 DUAL-PAIR WORKS — ANCS + BR/EDR profiles all visible.",
              flush=True)
    elif has_ancs:
        print("\n🟡 ANCS only — BR/EDR profiles missing. iOS may have "
              "chosen BLE-only pair.", flush=True)
    elif has_map or has_pbap:
        # Normal case so far
        pass
    return True

GLib.timeout_add_seconds(5, report)

# ---- Run loop ------------------------------------------------------------

loop = GLib.MainLoop()
def stop(*_):
    print("\n[+] Stopping. Unregistering advert ...", flush=True)
    try: ad_mgr.UnregisterAdvertisement(AncsAdvert.PATH)
    except Exception: pass
    loop.quit()
signal.signal(signal.SIGINT, stop)

try:
    loop.run()
except KeyboardInterrupt:
    stop()

print("\n[+] Done. Re-start verdigris with: systemctl --user start verdigris",
      flush=True)
