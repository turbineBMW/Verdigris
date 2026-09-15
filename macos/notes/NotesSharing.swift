import Foundation
import CloudKit

// Interpret the local CKShare cache using CloudKit's own secure decoder.
// No CloudKit requests, participant identities, or sharing links leave the Mac.
enum NotesSharing {
    enum Access: String {
        case privateNote, editable, readOnly, unknown
        var allowsEditing: Bool { self == .privateNote || self == .editable }
        var reason: String? {
            switch self {
            case .privateNote, .editable: return nil
            case .readOnly: return "You have view-only access to this shared note"
            case .unknown: return "Sharing permissions are updating or unavailable; open the note on the Mac and refresh"
            }
        }
        func matching(shared: Bool) -> Access {
            if (self == .privateNote) == shared { return .unknown }
            return self
        }
    }
    static let limit = 1024 * 1024

    static func access(permission: CKShare.ParticipantPermission, acceptance: CKShare.ParticipantAcceptanceStatus) -> Access {
        guard acceptance == .accepted else { return .unknown }
        switch permission {
        case .readWrite: return .editable
        case .readOnly: return .readOnly
        default: return .unknown
        }
    }

    static func decode(_ data: Data) -> Access {
        guard !data.isEmpty, data.count <= limit else { return .unknown }
        do {
            // Notes uses encodeSystemFields, rather than an archived root object.
            // Check its envelope before initializing a CloudKit record.
            guard let archive = try PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any],
                  archive["$archiver"] as? String == "NSKeyedArchiver",
                  let top = archive["$top"] as? [String: Any],
                  top["RecordType"] != nil, top["RecordID"] != nil, top["Participants"] != nil else { return .unknown }
            let decoder = try NSKeyedUnarchiver(forReadingFrom: data)
            decoder.decodingFailurePolicy = .setErrorAndReturn
            decoder.requiresSecureCoding = true
            defer { decoder.finishDecoding() }
            guard decoder.decodeObject(of: NSString.self, forKey: "RecordType") as String? == CKRecord.SystemType.share,
                  decoder.decodeObject(of: CKRecord.ID.self, forKey: "RecordID") != nil,
                  decoder.error == nil else { return .unknown }
            let share = CKShare(coder: decoder)
            guard decoder.error == nil, let me = share.currentUserParticipant else { return .unknown }
            return access(permission: me.permission, acceptance: me.acceptanceStatus)
        } catch { return .unknown }
    }

    static func access(archives: [Data], pending: Bool) -> Access {
        if pending { return .unknown }
        if archives.isEmpty { return .privateNote }
        let values = archives.map(decode)
        if values.contains(.unknown) { return .unknown }
        if values.contains(.readOnly) { return .readOnly }
        return .editable
    }
}
