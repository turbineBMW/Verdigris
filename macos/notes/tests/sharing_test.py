#!/usr/bin/env python3
"""Mac-only CKShare permission tests. Uses synthetic archives, never an account."""
from pathlib import Path
import subprocess,tempfile,plistlib,copy,json,sqlite3,base64
root=Path(__file__).resolve().parent.parent
with tempfile.TemporaryDirectory() as directory:
    directory=Path(directory)
    (directory/'make.swift').write_text('''
import Foundation
import CloudKit
let share = CKShare(rootRecord: CKRecord(recordType: "Fixture", recordID: CKRecord.ID(recordName: "test-note")))
let coder = NSKeyedArchiver(requiringSecureCoding: true)
share.encodeSystemFields(with: coder)
coder.finishEncoding()
try coder.encodedData.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
''')
    subprocess.run(['swift',str(directory/'make.swift'),str(directory/'base.archive')],check=True)
    base=plistlib.loads((directory/'base.archive').read_bytes())
    variants={}
    for name,permission,acceptance,current in [('editable',3,2,True),('view-only',2,2,True),('pending',3,1,True),('removed',1,2,True),('unknown',0,2,True),('no-current-user',3,2,False)]:
        archive=copy.deepcopy(base);objects=archive['$objects']
        for value in objects:
            if isinstance(value,dict) and isinstance(value.get('$class'),plistlib.UID) and objects[value['$class'].data].get('$classname')=='CKShareParticipant':
                value.update(Permission=permission,AcceptanceStatus=acceptance,IsCurrentUser=current)
        variants[name]=plistlib.dumps(archive,fmt=plistlib.FMT_BINARY)
        (directory/(name+'.archive')).write_bytes(variants[name])
    database=directory/'Notes.sqlite';db=sqlite3.connect(database)
    db.execute('CREATE TABLE Z_METADATA (Z_UUID TEXT)');db.execute("INSERT INTO Z_METADATA VALUES ('TEST')")
    db.execute('CREATE TABLE ZICCLOUDSYNCINGOBJECT (Z_PK INTEGER, ZISPASSWORDPROTECTED INTEGER, ZMARKEDFORDELETION INTEGER, ZFOLDER INTEGER, ZFOLDERTYPE INTEGER, ZSERVERSHAREDATA BLOB, ZISSHAREDIRTY INTEGER, ZPARENT INTEGER)')
    db.execute('CREATE TABLE ZICNOTEDATA (ZNOTE INTEGER, ZDATA BLOB)')
    db.executemany('INSERT INTO ZICCLOUDSYNCINGOBJECT VALUES (?,?,?,?,?,?,?,?)',[(1,0,0,10,None,None,0,None),(10,0,0,None,0,None,0,12),(12,0,0,None,0,None,0,None)])
    blob=next(v['data'] for v in json.loads((root/'tests/documents.json').read_text()) if v['name']=='ordinary-text')
    db.execute('INSERT INTO ZICNOTEDATA VALUES (?,?)',(1,base64.b64decode(blob)));db.commit();db.close()
    source=(root/'NotesStore.swift').read_text().replace('import Vapor','''
typealias Content = Codable
enum Status { case unprocessableEntity, forbidden, notFound }
struct Abort: Error { init(_ status: Status, reason: String = "") {} }
''')
    (directory/'NotesStore.swift').write_text(source)
    (directory/'NotesSharing.swift').write_text((root/'NotesSharing.swift').read_text())
    (directory/'main.swift').write_text('''
import Foundation
import SQLite3
let directory = URL(fileURLWithPath: CommandLine.arguments[1])
func archive(_ name: String) throws -> Data { try Data(contentsOf: directory.appendingPathComponent(name + ".archive")) }
for (name, expected) in [("editable", NotesSharing.Access.editable), ("view-only", .readOnly), ("pending", .unknown), ("removed", .unknown), ("unknown", .unknown), ("no-current-user", .unknown)] {
    precondition(NotesSharing.decode(try archive(name)) == expected, "Incorrect permission for " + name)
    print("PASS", name)
}
precondition(NotesSharing.decode(Data("invalid".utf8)) == .unknown)
precondition(NotesSharing.decode(Data(repeating: 0, count: NotesSharing.limit + 1)) == .unknown)
precondition(!NotesSharing.Access.privateNote.matching(shared: true).allowsEditing)
precondition(!NotesSharing.Access.editable.matching(shared: false).allowsEditing)
let writable = try archive("editable"), readOnly = try archive("view-only")
precondition(NotesSharing.access(archives: [writable, readOnly], pending: false) == .readOnly)
precondition(NotesSharing.access(archives: [writable], pending: true) == .unknown)
let database = directory.appendingPathComponent("Notes.sqlite")
func read() throws -> NotesDocument { try NotesStore.read("x-coredata://TEST/ICNote/p1", database: database) }
var db: OpaquePointer?
precondition(sqlite3_open(database.path, &db) == SQLITE_OK)
defer { sqlite3_close(db) }
func sql(_ text: String) { precondition(sqlite3_exec(db, text, nil, nil, nil) == SQLITE_OK) }
func setShare(_ data: Data, id: Int) {
    let hex = data.map { String(format: "%02x", $0) }.joined()
    sql("UPDATE ZICCLOUDSYNCINGOBJECT SET ZSERVERSHAREDATA=X'" + hex + "' WHERE Z_PK=" + String(id))
}
let original = try read()
precondition(original.sharingAccess == .privateNote)
setShare(writable, id: 1)
let direct = try read()
precondition(direct.sharingAccess == .editable && direct.revision != original.revision)
setShare(readOnly, id: 1)
let revoked = try read()
precondition(revoked.sharingAccess == .readOnly && revoked.revision != direct.revision)
sql("UPDATE ZICCLOUDSYNCINGOBJECT SET ZSERVERSHAREDATA=NULL WHERE Z_PK=1")
setShare(writable, id: 12)
let inherited = try read()
precondition(inherited.sharingAccess == .editable && inherited.revision != direct.revision)
sql("UPDATE ZICCLOUDSYNCINGOBJECT SET ZISSHAREDIRTY=1 WHERE Z_PK=12")
let pending = try read()
precondition(pending.sharingAccess == .unknown && pending.revision != inherited.revision)
sql("UPDATE ZICCLOUDSYNCINGOBJECT SET ZISSHAREDIRTY=0,ZPARENT=10 WHERE Z_PK=12")
do { _ = try read(); preconditionFailure("Accepted ancestor cycle") } catch {}
sql("UPDATE ZICCLOUDSYNCINGOBJECT SET ZPARENT=NULL,ZFOLDERTYPE=1 WHERE Z_PK=12")
do { _ = try read(); preconditionFailure("Accepted deleted ancestor") } catch {}
print("PASS direct/inherited sharing, revocation revisions, pending permissions, invalid ancestry, and malformed archives")
''')
    # Throwing expressions must be evaluated before Swift's nonthrowing precondition autoclosure.
    s=(directory/'main.swift').read_text().replace('    precondition(NotesSharing.decode(try archive(name)) == expected, "Incorrect permission for " + name)','    let data = try archive(name)\n    precondition(NotesSharing.decode(data) == expected, "Incorrect permission for " + name)')
    (directory/'main.swift').write_text(s)
    subprocess.run(['xcrun','swiftc',str(directory/'NotesSharing.swift'),str(directory/'NotesStore.swift'),str(directory/'main.swift'),'-o',str(directory/'test')],check=True)
    subprocess.run([str(directory/'test'),str(directory)],check=True)
