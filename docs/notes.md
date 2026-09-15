# Apple Notes through the Mac

**Notes** (`verdigris-notes`) uses the same Mac companion, connection settings,
and SSH tunnel as Reminders. There is no second server or account sign-in on Linux.

The app provides a native reader, folder filtering, search across note
titles and text, creation and editing of ordinary text notes, and native checklist controls.
Check a box to change its state, or click its pencil to edit the item text.
Supported text-only notes open as editable title and body fields in the main window;
changes save automatically after a short pause in typing, and Ctrl+E focuses the
title. Ctrl+N creates a note, and Ctrl+R
refreshes the full collection. The Mac watches Notes' local SQLite and WAL files
with a vnode write source, uses FSEvents to re-arm watches when those files are
replaced, debounces each write burst, and publishes a content-free Notes generation
on the companion's authenticated change stream. The Linux client then
reconciles through the ordinary REST API; note text and titles never travel over
the socket. A 15-minute safety refresh remains while the window is open, but the
old 15-second focused-note polling is gone. Invalidations that arrive while the
window is inactive or an edit is saving are retained and reconciled afterward.
Notes keeps a private snapshot for offline reading. New notes are created in the
selected folder, or the Mac's default Notes folder when browsing all folders.

## Mac extension

The upstream iCloudBridge server does not include Notes. Verdigris supplies
[NotesController.swift](../macos/notes/NotesController.swift) and a source patcher.
They extend the same upstream version documented in [Reminders setup](reminders.md).

Copy `macos/notes`, `macos/push`, and `macos/signing` to the Mac, keeping them beside each other.
Prepare the persistent local signing identity once:

```sh
python3 /path/to/macos/signing/sign.py
```

Run the following in **Terminal on the Mac**, approving macOS's authorization
prompt. It trusts this local identity for code signing only, in the user's trust
settings; it does not add a system or web certificate authority.

```sh
security add-trusted-cert -r trustRoot -p codeSign \
  -k "$HOME/Library/Application Support/VerdigrisSigning/local-builds.keychain-db" \
  "$HOME/Library/Application Support/VerdigrisSigning/local-builds.der"
```

Build and install with the signing wrapper:

```sh
bash /path/to/macos/notes/build.sh ~/Projects/verdigris-icloudbridge
osascript -e 'tell application id "com.cleverdevil.iCloudBridge" to quit'
# Wait for the bridge to quit before replacing its app bundle.
ditto ~/Projects/verdigris-icloudbridge/build/iCloudBridge.app ~/Applications/iCloudBridge.app
open ~/Applications/iCloudBridge.app
```

Open Apple Notes once and complete any first-run setup. On the first Notes
request, allow the bridge to control Notes if prompted. Permissions can be
checked in **System Settings → Privacy & Security → Automation**. Permission
granted to `sshd` for command-line diagnostics is separate from a GUI app's
permission. For checklist support, add the installed **iCloudBridge.app** to
**Full Disk Access** (read-only access to Notes' local database and its live-change
monitor) and
**Accessibility** (targeted native edits). Ordinary reading and creation use
Apple Events and remain available without these extra permissions.

The wrapper signs with the same local certificate and designated requirement
on every build. The first switch from the previous ad-hoc signature requires
granting permissions again; a subsequent signed rebuild was verified to retain
Reminders, Accessibility, and Full Disk Access. Keep the signing identity in
`~/Library/Application Support/VerdigrisSigning`. Running only the upstream
build script produces an ad-hoc signature and can reset permissions again.

Open **Notes** on Linux after running `./install.sh`. The shared connection is
under **Settings → Reminders and Notes connection**, retaining the existing
`reminders.json` configuration and keyring credential for compatibility.

## Content and permissions

- The bridge exposes all folders and notes visible through Apple Notes scripting
  in the logged-in Mac account. The Reminders list selection does not filter Notes.
  This includes Recently Deleted when it is present in the scripting interface.
- Native checklist state is loaded when a note is opened, with checkboxes,
  indentation, completion counts, and item-text editing. Other content is shown
  as text, with attachment names; rich presentation remains in Apple Notes.
- Protected-note titles appear, but their contents and attachments are skipped,
  even if the protected note was unlocked in Notes. The Linux client also
  discards content if a bridge response labels a note as protected.
- Ordinary text-only notes support title and body edits, including blank lines,
  Unicode, and new paragraphs. Unchanged graphemes retain their native formatting.
  The editor provides text controls; formatting commands remain in Apple Notes.
  Notes containing attachments, tables, native lists, or unsupported paragraph
  types do not offer whole-note text editing. Each edited note is limited to
  64 KiB; changes spanning more than 8,192 changed-region graphemes or 24 separate
  replacements must be split into smaller saves.
- Supported checklist items can be checked/unchecked and their single-line text
  edited. Parent items with nested children cannot be toggled yet. Protected
  notes and unsupported structures remain read-only. General rich-text
  editing and adding/removing checklist items are outside this version.
