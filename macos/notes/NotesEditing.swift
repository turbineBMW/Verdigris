import AppKit
import ApplicationServices
import CryptoKit
import IOKit.pwr_mgt
import Vapor

struct NoteDetailDTO: Content {
    let note: NoteDTO
    let revision: String?
    let items: [ChecklistItemDTO]
    let editable: Bool
    let reason: String?
    var canEditText = false
    var textReason: String? = nil
    var shared = false
}

struct ChecklistUpdateDTO: Content {
    let operationId: UUID
    let revision: String
    let checked: Bool?
    let text: String?
}

struct NoteTextUpdateDTO: Content {
    let operationId: UUID
    let revision: String
    let title: String
    let text: String
}

@MainActor
enum NotesEditing {
    static var writing = false
    static var consoleSessionAvailable: Bool {
        guard let session = CGSessionCopyCurrentDictionary() as? [String: Any] else { return false }
        return session[kCGSessionOnConsoleKey as String] as? Bool == true &&
            session[kCGSessionLoginDoneKey as String] as? Bool == true
    }
    static var sessionAvailable: Bool {
        guard let session = CGSessionCopyCurrentDictionary() as? [String: Any] else { return false }
        return session[kCGSessionOnConsoleKey as String] as? Bool == true &&
            session[kCGSessionLoginDoneKey as String] as? Bool == true &&
            session["CGSSessionScreenIsLocked"] as? Bool != true
    }
    static func requireSession() throws {
        guard sessionAvailable else {
            throw Abort(.locked, reason: "Unlock the Mac mini, then refresh Notes to edit notes")
        }
    }
    static func prepareSession() async throws -> IOPMAssertionID {
        // Display sleep can set CGSSessionScreenIsLocked even when waking needs
        // no authentication. Wake for an explicit edit, then inspect the session.
        var activity: IOPMAssertionID = 0
        guard IOPMAssertionDeclareUserActivity("Verdigris Notes edit" as CFString, kIOPMUserActiveRemote, &activity) == kIOReturnSuccess else {
            throw Abort(.serviceUnavailable, reason: "The Mac display could not be woken for editing")
        }
        do {
            for _ in 0..<20 {
                if sessionAvailable && CGDisplayIsAsleep(CGMainDisplayID()) == 0 { return activity }
                try await Task.sleep(nanoseconds: 100_000_000)
            }
            try requireSession()
            throw Abort(.serviceUnavailable, reason: "The Mac display is not ready; refresh before editing")
        } catch {
            IOPMAssertionRelease(activity)
            throw error
        }
    }
    static func sameText(_ lhs: String, _ rhs: String) -> Bool {
        // Swift String equality is canonically equivalent; AX ranges are UTF-16.
        NotesText.same(lhs, rhs)
    }
    static func attribute(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success else { return nil }
        return value
    }
    static func walk(_ element: AXUIElement, depth: Int = 0) -> [AXUIElement] {
        guard depth < 20 else { return [] }
        return [element] + (attribute(element, kAXChildrenAttribute) as? [AXUIElement] ?? [])
            .flatMap { walk($0, depth: depth + 1) }
    }
    static func check(_ result: AXError) throws {
        guard result == .success else { throw Abort(.serviceUnavailable, reason: "Notes could not select or edit this item; refresh before retrying") }
    }
    static func metadata(_ id: String) throws -> (NoteDTO, Bool) {
        let result = try NotesBridge.call("editingData", [id])
        guard let note = result.atIndex(1), let shared = result.atIndex(2) else { throw NotesStore.invalid() }
        return try (NotesBridge.note(note), shared.booleanValue)
    }
    static func detail(_ id: String) throws -> NoteDetailDTO {
        let (note, shared) = try metadata(id)
        if note.locked { return NoteDetailDTO(note: note, revision: nil, items: [], editable: false, reason: "Protected note") }
        do {
            let document = try NotesStore.read(id)
            guard sameText(document.text, note.text), document.folderID == (try NotesBridge.decodeID(note.folderId)) else {
                throw Abort(.conflict, reason: "Notes is updating this document or its folder; refresh again")
            }
            let access = document.sharingAccess.matching(shared: shared)
            let accessible = AXIsProcessTrusted()
            // Capability discovery must not reject macOS's transient lock flag.
            // A headless console can report ScreenIsLocked while screen locking
            // is disabled and the logical display is still awake. The mutation
            // path calls prepareSession(), which asserts user activity and then
            // requires the session to be genuinely unlocked before dispatch.
            let enabled = accessible && access.allowsEditing && consoleSessionAvailable
            var detail = NoteDetailDTO(note: note, revision: document.revision, items: document.items,
                editable: enabled, reason: access.reason ??
                    (!accessible ? "Grant iCloud Bridge Accessibility access on your Mac to edit notes" :
                    (enabled ? nil : "Log into the Mac mini, then refresh Notes to edit notes")))
            let plain = document.canEditText && sameText(document.text.components(separatedBy: "\n")[0], note.title)
            detail.shared = shared
            detail.canEditText = enabled && plain
            detail.textReason = detail.reason ?? (plain ? nil : "Text editing is available for text-only notes with ordinary paragraphs")
            return detail
        } catch let error as Abort {
            // The ordinary reader stays usable when private structure isn't available.
            return NoteDetailDTO(note: note, revision: nil, items: [], editable: false, reason: error.reason, shared: shared)
        }
    }
    static func document(_ id: String) throws -> NotesDocument {
        let (note, shared) = try metadata(id)
        guard !note.locked else { throw Abort(.forbidden, reason: "Protected notes cannot be edited") }
        let result = try NotesStore.read(id)
        let access = result.sharingAccess.matching(shared: shared)
        guard access.allowsEditing else { throw Abort(.forbidden, reason: access.reason ?? "Shared note cannot be edited") }
        guard sameText(result.text, note.text), result.folderID == (try NotesBridge.decodeID(note.folderId)) else {
            throw Abort(.conflict, reason: "Notes is still updating this document or its folder; refresh again")
        }
        return result
    }
    static func selected(_ id: String) throws -> Bool {
        try NotesBridge.call("editingSelection", [id]).booleanValue
    }

