"""Regression tests for first-run setup portability."""
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from verdigris import cli, pair_setup

ROOT = Path(__file__).resolve().parents[1]


def test_find_obexd_uses_path(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/opt/bin/obexd")
    assert cli._find_obexd() == "/opt/bin/obexd"


def test_find_obexd_accepts_arch_layout(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        cli.os.path, "isfile", lambda path: path == "/usr/lib/bluetooth/obexd"
    )
    monkeypatch.setattr(cli.os, "access", lambda path, mode: True)
    assert cli._find_obexd() == "/usr/lib/bluetooth/obexd"


def test_managed_has_all_ancs_characteristics_for_target_device():
    from verdigris.ancs.constants import ANCS_CHAR_UUIDS

    target = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
    managed = {
        f"{target}/service0001/char{index:04d}": {
            "org.bluez.GattCharacteristic1": {"UUID": uuid.upper()}
        }
        for index, uuid in enumerate(ANCS_CHAR_UUIDS)
    }
    # An unrelated device must not influence the result.
    managed["/org/bluez/hci0/dev_00_11_22_33_44_55/service1/char1"] = {
        "org.bluez.GattCharacteristic1": {"UUID": next(iter(ANCS_CHAR_UUIDS))}
    }

    assert cli._managed_has_ancs(managed, target)


def test_managed_requires_every_ancs_characteristic():
    from verdigris.ancs.constants import ANCS_CHAR_UUIDS

    target = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
    one_uuid = next(iter(ANCS_CHAR_UUIDS))
    managed = {
        f"{target}/service0001/char0001": {
            "org.bluez.GattCharacteristic1": {"UUID": one_uuid}
        }
    }

    assert not cli._managed_has_ancs(managed, target)


def test_managed_live_ancs_rejects_cached_chars_after_le_disconnect():
    from verdigris.ancs.constants import ANCS_CHAR_UUIDS

    target = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
    managed = {
        target: {
            "org.bluez.Device1": {"Connected": True},
            "org.bluez.Bearer.BREDR1": {"Connected": True},
            "org.bluez.Bearer.LE1": {"Connected": False},
        },
        **{
            f"{target}/service0001/char{index:04d}": {
                "org.bluez.GattCharacteristic1": {"UUID": uuid}
            }
            for index, uuid in enumerate(ANCS_CHAR_UUIDS)
        },
    }

    assert cli._managed_has_ancs(managed, target)
    assert not cli._managed_has_live_ancs(managed, target)
    managed[target]["org.bluez.Bearer.LE1"]["Connected"] = True
    assert cli._managed_has_live_ancs(managed, target)


def test_ancs_enable_tolerates_reconnect_false_negative(monkeypatch):
    import subprocess
    import time

    import dbus
    import dbus.mainloop.glib

    bus = SimpleNamespace(get_object=lambda *_args: object())
    props = SimpleNamespace(Get=lambda *_args: "AA:BB:CC:DD:EE:FF")
    monkeypatch.setattr(dbus, "SystemBus", lambda: bus)
    monkeypatch.setattr(dbus, "Interface", lambda *_args: props)
    monkeypatch.setattr(dbus.mainloop.glib, "DBusGMainLoop", lambda **_kwargs: None)
    live_states = iter([False, True])
    monkeypatch.setattr(
        cli, "_ancs_gatt_available", lambda: next(live_states)
    )
    monkeypatch.setattr(cli, "_set_preferred_bearer", lambda *_args: False)
    monkeypatch.setattr(cli, "_connect_le_bearer", lambda *_args: False)
    monkeypatch.setattr(
        cli.bluez_setup, "enable_le_address_resolution", lambda: True
    )
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    def fake_run(command, **_kwargs):
        if command[0] == "sudo":
            return SimpleNamespace(returncode=0, stdout="bearer set\n", stderr="")
        if command[1] == "connect":
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="Failed to connect: le-connection-abort-by-local\n",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = CliRunner().invoke(cli.app, ["ancs-enable"])

    assert result.exit_code == 0
    assert "LE connect did not complete; still waiting" in result.stdout
    assert "ANCS is live" in result.stdout


def test_set_preferred_bearer_uses_live_bluez_property(monkeypatch):
    import dbus

    calls = []
    props = SimpleNamespace(Set=lambda *args: calls.append(args))
    bus = SimpleNamespace(get_object=lambda *_args: object())
    monkeypatch.setattr(dbus, "Interface", lambda *_args: props)

    assert cli._set_preferred_bearer(bus, "/org/bluez/hci0/dev_AA_BB")
    assert calls[0][0:2] == ("org.bluez.Device1", "PreferredBearer")
    assert str(calls[0][2]) == "le"


def test_user_service_uses_stable_cli_symlink():
    unit = (ROOT / "systemd" / "verdigris.service").read_text()
    assert "ExecStart=%h/.local/bin/verdigris run" in unit
    assert "%h/blue/.venv" not in unit


def test_cod_sudoers_is_installer_template():
    template = (ROOT / "systemd" / "sudoers-verdigris-cod").read_text()
    assert "@VERDIGRIS_USER@" in template
    assert "@BTMGMT_PATH@ class 4 8" in template
    assert "bash ALL=" not in template


def test_ancs_helper_writes_bearer_keys_inside_general_section():
    helper = (ROOT / "systemd" / "set-le-bearer.sh").read_text()
    assert 'in_general = ($0 == "[General]")' in helper
    assert 'print "PreferredBearer=le"' in helper
    assert 'echo "LastUsedBearer=le" >>' not in helper


def test_ancs_installer_enables_userspace_experimental_api():
    installer = (ROOT / "systemd" / "install-ancs-sudoers.sh").read_text()
    assert "Experimental = true" in installer
    assert "KernelExperimental" not in installer


def test_ancs_installer_adds_address_resolution_helper():
    installer = (ROOT / "systemd" / "install-ancs-sudoers.sh").read_text()
    helper = (ROOT / "systemd" / "set-le-bearer.sh").read_text()

    assert "verdigris-set-le-bearer" in installer
    assert "--address-resolution" in helper
    assert '[[ "$HCI" =~ ^hci([0-9]+)$ ]]' in helper
    assert 'set-flags \\' in helper
    assert '-t "$MGMT_ADDRESS_TYPE" -f 4 "$DEVICE"' in helper


def test_pair_setup_restarts_before_phone_instructions(monkeypatch, capsys, tmp_path):
    device = pair_setup.PairedDevice(
        mac="AA:BB:CC:DD:EE:FF",
        name="Test iPhone",
        icon="phone",
        trusted=True,
        connected=True,
        paired=True,
        adapter_path="/org/bluez/hci0",
    )
    confirmations = iter([True, True])

    monkeypatch.setattr(pair_setup, "list_paired_devices", lambda: [device])
    monkeypatch.setattr(pair_setup, "write_local_env", lambda _mac: tmp_path / "local.env")
    monkeypatch.setattr(pair_setup.typer, "confirm", lambda *args, **kwargs: next(confirmations))
    monkeypatch.setattr(pair_setup.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        pair_setup.subprocess,
        "run",
        lambda cmd, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="active\n" if "is-active" in cmd else "",
            stderr="",
        ),
    )

    assert pair_setup.run_wizard() == 0
    output = capsys.readouterr().out
    assert output.index("Daemon restarted") < output.index("=== On the iPhone ===")


def test_legacy_config_loads_without_overriding_verdigris_settings(tmp_path, monkeypatch):
    from verdigris import config

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("VERDIGRIS_MAC", raising=False)
    legacy = tmp_path / "iphonebridge/local.env"
    legacy.parent.mkdir()
    legacy.write_text("IPHONEBRIDGE_MAC=AA:BB:CC:DD:EE:01\nVERDIGRIS_MAC=AA:BB:CC:DD:EE:02\n")
    config._load_local_env()
    assert config.os.environ["VERDIGRIS_MAC"] == "AA:BB:CC:DD:EE:02"
    monkeypatch.setenv("VERDIGRIS_MAC", "AA:BB:CC:DD:EE:03")
    config._load_local_env()
    assert config.os.environ["VERDIGRIS_MAC"] == "AA:BB:CC:DD:EE:03"
