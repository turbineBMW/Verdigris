# Changelog

## [Unreleased]

Two subsystems that did not exist at 0.1.0, plus the app that renders them.

### Performance
- **Scroll and live updates no longer hitch on large histories** (~200k
  events / ~20k threads). Receipts and tapbacks repaint in place instead of
  rebuilding the message list; disk sync no longer reloads the open chat on
  every store write; runtime thread-summary refresh only pulls the sidebar
  window; the sidebar shadow sits on a static plate so scrolling does not
  re-rasterize the list; avatar PNGs and SQLite page cache/mmap are
  memoized for cold reads. (Message-list `reuseItems` and solid edge-fade
  overlays were tried and reverted — they stretched bubbles full-width and
  painted black bars over the frosted glass fade.)

### Changed
- **Message history is SQLite** (`~/.local/state/iphonebridge/messages.sqlite`).
  The daemon (`SqliteSink`) and `backup-sync` write one store; the Qt UI and
  `sms-list --source local` read it. Legacy `events.jsonl` / `backup_events.jsonl`
  are imported once on first open. Incremental UI sync uses a max-id cursor plus a
  `write_seq` meta counter so upserts/edits are not missed.
- **Paged conversations + FTS search.** Cold start loads the thread list only; opening a
  chat pages recent messages from SQLite (scroll up for older). Search is the **sidebar
  field** (type-as-you-go FTS5 over bodies); **Edit → Find…** / Ctrl+F focuses that
  field. Hits replace the thread list (one row per match, highlighted preview) and open
  the bubble. There is no separate Search menu or Find dialog.

### Added
- **Direct iMessage transport** (`ib-imessage`, Rust) — speaks to Apple through
  rustpush over its own systemd unit, giving guids, replies, edits, unsends,
  tapbacks, typing indicators, attachments and receipts. MAP carries none of
  these; it delivers incoming text and nothing more.
- **Qt/QML desktop app** (`iphonebridge-qt`), replacing the GTK4 app.
- **iOS backup import** (`iphonebridge backup-sync`), normalizing `sms.db` into
  the same event shape the daemon emits.
- Cross-transport deduplication: the same message arrives over MAP and iMessage
  with no shared identifier, so matching is heuristic and only ever applied
  across differing transports.
- Delivery/read state and edit history persisted as `message_state` rows, so
  captions and post-edit text survive a restart.
- Any emoji can be sent as a tapback, not only the classic six: the bubble
  menu's reaction row gained a picker with search over the whole emoji table.
- A message shows every tapback on it, one per person, as an overlapping
  cluster of emoji. Reacting again replaces that person's; their removal takes
  only theirs off.

### Fixed
- True inline media (U+F00A in the body, e.g. "3-0 on my return") is drawn
  inside the text bubble. Free-standing Bitmoji/peels (U+FFFC) stay their own
  rows, with the placeholder glyph stripped from the caption.
- Group chats could show the last sender's name as the thread title; titles now
  prefer `chat_name` / participants.
- Pinned conversations keyed on display names forked after rename or MAP vs
  guid handle mismatch; pins and 1:1 thread keys migrate toward stable
  `tel:+…` (or email) handles.
- Duplicate threads for the same person (MAP transfer id vs `guid:…`) are
  folded on materialize when a guid handle exists.
- After the SQLite switch, a long-running daemon still writing JSONL left the
  UI missing the newest hour until restart — durable history is `SqliteSink`
  only; restart once after upgrade.
- App crash / fail-to-reopen from a `_watcher` reference used before create and
  from double `onContentYChanged` handlers fighting scroll restore.
- Tapback badges are the emoji itself rather than one of six icons, so an
  arbitrary-emoji reaction is no longer a special case pasted into an empty
  bubble — and taking a reaction back on the phone now clears the badge, where
  every "Removed a … from" used to render as the reaction it undid.
- A message could only hold one reaction: the second person to react replaced
  the first, silently, on both the live and the backup path.
- iOS 18 emoji tapbacks (`associated_message_type` 2006) were imported from
  `sms.db` as ordinary messages, so a conversation grew literal bubbles reading
  `Reacted 😋 to "…"` instead of badges — 737 of them in one real history.
- Tapbacks whose target was stored as `bp:<guid>` kept the prefix and matched
  nothing, so 323 more went nowhere.
