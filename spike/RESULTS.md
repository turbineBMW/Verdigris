# Historical Blue protocol experiments

These observations were inherited by Verdigris from Blue. Names, paths, and example
messages below describe the original experiments, not the current app.

# Phase 0 — Results & Plan Revisions

**Date:** 2026-05-19  •  **Target:** iPhone 16 Pro Max, iOS 26.5, MAC `AA:BB:CC:DD:EE:FF`
**Linux:** Pop!_OS 24.04, BlueZ 5.72, PipeWire 1.5.85, WirePlumber 1.5.85
**Plan reference:** `~/.claude/plans/steady-crunching-lynx.md`

## Executive summary

**The project is viable.** Three of four Bluetooth profiles are proven working against the actual target device; the fourth has a clear path forward. The original plan's 6–9-month "I use this daily" estimate is not invalidated — it's confirmed within reasonable bounds.

The single most valuable Phase 0 finding is **the hidden-toggle dance**: iOS 26.5 *does* surface per-device permission toggles for MAP, PBAP, and (presumably) ANCS — but **only after the paired adapter satisfies specific identity conditions** (CoD = A/V Hands-Free, BLE peripheral advert with `SolicitUUIDs=ANCS` active). Without that dance, the toggles are hidden and the corresponding OBEX servers refuse access with `0x43 Forbidden`. The earlier-search-result claim that "Apple removed the toggle around iOS 16.5" was misleading — Apple didn't remove it, they made it conditional on what the accessory advertises.

## Profile scoreboard

| # | Profile | Status | Toggle (iOS) | Evidence |
|---|---|---|---|---|
| 02 | **MAP read** | ✅ PASS | "Show Message Notifications" | Pulled 10 SMS handles + metadata (sender, body in `Subject`, timestamp, read state) |
| 03 | **MAP MNS** | ✅ PASS | (covered by Message Notifications) | 5 real-time `InterfacesAdded` push events fired within seconds of incoming SMS |
| 04 | **PBAP** | ✅ PASS | "Sync Contacts" | Pulled 1291 vCards in one 2.7 MB transfer |
| 05 | **HFP HF role** | ⚠ PARTIAL | — (Linux-side, not iOS) | PipeWire sees iPhone BT card. Only "audio-gateway" profile exposed; WirePlumber config to enable HF role didn't take effect with the obvious syntax. Needs Phase 2c work. |
| 01 | **ANCS** | ⚠ DEFER | (presumed "Show Notifications") | BLE advert with `SolicitUUIDs=ANCS` registered cleanly; iPhone did not BLE-attach. Hypothesis: BR/EDR-paired devices won't auto-BLE-attach to solicit adverts. Verification needs unpair-and-BLE-rebind, which would lose MAP/PBAP toggles. Push to Phase 1. |
| 05b | **HFP HF role** (oFono) | ✅ PASS | — (Linux-side) | See the 2026-05-20 addendum below — oFono call control, caller ID, SCO audio, and 3/3 outgoing dials all confirmed. |

## Addendum — 2026-05-20: HFP HF role confirmed (spike 05b)

Phase 2c revisited the HFP Hands-Free role with `spike/05b_hfp_ofono.py`,
this time testing call **control** — not just whether PipeWire sees the audio
card. **Every check passed against iPhone 16 Pro Max / iOS 26.5.**

| Check | Result |
|---|---|
| oFono exposes an HFP modem with `VoiceCallManager` | ✅ PASS |
| Incoming call → `CallAdded` with caller ID (`LineIdentification`) | ✅ PASS (`+1407…`) |
| `Answer()` → SCO audio routes to the laptop | ✅ PASS |
| `Hangup()` ends the call cleanly | ✅ PASS |
| Outgoing `Dial()` actually rings the target | ✅ **3/3** |
| Negotiated codec | mSBC (wideband) |

Key findings:

- **The architecture is oFono + PipeWire.** oFono (`org.ofono`, system bus)
  owns the HFP protocol and exposes call control on D-Bus; PipeWire's oFono
  HFP backend carries the SCO call audio. WirePlumber needs one config key —
  `bluez5.hfphsp-backend = "ofono"` — which `iphonebridge hfp-enable` writes.

- **Startup ordering matters.** oFono and PipeWire's *native* HFP backend
  both try to register the HFP profile with BlueZ; whoever loses logs
  `RegisterProfile … UUID already registered` and never gets a working modem.
  Fix: bring WirePlumber up on the oFono backend first, *then* (re)start oFono.

