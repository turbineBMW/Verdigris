"""The Mac source patch is idempotent and refuses partial updates."""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATCHER = ROOT / "macos/push/apply.py"


def checkout(tmp_path: Path, valid_manager: bool = True) -> Path:
    root = tmp_path / "iCloudBridge"
    api = root / "Sources/iCloudBridge/API"
    services = root / "Sources/iCloudBridge/Services"
    api.mkdir(parents=True)
    services.mkdir(parents=True)
    (api / "Routes.swift").write_text(
        "func configureRoutes(\n"
        "    tokenManager: TokenManager,\n"
        "    isAuthEnabled: @escaping () -> Bool\n"
        ") throws {\n"
        '    let api = app.grouped("api", "v1").grouped(authMiddleware)\n'
        "}\n"
    )
    (services / "ServerManager.swift").write_text(
        (
            "actor ServerManager {\n    private var app: Application?\n"
            if valid_manager
            else "actor Unknown {\n"
        )
        + "    func start() async throws {\n"
        "        // Configure JSON encoder for dates\n"
        "        configureRoutes(\n"
        "            tokenManager: tokenManager,\n"
        "            isAuthEnabled: allowRemoteConnections\n"
        "        )\n"
        "    }\n}\n"
    )
    (services / "RemindersService.swift").write_text(
        "@MainActor\nclass RemindersService {\n"
        "    private var eventStore = EKEventStore()\n"
        "    init() {\n"
        "        if authorizationStatus == .fullAccess {\n"
        "            loadLists()\n"
        "        }\n"
        "    }\n}\n"
    )
    return root


def test_push_patcher_is_idempotent(tmp_path):
    root = checkout(tmp_path)
    for _ in range(2):
        subprocess.run(["python3", str(PATCHER), str(root)], check=True)
    source = root / "Sources/iCloudBridge"
    routes = (source / "API/Routes.swift").read_text()
    manager = (source / "Services/ServerManager.swift").read_text()
    reminders = (source / "Services/RemindersService.swift").read_text()
    assert routes.count('api.webSocket("changes")') == 1
    assert manager.count("private let changeHub = ChangeHub()") == 1
    assert manager.count("private var notesChangeMonitor: NotesChangeMonitor?") == 1
    assert manager.count("notesChangeMonitor = NotesChangeMonitor") == 1
    assert reminders.count("private func scheduleEventStoreRefresh()") == 1
    assert (source / "API/ChangeHub.swift").read_text() == (
        ROOT / "macos/push/ChangeHub.swift"
    ).read_text()
    assert (source / "API/NotesChangeMonitor.swift").read_text() == (
        ROOT / "macos/push/NotesChangeMonitor.swift"
    ).read_text()


def test_push_patcher_does_not_partially_modify_unknown_checkout(tmp_path):
    root = checkout(tmp_path, valid_manager=False)
    source = root / "Sources/iCloudBridge"
    before = {path: path.read_bytes() for path in source.rglob("*.swift")}
    result = subprocess.run(["python3", str(PATCHER), str(root)], capture_output=True, text=True)
    assert result.returncode != 0
    assert "no files changed" in result.stderr
    assert not (source / "API/ChangeHub.swift").exists()
    assert not (source / "API/NotesChangeMonitor.swift").exists()
    assert all(path.read_bytes() == contents for path, contents in before.items())
