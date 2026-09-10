#!/usr/bin/env python3
"""05b_hfp_ofono.py  —  HFP HF role feasibility, take 2: oFono call control.

Phase 0's 05_hfp_audio.py only proved PipeWire *sees* the iPhone as an audio
card. It never tested call control or SCO audio. This spike answers the real
go/no-go questions for the HFP Hands-Free feature:

  1. With the iPhone connected, does oFono expose an HFP modem with a
     VoiceCallManager? (org.ofono on the SYSTEM bus.)
  2. Incoming call  → does a VoiceCall object appear with caller ID
     (LineIdentification) and a usable State?
  3. Answer()       → does the SCO audio link come up and a PipeWire HFP
     source/sink appear?
  4. Hangup()       → does the call end cleanly?
  5. Outgoing Dial  → does the iPhone actually place the call, reliably?
     (This is the BACKLOG.md "Won't do" assumption — 'HFP HF can't reliably
     ATD on iPhone' — under empirical test. It has never actually been tried.)
  6. Which codec gets negotiated (CVSD narrowband vs mSBC wideband)?

The verdict picks the integration backend (see plan Phase A0 decision gate):
oFono client  vs.  roll-our-own AT-over-RFCOMM  vs.  incoming-only.

Prerequisites
-------------
  sudo apt install ofono
  sudo systemctl enable --now ofono
  # WirePlumber must hand HFP to oFono, not its own native backend.
  # This script writes that config for you on first run if it's missing.

Run
---
    python3 05b_hfp_ofono.py 2>&1 | tee results/05b_hfp_ofono.log

It's an interactive guided test — it will prompt you to place real calls.
Have the iPhone connected and unlocked, and a second phone handy.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import dbus

# ---- config --------------------------------------------------------------
# The iPhone's Bluetooth MAC. Auto-read from the installed verdigris config
# if present; otherwise edit this placeholder.
IPHONE_MAC = "AA:BB:CC:DD:EE:FF"


def _load_mac() -> str:
    env = Path.home() / ".config" / "verdigris" / "local.env"
    if env.is_file():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line.startswith("VERDIGRIS_MAC="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return IPHONE_MAC


IPHONE_MAC = _load_mac()
MAC_USCORE = IPHONE_MAC.replace(":", "_").upper()
CARD = f"bluez_card.{MAC_USCORE}"

WP_CONF = (Path.home() / ".config" / "wireplumber" / "wireplumber.conf.d"
           / "51-bluez-hfp-hf.conf")
# Note: a Phase-0 version of this file may already exist pinning the *native*
# HFP backend. oFono can only claim the HFP link if the backend is "ofono",
# so this spike rewrites the file when it finds the wrong backend.
WP_CONF_BODY = """# HFP Hands-Free role for bluez5 devices, with HFP/HSP handed to oFono
# so call control (answer/hangup/dial, caller ID) is available on D-Bus.
# Reversible: restore the .bak alongside this file + restart wireplumber.
monitor.bluez.properties = {
  "bluez5.roles"            = [ "hsp_hs", "hsp_ag", "hfp_hf", "hfp_ag", "a2dp_sink", "a2dp_source" ]
  "bluez5.codecs"           = [ "sbc", "sbc_xq", "msbc" ]
  "bluez5.enable-msbc"      = true
  "bluez5.enable-hw-volume" = true
  "bluez5.hfphsp-backend"   = "ofono"
}
"""

OFONO = "org.ofono"
RING_TIMEOUT = 60   # seconds to wait for an incoming call to show up
DIAL_TRIES = 3      # how many outgoing-call attempts to measure reliability


def hr(title: str) -> None:
    print(f"\n{'=' * 68}\n{title}\n{'=' * 68}", flush=True)


def run(cmd: list[str]) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return r.returncode, (r.stdout + r.stderr)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return 1, str(e)


def ask(prompt: str) -> str:
    try:
        return input(f"\n>>> {prompt} ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n[abort]", flush=True)
        sys.exit(1)


# ---- 0. preconditions ----------------------------------------------------

hr("05b — HFP HF role via oFono")
print(f"[+] iPhone MAC : {IPHONE_MAC}", flush=True)
print(f"[+] PipeWire card: {CARD}", flush=True)
if IPHONE_MAC == "AA:BB:CC:DD:EE:FF":
    print("[FAIL] No iPhone MAC. Edit IPHONE_MAC at the top of this script, "
          "or create ~/.config/verdigris/local.env with VERDIGRIS_MAC=.",
          flush=True)
    sys.exit(2)

bus = dbus.SystemBus()

# Is oFono installed and running?
try:
    dbus.Interface(bus.get_object(OFONO, "/"), f"{OFONO}.Manager")
except dbus.DBusException:
    print("[FAIL] org.ofono not on the system bus.\n"
          "       Install + start it:\n"
          "         sudo apt install ofono\n"
          "         sudo systemctl enable --now ofono\n"
          "       Then re-run this script.", flush=True)
    sys.exit(2)

# Is WirePlumber configured to hand HFP to oFono? A Phase-0 file may exist
# but pin the "native" backend — in that case oFono never gets the modem.
def _hfp_backend(text: str) -> str | None:
    for line in text.splitlines():
        if "hfphsp-backend" in line and "=" in line:
            return line.split("=", 1)[1].strip().strip(',').strip().strip('"')
    return None


_need_wp_restart = False
if not WP_CONF.is_file():
    WP_CONF.parent.mkdir(parents=True, exist_ok=True)
    WP_CONF.write_text(WP_CONF_BODY)
    print(f"\n[!] Wrote {WP_CONF}", flush=True)
    _need_wp_restart = True
elif _hfp_backend(WP_CONF.read_text()) != "ofono":
    backend = _hfp_backend(WP_CONF.read_text())
    backup = WP_CONF.with_suffix(f".conf.bak.{int(time.time())}")
    backup.write_text(WP_CONF.read_text())
    WP_CONF.write_text(WP_CONF_BODY)
    print(f"\n[!] Existing {WP_CONF}\n    used the {backend!r} HFP backend — "
          f"oFono can't claim the link with that.\n"
          f"[!] Backed it up to {backup} and rewrote it for the oFono backend.",
          flush=True)
    _need_wp_restart = True

if _need_wp_restart:
    print("\n[!] Apply the config and reconnect the iPhone, then re-run:\n\n"
          "      systemctl --user restart wireplumber pipewire pipewire-pulse\n"
          f"      bluetoothctl disconnect {IPHONE_MAC}\n"
          f"      bluetoothctl connect {IPHONE_MAC}\n", flush=True)
    sys.exit(0)
print(f"[+] WirePlumber HFP/oFono config present: {WP_CONF}", flush=True)


# ---- 1. find the HFP modem ----------------------------------------------

hr("Check 1 — oFono HFP modem")
manager = dbus.Interface(bus.get_object(OFONO, "/"), f"{OFONO}.Manager")


def find_modem() -> tuple[str, dict] | tuple[None, None]:
    """Return (modem_path, properties) for the iPhone's HFP modem, or (None, None)."""
    try:
        modems = manager.GetModems()
    except dbus.DBusException as e:
        print(f"    [WARN] GetModems failed: {e.get_dbus_message()}", flush=True)
        return None, None
    for path, props in modems:
        # HFP modems live under /hfp/... and carry the device MAC in the path.
        if MAC_USCORE in str(path).upper() or "hfp" in str(path).lower():
            return str(path), dict(props)
    # Fallback: any modem at all.
    if modems:
        p, props = modems[0]
        return str(p), dict(props)
    return None, None


