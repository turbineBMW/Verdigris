#!/usr/bin/env python3
"""Disposable API + GTK exercise. Run under dbus-run-session and xvfb-run.

Requires xdotool and ImageMagick. Never connects to the user's Mac or keyring.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

root = Path(__file__).resolve().parents[1]
artifacts = Path(tempfile.mkdtemp(prefix="verdigris-reminders-smoke-"))
items = [
    dict(id="one", title="Buy coffee beans", notes="Keep this note", isCompleted=False,
         dueDate="2026-09-13T14:00:00Z", listId="personal"),
    dict(id="two", title="Plan the weekend", notes="Trail walk", isCompleted=False,
         dueDate=None, listId="personal"),
]
writes = []
offline = False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def reply(self, body, status=200):
        body = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # Model an older bridge without the live WebSocket endpoint.
        if self.path == "/api/v1/changes":
            self.send_error(404)
        elif offline:
            self.reply({}, 503)
        elif self.path == "/api/v1/lists":
            self.reply([dict(id="personal", title="Personal", reminderCount=2)])
        else:
            assert self.path == "/api/v1/lists/personal/reminders?includeCompleted=true"
            self.reply(items)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        writes.append(("POST", body))
        item = dict(id="new", isCompleted=False, dueDate=None, listId="personal", **body)
        items.append(item)
        self.reply(item)

    def do_PUT(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        writes.append(("PUT", body))
        item = next(item for item in items if self.path.endswith("/" + item["id"]))
        item.update(body)
        self.reply(item)


def wait_for(predicate):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError("Timed out waiting for fixture state")


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
with tempfile.TemporaryDirectory(prefix="verdigris-reminders-fixture-") as tmp:
    env = dict(os.environ, XDG_CONFIG_HOME=tmp + "/config", XDG_DATA_HOME=tmp + "/data",
               GDK_BACKEND="x11", GTK_A11Y="none", GDK_SCALE="1", GDK_DPI_SCALE="1",
               WAYLAND_DISPLAY="", XDG_CURRENT_DESKTOP="", GSK_RENDERER="cairo")
    config = Path(tmp) / "config/verdigris"
    config.mkdir(parents=True)
    (config / "reminders.json").write_text(json.dumps(dict(server=f"http://127.0.0.1:{server.server_port}/")))

    def xdo(*args):
        subprocess.run(["xdotool", *args], env=env, check=True)

    def click(x, y):
        xdo("mousemove", str(x), str(y), "click", "1")
        time.sleep(0.2)

    def shot(name):
        subprocess.run(["import", "-window", "root", str(artifacts / f"{name}.png")], env=env, check=True)

    def cache():
        paths = list((Path(tmp) / "data/verdigris").glob("reminders-*.json"))
        return json.loads(paths[0].read_text()) if paths else {}

    with (artifacts / "smoke.log").open("w") as log:
        app = subprocess.Popen([root / "target/debug/verdigris-reminders"], env=env, stdout=log, stderr=log)
        try:
            wait_for(lambda: len(cache().get("reminders", [])) == 2)
            time.sleep(0.5)
            shot("reminders")
            click(44, 235)
            wait_for(lambda: writes == [("PUT", {"isCompleted": True})])
            wait_for(lambda: cache()["reminders"][0]["isCompleted"])
            time.sleep(0.4)
            xdo("key", "ctrl+n")
            time.sleep(0.5)
            click(250, 243)
            xdo("type", "Fixture reminder")
            click(230, 340)
            xdo("type", "Fixture notes")
            shot("editor")
            click(350, 503)
            wait_for(lambda: len(writes) == 2)
            assert writes[-1] == ("POST", {"title": "Fixture reminder", "notes": "Fixture notes"}), writes
            wait_for(lambda: len(cache().get("reminders", [])) == 3)
            time.sleep(0.5)
            click(567, 125)  # Show completed; edit the original first row.
            click(672, 235)
            time.sleep(0.5)
            click(250, 243)
            xdo("key", "ctrl+a")
            xdo("type", "Edited coffee reminder")
            click(350, 503)
            wait_for(lambda: len(writes) == 3)
            assert writes[-1] == ("PUT", {"title": "Edited coffee reminder"}), writes
            assert items[0]["notes"] == "Keep this note"
            assert items[0]["dueDate"] == "2026-09-13T14:00:00Z"
            wait_for(lambda: cache()["reminders"][0]["title"] == "Edited coffee reminder")
            time.sleep(0.5)
            saved_cache = cache()
            offline = True
            xdo("key", "ctrl+r")
            time.sleep(0.7)
            shot("offline")
            click(44, 235)
            time.sleep(0.3)
            assert len(writes) == 3, "Offline click wrote to the bridge"
            assert cache() == saved_cache, "Failed refresh replaced the cache"
            assert app.poll() is None, "Reminders crashed"
        finally:
            app.terminate()
            app.wait(timeout=10)
    errors = (artifacts / "smoke.log").read_text()
    assert "panicked" not in errors and "CRITICAL" not in errors, errors
server.shutdown()
print(f"Reminders create/edit/complete, cache, and offline GTK checks passed. Screenshots: {artifacts}")
