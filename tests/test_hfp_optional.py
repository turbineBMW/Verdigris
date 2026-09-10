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
    manager = ofono_client.HfpManager(on_event=lambda _event: None)

    manager.start()

    assert manager._mgr_matches == []
    assert manager._modem_path is None
