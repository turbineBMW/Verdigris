"""An emoji tapback in sms.db is a tapback, not a message.

`associated_message_type` 2000-2005 are the six classic tapbacks and were the
only ones the importer knew. iOS 18's arbitrary-emoji tapback is 2006, with
the emoji in its own column and no verb anywhere — so it fell through as an
ordinary message and the conversation grew a literal bubble reading
'Reacted 😋 to "…"'. In one real history that was 737 of them.
"""
from __future__ import annotations

import sqlite3

from verdigris.backup.imessage_db import REMOVED_EMOJI_VERB, read_messages

_SCHEMA = """
CREATE TABLE message (
    ROWID INTEGER PRIMARY KEY, guid TEXT, text TEXT, handle_id INTEGER,
    is_from_me INTEGER, date INTEGER, is_read INTEGER, service TEXT,
    associated_message_type INTEGER, associated_message_guid TEXT,
    associated_message_emoji TEXT, thread_originator_guid TEXT);
CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, chat_identifier TEXT, guid TEXT,
                   style INTEGER, display_name TEXT, room_name TEXT);
CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY, filename TEXT,
                         mime_type TEXT, transfer_name TEXT);
CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER);
"""


def _db(tmp_path, rows):
    """A minimal sms.db holding one chat and the given message rows."""
    path = tmp_path / "sms.db"
    con = sqlite3.connect(path)
    con.executescript(_SCHEMA)
    con.execute("INSERT INTO handle VALUES (1, '+12155550102')")
    con.execute("INSERT INTO chat VALUES (1, '+12155550102', "
                "'iMessage;-;+12155550102', 45, NULL, NULL)")
    for i, row in enumerate(rows, start=1):
        con.execute(
            "INSERT INTO message VALUES (?,?,?,1,?,?,1,'iMessage',?,?,?,NULL)",
            (i, row["guid"], row.get("text", ""), row.get("from_me", 0),
             600_000_000 + i, row.get("atype", 0), row.get("target"),
             row.get("emoji")))
        con.execute("INSERT INTO chat_message_join VALUES (1, ?)", (i,))
    con.commit()
    con.close()
    return path


def _read(tmp_path, rows):
    return {m.guid: m for m in read_messages(_db(tmp_path, rows))}


def test_an_emoji_tapback_is_read_as_a_reaction(tmp_path):
    msgs = _read(tmp_path, [
        {"guid": "TARGET", "text": "so I just gotta stay home"},
        {"guid": "TAP", "text": 'Reacted 😋 to "so I just gotta stay home"',
         "atype": 2006, "target": "p:0/TARGET", "emoji": "😋"},
    ])
    tap = msgs["TAP"]
    assert tap.is_reaction
    assert tap.reaction_verb == "Reacted 😋"
    assert tap.reaction_target_guid == "TARGET"


def test_the_target_guid_survives_its_prefix(tmp_path):
    """sms.db stores it as "p:0/<guid>" or "bp:<guid>"."""
    msgs = _read(tmp_path, [
        {"guid": "T", "text": "hi"},
        {"guid": "A", "atype": 2006, "target": "bp:T", "emoji": "🔥"},
    ])
    assert msgs["A"].reaction_target_guid == "T"


def test_taking_an_emoji_tapback_back(tmp_path):
    msgs = _read(tmp_path, [
        {"guid": "T", "text": "hi"},
        {"guid": "R", "atype": 3006, "target": "p:0/T", "emoji": "🔥"},
    ])
    assert msgs["R"].reaction_verb == REMOVED_EMOJI_VERB
    assert msgs["R"].reaction_target_guid == "T"


def test_the_classic_six_still_work(tmp_path):
    msgs = _read(tmp_path, [
        {"guid": "T", "text": "hi"},
        {"guid": "L", "atype": 2000, "target": "p:0/T"},
        {"guid": "X", "atype": 3000, "target": "p:0/T"},
    ])
    assert msgs["L"].reaction_verb == "Loved"
    assert msgs["X"].reaction_verb == "Removed a heart from"


def test_a_sticker_tapback_is_left_alone(tmp_path):
    """Type 2007 is a sticker stuck to a message. It carries no emoji, so
    there is nothing to draw — better an untouched row than a blank badge."""
    msgs = _read(tmp_path, [
        {"guid": "T", "text": "hi"},
        {"guid": "S", "atype": 2007, "target": "p:0/T",
         "text": 'Reacted with a sticker to "hi"'},
    ])
    assert msgs["S"].reaction_verb is None
    assert not msgs["S"].is_reaction


def test_an_ordinary_message_is_not_a_reaction(tmp_path):
    msgs = _read(tmp_path, [{"guid": "M", "text": "hello"}])
    assert msgs["M"].reaction_verb is None
    assert msgs["M"].text == "hello"