- A live emoji tapback carried rustpush's whole clause as its verb, which put
  a stray "to" on the badge.
- Reactions arriving over the native transport were matched to their target by
  quoted text, ignoring the exact guid the transport supplies.
- Right-clicking a second message while a bubble menu was open only dismissed
  the first one, so every other message looked like it could not be reacted to.
  The menu is no longer modal and the page dismisses it, leaving the press free
  to reach the bubble under it.
- The unread dot on a pinned tile sat out at the tile's edge and pushed the
  thread name off centre.
- A reset APNs connection left the helper alive and serving with nothing behind
  it: sends still worked and status still read "available" while no inbound
  message arrived again. The helper now exits so systemd rebuilds the
  connection, and the daemon treats the loss as loss of transport.
- Desktop notifications never closed on the iMessage path. The only automatic
  close was a BlueZ MAP property change, which an iMessage does not produce.
- Cleared unread badges (especially on pinned tiles) came back after restart:
  cold-start recount compared ISO timestamps as strings, so a UTC-spelled
  earlier message sorted after a local-offset read mark and looked new.

### Removed
- The GTK4/libadwaita app and its desktop entry.
- The tapback icon SVGs (two mirrored sets of seven), now that the badge draws
  the emoji.
- Global-menu **Search** submenu / standalone Find dialog (search lives in the
  Messages sidebar only).

## [0.1.0] — 2026-05-19

First tagged release. Working iphonebridge daemon on Pop!_OS 24.04
against iPhone 16 Pro Max running iOS 26.5.

### Confirmed working
- Real-time SMS + iMessage notifications via MAP MNS push
- Outgoing SMS + iMessage send via MAP `PushMessage` — iOS auto-routes
  as iMessage when the recipient is iMessage-capable
- 1000+ contacts pulled via PBAP, cached in SQLite, name-resolved for
  incoming messages
- systemd user service for autostart, graceful degradation when iPhone
  toggles are off, automatic retry every 60s
- DBus service `com.gabriel.iphonebridge.Messages1` with Send,
  ListRecent, IsHealthy methods
- CLI: `run`, `doctor`, `pair-setup`, `sms-list`, `sms-send`,
  `contacts-sync`, `version`

