import Foundation
import CryptoKit
import SQLite3
import zlib
import Vapor

enum NotesText {
    static func same(_ lhs: String, _ rhs: String) -> Bool { lhs.utf16.elementsEqual(rhs.utf16) }
    static func validLabel(_ text: String) -> Bool {
        !text.isEmpty && text.utf8.count < 4096 && !text.unicodeScalars.contains {
            $0.properties.generalCategory == .control || [0x2028, 0x2029, 0xfffc].contains($0.value)
        }
    }
    static let textLimit = 64 * 1024
    struct Edit {
        var range: NSRange
        var text: String
    }
    static func validBody(_ text: String) -> Bool {
        text.utf8.count <= textLimit && !text.unicodeScalars.contains {
            ($0.properties.generalCategory == .control && $0.value != 10 && $0.value != 9) ||
                [0x2028, 0x2029, 0xfffc].contains($0.value)
        }
    }
    static func complete(title: String, body: String) throws -> String {
        guard validLabel(title), title.utf16.count <= 1000,
              !title.trimmingCharacters(in: .whitespaces).isEmpty, validBody(body) else { throw invalidText() }
        let text = title + "\n" + body
        let complete = text.hasSuffix("\n") ? text : text + "\n"
        guard validBody(complete) else { throw invalidText() }
        return complete
    }
    static func invalidText() -> Abort {
        Abort(.unprocessableEntity, reason: "Use a single-line title and text under 64 KiB, without embedded objects")
    }
    // Native replacements operate on grapheme boundaries. Every unchanged
    // grapheme stays in Notes, retaining its existing rich-text attributes.
    static func edits(old: String, new: String, location: Int = 0) throws -> [Edit] {
        let span = replacement(old: old, new: new, location: 0)
        let middle = (old as NSString).substring(with: span.range)
        let a = Array(middle).map(String.init), b = Array(span.text).map(String.init)
        guard a.count + b.count <= 8192 else {
            throw Abort(.unprocessableEntity, reason: "This edit spans too much text; save a smaller set of changes at a time")
        }
        let difference = b.difference(from: a, by: same)
        var removed = Set<Int>(), inserted = Set<Int>()
        for change in difference {
            switch change {
            case .remove(let offset, _, _): removed.insert(offset)
            case .insert(let offset, _, _): inserted.insert(offset)
            }
        }
        var result: [Edit] = [], i = 0, j = 0, position = location + span.range.location
        while i < a.count || j < b.count {
            let start = position
            var length = 0, text = ""
            while i < a.count && removed.contains(i) { length += a[i].utf16.count; i += 1 }
            position += length
            while j < b.count && inserted.contains(j) { text += b[j]; j += 1 }
            if length > 0 || !text.isEmpty { result.append(Edit(range: NSRange(location: start, length: length), text: text)) }
            if i < a.count && j < b.count {
                guard same(a[i], b[j]) else { throw invalidText() }
                position += a[i].utf16.count; i += 1; j += 1
            } else if i < a.count || j < b.count { throw invalidText() }
        }
        guard result.count <= 24 else {
            throw Abort(.unprocessableEntity, reason: "There are too many separate changes; save a smaller set at a time")
        }
        return result
    }
    static func applying(_ edit: Edit, to text: String) -> String {
        (text as NSString).replacingCharacters(in: edit.range, with: edit.text)
    }
    static func replacement(old: String, new: String, location: Int) -> (range: NSRange, text: String) {
        let oldCharacters = Array(old), newCharacters = Array(new)
        var prefix = 0, suffix = 0
        while prefix < min(oldCharacters.count, newCharacters.count) && same(String(oldCharacters[prefix]), String(newCharacters[prefix])) { prefix += 1 }
        while suffix < min(oldCharacters.count, newCharacters.count) - prefix && same(String(oldCharacters[oldCharacters.count - suffix - 1]), String(newCharacters[newCharacters.count - suffix - 1])) { suffix += 1 }
        let prefixLength = String(oldCharacters.prefix(prefix)).utf16.count
        let suffixLength = String(oldCharacters.suffix(suffix)).utf16.count
        return (NSRange(location: location + prefixLength, length: old.utf16.count - prefixLength - suffixLength),
                String(newCharacters[prefix..<newCharacters.count - suffix]))
    }
}

