"""Typer CLI entrypoints for Verdigris (`verdigris`)."""
from __future__ import annotations

import logging
import os
import shutil
import sys

import typer

from verdigris import bluez_setup, config

app = typer.Typer(
    add_completion=False,
    name="verdigris",
    help="Verdigris — your iPhone’s messages, calls, and notifications on Linux.",
    no_args_is_help=True,
)


def _find_obexd() -> str | None:
    """Find BlueZ's OBEX daemon across common distro layouts."""
    found = shutil.which("obexd")
    if found:
        return found
    for candidate in (
        "/usr/libexec/bluetooth/obexd",  # Debian/Ubuntu
        "/usr/lib/bluetooth/obexd",      # Arch/Fedora and derivatives
    ):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _managed_has_ancs(managed: dict, device_path: str) -> bool:
    """Do BlueZ's managed objects contain all ANCS chars for this device?"""
    from verdigris.ancs.constants import ANCS_CHAR_UUIDS

    found: set[str] = set()
    prefix = f"{device_path}/"
    for path, ifaces in managed.items():
        if not str(path).startswith(prefix):
            continue
        char = ifaces.get("org.bluez.GattCharacteristic1")
        if char is not None:
            found.add(str(char.get("UUID", "")).lower())
    return ANCS_CHAR_UUIDS <= found


def _managed_has_live_ancs(managed: dict, device_path: str) -> bool:
    """Do the cached ANCS chars belong to a currently connected LE bearer?"""
    device_ifaces = next(
        (ifaces for path, ifaces in managed.items()
         if str(path) == device_path),
        {},
    )
    le = device_ifaces.get("org.bluez.Bearer.LE1")
    if le is not None:
        # BlueZ 5.87 retains GATT objects and Notifying=true after LE drops.
        # Its bearer-specific state is the authoritative liveness check.
        live = bool(le.get("Connected", False))
    else:
        # Compatibility fallback for BlueZ versions without Bearer.LE1.
        device = device_ifaces.get("org.bluez.Device1", {})
        live = bool(device.get("Connected", False))
    return live and _managed_has_ancs(managed, device_path)


def _ancs_gatt_available() -> bool:
    """Whether the configured iPhone currently exposes ANCS over BLE."""
    import dbus

    device_path = (
        f"/org/bluez/{config.ADAPTER}/dev_"
        f"{config.IPHONE_MAC.replace(':', '_').upper()}"
    )
    try:
        bus = dbus.SystemBus()
        manager = dbus.Interface(
            bus.get_object("org.bluez", "/"),
            "org.freedesktop.DBus.ObjectManager",
        )
        return _managed_has_live_ancs(
            manager.GetManagedObjects(), device_path
        )
    except dbus.exceptions.DBusException:
        return False


def _set_preferred_bearer(bus, device_path: str) -> bool:
    """Prefer LE through BlueZ's live API when that API is enabled."""
    import dbus

    try:
        props = dbus.Interface(
            bus.get_object("org.bluez", device_path),
            "org.freedesktop.DBus.Properties",
        )
        props.Set(
            "org.bluez.Device1",
            "PreferredBearer",
            dbus.String("le", variant_level=1),
        )
        return True
    except dbus.exceptions.DBusException as e:
        logging.getLogger(__name__).debug(
            "PreferredBearer API unavailable: %s", e.get_dbus_name())
        return False


def _connect_le_bearer(bus, device_path: str) -> bool | None:
    """Connect only LE, preserving an existing Classic MAP/PBAP link.

    Returns None when this BlueZ version lacks the bearer-specific API so the
    caller can use the legacy whole-device reconnect path.
    """
    import dbus

    try:
        bearer = dbus.Interface(
            bus.get_object("org.bluez", device_path),
            "org.bluez.Bearer.LE1",
        )
        bearer.Connect(timeout=20.0)
        return True
    except dbus.exceptions.DBusException as e:
        if e.get_dbus_name() == "org.bluez.Error.AlreadyConnected":
            return True
        if e.get_dbus_name() in {
            "org.freedesktop.DBus.Error.UnknownInterface",
            "org.freedesktop.DBus.Error.UnknownMethod",
            "org.bluez.Error.NotSupported",
        }:
            return None
        logging.getLogger(__name__).debug(
            "LE bearer connect failed: %s: %s",
            e.get_dbus_name(), e.get_dbus_message(),
        )
        return False


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s %(levelname)-5s %(name)s: %(message)s"
    logging.basicConfig(level=level, format=fmt, stream=sys.stderr)


