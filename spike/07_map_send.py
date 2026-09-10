#!/usr/bin/env python3
"""07_map_send.py — does MAP PushMessage route as iMessage?

Phase 2b research. Phase 0 + the post-launch finding (RESULTS.md §6)
showed that iOS 26.5 exposes incoming iMessage *and* SMS over MAP MNS,
labeled identically as Type=sms-gsm. The next question is the write
side: when we PushMessage to an iMessage-capable recipient, does iOS
route it as iMessage (blue bubble) or only as SMS (green bubble)?

This script:
  1. Stops the running verdigris daemon (frees the MAP session
     iPhone allows only one of at a time).
  2. Opens its own MAP session to the iPhone.
  3. Constructs a minimal valid bMessage targeted at TARGET_NUMBER.
  4. Calls MessageAccess1.PushMessage with that bMessage.
  5. Waits for the transfer to complete.
  6. Asks the user to look at the recipient's conversation on the
     iPhone and report what color the bubble is.
  7. Restarts the daemon.

EDIT TARGET_NUMBER below to a number you can verify:
  - Pick someone you know is iMessage-capable (iPhone, iMessage on)
  - Or use a second phone you have (cheap way to verify yourself)

Run:
  source ~/code/verdigris/.venv/bin/activate
  python3 ~/code/verdigris/spike/07_map_send.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

import dbus
import dbus.exceptions
from dbus.mainloop.glib import DBusGMainLoop

# ===== CONFIG — edit before running ======================================

IPHONE_MAC      = "AA:BB:CC:DD:EE:FF"

# CHANGE THIS to the number you want the test sent to.
# Format: E.164 with country code, e.g. "+15551234567".
TARGET_NUMBER   = "+15551234567"   # Contact A (known iMessage-capable)

TEST_BODY       = "verdigris MAP-send test — please ignore"

# Transparent=True: send directly without saving to iPhone's outbox.
# Transparent=False: stage in outbox and rely on the iPhone to push it.
TRANSPARENT     = True

# =========================================================================

DBusGMainLoop(set_as_default=True)
bus = dbus.SessionBus()

def obex(path, iface):
    return dbus.Interface(bus.get_object("org.bluez.obex", path), iface)

def bail(msg, code=1):
    print(f"[FAIL] {msg}", flush=True); sys.exit(code)


print("=== Phase 2b: MAP PushMessage / iMessage routing test ===\n", flush=True)

# 1) Stop daemon to free MAP session
print("[+] Stopping verdigris daemon ...", flush=True)
subprocess.run(["systemctl", "--user", "stop", "verdigris"], check=False)
time.sleep(2)

# 2) Restart obexd for a clean OBEX state (Phase 0 quirk #2)
print("[+] Restarting user obex.service ...", flush=True)
subprocess.run(["systemctl", "--user", "restart", "obex.service"], check=False)
time.sleep(1)

# 3) Open MAP session
print(f"[+] Creating MAP session to {IPHONE_MAC} ...", flush=True)
client = obex("/org/bluez/obex", "org.bluez.obex.Client1")
try:
    session_path = str(client.CreateSession(
        IPHONE_MAC, {"Target": "MAP"}, timeout=30.0
    ))
except dbus.exceptions.DBusException as e:
    bail(f"CreateSession: {e.get_dbus_name()}: {e.get_dbus_message()}", 2)
print(f"    {session_path}", flush=True)

try:
    map_iface = obex(session_path, "org.bluez.obex.MessageAccess1")

    # 4) Navigate to telecom/msg/outbox
    print("[+] SetFolder telecom/msg/outbox ...", flush=True)
    try:
        map_iface.SetFolder("/")
    except dbus.exceptions.DBusException:
        pass
    for seg in ("telecom", "msg", "outbox"):
        try:
            map_iface.SetFolder(seg)
        except dbus.exceptions.DBusException as e:
            print(f"    [WARN] SetFolder({seg}): {e.get_dbus_name()}",
                  flush=True)

    # 5) Construct minimal bMessage.
    # Outgoing structure per MAP spec 1.4:
    #   - One VCARD OUTSIDE BENV   → originator (empty for outgoing)
    #   - One VCARD INSIDE BENV    → recipient (has TEL)
    encoded = TEST_BODY.encode("utf-8")
    bmsg_lines = [
        "BEGIN:BMSG",
        "VERSION:1.0",
        "STATUS:UNREAD",
        "TYPE:SMS_GSM",                              # iOS uses this for both
        "FOLDER:telecom/msg/outbox",
        # Originator (we leave empty — iPhone fills in our own info)
        "BEGIN:VCARD",
        "VERSION:2.1",
        "N:;;;;",
        "TEL:",
        "END:VCARD",
        "BEGIN:BENV",
        # Recipient
        "BEGIN:VCARD",
        "VERSION:2.1",
        "N:;;;;",
        f"TEL:{TARGET_NUMBER}",
        "END:VCARD",
        "BEGIN:BBODY",
        "CHARSET:UTF-8",
        f"LENGTH:{len(encoded)}",
        "BEGIN:MSG",
        TEST_BODY,
        "END:MSG",
        "END:BBODY",
        "END:BENV",
        "END:BMSG",
    ]
    bmsg = "\r\n".join(bmsg_lines) + "\r\n"

    out = Path(tempfile.mkstemp(prefix="ibridge_send_", suffix=".bmsg")[1])
    out.write_text(bmsg, encoding="utf-8")
    print(f"\n[+] bMessage staged at {out}:\n", flush=True)
    print("    " + "\n    ".join(bmsg.splitlines()), flush=True)

    # 6) PushMessage — try minimal args first; if that fails, dump obex
    # journal for the precise reason.
    print(f"\n[+] PushMessage to {TARGET_NUMBER} ...", flush=True)
    attempts = [
        ("empty args, folder=telecom/msg/outbox",
         "telecom/msg/outbox", {}),
        ("empty args, folder=outbox",
         "outbox", {}),
        ("Transparent only, folder=telecom/msg/outbox",
         "telecom/msg/outbox", {"Transparent": dbus.Boolean(TRANSPARENT)}),
    ]
    transfer_path = None
    for label, folder, args in attempts:
        try:
            ret = map_iface.PushMessage(str(out), folder, args)
            transfer_path = str(ret[0]) if isinstance(ret, (tuple, list)) else str(ret)
            print(f"    [{label}] OK — transfer: {transfer_path}", flush=True)
            break
        except dbus.exceptions.DBusException as e:
            print(f"    [{label}] {e.get_dbus_name()}: "
                  f"{e.get_dbus_message() or '(no message)'}",
                  flush=True)
    if transfer_path is None:
        # Last resort: dump recent obex journal lines for the real reason
        print("\n[!] All PushMessage attempts failed. Recent obex log:",
              flush=True)
        log = subprocess.run(
            ["journalctl", "--user", "-u", "obex.service",
             "--since", "30 sec ago", "--no-pager"],
            capture_output=True, text=True,
        )
        print(log.stdout[-2000:], flush=True)
        bail("PushMessage failed — see attempts + obex log above", 3)

    # 7) Poll transfer to completion
    tprops = obex(transfer_path, "org.freedesktop.DBus.Properties")
    status = None
    for _ in range(300):  # up to 30s
        try:
            status = str(tprops.Get("org.bluez.obex.Transfer1", "Status"))
        except dbus.exceptions.DBusException:
            status = "gone"
            break
        if status in ("complete", "error"):
            break
        time.sleep(0.1)
    print(f"\n[+] transfer status: {status}", flush=True)

    if status not in ("complete", "gone"):
        bail(f"transfer did not complete: {status}", 4)

    print(f"""
================================================================
NOW VERIFY ON YOUR iPHONE:

  1. Open the conversation with {TARGET_NUMBER}.
  2. Look for the message body: "{TEST_BODY}"
  3. Report back:
     • Did the message appear in your sent thread?
     • If YES, what color is the bubble?
         BLUE  → iMessage (HUGE — outgoing iMessage works via MAP!)
         GREEN → SMS (Apple's published behavior; expected)
         Both colors / unclear → tap-and-hold the bubble for details
     • Did the recipient (the other person) actually receive it?
================================================================
""", flush=True)

finally:
    try:
        client.RemoveSession(session_path)
        print("[+] Session closed.", flush=True)
    except Exception:
        pass
    print("[+] Restarting verdigris daemon ...", flush=True)
    subprocess.run(["systemctl", "--user", "start", "verdigris"], check=False)
