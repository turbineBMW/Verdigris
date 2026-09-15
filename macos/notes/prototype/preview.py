#!/usr/bin/env python3
"""Linux GTK preview for the registered disposable Mac checklist only."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import subprocess
import sys

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk


class Preview(Gtk.Application):
    def __init__(self, mac):
        super().__init__(application_id="dev.turbinebmw.Verdigris.ChecklistPreview",
                         flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.mac = mac
        self.worker = ThreadPoolExecutor(max_workers=1)
        self.state = None
        self.connect("activate", self.activate_window)

    def activate_window(self, _):
        self.window = Gtk.ApplicationWindow(application=self, title="Native Notes Checklist Prototype")
        self.window.set_default_size(650, 450)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                        margin_top=20, margin_bottom=20, margin_start=20, margin_end=20)
        self.window.set_child(outer)
        outer.append(Gtk.Label(label="Disposable test note · changes are saved in Apple Notes", xalign=0))
        self.title = Gtk.Label(label="", xalign=0, selectable=True)
        outer.append(self.title)
        self.rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_child(self.rows)
        outer.append(scroll)
        self.status = Gtk.Label(label="", xalign=0, wrap=True, selectable=True)
        outer.append(self.status)
        self.refresh = Gtk.Button(label="Refresh from Mac")
        self.refresh.connect("clicked", lambda _: self.request({"operation": "read"}))
        outer.append(self.refresh)
        self.window.present()
        self.request({"operation": "read"})

    def remote(self, payload):
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", self.mac,
             "cd ~/Projects/verdigris-checklist-prototype && python3 probe.py rpc"],
            input=json.dumps(payload), capture_output=True, text=True, timeout=90)
        if result.returncode:
            raise RuntimeError(result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "Mac request failed")
        state = json.loads(result.stdout)
        if not state["text"].startswith("Verdigris Checklist Probe "):
            raise RuntimeError("Mac returned a non-fixture note")
        return state

    def request(self, payload):
        self.rows.set_sensitive(False)
        self.refresh.set_sensitive(False)
        self.status.set_text("Reading from Mac…" if payload["operation"] == "read" else "Saving through Mac…")
        future = self.worker.submit(self.remote, payload)
        future.add_done_callback(lambda completed: GLib.idle_add(self.finish, completed))

    def finish(self, future):
        self.refresh.set_sensitive(True)
        try:
            self.state = future.result()
        except Exception as error:
            self.status.set_text(str(error) + " — Refresh before making another change.")
            return GLib.SOURCE_REMOVE
        self.title.set_text(self.state["text"].split("\n", 1)[0])
        while child := self.rows.get_first_child():
            self.rows.remove(child)
        for item in self.state["items"]:
            row = Gtk.Box(spacing=8, margin_start=min(item["indent"], 8) * 20)
            check = Gtk.CheckButton(active=item["checked"])
            check.connect("toggled", self.toggle, item["id"])
            row.append(check)
            label = Gtk.Entry(text=item["text"], hexpand=True)
            row.append(label)
            save = Gtk.Button(label="Save text")
            save.connect("clicked", self.edit, item["id"], label)
            row.append(save)
            self.rows.append(row)
        self.rows.set_sensitive(True)
        self.status.set_text("Verified against the Mac’s stored note. iCloud sync may follow shortly.")
        return GLib.SOURCE_REMOVE

    def toggle(self, button, identifier):
        self.request({"operation": "set", "item": identifier, "checked": button.get_active(),
                      "revision": self.state["revision"]})

    def edit(self, _, identifier, entry):
        self.request({"operation": "edit", "item": identifier, "text": entry.get_text(),
                      "revision": self.state["revision"]})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mac", default="192.168.1.150")
    args = parser.parse_args()
    app = Preview(args.mac)
    try:
        app.run([sys.argv[0]])
    finally:
        app.worker.shutdown(wait=False, cancel_futures=True)
