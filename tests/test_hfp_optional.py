"""The optional HFP integration must not gate daemon startup."""

import dbus.exceptions

from verdigris.hfp import ofono_client


def test_missing_ofono_leaves_hfp_dormant(monkeypatch):
    def missing_service(*_args, **_kwargs):
        raise dbus.exceptions.DBusException(
            "The name is not activatable",
            name="org.freedesktop.DBus.Error.ServiceUnknown",
        )

    monkeypatch.setattr(ofono_client.system_bus, "get_object", missing_service)
    monkeypatch.setattr(ofono_client.session_bus, "get_object", missing_service)
    manager = ofono_client.HfpManager(on_event=lambda _event: None)

    manager.start()

    assert manager._mgr_matches == []
    assert manager._modem_path is None



class _Match:
    def remove(self):
        pass


class _NativeTelephony:
    def __init__(self):
        self.signals = {}

    def GetModems(self):
        return {"/org/pipewire/Telephony/ag1": {}}

    def GetCalls(self):
        return {}

    def connect_to_signal(self, name, callback):
        self.signals[name] = callback
        return _Match()


def test_native_pipewire_telephony_is_preferred(monkeypatch, tmp_path):
    native = _NativeTelephony()
    monkeypatch.setattr(
        ofono_client.session_bus, "get_object", lambda *_args: native)
    monkeypatch.setattr(
        ofono_client.dbus, "Interface", lambda obj, _interface: obj)

    manager = ofono_client.HfpManager(on_event=lambda _event: None)
    manager.start()

    assert manager._native_pipewire is True
    assert manager._service == "org.pipewire.Telephony"
    assert manager._modem_path == "/org/pipewire/Telephony/ag1"
    assert manager._vcm_hooked is True
    assert "ModemAdded" in native.signals
    assert "CallAdded" in native.signals


def test_native_pipewire_dial_uses_one_argument(monkeypatch, tmp_path):
    class NativeWithDial(_NativeTelephony):
        dialed = None

        def Dial(self, number):
            self.dialed = number

    native = NativeWithDial()
    monkeypatch.setattr(
        ofono_client.session_bus, "get_object", lambda *_args: native)
    monkeypatch.setattr(
        ofono_client.dbus, "Interface", lambda obj, _interface: obj)

    manager = ofono_client.HfpManager(on_event=lambda _event: None)
    manager.start()

    assert manager.dial("+15551234567") == ""
    assert native.dialed == "+15551234567"


def test_native_pipewire_availability(monkeypatch):
    native = _NativeTelephony()
    monkeypatch.setattr(
        ofono_client.session_bus, "get_object", lambda *_args: native)
    monkeypatch.setattr(
        ofono_client.dbus, "Interface", lambda obj, _interface: obj)

    assert ofono_client.native_pipewire_available() is True
