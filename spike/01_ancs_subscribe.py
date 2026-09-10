#!/usr/bin/env python3
"""01_ancs_subscribe.py  —  Phase 0 step 01: prove ANCS works on iOS 26.5.

Strategy
--------
ANCS is GATT-over-BLE only. Our current bond is BR/EDR; the iPhone has not
opened a BLE link to us, so its ANCS service is not visible.

Fix (mirrors pzmarzly/ancs4linux): register a BLE advertisement that
*solicits* the ANCS service. iOS, seeing a known accessory advertise with
SolicitUUIDs = ANCS, opens a BLE GATT connection. After that, the ANCS
service tree appears under the device path and we can subscribe normally.

Run
---
    python3 01_ancs_subscribe.py 2>&1 | tee results/01_ancs_subscribe.log

Then on the iPhone:
    - Make sure it is UNLOCKED at least once after the script starts.
    - Send yourself an iMessage / email / Slack / anything.

Go signal: [NS] + [DS] entries below showing real app + title + message.
"""
from __future__ import annotations

import signal
import sys
import time

import dbus
import dbus.exceptions
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

# ---- config --------------------------------------------------------------

IPHONE_MAC = "AA:BB:CC:DD:EE:FF"
ADAPTER    = "hci0"
RUN_S      = 600

ANCS_SVC = "7905F431-B5CE-4E99-A40F-4B1E122D00D0"
NS_UUID  = "9fbf120d-6301-42d9-8c58-25e699a21dbd"   # Notification Source
DS_UUID  = "22eac6e9-24d6-4bb5-be44-b36ace7c7bfb"   # Data Source
CP_UUID  = "69d1d8f3-45e1-49a8-9821-9bbdfdaad9d9"   # Control Point

CATEGORIES = {0:"Other",1:"IncomingCall",2:"MissedCall",3:"Voicemail",
              4:"Social",5:"Schedule",6:"Email",7:"News",
              8:"HealthFitness",9:"BusinessFinance",10:"Location",
              11:"Entertainment"}
EVENTS = {0:"Added",1:"Modified",2:"Removed"}
ATTRS  = {0:"App",1:"Title",2:"Subtitle",3:"Message",4:"MessageSize",
          5:"Date",6:"PositiveAction",7:"NegativeAction"}

# ---- dbus setup ----------------------------------------------------------

dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
bus = dbus.SystemBus()
device_path = f"/org/bluez/{ADAPTER}/dev_{IPHONE_MAC.replace(':','_')}"


# ---- BLE advertisement (peripheral, solicits ANCS) -----------------------

class AncsAdvert(dbus.service.Object):
    PATH = "/verdigris/ancs_advert"

    @dbus.service.method("org.bluez.LEAdvertisement1",
                         in_signature="", out_signature="")
    def Release(self):
        return None

    @dbus.service.method("org.freedesktop.DBus.Properties",
                         in_signature="s", out_signature="a{sv}")
    def GetAll(self, iface):
        if iface != "org.bluez.LEAdvertisement1":
            raise dbus.exceptions.DBusException(
                f"Unknown interface {iface}",
                name="org.freedesktop.DBus.Error.InvalidArgs")
        return {
            "Type": dbus.String("peripheral"),
            "SolicitUUIDs": dbus.Array([ANCS_SVC], signature="s"),
            "LocalName": dbus.String("pop-os-ancs"),
            "Includes": dbus.Array(["tx-power"], signature="s"),
        }

    @dbus.service.method("org.freedesktop.DBus.Properties",
                         in_signature="ss", out_signature="v")
    def Get(self, iface, prop):
        return self.GetAll(iface)[prop]


advert = AncsAdvert(bus, AncsAdvert.PATH)
ad_mgr = dbus.Interface(
    bus.get_object("org.bluez", f"/org/bluez/{ADAPTER}"),
    "org.bluez.LEAdvertisingManager1",
)
print("[+] Registering BLE advertisement (SolicitUUIDs=ANCS) ...", flush=True)
try:
    ad_mgr.RegisterAdvertisement(AncsAdvert.PATH, {})
