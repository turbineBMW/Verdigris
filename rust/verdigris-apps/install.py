#!/usr/bin/env python3
"""Install the native preview beside Verdigris. Does not alter Bluetooth or old apps."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from migration import migrate_native

parser = argparse.ArgumentParser()
parser.add_argument("--release", action="store_true")
parser.add_argument("--copy", action="store_true", help="Install standalone binaries instead of symlinks to this checkout")
args = parser.parse_args()
root = Path(__file__).resolve().parent
profile = "release" if args.release else "debug"
command = ["cargo", "build", "--manifest-path", str(root / "Cargo.toml"), "--bins", "--locked"]
if args.release:
    command.append("--release")
subprocess.run(command, check=True)
bin_dir = Path.home() / ".local/bin"
data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
migrate_native(config, data)
bin_dir.mkdir(parents=True, exist_ok=True)
(data / "applications").mkdir(parents=True, exist_ok=True)
(data / "dbus-1/services").mkdir(parents=True, exist_ok=True)
(config / "systemd/user").mkdir(parents=True, exist_ok=True)
for binary in ("verdigris-messages", "verdigris-phone", "verdigris-reminders", "verdigris-notes", "verdigris-settings", "verdigris-sync"):
    destination = bin_dir / binary
    target = root / "target" / profile / binary
    if not args.copy and destination.exists() and not destination.is_symlink():
        raise SystemExit(f"Refusing to overwrite existing executable: {destination}")
    # Stage beside the destination so replacement is atomic, including when upgrading
    # an existing symlink or a running executable. Never write through the symlink.
    with tempfile.TemporaryDirectory(prefix=f".{binary}-", dir=bin_dir) as staging:
        temporary = Path(staging) / binary
        if args.copy:
            shutil.copyfile(target, temporary)
            temporary.chmod(0o755)
        else:
            temporary.symlink_to(target)
        temporary.replace(destination)

# Unthemed icons keep the supplied PNGs intact; GTK and launchers scale them to fit.
icons = data / "icons"
icons.mkdir(parents=True, exist_ok=True)
for kind, category in (("Messages", "Chat"), ("Phone", "Telephony"), ("Reminders", "Calendar"), ("Notes", "TextTools")):
    icon = f"dev.turbinebmw.Verdigris.{kind}"
    extension = "svg" if kind in ("Reminders", "Notes") else "png"
    shutil.copyfile(root / "data/icons" / f"{icon}.{extension}", icons / f"{icon}.{extension}")
    (icons / f"{icon}.{extension}").chmod(0o644)
    categories = f'{"Office" if kind in ("Reminders", "Notes") else "Network"};{category};'
    executable = str(bin_dir / f"verdigris-{kind.lower()}")
    # Desktop Exec quoting is distinct from shell quoting.
    executable = executable.replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$')
    (data / "applications" / f"dev.turbinebmw.Verdigris.{kind}.desktop").write_text(
        f'[Desktop Entry]\nType=Application\nName={kind}\nComment=Verdigris native {kind.lower()}\n'
        f'Exec="{executable}"\nIcon={icon}\nTerminal=false\nCategories={categories}\n'
        f'StartupNotify=true\nStartupWMClass=dev.turbinebmw.Verdigris.{kind}\n'
    )
# Remove the native Blue launchers superseded by this fork. Keep the Bluetooth
# backend installed until the user upgrades that service separately.
for kind in ("Messages", "Phone", "Settings"):
    (data / "applications" / f"dev.turbinebmw.Blue.{kind}.desktop").unlink(missing_ok=True)
    (data / "icons" / f"dev.turbinebmw.Blue.{kind}.png").unlink(missing_ok=True)
for binary in ("blue-messages", "blue-phone", "blue-settings", "blue-sync"):
    (bin_dir / binary).unlink(missing_ok=True)
(data / "dbus-1/services/dev.turbinebmw.Blue.Sync.service").unlink(missing_ok=True)
old_sync = config / "systemd/user/blue-native-sync.service"
if old_sync.exists():
    # Prevent two native workers from reconciling the same account after upgrade.
    subprocess.run(["systemctl", "--user", "disable", "--now", "blue-native-sync.service"], check=True)
    old_sync.unlink()
# Settings is launched by Messages and Phone, never as an app-grid entry.
# Also clean up the launcher left by earlier installations.
(data / "applications/dev.turbinebmw.Verdigris.Settings.desktop").unlink(missing_ok=True)
(data / "dbus-1/services/dev.turbinebmw.Verdigris.Sync.service").write_text(
    f"[D-BUS Service]\nName=dev.turbinebmw.Verdigris.Sync\nExec={bin_dir / 'verdigris-sync'}\nSystemdService=verdigris-sync.service\n"
)
(config / "systemd/user/verdigris-sync.service").write_text(
    '[Unit]\nDescription=Verdigris native message synchronization\nAfter=network.target\n\n'
    '[Service]\nType=dbus\nBusName=dev.turbinebmw.Verdigris.Sync\n'
    f'ExecStart="{bin_dir / "verdigris-sync"}"\nRestart=on-failure\nRestartSec=5\nUMask=0077\n\n'
    '[Install]\nWantedBy=default.target\n'
)
subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
if shutil.which("update-desktop-database"):
    subprocess.run(["update-desktop-database", str(data / "applications")], check=False)
print("Installed Messages, Phone, Reminders, and Notes. Open Settings from any app to connect your Mac.")
print("After connection, enable background startup with: systemctl --user enable --now verdigris-sync")