- **The stale "Won't do" assumption was wrong.** `BACKLOG.md` had claimed
  "HFP HF role can't reliably ATD on iPhone." Outgoing dial succeeded 3/3 —
  placing calls from the laptop works fine. The note has been corrected.

This upgrades the row-05 verdict from ⚠ PARTIAL to ✅ PASS and unblocked the
HFP integration shipped in `src/iphonebridge/hfp/`.

## Key non-obvious discoveries

### 1. The hidden-toggle dance (the most important finding)

For iOS to surface `Show Message Notifications` and `Sync Contacts` toggles on the iPhone's `Settings → Bluetooth → (i)` info screen for a paired adapter, the adapter must satisfy **all** of:

- **Class-of-Device = A/V Hands-Free** (`0x240408` or with extra service-class bits like `0x007c0408`). Set via `sudo btmgmt class 4 8` (Major=4, Minor=8 — bits 7-2 = `0x02` = Hands-Free).
- **An active BLE advertisement registered via `org.bluez.LEAdvertisingManager1`** with `SolicitUUIDs` including the ANCS UUID `7905F431-B5CE-4E99-A40F-4B1E122D00D0` and type `peripheral`.
- (Probably) **Adapter discoverable and pairable at the time of pairing.**

When all conditions are met, the toggles appear within seconds and the OBEX servers (MAP/PBAP) become reachable. The toggles are **per-profile**: enabling Message Notifications does not enable Contacts and vice versa.

We never got a `Show Notifications` toggle (the presumed ANCS gate) to surface despite the same conditions. That's the open ANCS question for Phase 1.

### 2. Single-OBEX-session per fresh obexd

iPhone refuses repeat MAP/PBAP `OBEX Connect` requests within a short window after the first session is torn down — returns `org.bluez.obex.Error.Failed: Forbidden`. The fix that always works: `systemctl --user restart obex.service` to get a fresh daemon, then connect once.

**Production-app implication:** the app must keep one long-lived MAP session (and one PBAP session) for its lifetime. Don't create/destroy sessions per query.

### 3. iOS MAP puts the SMS body in `Subject`

Counterintuitive but consistent: each message's `org.bluez.obex.Message1` properties expose the SMS body in the `Subject` field, not in a separate body field. Pull `Subject`, `Sender`, `Timestamp`, `Read`, `Type` and you have a complete event. (`Get()` downloads a full bMessage if needed, but `Subject` already has the body for our purposes.)

### 4. PBAP uses `Select(location, phonebook)` — *not* `SetFolder`

A small API divergence from MAP. `Select("int", "pb")` for the main phonebook. Documented in BlueZ but easy to miss.

### 5. The BR/EDR-vs-BLE pairing mutex for ANCS (CONFIRMED 2026-05-19)

iPhone has a single Bluetooth controller with one MAC for both BR/EDR and BLE. Our pairing on Pop!_OS is BR/EDR (AddressType `public`). iPhone exposes MAP/PBAP/HFP-AG over BR/EDR but **not** ANCS — which lives on BLE only.

**Path 1 (dual-pair via BLE solicit advert during fresh pair) — DOES NOT WORK.**

`spike/06_dualpair_test.py` ran the empirical test on 2026-05-19:
- Forgot iPhone pair on both sides
- Restarted bluetoothd (clean adapter state)
- Set CoD = A/V Hands-Free
- Registered BLE peripheral advert with `SolicitUUIDs=[ANCS UUID]` BEFORE the iPhone could see us
- Made adapter discoverable + pairable
- iPhone tapped pop-os under Other Devices, pair completed
- Result: `Paired=True Connected=True ANCS=✗ MAP=✓ PBAP=✓ (14 UUIDs)`

iOS treated us as a single device and bonded over BR/EDR only. The BLE advert with ANCS solicit was ignored. **Same MAC = same device to iOS**, and when both modes are offered, BR/EDR wins.

**Implications for Phase 2a (ANCS for per-app notifications):**

