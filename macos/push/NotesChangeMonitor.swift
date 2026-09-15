import CoreServices
import Darwin
import Foundation

/// Watches Apple Notes' database files as an invalidation hint. Vnode events
/// catch its long-lived mmap'd WAL; FSEvents catches replacement/recreation.
/// Neither reveals which note changed, so clients still reconcile through REST.
final class NotesChangeMonitor: @unchecked Sendable {
    private let queue = DispatchQueue(label: "dev.turbinebmw.verdigris.notes-changes")
    private let onChange: @Sendable () -> Void
    private var stream: FSEventStreamRef?
    private var fileSources: [DispatchSourceFileSystemObject] = []
    private var notesDirectory = URL(fileURLWithPath: "/")
    private var pending: DispatchWorkItem?
    private var pendingRearm: DispatchWorkItem?

    init?(onChange: @escaping @Sendable () -> Void) {
        self.onChange = onChange
        let directory = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Group Containers/group.com.apple.notes")
        guard FileManager.default.fileExists(atPath: directory.path) else {
            return nil
        }
        notesDirectory = directory

        var context = FSEventStreamContext(
            version: 0,
            info: Unmanaged.passUnretained(self).toOpaque(),
            retain: nil,
            release: nil,
            copyDescription: nil
        )
        let callback: FSEventStreamCallback = { _, info, count, eventPaths, eventFlags, _ in
            guard let info else { return }
            let monitor = Unmanaged<NotesChangeMonitor>.fromOpaque(info).takeUnretainedValue()
            let paths = unsafeBitCast(eventPaths, to: NSArray.self) as? [String] ?? []
            monitor.receive(paths: paths, flags: eventFlags, count: count)
        }
        let flags = FSEventStreamCreateFlags(
            kFSEventStreamCreateFlagUseCFTypes
                | kFSEventStreamCreateFlagFileEvents
                | kFSEventStreamCreateFlagNoDefer
                | kFSEventStreamCreateFlagWatchRoot
        )
        guard let stream = FSEventStreamCreate(
            nil,
            callback,
            &context,
            [directory.path] as CFArray,
            FSEventStreamEventId(kFSEventStreamEventIdSinceNow),
            0.5,
            flags
        ) else {
            return nil
        }
        self.stream = stream
        FSEventStreamSetDispatchQueue(stream, queue)
        guard FSEventStreamStart(stream) else {
            FSEventStreamInvalidate(stream)
            FSEventStreamRelease(stream)
            self.stream = nil
            return nil
        }
        queue.async { [weak self] in
            self?.armDatabaseFiles()
        }
    }

    deinit {
        pending?.cancel()
        pendingRearm?.cancel()
        fileSources.forEach { $0.cancel() }
        if let stream {
            FSEventStreamStop(stream)
            FSEventStreamInvalidate(stream)
            FSEventStreamRelease(stream)
        }
    }

    private func receive(
        paths: [String],
        flags: UnsafePointer<FSEventStreamEventFlags>,
        count: Int
    ) {
        let rescanFlags = FSEventStreamEventFlags(
            kFSEventStreamEventFlagMustScanSubDirs
                | kFSEventStreamEventFlagUserDropped
                | kFSEventStreamEventFlagKernelDropped
                | kFSEventStreamEventFlagRootChanged
                | kFSEventStreamEventFlagEventIdsWrapped
        )
        let relevant = (0..<count).contains { index in
            (flags[index] & rescanFlags) != 0
                || (index < paths.count
                    && URL(fileURLWithPath: paths[index]).lastPathComponent
                        .hasPrefix("NoteStore.sqlite"))
        }
        guard relevant else { return }
        scheduleChange()
        scheduleRearm()
    }

    private func scheduleChange() {
        // Notes commonly writes the WAL and metadata in a short burst. A
        // trailing debounce turns that burst into one content-free event.
        pending?.cancel()
        let work = DispatchWorkItem { [weak self] in
            self?.onChange()
        }
        pending = work
        queue.asyncAfter(deadline: .now() + 1.5, execute: work)
    }

    private func scheduleRearm() {
        pendingRearm?.cancel()
        let work = DispatchWorkItem { [weak self] in
            self?.armDatabaseFiles()
        }
        pendingRearm = work
        queue.asyncAfter(deadline: .now() + 0.25, execute: work)
    }

    private func armDatabaseFiles() {
        fileSources.forEach { $0.cancel() }
        fileSources.removeAll()
        for name in ["NoteStore.sqlite", "NoteStore.sqlite-wal"] {
            let descriptor = open(notesDirectory.appendingPathComponent(name).path, O_EVTONLY)
            guard descriptor >= 0 else { continue }
            let source = DispatchSource.makeFileSystemObjectSource(
                fileDescriptor: descriptor,
                eventMask: [.write, .extend, .delete, .rename, .revoke],
                queue: queue
            )
            source.setEventHandler { [weak self] in
                self?.scheduleChange()
                self?.scheduleRearm()
            }
            source.setCancelHandler {
                close(descriptor)
            }
            fileSources.append(source)
            source.resume()
        }
    }
}
