"""Verdigris install/upgrade behavior, with fixture data and no real services."""
from __future__ import annotations

import importlib.util
import runpy
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

NATIVE = Path(__file__).resolve().parents[1] / "rust/verdigris-apps"
_spec = importlib.util.spec_from_file_location("verdigris_migration", NATIVE / "migration.py")
assert _spec is not None and _spec.loader is not None
migration = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migration)


def test_native_upgrade_snapshots_wal_and_preserves_existing_files(tmp_path):
    config, data = tmp_path / "config", tmp_path / "data"
    old_config = config / "blue-native"
    old_config.mkdir(parents=True)
    (old_config / "connection.json").write_text('{"server":"http://localhost/"}')
    old_data = data / "blue-native"
    old_data.mkdir(parents=True)
    db = sqlite3.connect(old_data / "fixture.sqlite")
    db.executescript("PRAGMA journal_mode=WAL; CREATE TABLE messages(text); INSERT INTO messages VALUES('cached');")
    (old_data / "image.png").write_bytes(b"fixture-image")
    (old_data / "interrupted.part").write_bytes(b"partial")
    (old_data / "symlink").symlink_to(tmp_path)
    try:
        migration.migrate_native(config, data)
        with sqlite3.connect(data / "verdigris/fixture.sqlite") as copied:
            assert copied.execute("SELECT text FROM messages").fetchone() == ("cached",)
        assert db.execute("SELECT text FROM messages").fetchone() == ("cached",)
        assert not (data / "verdigris/interrupted.part").exists()
        assert not (data / "verdigris/symlink").exists()
        assert (data / "verdigris/image.png").read_bytes() == b"fixture-image"
        connection = config / "verdigris/connection.json"
        assert connection.stat().st_mode & 0o777 == 0o600
        connection.write_text('new settings')
        migration.migrate_native(config, data)
        assert connection.read_text() == 'new settings'
        assert (old_config / 'connection.json').read_text() != 'new settings'
    finally:
        db.close()


def test_installer_renames_launchers_and_keeps_settings_internal(tmp_path, monkeypatch):
    root = tmp_path / "checkout with spaces"
    root.mkdir()
    for name in ("install.py", "migration.py"):
        shutil.copyfile(NATIVE / name, root / name)
    shutil.copytree(NATIVE / "data", root / "data")
    home, data, config = tmp_path / "home", tmp_path / "data", tmp_path / "config"
    apps = data / "applications"
    apps.mkdir(parents=True)
    for kind in ("Phone", "Messages", "Settings"):
        (apps / f"dev.turbinebmw.Blue.{kind}.desktop").write_text("old launcher")
    old_unit = config / "systemd/user/blue-native-sync.service"
    old_unit.parent.mkdir(parents=True)
    old_unit.write_text("old service")
    bin_dir = home / ".local/bin"
    bin_dir.mkdir(parents=True)
    for name in ("blue-messages", "blue-phone", "blue-settings", "blue-sync"):
        (bin_dir / name).write_text("old native binary")
    (bin_dir / "blue").write_text("old Bluetooth backend")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setattr(sys, "argv", [str(root / "install.py"), "--copy", "--release"])
    monkeypatch.syspath_prepend(str(root))
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[0] == 'cargo':
            assert '--locked' in command and '--release' in command
            target = root / 'target/release'
            target.mkdir(parents=True, exist_ok=True)
            for kind in ('messages', 'phone', 'reminders', 'notes', 'settings', 'sync'):
                (target / f'verdigris-{kind}').write_text('#!/bin/sh\nexit 0\n')
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, 'run', run)
    for _ in range(2):
        runpy.run_path(str(root / 'install.py'), run_name='__main__')
    for kind in ('Phone', 'Messages', 'Reminders', 'Notes'):
        app_id = f'dev.turbinebmw.Verdigris.{kind}'
        desktop = (apps / f'{app_id}.desktop').read_text()
        assert f'Name={kind}\n' in desktop
        assert f'Icon={app_id}\n' in desktop
        assert f'verdigris-{kind.lower()}' in desktop
        extension = 'svg' if kind in ('Reminders', 'Notes') else 'png'
        assert (data / f'icons/{app_id}.{extension}').read_bytes() == (NATIVE / f'data/icons/{app_id}.{extension}').read_bytes()
    assert not list(apps.glob('*.Settings.desktop'))
    assert not list(apps.glob('dev.turbinebmw.Blue.*'))
    assert not old_unit.exists()
    assert ['systemctl', '--user', 'disable', '--now', 'blue-native-sync.service'] in calls
    assert (config / 'systemd/user/verdigris-sync.service').exists()
    assert (bin_dir / 'blue').read_text() == 'old Bluetooth backend'
    for kind in ('messages', 'phone', 'reminders', 'notes', 'settings', 'sync'):
        installed = bin_dir / f'verdigris-{kind}'
        assert not installed.is_symlink()
        assert installed.stat().st_mode & 0o777 == 0o755
        assert not (bin_dir / f'blue-{kind}').exists()
