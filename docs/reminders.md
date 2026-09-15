# Apple Reminders through the Mac

Verdigris Reminders is a native Rust/GTK4 client for
[cleverdevil/iCloudBridge](https://github.com/cleverdevil/iCloudBridge). This is
the Swift REST server, **not** the unrelated Nextcloud sync utility of the same
name. BlueBubbles continues to handle Messages.

The first version supports list filtering, title/notes search, showing completed
items, creating reminders, editing titles and notes, and marking tasks complete
or incomplete. It displays due dates and saves a private local snapshot. The Mac
pushes EventKit invalidations over an authenticated WebSocket while the window is
open; Ctrl+R refreshes immediately and a 15-minute safety refresh covers a broken
stream or an older bridge. Ctrl+N opens a new reminder. Failed refreshes preserve the last snapshot and
disable editing until the connection recovers. Writes are never queued offline
or automatically retried.

## Mac setup

Requires macOS 14 or later, an active user session, and iCloud Reminders enabled
for the account used on your iPhone. Keep the Mac awake for continuous access.
The bridge has its own Reminders permission, separate from BlueBubbles.

The integration was developed against upstream commit
`0d88afb2224f08a10d2f92d46c82898c1461777e`. Its onboarding requires all three
Reminders, Calendars, and Photos permissions even if only Reminders is used.
Our [patch](../macos/icloudbridge-reminders-only.patch) makes Reminders sufficient
and shows just that permission step. It does not change macOS permission checks
or the REST protocol. The other bridge capabilities are not configured by this
integration.

With Xcode Command Line Tools installed, build on the Mac:

```sh
git clone https://github.com/cleverdevil/iCloudBridge.git ~/Projects/verdigris-icloudbridge
cd ~/Projects/verdigris-icloudbridge
git checkout 0d88afb2224f08a10d2f92d46c82898c1461777e
# Copy macos/icloudbridge-reminders-only.patch from Verdigris to the Mac first.
git apply /path/to/icloudbridge-reminders-only.patch
# Copy macos/push and macos/signing beside each other, then apply, build, and sign.
bash /path/to/macos/push/build.sh ~/Projects/verdigris-icloudbridge
mkdir -p ~/Applications
ditto build/iCloudBridge.app ~/Applications/iCloudBridge.app
open ~/Applications/iCloudBridge.app
```

Grant Reminders access in the setup window, select the lists to expose, and
choose **Save & Start Server** in Settings. Leave **Allow remote connections**
off when using the SSH tunnel below. Add the app to your Mac login items so it
returns after login. Avoid replacing a running installation when rebuilding;
quit that bridge app first.

## Linux connection

Install the native apps with `./install.sh`, then open **Reminders**.

An SSH tunnel uses your existing SSH key and keeps the bridge on the Mac's
loopback interface. Run this on Linux, substituting your Mac's SSH address:

```sh
ssh -NT -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 -L 127.0.0.1:31337:127.0.0.1:31337 username@macmini.local
```

In **Settings → Reminders and Notes connection**, enter `http://127.0.0.1:31337/`, leave
the token empty, and click **Save and connect**. Then refresh Reminders. The URL
is the server base, without `/api/v1`. The connection is independent of Messages.

For persistent tunneling, the same SSH command can run in a systemd user service
with `BatchMode=yes`, `Restart=always`, and `RestartSec=5`. If the local port is
already in use, choose another local port and use it in Verdigris Settings.

Direct remote connections require the bridge's **Allow remote connections**
setting and an API token. Use an encrypted VPN or HTTPS; the bridge itself
serves HTTP. The token is stored in the desktop Secret Service keyring, separately
from the BlueBubbles password, and sent only in the Authorization header. HTTP
redirects are rejected. Do not expose the localhost-exempt bridge through a
public reverse proxy without enforcing authentication there.

Settings are stored in `$XDG_CONFIG_HOME/verdigris/reminders.json` and snapshots
in `$XDG_DATA_HOME/verdigris/reminders-<server-hash>.json` (standard XDG defaults
apply). Files are mode 0600. Snapshots are separated by server URL; changing the
connection does not show the previous server's reminders. Reusing the same URL
for a different Apple account requires removing its old snapshot first.

## Current boundaries

- Dates are displayed but not created, changed, or cleared. The evaluated
  bridge's date-update code removes alarms, and its response cannot distinguish
  a date-only task from a timed task at midnight. Display uses Linux local time.
- Existing date, priority, and other fields are omitted from our edits. Title
  and notes updates include only fields the user actually changed. The bridge
  has no conditional-write/version API, so simultaneous edits to the same field
  on another device can still conflict.
- No list creation, reminder deletion, recurrence editor, tags, subtasks, or
  Linux scheduling/notifications in this first version. Completing a recurring
  reminder is handled by EventKit; refresh reloads the resulting list.
- The upstream bridge has no Notes API. Verdigris now provides an optional
  [Notes extension](notes.md) sharing this connection and SSH tunnel.
- Live updates carry only an epoch and generation counter; reminder contents
  still use the authenticated REST endpoints. A reconnect always forces a
  reconciliation, and the stream uses the same SSH tunnel or API-token policy.
- This is an app-window client; `verdigris-sync` continues to handle Messages.

## Validation

```sh
cargo test --manifest-path rust/verdigris-apps/Cargo.toml --test reminders --locked
pytest -q tests/test_icloudbridge_push_patch.py
cargo test --manifest-path rust/verdigris-apps/Cargo.toml --test icloud_push --locked
pytest -q tests/test_native_install.py
cargo build --manifest-path rust/verdigris-apps/Cargo.toml --bins --locked
dbus-run-session -- xvfb-run -a --server-args='-screen 0 1100x850x24' \
  python3 rust/verdigris-apps/tests/reminders_smoke.py
```

The GTK test uses a disposable loopback fixture, temporary XDG directories,
xdotool, and ImageMagick. It exercises create/edit/complete requests, preserves
dates during edits, and checks cached viewing with writes disabled offline.
The Rust tests cover URL encoding, authentication, rejected redirects/errors,
sparse updates, response validation, and cache permissions/isolation.

Live validation also passed on macOS 26.5.2 through an SSH tunnel: the installed
Linux app loaded the selected lists and saved its snapshot, and a temporary
reminder was created, edited, completed, read back, and deleted. The Mac bridge
was confirmed to listen only on `127.0.0.1`.

Live push validation passed on macOS 26.6.2 through the same tunnel. A WebSocket
subscriber received generation 0 on connection, generation 1 after creating a
disposable reminder, and generation 2 after deleting it. The probe reminder was
removed, the signed bridge restarted cleanly, and its REST health endpoint stayed
available after installation.