### Documented constraints (won't change)
- No iMessage attachments / reactions / read receipts / typing
  indicators (MAP doesn't expose them)
- No group iMessage / MMS / RCS (MAP is 1:1 only)
- No outgoing call audio routing (HFP HF role — Phase 2c)

## [0.4.2] — 2026-05-20

### Verification codes auto-copied to the clipboard

- When an incoming text carries a one-time / 2FA code, the daemon detects it
  and copies it straight to the system clipboard, with a short "Code copied"
  notification — paste with Ctrl+V, no reaching for the phone. New
  `ClipboardSink`.
- Detection requires a verification keyword *and* a 4-8 digit number, so an
  ordinary text that just happens to contain a number doesn't trigger.
- Uses `wl-copy` (Wayland) or `xclip` / `xsel` (X11) — install `wl-clipboard`
  for the Wayland path.

## [0.4.1] — 2026-05-20

### Sent messages in conversation history

- The daemon now records every message sent through iphonebridge (the UI
  compose box or `iphonebridge sms-send`) to `events.jsonl` as a `sms_sent`
  event, and broadcasts a `MessageSent` signal on `Events1`. The UI threads
  these in, so a conversation shows both sides — incoming **and** the replies
  you sent from the desktop.
- No desktop notification fires for your own sent messages.
- Note: messages composed on the iPhone itself remain invisible — iOS does
  not expose sent content over MAP (the `sent` folder is empty, and no MNS
  push fires for outgoing). This was verified empirically; see the commit.

## [0.4.0] — 2026-05-20

### Phase 2d — GTK4 / libadwaita desktop app

- **`iphonebridge-ui`** — a standalone GTK4 / libadwaita app, separate from
  the daemon, talking to it over D-Bus. Four surfaces:
  - **Messages** — SMS/iMessage threads with history and a compose box
  - **Notifications** — a live feed of per-app ANCS notifications
  - **Calls** — a dialer plus answer / hang-up controls for active calls
  - **Setup** — daemon health, data counts, and the iPhone-toggle checklist
- New `src/iphonebridge/ui/` package; `DaemonClient` subscribes to the
  daemon's live signals and reads history from `events.jsonl`.
- Daemon broadcasts a live event feed on a new D-Bus interface
  `com.gabriel.iphonebridge.Events1` (`MessageReceived`, `MessageSeen`,
  `AncsNotification` signals) for the UI to consume.
- `data/` — `.desktop` entry, AppStream metainfo, and an app icon.

## [0.3.0] — 2026-05-20

### Phase 2c — HFP Hands-Free calls

- **Take and place iPhone calls on the laptop.** New `src/iphonebridge/hfp/`
  subsystem: call control runs through oFono (`org.ofono`, system bus), and
  call audio (SCO) rides PipeWire's oFono HFP backend.
- Incoming calls raise a desktop notification with **Answer / Decline**
  buttons; caller ID is resolved against the contacts cache.
- New CLI: `call <number|contact>`, `hangup`, `calls`, and `hfp-enable`
  (writes the WirePlumber config that routes HFP through oFono).
- New D-Bus interface `com.gabriel.iphonebridge.Calls1` — `Dial`,
  `AnswerCall`, `HangupCall`, `HangupAll`, `ListCalls`, and a
  `CallStateChanged` signal.
- Daemon: sinks now initialise independently of the MAP/PBAP sessions, so
  ANCS and call notifications reach the desktop even in degraded mode.
- Empirically confirmed against iPhone 16 Pro Max / iOS 26.5 — including
  **3/3 reliable outgoing dials**, which overturns the old "HFP HF can't
  reliably ATD on iPhone" assumption. See `spike/05b_hfp_ofono.py` and the
  HFP addendum in `spike/RESULTS.md`.
- `pyproject.toml`: `testpaths = ["tests"]` so a bare `pytest` no longer
  recurses (and hangs on) the whole repo tree.

## [Unreleased]

### Project-defining discoveries (2026-05-19, post-launch)

- **Incoming iMessage IS exposed via MAP on iOS 26.5 / iPhone 16 Pro Max**, labeled as `Type: sms-gsm` indistinguishably from SMS. This contradicts every prior Bluetooth-on-Linux writeup. Verified: sender (Contact B, confirmed iMessage thread, both on iPhone) sent "test-iphonebridge-XYZ123" → daemon received and rendered the body within ~2s.

- **Outgoing iMessage via MAP `PushMessage` ALSO works.** Tested via `spike/07_map_send.py`: constructed a minimal bMessage (originator + BENV-wrapped recipient VCARD), called `MessageAccess1.PushMessage(sourcefile, "telecom/msg/outbox", {})` — transfer completed, the iPhone's outgoing bubble appeared **blue** (iMessage) in the recipient thread.

Together: **iphonebridge is potentially the first free open-source Linux iMessage bridge that does not require a Mac relay**. README, BACKLOG, RESULTS.md updated accordingly.

### Phase 1 — MVP daemon (2026-05-19)
- Working iphonebridge daemon: BLE-advert / CoD startup dance, long-lived MAP + PBAP sessions, MAP MNS push subscription, bMessage parsing, SQLite contacts cache, libnotify + JSONL sinks.
- Typer CLI: `run`, `doctor`, `sms-list`, `contacts-sync`, `version`.
- systemd user service for auto-start.
- sudoers.d entry (`install-cod-sudoers.sh`) for passwordless `btmgmt class 4 8` so CoD survives reboots.
- End-to-end verified: SMS from a known contact arrives as a GNOME desktop notification within ~20 ms of the iPhone push.

### Phase 0 — Empirical spike (2026-05-19)
- Confirmed against iPhone 16 Pro Max / iOS 26.5: MAP read ✓, MAP MNS push ✓, PBAP (1957 contacts) ✓, HFP HF role partial (needs WirePlumber config work), ANCS deferred (needs BLE-only pairing flow incompatible with the BR/EDR pair MAP/PBAP need).
- Documented non-obvious findings in `spike/RESULTS.md`: the hidden-toggle dance, single-OBEX-session-per-fresh-obexd, SMS body in `Subject`, PBAP `Select` vs `SetFolder`, BR/EDR-vs-BLE pairing mutex for ANCS.