- **Path 2 (second BT adapter)** is the realistic route. A second adapter (USB BT dongle, ~$10) has a different MAC. iOS will treat it as a distinct accessory. Pair it BLE-only with ANCS; keep the built-in adapter BR/EDR-paired for MAP/PBAP. Daemon would manage both adapters.
- **Path 3 (accept the tradeoff)** — keep MAP-led notifications, skip ANCS. SMS-only desktop mirror. We already have this; the cost of giving up is losing only third-party app notification *titles* (no bodies since iMessage isn't on MAP anyway).

### 6. iMessage IS exposed via MAP on iOS 26.5 (DISCOVERED 2026-05-19, post-launch)

**This contradicts every other writeup in existence** (Apple Developer Forums, ancs4linux docs, BT-MAP spec discussions, the entire pre-2026 Bluetooth-on-Linux community). On iOS 26.5 / iPhone 16 Pro Max, the MAP server exposes **both SMS and iMessage**, labeled identically as `Type: sms-gsm`. iOS does not distinguish at the MAP protocol level.

**How we discovered it.** After the daemon's first end-to-end success showed a "Contact A: Kk" notification, the user pointed out that his text thread with Contact A is actually iMessage (blue bubbles). To rule out an SMS fallback, a deliberate test was run: have Contact B (confirmed iMessage thread, both on iPhone) send "test-iphonebridge-XYZ123". The message arrived through MNS within ~2 s, body intact:

```
[19:27:53] new Message1 at message637055617829954636 (Status=notification Type=sms-gsm Size=0) — fetching body
[19:27:53] sms_received from Contact B: 'test-iphonebridge-XYZ123'
```

the user confirmed: blue bubble, iMessage thread, sender on iPhone with iMessage active. No fallback.

**Implications:**

- **The "iMessage requires a Mac relay" assumption was wrong, at least for read on iOS 26.5.** iphonebridge already mirrors iMessage in real time — same code path as SMS.
- **`Type: sms-gsm` is a misleading label** — iOS uses it for ALL incoming messages over MAP regardless of underlying transport. Don't use it to distinguish SMS vs iMessage; you can't, from MAP alone.
- **Outgoing iMessage via MAP `PushMessage` ALSO works** (DISCOVERED 2026-05-19, same session). `spike/07_map_send.py` constructed a minimal bMessage (originator + BENV-wrapped recipient VCARD, TYPE=SMS_GSM, FOLDER=telecom/msg/outbox), called `MessageAccess1.PushMessage` with `folder="telecom/msg/outbox"` and empty filter args. Transfer completed (`status: gone`). On the iPhone the outgoing bubble appeared in the recipient thread as **blue** — confirming iOS routed it as iMessage. iphonebridge is now potentially the first free open-source Linux iMessage bridge (read + send) that does not require a Mac relay.
- **Older iOS versions probably behave differently.** This may be specific to iOS 26.x or a very recent change. Worth checking iOS 18/19 if we ever get hands on test devices.

**Things still NOT exposed:** group iMessage / RCS, threading metadata, read receipts, typing indicators, full attachments. Just message text + sender + timestamp. That's still the killer feature for a daily-driver notification mirror.

## Plan impact

| Plan assertion | Phase 0 verdict |
|---|---|
| MAP read works on iOS 26.5 | ✅ Confirmed (with toggle dance) |
| MAP MNS push works | ✅ Confirmed |
| PBAP works | ✅ Confirmed (with toggle dance) |
| HFP HF role needs config + maybe oFono | ✅ Partially confirmed — config tuning is more opaque than expected; Phase 2c retains its 5–7 weekend budget |
| ANCS works (foundation of project) | ⚠ Needs Phase 1 verification via BLE-pairing approach. **This is the most material open question.** |
| Toggle gate hypothesis (early-evening pessimism) | ❌ Overturned — toggles exist and respond to specific accessory identity |
| "iOS 16.5 removed the toggle" web claim | ❌ Misleading — toggle is conditional, not removed |

**Revised Phase 1 scope (if ANCS turns out to require disruptive re-pair):**
ANCS + MAP/PBAP coexistence may not be possible. In that case Phase 1 should default to **MAP-led notification mirror** (SMS-only, but with conversation history, contacts resolution, and real-time push) rather than ANCS-led (every-app, no SMS bodies). The MAP-led path is the higher-utility one for general daily use.

**Original 6–9 month estimate:** still valid. No findings invalidate the schedule.

## Things to encode in the production app

These should appear in the daemon's startup sequence:

1. **Set adapter CoD to A/V Hands-Free at startup** — `btmgmt class 4 8` or its DBus equivalent. Persist via systemd drop-in or by inheriting the manual main.conf change.
2. **Register a long-lived BLE advert with `SolicitUUIDs=ANCS`** even if we don't end up using ANCS — it appears to be load-bearing for the OBEX toggles too. Register at startup via `org.bluez.LEAdvertisingManager1.RegisterAdvertisement`. Don't unregister until shutdown.
3. **Keep one MAP session and one PBAP session open for the daemon's lifetime.** Reopen only after BlueZ obex restart or BT reset.
4. **Document the user-side iPhone setup:** during first-run wizard, the user must enable "Show Message Notifications" and "Sync Contacts" on the iPhone for the pop-os device. Without those toggles enabled, MAP and PBAP return `0x43 Forbidden` even though they're protocol-reachable.
5. **Body is in `Subject`** — the parser should pull SMS text from `Message1.Subject`, not from a downloaded bMessage body, for performance.

## State left on the host

| Item | What | Reversible? |
|---|---|---|
| `bluez-obexd` 5.72-0ubuntu5.5 | apt-installed | Yes — `sudo apt remove bluez-obexd` |
| `/etc/bluetooth/main.conf` | Added `Class = 0x240408` to `[General]`. Note: ignored by BlueZ in practice (BlueZ derives Class itself), but harmless. | Yes — rollback files at `/etc/bluetooth/main.conf.bak.1779226729` |
| Adapter Class (runtime) | Set to `0x007c0408` (AV/Hands-Free) via `sudo btmgmt class 4 8`. Survives until `bluetooth.service` restart. | Yes — `sudo btmgmt class 1 4` to restore Computer/Desktop |
| `~/.config/wireplumber/wireplumber.conf.d/51-bluez-hfp-hf.conf` | Created (did not take effect, but harmless) | Yes — delete + `systemctl --user restart wireplumber` |
| Orphan LE advertisement | `ActiveInstances=1` for `/iphonebridge/ancs_advert` (the Python process exited before unregister fired). Will clear on `sudo systemctl restart bluetooth`. | Yes — service restart |
| iPhone-side pair | Still paired, bonded, trusted, connected. Toggles "Show Message Notifications" and "Sync Contacts" enabled. | User can forget device on iPhone |

**To fully reset after Phase 0:**
1. Delete `~/.config/wireplumber/wireplumber.conf.d/51-bluez-hfp-hf.conf` (config didn't help)
2. `sudo cp /etc/bluetooth/main.conf.bak.1779226729 /etc/bluetooth/main.conf` (optional — the Class line is harmless)
3. `sudo systemctl restart bluetooth.service` (clears orphan advert, resets CoD)

But there's no reason to reset — the current state is a usable platform for Phase 1.

## Open questions for next session

1. **ANCS via BLE pair**: try unpair → BLE-only pair flow. Risk: lose MAP/PBAP toggles. Mitigation: re-pair BR/EDR after, see if toggles re-surface.
2. **Dual-mode pair**: does iOS allow the same Linux MAC to be bonded both BR/EDR and BLE simultaneously? Untested.
3. **HFP HF role**: which WirePlumber 1.5 config key actually enables `bluez5.roles=[hfp_hf,...]`. Possibly needs oFono backend. Phase 2c work, deferred.
4. **`btmgmt class` persistence**: figure out the cleanest way to make the CoD setting survive reboots and `bluetooth.service` restarts. Options: systemd ExecStartPost, or correct main.conf incantation, or a small udev rule.
5. **iOS 26.5 toggle naming**: confirm whether enabling "Sync Contacts" was specifically what unlocked PBAP, or if it was the cumulative state. (Likely the former, given the per-profile pattern.)

## Files added by Phase 0

```
/home/gabrielmeir53/code/iphonebridge/spike/
├── 00_install.sh                  # apt install bluez-obexd
├── 01_ancs_subscribe.py           # BLE advert + ANCS subscribe — PARTIAL, needs BLE pair
├── 01a_cod_repair.sh              # CoD change + re-pair helper (mostly superseded by btmgmt)
├── 02_obex_map_session.py         # MAP read — PASS
├── 03_obex_map_notify.py          # MAP MNS push — PASS
├── 04_obex_pbap.py                # PBAP pull — PASS (1291 contacts)
├── 05_hfp_audio.py                # HFP HF role check — PARTIAL
├── README.md                      # Phase 0 overview
├── RESULTS.md                     # This file
└── results/
    ├── 00_install.log
    ├── 01a_cod_repair.log
    ├── 02_obex_map_session.log
    ├── 03_obex_map_notify.log
    ├── 04_obex_pbap.log
    └── 05_hfp_audio.log
```

## Go/no-go for Phase 1

**GO.** All four protocols are either confirmed working or have clear paths forward. Phase 1 should default to the **MAP-led "notification + SMS mirror"** scope — it's already proven and gives the daily-driver value. ANCS verification can run as a Phase 1 side experiment without blocking the main build.
