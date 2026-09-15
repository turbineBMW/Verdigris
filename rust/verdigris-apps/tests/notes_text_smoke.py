#!/usr/bin/env python3
"""Real GTK ordinary-note editing, conflict recovery, and offline smoke test."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

root=Path(__file__).resolve().parents[1]
artifacts=Path(tempfile.mkdtemp(prefix='verdigris-text-smoke-'))
state=dict(title='Ordinary note',text='Ordinary note\nFirst line\nSecond line\n')
version=1
writes=[]
reads=[]
conflict=True
offline=False
can_edit=True
delay_next=False

def detail():
    return dict(note=dict(id='one',folderId='folder',locked=False,attachments=[],**state),
                revision=f'{version:064x}',items=[],shared=True,editable=can_edit,canEditText=can_edit,
                reason=None if can_edit else "You have view-only access to this shared note")
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def reply(self,body,status=200):
        data=json.dumps(body).encode();self.send_response(status);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    def do_GET(self):
        reads.append(self.path)
        if offline:self.reply({},503)
        elif self.path=='/api/v1/notes':self.reply(dict(folders=[dict(id='folder',title='iCloud / Notes')],notes=[detail()['note']]))
        elif self.path=='/api/v1/notes/one/checklist':self.reply(detail())
        else:self.reply({},404)
    def do_PATCH(self):
        global version,conflict,can_edit,delay_next
        assert self.path=='/api/v1/notes/one/text'
        data=json.loads(self.rfile.read(int(self.headers['Content-Length'])));writes.append(data)
        if conflict:
            conflict=False;version+=1;state.update(title='Changed on iPhone',text='Changed on iPhone\nRemote body\n')
            if os.environ.get('NOTES_REVOKE_ON_CONFLICT'):
                can_edit=False;self.reply({},403)
            else:self.reply({},409)
            return
        if delay_next:
            delay_next=False;time.sleep(1.5)
        assert data['revision']==detail()['revision']
        text=data['title']+'\n'+data['text']
        if not text.endswith('\n'):text+='\n'
        state.update(title=data['title'],text=text);version+=1;self.reply(detail())
def wait_for(predicate):
    deadline=time.monotonic()+20
    while time.monotonic()<deadline:
        if predicate():return
        time.sleep(.1)
    raise AssertionError('Timed out waiting for text editor fixture')
server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
threading.Thread(target=server.serve_forever,daemon=True).start()
with tempfile.TemporaryDirectory(prefix='verdigris-text-fixture-') as tmp:
    env=dict(os.environ,XDG_CONFIG_HOME=tmp+'/config',XDG_DATA_HOME=tmp+'/data',GDK_BACKEND='x11',GTK_A11Y='none',GDK_SCALE='1',GDK_DPI_SCALE='1',WAYLAND_DISPLAY='',XDG_CURRENT_DESKTOP='',GSK_RENDERER='cairo')
    config=Path(tmp)/'config/verdigris';config.mkdir(parents=True)
    (config/'reminders.json').write_text(json.dumps(dict(server=f'http://127.0.0.1:{server.server_port}/')))
    def xdo(*args):subprocess.run(['xdotool',*args],env=env,check=True)
    def click(x,y):xdo('mousemove',str(x),str(y),'click','1');time.sleep(.3)
    def shot(name):subprocess.run(['import','-window','root',str(artifacts/f'{name}.png')],env=env,check=True)
    def cache():
        paths=list((Path(tmp)/'data/verdigris').glob('notes-*.json'))
        return json.loads(paths[0].read_text()) if paths else {}
    with (artifacts/'smoke.log').open('w') as log:
        app=subprocess.Popen([root/'target/debug/verdigris-notes'],env=env,stdout=log,stderr=log)
        try:
            wait_for(lambda:'one' in cache().get('checklists',{}));time.sleep(.4);shot('reader')
            # An older bridge returns 404 for live updates. Neither the full
            # collection nor the active note should fall back to 15-second polling.
            startup_reads=list(reads);time.sleep(16)
            assert reads==startup_reads and reads.count('/api/v1/notes')==1,reads
            # Supported notes are editable directly in the main pane. Ctrl+E
            # remains a convenience for focusing the inline title field.
            xdo('key','ctrl+e');time.sleep(.5);shot('editor')
            if not os.environ.get('NOTES_TEXT_PREVIEW'):
                click(460,92);xdo('key','ctrl+a');xdo('type','Draft title')
                click(410,175);xdo('key','ctrl+a');xdo('type','Draft body');xdo('key','Return','Return');xdo('type','New paragraph')
                wait_for(lambda:len(writes)==1);time.sleep(.5);shot('conflict')
                assert writes[0]['title']=='Draft title' and writes[0]['text']=='Draft body\n\nNew paragraph'
                time.sleep(1.2);assert len(writes)==1,'Failed autosave retried without review'
                xdo('key','ctrl+shift+r');time.sleep(.5);shot('reloaded')
                if os.environ.get('NOTES_REVOKE_ON_CONFLICT'):
                    time.sleep(1.2);assert len(writes)==1,'Autosaved after edit permission was revoked'
                    can_edit=True;version+=1
                    xdo('key','ctrl+r');time.sleep(.8);xdo('key','ctrl+shift+r');time.sleep(.3);shot('permission-restored')
                wait_for(lambda:len(writes)==2)
                assert writes[1]['title']==writes[0]['title'] and writes[1]['text']==writes[0]['text'],'Reload overwrote the draft'
                assert writes[1]['revision']!=writes[0]['revision']
                assert writes[1]['operationId']!=writes[0]['operationId']
                wait_for(lambda:cache()['notes'][0]['title']=='Draft title');time.sleep(.4);shot('saved')
                # Keystrokes made while a save is in flight must queue another
                # autosave against the newly acknowledged revision.
                delay_next=True
                click(410,175);xdo('key','ctrl+a');xdo('type','First queued body')
                wait_for(lambda:len(writes)==3)
                click(410,175);xdo('key','ctrl+a');xdo('type','Final queued body')
                wait_for(lambda:len(writes)==4)
                assert writes[3]['text']=='Final queued body'
                assert writes[3]['revision']!=writes[2]['revision']
                wait_for(lambda:cache()['notes'][0]['text']=='Draft title\nFinal queued body\n')
                saved=cache();offline=True;xdo('key','ctrl+r');time.sleep(.6)
                assert len(writes)==4 and cache()==saved;shot('offline')
            assert app.poll() is None
        finally:app.terminate();app.wait(timeout=10)
    errors=(artifacts/'smoke.log').read_text()
    assert 'panicked' not in errors and 'CRITICAL' not in errors,errors
server.shutdown()
print('Ordinary note UI checks completed. Screenshots:',artifacts)