except dbus.exceptions.DBusException as e:
    print(f"[FAIL] RegisterAdvertisement: {e.get_dbus_name()}: "
          f"{e.get_dbus_message()}", flush=True)
    sys.exit(2)
print("[+] Advert registered.", flush=True)


# ---- wait for iPhone to open BLE link and expose ANCS chars --------------

om = dbus.Interface(bus.get_object("org.bluez", "/"),
                    "org.freedesktop.DBus.ObjectManager")

def find_ancs():
    """Return dict {NS_UUID: path|None, DS_UUID: path|None, CP_UUID: path|None}."""
    found = {NS_UUID: None, DS_UUID: None, CP_UUID: None}
    for p, ifaces in om.GetManagedObjects().items():
        if not p.startswith(device_path):
            continue
        c = ifaces.get("org.bluez.GattCharacteristic1")
        if not c:
            continue
        u = str(c.get("UUID", "")).lower()
        if u in found:
            found[u] = p
    return found

ns_path = ds_path = cp_path = None
deadline = time.time() + 60
last_reminder = 0
while time.time() < deadline:
    paths = find_ancs()
    ns_path, ds_path, cp_path = paths[NS_UUID], paths[DS_UUID], paths[CP_UUID]
    if ns_path and ds_path and cp_path:
        break
    elapsed = int(60 - (deadline - time.time()))
    if elapsed - last_reminder >= 10:
        last_reminder = elapsed
        print(f"    ({elapsed}s) waiting for iPhone BLE link. "
              "Unlock phone; tap Bluetooth screen to wake.", flush=True)
    time.sleep(1)

if not (ns_path and ds_path and cp_path):
    print("[FAIL] ANCS characteristics never appeared within 60s.", flush=True)
    print("       This means iPhone didn't open a BLE link in response to "
          "our SolicitUUIDs advert.", flush=True)
    print("       Found:", {k: v for k, v in find_ancs().items()}, flush=True)
    try: ad_mgr.UnregisterAdvertisement(AncsAdvert.PATH)
    except Exception: pass
    sys.exit(3)

print(f"\n[+] ANCS service found!")
print(f"    Notification Source: {ns_path}")
print(f"    Data Source:         {ds_path}")
print(f"    Control Point:       {cp_path}")


# ---- decoders ------------------------------------------------------------

def decode_ns(value: bytes):
    if len(value) < 8:
        return None
    return {
        "event":    EVENTS.get(value[0], f"?{value[0]}"),
        "flags":    value[1],
        "category": CATEGORIES.get(value[2], f"?{value[2]}"),
        "count":    value[3],
        "uid":      int.from_bytes(value[4:8], "little"),
    }

ds_buf: dict[int, bytearray] = {}

def parse_ds(value: bytes):
    """Parse possibly-fragmented Get Notification Attributes response."""
    if len(value) < 5:
        return None
    uid = int.from_bytes(value[1:5], "little")
    buf = ds_buf.setdefault(uid, bytearray())
    buf.extend(value[5:])
    attrs, pos = {}, 0
    while pos + 3 <= len(buf):
        aid = buf[pos]
        alen = int.from_bytes(buf[pos+1:pos+3], "little")
        if pos + 3 + alen > len(buf):
            return None  # incomplete, wait for more fragments
        try:
            val = buf[pos+3:pos+3+alen].decode("utf-8", "replace")
        except Exception:
            val = repr(bytes(buf[pos+3:pos+3+alen]))
        attrs[ATTRS.get(aid, f"?{aid}")] = val
        pos += 3 + alen
    if pos == len(buf):
        del ds_buf[uid]
    return {"uid": uid, "attrs": attrs}


# ---- CP write: Get Notification Attributes -------------------------------

