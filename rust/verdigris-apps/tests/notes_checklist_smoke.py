#!/usr/bin/env python3
"""GTK checklist/edit/conflict smoke test with a disposable HTTP fixture."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

root = Path(__file__).resolve().parents[1]
artifacts = Path(tempfile.mkdtemp(prefix="verdigris-checklist-smoke-"))
version = 1
items = [dict(id="milk",text="Milk 🥛",checked=False,indent=0,canToggle=True,canEdit=True),
         dict(id="bread",text="Bread",checked=False,indent=0,canToggle=True,canEdit=True)]
writes = []
conflict = False
offline = False

def detail():
    text = "Groceries\n"
    rows = []
    for item in items:
        rows.append(dict(item,location=len(text.encode("utf-16-le"))//2,length=len(item["text"].encode("utf-16-le"))//2))
        text += item["text"] + "\n"
    note = dict(id="one",title="Groceries",text=text,folderId="folder",locked=False,attachments=[])
    return dict(note=note,revision=f"{version:064x}",items=rows,editable=True,reason=None)

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def reply(self,body,status=200):
        data=json.dumps(body).encode(); self.send_response(status); self.send_header("Content-Length",str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        if offline: self.reply({},503)
        elif self.path=="/api/v1/notes": self.reply(dict(folders=[dict(id="folder",title="iCloud / Notes")],notes=[detail()["note"]]))
        elif self.path=="/api/v1/notes/one/checklist": self.reply(detail())
        else: self.reply({},404)
    def do_PATCH(self):
        global version, conflict
        data=json.loads(self.rfile.read(int(self.headers["Content-Length"]))); writes.append(data)
        target=self.path.rsplit("/",1)[-1]
        if conflict:
            conflict=False; version+=1
            next(i for i in items if i["id"]==target)["text"]="Changed on iPhone"
            self.reply({},409); return
        assert data["revision"]==detail()["revision"]
        item=next(i for i in items if i["id"]==target)
        if "checked" in data: item["checked"]=data["checked"]
        if "text" in data: item["text"]=data["text"]
        version+=1; self.reply(detail())

def wait_for(predicate):
    deadline=time.monotonic()+12
    while time.monotonic()<deadline:
        if predicate(): return
        time.sleep(.1)
    raise AssertionError("Timed out waiting for checklist fixture")

server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
threading.Thread(target=server.serve_forever,daemon=True).start()
with tempfile.TemporaryDirectory(prefix="verdigris-checklist-fixture-") as tmp:
    env=dict(os.environ,XDG_CONFIG_HOME=tmp+"/config",XDG_DATA_HOME=tmp+"/data",GDK_BACKEND="x11",GTK_A11Y="none",GDK_SCALE="1",GDK_DPI_SCALE="1",WAYLAND_DISPLAY="",XDG_CURRENT_DESKTOP="",GSK_RENDERER="cairo")
    config=Path(tmp)/"config/verdigris"; config.mkdir(parents=True)
    (config/"reminders.json").write_text(json.dumps(dict(server=f"http://127.0.0.1:{server.server_port}/")))
    def xdo(*args): subprocess.run(["xdotool",*args],env=env,check=True)
    def click(x,y): xdo("mousemove",str(x),str(y),"click","1"); time.sleep(.3)
    def shot(name): subprocess.run(["import","-window","root",str(artifacts/f"{name}.png")],env=env,check=True)
    def cache():
        paths=list((Path(tmp)/"data/verdigris").glob("notes-*.json"))
        return json.loads(paths[0].read_text()) if paths else {}
    with (artifacts/"smoke.log").open("w") as log:
        app=subprocess.Popen([root/"target/debug/verdigris-notes"],env=env,stdout=log,stderr=log)
        try:
            wait_for(lambda: "one" in cache().get("checklists",{})); time.sleep(.5); shot("checklist")
            if not os.environ.get("NOTES_PREVIEW_ONLY"):
                click(301,167)
                wait_for(lambda:len(writes)==1)
                assert writes[0]["checked"] is True
                wait_for(lambda:cache()["checklists"]["one"]["items"][0]["checked"])
                shot("checked")
                click(954,167); time.sleep(.4); shot("editor")
                # The following coordinates target the centered item-edit dialog.
                click(320,328); xdo("key","ctrl+a"); xdo("type","Draft from Linux")
                conflict=True
                click(320,452)
                wait_for(lambda:len(writes)==2); time.sleep(.5); shot("conflict")
                assert writes[1]["text"]=="Draft from Linux"
                click(320,376); time.sleep(.4)
                assert len(writes)==2,"Failed write replayed without reload"
                click(320,426); time.sleep(.5); shot("reloaded")
                if not os.environ.get("NOTES_RELOAD_PREVIEW"):
                    click(320,496)
                    wait_for(lambda:len(writes)==3)
                    assert writes[2]["text"]=="Draft from Linux","Reload discarded the draft"
                    assert writes[2]["revision"]!=writes[1]["revision"]
                    wait_for(lambda:cache()["checklists"]["one"]["items"][0]["text"]=="Draft from Linux")
                    time.sleep(.5); shot("saved")
                    saved=cache(); offline=True; xdo("key","ctrl+r"); time.sleep(.7)
                    click(301,167); time.sleep(.4)
                    assert len(writes)==3,"Offline checkbox dispatched a write"
                    assert cache()==saved,"Offline failure changed the cached checklist"
                    shot("offline")
            assert app.poll() is None
        finally: app.terminate(); app.wait(timeout=10)
    errors=(artifacts/"smoke.log").read_text()
    assert "panicked" not in errors and "CRITICAL" not in errors,errors
server.shutdown()
print("Checklist UI checks completed. Screenshots:",artifacts)
