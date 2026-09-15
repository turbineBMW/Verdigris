import AppKit
import Carbon
import Vapor

struct NotesFolderDTO: Content {
    let id: String
    let title: String
}

struct NoteDTO: Content {
    let id: String
    let title: String
    let folderId: String
    let text: String
    let locked: Bool
    let attachments: [String]
}

struct NotesSnapshotDTO: Content {
    let folders: [NotesFolderDTO]
    let notes: [NoteDTO]
    let defaultFolderId: String?
}

struct CreateNoteDTO: Content {
    let folderId: String
    let title: String
    let text: String
}

// All script source is fixed. User data travels as Apple Event parameters,
// never as AppleScript source or shell arguments. Existing note bodies are
// deliberately never assigned: doing so can destroy rich Notes content.
@MainActor
enum NotesBridge {
    static let source = #"""
    on folderTitle(f)
        tell application "Notes"
            set resultName to name of f
            set parentObject to container of f
            repeat 30 times
                set resultName to (name of parentObject) & " / " & resultName
                if class of parentObject is account then exit repeat
                set parentObject to container of parentObject
            end repeat
            return resultName
        end tell
    end folderTitle

    on noteData(n)
        tell application "Notes"
            set noteID to id of n
            set noteTitle to name of n
            set noteContainer to get container of n
            set folderID to id of noteContainer
            set isLocked to password protected of n
            if isLocked then return {noteID, noteTitle, folderID, "", true, {}}
            set noteText to plaintext of n
            set fileNames to name of every attachment of n
            return {noteID, noteTitle, folderID, noteText, false, fileNames}
        end tell
    end noteData

    on snapshotData()
        with timeout of 90 seconds
            tell application "Notes"
                set folderData to {}
                set allFolders to get every folder
                repeat with f in allFolders
                    set end of folderData to {id of f, my folderTitle(contents of f)}
                end repeat
                set notesData to {}
                set allNotes to get every note
                repeat with n in allNotes
                    set end of notesData to my noteData(contents of n)
                end repeat
                set defaultID to ""
                try
                    set defaultAccount to default account
                    set defaultFolder to default folder of defaultAccount
                    set defaultID to id of defaultFolder
                end try
                return {folderData, notesData, defaultID}
            end tell
        end timeout
    end snapshotData

    on createNote(folderID, htmlText)
        with timeout of 60 seconds
            tell application "Notes"
                set destinationFolder to folder id folderID
                set newNote to make new note at destinationFolder with properties {body:htmlText}
                return my noteData(newNote)
            end tell
        end timeout
    end createNote

    on deleteNote(noteID)
        tell application "Notes" to delete note id noteID
        return true
    end deleteNote

    on editingData(noteID)
        tell application "Notes"
            set n to note id noteID
            set isShared to shared of n
            set f to container of n
            repeat 32 times
                if class of f is account then return {my noteData(n), isShared}
                if shared of f then set isShared to true
                set f to container of f
            end repeat
            error number -1728
        end tell
    end editingData

    on showForEditing(noteID)
        tell application "Notes"
            set n to note id noteID
            if password protected of n then error number -1743
            show n
            activate
        end tell
        return true
    end showForEditing

