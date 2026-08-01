"""Every tapback reaches the bubble as an emoji, and a message holds all of them.

The badge used to be one of six hand-drawn SVGs, which left iOS 18's
arbitrary-emoji tapbacks to borrow an empty bubble and paint the emoji into a
hole measured off the artwork. One emoji list covers both kinds now — and it
is a list because everyone in a thread can react to the same message, where
the old single field meant the last reaction silently replaced the rest.
"""
from __future__ import annotations

from iphonebridge.qtui.models import (
    EmojiCompleter, MessageListModel, ThreadStore,
    _kind_to_reaction_verb, _kind_to_removal_verb, reaction_emoji,
)


def _store() -> ThreadStore:
    s = ThreadStore.__new__(ThreadStore)
    s._threads = {}
    s._current = None
    s._by_guid = {}
    return s


def _msg(**kw) -> dict:
    m = {"body": "hi", "ts": "2026-07-31T12:00:00+00:00", "outgoing": False,
         "reactions": {}, "attachments": [], "guid": "g1", "reply_to": "",
         "sender_name": "aiden", "sender_phone": "+12155550102", "edits": []}
    m.update(kw)
    return m


def _badges(msg: dict) -> list[str]:
    """What the delegate would draw, in order."""
    return _store()._rows_for(msg, None)[-1]["reactions"]


def _reacted(*pairs: tuple[str, str], **kw) -> dict:
    """A message carrying these (verb, reactor) tapbacks, applied in order."""
    m = _msg(**kw)
    for verb, reactor in pairs:
        ThreadStore._apply_reaction(m, verb, reactor)
    return m


# ---- the role ------------------------------------------------------------

def test_the_role_is_exported_to_qml():
    """A role the delegate declares `required` and the model doesn't export
    stops the whole list rendering, not just the badge."""
    assert "reactions" in MessageListModel._ROLES.values()


def test_the_role_numbers_stay_unique():
    values = list(MessageListModel._ROLES)
    assert len(values) == len(set(values))


# ---- verb → emoji --------------------------------------------------------

def test_classic_verbs_become_emoji():
    assert reaction_emoji("Loved") == "❤️"
    assert reaction_emoji("Laughed at") == "😂"
    assert reaction_emoji("Questioned") == "❓"


def test_an_arbitrary_emoji_needs_no_lookup():
    """iOS 18+ sends the emoji itself, prefixed. There's no table it could
    be found in, which is the whole reason the badge is emoji-based now."""
    assert reaction_emoji("Reacted 🥰") == "🥰"


def test_the_rustpush_spelling_carries_a_trailing_to():
    """The bridge phrases the whole clause where MAP's synthesized text stops
    at the emoji. Both have to land on the same badge, or a live tapback and
    the same one re-read from disk disagree — this one rendered as "🥰 to"."""
    assert reaction_emoji("Reacted 🥰 to") == "🥰"


def test_a_removal_is_not_an_emoji():
    assert reaction_emoji("Removed a heart from") == ""
    assert reaction_emoji(None) == ""
    assert reaction_emoji("") == ""


# ---- picker kind → verb --------------------------------------------------

def test_classic_picker_kinds_become_verbs():
    assert _kind_to_reaction_verb("Heart") == "Loved"
    assert _kind_to_reaction_verb("Laugh") == "Laughed at"
    assert _kind_to_reaction_verb("Emphasize") == "Emphasized"


def test_emoji_picker_kind_becomes_reacted_verb():
    assert _kind_to_reaction_verb("🎉") == "Reacted 🎉"
    assert reaction_emoji(_kind_to_reaction_verb("🎉")) == "🎉"


def test_removal_kinds_name_what_they_withdraw():
    assert _kind_to_removal_verb("Heart") == "Removed a heart from"
    assert _kind_to_removal_verb("🎉") == "Removed a reaction from"


# ---- one message, several people ----------------------------------------

def test_no_reaction_is_the_normal_case():
    assert _badges(_msg()) == []


def test_one_reaction_draws_one_badge():
    assert _badges(_reacted(("Loved", "aiden"))) == ["❤️"]


def test_two_people_both_get_a_badge():
    m = _reacted(("Loved", "aiden"), ("Laughed at", "rey"))
    assert _badges(m) == ["❤️", "😂"]


def test_the_newest_reaction_goes_last():
    """The delegate overlaps the badges and later siblings paint on top, so
    row order is what puts the newest reaction at the front of the stack."""
    m = _reacted(("Loved", "aiden"), ("Reacted 🔥", "rey"))
    assert _badges(m)[-1] == "🔥"


def test_one_person_holds_one_reaction():
    """Reacting again replaces what they had — two badges from one person is
    not a thing iMessage can express."""
    m = _reacted(("Loved", "aiden"), ("Laughed at", "aiden"))
    assert _badges(m) == ["😂"]


def test_the_same_emoji_from_two_people_is_one_badge():
    """iOS draws a count instead; three identical hearts in a row reads as a
    rendering bug."""
    m = _reacted(("Loved", "aiden"), ("Loved", "rey"))
    assert _badges(m) == ["❤️"]


def test_a_removal_takes_off_only_that_person_s():
    m = _reacted(("Loved", "aiden"), ("Laughed at", "rey"),
                 ("Removed a heart from", "aiden"))
    assert _badges(m) == ["😂"]


def test_a_removal_from_someone_who_never_reacted_is_harmless():
    m = _reacted(("Loved", "aiden"), ("Removed a like from", "nobody"))
    assert _badges(m) == ["❤️"]


def test_the_side_does_not_change_the_badge():
    """Two mirrored icon sets used to exist so the tapback's tail faced the
    right way. An emoji has no tail, so ours and theirs render the same."""
    mine = _reacted(("Liked", "aiden"), outgoing=True)
    theirs = _reacted(("Liked", "aiden"))
    assert _badges(mine) == _badges(theirs)


# ---- who reacted ---------------------------------------------------------

def test_my_own_reaction_is_keyed_as_mine():
    """Whatever handle a self-reaction arrives under, it has to collapse onto
    the same key or reacting from the phone and from here would double up."""
    assert ThreadStore._reactor({"sender_phone": "+12155551050"}, True) == "me"


def test_theirs_is_keyed_by_handle():
    ev = {"sender_phone_norm": "12155550102", "sender_name": "aiden"}
    assert ThreadStore._reactor(ev, False) == "12155550102"


def test_an_unidentified_sender_still_gets_a_key():
    """A key of "" would let two strangers overwrite each other."""
    assert ThreadStore._reactor({}, False) == "them"


# ---- the picker ----------------------------------------------------------

def test_the_picker_opens_onto_something():
    """`search` answers nothing for an empty prefix — right for the
    composer's `:shortcode` popup, fatal for a picker, which would open onto
    a blank grid."""
    opening = EmojiCompleter().popular(24)
    assert len(opening) == 24
    assert all(row["emoji"] for row in opening)


def test_the_picker_grid_has_no_duplicates():
    chars = [row["emoji"] for row in EmojiCompleter().popular(24)]
    assert len(chars) == len(set(chars))


def test_the_picker_does_not_repeat_the_classic_six():
    """They have their own row directly above the grid."""
    classic = {"❤️", "👍", "👎", "😂", "‼️", "❓"}
    assert not classic & {row["emoji"] for row in EmojiCompleter().popular(24)}


def test_search_still_answers_the_picker():
    hits = EmojiCompleter().search("fire")
    assert any(row["emoji"] == "🔥" for row in hits)
