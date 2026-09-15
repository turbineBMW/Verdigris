#!/usr/bin/env python3
"""Compile and test the actual Swift decoder on macOS without launching the app.

Only Vapor's DTO/error declarations are stubbed. SQLite, zlib, CryptoKit and
the complete production NotesStore implementation run unchanged.
"""
from pathlib import Path
import subprocess
import tempfile
import sqlite3
import json
import base64

root = Path(__file__).resolve().parent.parent
with tempfile.TemporaryDirectory() as temporary:
    temporary = Path(temporary)
    database = temporary / "Notes.sqlite"
    db = sqlite3.connect(database)
    db.execute("CREATE TABLE Z_METADATA (Z_UUID TEXT)")
    db.execute("INSERT INTO Z_METADATA VALUES ('TEST')")
    db.execute("CREATE TABLE ZICCLOUDSYNCINGOBJECT (Z_PK INTEGER, ZISPASSWORDPROTECTED INTEGER, ZMARKEDFORDELETION INTEGER, ZFOLDER INTEGER, ZFOLDERTYPE INTEGER, ZSERVERSHAREDATA BLOB, ZISSHAREDIRTY INTEGER, ZPARENT INTEGER)")
    db.execute("CREATE TABLE ZICNOTEDATA (ZNOTE INTEGER, ZDATA BLOB)")
    db.executemany("INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK,ZISPASSWORDPROTECTED,ZMARKEDFORDELETION,ZFOLDER,ZFOLDERTYPE) VALUES (?,?,?,?,?)",[(10,0,0,None,0),(11,0,0,None,1),(1,0,0,10,None),(2,0,0,11,None),(3,1,0,10,None)])
    blob = base64.b64decode(json.loads((root / "tests/documents.json").read_text())[0]["data"])
    db.executemany("INSERT INTO ZICNOTEDATA VALUES (?,?)",[(1,blob),(2,blob),(3,blob)])
    db.commit(); db.close()
    source = (root / "NotesStore.swift").read_text().replace("import Vapor", """
typealias Content = Codable
enum Status { case unprocessableEntity, forbidden, notFound }
struct Abort: Error {
    init(_ status: Status, reason: String = "") {}
}
""")
    (temporary / "NotesStore.swift").write_text(source)
    (temporary / "NotesSharing.swift").write_text((root / "NotesSharing.swift").read_text())
    (temporary / "main.swift").write_text('''
import Foundation
let cases = try JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))) as! [[String: Any]]
for fixture in cases {
    let valid = fixture["valid"] as! Bool
    do {
        let document = try NotesStore.decode(Data(base64Encoded: fixture["data"] as! String)!)
        precondition(valid, "Accepted malformed document")
        precondition(document.items.map(\\.location) == fixture["locations"] as! [Int])
        if let expected = fixture["canEditText"] as? Bool { precondition(document.canEditText == expected) }
        if fixture["name"] as! String == "emoji-duplicates" {
            precondition(document.items.map(\\.checked) == [false, true, false])
            precondition(document.items.map(\\.canToggle) == [false, true, true])
        }
    } catch { precondition(!valid, "Rejected valid document") }
    print("PASS", fixture["name"]!)
}
let database = URL(fileURLWithPath: CommandLine.arguments[2])
let ordinary = try NotesStore.read("x-coredata://TEST/ICNote/p1", database: database)
precondition(ordinary.folderID == "x-coredata://TEST/ICFolder/p10")
for identifier in ["x-coredata://TEST/ICNote/p2", "x-coredata://TEST/ICNote/p3", "x-coredata://WRONG/ICNote/p1"] {
    do {
        _ = try NotesStore.read(identifier, database: database)
        preconditionFailure("Accepted a trashed, protected, or wrong-store note")
    } catch {}
}
print("PASS ordinary-folder, Recently Deleted, protected, and store-identity checks")
precondition(!NotesText.same("cafe\\u{301}", "café"))
let normalized = NotesText.replacement(old: "e\\u{301}🙂", new: "é🙂", location: 10)
precondition(normalized.range == NSRange(location: 10, length: 2) && normalized.text == "é")
let inserted = NotesText.replacement(old: "Milk 🥛", new: "Milk 🥛 café", location: 10)
precondition(inserted.range == NSRange(location: 17, length: 0) && inserted.text == " café")
let middle = NotesText.replacement(old: "Bold word", new: "Bold changed word", location: 0)
precondition(middle.range == NSRange(location: 5, length: 0) && middle.text == "changed ")
print("PASS exact Unicode comparison and minimal grapheme-span edits")
precondition(NotesText.validLabel("Family 👨‍👩‍👧‍👦 café"))
precondition(!NotesText.validLabel("line\\nnext") && !NotesText.validLabel("null\\u{0}"))
print("PASS joined-emoji labels and control-character rejection")

let casesText = [("First bold Last", "Changed bold Ending"), ("e\\u{301}🙂", "é👨‍👩‍👧‍👦"),
                 ("", "Inserted\\ntext"), ("Deleted\\ntext", ""), ("Keep\\nheading\\nend\\n", "Keep\\nheading\\nadded\\nend\\n")]
for (old, new) in casesText {
    let edits = try NotesText.edits(old: old, new: new)
    var result = old
    for edit in edits.reversed() { result = NotesText.applying(edit, to: result) }
    precondition(NotesText.same(result, new))
}
var seed: UInt64 = 1234
func next(_ count: Int) -> Int { seed = seed &* 6364136223846793005 &+ 1; return Int((seed >> 32) % UInt64(count)) }
let alphabet = ["a", "b", " ", "é", "e\\u{301}", "👨‍👩‍👧‍👦", "\\n"]
for _ in 0..<400 {
    let old = (0..<next(20)).map { _ in alphabet[next(alphabet.count)] }.joined()
    let new = (0..<next(20)).map { _ in alphabet[next(alphabet.count)] }.joined()
    var result = old
    for edit in try NotesText.edits(old: old, new: new).reversed() { result = NotesText.applying(edit, to: result) }
    precondition(NotesText.same(result, new))
}
let spans = try NotesText.edits(old: "First bold Last", new: "Changed bold Ending")
precondition(spans.count > 1, "Overwrote unchanged formatted text between edits")
let complete = try NotesText.complete(title: "Title", body: "line\\n\\nnext")
precondition(complete == "Title\\nline\\n\\nnext\\n")
do { _ = try NotesText.complete(title: "bad\\nname", body: "text"); preconditionFailure() } catch {}
do { _ = try NotesText.edits(old: String(repeating: "a", count: 5000), new: String(repeating: "b", count: 5000)); preconditionFailure() } catch {}
print("PASS exact multi-span Unicode edits, preserved unchanged text, and bounded edit planning")

''')
    subprocess.run(["xcrun", "swiftc", str(temporary / "NotesSharing.swift"), str(temporary / "NotesStore.swift"), str(temporary / "main.swift"),
                    "-o", str(temporary / "test")], check=True)
    subprocess.run([str(temporary / "test"), str(root / "tests/documents.json"), str(database)], check=True)