@app.command()
def run(verbose: bool = typer.Option(False, "-v", "--verbose")):
    """Start the Verdigris daemon (runs until Ctrl+C / SIGTERM)."""
    _setup_logging(verbose)
    # Import inside command to avoid loading dbus stack just to print --help
    from verdigris.daemon import Daemon
    Daemon().run()


@app.command()
def doctor(verbose: bool = typer.Option(False, "-v", "--verbose")):
    """Check that all prerequisites are in place."""
    _setup_logging(verbose)
    log = logging.getLogger("doctor")

    ok = True

    # VERDIGRIS_MAC configured?
    if config.IPHONE_MAC.upper() in ("AA:BB:CC:DD:EE:FF", ""):
        log.error("VERDIGRIS_MAC not configured (still the placeholder).")
        log.error("    Set your iPhone's Bluetooth MAC via env var, e.g.:")
        log.error("    export VERDIGRIS_MAC=AA:BB:CC:DD:EE:FF")
        log.error("    Or persist it in ~/.config/verdigris/local.env")
        log.error("    (see README — 'Setup'). The systemd unit picks it up.")
        ok = False
    else:
        log.info("Target MAC configured: %s", config.IPHONE_MAC)

    # BlueZ OBEX daemon present? Its location and package name vary by distro.
    obexd = _find_obexd()
    if obexd is None:
        log.error("BlueZ OBEX daemon (obexd) not found")
        log.error("    Install your distro's BlueZ OBEX package "
                  "(bluez-obexd on Debian/Ubuntu; bluez-obex on Arch).")
        ok = False
    else:
        log.info("BlueZ OBEX daemon installed: %s", obexd)

    # Adapter CoD
    cod = bluez_setup.current_cod()
    if cod is None:
        log.error("Adapter %s not reachable via DBus", config.ADAPTER)
        ok = False
    else:
        match = bluez_setup.desired_cod_matches(cod)
        if match:
            log.info("Adapter CoD = 0x%06x (A/V Hands-Free)  OK", cod)
        else:
            log.warning("Adapter CoD = 0x%06x — not A/V Hands-Free. "
                        "Run `verdigris run` (needs sudo) or set manually:",
                        cod)
            log.warning("    sudo btmgmt class %d %d",
                        config.COD_MAJOR, config.COD_MINOR)
            ok = False

    # State dir writable
    try:
        config.ensure_dirs()
        log.info("State dir writable: %s", config.STATE_DIR)
    except OSError as e:
        log.error("State dir not writable: %s", e)
        ok = False

    # ANCS is an optional, hardware-dependent BLE feature. Report it without
    # making an otherwise healthy MAP/PBAP setup fail doctor.
    if _ancs_gatt_available():
        log.info("ANCS GATT service active (per-app notifications)  OK")
    else:
        log.info("ANCS GATT service not active (optional; requires a BLE bond)")

    if ok:
        typer.echo(typer.style("All checks passed.", fg=typer.colors.GREEN))
    else:
        typer.echo(typer.style("One or more checks FAILED.",
                               fg=typer.colors.RED))
        raise typer.Exit(code=1)


@app.command()
def contacts_sync(verbose: bool = typer.Option(False, "-v", "--verbose")):
    """Force a fresh PBAP pull from the iPhone (rebuilds the contacts cache)."""
    _setup_logging(verbose)

    # Prefer the running daemon: the iPhone allows only one OBEX session at
    # a time, so opening our own here fails with "Connection refused"
    # whenever the daemon already holds one.
    import dbus
    import dbus.exceptions
    try:
        svc = dbus.Interface(
            dbus.SessionBus().get_object("dev.turbinebmw.Verdigris.Bridge",
                                         "/dev/turbinebmw/Verdigris/Bridge"),
            "dev.turbinebmw.Verdigris.Bridge.Messages1")
        n = int(svc.RefreshContacts(timeout=180))
        typer.echo(f"Pulled contacts via the daemon — {n} cached in "
                   f"{config.CONTACTS_DB}")
        return
    except dbus.exceptions.DBusException as e:
        name = e.get_dbus_name() or ""
        if "ServiceUnknown" not in name and "NoReply" not in name:
            typer.echo(typer.style(
                f"Daemon refresh failed: {e.get_dbus_message() or e}",
                fg=typer.colors.RED))
            raise typer.Exit(1)
        typer.echo("Daemon not running — opening a direct session instead.")

    from verdigris.contacts import pull_phonebook
    from verdigris.obex.sessions import SessionManager
    sm = SessionManager()
    sm.open_all()
    try:
        n = pull_phonebook(sm)
        typer.echo(f"Pulled {n} contacts into {config.CONTACTS_DB}")
    finally:
        sm.close_all()


