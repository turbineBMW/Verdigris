# Verdigris Bluetooth backend and legacy Qt frontend

Verdigris is a fork of [Blue](https://github.com/gutbash/blue). This guide covers
its inherited Bluetooth backend, CLI, optional Qt frontend, and experimental direct
Apple transport. For the native Phone and Messages apps, start with the
[main README](../README.md).

The native Messages app uses a BlueBubbles Mac relay. The Bluetooth and direct
Apple paths described below are separate inherited components.

## Setup

### 1. System packages

```bash
sudo apt install bluez bluez-obexd python3-dbus python3-gi python3-venv
# Verification-code auto-copy (Wayland):
sudo apt install wl-clipboard
```

### 2. Install Verdigris

```bash
# From your Verdigris checkout:

# Inherit system PyGObject + dbus-python (do not install those from PyPI).
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -e ".[qt]"

# Put the CLI and desktop app on your PATH
mkdir -p ~/.local/bin
ln -sf "$(pwd)/.venv/bin/verdigris" ~/.local/bin/verdigris
ln -sf "$(pwd)/.venv/bin/verdigris-qt" ~/.local/bin/verdigris-qt
```

Ensure `~/.local/bin` is on your `PATH` (most desktops add it after a logout).

### 3. Pair your iPhone

Pair in GNOME **Settings → Bluetooth**, KDE **System Settings → Bluetooth**, or `bluetoothctl`. Then:

```bash
verdigris pair-setup
```

This finds your iPhone among paired devices, writes `~/.config/verdigris/local.env`, and prints the iPhone-side steps.

### 4. Start the daemon

```bash
mkdir -p ~/.config/systemd/user
cp systemd/verdigris.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now verdigris
```

Check health:

```bash
verdigris doctor
systemctl --user status verdigris
journalctl --user -u verdigris -f
```

### 5. iPhone toggles

On the iPhone: **Settings → Bluetooth → ⓘ next to your computer →** enable:

| Toggle | What it enables |
|---|---|
| **Show Message Notifications** | SMS / iMessage (MAP) |
| **Sync Contacts** | Contacts (PBAP) |
| **Share System Notifications** | Per-app notifications (ANCS; optional) |

**Show Message Notifications** and **Sync Contacts** appear after the daemon
has run with the correct adapter class. **Share System Notifications** is
different: iOS shows it only after the computer has formed a real BLE bond and
requested ANCS authorization. Follow the optional ANCS setup below; its absence
does not indicate a broken MAP/PBAP setup.

### 6. Open the desktop app

```bash
verdigris-qt
```

The first launch:

- Installs the **blue message-bubble** icon into `~/.local/share/icons/hicolor/`
- Installs a **Verdigris** launcher into `~/.local/share/applications/`
- Pins cleanly on Plasma / GNOME taskbars as **Verdigris**

Pin **Verdigris** from your app menu (or right-click the taskbar icon → Pin). You can also enable **Launch at Login** from the app menu.

Four pages:

- **Messages** — conversations, search, reply / react / edit (full iMessage when the helper is running)
- **Notifications** — ANCS feed from every app
- **Calls** — dialer; Answer / Decline on incoming
- **Setup** — daemon health and the iPhone-toggle checklist

---

## Optional features

<details>
<summary><b>Per-app notifications (ANCS)</b></summary>

ANCS needs a true BLE bond formed during a fresh pairing while Verdigris's ANCS
advertisement is active. Intel adapters are validated; other chipsets may fail
to create the BLE bond even when ordinary Bluetooth, MAP, and PBAP work.

First install the narrowly scoped bearer helper. The installer also enables
BlueZ's experimental userspace D-Bus API for its live `PreferredBearer`
property (it does not enable kernel-experimental features):

```bash
sudo bash systemd/install-ancs-sudoers.sh
sudo systemctl restart bluetooth
systemctl --user restart verdigris
```

Then leave the Verdigris daemon running, forget the computer on the iPhone, remove
the iPhone from Linux, and pair again **from the iPhone**. The active advert and
fresh pairing let iOS/BlueZ derive both Classic and BLE keys. After the new pair:

```bash
verdigris pair-setup
verdigris ancs-enable
```

Allow the notification-sharing prompt on the iPhone. If needed, the resulting
permission is under **Settings → Bluetooth → ⓘ → Share System Notifications**.
`verdigris doctor` reports whether the ANCS GATT service is actually active.

iOS may show two same-named computer rows while the linked Classic and LE
transports are active. Do not forget only the disconnected-looking row to try
to remove the duplicate: iOS can remove the shared bond behind both rows.

</details>

<details>
<summary><b>Phone calls (HFP)</b></summary>

Calls use **oFono** for HFP control and PipeWire’s oFono backend for audio:

```bash
sudo apt install ofono
sudo systemctl enable --now ofono
verdigris hfp-enable
```

Follow the printed steps — restart oFono **after** WirePlumber so it can claim HFP, reconnect the iPhone, restart the daemon. Incoming calls show **Answer / Decline**. Place calls with `verdigris call`.

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

Bluetooth MAP only carries plain text. For guids, media, reply threading, any-emoji tapbacks, edits, typing, and delivery/read receipts, run the direct Apple transport (`verdigris-imessage`). It reuses an OpenBubbles-style registration and talks to APNs/IDS — not to the phone over Bluetooth.

```bash
cd rust/verdigris-imessage && cargo build --release
cp target/release/verdigris-imessage ~/.local/bin/.verdigris-imessage.new \
  && mv -f ~/.local/bin/.verdigris-imessage.new ~/.local/bin/verdigris-imessage

# Import registration from OpenBubbles (once), then enable the user unit.
# See comments in systemd/verdigris-imessage.service for import flags.
mkdir -p ~/.config/systemd/user
cp systemd/verdigris-imessage.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now verdigris-imessage
```

**Important:** Apple allows **one connection per push token**. Stop `verdigris-imessage` before opening OpenBubbles to renew registration, re-import, then start the unit again. Details are in the unit file comments and the service file comments.

</details>

<details>
<summary><b>Import full history from an iOS backup</b></summary>

MAP never shows messages you typed on the phone, and it has no attachments. A local USB (or Wi‑Fi) backup has both:

```bash
verdigris backup-sync                # fresh backup + import into messages.sqlite
verdigris backup-sync --skip-backup  # re-read the last backup on disk
verdigris backup-wifi                # allow later backups without a cable
```

Open (or restart) Verdigris to see imported threads. History merges into the same SQLite store the daemon uses for live traffic.

</details>

---

## CLI

| Command | What it does |
|---|---|
| `verdigris run` | Run the daemon in the foreground |
| `verdigris doctor` | Check config, adapter class, obexd, state dir |
| `verdigris pair-setup` | First-run wizard |
| `verdigris sms-list` | Recent messages (`-n`, `--from`, `--source iphone\|local`) |
| `verdigris sms-send <to> <body>` | Send SMS / iMessage (number or contact name) |
| `verdigris backup-sync` | Import history + attachments from a USB iOS backup |
| `verdigris backup-wifi` | Allow backups over Wi‑Fi after first USB trust |
| `verdigris call <to>` | Place a call over HFP |
| `verdigris calls` / `hangup` | List or hang up active calls |
| `verdigris contacts-sync` | Force a contacts refresh |
| `verdigris ancs-enable` / `hfp-enable` | One-time ANCS / HFP setup |
| `verdigris version` | Print the version |

```bash
verdigris sms-list -n 20
verdigris sms-list --from Maddie
verdigris sms-list --source local   # ~/.local/state/verdigris/messages.sqlite

verdigris backup-sync
verdigris sms-send "+15551234567" "on my way"
verdigris sms-send Maddie "running late"

verdigris call Maddie
journalctl --user -u verdigris -f
systemctl --user {start,stop,restart} verdigris
```

---

## How it works

```
              iPhone  (paired: BR/EDR + BLE)     Apple APNs/IDS
   ┌──────────┬──────────┬───────────┬──────────┐      │
   │ MAP      │ PBAP     │ ANCS      │ HFP      │  verdigris-imessage
   │ (OBEX)   │ (OBEX)   │ (BLE GATT)│ (oFono)  │  (Rust helper)
   ▼          ▼          ▼           ▼           ▼
 messages   contacts   app notifs   calls    full iMessage
   └──────────┴──────────┴───────────┴───────────┘
                     │
                 Verdigris daemon
         (Python · GLib · D-Bus)
                     │
        ┌────────────┼────────────┐
        ▼            ▼            ▼
  notifications  messages.sqlite  D-Bus service
  + clipboard     (FTS history)   (CLI · Verdigris app)
```

- **MAP** — read/send SMS and iMessage *text* in real time. No attachments or reply metadata by itself.
- **Direct iMessage** (`verdigris-imessage`) — Apple-facing transport for guids, attachments, replies, tapbacks, edits, typing, and receipts.
- **PBAP** — contacts so messages show names, not numbers.
- **ANCS** — every app’s notifications over BLE GATT.
- **HFP** — calls via oFono; PipeWire carries audio.
- **Backup import** — fills gaps MAP cannot (sent-from-phone, media, full history).

Design notes: [`spike/RESULTS.md`](../spike/RESULTS.md). Contributor notes: the service file comments.

### Behaviour notes

- Incoming messages raise **persistent notifications** until you dismiss them on the desktop or read them on the iPhone.
- Texts with a verification keyword and a 4–8 digit code are **copied to the clipboard**.
- Incoming calls raise a notification with **Answer / Decline**.
- History lives in **`~/.local/state/verdigris/messages.sqlite`**. After upgrading to a SQLite-using build, **restart the daemon once**.

### iMessage over Bluetooth

On recent iOS, Verdigris receives *and sends* iMessage through the standard MAP profile with no Mac and no Apple ID on the Linux side. iOS labels iMessage and SMS the same over MAP (`Type: sms-gsm`). MAP alone is plain text; enable `verdigris-imessage` for full verdigris-bubble fidelity. Empirical notes: [`spike/RESULTS.md`](../spike/RESULTS.md).

---

## Troubleshooting

<details>
<summary><b>Messages stopped arriving</b></summary>

```bash
systemctl --user restart verdigris
```

If the iOS toggles vanished, run `verdigris doctor` first. Re-pair only when it
reports that the required transport is missing.
</details>

<details>
<summary><b><code>Forbidden</code> errors in the log</b></summary>

An iPhone toggle is off. Check **Settings → Bluetooth → ⓘ → Show Message Notifications / Sync Contacts**. ANCS has a separate BLE setup; see below.
</details>

<details>
<summary><b>ANCS notifications never arrive</b></summary>

If `verdigris doctor` says ANCS GATT is not active, install the ANCS helper, restart
Bluetooth and Verdigris as shown above, then run `verdigris ancs-enable`. Only if the
device still lacks `LE.Bonded: yes` should you forget the pairing on both ends
and pair again from the iPhone while Verdigris is running. Prefer an Intel adapter;
other chipsets may not form the required BLE bond. Once ANCS is active, enable
**Share System Notifications** on the iPhone.

BlueZ 5.87 has an LE address-resolution regression for dual-mode bonded
phones: `LE.Bonded: yes` can coexist with an endlessly timing-out LE connect.
The ANCS installer includes a narrowly scoped helper for that regression, and
Verdigris reapplies the kernel-only flag whenever the daemon starts. `verdigris doctor`
requires a live LE bearer, so cached GATT characteristics can no longer produce
a false-positive ANCS result.

ANCS supplies each source app's bundle identifier and display name, so Verdigris
labels notifications as **Wallet**, **Reminders**, **Slack**, and so on. Classic
ANCS does not transmit app-icon image data. Verdigris uses a matching installed
Linux application's icon when one exists, otherwise a category-specific theme
icon. Native **Settings → iPhone app notifications** lets you disable a source app,
choose a Linux app to launch when clicked, and override its icon. These rules apply
without restarting the updated Verdigris backend; disabled notifications are dropped
before desktop popups, history sinks, and D-Bus. Only the source app's ID/name is
retained in the discovered-app registry, so it remains available in Settings.

For manual icon overrides, the existing PNG, SVG, WebP, or JPEG files named after
the bundle identifier in `~/.local/state/verdigris/ancs_app_icons/` still work
(for example, `com.example.SomeApp.png`). Set `VERDIGRIS_ANCS_ICON_DIR` to use a
different directory. An icon selected in native Settings takes precedence. Bundle
identifiers are shown in each app's notification settings. ANCS does not include a
Linux-compatible deep link, so click targets open the app without selecting a
particular conversation.
</details>

<details>
<summary><b>Calls don’t connect, or there’s no call audio</b></summary>

Run `verdigris hfp-enable`, then `sudo systemctl restart ofono` **after** WirePlumber is up, reconnect the iPhone, restart the daemon. If oFono logs `UUID already registered`, the start order is wrong — restart oFono again after WirePlumber.
</details>

<details>
<summary><b>Verification codes aren’t being copied</b></summary>

Install `wl-clipboard` (Wayland) or `xclip` (X11). The log shows `no clipboard tool worked` when none is present.
</details>

<details>
<summary><b><code>verdigris: command not found</code></b></summary>

Use `source .venv/bin/activate`, or create the `~/.local/bin` symlinks (`verdigris`, `verdigris-qt`) from install step 2.
</details>

<details>
<summary><b>App history is empty / missing recent messages</b></summary>

1. `ls -la ~/.local/state/verdigris/messages.sqlite`
2. **Restart the daemon** after upgrade — only a process with `SqliteSink` writes live events (`systemctl --user restart verdigris`).
3. For older sent-from-phone messages and media: `verdigris backup-sync`.
4. Restart Verdigris after a large import.
</details>

<details>
<summary><b>Taskbar icon is missing or wrong</b></summary>

Launch Verdigris once (`verdigris-qt`) so it installs the message-bubble icon and desktop entry, then:

```bash
# Or install manually from the repo:
mkdir -p ~/.local/share/icons/hicolor/scalable/apps
cp data/icons/dev.turbinebmw.Verdigris.UI.svg \
  ~/.local/share/icons/hicolor/scalable/apps/dev.turbinebmw.Verdigris.Qt.svg
cp data/icons/dev.turbinebmw.Verdigris.UI.svg \
  ~/.local/share/icons/hicolor/scalable/apps/dev.turbinebmw.Verdigris.UI.svg
cp data/dev.turbinebmw.Verdigris.Qt.desktop ~/.local/share/applications/
# Fix Exec= to your real path if needed:
sed -i "s|^Exec=.*|Exec=$HOME/.local/bin/verdigris-qt|" \
  ~/.local/share/applications/dev.turbinebmw.Verdigris.Qt.desktop
gtk-update-icon-cache -f -t ~/.local/share/icons/hicolor 2>/dev/null || true
```

Unpin and re-pin **Verdigris** if the panel still shows a generic icon.
</details>

<details>
<summary><b>iMessage attachments / tapbacks never appear</b></summary>

Those need the **direct iMessage helper**, not MAP. Check:

```bash
systemctl --user status verdigris-imessage
journalctl --user -u verdigris-imessage -f
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

- **Flatpak** for the UI — draft in [`packaging/flatpak/` ](../packaging/flatpak/) still targets the removed GTK app; needs a Qt rewrite.
- Encrypted message store, multi-device support — see [`BACKLOG.md`](../BACKLOG.md).