modem_path, modem_props = find_modem()
if not modem_path:
    print("[FAIL] No oFono modem. The iPhone isn't connected, or PipeWire's\n"
          "       native HFP backend grabbed the link before oFono could.\n"
          "       Confirm: bluetoothctl info " + IPHONE_MAC + "  (Connected: yes)\n"
          "       Confirm WirePlumber restarted after the config above.\n"
          "       If oFono still never sees a modem, that's the signal to\n"
          "       fall back to the roll-our-own AT-over-RFCOMM backend.",
          flush=True)
    sys.exit(3)

print(f"[+] Modem: {modem_path}", flush=True)
ifaces = [str(i) for i in modem_props.get("Interfaces", [])]
print(f"    Powered={modem_props.get('Powered')} "
      f"Online={modem_props.get('Online')}", flush=True)
print(f"    Name={modem_props.get('Name')!r} "
      f"Type={modem_props.get('Type')!r}", flush=True)
print(f"    Interfaces: {ifaces}", flush=True)

modem = dbus.Interface(bus.get_object(OFONO, modem_path), f"{OFONO}.Modem")


def modem_prop(name: str):
    try:
        return dict(modem.GetProperties()).get(name)
    except dbus.DBusException:
        return None


# An HFP modem normally auto-powers once oFono's service-level connection is
# up. If it didn't, the usual cause is a startup-order race: PipeWire's native
# HFP backend grabbed the BlueZ HFP-profile registration before oFono could.
# Power before Online — Online is rejected ('not available') while unpowered.
if not modem_prop("Powered"):
    try:
        modem.SetProperty("Powered", dbus.Boolean(True))
        time.sleep(3)
    except dbus.DBusException as e:
        print(f"    [WARN] SetProperty(Powered): {e.get_dbus_message()}",
              flush=True)
if modem_prop("Powered") and not modem_prop("Online"):
    try:
        modem.SetProperty("Online", dbus.Boolean(True))
        time.sleep(2)
    except dbus.DBusException as e:
        print(f"    [WARN] SetProperty(Online): {e.get_dbus_message()}",
              flush=True)

