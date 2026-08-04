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

**Blue** is that missing piece: pair your iPhone over Bluetooth, run a small desktop daemon, and get SMS, iMessage, calls, contacts, and app notifications on Linux. Optional full-fidelity iMessage (attachments, tapbacks, edits, typing, receipts) uses a direct Apple transport — still no Mac and no paid relay.

> **Names.** The product and CLI are **Blue** (`blue`, `blue-qt`). The Python package, config dir, D-Bus id, and systemd unit names remain `iphonebridge` for compatibility (`com.gabriel.iphonebridge`, `~/.config/iphonebridge/`, `systemctl --user … iphonebridge`). Legacy `iphonebridge` / `iphonebridge-qt` commands still work as aliases.

## Features

| Feature | How | Status |
|---|---|---|
| Incoming SMS + iMessage as desktop notifications | MAP MNS push | ✅ |
| Send SMS + iMessage from the app or CLI | MAP `PushMessage` | ✅ |
| Full iMessage — attachments, replies, tapbacks, edits, typing, receipts | Direct Apple transport (`ib-imessage`) | ✅ optional |
| Verification codes auto-copied to the clipboard | OTP detection | ✅ |
| Contact names & photos | PBAP → SQLite cache | ✅ |
| Every app’s notifications (Slack, WhatsApp, Mail…) | ANCS over BLE | ✅ |
| Take & place phone calls | HFP via oFono | ✅ |
| Read-state sync | MAP + iMessage paths | ✅ |
| Searchable message history | SQLite + FTS5 | ✅ |
| Desktop app — conversations, notifications, dialer | Qt / QML | ✅ |
| Import full history from a local iOS backup | `backup-sync` | ✅ |
| Runs unattended as a systemd user service | — | ✅ |

## Requirements

| | Minimum | Tested with |
|---|---|---|
| **OS** | Linux + BlueZ 5.72+ | Pop!_OS 24.04 |
| **Desktop** | GNOME or KDE Plasma | — |
| **Bluetooth adapter** | Intel chipset (for ANCS) | Intel AX-series |
| **Python** | 3.10+ | 3.12 / 3.14 |
| **iPhone** | iOS 16.5+ | iPhone 16 Pro Max, iOS 26.5 |
| **System packages** | `bluez`, `bluez-obexd`, `python3-dbus`, `python3-gi` | — |

Optional: `ofono` (calls), `wl-clipboard` or `xclip` (auto-copy codes), Rust/`cargo` (full iMessage helper).

