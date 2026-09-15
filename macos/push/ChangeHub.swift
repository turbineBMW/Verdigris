import Foundation
import Vapor

/// Fans small, content-free invalidation messages out to connected clients.
/// REST remains the authoritative data path; this socket only says when to refetch.
final class ChangeHub: @unchecked Sendable {
    private struct State: Encodable {
        let version = 1
        let epoch: String
        let reminders: UInt64
        let notes: UInt64
    }

    private let lock = NSLock()
    private let epoch = UUID().uuidString.lowercased()
    private var reminders: UInt64 = 0
    private var notes: UInt64 = 0
    private var clients: [UUID: WebSocket] = [:]

    func connect(_ socket: WebSocket) {
        let id = UUID()
        let payload: String
        lock.lock()
        clients[id] = socket
        payload = encodedStateLocked()
        lock.unlock()

        socket.send(payload)
        socket.onClose.whenComplete { [weak self] _ in
            self?.disconnect(id)
        }
    }

    func invalidateReminders() {
        invalidate { reminders &+= 1 }
    }

    func invalidateNotes() {
        invalidate { notes &+= 1 }
    }

    private func invalidate(_ change: () -> Void) {
        let payload: String
        let sockets: [WebSocket]
        lock.lock()
        change()
        payload = encodedStateLocked()
        sockets = Array(clients.values)
        lock.unlock()

        for socket in sockets {
            socket.send(payload)
        }
    }

    private func disconnect(_ id: UUID) {
        lock.lock()
        clients.removeValue(forKey: id)
        lock.unlock()
    }

    private func encodedStateLocked() -> String {
        let state = State(epoch: epoch, reminders: reminders, notes: notes)
        guard let data = try? JSONEncoder().encode(state),
              let text = String(data: data, encoding: .utf8) else {
            // All fields have unconditional encodings, so this is defensive only.
            return #"{"version":1,"epoch":"invalid","reminders":0,"notes":0}"#
        }
        return text
    }
}