@app.command("sms-list")
def sms_list(
    n: int = typer.Option(20, "-n", "--limit", help="Max messages to show (most recent first)"),
    source: str = typer.Option("iphone", "--source",
                               help="iphone (live MAP query) | local (message store)"),
    folder: str = typer.Option("telecom/msg/INBOX", "--folder",
                               help="MAP folder when --source=iphone "
                                    "(e.g. telecom/msg/INBOX or telecom/msg/sent)"),
    from_contact: str = typer.Option(None, "--from",
                                     help="Only show messages from this contact name or phone"),
):
    """Show recent SMS / iMessage history.

    --source iphone (default): live MAP query via the running daemon. Shows
        the iPhone's actual recent inbox (or other folder via --folder).
    --source local: read ~/.local/state/verdigris/messages.sqlite
        (live + backup history). Works even if the daemon isn't running.
    """
    import json
    from datetime import datetime

    from verdigris.events import normalize_phone

    # ---- build a from-filter predicate from --from ----------------------
    # Two layers:
    #   • filter_phone_norms — for events that have a phone digit string
    #   • from_text_lower    — for events where MAP returned only the
    #                          contact's FN as sender (no phone)
    filter_phone_norms: set[str] = set()
    from_text_lower: str | None = None

    if from_contact:
        if _RECIPIENT_LOOKS_LIKE_PHONE.match(from_contact):
            norm = normalize_phone(from_contact) or ""
            filter_phone_norms = {norm, norm[-10:]} if len(norm) >= 10 else {norm}
        else:
            from verdigris.contacts import ContactsResolver
            matches = ContactsResolver().find_by_name(from_contact)
            if not matches:
                typer.echo(typer.style(
                    f"No contact matched {from_contact!r}.",
                    fg=typer.colors.YELLOW))
                raise typer.Exit(code=1)
            for _, phone in matches:
                filter_phone_norms.add(phone)
                if len(phone) >= 10:
                    filter_phone_norms.add(phone[-10:])
            from_text_lower = from_contact.lower()

    def passes_from_filter(e: dict) -> bool:
        if not filter_phone_norms and not from_text_lower:
            return True
        # Phone match (for entries with real phone digits)
        sp = (e.get("sender_phone_norm") or "")
        if sp:
            sp_tail = sp[-10:] if len(sp) >= 10 else sp
            if sp in filter_phone_norms or sp_tail in filter_phone_norms:
                return True
        # Substring match against raw sender (for FN-only entries)
        if from_text_lower:
            raw = (e.get("sender") or e.get("sender_phone") or "").lower()
            if from_text_lower in raw:
                return True
        return False

    # ---- when filtering, pull a wider net from MAP ----------------------
    fetch_n = max(n * 10, 100) if (filter_phone_norms or from_text_lower) else n

    # ---- helper to render a record ---------------------------------------
    def render(sender: str, body: str, ts_str: str, *, read: bool = True) -> None:
        if len(body) > 120:
            body = body[:119] + "…"
        body = body.replace("\n", " ⏎ ")
        sender_styled = typer.style(f"{sender:>20s}",
                                    fg=typer.colors.CYAN, bold=True)
        ts_styled = typer.style(ts_str, dim=True)
        unread = typer.style("•", fg=typer.colors.YELLOW) if not read else " "
        typer.echo(f"{ts_styled}  {unread} {sender_styled}  {body}")

    # ---- live MAP source ------------------------------------------------
    if source == "iphone":
        import dbus
        import dbus.mainloop.glib
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        bus = dbus.SessionBus()
        try:
            proxy = bus.get_object("dev.turbinebmw.Verdigris.Bridge",
                                   "/dev/turbinebmw/Verdigris/Bridge")
            iface = dbus.Interface(proxy, "dev.turbinebmw.Verdigris.Bridge.Messages1")
        except dbus.exceptions.DBusException as e:
            typer.echo(typer.style(
                f"Daemon not reachable on DBus: {e.get_dbus_message()}\n"
                "Falling back to local store (--source local).",
                fg=typer.colors.YELLOW,
            ))
            source = "local"
        else:
            try:
                raw = str(iface.ListRecent(folder, dbus.UInt32(fetch_n), timeout=30))
            except dbus.exceptions.DBusException as e:
                typer.echo(typer.style(
                    f"Live query failed: {e.get_dbus_message() or e.get_dbus_name()}\n"
                    "Falling back to local store.",
                    fg=typer.colors.YELLOW,
                ))
                source = "local"
            else:
                msgs = json.loads(raw)
                if filter_phone_norms or from_text_lower:
                    msgs = [m for m in msgs if passes_from_filter(m)]
                msgs = msgs[:n]
                if not msgs:
                    if filter_phone_norms or from_text_lower:
                        typer.echo(typer.style(
                            "(no recent messages from that contact in the "
                            "iPhone's MAP inbox window)", fg=typer.colors.YELLOW))
                        typer.echo("iOS only exposes a small slice of recent "
                                   "messages via MAP. For older history, try:")
                        typer.echo(typer.style(
                            f"  verdigris sms-list --from {from_contact!r} "
                            f"--source local -n {n}",
                            fg=typer.colors.WHITE))
                    else:
                        typer.echo("(no messages)")
                    return
                # Resolve contact names via local cache
                from verdigris.contacts import ContactsResolver
                resolver = ContactsResolver()
                for m in msgs:
                    contact = resolver.resolve(m.get("sender") or
                                               m.get("sender_phone_norm"))
                    sender = contact or m.get("sender") or "?"
                    ts_raw = m.get("timestamp", "")
                    try:
                        dt = datetime.fromisoformat(ts_raw)
                        ts = dt.astimezone().strftime("%m-%d %H:%M")
                    except (ValueError, AttributeError):
                        ts = ts_raw[:16] if ts_raw else "??-?? ??:??"
                    render(sender, m.get("body", ""), ts,
                           read=m.get("read", True))
                return

    # ---- local SQLite message store ------------------------------------
    from verdigris.message_store import MESSAGE_KINDS, MessageStore

    store = MessageStore()
    # Pull a wider net when filtering, then take the newest N matches.
    pull = max(n * 10, 100) if (filter_phone_norms or from_text_lower) else n
    try:
        events = store.read_events(kinds=set(MESSAGE_KINDS), limit=pull)
    except Exception as e:
        typer.echo(typer.style(
            f"Could not read message store at {config.MESSAGES_DB}: {e}",
            fg=typer.colors.YELLOW,
        ))
        raise typer.Exit(code=1)

    if not events and not config.MESSAGES_DB.exists():
        typer.echo(typer.style(
            f"No local message store yet at {config.MESSAGES_DB}",
            fg=typer.colors.YELLOW,
        ))
        typer.echo("Is the Verdigris daemon running? "
                   "Try: systemctl --user status verdigris")
        raise typer.Exit(code=1)

    if filter_phone_norms or from_text_lower:
        events = [e for e in events if passes_from_filter(e)]

    # Newest first for display (store returns oldest-first within the window).
    events = list(reversed(events[-n:]))
    if not events:
        typer.echo("(no events)")
        return

    for e in events:
        sender = e.get("contact_name") or e.get("sender_phone") or "?"
        body = e.get("body") or ""
        ts_raw = e.get("timestamp") or e.get("seen_at") or ""
        try:
            dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            ts = dt.astimezone().strftime("%m-%d %H:%M")
        except (ValueError, AttributeError):
            ts = ts_raw[:16]
        render(sender, body, ts, read=e.get("is_read", True))


