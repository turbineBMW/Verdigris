#!/usr/bin/env python3
"""Disposable GTK/API test. Run with dbus-run-session + xvfb-run; no real notes."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

root=Path(__file__).resolve().parents[1]
artifacts=Path(tempfile.mkdtemp(prefix='verdigris-notes-smoke-'))
notes=[dict(id='one',title='Weekend plans',folderId='personal',text='Weekend plans\nFind a café near the trail.\nBring sandwiches & coffee.',locked=False,attachments=['trail-map.pdf']),
       dict(id='two',title='Private note',folderId='personal',text='PROTECTED CONTENT MUST NOT APPEAR',locked=True,attachments=[])]
folders=[dict(id='personal',title='iCloud / Notes')]
writes=[]
offline=False
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*_args): pass
 def reply(self,body,status=200):
  body=json.dumps(body).encode(); self.send_response(status); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
 def do_GET(self):
  if self.path=='/api/v1/changes':
   self.reply({},404); return
  if self.path.endswith('/checklist'):
   self.reply({},404); return
  assert self.path=='/api/v1/notes',self.path
  self.reply({} if offline else dict(folders=folders,notes=notes),503 if offline else 200)
 def do_POST(self):
  assert self.path=='/api/v1/notes'
  data=json.loads(self.rfile.read(int(self.headers['Content-Length']))); writes.append(data)
  note=dict(id='new',locked=False,attachments=[],**data); notes.append(note); self.reply(note)
def wait_for(predicate):
 deadline=time.monotonic()+10
 while time.monotonic()<deadline:
  if predicate(): return
  time.sleep(.1)
 raise AssertionError('Timed out waiting for fixture state')
server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
threading.Thread(target=server.serve_forever,daemon=True).start()
with tempfile.TemporaryDirectory(prefix='verdigris-notes-fixture-') as tmp:
 env=dict(os.environ,XDG_CONFIG_HOME=tmp+'/config',XDG_DATA_HOME=tmp+'/data',GDK_BACKEND='x11',GTK_A11Y='none',GDK_SCALE='1',GDK_DPI_SCALE='1',WAYLAND_DISPLAY='',XDG_CURRENT_DESKTOP='',GSK_RENDERER='cairo')
 config=Path(tmp)/'config/verdigris'; config.mkdir(parents=True)
 (config/'reminders.json').write_text(json.dumps(dict(server=f'http://127.0.0.1:{server.server_port}/')))
 def xdo(*args): subprocess.run(['xdotool',*args],env=env,check=True)
 def click(x,y): xdo('mousemove',str(x),str(y),'click','1'); time.sleep(.2)
 def shot(name): subprocess.run(['import','-window','root',str(artifacts/f'{name}.png')],env=env,check=True)
 def cache():
  paths=list((Path(tmp)/'data/verdigris').glob('notes-*.json'))
  return json.loads(paths[0].read_text()) if paths else {}
 with (artifacts/'smoke.log').open('w') as log:
  app=subprocess.Popen([root/'target/debug/verdigris-notes'],env=env,stdout=log,stderr=log)
  try:
   wait_for(lambda:len(cache().get('notes',[]))==2); time.sleep(.5)
   assert cache()['notes'][1]['text']=='', 'Protected note content leaked into cache'
   shot('notes')
   click(100,79); xdo('type','sandwiches'); time.sleep(.5); shot('search')
   xdo('key','ctrl+a'); xdo('key','BackSpace'); time.sleep(.3)
   xdo('key','ctrl+n'); time.sleep(.5); shot('new-note')
   if os.environ.get('NOTES_PREVIEW_ONLY'): print('Preview:',artifacts)
   else:
    click(400,220); xdo('type','Linux fixture note')
    click(400,300); xdo('type','Literal <b>text</b> & quotes'); xdo('key','Return'); xdo('type','Second line')
    shot('draft'); xdo('key','ctrl+Tab'); xdo('key','Return')
    wait_for(lambda:len(writes)==1)
    assert writes[0]==dict(folderId='personal',title='Linux fixture note',text='Literal <b>text</b> & quotes\nSecond line'),writes
    wait_for(lambda:len(cache().get('notes',[]))==3); time.sleep(.5); shot('created')
    saved=cache(); offline=True; xdo('key','ctrl+r'); time.sleep(.8)
    assert cache()==saved,'Failed refresh replaced offline notes'
    xdo('key','ctrl+n'); time.sleep(.3); shot('offline')
    assert len(writes)==1
    assert app.poll() is None,'Notes crashed'
  finally: app.terminate(); app.wait(timeout=10)
 errors=(artifacts/'smoke.log').read_text()
 assert 'panicked' not in errors and 'CRITICAL' not in errors,errors
server.shutdown()
print('Notes UI checks completed. Screenshots:',artifacts)
