# Native Verdigris apps

Verdigris is a fork of [Blue](https://github.com/gutbash/blue). Its native
Rust/GTK4/libadwaita applications follow Bubo's layout and native stack.
The desktop launchers are **Phone** and **Messages**; `verdigris-settings` is
opened internally by either app. `verdigris-sync` owns message synchronization.
The inherited Qt frontend and Bluetooth backend remain separate components.

```sh
./install.sh
verdigris-messages
verdigris-phone
```

Run `./install.sh` from the repository root. Like Bubo's installer, it builds optimized
release binaries and copies them into `~/.local/bin`, with no root privileges required.
Building requires Cargo/Rust, Python 3, pkg-config, and the GTK 4.18+ and libadwaita 1.7+
development packages. Installation also requires a running systemd user session.
It installs the custom Messages and Phone icons and desktop launchers, a D-Bus
activation entry, and an optional user service. Settings opens from Messages' menu
or Phone's settings button; its old desktop launcher is removed during installation.
Rerun it to update the installation; the installed apps work independently of this
checkout. It does not change Bluetooth, oFono, PipeWire, or the inherited backend service.
When upgrading, it removes the old native Blue launchers and disables the old
native sync unit. It does not automatically enable the Verdigris sync unit.

For development, `python3 rust/verdigris-apps/install.py` builds debug binaries and installs
symlinks to this checkout instead. It refuses to replace standalone executables;
use `python3 rust/verdigris-apps/install.py --copy` for a standalone debug installation.

## Mac setup

1. On the Mac mini, sign into Apple's Messages app with the account used on the iPhone.
   Verify Messages can receive and send normally.
2. Install [BlueBubbles Server](https://github.com/BlueBubblesApp/bluebubbles-server/releases)
   and grant its required macOS permissions, including Full Disk Access.
3. Configure a server password and a stable URL reachable from Linux. Use HTTPS or a
   trusted encrypted VPN for remote access. This native client does not use Firebase.
4. Keep the Mac awake and BlueBubbles running in its logged-in user session.
5. Open Settings from Messages' menu or Phone's settings button, then enter the base
   server URL and password. Settings tests the
   server before saving. The password is stored in the desktop Secret Service keyring.

For SMS forwarding, configure the iPhone to forward texts to the Mac and verify those
messages appear in the Mac's Messages app. Availability of SMS/MMS/RCS depends on what
Apple actually delivers to that Mac; this client cannot manufacture missing history.
Text sending, new one-to-one conversations, and attachment sending use AppleScript;
no Private API features are required for those paths. Creating new group chats on
modern macOS requires the Private API and is not exposed by this client yet.

## Implemented

- **Messages:** conversation list, contact names and circular avatars, cached text history,
  text replies, read/delivery timestamps, and loading up to 1,000 recent messages per
  selected conversation. Search filters conversations by contact name, address, or last
  message preview; it does not search full message history yet. Drafts are held separately
  per conversation while the window is open. The window uses Bubo's split view, separate
  headers, conversation rows, message bubbles, and system accent handling. Search is in
  the sidebar menu (Ctrl+F); refresh is Ctrl+R. Narrow windows show one pane at a time.
- **Composer and GIFs:** Bubo's growing multiline input, attachment/GIF/emoji controls,
  and circular Send button. Enter adds a newline; Ctrl+Enter sends. The GIF popover
  searches DuckDuckGo with debounced queries and a three-column thumbnail grid. Selecting
  a tile sends the animation through the Mac as an attachment, leaving any text draft
  intact. GIF downloads are limited to 8 MiB, previews to 2 MiB, and failed sends are
  never retried automatically. This uses Bubo's keyless, undocumented search provider;
  provider changes or rate limits can temporarily prevent search.
- **New message:** the compose button or Ctrl+N opens a one-to-one composer with contact suggestions,
  phone-number/email entry, and an iMessage/SMS selector. Clicking Send creates the chat
  and sends its first message. Nothing is sent just by choosing a contact.
- **Attachments:** Open downloads a file on demand and launches its default viewer.
  Images near the viewport download automatically and appear inline; GIF previews animate. The paperclip opens a file chooser and
  a filename/recipient review before sending. File sends are separate from any text draft.
  Transfers are limited to 100 MiB and three minutes. Completed downloads are cached per
  server and attachment GUID; interrupted transfers never become valid cache files.
- **Phone:** number entry, dial pad, dial, answer, and hang up using Verdigris's existing
  `dev.turbinebmw.Verdigris.Bridge.Calls1` D-Bus service. A local snapshot every two seconds
  updates the call view. Existing Verdigris notifications and PipeWire audio remain responsible
  for incoming-call alerts and audio.
- **Settings:** shared Mac URL/password, connection test, and Bluetooth service check.
  Both apps open the same single-instance Settings application. Bluetooth pairing remains
  in the existing `verdigris pair-setup` / `verdigris hfp-enable` tools for this slice.
- **verdigris-sync:** one shared process owns the cache and synchronization. Socket.IO events,
  Bluetooth Messages events, app focus, and manual refresh trigger synchronization.
  A two-minute reconciliation also recovers silent disconnections. A four-second follow-up
  handles a phone notification arriving before the Mac has recorded its message.

The first sync seeds the latest 100 messages across the account. Opening a conversation
fetches its own recent history. Later catch-up syncs page through the entire gap, with a
five-minute overlap, and deduplicate by chat/message GUID. Cache updates and the sync
cursor commit together, only after every page succeeds. Older edits/deletions are not
fully reconciled yet; reopening a thread refreshes its recent messages and receipts.
Timed-out sends are **never automatically retried** because delivery may already have occurred.

Cache and configuration live under the XDG data/config directories in `verdigris/`.
Each server URL has a separate SQLite database. The current design assumes one Apple
account per server URL. The Bluetooth backend’s `messages.sqlite` is separate from this native cache.
The cache is protected by a private directory; it is not encrypted at rest.

Names, nicknames, and photos are read from Verdigris's existing iPhone PBAP cache at
`$XDG_STATE_HOME/verdigris/contacts.sqlite` (normally `~/.local/state/verdigris/`).
The native apps open it read-only. Messages uses names in the sidebar, thread header,
and sender labels; Phone uses names/photos on active calls. Named groups retain their
group title; unnamed groups resolve participant names. Missing or unreadable photos
fall back to initials. Email-only iMessage identities remain addresses because Verdigris's
current contact cache indexes phone numbers only. These are address-book photos, not
automatic downloads of people's latest shared iMessage profile pictures.

The installer preserves old Blue native data and supports its existing keyring
entry; see [Upgrading from Blue](../../README.md#upgrading-from-blue).

Use `verdigris contacts-sync` with your paired iPhone nearby to refresh the source cache.
Then refresh or refocus Messages to pick up updated contacts and photos.

Once connected, enable startup when logging in:

```sh
systemctl --user enable --now verdigris-sync.service
```

## Validation and remaining work

```sh
cargo test --manifest-path rust/verdigris-apps/Cargo.toml
cargo clippy --manifest-path rust/verdigris-apps/Cargo.toml --all-targets -- -D warnings
dbus-run-session --config-file=rust/verdigris-apps/tests/dbus.conf -- xvfb-run -a -s '-screen 0 1280x850x24' python rust/verdigris-apps/tests/smoke.py
```

HTTP integration tests use a local fake server, including >100 missed messages,
a failure partway through pagination, GUID deduplication, receipts, new-conversation and
multipart send payloads, attachment caching/interruption/size limits, and password redaction.
The GTK smoke test requires Python GObject bindings, Xvfb, and ImageMagick. It exercises the split view, multiline drafts, GIF popover, new-message composer, and cached image previews with fixture data. GIF transfer tests cover invalid responses, size limits, and temporary-file cleanup.
No automated test sends messages to a real person or places calls.

This is a preview, not the complete replacement: group creation, reactions,
threaded replies, edits, full-history search, persistent drafts,
full history pagination, integrated Bluetooth setup, and UI polish remain. Live Mac,
Socket.IO compatibility, and call-audio acceptance testing require the configured Mac
and paired iPhone. The Mac server has not been installed remotely by this project.

Protocol references: [REST API](https://github.com/BlueBubblesApp/bluebubbles-docs/blob/master/server/developer-guides/rest-api-and-webhooks.md),
[HTTP routers](https://github.com/BlueBubblesApp/bluebubbles-server/tree/master/packages/server/src/server/api/http/api/v1/routers),
[Socket.IO events](https://github.com/BlueBubblesApp/bluebubbles-server/blob/master/packages/server/src/server/events.ts).
