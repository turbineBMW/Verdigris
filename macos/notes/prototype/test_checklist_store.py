import gzip
from pathlib import Path
import sqlite3
import tempfile
import unittest

from checklist_store import decode, fields, read_fixture


def varint(value):
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    return bytes(result + bytes([value]))


def field(number, value):
    if isinstance(value, int):
        return varint(number << 3) + varint(value)
    return varint(number << 3 | 2) + varint(len(value)) + value


def run(length, identifier=None, checked=0, indent=0):
    result = field(1, length)
    if identifier is not None:
        checklist = field(1, identifier) + field(2, checked)
        result += field(2, field(1, 103) + field(4, indent) + field(5, checklist))
    return field(5, result)


def blob(text, runs):
    note = field(2, text.encode()) + b"".join(runs)
    return gzip.compress(field(2, field(2, 1) + field(3, note)))


class ChecklistStoreTests(unittest.TestCase):
    def test_duplicate_labels_and_emoji_use_identity_and_utf16_ranges(self):
        data = blob("Title\n🧪 item\nsame\nsame\n", [run(6), run(8, b"a"),
                                                     run(5, b"b", 1, 1), run(5, b"c")])
        state = decode(data)
        self.assertEqual([item["location"] for item in state["items"]], [6, 14, 19])
        self.assertEqual([item["length"] for item in state["items"]], [7, 4, 4])
        self.assertEqual([item["checked"] for item in state["items"]], [False, True, False])
        self.assertEqual(state["items"][1]["indent"], 1)
        self.assertNotEqual(state["items"][1]["id"], state["items"][2]["id"])

    def test_styling_runs_in_one_item_are_coalesced(self):
        state = decode(blob("Bold normal\n", [run(4, b"a"), run(8, b"a")]))
        self.assertEqual(len(state["items"]), 1)
        self.assertEqual(state["items"][0]["text"], "Bold normal")

    def test_ambiguous_or_incomplete_data_is_rejected(self):
        cases = [blob("one\ntwo\n", [run(8, b"a")]),
                 blob("one\n", [run(2, b"a"), run(2, b"b")]),
                 blob("one\n", [run(3, b"a")]),
                 blob("one\n", [run(5, b"a")]),
                 blob("one\n", [run(4, b"a", checked=2)])]
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ValueError):
                decode(data)

    def test_wire_parser_handles_64_bits_and_rejects_truncation(self):
        self.assertEqual(fields(field(1, 2**64 - 1)), {1: [2**64 - 1]})
        for data in [b"\x08\x80", b"\x12\x05ab", b"\x0dabc", b"\x00\x00",
                     b"\x08" + b"\xff" * 10 + b"\x01"]:
            with self.subTest(data=data), self.assertRaises(ValueError):
                fields(data)

    def test_read_only_wal_snapshot_and_fixture_protection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "NoteStore.sqlite"
            connection = sqlite3.connect(path)
            self.addCleanup(connection.close)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE ZICCLOUDSYNCINGOBJECT (Z_PK INTEGER, ZTITLE1 TEXT, ZISPASSWORDPROTECTED INTEGER)")
            connection.execute("CREATE TABLE ZICNOTEDATA (ZNOTE INTEGER, ZDATA BLOB)")
            title = "Verdigris Checklist Probe fixture"
            text = title + "\nitem\n"
            connection.execute("INSERT INTO ZICCLOUDSYNCINGOBJECT VALUES (1,?,0)", (title,))
            connection.execute("INSERT INTO ZICNOTEDATA VALUES (1,?)",
                               (blob(text, [run(len(title) + 1), run(5, b"a")]),))
            connection.commit()
            state = read_fixture("x-coredata://TEST/ICNote/p1", path)
            self.assertEqual(state["items"][0]["text"], "item")
            connection.execute("UPDATE ZICCLOUDSYNCINGOBJECT SET ZISPASSWORDPROTECTED=1")
            connection.commit()
            with self.assertRaisesRegex(ValueError, "unprotected disposable"):
                read_fixture("x-coredata://TEST/ICNote/p1", path)
            connection.execute("UPDATE ZICCLOUDSYNCINGOBJECT SET ZISPASSWORDPROTECTED=0, ZTITLE1='Personal note'")
            connection.commit()
            with self.assertRaisesRegex(ValueError, "unprotected disposable"):
                read_fixture("x-coredata://TEST/ICNote/p1", path)
            self.assertEqual(connection.execute("SELECT count(*) FROM ZICNOTEDATA").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
