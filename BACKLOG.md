# iphonebridge — Backlog

Park ideas here so they don't derail active work. Done items stay checked for context.

## Done (recent)

- [x] MAP send / iMessage send (MAP `PushMessage` + desktop CLI/UI)
- [x] HFP HF role (oFono + PipeWire) — answer, decline, dial
- [x] Qt/QML desktop app (`iphonebridge-qt`) replacing GTK
- [x] Direct iMessage transport (`ib-imessage` / rustpush)
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
  needs Qt rewrite (daemon stays native)
- [ ] Multi-device support (currently one `IPHONEBRIDGE_MAC`)
- [ ] iOS version regression test matrix

## Won't do (unless a real protocol path appears)

- Per-app reply via ANCS (ANCS is read-only)
- FaceTime / conference calls over HFP
- Replaying Apple traffic missed while APNs was down (use backup import)
- Global-menu Search submenu (Plasma dbusmenu made it unreliable; sidebar search is the product)
