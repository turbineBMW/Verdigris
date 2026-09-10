"""First-run wizard — enumerate paired devices, write local.env, prompt
the user through the iPhone-side toggles.

Doesn't drive the pairing itself — GNOME's BT panel (or `bluetoothctl`)
does that more reliably. We just connect the dots after pairing.
"""
from __future__ import annotations

import os
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import dbus
import dbus.exceptions
import typer

from verdigris.bus import system_bus

LOCAL_ENV_PATH = Path(
    os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
) / "verdigris" / "local.env"


@dataclass(slots=True)
class PairedDevice:
    mac: str
    name: str
    icon: str           # e.g. "phone", "audio-headset"
    trusted: bool
    connected: bool
    paired: bool
    adapter_path: str   # /org/bluez/hci0 ...

    @property
    def likely_iphone(self) -> bool:
        return (
            "phone" in (self.icon or "").lower()
            or "iphone" in (self.name or "").lower()
            or "ipad" in (self.name or "").lower()
        )


def list_paired_devices() -> list[PairedDevice]:
    om = dbus.Interface(
        system_bus.get_object("org.bluez", "/"),
        "org.freedesktop.DBus.ObjectManager",
    )
    out: list[PairedDevice] = []
    for path, ifaces in om.GetManagedObjects().items():
        d = ifaces.get("org.bluez.Device1")
        if d is None:
            continue
        if not bool(d.get("Paired", False)):
            continue
        out.append(PairedDevice(
            mac=str(d.get("Address", "")),
            name=str(d.get("Name", "(unnamed)")),
            icon=str(d.get("Icon", "")),
            trusted=bool(d.get("Trusted", False)),
            connected=bool(d.get("Connected", False)),
            paired=True,
            adapter_path=path.rsplit("/", 1)[0],
        ))
    return out


def trust_device(mac: str, adapter_path: str) -> None:
    dev_path = f"{adapter_path}/dev_{mac.replace(':', '_')}"
    props = dbus.Interface(
        system_bus.get_object("org.bluez", dev_path),
        "org.freedesktop.DBus.Properties",
    )
    props.Set("org.bluez.Device1", "Trusted", dbus.Boolean(True))