@app.command("ancs-enable")
def ancs_enable(
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    """Enable ANCS (per-app notifications) for the paired iPhone.

    Requires the sudoers helper installed via
        sudo bash systemd/install-ancs-sudoers.sh

    What this does:
      1. Looks up the local adapter MAC.
      2. Sets BlueZ's live PreferredBearer property to LE, falling back to
         the sudoers-gated bonding-record helper on older BlueZ versions.
      3. Restores BlueZ's LE address-resolution flag and connects the LE
         bearer without disrupting an existing Classic MAP/PBAP connection.
    """
    _setup_logging(verbose)
    import subprocess
    import time

    import dbus
    import dbus.mainloop.glib
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    sysbus = dbus.SystemBus()

    # Adapter MAC via BlueZ DBus
    try:
        adapter_mac = str(
            dbus.Interface(
                sysbus.get_object("org.bluez", f"/org/bluez/{config.ADAPTER}"),
                "org.freedesktop.DBus.Properties",
            ).Get("org.bluez.Adapter1", "Address")
        )
    except dbus.exceptions.DBusException as e:
        typer.echo(typer.style(
            f"Couldn't read adapter MAC: {e.get_dbus_message()}",
            fg=typer.colors.RED))
        raise typer.Exit(code=2) from None

    device_mac = config.IPHONE_MAC
    device_path = (
        f"/org/bluez/{config.ADAPTER}/dev_"
        f"{device_mac.replace(':', '_').upper()}"
    )
    typer.echo(f"adapter: {adapter_mac}  device: {device_mac}")

    if _set_preferred_bearer(sysbus, device_path):
        typer.echo(typer.style(
            "[ok] BlueZ live PreferredBearer=le", fg=typer.colors.GREEN))
    else:
        typer.echo(typer.style(
            "BlueZ live PreferredBearer API unavailable; using the on-disk "
            "fallback",
            fg=typer.colors.YELLOW,
        ))
        r = subprocess.run(
            ["sudo", "-n", "/usr/local/bin/verdigris-set-le-bearer",
             adapter_mac, device_mac],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            msg = r.stderr.strip() or r.stdout.strip() or "(no output)"
            typer.echo(typer.style(f"helper failed: {msg}", fg=typer.colors.RED))
            if "password is required" in msg or "may not run" in msg.lower():
                typer.echo("Install the ANCS setup first:")
                typer.echo("  sudo bash systemd/install-ancs-sudoers.sh")
            raise typer.Exit(code=3)
        typer.echo(typer.style(r.stdout.strip(), fg=typer.colors.GREEN))

    resolution_ready = bluez_setup.enable_le_address_resolution()
    if resolution_ready:
        typer.echo(typer.style(
            "[ok] LE address resolution enabled", fg=typer.colors.GREEN))
    else:
        typer.echo(typer.style(
            "LE address-resolution helper unavailable; the connection may "
            "time out on BlueZ 5.87",
            fg=typer.colors.YELLOW,
        ))

    if _ancs_gatt_available():
        typer.echo(typer.style(
            "✓ ANCS is already live.", fg=typer.colors.GREEN))
        return

    typer.echo("connecting the LE bearer ...")
    le_result = _connect_le_bearer(sysbus, device_path)
    if le_result is None:
        # Older BlueZ has no bearer-specific API. Retain the prior fallback,
        # but wait for Disconnect() to settle before asking Connect().
        typer.echo("bearer-specific API unavailable; cycling the device ...")
        subprocess.run(["bluetoothctl", "disconnect", device_mac],
                       capture_output=True)
        for _ in range(40):
            if not bluez_setup.device_connected():
                break
            time.sleep(0.25)
        r = subprocess.run(["bluetoothctl", "connect", device_mac],
                           capture_output=True, text=True)
        output = (r.stdout or r.stderr).strip()
        typer.echo(output.splitlines()[-1]
                   if output else "(reconnect returned no output)")
        le_result = r.returncode == 0

    if not le_result:
        # Connect can time out even if iOS completes its own inbound connection
        # moments later. Poll the live LE state before declaring failure.
        typer.echo(typer.style(
            "LE connect did not complete; still waiting for an iPhone-initiated "
            "LE connection ...",
            fg=typer.colors.YELLOW,
        ))

    typer.echo("waiting for a live ANCS GATT connection ...")
    for _ in range(40):
        if _ancs_gatt_available():
            typer.echo(typer.style(
                "✓ ANCS is live. Allow the iPhone notification prompt, or "
                "enable Share System Notifications in Bluetooth settings.",
                fg=typer.colors.GREEN,
            ))
            return
        time.sleep(0.5)

    typer.echo(typer.style(
        "ANCS is not live: the BLE bearer did not connect.",
        fg=typer.colors.YELLOW,
    ))
    typer.echo("The existing bond may still be valid; confirm `LE.Bonded: yes` before")
    typer.echo("considering a fresh pairing. Keep Verdigris running and try again nearby.")
    raise typer.Exit(code=4)


@app.command("pair-setup")
def pair_setup(
    no_restart: bool = typer.Option(False, "--no-restart",
                                     help="Don't restart the daemon at the end"),
):
    """First-run wizard: pick a paired iPhone, write the local config,
    walk through the iPhone-side toggle steps."""
    from verdigris.pair_setup import run_wizard
    raise typer.Exit(code=run_wizard(restart_after=not no_restart))


_RECIPIENT_LOOKS_LIKE_PHONE = __import__("re").compile(r"^\+?[\d\s()\-.]{7,}$")


def _resolve_recipient(raw: str) -> str:
    """Turn a recipient argument into a phone number.

    - If it parses as a phone number, return it normalized with leading +.
    - Otherwise, treat as a contact name substring, look it up in the
      contacts cache, prompt to disambiguate if multiple matches.

    Aborts the command (Exit) if no match is found.
    """
    raw = raw.strip()
    if _RECIPIENT_LOOKS_LIKE_PHONE.match(raw):
        # Keep it as the user typed it; daemon-side bMessage builder is
        # liberal about format.
        return raw

    # Treat as a name. Pull candidates from contact cache.
    from verdigris.contacts import ContactsResolver
    resolver = ContactsResolver()
    matches = resolver.find_by_name(raw)
    if not matches:
        typer.echo(typer.style(
            f"No contact matched {raw!r}. Try a phone number with +, or run "
            "`verdigris contacts-sync` to refresh the cache.",
            fg=typer.colors.RED,
        ))
        raise typer.Exit(code=2)

    # Unique-by-name first; if exactly one unique name (across possibly
    # multiple numbers), pick a sensible default.
    by_name: dict[str, list[str]] = {}
    for name, phone in matches:
        by_name.setdefault(name, []).append(phone)

    if len(by_name) == 1:
        name = next(iter(by_name))
        phones = by_name[name]
        if len(phones) == 1:
            chosen = phones[0]
            typer.echo(typer.style(
                f"→ {name}  +{chosen}", fg=typer.colors.CYAN))
            return f"+{chosen}"
        # Multiple phones for one contact — list and prompt
        typer.echo(f"{name} has multiple numbers:")
        for i, p in enumerate(phones, 1):
            typer.echo(f"  [{i}] +{p}")
        idx = typer.prompt("Pick", type=int, default=1)
        return f"+{phones[idx - 1]}"

    # Multiple distinct contacts — show + prompt
    typer.echo(f"Multiple contacts match {raw!r}:")
    flat: list[tuple[str, str]] = []
    for name, phones in sorted(by_name.items()):
        for p in phones:
            flat.append((name, p))
    for i, (name, p) in enumerate(flat, 1):
        typer.echo(f"  [{i}] {name}  +{p}")
    idx = typer.prompt("Pick", type=int, default=1)
    try:
        chosen_name, chosen_phone = flat[idx - 1]
    except IndexError:
        typer.echo(typer.style("Invalid choice.", fg=typer.colors.RED))
        raise typer.Exit(code=2) from None
    typer.echo(typer.style(
        f"→ {chosen_name}  +{chosen_phone}", fg=typer.colors.CYAN))
    return f"+{chosen_phone}"


@app.command("sms-send")
def sms_send(
    recipient: str = typer.Argument(...,
        help="Recipient: phone (e.g. +15551234567) OR contact name substring "
             "(e.g. 'Maddie')"),
    body: str = typer.Argument(..., help="Message body"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    """Send an SMS or iMessage via the running daemon's MAP session.

    The iPhone automatically routes to iMessage when the recipient is
    iMessage-capable (blue bubble). Otherwise falls back to SMS.

    Requires the Verdigris daemon to be running
    (systemctl --user start verdigris).
    """
    _setup_logging(verbose)

    # If the recipient doesn't look like a phone, resolve via contacts.
    resolved = _resolve_recipient(recipient)

    import dbus
    import dbus.mainloop.glib
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()
    try:
        proxy = bus.get_object("dev.turbinebmw.Verdigris.Bridge",
                               "/dev/turbinebmw/Verdigris/Bridge")
        iface = dbus.Interface(proxy, "dev.turbinebmw.Verdigris.Bridge.Messages1")
    except dbus.exceptions.DBusException as e:
        typer.echo(typer.style(
            f"Couldn't reach the Verdigris daemon on D-Bus: {e.get_dbus_message()}",
            fg=typer.colors.RED,
        ))
        typer.echo("Start it with: systemctl --user start verdigris")
        raise typer.Exit(code=2) from None

    try:
        transfer = str(iface.Send(resolved, body, timeout=45))
    except dbus.exceptions.DBusException as e:
        typer.echo(typer.style(
            f"Send failed: {e.get_dbus_name()}\n  {e.get_dbus_message()}",
            fg=typer.colors.RED,
        ))
        raise typer.Exit(code=3) from None

    typer.echo(typer.style(
        f"Sent. Transfer: {transfer}",
        fg=typer.colors.GREEN,
    ))


def _daemon_iface(iface_name: str):
    """Build an Interface onto the running daemon's session-bus object.

    The proxy is lazy — connection errors surface when a method is called,
    so callers should wrap the actual call in a try/except.
    """
    import dbus
    import dbus.mainloop.glib
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()
    proxy = bus.get_object("dev.turbinebmw.Verdigris.Bridge",
                           "/dev/turbinebmw/Verdigris/Bridge")
    return dbus.Interface(proxy, iface_name)


@app.command()
def call(
    recipient: str = typer.Argument(...,
        help="Phone number (e.g. +15551234567) OR contact name (e.g. 'Maddie')"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    """Place a phone call through the iPhone (HFP Hands-Free).

    Call audio routes through the laptop's mic + speakers. Requires the
    Verdigris daemon running and HFP set up — see `verdigris hfp-enable`.
    """
    _setup_logging(verbose)
    import dbus

    resolved = _resolve_recipient(recipient)
    iface = _daemon_iface("dev.turbinebmw.Verdigris.Bridge.Calls1")
    try:
        call_path = str(iface.Dial(resolved, timeout=45))
    except dbus.exceptions.DBusException as e:
        typer.echo(typer.style(
            f"Call failed: {e.get_dbus_name()}\n  {e.get_dbus_message()}",
            fg=typer.colors.RED,
        ))
        typer.echo("Is the Verdigris daemon running and the iPhone connected? "
                   "Try `verdigris hfp-enable`.")
        raise typer.Exit(code=3) from None
    typer.echo(typer.style(f"Calling {resolved} …  ({call_path})",
                           fg=typer.colors.GREEN))


@app.command()
def hangup(verbose: bool = typer.Option(False, "-v", "--verbose")):
    """Hang up all active phone calls."""
    _setup_logging(verbose)
    import dbus

    iface = _daemon_iface("dev.turbinebmw.Verdigris.Bridge.Calls1")
    try:
        iface.HangupAll(timeout=30)
    except dbus.exceptions.DBusException as e:
        typer.echo(typer.style(
            f"Hangup failed: {e.get_dbus_message() or e.get_dbus_name()}",
            fg=typer.colors.RED,
        ))
        raise typer.Exit(code=3) from None
    typer.echo("Hung up.")


@app.command()
def calls(verbose: bool = typer.Option(False, "-v", "--verbose")):
    """List active phone calls."""
    _setup_logging(verbose)
    import json

    import dbus

    iface = _daemon_iface("dev.turbinebmw.Verdigris.Bridge.Calls1")
    try:
        raw = str(iface.ListCalls(timeout=20))
    except dbus.exceptions.DBusException as e:
        typer.echo(typer.style(
            f"Query failed: {e.get_dbus_message() or e.get_dbus_name()}",
            fg=typer.colors.RED,
        ))
        raise typer.Exit(code=3) from None
    data = json.loads(raw)
    if not data:
        typer.echo("(no active calls)")
        return
    for c in data:
        peer = c.get("contact_name") or c.get("peer_phone") or "(unknown)"
        arrow = "←" if c.get("direction") == "incoming" else "→"
        typer.echo(f"  {arrow} {peer:<24s}  {c.get('state', '?')}")


@app.command("hfp-enable")
def hfp_enable(verbose: bool = typer.Option(False, "-v", "--verbose")):
    """Set up HFP call support.

    Uses PipeWire's native telephony backend when available. On older systems,
    writes the WirePlumber config that routes HFP/HSP through oFono and prints
    the remaining setup steps. HFP lets you take and place iPhone calls on the
    laptop — caller ID, answer/decline, dialing.
    """
    _setup_logging(verbose)
    import shutil
    import subprocess
    from pathlib import Path

    from verdigris.hfp.ofono_client import (
        native_pipewire_available,
        write_wireplumber_config,
    )

    if native_pipewire_available():
        typer.echo(typer.style(
            "PipeWire native HFP call control is ready.",
            fg=typer.colors.GREEN,
        ))
        typer.echo("No Bluetooth, pairing, or WirePlumber configuration was changed.")
        subprocess.run(
            ["systemctl", "--user", "try-restart", "verdigris"],
            check=False,
        )
        typer.echo("Verdigris restarted if it was already running.")
        return

    path, backup = write_wireplumber_config()
    if backup:
        typer.echo(f"Wrote {path}  (previous backed up → {backup})")
    else:
        typer.echo(f"Wrote {path}")

    typer.echo("Restarting WirePlumber / PipeWire …")
    subprocess.run(
        ["systemctl", "--user", "restart",
         "wireplumber", "pipewire", "pipewire-pulse"],
        check=False,
    )

    ofono_installed = bool(shutil.which("ofonod")) \
        or Path("/usr/sbin/ofonod").exists()
    typer.echo("")
    if ofono_installed:
        typer.echo(typer.style("oFono is installed.", fg=typer.colors.GREEN))
        typer.echo("Finish setup — this needs root, run it yourself:")
        typer.echo(typer.style(
            "  sudo systemctl restart ofono", fg=typer.colors.WHITE))
        typer.echo("    (restart oFono AFTER WirePlumber so it can claim the "
                   "HFP profile)")
    else:
        typer.echo(typer.style("oFono is NOT installed.",
                               fg=typer.colors.YELLOW))
        typer.echo("Install it and enable the service — needs root:")
        typer.echo(typer.style(
            "  sudo apt install ofono", fg=typer.colors.WHITE))
        typer.echo(typer.style(
            "  sudo systemctl enable --now ofono", fg=typer.colors.WHITE))

    typer.echo(f"  bluetoothctl disconnect {config.IPHONE_MAC}")
    typer.echo(f"  bluetoothctl connect {config.IPHONE_MAC}")
    typer.echo("")
    typer.echo("Then restart the daemon:  "
               "systemctl --user restart verdigris")


@app.command("backup-wifi")
def backup_wifi(
    off: bool = typer.Option(False, "--off", help="Turn Wi-Fi sync back off."),
):
    """Allow backups over Wi-Fi, so the cable is only needed once.

    Flips iOS's `EnableWiFiConnections` — the same switch as Finder's "Sync
    over Wi-Fi". The phone must be plugged in for this one command; after
    that it can be backed up wirelessly whenever it's on the same network.
    """
    import subprocess

    from verdigris.backup.runner import device_info

    if device_info() is None:
        typer.echo(typer.style(
            "Connect the iPhone by USB for this one step.",
            fg=typer.colors.RED))
        raise typer.Exit(1)

    state = "off" if off else "on"
    proc = subprocess.run(
        [sys.executable, "-m", "pymobiledevice3", "lockdown",
         "wifi-connections", "--state", state],
        capture_output=True, text=True)
    if proc.returncode != 0:
        typer.echo(typer.style(
            f"Could not set Wi-Fi sync: {(proc.stderr or proc.stdout).strip()}",
            fg=typer.colors.RED))
        raise typer.Exit(1)
    typer.echo(typer.style(f"Wi-Fi sync {state}.", fg=typer.colors.GREEN))
    if not off:
        typer.echo("You can unplug now — backups will find the phone over "
                   "the network when it's unlocked and on Wi-Fi.")


@app.command("backup-sync")
def backup_sync(
    skip_backup: bool = typer.Option(
        False, "--skip-backup",
        help="Use the existing backup instead of taking a fresh one."),
    limit: int = typer.Option(
        0, "--limit", help="Only import the newest N messages (0 = all)."),
    if_available: bool = typer.Option(
        False, "--if-available",
        help="Exit quietly when the phone isn't reachable (for timers)."),
    udid: str = typer.Option(
        "", "--udid",
        help="Sync this device instead of the one the history came from."),
):
    """Pull messages + attachments from a USB backup of the iPhone.

    Bluetooth MAP can't carry attachments, messages you sent from the phone,
    or reply threading. A local backup has all of it. Needs the iPhone
    connected by USB and trusted — no Mac involved.
    """
    from verdigris.backup.runner import (
        BackupError,
        device_info,
        is_paired,
        pair,
    )
    from verdigris.backup.sync import sync

    # --skip-backup reads a backup that's already on disk, so it needs no
    # device at all. Only require one when we're actually going to talk to
    # the phone.
    if not skip_backup:
        info = device_info(udid or None)
        if info is None:
            if if_available:
                # Scheduled run and the phone isn't around — that's normal,
                # not a failure. Say so quietly and leave the unit green.
                typer.echo("iPhone not reachable — skipping this run.")
                raise typer.Exit(0)
            typer.echo(typer.style(
                "No iOS device found over USB.", fg=typer.colors.RED))
            typer.echo("  • Connect the iPhone with a cable")
            typer.echo("  • Unlock it and tap Trust, then enter your passcode")
            typer.echo("  • Or pass --skip-backup to import the last backup")
            raise typer.Exit(1)

        typer.echo(f"Device: {info.name or '?'} (iOS {info.version or '?'})")

        if not is_paired(info.udid):
            typer.echo("Not paired yet — tap Trust on the iPhone…")
            ok, msg = pair(info.udid)
            if not ok:
                typer.echo(typer.style(f"Pairing failed: {msg}",
                                       fg=typer.colors.RED))
                raise typer.Exit(1)
            typer.echo("Paired.")
    else:
        typer.echo("Using the existing backup on disk (no device needed).")

    try:
        events = sync(run_new_backup=not skip_backup,
                      limit=limit or None,
                      udid=udid or None,
                      progress=lambda line: typer.echo(f"  {line}")
                      if "Sending" not in line else None)
    except BackupError as e:
        typer.echo(typer.style(str(e), fg=typer.colors.RED))
        raise typer.Exit(1)

    sent = sum(1 for e in events if e["kind"] == "sms_sent")
    atts = sum(len(e["attachments"]) for e in events)
    typer.echo(typer.style(
        f"\n{len(events)} messages  ({sent} sent from the phone, "
        f"{atts} attachments extracted)", fg=typer.colors.GREEN))
    typer.echo("Open the app to see them — restart it if it's already running.")


@app.command()
def version():
    """Print version and exit."""
    from verdigris import __version__
    typer.echo(f"verdigris {__version__}")


if __name__ == "__main__":
    app()
