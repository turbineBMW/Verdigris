#!/usr/bin/env python3
"""Run under dbus-run-session + xvfb-run; uses only disposable fixture data."""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time

root = Path(__file__).resolve().parents[1]
binary = root / "target/debug"
artifacts = Path(tempfile.mkdtemp(prefix="verdigris-preview-"))
with tempfile.TemporaryDirectory(prefix="verdigris-test-") as temporary:
    temporary = Path(temporary)
    env = dict(os.environ, XDG_CONFIG_HOME=str(temporary / "config"),
               XDG_DATA_HOME=str(temporary / "data"), XDG_STATE_HOME=str(temporary / "state"), GDK_BACKEND="x11", GTK_A11Y="none",
               GDK_SCALE="1", GDK_DPI_SCALE="1", ADW_DEBUG_COLOR_SCHEME="prefer-dark", XDG_CURRENT_DESKTOP="", WAYLAND_DISPLAY="")
    config = temporary / "config/verdigris"
    data = temporary / "data/verdigris"
    config.mkdir(parents=True)
    data.mkdir(parents=True)
    (data / "notification-apps.json").write_text(json.dumps({"com.example.chat": "Example Chat"}))
    applications = temporary / "data/applications"
    applications.mkdir()
    (applications / "verdigris-fixture.desktop").write_text("[Desktop Entry]\nType=Application\nName=Verdigris Fixture Chat\nExec=false\nIcon=mail-message-new\n")
    server = "http://127.0.0.1:1/"
    (config / "connection.json").write_text(json.dumps({"server": server}))
    connection = sqlite3.connect(data / (hashlib.sha256(server.encode()).hexdigest() + ".sqlite"))
    connection.executescript("""
        CREATE TABLE chats(guid TEXT PRIMARY KEY,data TEXT NOT NULL,date INTEGER NOT NULL);
        CREATE TABLE messages(chat TEXT NOT NULL,guid TEXT NOT NULL,data TEXT NOT NULL,date INTEGER NOT NULL,PRIMARY KEY(chat,guid));
        CREATE TABLE sync(id INTEGER PRIMARY KEY,watermark INTEGER NOT NULL);
    """)
    messages = [
        {"guid": "one", "text": "Did the messages come through while your laptop was off?", "dateCreated": 1788793200000, "isFromMe": False, "handle": {"address": "Alex"}},
        {"guid": "two", "text": "Yes! They're here when I open the app.", "dateCreated": 1788793260000, "isFromMe": True, "dateDelivered": 1788793265000},
        {"guid": "three", "text": "Great. See you at dinner.", "dateCreated": 1788793320000, "isFromMe": False, "handle": {"address": "Alex"}},
    ]
    chat = {"guid": "fixture-chat", "displayName": "Alex", "lastMessage": messages[-1]}
    connection.execute("INSERT INTO chats VALUES(?,?,?)", (chat["guid"], json.dumps(chat), 1788793320000))
    for message in messages:
        connection.execute("INSERT INTO messages VALUES(?,?,?,?)", (chat["guid"], message["guid"], json.dumps(message), message["dateCreated"]))
    connection.commit()
    connection.close()
    contact_dir = temporary / "state/verdigris"
    contact_dir.mkdir(parents=True)
    photo = contact_dir / "fixture.svg"
    photo.write_text("<svg xmlns='http://www.w3.org/2000/svg' width='64' height='64'><rect width='64' height='64' fill='#3584e4'/><circle cx='32' cy='23' r='12' fill='white'/><ellipse cx='32' cy='62' rx='24' ry='24' fill='white'/></svg>")
    notification_icons = config / "notification-icons"
    notification_icons.mkdir()
    (notification_icons / "fixture.svg").write_text(photo.read_text())
    (config / "notification-rules.json").write_text(json.dumps({"version": 1, "apps": {
        "com.example.chat": {"enabled": True, "desktop_id": None, "icon": "fixture.svg"}
    }}))
    book = sqlite3.connect(contact_dir / "contacts.sqlite")
    book.executescript("CREATE TABLE contacts(id INTEGER PRIMARY KEY,full_name TEXT,nickname TEXT,photo_path TEXT); CREATE TABLE phones(phone_norm TEXT,contact_id INTEGER);")
    book.execute("INSERT INTO contacts VALUES(1,'Alex Example','Alex',?)", (str(photo),))
    book.execute("INSERT INTO phones VALUES('15555550100',1)")
    book.commit()
    book.close()
    connection = sqlite3.connect(data / (hashlib.sha256(server.encode()).hexdigest() + ".sqlite"))
    chat['participants'] = [{'address': '+15555550100'}]
    connection.execute('UPDATE chats SET data=?', (json.dumps(chat),))
    # A previously downloaded attachment exercises inline image rendering offline.
    attachment_guid = 'fixture-image'
    attachment_name = 'photo.svg'
    attachment_path = data / 'attachments' / hashlib.sha256(server.encode()).hexdigest() / hashlib.sha256(attachment_guid.encode()).hexdigest() / attachment_name
    attachment_path.parent.mkdir(parents=True)
    attachment_path.write_text(photo.read_text())
    messages[-1]['attachments'] = [{'guid':attachment_guid,'transferName':attachment_name,'mimeType':'image/svg+xml'}]
    connection.execute('UPDATE messages SET data=? WHERE guid=?', (json.dumps(messages[-1]),messages[-1]['guid']))
    connection.commit()
    connection.close()
    connection = sqlite3.connect(data / (hashlib.sha256(server.encode()).hexdigest() + ".sqlite"))
    second = {"guid": "second-chat", "displayName": "Sam"}
    connection.execute("INSERT INTO chats VALUES(?,?,?)", (second["guid"], json.dumps(second), 1788790000000))
    connection.commit()
    connection.close()
    log = (artifacts / "smoke.log").open("w")
    worker = subprocess.Popen([binary / "verdigris-sync"], env=env, stdout=log, stderr=log)
    try:
        time.sleep(0.5)
        assert worker.poll() is None, "Sync service exited"
        call = ["gdbus", "call", "--session", "--dest", "dev.turbinebmw.Verdigris.Sync", "--object-path", "/dev/turbinebmw/Verdigris/Sync", "--method"]
        result = subprocess.check_output(call + ["dev.turbinebmw.Verdigris.Sync1.Chats"], env=env, text=True)
        assert "Alex" in result, result
        result = subprocess.check_output(call + ["dev.turbinebmw.Verdigris.Sync1.Messages", "fixture-chat", "100"], env=env, text=True)
        assert "dinner" in result, result
        subprocess.check_call(call + ["dev.turbinebmw.Verdigris.Sync1.Refresh"], env=env, stdout=log)

        for name in ("messages", "phone", "settings"):
            app = subprocess.Popen([binary / f"verdigris-{name}"], env=env, stdout=log, stderr=log)
            try:
                time.sleep(1.5)
                assert app.poll() is None, f"{name} exited"
                if name == "messages":
                    subprocess.run(["import", "-window", "root", str(artifacts / "empty.png")], env=env, check=True)
                    x11 = ctypes.CDLL("libX11.so.6")
                    xtst = ctypes.CDLL("libXtst.so.6")
                    x11.XOpenDisplay.restype = ctypes.c_void_p
                    display = x11.XOpenDisplay(None)
                    xtst.XTestFakeMotionEvent.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_ulong]
                    xtst.XTestFakeButtonEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
                    x11.XFlush.argtypes = [ctypes.c_void_p]
                    xtst.XTestFakeKeyEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
                    x11.XStringToKeysym.argtypes = [ctypes.c_char_p]
                    x11.XStringToKeysym.restype = ctypes.c_ulong
                    x11.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
                    x11.XKeysymToKeycode.restype = ctypes.c_uint
                    def click(x, y):
                        xtst.XTestFakeMotionEvent(display, -1, x, y, 0)
                        xtst.XTestFakeButtonEvent(display, 1, 1, 0)
                        xtst.XTestFakeButtonEvent(display, 1, 0, 0)
                        x11.XFlush(display)
                    def key(name, control=False):
                        code = x11.XKeysymToKeycode(display, x11.XStringToKeysym(name.encode()))
                        ctrl = x11.XKeysymToKeycode(display, x11.XStringToKeysym(b"Control_L"))
                        if control: xtst.XTestFakeKeyEvent(display, ctrl, 1, 0)
                        xtst.XTestFakeKeyEvent(display, code, 1, 0)
                        xtst.XTestFakeKeyEvent(display, code, 0, 0)
                        if control: xtst.XTestFakeKeyEvent(display, ctrl, 0, 0)
                        x11.XFlush(display)
                    def copied_draft():
                        key("a", control=True)
                        key("c", control=True)
                        time.sleep(0.2)
                        # Read only this disposable X server's clipboard.
                        return subprocess.check_output([sys.executable, "-c", """
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, Gdk, GLib
Gtk.init()
loop = GLib.MainLoop()
def done(clipboard, result):
    print(clipboard.read_text_finish(result), end='')
    loop.quit()
Gdk.Display.get_default().get_clipboard().read_text_async(None, done)
GLib.timeout_add_seconds(3, loop.quit)
loop.run()"""], env=env, text=True, timeout=5)
                    click(90, 85)
                    time.sleep(0.7)
                    assert app.poll() is None, "Messages crashed on conversation activation"
                subprocess.run(["import", "-window", "root", str(artifacts / f"{name}.png")], env=env, check=True)
                if name == 'messages':
                    click(520, 666)
                    for character in ["h", "i", "Return", "t", "h", "e", "r", "e"]:
                        key(character)
                    time.sleep(0.2)
                    assert copied_draft() == "hi\nthere", "Enter should insert a newline"
                    click(90, 145)
                    time.sleep(0.4)
                    click(90, 85)
                    time.sleep(0.4)
                    click(520, 666)
                    assert copied_draft() == "hi\nthere", "Switching chats lost the multiline draft"
                    key("Right")
                    subprocess.run(['import', '-window', 'root', str(artifacts / 'multiline.png')], env=env, check=True)
                    # A failed send to the disposable, unconfigured service must retain the draft.
                    key("Return", control=True)
                    time.sleep(0.5)
                    assert copied_draft() == "hi\nthere", "Failed send lost the draft"
                    key("BackSpace")
                    # Bubo's GIF control is next to the attachment button at the bottom.
                    xtst.XTestFakeMotionEvent(display, -1, 343, 670, 0)
                    xtst.XTestFakeButtonEvent(display, 1, 1, 0)
                    xtst.XTestFakeButtonEvent(display, 1, 0, 0)
                    x11.XFlush(display)
                    time.sleep(0.5)
                    subprocess.run(['import', '-window', 'root', str(artifacts / 'gif-picker.png')], env=env, check=True)
                    # Close the popover before opening a dialog.
                    xtst.XTestFakeMotionEvent(display, -1, 700, 200, 0)
                    xtst.XTestFakeButtonEvent(display, 1, 1, 0)
                    xtst.XTestFakeButtonEvent(display, 1, 0, 0)
                    x11.XFlush(display)
                    subprocess.run(['gdbus','call','--session','--dest','dev.turbinebmw.Verdigris.Messages',
                                    '--object-path','/dev/turbinebmw/Verdigris/Messages','--method','org.gtk.Actions.Activate',
                                    'new-message','[]','{}'],env=env,check=True,stdout=log)
                    time.sleep(1)
                    assert app.poll() is None, 'Messages crashed opening new-message composer'
                    subprocess.run(['import','-window','root',str(artifacts / 'compose.png')],env=env,check=True)
                if name == "settings":
                    click(300, 650)  # iPhone app notifications
                    time.sleep(0.8)
                    subprocess.run(['import', '-window', 'root', str(artifacts / 'notification-apps.png')], env=env, check=True)
                    click(250, 205)  # Example Chat
                    time.sleep(0.8)
                    subprocess.run(['import', '-window', 'root', str(artifacts / 'notification-rule.png')], env=env, check=True)
                    click(500, 181)  # Disable this source app.
                    click(250, 235)  # Select a click target.
                    time.sleep(0.8)
                    click(230, 130)
                    for character in "fixture":
                        key(character)
                    time.sleep(0.8)
                    subprocess.run(['import', '-window', 'root', str(artifacts / 'notification-app-picker.png')], env=env, check=True)
                    click(250, 185)
                    time.sleep(0.5)
                    click(508, 291)  # Reset custom icon to automatic.
                    click(482, 73)   # Save.
                    time.sleep(0.8)
                    rules = json.loads((config / 'notification-rules.json').read_text())
                    assert rules['apps']['com.example.chat'] == {
                        'enabled': False, 'desktop_id': 'verdigris-fixture.desktop', 'icon': None,
                    }, rules
                    assert app.poll() is None, 'Settings crashed saving notification preferences'
            finally:
                app.terminate()
                app.wait(timeout=10)
    finally:
        worker.send_signal(signal.SIGINT)
        worker.wait(timeout=10)
        log.close()
    errors = (artifacts / "smoke.log").read_text()
    assert "panicked" not in errors, errors
    assert "CRITICAL" not in errors, errors
print(f"D-Bus cache reads and all three GTK windows passed. Screenshots: {artifacts}")