# Interfaces lag the modem by a beat — poll for VoiceCallManager.
ifaces = []
for _ in range(10):
    ifaces = [str(i) for i in (modem_prop("Interfaces") or [])]
    if f"{OFONO}.VoiceCallManager" in ifaces:
        break
    time.sleep(1)

if not modem_prop("Powered") or f"{OFONO}.VoiceCallManager" not in ifaces:
    print(f"[FAIL] HFP modem never came up — Powered={modem_prop('Powered')} "
          f"Interfaces={ifaces}\n"
          "       Most likely oFono lost the HFP-profile registration race\n"
          "       against PipeWire's native backend. Fix the start order:\n\n"
          "         systemctl --user restart wireplumber   # PipeWire releases HFP\n"
          "         sudo systemctl restart ofono           # oFono claims it\n"
          f"         bluetoothctl disconnect {IPHONE_MAC}\n"
          f"         bluetoothctl connect {IPHONE_MAC}\n\n"
          "       Confirm with: journalctl -u ofono   (must NOT show\n"
          "       'RegisterProfile ... UUID already registered').", flush=True)
    sys.exit(3)
print(f"[+] Modem Powered={modem_prop('Powered')} Online={modem_prop('Online')}",
      flush=True)
print(f"[+] Interfaces: {ifaces}", flush=True)
print("[VERDICT 1] PASS — oFono exposes an HFP modem with call control.",
      flush=True)

vcm = dbus.Interface(bus.get_object(OFONO, modem_path),
                     f"{OFONO}.VoiceCallManager")


def get_calls() -> list[tuple[str, dict]]:
    try:
        return [(str(p), dict(props)) for p, props in vcm.GetCalls()]
    except dbus.DBusException:
        return []


def wait_for_call(timeout: int, state_filter: tuple[str, ...] | None = None
                  ) -> tuple[str, dict] | tuple[None, None]:
    """Poll GetCalls() until a call (optionally in one of state_filter) appears."""
    deadline = time.time() + timeout
    last = -1
    while time.time() < deadline:
        for path, props in get_calls():
            if state_filter is None or str(props.get("State")) in state_filter:
                return path, props
        left = int(deadline - time.time())
        if left != last and left % 5 == 0:
            print(f"    ... waiting ({left}s left)", flush=True)
            last = left
        time.sleep(1)
    return None, None


def call_props(call_path: str) -> dict:
    try:
        ci = dbus.Interface(bus.get_object(OFONO, call_path),
                            f"{OFONO}.VoiceCall")
        return dict(ci.GetProperties())
    except dbus.DBusException:
        return {}


# ---- 2. incoming call + caller ID ---------------------------------------

hr("Check 2 — incoming call + caller ID")
ask(f"Call the iPhone ({IPHONE_MAC}) from another phone, then press Enter.")
in_path, in_props = wait_for_call(RING_TIMEOUT,
                                  state_filter=("incoming", "waiting"))
if not in_path:
    print("[FAIL] No incoming call seen within "
          f"{RING_TIMEOUT}s. oFono didn't surface the ring.", flush=True)
    sys.exit(4)

in_props = call_props(in_path) or in_props
clip = in_props.get("LineIdentification")
name = in_props.get("Name")
print(f"[+] Incoming call object: {in_path}", flush=True)
print(f"    State              = {in_props.get('State')}", flush=True)
print(f"    LineIdentification = {clip!r}   (caller ID)", flush=True)
print(f"    Name               = {name!r}", flush=True)
clip_ok = bool(clip) and str(clip) not in ("", "withheld")
print(f"[VERDICT 2] {'PASS' if clip_ok else 'PARTIAL'} — incoming call seen; "
      f"caller ID {'present' if clip_ok else 'MISSING/withheld'}.", flush=True)


# ---- 3. answer + SCO audio ----------------------------------------------

hr("Check 3 — answer + SCO audio")
ask("Press Enter to ANSWER the call on the laptop.")
in_call = dbus.Interface(bus.get_object(OFONO, in_path), f"{OFONO}.VoiceCall")
try:
    in_call.Answer()
except dbus.DBusException as e:
    print(f"[FAIL] Answer() failed: {e.get_dbus_message()}", flush=True)
    sys.exit(5)

# Wait for the call to go active.
active_path, _ = wait_for_call(15, state_filter=("active",))
print(f"[+] Call state after Answer: "
      f"{call_props(in_path).get('State')}", flush=True)