    static func requireWritableEditor(_ editor: AXUIElement) throws {
        var settable = DarwinBoolean(false)
        guard AXUIElementIsAttributeSettable(editor, kAXSelectedTextAttribute as CFString, &settable) == .success,
              settable.boolValue else {
            throw Abort(.forbidden, reason: "Apple Notes does not allow editing this document")
        }
    }

    static func dispatch(_ id: String, _ before: NotesDocument, _ item: ChecklistItemDTO, _ input: ChecklistUpdateDTO) throws {
        let edit = input.text.map { NotesText.replacement(old: item.text, new: $0, location: item.location) }
        try dispatchEdit(id, before, range: edit?.range ?? NSRange(location: item.location, length: 0), inserted: edit?.text)
    }

    static func dispatchEdit(_ id: String, _ before: NotesDocument, range editRange: NSRange, inserted: String?) throws {
        guard AXIsProcessTrusted() else { throw Abort(.forbidden, reason: "Grant iCloud Bridge Accessibility access on the Mac") }
        try requireSession()
        _ = try NotesBridge.call("showForEditing", [id])
        guard let notes = NSRunningApplication.runningApplications(withBundleIdentifier: "com.apple.Notes").first else { throw Abort(.serviceUnavailable) }
        let application = AXUIElementCreateApplication(notes.processIdentifier)
        AXUIElementSetMessagingTimeout(application, 2)
        let windows = attribute(application, kAXWindowsAttribute) as? [AXUIElement] ?? []
        guard windows.count == 1 else { throw Abort(.conflict, reason: "Close extra Notes windows on the Mac before editing") }
        let elements = walk(windows[0])
        guard !elements.contains(where: { attribute($0, kAXRoleAttribute) as? String == kAXSheetRole }) else {
            throw Abort(.conflict, reason: "Dismiss the dialog in Notes on your Mac, then refresh")
        }
        let editors = elements.filter { attribute($0, kAXRoleAttribute) as? String == kAXTextAreaRole }
        guard editors.count == 1, let editor = editors.first else {
            throw Abort(.conflict, reason: "Notes does not have exactly one document editor open; refresh before editing")
        }
        guard try selected(id) else {
            throw Abort(.conflict, reason: "Notes selection changed; refresh before editing")
        }
        guard let editorText = attribute(editor, kAXValueAttribute) as? String,
              sameText(editorText, before.text) else {
            throw Abort(.conflict, reason: "Notes editor is still updating; refresh before editing")
        }
        try requireWritableEditor(editor)
        try check(AXUIElementSetAttributeValue(editor, kAXFocusedAttribute as CFString, kCFBooleanTrue))
        var range = CFRange(location: editRange.location, length: editRange.length)
        guard let value = AXValueCreate(.cfRange, &range) else { throw NotesStore.invalid() }
        try check(AXUIElementSetAttributeValue(editor, kAXSelectedTextRangeAttribute as CFString, value))
        // Verify the selected range, document identity and revision after selection.
        var actual = CFRange()
        guard let selectedRange = attribute(editor, kAXSelectedTextRangeAttribute),
              CFGetTypeID(selectedRange) == AXValueGetTypeID(),
              AXValueGetValue(selectedRange as! AXValue, .cfRange, &actual), actual.location == range.location, actual.length == range.length,
              NSWorkspace.shared.frontmostApplication?.processIdentifier == notes.processIdentifier,
              let currentText = attribute(editor, kAXValueAttribute) as? String, sameText(currentText, before.text),
              try selected(id), try document(id).revision == before.revision else {
            throw Abort(.conflict, reason: "Note changed while preparing the edit; refresh before retrying")
        }
        try requireSession()
        try requireWritableEditor(editor)
        if let replacement = inserted {
            try check(AXUIElementSetAttributeValue(editor, kAXSelectedTextAttribute as CFString, replacement as CFString))
        } else {
            // Invoke the menu action by its key equivalent (not a layout-dependent virtual key).
            guard let rawMenu = attribute(application, kAXMenuBarAttribute) else { throw Abort(.serviceUnavailable) }
            let candidates = walk(rawMenu as! AXUIElement).filter {
                (attribute($0, kAXMenuItemCmdCharAttribute) as? String)?.lowercased() == "u" &&
                (attribute($0, kAXMenuItemCmdModifiersAttribute) as? NSNumber)?.intValue == 1 &&
                (attribute($0, kAXEnabledAttribute) as? Bool) == true
            }
            guard candidates.count == 1 else { throw Abort(.unprocessableEntity, reason: "Native checkbox command is unavailable on this Mac") }
            try check(AXUIElementPerformAction(candidates[0], kAXPressAction as CFString))
        }
    }