    on editingSelection(noteID)
        tell application "Notes"
            set selectedNotes to selection
            if (count of selectedNotes) is not 1 then return false
            return id of item 1 of selectedNotes is noteID
        end tell
    end editingSelection
    """#

    static func call(_ handler: String, _ arguments: [String] = []) throws -> NSAppleEventDescriptor {
        guard let script = NSAppleScript(source: source) else {
            throw Abort(.internalServerError, reason: "Notes script could not be loaded")
        }
        let event = NSAppleEventDescriptor(eventClass: AEEventClass(kASAppleScriptSuite),
            eventID: AEEventID(kASSubroutineEvent), targetDescriptor: nil,
            returnID: AEReturnID(kAutoGenerateReturnID), transactionID: AETransactionID(kAnyTransactionID))
        event.setDescriptor(NSAppleEventDescriptor(string: handler.lowercased()), forKeyword: AEKeyword(keyASSubroutineName))
        let parameters = NSAppleEventDescriptor.list()
        for (index, value) in arguments.enumerated() {
            parameters.insert(NSAppleEventDescriptor(string: value), at: index + 1)
        }
        event.setDescriptor(parameters, forKeyword: AEKeyword(keyDirectObject))
        var error: NSDictionary?
        let result = script.executeAppleEvent(event, error: &error)
        if let error = error {
            // Never return/log script error descriptions: they can include note content.
            let code = error[NSAppleScript.errorNumber] as? Int ?? 0
            if code == -1743 {
                throw Abort(.forbidden, reason: "Allow iCloud Bridge to control Notes in macOS Privacy & Security → Automation")
            }
            if code == -1728 { throw Abort(.notFound, reason: "Note or folder no longer exists") }
            throw Abort(.serviceUnavailable, reason: "Notes is unavailable (Apple Event error \(code))")
        }
        return result
    }

    static func rows(_ descriptor: NSAppleEventDescriptor) -> [NSAppleEventDescriptor] {
        guard descriptor.numberOfItems > 0 else { return [] }
        return (1...descriptor.numberOfItems).compactMap { descriptor.atIndex($0) }
    }

    static func string(_ descriptor: NSAppleEventDescriptor, _ index: Int) throws -> String {
        guard let value = descriptor.atIndex(index)?.stringValue else {
            throw Abort(.serviceUnavailable, reason: "Notes returned incomplete data; refresh again")
        }
        return value
    }

    // Apple Notes identifiers contain slashes. Expose opaque URL-safe IDs so
    // routers cannot interpret identifiers as path structure.
    static func encodeID(_ value: String) -> String {
        Data(value.utf8).base64EncodedString().replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_").replacingOccurrences(of: "=", with: "")
    }

    static func decodeID(_ value: String) throws -> String {
        var base64 = value.replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/")
        base64 += String(repeating: "=", count: (4 - base64.count % 4) % 4)
        guard let bytes = Data(base64Encoded: base64), let decoded = String(data: bytes, encoding: .utf8),
              decoded.hasPrefix("x-coredata://") else {
            throw Abort(.badRequest, reason: "Invalid Notes identifier")
        }
        return decoded
    }

    static func note(_ descriptor: NSAppleEventDescriptor) throws -> NoteDTO {
        guard descriptor.numberOfItems == 6, let locked = descriptor.atIndex(5),
              let attachments = descriptor.atIndex(6) else {
            throw Abort(.serviceUnavailable, reason: "Notes returned incomplete data")
        }
        return try NoteDTO(id: encodeID(string(descriptor, 1)), title: string(descriptor, 2),
            folderId: encodeID(string(descriptor, 3)), text: locked.booleanValue ? "" : string(descriptor, 4),
            locked: locked.booleanValue, attachments: locked.booleanValue ? [] : rows(attachments).compactMap { $0.stringValue })
    }

    static func snapshot() throws -> NotesSnapshotDTO {
        let result = try call("snapshotData")
        guard let folders = result.atIndex(1), let notes = result.atIndex(2) else {
            throw Abort(.serviceUnavailable, reason: "Notes returned incomplete data")
        }
        return try NotesSnapshotDTO(folders: rows(folders).map {
            try NotesFolderDTO(id: encodeID(string($0, 1)), title: string($0, 2))
        }, notes: rows(notes).map { try note($0) },
            defaultFolderId: result.atIndex(3)?.stringValue.flatMap { $0.isEmpty ? nil : encodeID($0) })
    }

    static func escapeHTML(_ value: String) -> String {
        value.replacingOccurrences(of: "&", with: "&amp;")
            .replacingOccurrences(of: "<", with: "&lt;")
            .replacingOccurrences(of: ">", with: "&gt;")
            .replacingOccurrences(of: "\"", with: "&quot;")
            .replacingOccurrences(of: "'", with: "&#39;")
    }

    static func create(_ input: CreateNoteDTO) throws -> NoteDTO {
        let title = input.title.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !title.isEmpty, title.count <= 1000, !title.contains("\n"), !title.contains("\r"),
              input.text.utf8.count <= 1_000_000 else {
            throw Abort(.badRequest, reason: "Use a single-line title and a note under 1 MB")
        }
        let text = input.text.replacingOccurrences(of: "\r\n", with: "\n").replacingOccurrences(of: "\r", with: "\n")
        let html = "<div><b>" + escapeHTML(title) + "</b></div>" + text.components(separatedBy: "\n")
            .map { "<div>" + ($0.isEmpty ? "<br>" : escapeHTML($0)) + "</div>" }.joined()
        return try note(call("createNote", [decodeID(input.folderId), html]))
    }
}

struct NotesController: RouteCollection {
    func boot(routes: RoutesBuilder) throws {
        let notes = routes.grouped("notes")
        notes.get { _ async throws -> NotesSnapshotDTO in
            try await MainActor.run { try NotesBridge.snapshot() }
        }
        notes.on(.POST, body: .collect(maxSize: "2mb")) { req async throws -> NoteDTO in
            let input = try req.content.decode(CreateNoteDTO.self)
            return try await MainActor.run {
                guard !NotesEditing.writing else { throw Abort(.conflict) }
                return try NotesBridge.create(input)
            }
        }
        notes.get(":noteId", "checklist") { req async throws -> NoteDetailDTO in
            guard let id = req.parameters.get("noteId") else { throw Abort(.badRequest) }
            return try await MainActor.run { try NotesEditing.detail(NotesBridge.decodeID(id)) }
        }
        notes.on(.PATCH, ":noteId", "checklist", ":itemId", body: .collect(maxSize: "16kb")) { req async throws -> NoteDetailDTO in
            guard let id = req.parameters.get("noteId"), let item = req.parameters.get("itemId") else { throw Abort(.badRequest) }
            let input = try req.content.decode(ChecklistUpdateDTO.self)
            return try await NotesEditing.update(NotesBridge.decodeID(id), itemID: item, input: input)
        }
        notes.on(.PATCH, ":noteId", "text", body: .collect(maxSize: "512kb")) { req async throws -> NoteDetailDTO in
            guard let id = req.parameters.get("noteId") else { throw Abort(.badRequest) }
            let input = try req.content.decode(NoteTextUpdateDTO.self)
            return try await NotesEditing.updateText(NotesBridge.decodeID(id), input: input)
        }
        notes.delete(":noteId") { req async throws -> HTTPStatus in
            guard let id = req.parameters.get("noteId") else { throw Abort(.badRequest) }
            try await MainActor.run {
                guard !NotesEditing.writing else { throw Abort(.conflict) }
                _ = try NotesBridge.call("deleteNote", [NotesBridge.decodeID(id)])
            }
            return .noContent
        }
    }
}