struct ChecklistItemDTO: Content, Equatable {
    let id: String
    let text: String
    let checked: Bool
    let indent: Int
    let location: Int
    let length: Int
    var canToggle = true
    var canEdit = true
}

struct NotesDocument {
    let text: String
    var revision: String
    var items: [ChecklistItemDTO]
    let attachments: [Data]
    var folderID: String? = nil
    var ordinaryParagraphs = true
    var sharingAccess: NotesSharing.Access = .privateNote
    var canEditText: Bool {
        ordinaryParagraphs && items.isEmpty && attachments.isEmpty &&
            NotesText.validBody(text) && !text.isEmpty
    }
}

enum NotesStore {
    static let limit = 8 * 1024 * 1024
    enum Value { case number(UInt64), bytes(Data) }
    static func invalid() -> Abort { Abort(.unprocessableEntity, reason: "This note's structure is not supported for editing") }

    static func fields(_ data: Data) throws -> [Int: [Value]] {
        let bytes = [UInt8](data)
        var index = 0
        func number() throws -> UInt64 {
            var value: UInt64 = 0
            for shift in stride(from: 0, through: 63, by: 7) {
                guard index < bytes.count else { throw invalid() }
                let byte = bytes[index]; index += 1
                guard shift != 63 || byte <= 1 else { throw invalid() }
                value |= UInt64(byte & 127) << shift
                if byte & 128 == 0 { return value }
            }
            throw invalid()
        }
        var result: [Int: [Value]] = [:]
        while index < bytes.count {
            let tag = try number()
            guard tag >> 3 > 0, tag >> 3 <= UInt64(Int.max) else { throw invalid() }
            let field = Int(tag >> 3)
            switch tag & 7 {
            case 0: result[field, default: []].append(.number(try number()))
            case 1, 2, 5:
                let size = try tag & 7 == 2 ? number() : (tag & 7 == 1 ? 8 : 4)
                guard size <= bytes.count - index else { throw invalid() }
                result[field, default: []].append(.bytes(Data(bytes[index..<index + Int(size)])))
                index += Int(size)
            default: throw invalid()
            }
        }
        return result
    }

    static func bytes(_ values: [Int: [Value]], _ key: Int) throws -> Data {
        guard let entries = values[key], entries.count == 1, case .bytes(let data) = entries[0] else { throw invalid() }
        return data
    }
    static func number(_ values: [Int: [Value]], _ key: Int, default fallback: UInt64? = nil) throws -> UInt64 {
        if values[key] == nil, let fallback { return fallback }
        guard let entries = values[key], entries.count == 1, case .number(let value) = entries[0] else { throw invalid() }
        return value
    }
    static func inflate(_ input: Data) throws -> Data {
        guard input.count <= limit else { throw invalid() }
        var stream = z_stream()
        guard inflateInit2_(&stream, 15 + 16, ZLIB_VERSION, Int32(MemoryLayout<z_stream>.size)) == Z_OK else { throw invalid() }
        defer { inflateEnd(&stream) }
        return try input.withUnsafeBytes { raw in
            stream.next_in = UnsafeMutablePointer(mutating: raw.bindMemory(to: Bytef.self).baseAddress)
            stream.avail_in = uInt(input.count)
            var output = Data()
            var buffer = [UInt8](repeating: 0, count: 65536)
            while true {
                let result = buffer.withUnsafeMutableBytes { raw -> Int32 in
                    stream.next_out = raw.bindMemory(to: Bytef.self).baseAddress
                    stream.avail_out = uInt(raw.count)
                    return zlib.inflate(&stream, Z_NO_FLUSH)
                }
                guard result == Z_OK || result == Z_STREAM_END else { throw invalid() }
                let count = buffer.count - Int(stream.avail_out)
                guard output.count + count <= limit, count > 0 || result == Z_STREAM_END else { throw invalid() }
                output.append(contentsOf: buffer.prefix(count))
                if result == Z_STREAM_END {
                    guard stream.avail_in == 0 else { throw invalid() }
                    return output
                }
            }
        }
    }

