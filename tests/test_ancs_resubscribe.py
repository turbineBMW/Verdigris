"""Regression: when the iPhone drops and re-exposes the ANCS characteristics
(the LE bearer bouncing), the client must not stack a second set of
PropertiesChanged receivers on the same paths — otherwise one notification
is delivered 2x2 = 4 times (two NS handlers each write CP, and each of the
two DS responses hits two DS handlers)."""
from __future__ import annotations

import struct

import pytest

from verdigris.ancs import client as client_mod
from verdigris.ancs.client import AncsClient
from verdigris.ancs.constants import (
    CONTROL_POINT_CHAR,
    DATA_SOURCE_CHAR,
    NOTIFICATION_SOURCE_CHAR,
    CommandID,
    EventID,
)

DEV = "/org/bluez/hci0/dev_AA_BB"
PROPS = "org.freedesktop.DBus.Properties"


def _attr(attr_id: int, s: str) -> bytes:
    b = s.encode()
    return struct.pack("<BH", attr_id, len(b)) + b


class _Match:
    def __init__(self, bus, key, cb):
        self.bus, self.key, self.cb = bus, key, cb

    def remove(self):
        self.bus.receivers[self.key].remove(self.cb)


class FakeBus:
    """Just enough of dbus.SystemBus for AncsClient."""

    def __init__(self):
        self.receivers: dict[str, list] = {}
        self.cp_writes: list[bytes] = []
        self.paths: dict[str, str] = {}  # path -> uuid
        self.client: AncsClient | None = None

    # -- bus API used by AncsClient
    def get_object(self, _name, path):
        return path

    def add_signal_receiver(self, cb, dbus_interface=None, signal_name=None, path=None):
        self.receivers.setdefault(path, []).append(cb)
        return _Match(self, path, cb)

    # -- test helpers
    def emit(self, path, value: bytes):
        for cb in list(self.receivers.get(path, [])):
            cb("org.bluez.GattCharacteristic1", {"Value": list(value)}, [])

    def add_chars(self, svc: str):
        for uuid, ch in ((CONTROL_POINT_CHAR, "char0001"),
                         (NOTIFICATION_SOURCE_CHAR, "char0002"),
                         (DATA_SOURCE_CHAR, "char0003")):
            path = f"{DEV}/{svc}/{ch}"
            self.paths[path] = uuid
            self.client._on_iface_added(
                path, {"org.bluez.GattCharacteristic1": {"UUID": uuid}})

    def remove_chars(self, svc: str):
        for ch in ("char0001", "char0002", "char0003"):
            path = f"{DEV}/{svc}/{ch}"
            self.paths.pop(path, None)
            self.client._on_iface_removed(path, ["org.bluez.GattCharacteristic1"])

    def ds_path(self, svc):
        return f"{DEV}/{svc}/char0003"

    def ns_path(self, svc):
        return f"{DEV}/{svc}/char0002"


class FakeIface:
    """Stands in for dbus.Interface(obj, iface). Answers CP writes on DS
    like a real iPhone would: one response per write."""

    def __init__(self, bus: FakeBus, path, _iface):
        self.bus, self.path = bus, path

    def StartNotify(self): pass
    def StopNotify(self): pass
    def GetManagedObjects(self): return {}
    def connect_to_signal(self, *_a, **_k): return _Match(self.bus, "om", lambda: None) if self.bus.receivers.setdefault("om", []) is not None else None

    def WriteValue(self, value, _opts):
        pkt = bytes(int(b) for b in value)
        self.bus.cp_writes.append(pkt)
        svc = self.path.split("/")[-2]
        ds = self.bus.ds_path(svc)
        if pkt[0] == CommandID.GetNotificationAttributes:
            uid = struct.unpack("<I", pkt[1:5])[0]
            resp = (bytes([CommandID.GetNotificationAttributes])
                    + struct.pack("<I", uid)
                    + _attr(0, "com.example.app") + _attr(1, "Hello")
                    + _attr(2, "") + _attr(3, "body"))
            self.bus.emit(ds, resp)
        elif pkt[0] == CommandID.GetAppAttributes:
            app_id = pkt[1:].split(b"\0")[0]
            resp = bytes([CommandID.GetAppAttributes]) + app_id + b"\0" + _attr(0, "Example")
            self.bus.emit(ds, resp)


@pytest.fixture
def bus(monkeypatch):
    fake = FakeBus()
    monkeypatch.setattr(client_mod, "system_bus", fake)
    monkeypatch.setattr(client_mod.dbus, "Interface",
                        lambda obj, iface: FakeIface(fake, obj, iface))
    return fake


def _notification_added(uid: int) -> bytes:
    return struct.pack("<BBBBI", EventID.NotificationAdded, 0, 4, 1, uid)


def test_one_notification_after_resubscribe_emits_once(bus):
    events = []
    c = AncsClient(DEV, on_event=events.append)
    bus.client = c
    c.start()

    bus.add_chars("service0019")
    bus.remove_chars("service0019")          # LE bearer dropped
    bus.add_chars("service0019")             # ... and came back, same handles

    bus.emit(bus.ns_path("service0019"), _notification_added(42))

    assert len(events) == 1, f"expected 1 event, got {len(events)}"
    n_attr = sum(1 for p in bus.cp_writes if p[0] == CommandID.GetNotificationAttributes)
    assert n_attr == 1, f"expected 1 GetNotificationAttributes write, got {n_attr}"