> **Adapter chipset matters for ANCS.** Per-app notifications need a real BLE bond. Intel adapters do this reliably; **Realtek adapters and many USB dongles do not**. SMS/iMessage/contacts (MAP/PBAP) work on any adapter — only ANCS is picky. See [ancs4linux hardware notes](https://github.com/bmh129/ancs4linux).

---

## Setup

### 1. System packages

```bash
sudo apt install bluez bluez-obexd python3-dbus python3-gi python3-venv
# Verification-code auto-copy (Wayland):
sudo apt install wl-clipboard
```

### 2. Install Blue

```bash
git clone https://github.com/gutbash/blue.git
cd blue

# Inherit system PyGObject + dbus-python (do not install those from PyPI).
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -e ".[qt]"

# Put the CLI and desktop app on your PATH
mkdir -p ~/.local/bin
ln -sf "$(pwd)/.venv/bin/blue" ~/.local/bin/blue
ln -sf "$(pwd)/.venv/bin/blue-qt" ~/.local/bin/blue-qt
```

Ensure `~/.local/bin` is on your `PATH` (most desktops add it after a logout).

### 3. Pair your iPhone

Pair in GNOME **Settings → Bluetooth**, KDE **System Settings → Bluetooth**, or `bluetoothctl`. Then:

```bash
blue pair-setup
```

This finds your iPhone among paired devices, writes `~/.config/iphonebridge/local.env`, and prints the iPhone-side steps.

### 4. Start the daemon

```bash
mkdir -p ~/.config/systemd/user
cp systemd/iphonebridge.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now iphonebridge
```

Check health:

```bash
blue doctor
systemctl --user status iphonebridge
journalctl --user -u iphonebridge -f
```

### 5. iPhone toggles

On the iPhone: **Settings → Bluetooth → ⓘ next to your computer →** enable:

| Toggle | What it enables |
|---|---|
| **Show Message Notifications** | SMS / iMessage (MAP) |
| **Sync Contacts** | Contacts (PBAP) |
| **Show System Notifications** | Per-app notifications (ANCS) |

These toggles appear after the daemon has run at least once (it sets the adapter class and advertises correctly). If they’re missing, re-run `blue pair-setup` and restart the daemon.

### 6. Open the desktop app

```bash
blue-qt
```

The first launch:

- Installs the **blue message-bubble** icon into `~/.local/share/icons/hicolor/`
- Installs a **Blue** launcher into `~/.local/share/applications/`
- Pins cleanly on Plasma / GNOME taskbars as **Blue**

Pin **Blue** from your app menu (or right-click the taskbar icon → Pin). You can also enable **Launch at Login** from the app menu.

Four pages:

- **Messages** — conversations, search, reply / react / edit (full iMessage when the helper is running)
- **Notifications** — ANCS feed from every app
- **Calls** — dialer; Answer / Decline on incoming
- **Setup** — daemon health and the iPhone-toggle checklist

---

## Optional features

<details>
<summary><b>Per-app notifications (ANCS)</b></summary>

ANCS needs a true BLE bond, formed during a fresh pairing with the adapter configured correctly:

```bash
sudo bash systemd/install-ancs-sudoers.sh
blue ancs-enable
```

Then **forget + re-pair** the iPhone (the wizard walks you through it). After that, iOS performs cross-transport key derivation and ANCS starts flowing.

</details>

<details>
<summary><b>Phone calls (HFP)</b></summary>

Calls use **oFono** for HFP control and PipeWire’s oFono backend for audio:

```bash
sudo apt install ofono
sudo systemctl enable --now ofono
blue hfp-enable
```

Follow the printed steps — restart oFono **after** WirePlumber so it can claim HFP, reconnect the iPhone, restart the daemon. Incoming calls show **Answer / Decline**. Place calls with `blue call`.

</details>

<details>
<summary><b>Persist the Bluetooth class across reboots</b></summary>

```bash
sudo bash systemd/install-cod-sudoers.sh
```

Lets the daemon set the adapter’s Class-of-Device on every start without a password prompt.

</details>

<details>
<summary><b>Full iMessage — attachments, replies, tapbacks, receipts</b></summary>

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
<summary><b>Import full history from an iOS backup</b></summary>

MAP never shows messages you typed on the phone, and it has no attachments. A local USB (or Wi‑Fi) backup has both:

```bash
blue backup-sync                # fresh backup + import into messages.sqlite
blue backup-sync --skip-backup  # re-read the last backup on disk
blue backup-wifi                # allow later backups without a cable
```

Open (or restart) Blue to see imported threads. History merges into the same SQLite store the daemon uses for live traffic.

</details>

---

## CLI

| Command | What it does |
|---|---|
| `blue run` | Run the daemon in the foreground |
| `blue doctor` | Check config, adapter class, obexd, state dir |
| `blue pair-setup` | First-run wizard |
| `blue sms-list` | Recent messages (`-n`, `--from`, `--source iphone\|local`) |
| `blue sms-send <to> <body>` | Send SMS / iMessage (number or contact name) |
| `blue backup-sync` | Import history + attachments from a USB iOS backup |
| `blue backup-wifi` | Allow backups over Wi‑Fi after first USB trust |
| `blue call <to>` | Place a call over HFP |
| `blue calls` / `hangup` | List or hang up active calls |
| `blue contacts-sync` | Force a contacts refresh |
| `blue ancs-enable` / `hfp-enable` | One-time ANCS / HFP setup |
| `blue version` | Print the version |

```bash
blue sms-list -n 20
blue sms-list --from Maddie
blue sms-list --source local   # ~/.local/state/iphonebridge/messages.sqlite

blue backup-sync
blue sms-send "+15551234567" "on my way"
blue sms-send Maddie "running late"

blue call Maddie
journalctl --user -u iphonebridge -f
systemctl --user {start,stop,restart} iphonebridge
```

---

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
  + clipboard     (FTS history)   (CLI · Blue app)
```

- **MAP** — read/send SMS and iMessage *text* in real time. No attachments or reply metadata by itself.
- **Direct iMessage** (`ib-imessage`) — Apple-facing transport for guids, attachments, replies, tapbacks, edits, typing, and receipts.
- **PBAP** — contacts so messages show names, not numbers.
- **ANCS** — every app’s notifications over BLE GATT.
- **HFP** — calls via oFono; PipeWire carries audio.
- **Backup import** — fills gaps MAP cannot (sent-from-phone, media, full history).

Design notes: [`spike/RESULTS.md`](spike/RESULTS.md). Contributor notes: [`AGENTS.md`](AGENTS.md).

### Behaviour notes

- Incoming messages raise **persistent notifications** until you dismiss them on the desktop or read them on the iPhone.
- Texts with a verification keyword and a 4–8 digit code are **copied to the clipboard**.
- Incoming calls raise a notification with **Answer / Decline**.
- History lives in **`~/.local/state/iphonebridge/messages.sqlite`**. After upgrading to a SQLite-using build, **restart the daemon once**.

### iMessage over Bluetooth

On recent iOS, Blue receives *and sends* iMessage through the standard MAP profile with no Mac and no Apple ID on the Linux side. iOS labels iMessage and SMS the same over MAP (`Type: sms-gsm`). MAP alone is plain text; enable `ib-imessage` for full blue-bubble fidelity. Empirical notes: [`spike/RESULTS.md`](spike/RESULTS.md).

---

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

Run `blue ancs-enable`, then forget + re-pair. Prefer an Intel adapter — Realtek and USB dongles usually can’t form the BLE bond.
</details>

<details>
<summary><b>Calls don’t connect, or there’s no call audio</b></summary>

Run `blue hfp-enable`, then `sudo systemctl restart ofono` **after** WirePlumber is up, reconnect the iPhone, restart the daemon. If oFono logs `UUID already registered`, the start order is wrong — restart oFono again after WirePlumber.
</details>

<details>
<summary><b>Verification codes aren’t being copied</b></summary>

Install `wl-clipboard` (Wayland) or `xclip` (X11). The log shows `no clipboard tool worked` when none is present.
</details>

<details>
<summary><b><code>blue: command not found</code></b></summary>

Use `source .venv/bin/activate`, or create the `~/.local/bin` symlinks (`blue`, `blue-qt`) from install step 2.
</details>

<details>
<summary><b>App history is empty / missing recent messages</b></summary>

1. `ls -la ~/.local/state/iphonebridge/messages.sqlite`
2. **Restart the daemon** after upgrade — only a process with `SqliteSink` writes live events (`systemctl --user restart iphonebridge`).
3. For older sent-from-phone messages and media: `blue backup-sync`.
4. Restart Blue after a large import.
</details>

<details>
<summary><b>Taskbar icon is missing or wrong</b></summary>

Launch Blue once (`blue-qt`) so it installs the message-bubble icon and desktop entry, then:

```bash
# Or install manually from the repo:
mkdir -p ~/.local/share/icons/hicolor/scalable/apps
cp data/icons/com.gabriel.iphonebridge.UI.svg \
  ~/.local/share/icons/hicolor/scalable/apps/com.gabriel.iphonebridge.Qt.svg
cp data/icons/com.gabriel.iphonebridge.UI.svg \
  ~/.local/share/icons/hicolor/scalable/apps/com.gabriel.iphonebridge.UI.svg
cp data/com.gabriel.iphonebridge.Qt.desktop ~/.local/share/applications/
# Fix Exec= to your real path if needed:
sed -i "s|^Exec=.*|Exec=$HOME/.local/bin/blue-qt|" \
  ~/.local/share/applications/com.gabriel.iphonebridge.Qt.desktop
gtk-update-icon-cache -f -t ~/.local/share/icons/hicolor 2>/dev/null || true
```

Unpin and re-pin **Blue** if the panel still shows a generic icon.
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

---

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

Blue is based on **[gabrielmeir53/iphonebridge](https://github.com/gabrielmeir53/iphonebridge)** by Gabe Shatunovsky.

It also builds on two GPL-2.0 projects:

- **[bmh129/ancs4linux](https://github.com/bmh129/ancs4linux)** — BR/EDR-vs-BLE coexistence, the `LastUsedBearer=le` unlock, and adapter compatibility. ANCS wire-format code in [`src/iphonebridge/ancs/`](src/iphonebridge/ancs/) is derived from their `observer/ancs/` modules.
- **[pzmarzly/ancs4linux](https://github.com/pzmarzly/ancs4linux)** — the original 2022 ANCS-on-Linux reference.

The direct iMessage path uses a vendored [rustpush](rust/rustpush/)-based stack and OpenBubbles-compatible registration.

## License

[GPL-2.0-or-later](LICENSE) · © 2026 Sebastian Gutierrez


