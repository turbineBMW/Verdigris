import AppKit
import ApplicationServices

// Disposable-note experiment only. No network listener or production bridge changes.
// A local driver submits one job at a time in this user's private support directory.
let directory = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent("Library/Application Support/VerdigrisChecklistProbe")
let fixturePrefix = "Verdigris Checklist Probe "

struct ProbeError: Error, CustomStringConvertible {
    let description: String
    init(_ message: String) { description = message }
}

func attribute(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success else { return nil }
    return value
}

func children(_ element: AXUIElement) -> [AXUIElement] {
    attribute(element, kAXChildrenAttribute) as? [AXUIElement] ?? []
}

func walk(_ element: AXUIElement, depth: Int = 0) -> [AXUIElement] {
    guard depth < 20 else { return [] }
    return [element] + children(element).flatMap { walk($0, depth: depth + 1) }
}

func checked(_ result: AXError, _ operation: String) throws {
    guard result == .success else { throw ProbeError("\(operation): AX error \(result.rawValue)") }
}

final class Delegate: NSObject, NSApplicationDelegate {
    var window: NSWindow!
    var status: NSTextField!
    var timer: Timer?

    func applicationDidFinishLaunching(_ notification: Notification) {
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
                                                attributes: [.posixPermissions: 0o700])
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 490, height: 210),
                          styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
        window.title = "Verdigris Checklist Probe"
        window.center()
        let explanation = NSTextField(wrappingLabelWithString:
            "This helper tests native Apple Notes checkboxes on disposable Verdigris test notes. Grant Accessibility access to let it select and toggle test items. Leave this helper open during the test.")
        explanation.frame = NSRect(x: 24, y: 95, width: 442, height: 90)
        window.contentView?.addSubview(explanation)
        let button = NSButton(title: "Grant Accessibility Access", target: self, action: #selector(grant))
        button.frame = NSRect(x: 24, y: 52, width: 260, height: 32)
        window.contentView?.addSubview(button)
        status = NSTextField(labelWithString: "")
        status.frame = NSRect(x: 24, y: 20, width: 442, height: 24)
        window.contentView?.addSubview(status)
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        timer = Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { [weak self] _ in self?.poll() }
        poll()
    }

    @objc func grant() {
        let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
        _ = AXIsProcessTrustedWithOptions(options)
        NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility")!)
    }

    func write(_ result: [String: Any], name: String) {
        do {
            let data = try JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted, .sortedKeys])
            let url = directory.appendingPathComponent(name)
            try data.write(to: url, options: .atomic)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
        } catch { status.stringValue = "Cannot write probe result" }
    }

    func poll() {
        let trusted = AXIsProcessTrusted()
        status.stringValue = trusted ? "Accessibility ready · waiting for test commands" : "Waiting for Accessibility permission"
        let statusURL = directory.appendingPathComponent("status.json")
        let current = (try? Data(contentsOf: statusURL)).flatMap { try? JSONSerialization.jsonObject(with: $0) } as? [String: Any]
        if current?["trusted"] as? Bool != trusted {
            write(["trusted": trusted, "pid": ProcessInfo.processInfo.processIdentifier], name: "status.json")
        }
        let jobURL = directory.appendingPathComponent("job.json")
        guard let data = try? Data(contentsOf: jobURL) else { return }
        // Consume before performing an action; ambiguous results must not replay it.
        try? FileManager.default.removeItem(at: jobURL)
        var jobID = "invalid"
        do {
            guard let job = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let id = job["id"] as? String else { throw ProbeError("Invalid job") }
            jobID = id
            var result = try run(job)
            result["id"] = id
            result["ok"] = true
            write(result, name: "result.json")
        } catch {
            write(["id": jobID, "ok": false, "error": String(describing: error)], name: "result.json")
        }
    }

    func run(_ job: [String: Any]) throws -> [String: Any] {
        guard AXIsProcessTrusted() else { throw ProbeError("Accessibility permission required") }
        guard let notes = NSRunningApplication.runningApplications(withBundleIdentifier: "com.apple.Notes").first else {
            throw ProbeError("Notes is not running")
        }
        let app = AXUIElementCreateApplication(notes.processIdentifier)
        AXUIElementSetMessagingTimeout(app, 3)
        let windows = attribute(app, kAXWindowsAttribute) as? [AXUIElement] ?? []
        let elements = windows.flatMap { walk($0) }
        let operation = job["operation"] as? String ?? "inspect"
        if operation == "inspect" {
            // No note names, sidebar values, or personal note bodies in diagnostics.
            let roles = elements.reduce(into: [String: Int]()) { counts, element in
                let role = attribute(element, kAXRoleAttribute) as? String ?? "unknown"
                counts[role, default: 0] += 1
            }
            let editors = elements.filter { attribute($0, kAXRoleAttribute) as? String == kAXTextAreaRole }
            let sheets = elements.filter { attribute($0, kAXRoleAttribute) as? String == kAXSheetRole }
            return ["roles": roles, "sheets": sheets.map { sheet in
                walk(sheet).compactMap { element -> String? in
                    let role = attribute(element, kAXRoleAttribute) as? String
                    if role == kAXButtonRole { return attribute(element, kAXTitleAttribute) as? String }
                    if role == kAXStaticTextRole { return attribute(element, kAXValueAttribute) as? String }
                    return nil
                }
            }, "editors": editors.map { element -> [String: Any] in
                var names: CFArray?
                _ = AXUIElementCopyAttributeNames(element, &names)
                let value = attribute(element, kAXValueAttribute) as? String ?? ""
                return ["attributes": names as? [String] ?? [], "fixture": value.hasPrefix(fixturePrefix),
                        "text": value.hasPrefix(fixturePrefix) ? value : "<not a fixture>"]
            }]
        }
        guard let expected = job["expectedText"] as? String, expected.hasPrefix(fixturePrefix),
              let editor = elements.first(where: {
                  attribute($0, kAXRoleAttribute) as? String == kAXTextAreaRole &&
                  attribute($0, kAXValueAttribute) as? String == expected
              }) else { throw ProbeError("Selected editor does not exactly match the disposable fixture") }
        if operation == "menus" {
            guard let menu = attribute(app, kAXMenuBarAttribute) else { throw ProbeError("No menu bar") }
            let titles = walk(menu as! AXUIElement).compactMap { attribute($0, kAXTitleAttribute) as? String }
            return ["menus": titles]
        }
        let sheets = elements.filter { attribute($0, kAXRoleAttribute) as? String == kAXSheetRole }
        if operation == "dismissSortPrompt" {
            guard sheets.count == 1, let buttonName = job["button"] as? String else { throw ProbeError("Expected sort prompt") }
            let contents = walk(sheets[0])
            let message = contents.compactMap { attribute($0, kAXValueAttribute) as? String }.joined(separator: " ").lowercased()
            guard message.contains("checked") && message.contains("sort"),
                  let button = contents.first(where: {
                      attribute($0, kAXRoleAttribute) as? String == kAXButtonRole &&
                      attribute($0, kAXTitleAttribute) as? String == buttonName
                  }) else { throw ProbeError("Sort prompt did not match") }
            try checked(AXUIElementPerformAction(button, kAXPressAction as CFString), "Respond to sort prompt")
            return ["dispatched": true]
        }
        guard sheets.isEmpty else { throw ProbeError("Notes has a modal dialog; no edit sent") }
        if operation == "read" {
            var selected = [String: Int]()
            if let value = attribute(editor, kAXSelectedTextRangeAttribute), CFGetTypeID(value) == AXValueGetTypeID() {
                var range = CFRange()
                if AXValueGetValue(value as! AXValue, .cfRange, &range) {
                    selected = ["location": range.location, "length": range.length]
                }
            }
            return ["text": expected, "selection": selected]
        }
        guard ["checklist", "toggle", "indent", "outdent", "replace", "select", "bold", "moveDown", "moveUp"].contains(operation),
              let location = job["location"] as? Int, let length = job["length"] as? Int,
              location >= 0, length >= 0, location <= expected.utf16.count,
              length <= expected.utf16.count - location else { throw ProbeError("Invalid operation or UTF-16 range") }
        notes.activate(options: [])
        try checked(AXUIElementSetAttributeValue(editor, kAXFocusedAttribute as CFString, kCFBooleanTrue), "Focus editor")
        var range = CFRange(location: location, length: length)
        guard let selection = AXValueCreate(.cfRange, &range) else { throw ProbeError("Invalid selection") }
        try checked(AXUIElementSetAttributeValue(editor, kAXSelectedTextRangeAttribute as CFString, selection), "Select paragraph")
        // Recheck after focus/selection to catch the user or sync replacing the editor.
        guard attribute(editor, kAXValueAttribute) as? String == expected,
              NSWorkspace.shared.frontmostApplication?.processIdentifier == notes.processIdentifier else {
            throw ProbeError("Editor or foreground app changed; no edit sent")
        }
        if operation == "select" { return ["selected": true] }
        if operation == "replace" {
            guard let replacement = job["replacement"] as? String, replacement.utf8.count < 4096,
                  location >= (expected.components(separatedBy: "\n").first ?? "").utf16.count + 1 else {
                throw ProbeError("Replacement must be inside fixture body")
            }
            try checked(AXUIElementSetAttributeValue(editor, kAXSelectedTextAttribute as CFString, replacement as CFString), "Replace selected text")
        } else {
            let key: CGKeyCode
            let flags: CGEventFlags
            switch operation {
            case "checklist": key = 37; flags = [.maskCommand, .maskShift] // L
            case "toggle": key = 32; flags = [.maskCommand, .maskShift] // U
            case "indent": key = 48; flags = [] // Tab
            case "bold": key = 11; flags = [.maskCommand] // B
            case "moveDown": key = 125; flags = [.maskCommand, .maskControl]
            case "moveUp": key = 126; flags = [.maskCommand, .maskControl]
            default: key = 48; flags = [.maskShift]
            }
            guard let down = CGEvent(keyboardEventSource: nil, virtualKey: key, keyDown: true),
                  let up = CGEvent(keyboardEventSource: nil, virtualKey: key, keyDown: false) else {
                throw ProbeError("Cannot construct native key event")
            }
            down.flags = flags
            up.flags = flags
            down.postToPid(notes.processIdentifier)
            up.postToPid(notes.processIdentifier)
        }
        return ["dispatched": true] // Driver must independently verify Notes' stored state.
    }
}

let application = NSApplication.shared
let delegate = Delegate()
application.delegate = delegate
application.setActivationPolicy(.regular)
application.run()
