"""DBus + GLib mainloop helpers shared across the daemon.

We use python-dbus + GLib MainLoop because:
- That's what the Phase 0 spike used and it's proven to work
- python3-dbus + python3-gi are system packages (no PyPI fragility)
- libspa-bluez5 / obexd both expose their APIs cleanly through this stack

Future migration to dasbus is straightforward — only this file would need
significant changes.
"""
from __future__ import annotations

import dbus
import dbus.mainloop.glib
from gi.repository import GLib

# Install the GLib mainloop integration exactly once, at import time.
dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

system_bus: dbus.SystemBus = dbus.SystemBus()
"""org.bluez and friends live here."""

session_bus: dbus.SessionBus = dbus.SessionBus()
"""org.bluez.obex (user-session obexd) and libnotify live here."""

main_loop: GLib.MainLoop = GLib.MainLoop()


def obex(path: str, iface: str) -> dbus.Interface:
    """Quick helper for session-bus obex objects."""
    return dbus.Interface(session_bus.get_object("org.bluez.obex", path), iface)


def bluez(path: str, iface: str) -> dbus.Interface:
    """Quick helper for system-bus BlueZ objects."""
    return dbus.Interface(system_bus.get_object("org.bluez", path), iface)