- Shared notes use the same text/checklist controls when the current user has
  accepted the share with edit permission. View-only access, pending permission
  changes, and unrecognized share metadata remain read-only. The reader labels
  shared notes. Existing content limits still apply to shared notes.
- New notes accept a single-line
  title and up to 1 MB of text. The bridge escapes text before creating the HTML
  body, preserving literal angle brackets, quotes, Unicode, and line breaks.
- Notes requests use the bridge's existing authentication middleware. Use the
  existing SSH tunnel or another encrypted connection, as with Reminders.
- Writes are not retried automatically. If a creation request fails ambiguously,
  the dialog retains the draft and requires closing/refreshing before retrying.
  Text and checklist edit failures retain the draft and require reviewing the
  current version before saving again. When a targeted refresh finds that an
  open note changed on the Mac, the inline editor keeps the draft and shows the
  current Mac version separately (Ctrl+Shift+R accepts that version as the new
  save base without replacing the draft). Browse/search and previously loaded checklist state
  remain available offline; all writes require a working bridge.
- Native edits operate in the Mac's logged-in GUI session and bring Notes to
  the front. Modal dialogs and extra Notes windows block writes. They should
  be dismissed/closed on the Mac before retrying. An edit automatically wakes a
  sleeping or headless display, then verifies the GUI session is unlocked. macOS
  can report a non-authenticating headless session as temporarily locked even when
  screen locking is disabled; the wake check clears that state before editing. A
  genuinely password-locked session still requires unlocking on the Mac; reading
  remains available. Waking uses a temporary remote-user-activity assertion and
  follows the Mac's display sleep settings afterward. The Mac itself must remain
  awake and logged in.

Snapshots are mode 0600 in `$XDG_DATA_HOME/verdigris/notes-<server-hash>.json`,
using the normal XDG default when unset. They contain note titles and unprotected
text plus loaded checklist state, not database exports or attachment files. Snapshots are separated
by server URL. Switching Apple accounts behind the same URL requires deleting
the old snapshot. A note protected or deleted elsewhere can remain in an old
offline snapshot until the next successful refresh.

## API and validation

The extension adds `GET /api/v1/notes` (folders, notes, and default folder),
`POST /api/v1/notes` (`folderId`, `title`, `text`), and
`DELETE /api/v1/notes/{id}`. Deletion is available at the API for cleanup but is
not exposed by the initial Linux UI. IDs are opaque URL-safe encodings of Notes
identifiers. Checklist routes are:

- `GET /api/v1/notes/{id}/checklist`: current note, revision, items and edit
  capabilities. Unsupported content returns a read-only result with a reason.
- `PATCH /api/v1/notes/{id}/checklist/{itemId}`: `operationId` (UUID), `revision`,
  and exactly one of `checked` (desired boolean) or `text` (single-line label).
- `PATCH /api/v1/notes/{id}/text`: `operationId`, `revision`, `title`, and `text`
  (body). The detail response adds `canEditText`, `textReason`, and `shared`;
  older bridges without text capabilities keep text editing disabled.

The shared authenticated `GET /api/v1/changes` WebSocket sends only
`version`, a process `epoch`, and monotonic `reminders`/`notes` generations.
The filesystem monitor is an invalidation hint rather than a record-level Notes
API: file replacement and watcher resets invalidate the full Notes collection,
reconnects force a reconciliation, and the 15-minute timer is the final safety net.

The bridge reads SQLite using a read-only transaction, verifies the Core Data
store identity, protected/deletion flags and ordinary-folder type (excluding
Recently Deleted), and decodes bounded gzip/protobuf
data. It never writes the Notes database. Sharing permissions come from the
local CloudKit share cache (`ZSERVERSHAREDATA`), securely decoded with `CKShare`.
Direct shares and ancestor-folder shares are checked in the same SQLite snapshot;
pending share changes and disagreement with AppleScript sharing state block edits.
Revisions include the sharing metadata and folder ancestry so a permission change
invalidates an earlier draft revision. The native editor must also report its
selected text as writable immediately before dispatch. Native edits select the note by ID,
check the editor text, selection and revision, then invoke the native checkbox
menu action or replace the changed text span through Accessibility. A readback
checks checklist identities, states, surrounding text and attachment references.
Ordinary text saves plan separate grapheme replacements for the title and body,
apply them from the end, and verify the complete expected text after each step.
A failure partway through a multi-step save can leave some changes applied; the
client preserves the draft and requires reviewing the current note before retrying.

Only one Notes write runs at a time. Operation UUIDs are reserved in private
files under `~/Library/Application Support/iCloudBridge/notes-operations` before
dispatch, preventing replay across process restarts. Conflicts return 409;
denied edit permissions return 403, locked Mac sessions return 423, and unconfirmed writes return 503. There is still no atomic transaction spanning
iCloud, SQLite and the GUI, so concurrent external edits remain a limitation.

