# Verdigris — Backlog

Park ideas here so they don't derail active work. Done items stay checked for context.

Verdigris is a fork of [Blue](https://github.com/gutbash/blue). Native app work and
inherited backend work are tracked separately below.

## Native apps

- [x] Phone and Messages apps with internal Settings and a shared sync worker
- [x] Bubo-style layout, multiline composer, emoji/GIF picker, animated previews
- [x] Custom icons, release installer, and Blue upgrade compatibility
- [ ] Group creation, reactions, threaded replies, and edits in the native app
- [ ] Persistent drafts, full history pagination, and full-message search
- [ ] Integrated Bluetooth setup and call-audio acceptance checks
- [ ] Flatpak packaging for the native apps

## Inherited backend work

- [x] MAP send / iMessage send (MAP `PushMessage` + desktop CLI/UI)
- [x] HFP HF role (oFono + PipeWire) — answer, decline, dial
- [x] Qt/QML desktop app (`verdigris-qt`) replacing GTK
- [x] Direct iMessage transport (`verdigris-imessage` / rustpush)
- [x] iOS backup import (`backup-sync`) for full history + attachments
- [x] SQLite message store (`messages.sqlite`) — replaces dual JSONL
- [x] Paged conversation load + FTS5 search in the sidebar
- [x] Cross-transport dedupe (MAP ↔ iMessage)
- [x] Arbitrary-emoji tapbacks + multi-person reaction clusters
- [x] Delivery/read/edit state persisted across restart

## Polish / reliability

- [ ] Reconnect-on-suspend-resume (laptop sleep breaks BT sessions)
- [ ] Notification dismissal sync — dismiss libnotify → mark SMS read on phone
- [ ] Graceful toggle-disabled handling — log + back off when iPhone toggles are off
  (avoid crash-looping the unit)
- [ ] Better contact resolution for international numbers (E.164 normalization)
- [ ] `sms-list` / live pull of folders beyond default inbox when useful

## Store / history

- [ ] Encrypted SQLite for message store (SQLCipher / at-rest)
- [ ] Optional retention / vacuum policy for very large histories
- [ ] Scheduled `backup-sync --if-available` unit (Wi-Fi backup path)

## Packaging / multi-device

- [~] Flatpak for the UI — draft in `packaging/flatpak/` still targets removed GTK;
  needs a native-app packaging design (backend stays outside the sandbox)
- [ ] Multi-device support (currently one `VERDIGRIS_MAC`)
- [ ] iOS version regression test matrix

## Won't do (unless a real protocol path appears)

- Per-app reply via ANCS (ANCS is read-only)
- FaceTime / conference calls over HFP
- Replaying Apple traffic missed while APNs was down (use backup import)
- Global-menu Search submenu (Plasma dbusmenu made it unreliable; sidebar search is the product)