# Did a PipeWire HFP audio path come up?
time.sleep(2)
rc, cards = run(["pactl", "list", "cards"])
audio_ok = False
codec = "unknown"
if CARD in cards:
    in_section = False
    for ln in cards.splitlines():
        s = ln.strip()
        if f"Name: {CARD}" in ln:
            in_section = True
        if in_section and s.startswith("Active Profile:"):
            prof = s.split(":", 1)[1].strip()
            print(f"    iPhone card Active Profile: {prof}", flush=True)
            audio_ok = "head-unit" in prof or "handsfree" in prof or "hf" in prof
        if in_section and "bluez5.codec" in s:
            codec = s.split("=")[-1].strip().strip('"')
# Codec also shows up in pw-dump node properties.
rc, dump = run(["pw-dump"])
if rc == 0:
    for ln in dump.splitlines():
        if "api.bluez5.codec" in ln:
            codec = ln.split(":", 1)[-1].strip().strip(',').strip('"')
            break
print(f"    Negotiated codec: {codec}", flush=True)
print("    (Talk into the laptop mic / listen on its speakers to confirm "
      "two-way audio.)", flush=True)
heard = ask("Did call audio route through the laptop? [y/N]").lower()
audio_ok = audio_ok or heard.startswith("y")
print(f"[VERDICT 3] {'PASS' if audio_ok else 'FAIL'} — SCO audio "
      f"{'routed to the laptop' if audio_ok else 'did NOT route'}.", flush=True)


# ---- 4. hang up ----------------------------------------------------------

hr("Check 4 — hang up")
ask("Press Enter to HANG UP from the laptop.")
hangup_ok = True
try:
    in_call.Hangup()
    time.sleep(2)
    still = [p for p, _ in get_calls() if p == in_path]
    hangup_ok = not still
except dbus.DBusException as e:
    print(f"    [WARN] Hangup(): {e.get_dbus_message()}", flush=True)
    hangup_ok = False
    try:
        vcm.HangupAll()
    except dbus.DBusException:
        pass
print(f"[VERDICT 4] {'PASS' if hangup_ok else 'FAIL'} — call "
      f"{'ended cleanly' if hangup_ok else 'did NOT end cleanly'}.", flush=True)


# ---- 5. outgoing dial reliability ---------------------------------------

hr("Check 5 — outgoing Dial reliability")
print("This tests the BACKLOG 'Won't do' assumption: 'HFP HF can't reliably\n"
      "ATD on iPhone'. We dial a real number a few times and measure.",
      flush=True)
number = ask("Enter a phone number to test-dial (e.g. another phone you hold):")
if not number:
    print("[SKIP] No number given — outgoing test skipped.", flush=True)
    dial_ok = dial_tries = 0
else:
    dial_ok = 0
    for i in range(1, DIAL_TRIES + 1):
        print(f"\n--- Dial attempt {i}/{DIAL_TRIES} ---", flush=True)
        try:
            vcm.Dial(number, "")
        except dbus.DBusException as e:
            print(f"    Dial() raised: {e.get_dbus_message()}", flush=True)
            continue
        out_path, out_props = wait_for_call(
            15, state_filter=("dialing", "alerting", "active"))
        if out_path:
            print(f"    OK — call object {out_path} "
                  f"state={call_props(out_path).get('State')}", flush=True)
            rang = ask("    Did the target phone actually ring? [y/N]").lower()
            if rang.startswith("y"):
                dial_ok += 1
        else:
            print("    No outgoing call object appeared.", flush=True)
        try:
            vcm.HangupAll()
        except dbus.DBusException:
            pass
        time.sleep(3)
    dial_tries = DIAL_TRIES
    print(f"\n[VERDICT 5] Outgoing dial succeeded {dial_ok}/{dial_tries} times.",
          flush=True)


# ---- summary -------------------------------------------------------------

hr("SUMMARY")
print(f"  1. oFono HFP modem + call control : PASS", flush=True)
print(f"  2. Incoming call + caller ID      : "
      f"{'PASS' if clip_ok else 'PARTIAL (no caller ID)'}", flush=True)
print(f"  3. Answer + SCO audio to laptop   : "
      f"{'PASS' if audio_ok else 'FAIL'}", flush=True)
print(f"  4. Hangup                         : "
      f"{'PASS' if hangup_ok else 'FAIL'}", flush=True)
if number:
    print(f"  5. Outgoing dial reliability      : {dial_ok}/{dial_tries}",
          flush=True)
else:
    print(f"  5. Outgoing dial reliability      : SKIPPED", flush=True)
print(f"     Negotiated codec               : {codec}", flush=True)

print("""
[DECISION GATE — record this in spike/RESULTS.md]
  • Checks 1–4 PASS         → integrate HFP via the oFono D-Bus client (plan A1).
  • oFono never saw a modem → fall back to roll-our-own AT-over-RFCOMM (A1-alt).
  • Outgoing 0/N            → ship incoming-only; mark 'place a call' best-effort
                              and correct the stale BACKLOG.md 'Won't do' note.
""", flush=True)
sys.exit(0)