    static func decode(_ blob: Data) throws -> NotesDocument {
        let root = try fields(inflate(blob))
        let wrapper = try fields(bytes(root, 2))
        let note = try fields(bytes(wrapper, 3))
        guard let text = try String(data: bytes(note, 2), encoding: .utf8) else { throw invalid() }
        let ns = text as NSString
        var paragraphs: [(Int, Int, String)] = []
        var position = 0
        for line in text.components(separatedBy: "\n") {
            let length = line.utf16.count
            if position < ns.length { paragraphs.append((position, min(position + length + 1, ns.length), line)) }
            position += length + 1
        }
        var items: [Int: ChecklistItemDTO] = [:]
        var attachments: [Data] = []
        var ordinaryParagraphs = true
        position = 0
        for rawRun in note[5] ?? [] {
            guard case .bytes(let data) = rawRun else { throw invalid() }
            let run = try fields(data)
            let length = try number(run, 1)
            guard length <= ns.length - position else { throw invalid() }
            let end = position + Int(length)
            if run[12] != nil { attachments.append(try bytes(run, 12)) }
            if run[2] != nil {
                let style = try fields(bytes(run, 2))
                let kind = try number(style, 1, default: UInt64.max)
                if ![UInt64.max, 0, 1, 2, 3].contains(kind) { ordinaryParagraphs = false }
                if kind == 103 {
                    let checklist = try fields(bytes(style, 5))
                    let id = try bytes(checklist, 1)
                    let checked = try number(checklist, 2)
                    let indent = try number(style, 4, default: 0)
                    guard !id.isEmpty, id.count <= 64, checked <= 1, indent <= 32 else { throw invalid() }
                    for (start, stop, label) in paragraphs where start < end && stop > position {
                        let item = ChecklistItemDTO(id: id.map { String(format: "%02x", $0) }.joined(),
                            text: label, checked: checked == 1, indent: Int(indent), location: start, length: label.utf16.count,
                            canToggle: true, canEdit: !label.contains("\u{fffc}") && !label.contains("\u{2028}") && !label.contains("\r"))
                        guard items[start] == nil || items[start] == item else { throw invalid() }
                        items[start] = item
                    }
                }
            }
            position = end
        }
        guard position == ns.length else { throw invalid() }
        var ordered = items.values.sorted { $0.location < $1.location }
        guard Set(ordered.map(\.id)).count == ordered.count else { throw invalid() }
        for index in ordered.indices {
            // Native parent toggles may cascade. Enable only proven leaf operations.
            if index + 1 < ordered.count && ordered[index + 1].indent > ordered[index].indent {
                ordered[index].canToggle = false
            }
        }
        return NotesDocument(text: text, revision: SHA256.hash(data: blob).map { String(format: "%02x", $0) }.joined(),
                             items: ordered, attachments: attachments, ordinaryParagraphs: ordinaryParagraphs)
    }