def write_local_env(mac: str) -> Path:
    LOCAL_ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOCAL_ENV_PATH.write_text(f"VERDIGRIS_MAC={mac}\n")
    os.chmod(LOCAL_ENV_PATH, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
    return LOCAL_ENV_PATH


# ---- wizard --------------------------------------------------------------

def run_wizard(*, restart_after: bool = True) -> int:
    """Returns process exit code."""
    typer.echo(typer.style("\n=== Verdigris first-run setup ===\n",
                           fg=typer.colors.CYAN, bold=True))

    devices = list_paired_devices()
    if not devices:
        typer.echo(typer.style("No paired Bluetooth devices found.\n",
                               fg=typer.colors.YELLOW))
        typer.echo("Pair your iPhone first. Two options:")
        typer.echo("  • GNOME Settings → Bluetooth → tap your iPhone under Other Devices")
        typer.echo("  • CLI: bluetoothctl  →  scan on, pair <MAC>, trust <MAC>, exit")
        typer.echo("\nThen run `verdigris pair-setup` again.")
        return 1

    # Prefer obvious iPhones; fall back to all paired devices if none matched.
    candidates = [d for d in devices if d.likely_iphone] or devices
    typer.echo(f"Found {len(candidates)} paired "
               f"{'iPhone-like device' if len(candidates) == 1 else 'iPhone-like devices'}:\n")
    for i, d in enumerate(candidates, 1):
        marker_t = typer.style("trusted" if d.trusted else "untrusted",
                               fg=typer.colors.GREEN if d.trusted else typer.colors.YELLOW)
        marker_c = typer.style("connected" if d.connected else "disconnected",
                               fg=typer.colors.GREEN if d.connected else typer.colors.YELLOW,
                               dim=not d.connected)
        typer.echo(f"  [{i}] {d.name}  ({d.mac})  "
                   f"icon={d.icon}  {marker_t}  {marker_c}")
    typer.echo("")

    if len(candidates) == 1:
        chosen = candidates[0]
        if not typer.confirm("Use this device?", default=True):
            return 0
    else:
        idx_s = typer.prompt("Pick a device by number", default="1")
        try:
            chosen = candidates[int(idx_s) - 1]
        except (ValueError, IndexError):
            typer.echo(typer.style("Invalid choice.", fg=typer.colors.RED))
            return 1

    # Trust the device if not already
    if not chosen.trusted:
        try:
            trust_device(chosen.mac, chosen.adapter_path)
            typer.echo(typer.style(
                f"✓ Trusted {chosen.mac}", fg=typer.colors.GREEN))
        except dbus.exceptions.DBusException as e:
            typer.echo(typer.style(
                f"⚠ Could not auto-trust ({e.get_dbus_name()}). "
                "Run `bluetoothctl trust {chosen.mac}` manually.",
                fg=typer.colors.YELLOW))

    # Write local.env
    p = write_local_env(chosen.mac)
    typer.echo(typer.style(
        f"✓ Wrote VERDIGRIS_MAC={chosen.mac} to {p}",
        fg=typer.colors.GREEN))

    # Restart before presenting the iPhone steps: startup sets the adapter CoD
    # and registers the BLE advert that cause iOS to reveal those toggles.
    if restart_after:
        if typer.confirm("\nRestart the Verdigris daemon now to advertise the profiles?",
                         default=True):
            r = subprocess.run(
                ["systemctl", "--user", "restart", "verdigris"],
                capture_output=True, text=True,
            )
            # A Type=simple unit can return successfully from `restart` just
            # before execve fails. Give systemd a moment, then verify that the
            # service really stayed active instead of reporting a false ✓.
            if r.returncode == 0:
                time.sleep(0.5)
                state = subprocess.run(
                    ["systemctl", "--user", "is-active", "verdigris"],
                    capture_output=True, text=True,
                ).stdout.strip()
            else:
                state = ""
            if r.returncode == 0 and state == "active":
                typer.echo(typer.style(
                    "✓ Daemon restarted. Tail logs: "
                    "journalctl --user -u verdigris -f",
                    fg=typer.colors.GREEN))
            else:
                typer.echo(typer.style(
                    f"⚠ Daemon failed to stay active"
                    f"{f' ({state})' if state else ''}: "
                    f"{r.stderr.strip() or r.stdout.strip() or 'check the journal'}",
                    fg=typer.colors.YELLOW))
                typer.echo("Inspect: systemctl --user status verdigris")
                typer.echo("         journalctl --user -u verdigris -n 50")

    # iPhone-side instructions
    typer.echo(typer.style("\n=== On the iPhone ===\n",
                           fg=typer.colors.CYAN, bold=True))
    typer.echo("  1. Open Settings → Bluetooth")
    typer.echo("  2. Tap the (i) next to your computer's name in My Devices")
    typer.echo(typer.style("  3. Enable: Show Message Notifications",
                           fg=typer.colors.WHITE, bold=True))
    typer.echo(typer.style("  4. Enable: Sync Contacts",
                           fg=typer.colors.WHITE, bold=True))
    typer.echo("")
    typer.echo("If the toggles aren't visible yet, the adapter's CoD likely")
    typer.echo("isn't set to A/V Hands-Free yet. Make sure you ran:")
    typer.echo(typer.style("  sudo bash systemd/install-cod-sudoers.sh",
                           fg=typer.colors.WHITE))
    typer.echo("and that you've started the daemon at least once after.")

    typer.echo("\nNext step: trigger an SMS/iMessage to your phone, then:")
    typer.echo("  verdigris sms-list")
    return 0