AppleScript source is fixed; user data is passed with typed Apple Event
parameters. Server errors omit note contents. Script operations run on the Mac's
main thread, so large libraries may briefly delay other companion requests.
The client bounds responses to 16 MiB and retains the prior snapshot on failure.

```sh
cargo test --manifest-path rust/verdigris-apps/Cargo.toml --test notes --test reminders --locked
pytest -q tests/test_native_install.py
cargo build --manifest-path rust/verdigris-apps/Cargo.toml --bins --locked
dbus-run-session -- xvfb-run -a --server-args='-screen 0 1100x850x24' \
  python3 rust/verdigris-apps/tests/notes_smoke.py
dbus-run-session -- xvfb-run -a --server-args='-screen 0 1100x850x24' \
  python3 rust/verdigris-apps/tests/notes_checklist_smoke.py
dbus-run-session -- xvfb-run -a --server-args='-screen 0 1100x850x24' \
  python3 rust/verdigris-apps/tests/notes_text_smoke.py
# On macOS:
python3 /path/to/macos/notes/tests/store_test.py
python3 /path/to/macos/notes/tests/sharing_test.py
```

These tests cover the API contract, literal text payloads, protected content,
cache isolation and file permissions, native request validation, Unicode ranges,
conflict recovery with retained drafts, UI creation, and offline behavior.
The GTK test uses disposable data and requires xdotool and ImageMagick.

Live validation on macOS 26.5.2 loaded the library through the SSH tunnel and
created a temporary note with literal HTML characters, quotes, Unicode, and
blank lines. Its text was read back unchanged, and the temporary note was deleted
and then removed from Recently Deleted. The existing Reminders lists remained accessible.

Live invalidation validation on macOS 26.6.2 observed a disposable note's
generation advance once after creation and again after deletion. The test also
showed that FSEvents alone does not report writes to Notes' long-lived mmap'd WAL;
the production monitor therefore combines direct vnode write events with FSEvents
re-arming when the database files are replaced. The disposable note was removed
from Recently Deleted after the check.

## Native editing validation

An isolated [checklist prototype](../macos/notes/prototype/README.md) demonstrates
native checkbox toggles and item-label edits through Mac Accessibility, with
checklist metadata read from the Notes database. See the
[live results and remaining limits](../macos/notes/prototype/RESULTS.md).
The verified operations are now integrated into the reader and production
bridge. The separate prototype remains available as a development tool.

The production pass verified native toggles and label edits on the disposable
fixture, stale-revision and duplicate-operation rejection, Recently Deleted
editing refusal with disposable-note cleanup, and permissions
retained across a signed rebuild. Before automatic wake was added, reading remained available after reboot while
a sleeping display caused checklist edits to return 423 without changing the
document revision. The later display-sleep test confirmed this flag also covers
a session that wakes without authentication. After unlocking, the installed bridge toggled and restored
a native checkbox, then added and removed a joined family emoji in its label;
each operation passed the bridge's readback checks. Read-only library validation decoded 109
checklist items; seven notes had supported editable checklists, forty were text
notes, six were shared, three protected, and four had unsupported structure.
The counts include the retained disposable fixture. No personal notes were
changed by the live tests.

Ordinary-note validation used two disposable notes with identical text and native
bold, underline, and heading formatting. Title changes, edits at separate body
locations, joined emoji, and added paragraphs were saved and restored. The
untouched twin retained its text, and both notes' native HTML matched after the
round trip. Stale revisions and replayed operation IDs were rejected. Display
sleep was tested separately from authentication: an explicit edit wakes the
display and rechecks the session, without changing lock settings.

The ordinary-text editor was enabled for 21 existing notes in the current library.
Both temporary text notes were removed, including from Recently Deleted, after
validation; the older checklist probe note was retained.

## Shared-note validation status

Shared-note editing is enabled, with live collaboration testing deferred at the
user's request. Read-only inspection confirmed editable permissions in existing
shares. Synthetic CloudKit archives test editable, view-only, pending, removed,
unknown, and missing-current-user cases. SQLite tests cover inherited permissions,
permission changes invalidating revisions, and invalid folder ancestry. The GTK
test verifies that revoking edit permission blocks autosave while preserving the
draft, and that restoring permission resumes autosave after review.

Native text and checkbox saves were regression-tested on disposable private notes.
No existing shared note was modified during development. Saves verify local Notes
state; simultaneous changes from another Apple account, remote permission
revocation, and subsequent iCloud convergence have not been live-tested.

`Verdigris Shared Notes Test 9c22e88f` is retained for later testing. Its fixture
manifest is stored privately at `~/.local/state/verdigris/probes/shared-note.json`.
When another account is available, share this note with edit permission, verify
edits from both accounts, and try saving a stale Linux draft after a collaborator
changes the note. A collaborator-owned view-only test should also confirm that
permission revocation prevents further edits. No invitation was sent by the agent.
