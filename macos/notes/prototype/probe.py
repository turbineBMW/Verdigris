#!/usr/bin/env python3
"""Mac-only driver for the isolated native checklist experiment.

Run `python3 probe.py create`, then `python3 probe.py inspect` after granting
Accessibility to the helper. All writes are restricted to the registered fixture.
"""
from __future__ import annotations

import argparse
import html
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

from checklist_store import FIXTURE_PREFIX, read_fixture

DIRECTORY = Path.home() / "Library/Application Support/VerdigrisChecklistProbe"
MANIFEST = DIRECTORY / "fixture.json"


def save(path: Path, value: dict):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
    temporary.replace(path)


def applescript(source: str, *arguments: str) -> str:
    result = subprocess.run(["osascript", "-e", source, "--", *arguments],
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError("Fixture AppleScript failed: " + result.stderr.strip())
    return result.stdout.strip()


def fixture() -> dict:
    result = json.loads(MANIFEST.read_text())
    if not result["title"].startswith(FIXTURE_PREFIX):
        raise RuntimeError("Invalid fixture manifest")
    return result


def show():
    current = fixture()
    applescript('''on run argv
        tell application "Notes"
            set n to note id (item 1 of argv)
            if name of n is not (item 2 of argv) then error "Fixture title changed"
            if password protected of n then error "Protected note"
            show n
            activate
        end tell
    end run''', current["noteId"], current["title"])


def job(operation: str, **arguments) -> dict:
    status = json.loads((DIRECTORY / "status.json").read_text())
    if not status.get("trusted"):
        raise RuntimeError("Grant Accessibility to Verdigris Checklist Probe first")
    job_id = uuid.uuid4().hex
    if (DIRECTORY / "job.json").exists():
        raise RuntimeError("Another helper job is pending")
    save(DIRECTORY / "job.json", {"id": job_id, "operation": operation, **arguments})
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            result = json.loads((DIRECTORY / "result.json").read_text())
            if result.get("id") == job_id:
                if not result.get("ok"):
                    raise RuntimeError(result.get("error", "Helper failed"))
                return result
        except FileNotFoundError:
            pass
        time.sleep(0.1)
    raise RuntimeError("Helper outcome unknown; inspect state before retrying")


def snapshot() -> dict:
    return read_fixture(fixture()["noteId"])


def wait_for(predicate, timeout=12) -> dict:
    deadline = time.monotonic() + timeout
    last_error = "Stored state did not match expectation"
    while time.monotonic() < deadline:
        try:
            state = snapshot()
            if predicate(state):
                return state
        except (ValueError, OSError) as error:
            last_error = str(error)
        time.sleep(0.25)
    raise RuntimeError(last_error)


def editor_text(state: dict) -> str:
    inspected = job("inspect")
    matches = [e["text"] for e in inspected["editors"] if e["fixture"]]
    if len(matches) != 1:
        raise RuntimeError("Expected exactly one disposable-note editor")
    # Notes may omit a trailing paragraph separator in its AX representation.
    if matches[0].rstrip("\n") != state["text"].rstrip("\n"):
        raise RuntimeError("Accessibility text and stored document disagree")
    return matches[0]


def create() -> dict:
    if MANIFEST.exists():
        raise RuntimeError("A fixture is already registered; reuse it or run cleanup")
    title = FIXTURE_PREFIX + uuid.uuid4().hex[:12]
    lines = [title, "Repeated item", "Emoji 🧪 café", "Repeated item", "Nested child", "Final item"]
    body = "".join("<div>" + html.escape(line) + "</div>" for line in lines)
    note_id = applescript('''on run argv
        tell application "Notes"
            set a to default account
            set f to default folder of a
            set n to make new note at f with properties {body:item 1 of argv}
            return id of n
        end tell
    end run''', body)
    save(MANIFEST, {"title": title, "noteId": note_id})
    show()
    return wait_for(lambda state: state["text"].startswith(title))


def convert() -> dict:
    show()
    before = snapshot()
    if before["items"]:
        raise RuntimeError("Fixture already has native checklist items")
    expected = editor_text(before)
    start = len((before["text"].split("\n", 1)[0] + "\n").encode("utf-16-le")) // 2
    size = len(expected.rstrip("\n").encode("utf-16-le")) // 2
    job("checklist", expectedText=expected, location=start, length=size - start)
    return wait_for(lambda state: len(state["items"]) == 5)


def set_checked(item_id: str, desired: bool, revision: str) -> dict:
    show()
    before = snapshot()
    if before["revision"] != revision:
        raise RuntimeError("Revision conflict; reread before editing")
    matches = [item for item in before["items"] if item["id"] == item_id]
    if len(matches) != 1:
        raise RuntimeError("Checklist item no longer exists")
    target = matches[0]
    if target["checked"] == desired:
        return {"changed": False, "snapshot": before}
    expected = editor_text(before)
    if snapshot()["revision"] != revision:
        raise RuntimeError("Revision changed while locating the editor")
    job("toggle", expectedText=expected, location=target["location"], length=0)
    def verified(state):
        old = {item["id"]: item for item in before["items"]}
        new = {item["id"]: item for item in state["items"]}
        if old.keys() != new.keys() or state["attachmentRuns"] != before["attachmentRuns"]:
            return False
        for key, item in old.items():
            expected_checked = desired if key == item_id else item["checked"]
            if (new[key]["checked"] != expected_checked or new[key]["text"] != item["text"]
                    or new[key]["indent"] != item["indent"]):
                return False
        return True
    return {"changed": True, "snapshot": wait_for(verified)}


def edit_item(item_id: str, replacement: str, revision: str) -> dict:
    if not replacement or "\n" in replacement or "\r" in replacement or len(replacement.encode()) >= 4096:
        raise ValueError("Use a nonempty single-line checklist label under 4 KiB")
    show()
    before = snapshot()
    if before["revision"] != revision:
        raise RuntimeError("Revision conflict; reread before editing")
    matches = [item for item in before["items"] if item["id"] == item_id]
    if len(matches) != 1:
        raise RuntimeError("Checklist item no longer exists")
    target = matches[0]
    if target["text"] == replacement:
        return {"changed": False, "snapshot": before}
    expected = editor_text(before)
    if snapshot()["revision"] != revision:
        raise RuntimeError("Revision changed while locating the editor")
    job("replace", expectedText=expected, location=target["location"], length=target["length"],
        replacement=replacement)
    def verified(state):
        old = {item["id"]: item for item in before["items"]}
        new = {item["id"]: item for item in state["items"]}
        if old.keys() != new.keys() or state["attachmentRuns"] != before["attachmentRuns"]:
            return False
        return all(new[key]["text"] == (replacement if key == item_id else item["text"])
                   and new[key]["checked"] == item["checked"] and new[key]["indent"] == item["indent"]
                   for key, item in old.items())
    return {"changed": True, "snapshot": wait_for(verified)}


def cleanup() -> dict:
    current = fixture()
    # Only the recorded ID/title may be removed. Notes may move it to Recently
    # Deleted on the first call, requiring a second deletion with the same ID.
    source = '''on run argv
        tell application "Notes"
            try
                set n to note id (item 1 of argv)
                if name of n is not (item 2 of argv) then error "Fixture title changed"
                delete n
                return "deleted"
            on error number -1728
                return "absent"
            end try
        end tell
    end run'''
    for _ in range(3):
        result = applescript(source, current["noteId"], current["title"])
        if result == "absent":
            MANIFEST.unlink()
            return {"cleaned": True}
        time.sleep(0.3)
    raise RuntimeError("Fixture deletion needs verification; manifest retained")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["create", "inspect", "read", "convert", "set", "edit", "cleanup", "job", "rpc"])
    parser.add_argument("--item")
    parser.add_argument("--checked", choices=["true", "false"])
    parser.add_argument("--revision")
    parser.add_argument("--text")
    args = parser.parse_args()
    if args.action == "create": result = create()
    elif args.action == "read": result = snapshot()
    elif args.action == "cleanup": result = cleanup()
    elif args.action == "convert": result = convert()
    elif args.action == "set":
        if not all([args.item, args.checked, args.revision]):
            parser.error("set requires --item, --checked, and --revision")
        result = set_checked(args.item, args.checked == "true", args.revision)
    elif args.action == "edit":
        if not all([args.item, args.text, args.revision]):
            parser.error("edit requires --item, --text, and --revision")
        result = edit_item(args.item, args.text, args.revision)
    elif args.action == "inspect":
        show()
        result = job("inspect")
    elif args.action == "rpc":
        import sys
        request = json.load(sys.stdin)
        operation = request["operation"]
        if operation == "read": result = snapshot()
        elif operation == "set":
            if type(request["checked"]) is not bool:
                raise ValueError("checked must be a boolean")
            result = set_checked(request["item"], request["checked"], request["revision"])["snapshot"]
        elif operation == "edit":
            result = edit_item(request["item"], request["text"], request["revision"])["snapshot"]
        else: raise ValueError("Unknown preview operation")
    else:
        import sys
        payload = json.load(sys.stdin)
        show()
        result = job(**payload)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
