"""Tests for Bluetooth adapter preparation."""

from types import SimpleNamespace

from verdigris import bluez_setup


def test_set_cod_retries_transient_controller_busy(monkeypatch):
    results = iter(
        [
            SimpleNamespace(returncode=1, stdout="", stderr="status 0x0a (Busy)"),
            SimpleNamespace(returncode=0, stdout="Set Dev Class succeeded", stderr=""),
        ]
    )
    calls = []
    sleeps = []

    monkeypatch.setattr(bluez_setup.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        bluez_setup.subprocess,
        "run",
        lambda cmd, **kwargs: calls.append((cmd, kwargs)) or next(results),
    )
    monkeypatch.setattr(bluez_setup.time, "sleep", sleeps.append)

    assert bluez_setup.set_cod()
    assert len(calls) == 2
    assert sleeps == [0.5]


def test_set_cod_does_not_retry_permission_error(monkeypatch):
    calls = []
    result = SimpleNamespace(returncode=1, stdout="", stderr="permission denied")

    monkeypatch.setattr(bluez_setup.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        bluez_setup.subprocess,
        "run",
        lambda cmd, **kwargs: calls.append((cmd, kwargs)) or result,
    )

    assert not bluez_setup.set_cod()
    assert len(calls) == 1


def test_enable_le_address_resolution_uses_narrow_helper(monkeypatch):
    calls = []
    props = SimpleNamespace(
        Get=lambda iface, prop: {
            ("org.bluez.Bearer.LE1", "Bonded"): True,
            ("org.bluez.Device1", "AddressType"): "public",
        }[(iface, prop)]
    )
    monkeypatch.setattr(
        bluez_setup.system_bus, "get_object", lambda *_args: object()
    )
    monkeypatch.setattr(bluez_setup.dbus, "Interface", lambda *_args: props)
    monkeypatch.setattr(bluez_setup.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(
        bluez_setup.subprocess,
        "run",
        lambda cmd, **kwargs: calls.append((cmd, kwargs))
        or SimpleNamespace(returncode=0, stdout="flag set", stderr=""),
    )

    assert bluez_setup.enable_le_address_resolution()
    assert calls[0][0] == [
        "sudo", "-n", bluez_setup.LE_RESOLUTION_HELPER,
        "--address-resolution", "hci0", "public",
        bluez_setup.config.IPHONE_MAC,
    ]


def test_connect_le_bearer_async_preserves_classic_link(monkeypatch):
    calls = []
    bearer = SimpleNamespace(
        Connect=lambda **kwargs: calls.append(kwargs)
    )
    monkeypatch.setattr(bluez_setup, "le_bearer_connected", lambda: False)
    monkeypatch.setattr(
        bluez_setup.system_bus, "get_object", lambda *_args: object()
    )
    monkeypatch.setattr(bluez_setup.dbus, "Interface", lambda *_args: bearer)

    assert bluez_setup.connect_le_bearer_async()
    assert len(calls) == 1
    assert calls[0]["timeout"] == 20.0
    assert callable(calls[0]["reply_handler"])
    assert callable(calls[0]["error_handler"])
