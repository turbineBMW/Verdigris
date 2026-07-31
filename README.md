<div align="center">

<img src="data/icons/com.gabriel.iphonebridge.UI.svg" alt="Blue" width="128" height="128">

# Blue

**Your iPhone’s messages, calls, notifications, and contacts — on your Linux desktop.**

[![CI](https://github.com/gutbash/blue/actions/workflows/ci.yml/badge.svg)](https://github.com/gutbash/blue/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/gutbash/blue?color=brightgreen)](https://github.com/gutbash/blue/releases)
[![License: GPL v2](https://img.shields.io/badge/license-GPL--2.0-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org)
[![Platform: Linux](https://img.shields.io/badge/platform-Linux-lightgrey.svg)](#requirements)

*No Mac relay. No cloud service. No subscription.*

</div>

---

Windows has Phone Link. Linux never got an equivalent — KDE Connect wants an app and Wi‑Fi, `ancs4linux` is notifications only, Mac-relay bridges need a real Mac, and Beeper isn’t free.

**Blue** is that missing piece: a Python daemon that pairs with your iPhone over standard Bluetooth profiles, plus an optional direct Apple iMessage transport for full-fidelity blue bubbles. On top of it sits a Qt desktop app and a full CLI. Internally the project still uses the package and command names `iphonebridge` / `iphonebridge-qt`.

## What it does

| Feature | How | Status |
|---|---|---|
| **Incoming SMS + iMessage** as desktop notifications | MAP MNS push | ✅ |
| **Send SMS + iMessage** from the CLI or app | MAP `PushMessage` | ✅ |
| **Full iMessage** — attachments, replies, tapbacks, edits, typing, receipts | Direct Apple transport (`ib-imessage`) | ✅ optional |
| **Verification codes** auto-copied to the clipboard | OTP detection | ✅ |
| **Contact names & photos** (thousands of contacts) | PBAP → SQLite cache | ✅ |
| **Every app’s notifications** — Slack, WhatsApp, Mail… | ANCS over BLE | ✅ |
| **Take & place phone calls** — caller ID, answer/decline, dial | HFP via oFono | ✅ |
| **Read-state sync** — read on either device | MAP + iMessage paths | ✅ |
| **Message history** — live + backup, searchable | SQLite + FTS5 | ✅ |
| **Desktop app** — conversations, notification feed, dialer | Qt / QML | ✅ |
| **Import full history** from a local iOS backup | `backup-sync` | ✅ |
| Runs unattended as a **systemd user service** | — | ✅ |

### The iMessage surprise

Every prior writeup of Bluetooth on iOS says **iMessage is invisible** to a paired computer — that you *must* use a Mac relay for blue-bubble messages.

**That is not true on recent iOS.** Blue receives *and sends* iMessage through the standard MAP Bluetooth profile, with no Mac and no Apple ID login on the Linux side. iOS labels iMessage and SMS the same over MAP (`Type: sms-gsm`) and exposes both. Outgoing messages route as iMessage when the recipient can take them.

MAP alone is plain text. For guids, media, reply threads, any-emoji tapbacks, edits, typing indicators, and delivery/read receipts, enable the optional **direct iMessage helper** (`ib-imessage`) — it talks to Apple’s APNs/IDS stack (OpenBubbles-style registration), not to the phone over Bluetooth. Empirical notes live in [`spike/RESULTS.md`](spike/RESULTS.md).

## Requirements

| | Minimum | Tested with |
|---|---|---|
| **OS** | Linux + BlueZ 5.72+ | Pop!_OS 24.04 |
| **Desktop** | GNOME or KDE Plasma (libnotify; Qt app) | — |
| **Bluetooth adapter** | Intel chipset (for ANCS) | Intel AX-series |
| **Python** | 3.10+ | 3.12 / 3.14 |
| **iPhone** | iOS 16.5+ | iPhone 16 Pro Max, iOS 26.5 |
| **System packages** | `bluez`, `bluez-obexd`, `python3-dbus`, `python3-gi` (+ `ofono` for calls, `wl-clipboard` for code auto-copy) | — |

> **Adapter chipset matters for ANCS.** Per-app notifications need a real BLE bond. Intel adapters do this reliably. **Realtek adapters and USB dongles tested so far do not** — their firmware blocks the cross-transport keys iOS needs. SMS/iMessage/contacts (MAP/PBAP) work on any adapter; only ANCS is picky. See [bmh129/ancs4linux’s hardware notes](https://github.com/bmh129/ancs4linux).

## Installation

### 1 · System packages

```bash
sudo apt install bluez bluez-obexd python3-dbus python3-gi python3-venv
# For auto-copying verification codes (Wayland):
sudo apt install wl-clipboard
```

### 2 · Clone & install

```bash
git clone https://github.com/gutbash/blue.git
cd blue

# Inherit system PyGObject + dbus-python (do not install those from PyPI).
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -e ".[qt]"

# Put the CLI and desktop app on your PATH
mkdir -p ~/.local/bin
ln -sf "$(pwd)/.venv/bin/iphonebridge" ~/.local/bin/iphonebridge
ln -sf "$(pwd)/.venv/bin/iphonebridge-qt" ~/.local/bin/iphonebridge-qt
```

### 3 · Pair your iPhone

Pair normally — GNOME **Settings → Bluetooth**, or `bluetoothctl`. Then:

```bash
iphonebridge pair-setup
```

Finds your iPhone among paired devices, writes `~/.config/iphonebridge/local.env`, and prints the iPhone-side steps.

### 4 · Install the daemon as a service

```bash
mkdir -p ~/.config/systemd/user
cp systemd/iphonebridge.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now iphonebridge
```

### 5 · iPhone-side toggles

On the iPhone: **Settings → Bluetooth → ⓘ next to your computer →** enable

- **Show Message Notifications** — SMS/iMessage (MAP)
- **Sync Contacts** — contacts (PBAP)
- **Show System Notifications** — per-app notifications (ANCS)

> These toggles appear once the daemon has run at least once (it sets the adapter class and advertises correctly). If they’re missing, re-run `iphonebridge pair-setup` and restart the daemon.

<details>
<summary><b>6 · (Optional) Per-app notifications — ANCS</b></summary>

ANCS needs a true BLE bond, formed during a fresh pairing with the adapter configured correctly:

```bash
sudo bash systemd/install-ancs-sudoers.sh
iphonebridge ancs-enable
```

Then **forget + re-pair** the iPhone (the wizard walks you through it). After that, iOS performs cross-transport key derivation and ANCS starts flowing. Once only.

</details>

<details>
<summary><b>7 · (Optional) Phone calls — HFP</b></summary>

Calls use **oFono** for HFP control and PipeWire’s oFono backend for audio:

```bash
sudo apt install ofono
sudo systemctl enable --now ofono
iphonebridge hfp-enable
```

`hfp-enable` writes WirePlumber config and restarts WirePlumber. Follow its printed steps — restart oFono **after** WirePlumber so it can claim HFP, reconnect the iPhone, restart the daemon. Incoming calls show **Answer / Decline**. Place calls with `iphonebridge call`.

</details>

<details>
<summary><b>(Optional) Persist the Bluetooth class across reboots</b></summary>

```bash
sudo bash systemd/install-cod-sudoers.sh
```

Lets the daemon set the adapter’s Class-of-Device on every start without a password prompt.

</details>

<details>
<summary><b>8 · (Optional) Full iMessage — attachments, replies, tapbacks, receipts</b></summary>

Bluetooth MAP only carries plain text. For guids, media, reply threading, any-emoji tapbacks, edits, typing, and delivery/read receipts, run the direct Apple transport (`ib-imessage`). It reuses an OpenBubbles-style registration and talks to APNs/IDS — not to the phone over Bluetooth.

```bash
cd rust/ib-imessage && cargo build --release
cp target/release/ib-imessage ~/.local/bin/.ib-imessage.new \
  && mv -f ~/.local/bin/.ib-imessage.new ~/.local/bin/ib-imessage

# Import registration from OpenBubbles (once), then enable the user unit.
# See comments in systemd/iphonebridge-imessage.service for import flags.
mkdir -p ~/.config/systemd/user
cp systemd/iphonebridge-imessage.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now iphonebridge-imessage
```

**Important:** Apple allows **one connection per push token**. Stop `iphonebridge-imessage` before opening OpenBubbles to renew registration, re-import, then start the unit again. Details are in the unit file comments and [`AGENTS.md`](AGENTS.md).

</details>

<details>
<summary><b>9 · (Optional) Import full history from an iOS backup</b></summary>

MAP never shows messages you typed on the phone, and it has no attachments. A local USB (or Wi‑Fi) backup has both:

```bash
iphonebridge backup-sync                # fresh backup + import into messages.sqlite
iphonebridge backup-sync --skip-backup  # re-read the last backup on disk
iphonebridge backup-wifi                # allow later backups without a cable
```

Open (or restart) the desktop app to see imported threads. History merges into the same SQLite store the daemon uses for live traffic.

</details>

## Desktop app

`iphonebridge-qt` is a Qt / QML app — a separate process from the daemon, talking over D-Bus, so you can open and close it while the daemon keeps running. On Plasma it exports a native global menu. Four pages:

- **Messages** — SMS & iMessage conversations. Opening a chat pages recent messages from SQLite; scroll up for older ones. Reply, react (any emoji), edit, and unsend from the bubble menu or compose box. Attachments and reply threads when the direct iMessage helper is running.
- **Notifications** — live feed of every app’s notifications mirrored over ANCS.
- **Calls** — dialer plus Answer / Hang-up; an incoming call raises this tab.
- **Setup** — daemon health, contact/message counts, and the iPhone-toggle checklist.

**Search** is the field at the top of the conversation list (or **Edit → Find…** / <kbd>Ctrl</kbd>+<kbd>F</kbd>). Type-as-you-go FTS over message bodies; hits replace the sidebar and jump to the bubble.

**Launch at Login** lives under the app menu (writes `~/.config/autostart/com.gabriel.iphonebridge.Qt.desktop`).

```bash
iphonebridge-qt
```

## CLI

| Command | What it does |
|---|---|
| `iphonebridge run` | Run the daemon in the foreground |
| `iphonebridge doctor` | Check config, adapter class, obexd, state dir |
| `iphonebridge pair-setup` | First-run wizard |
| `iphonebridge sms-list` | Recent messages (`-n`, `--from`, `--source iphone\|local`) |
| `iphonebridge sms-send <to> <body>` | Send SMS / iMessage (number or contact name) |
| `iphonebridge backup-sync` | Import history + attachments from a USB iOS backup |
| `iphonebridge backup-wifi` | Allow backups over Wi‑Fi after first USB trust |
| `iphonebridge call <to>` | Place a call over HFP |
| `iphonebridge calls` / `hangup` | List or hang up active calls |
| `iphonebridge contacts-sync` | Force a contacts refresh |
| `iphonebridge ancs-enable` / `hfp-enable` | One-time ANCS / HFP setup |
| `iphonebridge version` | Print the version |

```bash
iphonebridge sms-list -n 20
iphonebridge sms-list --from Maddie
iphonebridge sms-list --source local   # ~/.local/state/iphonebridge/messages.sqlite

iphonebridge backup-sync
iphonebridge sms-send "+15551234567" "on my way"
iphonebridge sms-send Maddie "running late"

iphonebridge call Maddie
journalctl --user -u iphonebridge -f
systemctl --user {start,stop,restart} iphonebridge
```

## How it behaves

- **Incoming messages** appear as **persistent notifications** until you dismiss them on the desktop or read them on the iPhone. Read-state syncs both ways where the transport supports it.
- **Verification codes** — texts with a verification keyword and a 4–8 digit code are copied to the clipboard automatically.
- **Incoming calls** raise a notification with **Answer / Decline**.
- **Sent messages** from the desktop are recorded into conversation history so threads show both sides.
- **History** lives in **`~/.local/state/iphonebridge/messages.sqlite`**. The daemon writes every live event; `backup-sync` merges USB backup data into the same store. After upgrading to a SQLite-using build, **restart the daemon once** so new messages land in the store.

## How it works

```
              iPhone  (paired: BR/EDR + BLE)     Apple APNs/IDS
   ┌──────────┬──────────┬───────────┬──────────┐      │
   │ MAP      │ PBAP     │ ANCS      │ HFP      │  ib-imessage
   │ (OBEX)   │ (OBEX)   │ (BLE GATT)│ (oFono)  │  (Rust helper)
   ▼          ▼          ▼           ▼           ▼
 messages   contacts   app notifs   calls    full iMessage
   └──────────┴──────────┴───────────┴───────────┘
                     │
              Blue daemon
         (Python · GLib · D-Bus)
                     │
        ┌────────────┼────────────┐
        ▼            ▼            ▼
  notifications  messages.sqlite  D-Bus service
  + clipboard     (FTS history)   (CLI · Qt app)
```

- **MAP** — read/send SMS and iMessage *text* in real time. No attachments, groups, or reply metadata by itself.
- **Direct iMessage** (`ib-imessage`) — Apple-facing transport for guids, attachments, replies, tapbacks, edits, unsends, typing, and receipts. Optional second systemd unit.
- **PBAP** — contacts so messages show names, not numbers.
- **ANCS** — every app’s notifications over BLE GATT.
- **HFP** — calls via oFono; PipeWire carries audio.
- **Backup import** — fills gaps MAP cannot (sent-from-phone, media, full history).
- One daemon, pluggable **sinks** (notifications, OTP clipboard, SQLite), and a **D-Bus** API for the CLI and Qt app.

Design notes: [`spike/RESULTS.md`](spike/RESULTS.md). Contributor notes: [`AGENTS.md`](AGENTS.md).

## Troubleshooting

<details>
<summary><b>Messages stopped arriving</b></summary>

```bash
systemctl --user restart iphonebridge
```

If the iOS toggles vanished, forget + re-pair the iPhone.
</details>

<details>
<summary><b><code>Forbidden</code> errors in the log</b></summary>

An iPhone toggle is off. Check **Settings → Bluetooth → ⓘ → Show Message Notifications / Sync Contacts / Show System Notifications**.
</details>

<details>
<summary><b>ANCS notifications never arrive</b></summary>

Run `iphonebridge ancs-enable`, then forget + re-pair. Prefer an Intel adapter — Realtek and USB dongles usually can’t form the BLE bond.
</details>

<details>
<summary><b>Calls don’t connect, or there’s no call audio</b></summary>

Run `iphonebridge hfp-enable`, then `sudo systemctl restart ofono` **after** WirePlumber is up, reconnect the iPhone, restart the daemon. If oFono logs `UUID already registered`, the start order is wrong — restart oFono again after WirePlumber.
</details>

<details>
<summary><b>Verification codes aren’t being copied</b></summary>

Install `wl-clipboard` (Wayland) or `xclip` (X11). The log shows `no clipboard tool worked` when none is present.
</details>

<details>
<summary><b><code>iphonebridge: command not found</code></b></summary>

Use `source .venv/bin/activate`, or create the `~/.local/bin` symlinks from install step 2.
</details>

<details>
<summary><b>App history is empty / missing recent messages</b></summary>

1. `ls -la ~/.local/state/iphonebridge/messages.sqlite`
2. **Restart the daemon** after upgrade — only a process with `SqliteSink` writes live events (`systemctl --user restart iphonebridge`).
3. For older sent-from-phone messages and media: `iphonebridge backup-sync`.
4. Restart the app after a large import.
</details>

<details>
<summary><b>iMessage attachments / tapbacks never appear</b></summary>

Those need the **direct iMessage helper**, not MAP. Check:

```bash
systemctl --user status iphonebridge-imessage
journalctl --user -u iphonebridge-imessage -f
```

If you also run OpenBubbles, stop one of them — they cannot share the same Apple push token.
</details>

## Limitations

**Over Bluetooth MAP alone** (no direct iMessage helper, no backup import):

- No attachments, reactions, read receipts, typing indicators, or reply threading.
- No group iMessage / MMS / RCS — MAP is 1-to-1 text only.
- Messages you type *on the iPhone* do not appear — iOS exposes the inbox, not the sent folder.

**With the direct iMessage helper and/or `backup-sync`**, those gaps close for iMessage threads. Remaining limits:

- HFP calls are **1-to-1 voice only** — no conference calls, no FaceTime.
- Notification bodies follow the iPhone’s “Show Previews” setting.
- Apple does not replay traffic missed while the APNs connection is down; a later backup import recovers those.
- SMS/MMS that never go through Apple remain MAP-limited unless present in an iOS backup.

## Roadmap

- **Flatpak** for the UI — draft in [`packaging/flatpak/`](packaging/flatpak/) still targets the removed GTK app; needs a Qt rewrite.
- Encrypted message store, multi-device support — see [`BACKLOG.md`](BACKLOG.md).

## Credits

Blue builds on two GPL-2.0 projects:

- **[bmh129/ancs4linux](https://github.com/bmh129/ancs4linux)** — BR/EDR-vs-BLE coexistence, the `LastUsedBearer=le` unlock, and adapter compatibility. ANCS wire-format code in [`src/iphonebridge/ancs/`](src/iphonebridge/ancs/) is derived from their `observer/ancs/` modules.
- **[pzmarzly/ancs4linux](https://github.com/pzmarzly/ancs4linux)** — the original 2022 ANCS-on-Linux reference.

The direct iMessage path uses a vendored [rustpush](rust/rustpush/)-based stack and OpenBubbles-compatible registration.

## License

[GPL-2.0-or-later](LICENSE) · © 2026 Gabe Shatunovsky