cp_iface = dbus.Interface(bus.get_object("org.bluez", cp_path),
                          "org.bluez.GattCharacteristic1")

def request_attrs(uid: int):
    pkt = bytearray([0])                                  # CommandID=0
    pkt += uid.to_bytes(4, "little")
    pkt.append(0)                                          # App, no maxlen
    pkt.append(1); pkt += (64).to_bytes(2, "little")       # Title max 64
    pkt.append(2); pkt += (64).to_bytes(2, "little")       # Subtitle max 64
    pkt.append(3); pkt += (255).to_bytes(2, "little")      # Message max 255
    try:
        cp_iface.WriteValue([dbus.Byte(b) for b in pkt], {})
    except dbus.DBusException as e:
        print(f"    [WARN] CP write: {e.get_dbus_name()}: "
              f"{e.get_dbus_message()}", flush=True)


# ---- signal handlers -----------------------------------------------------

def on_ns(iface, changed, _inv, path=None):
    if "Value" not in changed:
        return
    val = bytes(changed["Value"])
    n = decode_ns(val)
    if not n:
        return
    print(f"\n[NS] {n['event']} category={n['category']} count={n['count']}"
          f" flags=0x{n['flags']:02x} uid={n['uid']} raw={val.hex()}",
          flush=True)
    if n["event"] in ("Added", "Modified"):
        request_attrs(n["uid"])

def on_ds(iface, changed, _inv, path=None):
    if "Value" not in changed:
        return
    val = bytes(changed["Value"])
    p = parse_ds(val)
    if p and p["attrs"]:
        a = p["attrs"]
        print(f"[DS] uid={p['uid']} app={a.get('App','?')!r}"
              f" title={a.get('Title','?')!r}"
              f" subtitle={a.get('Subtitle','?')!r}"
              f" message={a.get('Message','?')!r}", flush=True)

bus.add_signal_receiver(
    on_ns,
    dbus_interface="org.freedesktop.DBus.Properties",
    signal_name="PropertiesChanged",
    path=ns_path,
)
bus.add_signal_receiver(
    on_ds,
    dbus_interface="org.freedesktop.DBus.Properties",
    signal_name="PropertiesChanged",
    path=ds_path,
)


# ---- enable notifications + run loop -------------------------------------

ns_iface = dbus.Interface(bus.get_object("org.bluez", ns_path),
                          "org.bluez.GattCharacteristic1")
ds_iface = dbus.Interface(bus.get_object("org.bluez", ds_path),
                          "org.bluez.GattCharacteristic1")

print("\n[+] StartNotify on Notification Source ...", flush=True)
ns_iface.StartNotify()
print("[+] StartNotify on Data Source ...", flush=True)
ds_iface.StartNotify()

print(f"\n[+] Subscribed. Running up to {RUN_S}s."
      "\n[+] Trigger something now: send yourself an iMessage, email, or"
      "\n    Slack notification. Ctrl+C to stop early.\n", flush=True)

loop = GLib.MainLoop()

def stop():
    print("\n[+] Stopping ...", flush=True)
    for fn in (ns_iface.StopNotify, ds_iface.StopNotify,
               lambda: ad_mgr.UnregisterAdvertisement(AncsAdvert.PATH)):
        try: fn()
        except Exception: pass
    loop.quit()
    return False

GLib.timeout_add_seconds(RUN_S, stop)
signal.signal(signal.SIGINT, lambda *_: stop())

try:
    loop.run()
except KeyboardInterrupt:
    stop()

print("\n[VERDICT]"
      "\n  PASS if [NS] AND [DS] entries above show real notifications with"
      "\n        app + title + message."
      "\n  PARTIAL if [NS] arrived but [DS] never did (attribute fetch broken)."
      "\n  FAIL if neither — ANCS effectively gated on iOS 26.5."
      "\n  If FAIL, the whole project stops here.",
      flush=True)