    static func reserve(_ operation: UUID) throws {
        let directory = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/iCloudBridge/notes-operations")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        // O_EXCL is the durable replay guard, including across process restarts.
        let path = directory.appendingPathComponent(operation.uuidString.lowercased()).path
        let fd = Darwin.open(path, O_CREAT | O_EXCL | O_WRONLY, 0o600)
        guard fd >= 0 else {
            if errno == EEXIST { throw Abort(.conflict, reason: "This edit was already submitted; refresh to check its outcome") }
            throw Abort(.serviceUnavailable, reason: "Cannot record edit; no change was sent")
        }
        guard fsync(fd) == 0 else { close(fd); throw Abort(.serviceUnavailable) }
        close(fd)
    }
    static func update(_ id: String, itemID: String, input: ChecklistUpdateDTO) async throws -> NoteDetailDTO {
        guard !writing else { throw Abort(.conflict, reason: "Another Notes edit is in progress; refresh shortly") }
        writing = true
        defer { writing = false }
        guard (input.checked == nil) != (input.text == nil), input.revision.count == 64 else { throw Abort(.badRequest) }
        if let text = input.text {
            guard NotesText.validLabel(text) else {
                throw Abort(.badRequest, reason: "Use a single-line item under 4 KiB")
            }
        }
        let before = try document(id)
        guard before.revision == input.revision else { throw Abort(.conflict, reason: "Note changed elsewhere; reload its current version before saving") }
        guard let item = before.items.first(where: { $0.id == itemID }),
              input.checked == nil ? item.canEdit : item.canToggle else { throw Abort(.unprocessableEntity, reason: "This item cannot be edited in this version") }
        if input.checked == item.checked || input.text.map({ sameText($0, item.text) }) == true { return try detail(id) }
        let activity = try await prepareSession()
        defer { IOPMAssertionRelease(activity) }
        try reserve(input.operationId)
        try dispatch(id, before, item, input)
        let deadline = Date().addingTimeInterval(12)
        while Date() < deadline {
            try await Task.sleep(nanoseconds: 250_000_000)
            if let after = try? document(id), verify(before, after, item, input) { return try detail(id) }
        }
        throw Abort(.serviceUnavailable, reason: "Edit outcome is unconfirmed; reload before making another change")
    }
    static func updateText(_ id: String, input: NoteTextUpdateDTO) async throws -> NoteDetailDTO {
        guard !writing else { throw Abort(.conflict, reason: "Another Notes edit is in progress; refresh shortly") }
        writing = true
        defer { writing = false }
        let target = try NotesText.complete(title: input.title, body: input.text)
        var current = try document(id)
        guard current.revision == input.revision else {
            throw Abort(.conflict, reason: "Note changed elsewhere; reload its current version before saving")
        }
        guard current.canEditText else { throw NotesText.invalidText() }
        let oldTitle = current.text.components(separatedBy: "\n")[0]
        let (metadata, _) = try metadata(id)
        guard sameText(oldTitle, metadata.title), current.text.contains("\n") else { throw NotesStore.invalid() }
        if sameText(current.text, target) { return try detail(id) }
        // Keep title and body edits separate so new body text doesn't inherit title formatting.
        let offset = oldTitle.utf16.count + 1
        let oldBody = (current.text as NSString).substring(from: offset)
        let newBody = (target as NSString).substring(from: input.title.utf16.count + 1)
        let edits = try NotesText.edits(old: oldTitle, new: input.title) + NotesText.edits(old: oldBody, new: newBody, location: offset)
        guard edits.count <= 24 else { throw NotesText.invalidText() }
        let activity = try await prepareSession()
        defer { IOPMAssertionRelease(activity) }
        try reserve(input.operationId)
        let deadline = Date().addingTimeInterval(45)
        for edit in edits.reversed() {
            guard Date() < deadline else { throw Abort(.serviceUnavailable, reason: "Saving stopped before all changes were confirmed; reload to review the current note") }
            let expected = NotesText.applying(edit, to: current.text)
            try dispatchEdit(id, current, range: edit.range, inserted: edit.text)
            var confirmed: NotesDocument?
            let stepDeadline = min(deadline, Date().addingTimeInterval(8))
            while Date() < stepDeadline {
                try await Task.sleep(nanoseconds: 150_000_000)
                if let after = try? document(id), sameText(after.text, expected),
                   after.folderID == current.folderID, after.canEditText {
                    confirmed = after; break
                }
            }
            guard let after = confirmed else {
                throw Abort(.serviceUnavailable, reason: "Edit outcome is unconfirmed; reload to review the current note")
            }
            current = after
        }
        guard sameText(current.text, target) else { throw Abort(.serviceUnavailable) }
        let result = try detail(id)
        guard sameText(result.note.text, target), sameText(result.note.title, input.title) else { throw Abort(.serviceUnavailable) }
        return result
    }

    static func verify(_ before: NotesDocument, _ after: NotesDocument, _ target: ChecklistItemDTO, _ input: ChecklistUpdateDTO) -> Bool {
        guard before.items.count == after.items.count, before.attachments == after.attachments else { return false }
        let new = Dictionary(uniqueKeysWithValues: after.items.map { ($0.id, $0) })
        for item in before.items {
            guard let current = new[item.id], current.indent == item.indent,
                  current.checked == (item.id == target.id ? input.checked ?? item.checked : item.checked),
                  sameText(current.text, item.id == target.id ? input.text ?? item.text : item.text) else { return false }
        }
        // The surrounding non-checklist content must also be preserved when sorting moves rows.
        func remaining(_ document: NotesDocument) -> String {
            let text = NSMutableString(string: document.text)
            for item in document.items.reversed() {
                let end = item.location + item.length
                let newline = end < text.length && text.character(at: end) == 10 ? 1 : 0
                text.deleteCharacters(in: NSRange(location: item.location, length: item.length + newline))
            }
            return text as String
        }
        return sameText(remaining(before), remaining(after))
    }
}
