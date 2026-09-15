# Native Apple Notes checklist experiment

This is an isolated proof of concept, not a production bridge extension. It uses
one disposable, registered note and a separate Mac helper app. It does not
rebuild iCloudBridge or change the installed Linux Notes reader.

## Design

- `checklist_store.py` opens `NoteStore.sqlite` read-only, including SQLite's live
  WAL, and decodes checklist identifiers, state, indentation, and UTF-16 ranges.
  It rejects protected/non-fixture notes, malformed data, and ambiguous targets.
- `ChecklistProbe.swift` runs in a logged-in Mac GUI session. It selects text
  through Accessibility and invokes native Notes commands. For text edits it
  uses `AXSelectedText`, avoiding whole-document HTML replacement.
- `probe.py` coordinates the experiment. Check requests carry an item identifier,
  desired boolean state, and document revision. The driver checks the revision
  again after finding the editor and reads stored state after every toggle.
- Jobs/results live in the user's mode-0700 Application Support directory, with
  mode-0600 files. The helper has no network listener. Commands are consumed
  before execution and are not automatically replayed after a timeout.

The field definitions are based on the
[Apple Notes parser schema](https://github.com/threeplanetssoftware/apple_cloud_notes_parser/blob/master/proto/notestore.proto).
The native commands are documented in
[Apple's Notes keyboard shortcuts](https://support.apple.com/en-ae/guide/notes/apd46c25187e/mac).
The Python decoder is implemented here using the standard library.

## Run on the Mac

Copy this directory to the Mac, then:

```sh
bash build.sh
python3 probe.py create
python3 probe.py inspect
python3 probe.py convert
python3 probe.py read
```

Grant **Accessibility** to **Verdigris Checklist Probe** using its setup window.
The driver also needs Notes Automation and read access to the Notes database.
On the development Mac, the SSH session already has the latter two permissions.
The compiled GUI helper's permissions are separate from SSH's permissions.
Rebuilding the ad-hoc-signed helper can invalidate its Accessibility grant.

The `create` command registers the note ID and unique title in `fixture.json`.
Use the exact item ID and revision returned by `read`:

```sh
python3 probe.py set --item ITEM_ID --checked true --revision REVISION
python3 probe.py edit --item ITEM_ID --text 'New label' --revision REVISION
python3 probe.py cleanup
```

Cleanup removes only the recorded fixture, including Recently Deleted when
Notes exposes it, and verifies its absence before removing the manifest.
Quit the helper when the experiment is finished. There is no autostart entry.

For diagnostics, `probe.py job` accepts a JSON object on stdin with `operation`
and arguments. Mutation operations require an exact `expectedText` match with
the displayed fixture, plus UTF-16 `location` and `length`. Supported operations
include `select`, `checklist`, `toggle`, `indent`, `outdent`, `replace`, `bold`,
`moveUp`, and `moveDown`. A dispatched operation is not proof of a saved edit;
the caller must verify the Notes database. Modal dialogs block edits. The
separate `dismissSortPrompt` operation only handles a checked-item sorting prompt.

## Linux preview

After the Mac fixture is prepared, run from the repository root on Linux:

```sh
python3 macos/notes/prototype/preview.py --mac 192.168.1.150
```

This requires Python's GTK 4 bindings and the existing passwordless SSH setup.
The preview can check/uncheck and edit the labels of the registered fixture's
native items. It passes JSON over SSH stdin, serializes requests, and requires
a refresh after an error. It does not add a launcher or change the installed
Notes app. The Mac driver is expected in `~/Projects/verdigris-checklist-prototype`.
Keep the fixture until finished with the preview, then run `probe.py cleanup`
on the Mac and close both preview and helper windows.

## Tests and limits

On Linux or macOS:

```sh
python3 -m unittest discover -s macos/notes/prototype -p 'test_*.py' -v
```

This checks Unicode ranges, duplicate labels, styling-run coalescing, malformed
protobuf rejection, a live SQLite WAL, protected/non-fixture rejection, and
conflict/no-op guards.

This prototype has no atomic transaction spanning SQLite, Accessibility, and
iCloud. A change can still arrive between the last check and the native command.
It requires an available GUI session, uses US-layout virtual key codes, and has
not been designed for concurrent callers. Its revision token describes local
stored content, not completion of an iCloud upload. Full-editor integration
needs a serialized Mac operation queue, capability reporting, conflict UX,
and additional testing of shared notes, tables, attachments, locked sessions,
and macOS versions. See `RESULTS.md` for the actual live test outcomes.