    static func read(_ id: String, database: URL? = nil) throws -> NotesDocument {
        let parts = id.components(separatedBy: "/")
        guard parts.count == 5, parts[0] == "x-coredata:", parts[3] == "ICNote",
              parts[4].first == "p", let pk = Int64(parts[4].dropFirst()), pk > 0 else { throw invalid() }
        let path = (database ?? FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Group Containers/group.com.apple.notes/NoteStore.sqlite")).path
        var db: OpaquePointer?
        guard sqlite3_open_v2(path, &db, SQLITE_OPEN_READONLY | SQLITE_OPEN_FULLMUTEX, nil) == SQLITE_OK else {
            if let db { sqlite3_close(db) }
            throw Abort(.forbidden, reason: "Grant iCloud Bridge Full Disk Access in macOS Privacy & Security")
        }
        defer { sqlite3_close(db) }
        sqlite3_busy_timeout(db, 1000)
        guard sqlite3_exec(db, "BEGIN", nil, nil, nil) == SQLITE_OK else { throw invalid() }
        defer { sqlite3_exec(db, "ROLLBACK", nil, nil, nil) }
        var statement: OpaquePointer?
        guard sqlite3_prepare_v2(db, "SELECT Z_UUID FROM Z_METADATA", -1, &statement, nil) == SQLITE_OK else { throw invalid() }
        defer { sqlite3_finalize(statement) }
        guard sqlite3_step(statement) == SQLITE_ROW, let uuid = sqlite3_column_text(statement, 0),
              String(cString: uuid).caseInsensitiveCompare(parts[2]) == .orderedSame else { throw invalid() }
        var query: OpaquePointer?
        guard sqlite3_prepare_v2(db, "SELECT n.ZISPASSWORDPROTECTED,n.ZMARKEDFORDELETION,d.ZDATA,f.ZFOLDERTYPE,f.ZMARKEDFORDELETION,n.ZFOLDER FROM ZICCLOUDSYNCINGOBJECT n JOIN ZICNOTEDATA d ON d.ZNOTE=n.Z_PK JOIN ZICCLOUDSYNCINGOBJECT f ON f.Z_PK=n.ZFOLDER WHERE n.Z_PK=?", -1, &query, nil) == SQLITE_OK else { throw invalid() }
        defer { sqlite3_finalize(query) }
        sqlite3_bind_int64(query, 1, pk)
        guard sqlite3_step(query) == SQLITE_ROW else { throw Abort(.notFound) }
        // Recently Deleted is folder type 1 on the validated schema; individual
        // trashed notes need not have ZMARKEDFORDELETION set. Only ordinary type-0
        // folders are supported, so unknown future folder types fail closed.
        guard sqlite3_column_int(query, 0) == 0, sqlite3_column_int(query, 1) == 0,
              sqlite3_column_type(query, 3) != SQLITE_NULL, sqlite3_column_int(query, 3) == 0,
              sqlite3_column_int(query, 4) == 0 else {
            throw Abort(.forbidden, reason: "Protected or deleted notes cannot be edited")
        }
        let size = Int(sqlite3_column_bytes(query, 2))
        guard size > 0, size <= limit, let pointer = sqlite3_column_blob(query, 2) else { throw invalid() }
        var document = try decode(Data(bytes: pointer, count: size))
        document.folderID = "x-coredata://\(parts[2])/ICFolder/p\(sqlite3_column_int64(query, 5))"
        // Include direct and inherited sharing permissions in the same SQLite
        // snapshot as the document, and in its revision fingerprint.
        var scope: OpaquePointer?
        guard sqlite3_prepare_v2(db, "SELECT ZSERVERSHAREDATA,ZISSHAREDIRTY,ZPARENT,ZFOLDERTYPE,ZMARKEDFORDELETION FROM ZICCLOUDSYNCINGOBJECT WHERE Z_PK=?", -1, &scope, nil) == SQLITE_OK else { throw invalid() }
        defer { sqlite3_finalize(scope) }
        var ids: Set<Int64> = [], next: Int64? = pk, archives: [Data] = [], pending = false
        var fingerprint = SHA256()
        fingerprint.update(data: Data(document.revision.utf8))
        while let current = next {
            guard ids.count < 32, ids.insert(current).inserted else { throw invalid() }
            sqlite3_reset(scope)
            sqlite3_bind_int64(scope, 1, current)
            guard sqlite3_step(scope) == SQLITE_ROW else { throw invalid() }
            if current != pk {
                guard sqlite3_column_type(scope, 3) != SQLITE_NULL, sqlite3_column_int(scope, 3) == 0,
                      sqlite3_column_int(scope, 4) == 0 else { throw invalid() }
            }
            let dirty = sqlite3_column_int(scope, 1) != 0
            pending = pending || dirty
            fingerprint.update(data: Data("/\(current):\(dirty)".utf8))
            if sqlite3_column_type(scope, 0) != SQLITE_NULL {
                let size = Int(sqlite3_column_bytes(scope, 0))
                guard size > 0, size <= NotesSharing.limit, let bytes = sqlite3_column_blob(scope, 0) else { throw invalid() }
                let share = Data(bytes: bytes, count: size)
                archives.append(share)
                fingerprint.update(data: Data(SHA256.hash(data: share)))
            }
            if current == pk { next = sqlite3_column_int64(query, 5) }
            else { next = sqlite3_column_type(scope, 2) == SQLITE_NULL ? nil : sqlite3_column_int64(scope, 2) }
        }
        document.sharingAccess = NotesSharing.access(archives: archives, pending: pending)
        document.revision = fingerprint.finalize().map { String(format: "%02x", $0) }.joined()
        return document
    }
}
