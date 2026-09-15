#!/usr/bin/env python3
"""Add Reminders and Notes change push to the pinned iCloudBridge checkout."""

import argparse
import shutil
from pathlib import Path


def replace_once(path: Path, content: str, old: str, new: str, already: str) -> str:
    if already in content:
        return content
    if old not in content:
        raise SystemExit(f"Unrecognized {path.name}; no files changed")
    return content.replace(old, new, 1)


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("checkout", type=Path)
args = parser.parse_args()
source = args.checkout / "Sources/iCloudBridge"
api = source / "API"
services = source / "Services"
if not api.is_dir() or not services.is_dir():
    raise SystemExit("Unrecognized iCloudBridge checkout; no files changed")

routes = api / "Routes.swift"
routes_content = replace_once(
    routes,
    routes.read_text(),
    "    tokenManager: TokenManager,\n    isAuthEnabled: @escaping () -> Bool\n) throws {",
    "    tokenManager: TokenManager,\n"
    "    isAuthEnabled: @escaping () -> Bool,\n"
    "    changeHub: ChangeHub\n) throws {",
    "    changeHub: ChangeHub\n) throws {",
)
routes_content = replace_once(
    routes,
    routes_content,
    '    let api = app.grouped("api", "v1").grouped(authMiddleware)\n',
    '    let api = app.grouped("api", "v1").grouped(authMiddleware)\n\n'
    '    api.webSocket("changes") { _, socket in\n'
    "        changeHub.connect(socket)\n"
    "    }\n",
    '    api.webSocket("changes")',
)

manager = services / "ServerManager.swift"
manager_content = replace_once(
    manager,
    manager.read_text(),
    "    private var app: Application?\n",
    "    private var app: Application?\n    private let changeHub = ChangeHub()\n",
    "    private let changeHub = ChangeHub()",
)
manager_content = replace_once(
    manager,
    manager_content,
    "    private let changeHub = ChangeHub()\n",
    "    private let changeHub = ChangeHub()\n"
    "    private var notesChangeMonitor: NotesChangeMonitor?\n",
    "    private var notesChangeMonitor: NotesChangeMonitor?",
)
manager_content = replace_once(
    manager,
    manager_content,
    "        // Configure JSON encoder for dates\n",
    "        // Forward EventKit invalidations only while this server manager is alive.\n"
    "        let changeHub = self.changeHub\n"
    "        await MainActor.run {\n"
    "            remindersService.onEventStoreChange = { [weak changeHub] in\n"
    "                changeHub?.invalidateReminders()\n"
    "            }\n"
    "        }\n\n"
    "        // Configure JSON encoder for dates\n",
    "            remindersService.onEventStoreChange = { [weak changeHub] in",
)
manager_content = replace_once(
    manager,
    manager_content,
    "        // Forward EventKit invalidations only while this server manager is alive.\n",
    "        if notesChangeMonitor == nil {\n"
    "            notesChangeMonitor = NotesChangeMonitor { [weak changeHub] in\n"
    "                changeHub?.invalidateNotes()\n"
    "            }\n"
    "        }\n\n"
    "        // Forward EventKit invalidations only while this server manager is alive.\n",
    "            notesChangeMonitor = NotesChangeMonitor { [weak changeHub] in",
)
manager_content = replace_once(
    manager,
    manager_content,
    "            tokenManager: tokenManager,\n            isAuthEnabled: allowRemoteConnections\n",
    "            tokenManager: tokenManager,\n"
    "            isAuthEnabled: allowRemoteConnections,\n"
    "            changeHub: changeHub\n",
    "            changeHub: changeHub\n",
)

reminders = services / "RemindersService.swift"
reminders_content = replace_once(
    reminders,
    reminders.read_text(),
    "    private var eventStore = EKEventStore()\n",
    "    private var eventStore = EKEventStore()\n"
    "    private var eventStoreChangeObserver: NSObjectProtocol?\n"
    "    private var pendingEventStoreRefresh: Task<Void, Never>?\n"
    "    var onEventStoreChange: (() -> Void)?\n",
    "    var onEventStoreChange: (() -> Void)?",
)
reminders_content = replace_once(
    reminders,
    reminders_content,
    "        if authorizationStatus == .fullAccess {\n            loadLists()\n        }\n    }\n",
    "        if authorizationStatus == .fullAccess {\n"
    "            loadLists()\n"
    "        }\n"
    "        eventStoreChangeObserver = NotificationCenter.default.addObserver(\n"
    "            forName: .EKEventStoreChanged, object: eventStore, queue: .main\n"
    "        ) { [weak self] _ in\n"
    "            Task { @MainActor [weak self] in\n"
    "                self?.scheduleEventStoreRefresh()\n"
    "            }\n"
    "        }\n"
    "    }\n\n"
    "    private func scheduleEventStoreRefresh() {\n"
    "        pendingEventStoreRefresh?.cancel()\n"
    "        pendingEventStoreRefresh = Task { @MainActor [weak self] in\n"
    "            do {\n"
    "                try await Task.sleep(nanoseconds: 750_000_000)\n"
    "            } catch {\n"
    "                return\n"
    "            }\n"
    "            guard let self else { return }\n"
    "            self.loadLists()\n"
    "            self.onEventStoreChange?()\n"
    "        }\n"
    "    }\n",
    "    private func scheduleEventStoreRefresh()",
)

# Validate every source marker before changing the checkout.
shutil.copyfile(Path(__file__).with_name("ChangeHub.swift"), api / "ChangeHub.swift")
shutil.copyfile(
    Path(__file__).with_name("NotesChangeMonitor.swift"), api / "NotesChangeMonitor.swift"
)
routes.write_text(routes_content)
manager.write_text(manager_content)
reminders.write_text(reminders_content)

print("Reminders and Notes push extension applied. Rebuild and restart iCloudBridge.")
