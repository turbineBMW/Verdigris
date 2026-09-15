# Native checklist prototype results

Tested 2026-09-12 on the development Mac mini running macOS 26.5.2, driven from
Linux over the existing SSH connection. All live writes were confined to the
registered note `Verdigris Checklist Probe 25e48496cb51`.

## Verified

| Experiment | Outcome |
| --- | --- |
| Native checklist creation | Selected five fixture paragraphs via Accessibility and used Notes' native checklist command. Five distinct checklist identifiers appeared in stored metadata. |
| Duplicate labels | Checked the second `Repeated item`; the first remained unchecked. |
| Desired-state operation | Repeated `set checked` and `set unchecked` requests were no-ops when already satisfied. |
| Nesting | Indented `Nested child` and toggled it while retaining its identity and indentation. |
| Unicode | Selected, checked, and edited labels containing emoji and accented text using UTF-16 ranges. |
| Automatic sorting | Checked entries moved down. Subsequent requests still located the intended item by identifier and its newly read range. |
| Native text edit | Replaced a checked item's label through `AXSelectedText`; its native checkbox identifier, checked state, and surrounding items survived. |
| Rich content around edits | Added bold text to a separate item and a small text attachment fixture. Later edits preserved the bold markup and stored attachment references. Saving the attachment back out of Notes returned the original file bytes. |
| Stale changes | Rejected an old revision before dispatch. Also rejected an edit while an attachment import was changing the document. |
| Persistence | Quit and reopened Notes; text, checklist identifiers, state, indentation, and attachment references matched. |
| Linux GTK controls | Under Xvfb, a checkbox signal changed native Mac state and was reverted. A label's Save button changed `Final item` to `Edited from Linux 🐧` while retaining the item identifier and state. |
| iCloud to iPhone | The user confirmed that the iPhone showed `Edited again 🧪 café` checked with the checked nested child beneath it. |
| Decoder/guard tests | Eight Python unit tests passed on Linux. |

Notes exposed two attachment instances/references after the single fixture import
settled. Subsequent checklist edits preserved both. This experiment establishes
preservation of this fixture, not general support for every attachment type.

## Findings for production integration

1. **Targeted native edits work.** A whole-note HTML replacement is unnecessary
   for checkbox toggles and single-line item-label edits on this macOS version.
2. **Checklists need structure in the API.** Return item IDs, checked state,
   indentation, UTF-16 ranges, and a revision. Send desired checked state rather
   than a blind toggle. Resolve the latest range on the Mac before dispatch.
3. **Notes has asynchronous UI and storage changes.** Read back after writes;
   do not treat a dispatched key event as a successful save. Attribute imports
   and automatic sorting can change a revision and item positions.
4. **Modal dialogs matter.** A first-use checkbox-sorting sheet blocked the
   initial indent attempt. The helper now rejects edits while a sheet is open.
   The sheet was dismissed by the user before automation resumed, so the added
   automated sort-prompt response itself has not been live-tested.
5. **Use a stable signing identity before extending the production bridge.**
   Rebuilding the ad-hoc helper changed its code hash. Toggling Accessibility
   off/on retained the old requirement; removing and re-adding its entry worked.
   The production bridge was not rebuilt for this experiment.

## Remaining limits

- The experiment is scoped to one disposable note. The preview is not the main
  Notes app, and the production bridge still has no edit/checklist endpoints.
- General rich-text editing, inserting/removing checklist items, tables,
  shared-note concurrent edits, locked-screen operation, alternate keyboard
  layouts, and other macOS releases are not validated.
- Checking a parent with nested children may have native effects on the subtree;
  that behavior needs an explicit API/UI contract and separate tests.
- A revision check followed by an Accessibility operation is not an atomic
  compare-and-swap. Production needs a serialized write queue, narrow targets,
  conflict handling, and verification; simultaneous iCloud edits remain a race
  to account for.
- The user confirmed Mac-to-iPhone sync. A separate iPhone-to-Linux mutation
  round trip has not been tested.

The fixture and separate preview/helper are intentionally left available for
hands-on testing. Run `python3 probe.py cleanup` in the Mac prototype directory
when finished; it removes only the recorded fixture, including Recently Deleted
when available, and verifies absence before dropping its manifest.
