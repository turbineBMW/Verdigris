# Phase 0 — Feasibility Spike

Throwaway scripts that prove each of the four Bluetooth profiles works against **the user's iPhone 16 Pro Max (iOS 26.5)** during the original Blue project. Verdigris is a fork of [Blue](https://github.com/gutbash/blue) and keeps these scripts as protocol references.

Plan reference: `/home/gabrielmeir53/.claude/plans/steady-crunching-lynx.md`

## Target device

| Field | Value |
|---|---|
| Device | the user's iPhone (iPhone 16 Pro Max) |
| iOS | 26.5 |
| BT MAC | `AA:BB:CC:DD:EE:FF` |
| Pairing | Already paired + connected + trusted on `pop-os` adapter |

## Scripts

| # | Script | Profile | Go signal | No-go consequence |
|---|---|---|---|---|
| 00 | `00_install.sh` | (setup) | `org.bluez.obex` activates | Stop — fix obexd before continuing |
| 01 | `01_ancs_subscribe.py` | ANCS / BLE | ≥1 real notification (title+body) prints in 10 min | **Stop the project** — ANCS is the foundation |
| 02 | `02_obex_map_session.py` | MAP / OBEX | ≥1 SMS body parses out of `GetMessage` | Phase 1 shrinks to ANCS-only mirror |
| 03 | `03_obex_map_notify.py` | MAP MNS | `MessageReceived` signal within 2s of self-text | Same as 02 |
| 04 | `04_obex_pbap.py` | PBAP / OBEX | `pb.vcf` ≥ 50 contacts parse | Fall back to manual contacts CSV |
| 05 | `05_hfp_audio.py` | HFP HF role | SCO link up + audio routable | Drop Phase 2c (call answer) |
| 05b | `05b_hfp_ofono.py` | HFP HF role (oFono) | oFono call control + caller ID + SCO audio; outgoing dial reliability measured | Pick a fallback backend per the in-script decision gate |

> `05b` is a later, interactive guided test (places real calls) for the
> Phase A HFP feature. Prereq: `sudo apt install ofono && sudo systemctl
> enable --now ofono`. It writes a WirePlumber config for the oFono HFP
> backend on first run, then exits asking for a restart + reconnect.

## iPhone-side prerequisites

Before running scripts 02–04, on the iPhone:

1. **Settings → Bluetooth** → tap the `(i)` next to **pop-os**
2. Toggle ON: **Show Notifications** (enables ANCS pushes)
3. Toggle ON: **Share System Notifications** (enables MAP/PBAP categories of access)
4. If a "trust this computer" prompt has not been answered yet, do it now

These toggles are per-device and not on by default. Apple's MAP server will silently refuse access if Share System Notifications is off.

## Result format

Each script writes to `results/<NN>_<name>.log` (raw output) and updates a
single line in `results/SUMMARY.md` with `PASS` / `FAIL` / `PARTIAL` + the
key observation.

## Tearing down

After Phase 0, if we proceed to Phase 1, the spike directory stays in git as a
historical reference. These are diagnostic scripts, not part of the native app installation.
`bluez-obexd` is left installed — it's harmless and tiny.
