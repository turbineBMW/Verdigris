"""Long-lived MAP + PBAP OBEX sessions.

Per spike/RESULTS.md §2: the iPhone refuses repeat OBEX connects within a
short window. The daemon keeps one MAP session and one PBAP session open
for its lifetime, reopening only on observed failure.
"""
from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass

import dbus
import dbus.exceptions

from iphonebridge import config
from iphonebridge.bus import obex

log = logging.getLogger(__name__)


class SessionError(RuntimeError):
    pass


@dataclass(slots=True)
class ObexSession:
    """A live OBEX session against the iPhone (Target = "MAP" or "PBAP")."""

    target: str               # "MAP" or "PBAP"
    path: str                 # /org/bluez/obex/client/session{N}

    @property
    def message_access(self) -> dbus.Interface:
        return obex(self.path, "org.bluez.obex.MessageAccess1")

    @property
    def phonebook(self) -> dbus.Interface:
        return obex(self.path, "org.bluez.obex.PhonebookAccess1")

    @property
    def properties(self) -> dbus.Interface:
        return obex(self.path, "org.freedesktop.DBus.Properties")


def _client() -> dbus.Interface:
    return obex("/org/bluez/obex", "org.bluez.obex.Client1")


def _restart_obexd() -> None:
    """Restart user obex.service to clear stale state — see spike/RESULTS.md §2."""
    log.info("restarting user obex.service for a clean state")
    subprocess.run(["systemctl", "--user", "restart", "obex.service"],
                   check=False)
    time.sleep(1.0)


def _create_session(target: str, *, retry_on_forbidden: bool = True) -> ObexSession:
    log.info("creating OBEX session (Target=%s) to %s", target, config.IPHONE_MAC)
    try:
        path = str(_client().CreateSession(
            config.IPHONE_MAC, {"Target": target}, timeout=30.0
        ))
        return ObexSession(target=target, path=path)
    except dbus.exceptions.DBusException as e:
        msg = e.get_dbus_message() or ""
        if retry_on_forbidden and ("Forbidden" in msg or "0x43" in msg):
            log.warning("OBEX %s got Forbidden — restarting obexd and "
                        "retrying once", target)
            _restart_obexd()
            return _create_session(target, retry_on_forbidden=False)
        raise SessionError(f"CreateSession({target}) failed: {e.get_dbus_name()}: {msg}")


class SessionManager:
    """Opens and tracks one MAP and one PBAP session for the daemon lifetime."""

    def __init__(self) -> None:
        self.map: ObexSession | None = None
        self.pbap: ObexSession | None = None

    def open_all(self) -> None:
        # Don't eagerly restart obexd here — obex.service is shared across
        # every process on the session bus (daemon + any CLI command), so
        # restarting it unconditionally tears down sessions other processes
        # already have open (e.g. the daemon's live MAP listener, killed by
        # a concurrent `contacts-sync`). _create_session() already restarts
        # and retries once, but only if CreateSession actually comes back
        # Forbidden — that's the only case that needs a clean baseline.
        self.map = _create_session("MAP")
        log.info("MAP session: %s", self.map.path)
        self.pbap = _create_session("PBAP")
        log.info("PBAP session: %s", self.pbap.path)

    def is_alive(self) -> bool:
        """Is the MAP session still real, as far as obexd is concerned?

        `self.map is not None` only says we once created a session — the
        Python object long outlives the session obexd tears down when the
        phone disconnects, which made the daemon report itself healthy with
        no link at all. Ask obexd instead.
        """
        if self.map is None:
            return False
        try:
            self.map.properties.GetAll("org.bluez.obex.Session1")
            return True
        except dbus.exceptions.DBusException:
            return False

    def reopen(self) -> bool:
        """Tear down whatever is left and open fresh sessions."""
        try:
            self.close_all()
        except Exception:
            log.debug("close_all during reopen failed", exc_info=True)
        try:
            self.open_all()
            return True
        except SessionError as e:
            log.warning("session reopen failed: %s", e)
            return False

    def close_all(self) -> None:
        client = _client()
        for sess in (self.map, self.pbap):
            if sess is None:
                continue
            try:
                client.RemoveSession(sess.path)
                log.info("closed %s session: %s", sess.target, sess.path)
            except dbus.exceptions.DBusException as e:
                log.debug("RemoveSession(%s): %s", sess.path, e.get_dbus_name())
        self.map = None
        self.pbap = None

    # Convenience accessors
    @property
    def map_path(self) -> str:
        if self.map is None:
            raise SessionError("MAP session not open")
        return self.map.path

    @property
    def pbap_path(self) -> str:
        if self.pbap is None:
            raise SessionError("PBAP session not open")
        return self.pbap.path
