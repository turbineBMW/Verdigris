"""Qt list models backing the QML conversation views.

`ThreadStore` owns the actual conversation state (ported from the GTK
`ConversationsPage`); the two QAbstractListModels are thin projections of it
for QML — one over the thread list, one over the open thread's messages.
"""
from __future__ import annotations

import json
import logging
import time
from concurrent.futures import Future, ThreadPoolExecutor
from html import escape
from pathlib import Path

from PySide6.QtCore import (
    Property,
    QAbstractListModel,
    QByteArray,
    QFileSystemWatcher,
    QModelIndex,
    QObject,
    Qt,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import QGuiApplication

from iphonebridge import config
from iphonebridge.avatars import circular as circular_avatar
from iphonebridge.contacts import ContactsResolver
from iphonebridge.emoji_text import body_markup
from iphonebridge.events import _detect_reaction, normalize_phone
from iphonebridge.message_store import (
    DEFAULT_MESSAGE_PAGE,
    MessageStore,
    default_store,
    thread_key_for,
)
from iphonebridge.qtui.emoji import search as emoji_search
from iphonebridge.qtui.util import (
    clock_ts,
    event_ts,
    format_ts,
    receipt_ts,
    relative_ts,
    separator_ts,
    ts_epoch,
    ts_gap_seconds,
)

log = logging.getLogger(__name__)

_ASSETS_DIR = Path(__file__).parent / "assets"

# Messages.app inserts a time divider when a conversation goes quiet for a
# while, rather than stamping every bubble.
_TIME_DIVIDER_GAP_SECONDS = 60 * 60

# Below this, consecutive messages are one burst and stack tightly; above it
# they were separate moments and get breathing room. Messages.app groups on
# roughly a minute, and the two cases genuinely read differently — three
# bubbles fired off in ten seconds are one thought, the same three spread over
# an afternoon are not, and drawn identically the reader can't tell.
#
# Only fills the range below _TIME_DIVIDER_GAP_SECONDS, where a pause was
# previously invisible: past an hour the divider already shows the break, and
# stacking a gap on top of it would double the separation.
_RUN_GAP_SECONDS = 60
# Added to the list's own 2px spacing.
_RUN_GAP_PX = 6

# Must track the bubble text size in ConversationsPage.qml — inline emoji are
# scaled relative to it.
_BODY_PX = 13

# Apple's free-standing attachment marker in attributed bodies (Bitmoji,
# peels, full stickers with a caption). Those stay their own rows; we only
# strip the glyph so it doesn't show as tofu in the caption.
_OBJ_REPLACEMENT = "\ufffc"
# Live inline-media slot (e.g. "3-0 on my return"). The matching image
# attachment is drawn *inside* the bubble at this position, not as a
# free-standing row above the text. Distinct from U+FFFC — treating those
# the same wrongly ate Bitmoji into the caption bubble.
_INLINE_MEDIA = "\uf00a"
# Inline media is as tall as the surrounding body text (see ConversationsPage
# bubble font). Reply quotes use the smaller quote size below.
_INLINE_MEDIA_PX = _BODY_PX
# Reply-quote caption size in ConversationsPage.qml — keep stickers there
# matching the dim quote text, not the bubble body.
_REPLY_PX = 11

# Backstop reconciliation against messages.sqlite (id-cursor incremental).
_SYNC_INTERVAL_MS = 10_000
# A message we send from here is echoed back by the phone in its SENT folder
# under a different handle. Same body + direction inside this window is
# treated as the same message.
_ECHO_WINDOW_SECONDS = 180

# How long a typing indicator lingers without a refresh. iMessage sends a
# start but not always a matching stop — a dropped stop would otherwise leave
# the bubble bouncing forever.
_TYPING_TIMEOUT_SEC = 12.0

_PINNED_FILE = config.STATE_DIR / "pinned_threads.json"
# thread key -> timestamp of the newest message the user has seen. Unread
# counts are derived from this, so they survive a restart.
_READ_FILE = config.STATE_DIR / "read_state.json"
# thread key -> timestamp it was cleared at. Messages at or before that point
# stay hidden; anything newer brings the conversation back, the way deleting
# a thread on a phone works. Local only — nothing is deleted on the iPhone.
_DELETED_FILE = config.STATE_DIR / "deleted_threads.json"
# How many conversations to show before the user scrolls for more.
_THREAD_PAGE = 40
# Messages loaded when opening a thread (newest first); scroll-up fetches more.
_MESSAGE_PAGE = DEFAULT_MESSAGE_PAGE
# Live search: throttle FTS so a fast typist doesn't queue dozens of queries,
# but always schedule a trailing run with the latest text. Debounce-only was
# wrong here — it only fired after the user *stopped* typing.
_SEARCH_THROTTLE_MS = 40

# The tapback picker's opening grid — see EmojiCompleter.popular(). The six
# classic tapbacks are deliberately absent: they have their own row directly
# above this one.
_PICKER_EMOJI = (
    ("joy", "😭"), ("skull", "💀"), ("fire", "🔥"), ("100", "💯"),
    ("pray", "🙏"), ("clap", "👏"), ("eyes", "👀"), ("thinking", "🤔"),
    ("smile", "😄"), ("wink", "😉"), ("sunglasses", "😎"), ("yum", "😋"),
    ("sob", "😢"), ("angry", "😡"), ("shrug", "🤷"), ("facepalm", "🤦"),
    ("heart_eyes", "😍"), ("kiss", "😘"), ("ok_hand", "👌"), ("muscle", "💪"),
    ("tada", "🎉"), ("check", "✅"), ("x", "❌"), ("wave", "👋"),
)

# The six classic tapbacks as the emoji the picker offers for them, so a
# tapback we sent and a tapback we received render identically. iOS 18 emoji
# tapbacks arrive as the emoji already and need no entry here.
_REACTION_EMOJI = {
    "Loved": "❤️", "Liked": "👍", "Disliked": "👎",
    "Laughed at": "😂", "Emphasized": "‼️", "Questioned": "❓",
}


# The other half of the same wire vocabulary: taking a tapback back. These
# carry no emoji of their own — they withdraw whatever that person put on the
# message — so they are a set rather than a mapping.
_REACTION_REMOVED = frozenset({
    "Removed a heart from", "Removed a like from", "Removed a dislike from",
    "Removed a laugh from", "Removed an exclamation from",
    "Removed a question mark from",
    # Withdrawing an emoji tapback. Not one of MAP's phrasings — that kind of
    # tapback never reaches MAP — see backup.imessage_db.REMOVED_EMOJI_VERB.
    "Removed a reaction from",
})


def reaction_emoji(verb: str | None) -> str:
    """The emoji a tapback verb puts on the bubble, or "" for none.

    Both spellings arrive: one of the six classic verbs, or an iOS 18
    arbitrary-emoji tapback as "Reacted 🥰", which already carries its emoji
    and only needs the prefix off. Removals return "" — the caller withdraws
    rather than draws, and checks `_REACTION_REMOVED` to tell the two apart.
    """
    if not verb:
        return ""
    classic = _REACTION_EMOJI.get(verb)
    if classic:
        return classic
    if verb.startswith("Reacted "):
        # rustpush phrases the whole clause ("Reacted 🥰 to"); MAP's
        # synthesized text stops at the emoji. Tolerate both here so a live
        # tapback and the same one re-read from disk agree.
        emoji = verb[len("Reacted "):].strip()
        if emoji.endswith(" to"):
            emoji = emoji[:-3].strip()
        return emoji
    return ""


def _thread_key(ev: dict) -> str:
    return (ev.get("contact_name") or ev.get("sender_phone")
            or ev.get("sender_phone_norm") or "(unknown)")


# Extensions that Qt (or the OS image plugins) can render as photos/stickers.
# Used when the attachment descriptor has an empty mime — backup and live
# paths both occasionally omit it, and without this every HEIC/JPEG with a
# blank mime lands as a paperclip chip instead of an image.
_IMAGE_EXTS = frozenset({
    ".jpg", ".jpeg", ".png", ".gif", ".heic", ".heif",
    ".webp", ".tif", ".tiff", ".bmp",
})


def _file_url(path: str | Path | None) -> str:
    """Absolute local path → percent-encoded `file://` URL for QML Image.

    `f"file://{path}"` breaks on spaces and other reserved characters (the
    Image then errors and the media row disappears entirely). `Path.as_uri()`
    encodes correctly. Relative paths are made absolute first.
    """
    if not path:
        return ""
    p = Path(path)
    if not p.is_absolute():
        p = p.absolute()
    try:
        return p.as_uri()
    except ValueError:
        return ""


def _is_image_att(att: dict) -> bool:
    """True when this attachment should render as a photo/sticker, not a chip.

    Prefer the declared mime; fall back to the filename/path extension so a
    missing `mime` (common on live downloads before meta is filled in, and
    on some backup rows) still draws as an image when the bytes are there.
    """
    if att.get("is_sticker"):
        return True
    mime = (att.get("mime") or att.get("mime_type") or "").lower()
    if mime.startswith("image/"):
        return True
    name = att.get("name") or ""
    path = str(att.get("path") or "")
    ext = Path(name).suffix.lower() or Path(path).suffix.lower()
    return ext in _IMAGE_EXTS


def _inline_img_html(att: dict, height_px: int = _INLINE_MEDIA_PX) -> str:
    """Qt rich-text `<img>` for media sitting on a text line.

    Height matches the surrounding font size; width follows the image's
    aspect ratio so a square sticker is a text-sized square, not a 80px tile.
    """
    url = _file_url(att.get("path"))
    w = int(att.get("w") or 0)
    h = int(att.get("h") or 0)
    height_px = max(1, int(height_px))
    if w > 0 and h > 0:
        scale = height_px / max(h, 1)
        dw = max(1, round(w * scale))
        dh = max(1, round(h * scale))
    else:
        dw = dh = height_px
    return (
        f'<img src="{escape(url, quote=True)}" '
        f'width="{dw}" height="{dh}" />'
    )


def _inline_candidate(att: dict) -> bool:
    """Could this attachment fill a U+F00A slot (once its bytes land)?

    Live inline stickers often arrive with `is_sticker: false` and a plain
    image UTI — the F00A marker is what makes them inline, not the flag.
    Path is optional: a pre-download descriptor is still claimed by the slot
    so it doesn't become a free-standing empty image row.
    """
    return _is_image_att(att)


def _body_with_inline_media(
    body: str, atts: list[dict], *, height_px: int = _INLINE_MEDIA_PX
) -> tuple[str, str, bool, list[dict]]:
    """Split a message into bubble text and free-standing attachment rows.

    U+F00A marks true *inline* media (words and image in one bubble). Each
    slot claims the next image attachment; with a path it becomes an `<img>`,
    without one the glyph is just dropped until download finishes. U+FFFC is
    only the free-standing attachment marker (Bitmoji, peels) — those stay
    their own rows and the glyph is stripped from the caption. Returns
    `(plain_body, rich_body, jumbo, free_standing_atts)`.

    `height_px` sizes inline images to the surrounding text — body vs reply
    quote use different font sizes.
    """
    body = body or ""
    # No inline slots: free-standing attachments keep their rows; just scrub
    # the attributed-string placeholder out of the caption.
    if _INLINE_MEDIA not in body:
        plain = body.replace(_OBJ_REPLACEMENT, "")
        rich, jumbo = body_markup(plain, height_px)
        return plain, rich, jumbo, list(atts)

    queue = [a for a in atts if _inline_candidate(a)]
    free = [a for a in atts if not _inline_candidate(a)]

    pieces = body.split(_INLINE_MEDIA)
    used: list[dict] = []
    segments: list[tuple[str, object]] = []
    for i, piece in enumerate(pieces):
        # FFFC can co-exist in theory; never draw it.
        segments.append(("t", piece.replace(_OBJ_REPLACEMENT, "")))
        if i + 1 >= len(pieces):
            break
        if not queue:
            # Unmatched F00A is dropped rather than shown as tofu.
            continue
        att = queue.pop(0)
        if att.get("path"):
            used.append(att)
            segments.append(("m", att))
        # Claimed but not on disk yet: hold the slot, draw nothing until
        # the attachment state lands and the row is rebuilt.

    plain = "".join(p for kind, p in segments if kind == "t")
    free = free + queue

    if not plain.strip() and not used:
        return "", "", False, free

    if not used:
        rich, jumbo = body_markup(plain, height_px)
        return plain, rich, jumbo, free

    chunks: list[str] = []
    for kind, val in segments:
        if kind == "t":
            em, _ = body_markup(val, height_px)
            chunks.append(em if em else escape(val).replace("\n", "<br>"))
        else:
            chunks.append(_inline_img_html(val, height_px))
    # Inline media keeps the bubble; jumbo is for emoji-only text.
    return plain, "".join(chunks), False, free


class _DictListModel(QAbstractListModel):
    """List model over a list of plain dicts, keyed by role name.

    Subclasses declare `_ROLES` as {role int: field name}. Qt probes models
    with standard roles (DisplayRole, ToolTipRole, ...) that we don't
    define; data() must return None for those rather than raising, or the
    exception propagates out of the delegate and the view renders nothing.
    """

    _ROLES: dict[int, str] = {}

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[dict] = []
        self._role_names = {
            role: QByteArray(field.encode()) for role, field in self._ROLES.items()
        }

    def roleNames(self) -> dict:
        return self._role_names

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def data(self, index: QModelIndex, role: int):
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        field = self._ROLES.get(role)
        return None if field is None else self._rows[index.row()].get(field)

    #: Field whose value identifies a row across reloads, if any.
    _IDENTITY: str | None = None

    def reload(self, rows: list[dict]) -> None:
        """Swap in new rows, resetting only when the shape actually changed.

        A full reset makes the view drop its scroll position, so refreshing
        after every incoming message threw the conversation list back to the
        top. When the same rows are present in the same order — the common
        case, where only a preview or timestamp changed — emit dataChanged
        instead, which updates in place and leaves scrolling alone.

        Per-row + per-role: only emit for cells that actually differ so QML
        does not rebind every avatar and Text on a stamp tick.
        """
        if self._IDENTITY is not None and len(rows) == len(self._rows):
            same = all(a.get(self._IDENTITY) == b.get(self._IDENTITY)
                       for a, b in zip(rows, self._rows, strict=True))
            if same:
                for i, (a, b) in enumerate(zip(rows, self._rows, strict=True)):
                    roles = [
                        role for role, field in self._ROLES.items()
                        if a.get(field) != b.get(field)
                    ]
                    if roles:
                        self._rows[i] = a
                        idx = self.index(i, 0)
                        self.dataChanged.emit(idx, idx, roles)
                    else:
                        # Keep the live dict so later in-place mutators still
                        # share identity with the model.
                        self._rows[i] = a
                return

        self.beginResetModel()
        self._rows = rows
        self.endResetModel()


class ThreadListModel(_DictListModel):
    KeyRole = Qt.ItemDataRole.UserRole + 1
    NameRole = Qt.ItemDataRole.UserRole + 2
    PreviewRole = Qt.ItemDataRole.UserRole + 3
    StampRole = Qt.ItemDataRole.UserRole + 4
    UnreadRole = Qt.ItemDataRole.UserRole + 5
    PinnedRole = Qt.ItemDataRole.UserRole + 6
    AvatarRole = Qt.ItemDataRole.UserRole + 7
    InitialsRole = Qt.ItemDataRole.UserRole + 8
    # Shared with SearchResultModel so one delegate can bind either list.
    ResultIdRole = Qt.ItemDataRole.UserRole + 9
    EventIdRole = Qt.ItemDataRole.UserRole + 10
    GuidRole = Qt.ItemDataRole.UserRole + 11
    RichPreviewRole = Qt.ItemDataRole.UserRole + 12

    _ROLES = {
        KeyRole: "threadKey",
        NameRole: "name",
        PreviewRole: "preview",
        StampRole: "stamp",
        UnreadRole: "unread",
        PinnedRole: "pinned",
        AvatarRole: "avatar",
        InitialsRole: "initials",
        ResultIdRole: "resultId",
        EventIdRole: "eventId",
        GuidRole: "guid",
        RichPreviewRole: "richPreview",
    }

    # Rows keep their identity across refreshes by thread key.
    _IDENTITY = "threadKey"

    def __init__(self, store: ThreadStore) -> None:
        super().__init__()
        self._store = store


class SearchResultModel(_DictListModel):
    """One row per matching message — a thread can appear many times."""

    KeyRole = Qt.ItemDataRole.UserRole + 1
    NameRole = Qt.ItemDataRole.UserRole + 2
    PreviewRole = Qt.ItemDataRole.UserRole + 3
    StampRole = Qt.ItemDataRole.UserRole + 4
    UnreadRole = Qt.ItemDataRole.UserRole + 5
    PinnedRole = Qt.ItemDataRole.UserRole + 6
    AvatarRole = Qt.ItemDataRole.UserRole + 7
    InitialsRole = Qt.ItemDataRole.UserRole + 8
    ResultIdRole = Qt.ItemDataRole.UserRole + 9
    EventIdRole = Qt.ItemDataRole.UserRole + 10
    GuidRole = Qt.ItemDataRole.UserRole + 11
    RichPreviewRole = Qt.ItemDataRole.UserRole + 12

    _ROLES = {
        KeyRole: "threadKey",
        NameRole: "name",
        PreviewRole: "preview",
        StampRole: "stamp",
        UnreadRole: "unread",
        PinnedRole: "pinned",
        AvatarRole: "avatar",
        InitialsRole: "initials",
        ResultIdRole: "resultId",
        EventIdRole: "eventId",
        GuidRole: "guid",
        RichPreviewRole: "richPreview",
    }
    _IDENTITY = "resultId"


class MessageListModel(_DictListModel):
    BodyRole = Qt.ItemDataRole.UserRole + 1
    OutgoingRole = Qt.ItemDataRole.UserRole + 2
    DividerRole = Qt.ItemDataRole.UserRole + 4
    # Tapbacks on this message, as the emoji themselves — ["❤️", "😂"], not
    # verbs and not icon paths. A list because everyone in a thread can react
    # to the same message, and one role covers both kinds of tapback: the six
    # classic verbs map onto the same emoji the picker offers, and an iOS 18
    # arbitrary emoji is already one. Empty for the great majority of messages.
    ReactionsRole = Qt.ItemDataRole.UserRole + 5
    MediaOnlyRole = Qt.ItemDataRole.UserRole + 6
    ImageRole = Qt.ItemDataRole.UserRole + 7
    FileLabelRole = Qt.ItemDataRole.UserRole + 8
    KindRole = Qt.ItemDataRole.UserRole + 9
    SenderRole = Qt.ItemDataRole.UserRole + 10
    SenderAvatarRole = Qt.ItemDataRole.UserRole + 11
    SenderInitialsRole = Qt.ItemDataRole.UserRole + 12
    TailRole = Qt.ItemDataRole.UserRole + 13
    RichBodyRole = Qt.ItemDataRole.UserRole + 14
    JumboRole = Qt.ItemDataRole.UserRole + 15
    ImageWRole = Qt.ItemDataRole.UserRole + 16
    ImageHRole = Qt.ItemDataRole.UserRole + 17
    # iMessage's own id. Empty for anything that arrived over MAP or came
    # out of the backup — and every action the UI can offer (tapback, reply,
    # edit, unsend) needs it, so it doubles as "is this actionable".
    GuidRole = Qt.ItemDataRole.UserRole + 18
    # "", "delivered" or "read". Only meaningful on outgoing messages.
    StateRole = Qt.ItemDataRole.UserRole + 19
    # Local time the receipt arrived, pre-formatted ("12:52 PM"). iOS shows
    # it beside "Read" but not beside "Delivered", so this is empty for the
    # delivered caption even though the receipt carried a timestamp.
    StateStampRole = Qt.ItemDataRole.UserRole + 20
    # Earlier versions of an edited message, oldest first. Empty for the
    # overwhelming majority of messages.
    EditsRole = Qt.ItemDataRole.UserRole + 21
    # Text of the message this one replies to, for the quoted line above the
    # bubble. Empty when it isn't a reply — which is nearly always.
    ReplyBodyRole = Qt.ItemDataRole.UserRole + 22
    # Extra space above this row, in px. Non-zero when there was a real pause
    # before the message — see _RUN_GAP_SECONDS.
    GapBeforeRole = Qt.ItemDataRole.UserRole + 23
    # Time of day this message was sent, pre-formatted ("6:24 PM"). Only ever
    # drawn with Show Times on, but carried always: it comes free with the row
    # and computing it on demand would mean rebuilding the whole model to
    # toggle a display option.
    TimeStampRole = Qt.ItemDataRole.UserRole + 24
    EventIdRole = Qt.ItemDataRole.UserRole + 25
    HighlightRole = Qt.ItemDataRole.UserRole + 26
    # Guid of the message this one replies to. Empty when it isn't a reply.
    # Paired with replyBody so the quote is tappable and can jump to the
    # original without re-resolving the target in QML.
    ReplyGuidRole = Qt.ItemDataRole.UserRole + 27

    _ROLES = {
        BodyRole: "body",
        OutgoingRole: "outgoing",
        DividerRole: "divider",
        ReactionsRole: "reactions",
        MediaOnlyRole: "mediaOnly",
        ImageRole: "image",
        FileLabelRole: "fileLabel",
        KindRole: "kind",
        SenderRole: "sender",
        SenderAvatarRole: "senderAvatar",
        SenderInitialsRole: "senderInitials",
        TailRole: "tail",
        RichBodyRole: "richBody",
        JumboRole: "jumbo",
        ImageWRole: "imageW",
        ImageHRole: "imageH",
        GuidRole: "guid",
        StateRole: "deliveryState",
        StateStampRole: "deliveryStamp",
        EditsRole: "edits",
        ReplyBodyRole: "replyBody",
        GapBeforeRole: "gapBefore",
        TimeStampRole: "timeStamp",
        EventIdRole: "eventId",
        HighlightRole: "highlight",
        ReplyGuidRole: "replyGuid",
    }

    # Identify a row by the message it came from plus its position within
    # that message, so a rebuild that changes only content — binding a guid
    # to a sent message, applying an edit, moving a delivery caption — updates
    # in place instead of resetting.
    #
    # A reset drops the view's contentY to zero, and the restore can only run
    # once layout has happened, so the reset was visible: pressing send
    # flashed the top of the conversation for a frame before snapping back.
    #
    # Not the guid: binding one is exactly the case this exists to make
    # non-destructive, and the guid changes when it happens.
    _IDENTITY = "rowKey"

    def rows(self) -> list[dict]:
        """The live row dicts, for in-place caption updates by the store."""
        return self._rows

    def captions_changed(self) -> None:
        """Repaint delivery captions after the store rewrote them.

        Emits over the whole list for the one role only: captions move
        between bubbles, so both the row that gained one and the row that
        lost one have to refresh, and finding them is not worth the
        bookkeeping for a list this size.
        """
        if not self._rows:
            return
        self.dataChanged.emit(
            self.index(0, 0), self.index(len(self._rows) - 1, 0),
            [self.StateRole, self.StateStampRole])

    def highlight_changed(self) -> None:
        """Repaint the accent rim after a jump highlight is set or cleared."""
        if not self._rows:
            return
        self.dataChanged.emit(
            self.index(0, 0), self.index(len(self._rows) - 1, 0),
            [self.HighlightRole])

    def append(self, row: dict) -> None:
        n = len(self._rows)
        self.beginInsertRows(QModelIndex(), n, n)
        self._rows.append(row)
        self.endInsertRows()

    def close_run(self) -> None:
        """Strip the tail and avatar from the last row.

        Called just before appending a message that continues the previous
        one's run. Those decorations mark the *end* of a run, so the message
        that was last has to give them up — otherwise, while a thread stays
        open, every consecutive bubble keeps the tail it was given when it
        was the newest, and in a group the sender's avatar repeats down the
        whole run. Reopening the thread hid it because that path rebuilds
        every row with full knowledge of what follows it.
        """
        if not self._rows:
            return
        row = self._rows[-1]
        if not (row["tail"] or row["senderAvatar"] or row["senderInitials"]):
            return
        row["tail"] = False
        row["senderAvatar"] = ""
        row["senderInitials"] = ""
        idx = self.index(len(self._rows) - 1, 0)
        self.dataChanged.emit(idx, idx, [self.TailRole, self.SenderAvatarRole,
                                         self.SenderInitialsRole])


class EmojiCompleter(QObject):
    """Backs the `:shortcode` autocomplete popup in the composer."""

    @Slot(str, result="QVariantList")
    def search(self, prefix: str) -> list[dict]:
        return emoji_search(prefix, limit=8)

    @Slot(int, result="QVariantList")
    def popular(self, limit: int = 24) -> list[dict]:
        """What the tapback picker shows before anything is typed.

        `search` answers nothing for an empty prefix — right for the
        composer's `:shortcode` popup, which should stay shut until there is
        a word to complete, but a picker opening onto a blank grid is a dead
        end. Hand-ordered rather than derived: this is a "what do people
        actually react with" list, and no ranking over Unicode names
        produces it.
        """
        return [{"code": c, "emoji": e} for c, e in _PICKER_EMOJI[:limit]]

    @Slot(str, int, result="QVariantMap")
    def tokenAt(self, text: str, cursor: int) -> dict:
        """Find a `:shortcode` token ending at the cursor.

        Returns {start, query} for the token being typed, or {start: -1}
        when the cursor isn't inside one. A token only counts if the colon
        starts a word — otherwise a bare ':' mid-sentence, or a completed
        `:foo:`, would keep the popup open.
        """
        if cursor < 0 or cursor > len(text):
            return {"start": -1, "query": ""}
        head = text[:cursor]
        colon = head.rfind(":")
        if colon < 0:
            return {"start": -1, "query": ""}
        if colon > 0 and not (head[colon - 1].isspace()):
            return {"start": -1, "query": ""}
        query = head[colon + 1:]
        if not query or any(c.isspace() or c == ":" for c in query):
            return {"start": -1, "query": ""}
        return {"start": colon, "query": query}


class ThreadStore(QObject):
    """Conversation state + the two models QML binds to."""

    currentChanged = Signal()
    typingChanged = Signal()
    peerChanged = Signal()
    pinsChanged = Signal()
    searchChanged = Signal()
    # Emitted after a search result opens — QML scrolls to this event id.
    jumpToMessage = Signal(int)
    # True while opening a search hit so QML skips the usual land-at-end.
    landAtEndSuppressedChanged = Signal()

    def __init__(self, client, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._client = client
        self._threads: dict[str, dict] = {}
        self._current: str | None = None
        self._seen_handles: set[str] = set()
        # Highest messages.sqlite row id we have applied. Incremental disk
        # sync only fetches id > this, so a 200k-row DB is not re-scanned
        # every ten seconds.
        self._last_event_id: int = 0
        self._last_write_seq: int = 0
        self._contacts = ContactsResolver()
        # Same process-wide connection as DaemonClient history reads —
        # see client.read_events. (Search still opens a private store on
        # the worker thread and closes it.)
        self._db = default_store()
        # Ordered, not a set: the pinned grid is user-arrangeable by drag,
        # so the order the user put them in *is* the data.
        self._pinned: list[str] = self._load_pinned()
        self._read_marks: dict[str, str] = self._load_read_marks()
        # Last rows handed to the thread model; the pinned grid reorders
        # these rather than rebuilding them.
        self._thread_rows: list[dict] = []
        # GUID → message, so backup tapbacks can find their exact target.
        self._by_guid: dict[str, dict] = {}
        # Delivered/Read that arrived before we had the message (or before
        # its guid was upgraded from a MAP-first copy). Applied when the
        # guid lands. Bounded in _apply_state.
        self._pending_receipts: dict[str, tuple[str, str]] = {}
        # Whether the main window is the active (focused) window. Combined
        # with the open thread so the daemon can suppress popups for a
        # conversation that is already on screen — see setWindowFocused.
        self._window_focused: bool = True
        # normalized phone -> thread key, so one person is one thread
        # regardless of what name each event happened to carry.
        self._by_phone: dict[str, str] = {}
        self._deleted: dict[str, str] = self._load_deleted()
        # thread key -> {person handle -> monotonic deadline}. Per-person so
        # a group can show several overlapping avatars at once; deadlines
        # still enforce _TYPING_TIMEOUT_SEC when a stop never arrives.
        self._typing_until: dict[str, dict[str, float]] = {}
        # Threads whose message list needs re-sorting after a bulk import.
        self._unsorted: set[int] = set()
        # Conversations are paged in: with a full backup imported
        # there can be hundreds, and building every row up front
        # is wasted work for a list that shows a dozen.
        self._thread_limit = _THREAD_PAGE
        self._search_query = ""
        self._search_pending = ""
        # Monotonic generation so a slow FTS for "he" never overwrites
        # fresher results for "hello" when the user types quickly.
        self._search_gen = 0
        self._search_inflight = False
        self._search_again = False
        self._search_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="ib-search"
        )
        self._highlight_event_id = 0
        # Guid of the bubble rim-highlighted by a reply-quote jump (search
        # uses event id; replies only always have a guid).
        self._highlight_guid = ""
        self._suppress_land_at_end = False
        self._refresh_pending = QTimer(self)
        self._refresh_pending.setSingleShot(True)
        self._refresh_pending.setInterval(60)
        self._refresh_pending.timeout.connect(self._refresh_threads_now)
        # Coalesce bursts (paste, IME) without waiting for the user to pause.
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(_SEARCH_THROTTLE_MS)
        self._search_timer.timeout.connect(self._dispatch_search)

        # Exposed to QML through Property accessors below — plain Python
        # attributes on a QObject are invisible to QML.
        self._thread_model = ThreadListModel(self)
        self._search_model = SearchResultModel()
        self._message_model = MessageListModel()

        # Watcher must exist before _load_history: cold start calls
        # _sync_from_disk → _watch_events_file. Creating it later crashed
        # every launch with AttributeError (app never opened).
        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(self._sync_from_disk)
        self._watcher.directoryChanged.connect(self._sync_from_disk)

        # WAL writes often touch -wal rather than the main file; the timer is
        # the reliable path. Incremental, so cheap.
        self._sync_timer = QTimer(self)
        self._sync_timer.timeout.connect(self._sync_from_disk)
        self._sync_timer.start(_SYNC_INTERVAL_MS)

        self._load_history()
        client.messageReceived.connect(self._on_signal)
        client.messageSent.connect(self._on_signal)
        client.messageStateChanged.connect(self._on_state_signal)
        self._watch_events_file()

        # Expire stale typing indicators. Cheap, and it runs regardless of
        # whether a matching "stopped" ever arrives.
        self._typing_timer = QTimer(self)
        self._typing_timer.timeout.connect(self._expire_typing)
        self._typing_timer.start(2000)

    def _expire_typing(self) -> None:
        now = time.monotonic()
        changed_current = False
        empty: list[str] = []
        for key, people in self._typing_until.items():
            stale = [h for h, deadline in people.items() if deadline <= now]
            for h in stale:
                people.pop(h, None)
            if stale and key == self._current:
                changed_current = True
            if not people:
                empty.append(key)
        for key in empty:
            self._typing_until.pop(key, None)
        if changed_current:
            self.typingChanged.emit()

    @Property(bool, notify=typingChanged)
    def peerTyping(self) -> bool:
        """Whether anyone is typing in the open conversation."""
        people = self._typing_until.get(self._current or "") or {}
        now = time.monotonic()
        return any(deadline > now for deadline in people.values())

    @Property("QVariantList", notify=typingChanged)
    def typingAvatars(self) -> list[dict]:
        """Avatars of people typing in the open *group* chat.

        Empty for 1:1 (the peer is already known) and when nobody is typing.
        Each entry is `{avatar, initials}` so the QML footer can stack them
        the way Messages.app does beside the dots bubble.
        """
        key = self._current or ""
        people = self._typing_until.get(key) or {}
        if not people:
            return []
        thread = self._threads.get(key)
        if not thread or not thread.get("is_group"):
            return []
        now = time.monotonic()
        out: list[dict] = []
        for who, deadline in people.items():
            if deadline <= now:
                continue
            name = self._typing_display_name(thread, who)
            out.append({
                "avatar": _file_url(circular_avatar(
                    self._contacts.resolve_photo(who))),
                "initials": self._initials(name or "?"),
            })
        return out

    def _on_signal(self, ev: dict) -> None:
        """Live D-Bus event. Trust the payload's own `kind` for direction."""
        self._ingest(ev, outgoing=(ev.get("kind") == "sms_sent"))

    def _on_state_signal(self, ev: dict) -> None:
        """Live D-Bus state. Peer is under `handle` on the wire."""
        # Disk records store the conversation peer as `peer_handle` so the
        # line's own `handle` can be a unique id for dedupe on reload.
        peer = ev.get("handle") or ev.get("peer_handle") or ""
        self._apply_state(
            state=ev.get("state") or "",
            guid=ev.get("guid") or "",
            peer=peer,
            body=ev.get("body") or "",
            timestamp=ev.get("timestamp") or "",
            refresh=True,
        )

    def _ingest_state(self, ev: dict, *, refresh: bool = True) -> None:
        """A `message_state` row from the message store (post-restart rebuild)."""
        handle = ev.get("handle") or ""
        if handle:
            if handle in self._seen_handles:
                return
            self._seen_handles.add(handle)
        self._apply_state(
            state=ev.get("state") or "",
            guid=ev.get("guid") or "",
            peer=ev.get("peer_handle") or ev.get("handle") or "",
            body=ev.get("body") or "",
            timestamp=ev.get("timestamp") or "",
            refresh=refresh,
        )

    def _apply_state(
        self,
        *,
        state: str,
        guid: str,
        peer: str,
        body: str,
        timestamp: str,
        refresh: bool,
    ) -> None:
        """Delivery/read/typing/edit. Updates a message rather than adding one."""
        if state in ("typing", "typing_stopped"):
            self._set_typing(peer, state == "typing")
            return

        # An edit or unsend we made ourselves. Apple doesn't echo these back
        # to the device that sent them, so the daemon reports them locally
        # instead — otherwise the desktop is the one place that never sees
        # its own edit. Same path as a remote Edit event once it hits disk.
        if state in ("edited", "unsent"):
            if self._apply_edit({"edited_from_guid": guid}, body):
                if refresh and self._current:
                    self._rebuild_messages()
            return

        # An incoming message's attachments, once the daemon has fetched the
        # bytes. They arrive after the message because iMessage delivers them
        # by reference — see `Daemon._fetch_attachments`. `body` is a JSON
        # list because D-Bus `a{sv}` cannot nest.
        if state == "attachments":
            msg = self._by_guid.get(guid)
            if msg is None:
                return
            try:
                atts = json.loads(body) if body else []
            except json.JSONDecodeError:
                log.warning("bad attachment payload for %s", guid)
                return
            # Only entries that actually landed on disk; a failed download
            # leaves the message as it was rather than drawing a broken image.
            atts = [a for a in atts if a.get("path")]
            if not atts:
                return
            msg["attachments"] = atts
            # The bridge puts "[name.png]" in the body when a message is
            # nothing but an attachment, so there's something to show before
            # the bytes exist. Once the image is here that text is the
            # placeholder it replaced — leaving it draws a filename caption
            # under every photo.
            placeholders = {f"[{a.get('name')}]" for a in atts}
            if (msg.get("body") or "") in placeholders:
                msg["body"] = ""
            if refresh and self._current:
                self._rebuild_messages()
            return

        if state not in ("delivered", "read"):
            return
        if not guid:
            return

        msg = self._by_guid.get(guid)
        if msg is None:
            # Receipt beat the message (or beat the guid upgrade of a
            # MAP-first phone send). Remember it and apply once the guid
            # shows up — dropping it here is why phone-sent bubbles never
            # got a Delivered caption.
            prev = self._pending_receipts.get(guid)
            if prev and prev[0] == "read" and state == "delivered":
                return
            self._pending_receipts[guid] = (state, timestamp)
            if len(self._pending_receipts) > 512:
                for old in list(self._pending_receipts)[:128]:
                    self._pending_receipts.pop(old, None)
            return
        if not self._stamp_receipt(msg, state, timestamp):
            return

        # Recompute captions if this message is in the open thread. Find the
        # owner by identity rather than assuming `_current` — live sends can
        # land under a phone-merged key that differs from the display name
        # the user is staring at only if we never merge, but more often the
        # receipt just needs a reliable repaint.
        if not refresh:
            return
        owner = None
        for thread in self._threads.values():
            if msg in thread["messages"]:
                owner = thread
                break
        if owner is None or owner.get("key") != self._current:
            return
        # Captions only rewrite two string roles — never the row set. A full
        # rebuild here used to re-estimate contentHeight and jolt the scroll
        # on every Delivered/Read.
        self._refresh_captions()

    @staticmethod
    def _stamp_receipt(msg: dict, state: str, timestamp: str) -> bool:
        """Apply delivered/read to a message. False if this was a downgrade."""
        # Never downgrade: Apple sends Delivered and Read as separate events
        # and they can arrive out of order on a slow link.
        if msg.get("state") == "read" and state == "delivered":
            return False
        msg["state"] = state
        # When it happened, not when the message was sent — "Read 12:52" is
        # the receipt's own clock.
        msg["state_ts"] = timestamp
        return True

    def _bind_guid(self, msg: dict, guid: str) -> None:
        """Attach a guid and any receipt that was waiting on it."""
        if not guid:
            return
        msg["guid"] = guid
        self._by_guid[guid] = msg
        pending = self._pending_receipts.pop(guid, None)
        if pending is not None:
            self._stamp_receipt(msg, pending[0], pending[1])

    def _upgrade_echo_guid(
        self, thread: dict, body: str, ts: str, outgoing: bool, guid: str,
    ) -> bool:
        """Give a MAP-first bubble the guid from its iMessage twin.

        Returns True if a message was upgraded (caller may need to repaint).
        """
        if not guid or guid in self._by_guid:
            return False
        for prev in reversed(thread["messages"][-12:]):
            if bool(prev.get("outgoing")) != outgoing or prev.get("body") != body:
                continue
            if ts_gap_seconds(prev.get("ts"), ts) > _ECHO_WINDOW_SECONDS:
                continue
            if prev.get("guid"):
                return False
            self._bind_guid(prev, guid)
            return True
        return False

    @staticmethod
    def _person_key(handle: str) -> str:
        """Stable identity for a typing person (phone digits, else raw)."""
        return normalize_phone(handle) or (handle or "").strip()

    def _handle_in_group(self, thread: dict, handle: str) -> bool:
        """Has this handle spoken in the group (or match a stored phone)?"""
        who = self._person_key(handle)
        if not who:
            return False
        for msg in thread.get("messages") or []:
            sp = msg.get("sender_phone") or ""
            if self._person_key(sp) == who or sp == handle:
                return True
        return False

    def _typing_thread_key(self, handle: str) -> str:
        """Which conversation a typing event belongs to.

        Typing on the wire carries only the typist's handle — no chat_guid —
        so 1:1 resolves via `_by_phone`. When the open thread is a group and
        this person has spoken there, prefer that group: otherwise a group
        typist would only light up their 1:1 (or nowhere).
        """
        if self._current:
            t = self._threads.get(self._current)
            if t and t.get("is_group") and self._handle_in_group(t, handle):
                return self._current
        key = self._by_phone.get(normalize_phone(handle) or "") or ""
        if not key:
            # Fall back to the raw handle: iMessage addresses can be emails,
            # which normalize_phone leaves alone and _by_phone keys directly.
            key = self._by_phone.get(handle, "")
        if key:
            return key
        # No 1:1 and the group isn't open — still try to attach to a group
        # where they've spoken so the indicator isn't dropped entirely.
        for t in self._threads.values():
            if t.get("is_group") and self._handle_in_group(t, handle):
                return t["key"]
        return ""

    def _typing_display_name(self, thread: dict, handle: str) -> str:
        """Best label for a typist: contact book, then a recent group sender name."""
        resolved = self._contacts.resolve(handle)
        if resolved:
            return resolved
        who = self._person_key(handle)
        for msg in reversed(thread.get("messages") or []):
            sp = msg.get("sender_phone") or ""
            if self._person_key(sp) == who or sp == handle:
                name = (msg.get("sender_name") or "").strip()
                if name:
                    return name
        return handle or "?"

    def _set_typing(self, handle: str, typing: bool) -> None:
        """Show/hide typing for `handle` in the conversation they belong to."""
        key = self._typing_thread_key(handle)
        if not key:
            return
        who = self._person_key(handle)
        if not who:
            return
        people = self._typing_until.setdefault(key, {})
        if typing:
            people[who] = time.monotonic() + _TYPING_TIMEOUT_SEC
        else:
            people.pop(who, None)
            if not people:
                self._typing_until.pop(key, None)
        if key == self._current:
            self.typingChanged.emit()

    def _watch_events_file(self) -> None:
        # Watch the main DB and its WAL sibling — SQLite WAL mode often only
        # bumps the -wal file's mtime while the writer is active.
        for path in (
            config.MESSAGES_DB,
            Path(str(config.MESSAGES_DB) + "-wal"),
        ):
            p = str(path)
            if path.exists() and p not in self._watcher.files():
                self._watcher.addPath(p)
        parent = str(config.STATE_DIR)
        if parent not in self._watcher.directories():
            self._watcher.addPath(parent)

    @Slot()
    def _sync_from_disk(self) -> None:
        # Re-adding is required after a rewrite: QFileSystemWatcher drops a
        # path once the inode it was watching goes away.
        self._watch_events_file()

        # Live upserts often rewrite low rowids (same guid handle), so an
        # id-cursor alone never sees them. write_seq bumps on every write;
        # when it moves, refresh thread summaries from the store.
        #
        # Do *not* repage the open conversation on every seq bump: a write
        # to any of 20k threads (or a state row elsewhere) used to reload
        # ~60 messages + rebuild every row and jolt the scroll every few
        # seconds. Live D-Bus already applies open-thread updates; only
        # repage when this conversation's summary actually moved.
        try:
            seq = self._db.write_seq()
        except Exception:
            seq = self._last_write_seq
        if seq != self._last_write_seq:
            self._last_write_seq = seq
            open_before = self._open_thread_fingerprint()
            self._refresh_thread_summaries_from_db()
            open_after = self._open_thread_fingerprint()
            cur = self._current
            if (cur and cur in self._threads
                    and open_before is not None
                    and open_after is not None
                    and open_before != open_after):
                t = self._threads[cur]
                if t.get("messages_loaded"):
                    self._load_thread_messages(cur)
                    self._rebuild_messages()

        after = self._last_event_id
        # New high-id inserts (backup re-sync, brand-new guids).
        for rid, ev in self._client.read_events_with_ids(
            kinds={"sms_received", "sms_sent"}, after_id=after
        ):
            if (ev.get("source") == "backup"
                    or ev.get("attachments")
                    or ev.get("reaction_target_guid")):
                self._ingest_backup(ev)
            else:
                self._ingest(ev, outgoing=(ev.get("kind") == "sms_sent"))
            if rid > self._last_event_id:
                self._last_event_id = rid
        for rid, ev in self._client.read_events_with_ids(
            kinds={"message_state"}, after_id=after
        ):
            self._ingest_state(ev)
            if rid > self._last_event_id:
                self._last_event_id = rid
        if self._search_pending.strip() or self._search_query:
            self._dispatch_search()

    def _refresh_thread_summaries_from_db(self) -> None:
        """Pull latest last_ts/preview/name from the threads table.

        Runtime refreshes only the sidebar window (+ open/pinned). A full
        backup can hold 20k+ threads; walking them all on the UI thread
        every write_seq bump was a multi-frame hitch with no visual gain —
        only the visible slice can change the sidebar.
        """
        try:
            # Head of the recency list is what the sidebar can show.
            window = max(self._thread_limit + 40, 80)
            summaries = self._db.list_threads(limit=window)
            have = {m["key"] for m in summaries}
            # Open + pinned may sit outside the window after a long scroll
            # of quieter chats — still need their previews accurate.
            extra_keys = []
            if self._current and self._current not in have:
                extra_keys.append(self._current)
            for k in self._pinned:
                if k not in have and k not in extra_keys:
                    extra_keys.append(k)
            for k in extra_keys:
                meta = self._db.get_thread(k)
                if meta is not None:
                    summaries.append(meta)
        except Exception:
            log.exception("refresh thread summaries failed")
            return
        changed = False
        for meta in summaries:
            key = meta["key"]
            if self._is_deleted(key, meta.get("last_ts") or ""):
                continue
            thread = self._threads.get(key)
            phone = meta.get("phone") or ""
            if key.startswith("tel:") and not phone:
                phone = key[4:]
            is_group = bool(meta.get("is_group"))
            if thread is None:
                thread = {
                    "key": key,
                    "name": meta.get("name") or key,
                    "phone": None if is_group else (phone or None),
                    "messages": [],
                    "unread": 0,
                    "is_group": is_group,
                    "last_ts": meta.get("last_ts") or "",
                    "last_preview": meta.get("last_preview") or "",
                    "last_event_id": int(meta.get("last_event_id") or 0),
                    "messages_loaded": False,
                    "has_older": True,
                }
                self._threads[key] = thread
                if not is_group and phone:
                    norm = normalize_phone(phone) or phone
                    self._by_phone.setdefault(norm, key)
                if key.startswith("tel:"):
                    self._by_phone.setdefault(key[4:], key)
                changed = True
            else:
                new_eid = int(meta.get("last_event_id") or 0)
                if ((meta.get("last_ts") or "") != (thread.get("last_ts") or "")
                        or (meta.get("last_preview") or "")
                        != (thread.get("last_preview") or "")
                        or new_eid != int(thread.get("last_event_id") or 0)):
                    changed = True
                thread["last_ts"] = meta.get("last_ts") or thread.get("last_ts")
                thread["last_preview"] = (
                    meta.get("last_preview") or thread.get("last_preview") or ""
                )
                thread["last_event_id"] = new_eid or int(
                    thread.get("last_event_id") or 0
                )
                thread["is_group"] = is_group
                if is_group:
                    # Never overwrite a group title with a member's contact name.
                    chat = (meta.get("name") or "").strip()
                    if chat and chat.lower() not in ("group message",):
                        thread["name"] = chat
                    thread["phone"] = None
                else:
                    if meta.get("name"):
                        thread["name"] = meta["name"]
                    if phone:
                        thread["phone"] = phone
            self._recount_unread(thread)
        if changed:
            self._refresh_threads()

    # ---- QML-visible models ---------------------------------------------

    @Property(QObject, constant=True)
    def threadModel(self) -> QObject:
        return self._thread_model

    @Property(QObject, constant=True)
    def searchModel(self) -> QObject:
        return self._search_model

    @Property(QObject, constant=True)
    def messageModel(self) -> QObject:
        return self._message_model

    @Property(bool, notify=searchChanged)
    def searchActive(self) -> bool:
        # Active as soon as the field is non-empty — don't wait for FTS.
        return bool(self._search_pending.strip() or self._search_query.strip())

    @Property(str, notify=searchChanged)
    def searchQuery(self) -> str:
        return self._search_pending or self._search_query

    def _clear_search_state(self) -> None:
        """Leave search mode: conversation list back, no leftover hits.

        Bumps generation so any in-flight FTS result is dropped when it
        lands. Does not touch `_search_pending` — the caller owns that.
        """
        self._search_timer.stop()
        self._search_gen += 1
        self._search_query = ""
        self._search_again = False
        # Can't abort the worker thread, but the gen bump makes its result
        # a no-op; clear the flag so the next keystroke can dispatch.
        self._search_inflight = False
        self._search_model.reload([])
        self.searchChanged.emit()

    @Slot(str)
    def setSearchQuery(self, text: str) -> None:
        """Live search — QML calls this on every keystroke.

        Switches the sidebar to results immediately, runs FTS off the UI
        thread, and always applies the *latest* query (stale results dropped).
        """
        text = text or ""
        prev_active = self.searchActive
        self._search_pending = text

        if not text.strip():
            # Empty field must always restore the conversation list. A
            # finishing FTS used to re-apply hits after clear (model reset
            # re-enters the event loop; trailing dispatch saw pending!="")
            # and left searchActive stuck true with no way out.
            self._clear_search_state()
            return

        self._search_gen += 1

        # Flip the list to search mode on the first character, before FTS
        # returns — otherwise the conversation list stays up while typing.
        if not prev_active:
            self.searchChanged.emit()

        # Throttle: fire soon, restarting only extends the wait slightly.
        # Unlike debounce, we also kick a run immediately if idle so the
        # first keystroke is not delayed.
        if not self._search_inflight and not self._search_timer.isActive():
            self._dispatch_search()
        else:
            self._search_again = True
            if not self._search_timer.isActive():
                self._search_timer.start()

    @Slot()
    def _dispatch_search(self) -> None:
        """Start a background FTS for the current pending text."""
        q = self._search_pending.strip()
        if not q:
            self._clear_search_state()
            return
        if self._search_inflight:
            self._search_again = True
            return

        gen = self._search_gen
        self._search_inflight = True
        self._search_again = False
        # Snapshot for the worker — MessageStore connections are not shared
        # across threads; open a fresh one in the worker.
        fut = self._search_pool.submit(self._search_worker, q)

        def _poll(f: Future = fut, g: int = gen, query: str = q) -> None:
            if not f.done():
                # Keep the UI responsive; re-check next frame-ish.
                QTimer.singleShot(16, _poll)
                return
            self._search_inflight = False
            try:
                hits = f.result()
            except Exception:
                log.exception("search worker failed")
                hits = []
            # Cleared or a newer keystroke won — never paint stale hits.
            if g != self._search_gen or not self._search_pending.strip():
                if self._search_pending.strip():
                    self._dispatch_search()
                elif self.searchActive or self._search_model.rowCount():
                    # Clear raced with this completion; force the list back.
                    self._search_query = ""
                    self._search_model.reload([])
                    self.searchChanged.emit()
                return
            self._apply_search_hits(query, hits, gen=g)
            # Trailing run only while the field still has text (clearing
            # sets pending to "" — must not restart search from that).
            pending = self._search_pending.strip()
            if pending and (self._search_again or pending != query):
                self._search_again = False
                self._dispatch_search()

        QTimer.singleShot(0, _poll)

    @staticmethod
    def _search_worker(query: str) -> list[dict]:
        store = MessageStore()
        try:
            return store.search(query)
        finally:
            store.close()

    def _apply_search_hits(
        self, query: str, hits: list[dict], *, gen: int | None = None
    ) -> None:
        # Refuse stale or cleared queries. `reload` → endResetModel can
        # re-enter the event loop (user hits × mid-apply); re-check before
        # committing so we never leave searchActive stuck on old hits.
        q = (query or "").strip()
        if not q:
            return
        if gen is not None and gen != self._search_gen:
            return
        if q != self._search_pending.strip():
            return

        rows = []
        for h in hits:
            phone = h.get("phone") or ""
            name = h.get("name") or h.get("threadKey") or ""
            if phone:
                resolved = self._contacts.resolve(phone)
                if resolved:
                    name = resolved
            rows.append({
                "resultId": h["resultId"],
                "threadKey": h["threadKey"],
                "name": name,
                "preview": h.get("previewPlain") or "",
                "richPreview": h.get("preview") or "",
                "stamp": relative_ts(h.get("stamp")),
                "unread": 0,
                "pinned": h["threadKey"] in self._pinned,
                "avatar": _file_url(circular_avatar(
                    self._contacts.resolve_photo(phone))),
                "initials": self._initials(name),
                "eventId": int(h.get("eventId") or 0),
                "guid": h.get("guid") or "",
            })
            # Mid-build clear (slow avatar work on a big hit list).
            if gen is not None and gen != self._search_gen:
                return
            if q != self._search_pending.strip():
                return

        if gen is not None and gen != self._search_gen:
            return
        if q != self._search_pending.strip():
            return

        self._search_query = q
        self._search_model.reload(rows)
        # Reload may re-enter; only advertise active search if still wanted.
        if q != self._search_pending.strip() or (
                gen is not None and gen != self._search_gen):
            self._search_query = ""
            self._search_model.reload([])
        self.searchChanged.emit()

    @Slot()
    def loadMoreThreads(self) -> None:
        """Extend the visible conversation window (infinite scroll)."""
        if self.searchActive:
            return
        total = len(self._threads)
        if self._thread_limit >= total:
            return
        self._thread_limit = min(total, self._thread_limit + _THREAD_PAGE)
        self._refresh_threads()

    @Slot()
    def loadOlderMessages(self) -> None:
        """Scroll-up paging for the open conversation."""
        key = self._current
        if not key:
            return
        thread = self._threads.get(key)
        if thread is None or not thread.get("messages_loaded"):
            return
        if not thread.get("has_older"):
            return
        msgs = thread.get("messages") or []
        if not msgs:
            return
        oldest_id = min(
            (int(m.get("event_id") or 0) for m in msgs), default=0
        )
        if oldest_id <= 0:
            thread["has_older"] = False
            return
        oldest_epoch = min(
            (float(m.get("ts_epoch") or ts_epoch(m.get("ts")) or 0)
             for m in msgs),
            default=0,
        )
        try:
            older = self._db.messages_page(
                key,
                before_epoch=oldest_epoch,
                before_id=oldest_id,
                limit=_MESSAGE_PAGE,
            )
        except Exception:
            log.exception("loadOlderMessages failed")
            return
        if not older:
            thread["has_older"] = False
            return
        # Preserve scroll: QML will re-anchor after prepend via contentY.
        self._prepend_events(thread, older)
        try:
            edge_epoch = min(
                float(ev.get("_ts_epoch") or ts_epoch(ev.get("timestamp")) or 0)
                for _, ev in older
            )
            edge_id = min(eid for eid, _ in older)
            thread["has_older"] = self._db.has_older_messages(
                key, edge_epoch, edge_id
            )
        except Exception:
            thread["has_older"] = len(older) >= _MESSAGE_PAGE
        self._rebuild_messages()

    @Property(int, notify=pinsChanged)
    def pinnedCount(self) -> int:
        """How many pinned threads actually exist (ignores stale keys)."""
        return sum(1 for k in self._pinned if k in self._threads)

    # ---- pinned persistence --------------------------------------------

    @staticmethod
    def _load_pinned() -> list[str]:
        """Pins in user order. The file predates ordering and older builds
        wrote a sorted list, so any list still loads — it just starts out in
        whatever order it was saved."""
        try:
            data = json.loads(_PINNED_FILE.read_text())
        except (OSError, ValueError, TypeError):
            return []
        if not isinstance(data, list):
            return []
        out: list[str] = []
        for k in data:  # de-dupe defensively; order is meaningful now
            if isinstance(k, str) and k not in out:
                out.append(k)
        return out

    def _save_pinned(self) -> None:
        try:
            config.ensure_dirs()
            _PINNED_FILE.write_text(json.dumps(self._pinned))
        except OSError as e:
            log.warning("could not save pinned threads: %s", e)

    # ---- local conversation deletion -------------------------------------

    @staticmethod
    def _load_deleted() -> dict[str, str]:
        try:
            data = json.loads(_DELETED_FILE.read_text())
            return {str(k): str(v) for k, v in data.items()}
        except (OSError, ValueError, AttributeError):
            return {}

    def _save_deleted(self) -> None:
        try:
            config.ensure_dirs()
            _DELETED_FILE.write_text(json.dumps(self._deleted))
        except OSError as e:
            log.warning("could not save deleted threads: %s", e)

    @Slot(str)
    def deleteThread(self, key: str) -> None:
        """Clear a conversation locally.

        Records a cut-off timestamp rather than a permanent tombstone, so a
        new message from that person brings the thread back instead of being
        swallowed forever. Nothing is deleted on the iPhone.
        """
        thread = self._threads.get(key)
        # Cut off at the newest message actually in the thread, computed
        # rather than trusted, so nothing already present slips past it.
        cutoff = ""
        if thread:
            for m in thread["messages"]:
                if ts_epoch(m.get("ts")) > ts_epoch(cutoff):
                    cutoff = m.get("ts") or cutoff
            cutoff = cutoff or thread.get("last_ts") or ""
        self._deleted[key] = cutoff
        self._save_deleted()

        self._threads.pop(key, None)
        if key in self._pinned:
            self._pinned.remove(key)
        self._save_pinned()
        if self._current == key:
            self._current = None
            self._message_model.reload([])
            self.peerChanged.emit()
            self.currentChanged.emit()
        self._refresh_threads()
        log.info("cleared conversation %r locally (cutoff %s)", key, cutoff)

    def _is_deleted(self, key: str, ts: str) -> bool:
        cutoff = self._deleted.get(key)
        if cutoff is None:
            return False
        if ts and ts_epoch(ts) > ts_epoch(cutoff):
            # Newer than the cut-off: the conversation comes back.
            del self._deleted[key]
            self._save_deleted()
            return False
        return True

    # ---- read-state persistence -----------------------------------------

    @staticmethod
    def _load_read_marks() -> dict[str, str]:
        try:
            data = json.loads(_READ_FILE.read_text())
            return {str(k): str(v) for k, v in data.items()}
        except (OSError, ValueError, AttributeError):
            return {}

    def _save_read_marks(self) -> None:
        try:
            config.ensure_dirs()
            _READ_FILE.write_text(json.dumps(self._read_marks))
        except OSError as e:
            log.warning("could not save read state: %s", e)

    def _recount_unread(self, thread: dict) -> None:
        """Unread = incoming messages newer than this thread's read mark."""
        mark = self._read_marks.get(thread["key"], "")
        if thread.get("messages_loaded") and thread.get("messages"):
            thread["unread"] = sum(
                1 for m in thread["messages"]
                if not m["outgoing"] and ts_epoch(m.get("ts")) > ts_epoch(mark))
            return
        # Unloaded archive: ask the store rather than loading the thread.
        try:
            thread["unread"] = self._db.count_unread(thread["key"], mark or None)
        except Exception:
            # Fall back to a boolean-ish badge from last_ts alone.
            if mark and ts_epoch(thread.get("last_ts")) > ts_epoch(mark):
                thread["unread"] = 1
            else:
                thread["unread"] = 0 if mark else 0

    def _mark_read(self, thread: dict) -> None:
        if thread["messages"]:
            self._read_marks[thread["key"]] = thread["messages"][-1].get("ts") or ""
            self._save_read_marks()
        elif thread.get("last_ts"):
            self._read_marks[thread["key"]] = thread["last_ts"]
            self._save_read_marks()
        thread["unread"] = 0
        self._dismiss_popups(thread)

    def _dismiss_popups(self, thread: dict) -> None:
        """Take this conversation's desktop notifications off the screen.

        Reading a message here is the event those popups were waiting for. The
        daemon has no other way to learn it: its only automatic close is a
        BlueZ read-property change, which an iMessage never produces.

        Every incoming handle in the thread, not just its key — a group
        notifies under whoever spoke, so one key would leave the rest on
        screen. Bounded, because a long thread would otherwise send a
        pointlessly large argument on every switch.
        """
        handles: list[str] = []
        seen: set[str] = set()
        for msg in reversed(thread["messages"][-200:]):
            if msg.get("outgoing"):
                continue
            for candidate in (msg.get("sender_phone"), thread.get("phone")):
                if candidate and candidate not in seen:
                    seen.add(candidate)
                    handles.append(candidate)
            if len(handles) >= 16:
                break
        if not handles and thread.get("phone"):
            handles.append(thread["phone"])
        if not handles:
            return
        try:
            self._client.dismiss_notifications(",".join(handles))
        except Exception:
            log.debug("dismiss_notifications failed", exc_info=True)

    def _active_identity(self, thread: dict | None) -> str:
        """Handle / group key the daemon uses to suppress focused-thread popups.

        Groups are identified by their thread key (chat_guid / imessage-group:…),
        which may itself contain commas — never join it into a multi-handle
        list. 1:1 uses the peer phone.
        """
        if not thread:
            return ""
        if thread.get("is_group"):
            return thread.get("key") or ""
        return thread.get("phone") or thread.get("key") or ""

    def _push_active_thread(self) -> None:
        """Sync open-thread + window focus to the daemon for popup suppression."""
        try:
            if not self._window_focused or not self._current:
                self._client.set_active_thread("", False)
                return
            thread = self._threads.get(self._current)
            identity = self._active_identity(thread)
            if not identity:
                self._client.set_active_thread("", False)
                return
            self._client.set_active_thread(identity, True)
        except Exception:
            log.debug("set_active_thread failed", exc_info=True)

    @Slot(bool)
    def setWindowFocused(self, focused: bool) -> None:
        """Called from the main window on ActivationChange.

        Focused + open thread → suppress new-message popups for that thread.
        Unfocused (minimized, other workspace, covered) → always notify.
        """
        focused = bool(focused)
        if focused == self._window_focused:
            return
        self._window_focused = focused
        # Leaving the window is the same as clicking away: the jump rim is
        # a transient pointer, not something that should outlive focus.
        if not focused:
            self.clearHighlight()
        self._push_active_thread()

    # iOS caps pinned conversations at nine.
    MAX_PINNED = 9

    @Slot(result=int)
    def maxPinned(self) -> int:
        return self.MAX_PINNED

    @Slot(str, result=bool)
    def togglePin(self, key: str) -> bool:
        """Pin/unpin. Returns False if the pin limit is already reached."""
        if key in self._pinned:
            self._pinned.remove(key)
        else:
            if len(self._pinned) >= self.MAX_PINNED:
                log.info("pin limit (%d) reached", self.MAX_PINNED)
                return False
            self._pinned.append(key)
        self._save_pinned()
        self._refresh_threads()
        return True

    @Slot(int, int, result=bool)
    def movePin(self, src: int, dst: int) -> bool:
        """Reorder the pinned grid (drag and drop).

        Indices are into `pinnedThreads`, which skips pins whose thread has
        gone away, so they are mapped back onto `_pinned` by key rather than
        used directly — otherwise a stale pin silently shifts the drop.
        """
        keys = [r["threadKey"] for r in self._pinned_rows()]
        if not (0 <= src < len(keys)) or not (0 <= dst < len(keys)) or src == dst:
            return False
        moved = keys.pop(src)
        keys.insert(dst, moved)
        # Keep any pins with no live thread, in their existing relative order.
        orphans = [k for k in self._pinned if k not in keys]
        self._pinned = keys + orphans
        self._save_pinned()
        self.pinsChanged.emit()
        return True

    # ---- data ----------------------------------------------------------

    def _load_history(self) -> None:
        """Cold start: thread list only. Messages load when a chat is opened.

        Full history is 200k+ rows — replaying it all into RAM made launch
        and memory scale with archive size. The store's `threads` table
        carries previews/timestamps; each conversation's bubbles are paged
        in on demand via `_load_thread_messages`.
        """
        try:
            summaries = self._db.list_threads()
            self._last_event_id = self._db.max_id()
            self._last_write_seq = self._db.write_seq()
        except Exception:
            log.exception("could not load thread list from message store")
            summaries = []
            self._last_event_id = 0
            self._last_write_seq = 0

        for meta in summaries:
            key = meta["key"]
            if self._is_deleted(key, meta.get("last_ts") or ""):
                continue
            is_group = bool(meta.get("is_group")) or key.startswith(
                "imessage-group:"
            )
            phone = meta.get("phone") or ""
            if key.startswith("tel:") and not phone:
                phone = key[4:]
            # Groups must not claim a member's phone — that made "the fucky"
            # show as "aiden" and stole the 1:1 phone mapping.
            thread = {
                "key": key,
                "name": meta.get("name") or key,
                "phone": None if is_group else (phone or None),
                "messages": [],
                "unread": 0,
                "is_group": is_group,
                "last_ts": meta.get("last_ts") or "",
                "last_preview": meta.get("last_preview") or "",
                "last_event_id": int(meta.get("last_event_id") or 0),
                "messages_loaded": False,
                "has_older": True,
            }
            self._threads[key] = thread
            if not is_group:
                if phone:
                    norm = normalize_phone(phone) or phone
                    self._by_phone.setdefault(norm, key)
                if key.startswith("tel:"):
                    self._by_phone.setdefault(key[4:], key)
            self._recount_unread(thread)

        self._migrate_pinned_keys()

        # Catch anything the daemon wrote after the last UI session without
        # forcing a full replay — only rows newer than the cursor.
        self._sync_from_disk()

        # Synchronous here: the debounce timer needs a running event loop,
        # which doesn't exist yet during construction.
        self._refresh_threads_now()
        # Open the most recent conversation rather than landing on an empty
        # pane, the way Messages.app restores a thread on launch.
        order = self._ordered()
        if order:
            # mark_read=False: restoring a thread on launch shouldn't clear
            # its unread badge before the user has actually looked at it.
            self._open(order[0]["key"], mark_read=False)

    def _migrate_pinned_keys(self) -> None:
        """Map legacy display-name pins onto stable tel:/group keys."""
        if not self._pinned:
            return
        # name.lower() → key, preferring 1:1 over groups for ambiguous names.
        by_name: dict[str, str] = {}
        for t in self._threads.values():
            for label in (t.get("name"), self._display_name(t)):
                if not label:
                    continue
                by_name.setdefault(str(label).strip().lower(), t["key"])
        # Contacts cache: "dad" → phone → tel: key
        new_pins: list[str] = []
        changed = False
        for pin in self._pinned:
            if pin in self._threads:
                new_pins.append(pin)
                continue
            key = by_name.get(pin.strip().lower())
            if key is None:
                # Try resolving the label as a contact name → phone.
                try:
                    matches = self._contacts.find_by_name(pin)
                except Exception:
                    matches = []
                for _name, phone in matches or []:
                    norm = normalize_phone(phone) or phone
                    cand = self._by_phone.get(norm) or f"tel:{norm}"
                    if cand in self._threads:
                        key = cand
                        break
                    if f"tel:{norm}" in self._threads:
                        key = f"tel:{norm}"
                        break
                # Nickname / partial: "quinton johnson" → thread named "q".
                if key is None:
                    tokens = [t for t in pin.lower().split() if len(t) > 1]
                    for tkey, t in self._threads.items():
                        if t.get("is_group"):
                            continue
                        label = (t.get("name") or "").lower()
                        if label and tokens and (
                            label in pin.lower()
                            or any(label == tok or tok.startswith(label)
                                   for tok in tokens)
                        ):
                            key = tkey
                            break
            if key and key in self._threads:
                if key not in new_pins:
                    new_pins.append(key)
                changed = True
            else:
                # Drop unresolvable legacy pins rather than leaving ghosts.
                changed = True
                log.info("dropping unresolvable pin %r", pin)
        # de-dupe preserve order
        seen: set[str] = set()
        ordered = []
        for k in new_pins:
            if k not in seen:
                seen.add(k)
                ordered.append(k)
        if changed or ordered != self._pinned:
            self._pinned = ordered
            self._save_pinned()
            self.pinsChanged.emit()

    def _ingest(self, ev: dict, *, outgoing: bool, refresh: bool = True) -> None:
        handle = ev.get("handle")
        if handle:
            if handle in self._seen_handles:
                return
            self._seen_handles.add(handle)
        key, thread = self._thread_for(ev)
        # Hidden by a local delete, unless this message is newer than the
        # cut-off — in which case the conversation comes back.
        if self._is_deleted(key, event_ts(ev)):
            return

        # Archive not loaded yet and this isn't the open chat: only bump the
        # sidebar preview. Opening the thread pages messages from SQLite.
        if (not thread.get("messages_loaded")
                and key != self._current
                and not (ev.get("reaction_verb") or ev.get("im_reaction_verb"))):
            body = ev.get("body") or ""
            ts = event_ts(ev)
            if ts_epoch(ts) > ts_epoch(thread.get("last_ts")):
                thread["last_ts"] = ts
                thread["last_preview"] = body.replace("\n", " ")[:200]
            if refresh:
                if not outgoing:
                    self._recount_unread(thread)
                self._refresh_threads()
            return
        if not thread.get("messages_loaded") and key == self._current:
            self._load_thread_messages(key)

        # Older events on disk predate reaction tagging — re-derive from the
        # body if the daemon didn't already stamp it. `im_` is the live D-Bus
        # spelling of the same fields; without it every reaction that arrived
        # over the native transport fell back to parsing its own body.
        verb = ev.get("reaction_verb") or ev.get("im_reaction_verb") or None
        snippet = ev.get("reaction_snippet")
        if verb is None:
            verb, snippet = _detect_reaction(ev.get("body"))

        if verb:
            # A tapback isn't a message of its own — attach it to whichever
            # existing bubble it targets instead of adding a row of its own.
            # By guid when the transport names one; the quoted snippet is
            # MAP's only link and stays the fallback.
            target_guid = (ev.get("reaction_target_guid")
                           or ev.get("im_reaction_target_guid"))
            target = self._by_guid.get(target_guid) if target_guid else None
            if target is None:
                target = self._find_reaction_target(thread, snippet)
            if target is not None:
                self._apply_reaction(target, verb, self._reactor(ev, outgoing))
                if refresh and self._current == key:
                    # Badge list only — never reshape the message list.
                    self._refresh_reactions(target)
            return

        body = ev.get("body") or ""
        ts = event_ts(ev)
        # The daemon prefixes iMessage-only metadata with `im_` to keep it
        # clear of SmsEvent's own fields; the backup path uses bare names for
        # the same things. Normalize here so a message row looks the same
        # whichever way it arrived.
        # `guid` is the persisted field, so it survives a reload from
        # events.jsonl; `im_guid` is the live D-Bus spelling. Accept both, or
        # every message stops being actionable after a restart.
        guid = ev.get("guid") or ev.get("im_guid") or ""

        if self._is_echo(thread, body, ts, outgoing, ev.get("raw_type")):
            # Phone-sent: MAP often lands first (no guid), then iMessage with
            # the same text and a guid. Treat as an upgrade, not a drop —
            # otherwise Delivered/Read never find a target.
            if guid and self._upgrade_echo_guid(
                    thread, body, ts, outgoing, guid):
                if refresh and self._current == key:
                    self._rebuild_messages()
            return

        if self._apply_edit(ev, body):
            if refresh and self._current == key:
                self._rebuild_messages()
            return

        msg = {"body": body, "ts": ts,
               "outgoing": outgoing, "reactions": {},
               "guid": "", "reply_to": (ev.get("reply_to_guid")
                            or ev.get("im_reply_to_guid") or ""),
               # Who said it. Only group threads render this (`_rows_for`),
               # but it has to be set on every live message or the label and
               # avatar come out blank and every participant reads as an
               # unknown number — the backup path set it and this one didn't,
               # so an imported group looked fine until a new message landed.
               # `contact_name` is the resolved one; the raw number is the
               # fallback, matching `SmsEvent.display_sender`.
               "sender_name": (ev.get("sender_name")
                               or ev.get("contact_name")
                               or ev.get("sender_phone") or ""),
               "sender_phone": ev.get("sender_phone") or "",
               # Receipts arrive later, on their own signal; an outgoing
               # message starts with no state and gains one when Apple acks.
               "state": "",
               "event_id": int(ev.get("_event_id") or 0)}
        # Bind guid (and any receipt that raced ahead) before append so a
        # same-tick rebuild sees the caption.
        self._bind_guid(msg, guid)
        thread["messages"].append(msg)
        self._note_message(thread, msg)
        # max, not "most recently ingested": events.jsonl is not in
        # chronological order (the phone backfills older messages after a
        # reconnect), so assigning blindly left last_ts pointing at an old
        # message — which made the delete cut-off too early and let the
        # conversation resurrect itself on the next load.
        if ts_epoch(msg["ts"]) > ts_epoch(thread.get("last_ts")):
            thread["last_ts"] = msg["ts"]
            thread["last_preview"] = body.replace("\n", " ")[:200]
        if refresh:
            # Anything arriving for a thread we're not looking at is unread;
            # if we are looking at it, it's read the moment it lands.
            if self._current == key:
                self._mark_read(thread)
            elif not outgoing:
                self._recount_unread(thread)
            self._refresh_threads()
            if self._current == key:
                prev = (thread["messages"][-2]
                        if len(thread["messages"]) > 1 else None)
                # The arriving message may continue the previous one's run, in
                # which case the bubble that was last has to hand over its
                # tail and avatar. Appending alone can't know that — only the
                # message that follows decides where a run ends.
                if self._continues_run(prev, msg):
                    self._message_model.close_run()
                for row in self._rows_for(msg, prev):
                    self._message_model.append(row)

    def _thread_for(self, ev: dict) -> tuple[str, dict]:
        """Find (or create) the thread this event belongs to.

        Keys match `message_store.thread_key_for` so live traffic and the
        SQLite summary table agree: groups by chat/membership, 1:1 by
        normalized phone (`tel:…`).
        """
        key = thread_key_for(ev)
        # Prefer an existing phone mapping if the event only carried a name
        # key — rare, but keeps MAP name-only rows with a known phone.
        norm = ev.get("sender_phone_norm") or normalize_phone(
            ev.get("sender_phone"))
        # Any group identifier means a group — including legacy phone-local
        # chat_guids (`any;+;chat…`) that predate imessage-group: keys.
        is_group = bool(
            key.startswith("imessage-group:")
            or ev.get("group_key")
            or ev.get("chat_guid")
            or ev.get("is_group")
        )
        if not is_group and key.startswith("name:") and norm:
            existing = self._by_phone.get(norm)
            if existing is not None and existing in self._threads:
                key = existing

        thread = self._threads.get(key)
        if thread is None:
            phone = ev.get("sender_phone") or ev.get("sender_phone_norm")
            if key.startswith("tel:") and not phone:
                phone = key[4:]
            thread = {
                "key": key,
                "name": (
                    (ev.get("chat_name") or "Group message")
                    if is_group
                    else (ev.get("contact_name") or phone or key)
                ),
                "phone": None if is_group else phone,
                "messages": [],
                "unread": 0,
                "is_group": is_group,
                "last_ts": "",
                "last_preview": "",
                # Live-created threads have no archive yet — treat as loaded
                # so new bubbles append immediately.
                "messages_loaded": True,
                "has_older": False,
            }
            self._threads[key] = thread
        else:
            if is_group:
                thread["is_group"] = True
                thread["phone"] = None
                if ev.get("chat_name"):
                    thread["name"] = ev["chat_name"]
            else:
                if not thread.get("phone") and ev.get("sender_phone"):
                    thread["phone"] = ev["sender_phone"]
        # Never map a member's phone onto a group key — that routes their
        # 1:1 traffic into the group and the reverse.
        if not is_group:
            if norm:
                self._by_phone.setdefault(norm, key)
            if key.startswith("tel:"):
                self._by_phone.setdefault(key[4:], key)
        return key, thread

    def _load_thread_messages(
        self,
        key: str,
        *,
        around_event_id: int | None = None,
    ) -> None:
        """Populate a thread's message list from SQLite (paged)."""
        thread = self._threads.get(key)
        if thread is None:
            return
        try:
            if around_event_id:
                rows = self._db.messages_around(
                    key, around_event_id, before=40, after=40
                )
            else:
                rows = self._db.messages_page(key, limit=_MESSAGE_PAGE)
        except Exception:
            log.exception("load thread messages failed for %s", key)
            thread["messages_loaded"] = True
            thread["has_older"] = False
            return

        # Drop guids that belonged only to this thread's previous page.
        for msg in thread.get("messages") or []:
            g = msg.get("guid")
            if g and self._by_guid.get(g) is msg:
                self._by_guid.pop(g, None)
        thread["messages"] = []
        thread["_dupe_index"] = None
        thread["messages_loaded"] = True
        self._unsorted.discard(id(thread))

        self._materialize_events(thread, rows)

        if rows:
            try:
                edge_epoch = min(
                    float(ev.get("_ts_epoch") or ts_epoch(ev.get("timestamp")) or 0)
                    for _, ev in rows
                )
                edge_id = min(eid for eid, _ in rows)
                thread["has_older"] = self._db.has_older_messages(
                    key, edge_epoch, edge_id
                )
            except Exception:
                thread["has_older"] = len(rows) >= _MESSAGE_PAGE
        else:
            thread["has_older"] = False

        # Apply delivery/edit/attachment state for guids we loaded.
        guids = [m.get("guid") for m in thread["messages"] if m.get("guid")]
        try:
            for st in self._db.states_for_guids(guids):
                self._ingest_state(st, refresh=False)
        except Exception:
            log.exception("state hydrate failed for %s", key)

        if thread.get("last_ts") is None or not thread.get("last_ts"):
            if thread["messages"]:
                thread["last_ts"] = thread["messages"][-1].get("ts") or ""

    def _materialize_events(
        self, thread: dict, rows: list[tuple[int, dict]], *, prepend: bool = False
    ) -> None:
        """Turn store rows into in-memory message dicts on `thread`."""
        built: list[dict] = []
        # Local guid map for reaction targeting within this batch.
        local_by_guid: dict[str, dict] = {}
        # Dedupe within this materialize pass (live+backup twins, MAP+iMessage).
        seen_guids: set[str] = set()
        if prepend:
            for m in thread.get("messages") or []:
                if m.get("guid"):
                    seen_guids.add(m["guid"])

        for eid, ev in rows:
            handle = ev.get("handle") or f"id:{eid}"
            self._seen_handles.add(handle)

            if ev.get("reaction_verb") or ev.get("im_reaction_verb"):
                verb = ev.get("reaction_verb") or ev.get("im_reaction_verb")
                target_guid = (
                    ev.get("reaction_target_guid")
                    or ev.get("im_reaction_target_guid")
                )
                target = (
                    local_by_guid.get(target_guid)
                    or self._by_guid.get(target_guid or "")
                )
                if target is not None and verb:
                    outgoing = ev.get("kind") == "sms_sent"
                    self._apply_reaction(
                        target, verb, self._reactor(ev, outgoing)
                    )
                continue

            body = ev.get("body") or ""
            # Edits that arrive as their own events.
            if ev.get("edited_from_guid") or ev.get("im_edited_from_guid"):
                if self._apply_edit(ev, body):
                    continue

            outgoing = ev.get("kind") == "sms_sent"
            ts = event_ts(ev)
            guid = ev.get("guid") or ev.get("im_guid") or ""
            # Same Apple message, two transport rows (guid:… vs UUID-timestamp,
            # or MAP messageN vs backup guid:…).
            if guid and guid in seen_guids:
                continue
            if guid and guid in local_by_guid:
                continue
            attachments = ev.get("attachments") or []

            def _same_bubble(p: dict) -> bool:
                return (
                    bool(p.get("outgoing")) == outgoing
                    and (p.get("body") or "") == body
                    and bool(body)
                    and ts_gap_seconds(p.get("ts"), ts) <= 120
                )

            pool = built + (thread.get("messages") or [] if prepend else [])
            # MAP/transfer without guid: drop if a twin already exists.
            if not guid and body and not attachments:
                if any(_same_bubble(p) for p in pool):
                    continue
            # Guid row wins over earlier guid-less MAP/transfer echoes.
            if guid and body:
                built = [m for m in built if not (
                    not m.get("guid") and _same_bubble(m)
                )]

            msg = {
                "body": body,
                "ts": ts,
                "outgoing": outgoing,
                "reactions": {},
                "attachments": attachments,
                "guid": guid,
                "reply_to": (
                    ev.get("reply_to_guid") or ev.get("im_reply_to_guid") or ""
                ),
                "sender_name": (
                    ev.get("sender_name")
                    or ev.get("contact_name")
                    or ev.get("sender_phone")
                    or ""
                ),
                "sender_phone": ev.get("sender_phone") or "",
                "state": "",
                "event_id": eid,
                "ts_epoch": float(
                    ev.get("_ts_epoch") or ts_epoch(ts) or 0
                ),
            }
            if guid:
                local_by_guid[guid] = msg
                self._by_guid[guid] = msg
                seen_guids.add(guid)
            built.append(msg)
            self._note_message(thread, msg)
            if ts_epoch(ts) > ts_epoch(thread.get("last_ts")):
                thread["last_ts"] = ts
                thread["last_preview"] = body.replace("\n", " ")[:200]

        if prepend:
            thread["messages"] = built + thread["messages"]
        else:
            thread["messages"].extend(built)
            thread["messages"].sort(key=lambda m: (
                ts_epoch(m.get("ts")), int(m.get("event_id") or 0)
            ))

    def _prepend_events(
        self, thread: dict, rows: list[tuple[int, dict]]
    ) -> None:
        self._materialize_events(thread, rows, prepend=True)

    # ---- backup import ---------------------------------------------------

    def _sort_pending(self) -> None:
        """Put messages back in time order after a bulk ingest.

        `_ingest_backup` appends without sorting — sorting per message is
        quadratic and made a full-history import never finish. The cost is
        that both callers have to do this afterwards, and forgetting is
        silent-ish: threads still report the right `last_ts` (it's a max), so
        the conversation list looks correct while the messages themselves are
        in append order — live ones first, then the whole backup dumped on
        top. In practice that buries anything new above years of history and
        reads as "my new messages disappeared".
        """
        for thread in self._threads.values():
            if id(thread) in self._unsorted:
                thread["messages"].sort(key=lambda m: ts_epoch(m.get("ts")))
        self._unsorted = set()

    def import_backup_events(self, events: list[dict]) -> int:
        """Merge normalized backup events into the live threads.

        Backup rows carry things MAP can't: messages sent from the phone,
        attachments, exact tapback targets and reply links. They're keyed by
        GUID so re-syncing is idempotent, and content-deduped against
        messages we already received live over MAP.
        """
        added = 0
        self._unsorted = set()
        for ev in events:
            if self._ingest_backup(ev):
                added += 1

        self._sort_pending()
        self._refresh_threads()
        self._rebuild_messages()
        log.info("imported %d new messages from backup", added)
        return added

    def _ingest_backup(self, ev: dict) -> bool:
        handle = ev.get("handle")
        if handle in self._seen_handles:
            return False

        key, thread = self._thread_for(ev)
        if self._is_deleted(key, event_ts(ev)):
            return False

        guid = ev.get("guid")
        outgoing = ev.get("kind") == "sms_sent"
        ts = event_ts(ev)

        if not thread.get("messages_loaded") and key != self._current:
            body = ev.get("body") or ""
            if not ev.get("reaction_verb") and ts_epoch(ts) > ts_epoch(
                    thread.get("last_ts")):
                thread["last_ts"] = ts
                thread["last_preview"] = body.replace("\n", " ")[:200]
            self._seen_handles.add(handle)
            self._refresh_threads()
            return False
        if not thread.get("messages_loaded") and key == self._current:
            self._load_thread_messages(key)

        # A tapback isn't a message. Unlike the MAP path — which can only
        # guess the target by matching quoted text — the backup gives the
        # exact GUID of the message being reacted to.
        if ev.get("reaction_verb"):
            target_guid = ev.get("reaction_target_guid")
            target = self._by_guid.get(target_guid) if target_guid else None
            if target is not None:
                self._apply_reaction(target, ev["reaction_verb"],
                                     self._reactor(ev, outgoing))
                self._seen_handles.add(handle)
                return False
            return False

        body = ev.get("body") or ""
        if self._apply_edit(ev, body):
            self._seen_handles.add(handle)
            return False

        attachments = ev.get("attachments") or []
        if self._already_have(thread, body, ts, outgoing, attachments):
            self._seen_handles.add(handle)
            return False

        msg = {"body": body, "ts": ts, "outgoing": outgoing,
               "reactions": {}, "attachments": attachments,
               # Both spellings, as in the live path: `im_reply_to_guid` is
               # the D-Bus name, `reply_to_guid` the one that persists. Taking
               # only one meant a reply stopped reading as a reply after a
               # restart.
               "guid": guid, "reply_to": (ev.get("reply_to_guid")
                            or ev.get("im_reply_to_guid") or ""),
               "sender_name": ev.get("sender_name") or "",
               "sender_phone": ev.get("sender_phone") or "",
               "event_id": int(ev.get("_event_id") or 0)}
        thread["messages"].append(msg)
        self._note_message(thread, msg)
        # Sorting here would re-sort the whole thread on every single insert —
        # quadratic, and it made a full-history import (223k messages) peg a
        # core for minutes without finishing. Threads are sorted once at the
        # end of import_backup_events instead.
        self._unsorted.add(id(thread))
        if ts_epoch(ts) > ts_epoch(thread.get("last_ts")):
            thread["last_ts"] = ts
            thread["last_preview"] = body.replace("\n", " ")[:200]
        if guid:
            self._by_guid[guid] = msg
        self._seen_handles.add(handle)
        return True

    def _reply_snippet(self, msg: dict) -> str:
        """Text of the message `msg` replies to, or "" if it isn't a reply.

        Replies were being stored with their target all along but never shown,
        so a reply rendered as an ordinary bubble — indistinguishable from
        replying not working.

        When the target has U+F00A inline media, the snippet is rich text with
        the sticker at quote height so the quote matches the original line
        instead of a tofu glyph (or no image at all).
        """
        target_guid = msg.get("reply_to") or ""
        if not target_guid:
            return ""
        target = self._by_guid.get(target_guid)
        if target is None:
            # Replying to something older than what we've loaded. Say so
            # rather than dropping the quote, so the bubble still reads as a
            # reply.
            return "…"
        body = target.get("body") or ""
        atts = target.get("attachments") or []
        plain, rich, _, _ = _body_with_inline_media(
            body, atts, height_px=_REPLY_PX)
        # Cap plain length the way we always did. With an inline image the
        # rich form is what the quote draws; only use it while the caption
        # is short enough that we aren't silently dropping the rest of a
        # long target.
        if len(plain) > 120:
            return plain[:120]
        if rich:
            return rich
        return plain

    def _apply_edit(self, ev: dict, body: str) -> bool:
        """Rewrite an edited message in place. True if this event was one.

        An edit is not a new message: Apple delivers it as its own event
        naming the message it replaces. Appending it verbatim is why editing
        appeared to work on the phone but not here — the original bubble kept
        its old text and the correction landed underneath as a separate line.

        Both spellings of the field are accepted: `im_edited_from_guid` is the
        live D-Bus name, `edited_from_guid` the one that persists to disk and
        comes out of the backup. Taking only one would mean edits stopped
        applying after a restart.
        """
        edited_from = (ev.get("edited_from_guid")
                       or ev.get("im_edited_from_guid") or "")
        if not edited_from:
            return False
        target = self._by_guid.get(edited_from)
        if target is None:
            # The original predates anything we've loaded, so there's nothing
            # to rewrite. Report it as not-an-edit so the caller shows the new
            # text as its own message — a stray bubble beats silently
            # swallowing the correction.
            return False
        # Already at this text (replaying a persisted state line) — don't
        # grow the edit history again.
        if target.get("body") == body:
            return True
        # Keep what it used to say; the UI reveals it behind "Edited".
        target.setdefault("edits", []).append(target["body"])
        target["body"] = body
        return True

    @staticmethod
    def _already_have(thread: dict, body: str, ts: str, outgoing: bool,
                      attachments: list) -> bool:
        """Did this message already arrive live over MAP?

        MAP and the backup key messages completely differently (transfer
        number vs GUID), so handle dedupe can't catch the overlap. Match on
        body + direction within a couple of minutes instead. A row that
        carries attachments is always kept — MAP never delivers those, so it
        strictly adds information even if the caption matches.
        """
        if attachments:
            return False
        # Only messages with identical text and direction can match, so look
        # those up directly instead of walking the thread. Scanning every
        # message per insert is quadratic; on a full-history import that was
        # the difference between seconds and never finishing.
        for prev_ts in ThreadStore._message_index(thread).get((outgoing, body), ()):
            if ts_gap_seconds(prev_ts, ts) <= 120:
                return True
        return False

    @staticmethod
    def _message_index(thread: dict) -> dict:
        """(outgoing, body) → timestamps already in this thread.

        Built once per thread on first use and maintained on append. Kept
        under a private key on the thread rather than in a parallel structure
        so it can't outlive the thread it describes.
        """
        idx = thread.get("_dupe_index")
        if idx is None:
            idx = {}
            for m in thread["messages"]:
                idx.setdefault((m["outgoing"], m["body"]), []).append(m.get("ts"))
            thread["_dupe_index"] = idx
        return idx

    @staticmethod
    def _note_message(thread: dict, msg: dict) -> None:
        """Keep the duplicate index in step with an appended message."""
        idx = thread.get("_dupe_index")
        if idx is not None:
            idx.setdefault((msg["outgoing"], msg["body"]), []).append(msg.get("ts"))

    @staticmethod
    def _is_echo(thread: dict, body: str, ts: str, outgoing: bool,
                 raw_type: str | None = None) -> bool:
        """True if this is the phone echoing back a message we just sent.

        Sending from here produces a MessageSent event keyed by the OBEX
        transfer path; the iPhone then pushes the same message from its SENT
        folder under a completely different handle, so handle-dedupe can't
        catch it. Match on body + direction within a short window instead.

        `raw_type` says where the event came from, and only an echo can be
        suppressed. A message we generated ourselves is never one: matching on
        text alone meant sending the same word twice in three minutes silently
        discarded the second one, so "test" → "test" showed a single bubble
        and looked like sending had stopped working.
        """
        if raw_type == "sms_sent":
            return False
        if not outgoing or not body:
            return False
        for prev in reversed(thread["messages"][-12:]):
            if not prev["outgoing"] or prev["body"] != body:
                continue
            if ts_gap_seconds(prev.get("ts"), ts) <= _ECHO_WINDOW_SECONDS:
                return True
        return False

    @staticmethod
    def _reactor(ev: dict, outgoing: bool) -> str:
        """Who put this tapback on. One key per person.

        A message can hold a tapback from everyone in the thread, and each
        person has exactly one: reacting again replaces what they had, and
        their removal takes only theirs off. Storing reactions in a dict keyed
        by this is what keeps those three rules from needing any code.
        """
        if outgoing:
            return "me"
        return (ev.get("sender_phone_norm") or ev.get("sender_phone")
                or ev.get("sender_name") or ev.get("contact_name") or "them")

    @staticmethod
    def _apply_reaction(target: dict, verb: str, reactor: str) -> None:
        """Record one person's tapback on a message, or withdraw it."""
        reactions = target.setdefault("reactions", {})
        if verb in _REACTION_REMOVED:
            reactions.pop(reactor, None)
            return
        emoji = reaction_emoji(verb)
        if emoji:
            reactions[reactor] = emoji

    @staticmethod
    def _find_reaction_target(thread: dict, snippet: str | None) -> dict | None:
        if not snippet:
            return None
        snippet = snippet.strip()
        for msg in reversed(thread["messages"]):
            body = (msg.get("body") or "").strip()
            if body == snippet or snippet in body or body in snippet:
                return msg
        return None

    # ---- projections ----------------------------------------------------

    def _ordered(self) -> list[dict]:
        return sorted(
            self._threads.values(),
            key=lambda t: ts_epoch(t.get("last_ts")),
            reverse=True,
        )

    def _refresh_threads(self) -> None:
        """Coalesce rebuilds. Each one resets the model, which tears down and
        recreates every delegate — doing that once per message during a
        backfill burst is what made the sidebar stutter."""
        self._refresh_pending.start()

    def _refresh_threads_now(self) -> None:
        ordered = self._ordered()
        if len(ordered) > self._thread_limit:
            head = ordered[:self._thread_limit]
            # Pinned conversations always render, even past the window.
            tail = [t for t in ordered[self._thread_limit:]
                    if t["key"] in self._pinned]
            ordered = head + tail
        rows = []
        for t in ordered:
            if t["messages"]:
                last = t["messages"][-1]["body"]
            else:
                last = t.get("last_preview") or ""
            rows.append({
                "threadKey": t["key"],
                "name": self._display_name(t),
                "preview": (last or "").replace("\n", " "),
                "stamp": relative_ts(t.get("last_ts")),
                "unread": int(t.get("unread", 0)),
                "pinned": t["key"] in self._pinned,
                "avatar": (
                    ""
                    if t.get("is_group")
                    else _file_url(circular_avatar(
                        self._contacts.resolve_photo(t.get("phone"))))
                ),
                "initials": self._initials(self._display_name(t)),
                "resultId": t["key"],
                "eventId": 0,
                "guid": "",
                "richPreview": "",
            })
        self._thread_rows = rows
        self._thread_model.reload(rows)
        self.pinsChanged.emit()

    def _pinned_rows(self) -> list[dict]:
        """The pinned tiles, in user order rather than the list's recency
        order — the grid is arrangeable, so it cannot ride the sidebar's
        sort the way it used to."""
        by_key = {r["threadKey"]: r for r in getattr(self, "_thread_rows", [])}
        return [by_key[k] for k in self._pinned if k in by_key]

    @Property("QVariantList", notify=pinsChanged)
    def pinnedThreads(self) -> list[dict]:
        return self._pinned_rows()

    def _display_name(self, thread: dict) -> str:
        """Re-resolve the name from contacts at render time.

        Events carry whatever name the daemon resolved when the message
        arrived, so history is full of stale names — a nickname added (or
        pulled) later would never show. Look it up fresh from the number and
        fall back to what the event recorded.

        Groups always keep their chat title — resolving the last sender's
        phone turned "the fucky" into "aiden".
        """
        if thread.get("is_group") or str(thread.get("key") or "").startswith(
            "imessage-group:"
        ):
            name = (thread.get("name") or "").strip()
            if name and name not in ("Group message",):
                return name
            return "Group message"
        phone = thread.get("phone")
        if phone:
            resolved = self._contacts.resolve(phone)
            if resolved:
                return resolved
        return thread.get("name") or thread.get("key") or ""

    @staticmethod
    def _initials(name: str) -> str:
        parts = [p for p in str(name).split() if p and p[0].isalnum()]
        if not parts:
            return "#"
        if len(parts) == 1:
            return parts[0][:2].upper()
        return (parts[0][0] + parts[-1][0]).upper()

    @staticmethod
    def _continues_run(prev: dict | None, msg: dict | None) -> bool:
        """Is `msg` part of the same run of bubbles as `prev`?

        A run is consecutive messages from one speaker close together in time.
        A time divider ends a run as well as starting one: the divider belongs
        to the *following* message, so a burst separated by hours would
        otherwise be re-labelled but keep its avatar stranded at the far end.
        """
        if prev is None or msg is None:
            return False
        if bool(prev["outgoing"]) != bool(msg["outgoing"]):
            return False
        if (prev.get("sender_name") or "") != (msg.get("sender_name") or ""):
            return False
        return ts_gap_seconds(prev.get("ts"),
                              msg.get("ts")) < _TIME_DIVIDER_GAP_SECONDS

    def _rows_for(self, msg: dict, prev: dict | None,
                  nxt: dict | None = None) -> list[dict]:
        """Rows to draw for one message.

        Photos and free-standing stickers (Bitmoji, peels) get their own rows.
        True inline media — marked by U+F00A in the body, as in
        "3-0 on my return" — is drawn inside the text bubble. U+FFFC is only
        scrubbed out of captions; those attachments stay free-standing.
        """
        # Only stamp the conversation where it actually paused, the way
        # Messages.app does — not once per bubble.
        divider = ""
        if prev is None or ts_gap_seconds(
                prev.get("ts"), msg.get("ts")) >= _TIME_DIVIDER_GAP_SECONDS:
            divider = separator_ts(msg.get("ts")) or format_ts(msg.get("ts"))

        # A pause short of a full divider still deserves to be visible. Not
        # applied when a divider is drawn (it already separates them) or on
        # the first row of the thread, where there's nothing to separate from.
        gap_before = 0
        if prev is not None and not divider and ts_gap_seconds(
                prev.get("ts"), msg.get("ts")) >= _RUN_GAP_SECONDS:
            gap_before = _RUN_GAP_PX

        # One badge per distinct emoji, in the order they were added. Three
        # people all sending ❤️ is one heart on the bubble, not three
        # identical badges — iOS counts them instead, which needs a number
        # this doesn't draw yet.
        reactions = list(dict.fromkeys(
            (msg.get("reactions") or {}).values()))

        outgoing = bool(msg["outgoing"])
        atts = msg.get("attachments") or []
        # U+F00A slots become `<img>` tags inside the bubble; free-standing
        # stickers/photos (and U+FFFC Bitmoji) still get their own rows.
        body, rich_body, jumbo, free_atts = _body_with_inline_media(
            msg.get("body") or "", atts)
        body = body.strip()
        # Defense in depth for the bridge's `[name.png]` placeholder: if the
        # body is exactly that and we have a real attachment (with or without
        # bytes yet), don't also draw a filename caption under the media.
        # `_apply_state` clears this for live downloads; backup/import can
        # still leave both on the same message.
        if body and atts:
            placeholders = {f"[{a.get('name')}]" for a in atts if a.get("name")}
            if body in placeholders:
                body, rich_body, jumbo = "", "", False
        # Only label the sender in a group chat — in a 1:1 thread it's the
        # person whose conversation you already have open.
        thread = self._threads.get(self._current) or {}
        in_group = bool(thread.get("is_group")) and not outgoing

        def same_speaker(other: dict | None) -> bool:
            """Part of the same run of consecutive messages?"""
            if other is None or bool(other["outgoing"]) != outgoing:
                return False
            return (other.get("sender_name") or "") == (
                msg.get("sender_name") or "")

        # Name goes above the first message of a run; the avatar sits beside
        # the last one — repeating either on every message is noise.
        #
        # Shares `_continues_run` with the incremental append path, so a
        # message that lands while the thread is open ends up drawn exactly
        # as a reload would draw it.
        #
        # The run itself is thread-agnostic: the tail is drawn on the last
        # bubble of every run, incoming or outgoing, group or 1:1. Only the
        # name/avatar decorations are gated on it being a group.
        run_ends = not self._continues_run(msg, nxt)
        starts_run = in_group and not (same_speaker(prev) and not divider)
        ends_run = in_group and run_ends

        sender = (msg.get("sender_name") or "") if starts_run else ""
        avatar = ""
        initials = ""
        if ends_run:
            avatar = _file_url(circular_avatar(
                self._contacts.resolve_photo(msg.get("sender_phone"))))
            initials = self._initials(msg.get("sender_name") or "?")

        eid = int(msg.get("event_id") or 0)
        def base_row(kind: str) -> dict:
            return {"kind": kind, "body": "", "outgoing": outgoing,
                    "reactions": [], "divider": "",
                    "mediaOnly": False, "image": "", "fileLabel": "",
                    "sender": "", "senderAvatar": "", "senderInitials": "",
                    "tail": False, "richBody": "", "jumbo": False,
                    "imageW": 0, "imageH": 0,
                    "guid": msg.get("guid") or "", "deliveryState": "",
                    "deliveryStamp": "", "gapBefore": 0,
                    "timeStamp": clock_ts(msg.get("ts")),
                    # Previous texts, oldest first. Non-empty means the
                    # message was edited, which is what draws the marker.
                    "edits": list(msg.get("edits") or []),
                    # Resolved here rather than in QML: the target is looked
                    # up by guid across the whole store, which the delegate
                    # has no access to.
                    "replyBody": self._reply_snippet(msg),
                    # Guid of the parent message, so tapping the quote can
                    # jump to it. Empty when this isn't a reply.
                    "replyGuid": msg.get("reply_to") or "",
                    "eventId": eid,
                    # Search jump lights by store event id; reply-quote jump
                    # lights by iMessage guid (always present on a reply
                    # target). Either match is enough for the blue rim.
                    "highlight": bool(
                        (eid and eid == getattr(
                            self, "_highlight_event_id", 0))
                        or (
                            getattr(self, "_highlight_guid", "")
                            and (msg.get("guid") or "")
                            == getattr(self, "_highlight_guid", "")
                        )
                    )}

        rows: list[dict] = []
        for att in free_atts:
            mime = (att.get("mime") or att.get("mime_type") or "").lower()
            name = att.get("name") or "Attachment"
            url = _file_url(att.get("path"))
            # Prefer a real image/sticker row only when we have a loadable URL.
            # Empty path used to produce kind=image with source="" — QML hid
            # the Image on error and the file chip never showed (bubble is
            # `visible: !isMedia`), so the user saw a blank gap or only the
            # `[name]` placeholder text.
            if att.get("is_sticker") and url:
                r = base_row("sticker")
                r["image"] = url
                # Keep the name so QML can fall back to a file chip if decode
                # fails (corrupt HEIC, missing plugin, …).
                r["fileLabel"] = name
                r["imageW"] = int(att.get("w") or 0)
                r["imageH"] = int(att.get("h") or 0)
            elif _is_image_att(att) and url:
                # HEIC included — Qt decodes it via libheif on this system.
                r = base_row("image")
                r["image"] = url
                r["fileLabel"] = name
                r["imageW"] = int(att.get("w") or 0)
                r["imageH"] = int(att.get("h") or 0)
            elif mime.startswith("video/"):
                # Image can't render video; offer it as a playable file.
                r = base_row("file")
                r["fileLabel"] = f"▶  {name}"
                r["image"] = url
            elif mime.startswith("audio/"):
                r = base_row("file")
                r["fileLabel"] = f"♪  {name}"
                r["image"] = url
            else:
                # Non-image, unknown mime, or image/sticker with no path yet.
                r = base_row("file")
                r["fileLabel"] = name
                r["image"] = url
            rows.append(r)

        if body or not rows:
            r = base_row("text")
            # Plain body has U+FFFC stripped (or never had one), so copy and
            # edit don't carry the placeholder glyph.
            r["body"] = body
            # Emoji need their own font-size to not look shrunken next to
            # the text; stickers land as `<img>` tags in the same rich body.
            # An emoji-only message drops its bubble entirely.
            r["richBody"] = rich_body
            r["jumbo"] = jumbo
            # No attachment and no text: media the phone never handed over
            # (MAP strips it), so say that instead of drawing a blank bubble.
            r["mediaOnly"] = not body and not atts
            rows.append(r)

        # Divider belongs to the first row; the tapback to the last, so it
        # sits on whichever bubble the reaction actually targeted.
        # Stable across rebuilds: message dicts are mutated in place, never
        # replaced, so their identity outlives any change to their content.
        # See `MessageListModel._IDENTITY`.
        for n, r in enumerate(rows):
            r["rowKey"] = f"{id(msg)}-{n}"

        rows[0]["divider"] = divider
        # First row only: a photo and its caption are one message and must
        # stay tight against each other, however long the pause before them.
        rows[0]["gapBefore"] = gap_before
        # Label on the first row of the message, avatar on the last, so a
        # photo-plus-caption still reads as one turn.
        rows[0]["sender"] = sender
        rows[-1]["senderAvatar"] = avatar
        rows[-1]["senderInitials"] = initials
        # Tail on the last row of the run. If that row is a photo or sticker
        # it draws nothing — bare media has no bubble to hang a tail off.
        rows[-1]["tail"] = run_ends
        rows[-1]["reactions"] = reactions
        return rows

    def _open_thread_fingerprint(self) -> tuple | None:
        """Cheap signature of the open conversation's store summary.

        Used by disk sync to decide whether a write_seq bump actually
        touched the chat on screen (vs. some other of the ~20k threads).
        """
        key = self._current
        if not key:
            return None
        t = self._threads.get(key)
        if t is None:
            return None
        # last_ts + preview only: last_event_id is often stale on the live
        # path (D-Bus does not carry the store row id), so including it
        # forced a full repage after every send even when the UI already
        # had the message.
        return (key, t.get("last_ts") or "", t.get("last_preview") or "")

    def _refresh_captions(self) -> None:
        """Repaint delivery captions without rebuilding the message list."""
        thread = self._threads.get(self._current)
        if thread is None:
            return
        rows = self._message_model.rows()
        if not rows:
            return
        self._apply_delivery_captions(thread["messages"], rows)
        self._message_model.captions_changed()

    def _refresh_reactions(self, msg: dict) -> None:
        """Update reaction badges on the rows that belong to `msg` in place.

        Reactions always sit on the last row of a multi-part message (photo +
        caption). Matching by message identity in `rowKey` keeps us correct
        even when the guid is still empty.
        """
        reactions = list(dict.fromkeys((msg.get("reactions") or {}).values()))
        prefix = f"{id(msg)}-"
        rows = self._message_model.rows()
        if not rows:
            return
        last_i = -1
        for i, r in enumerate(rows):
            if str(r.get("rowKey") or "").startswith(prefix):
                last_i = i
                if r.get("reactions") != []:
                    r["reactions"] = []
        if last_i < 0:
            # Fallback: guid match (rows rebuilt with a different msg id).
            guid = msg.get("guid") or ""
            if not guid:
                self._rebuild_messages()
                return
            for i, r in enumerate(rows):
                if r.get("guid") == guid:
                    last_i = i
                    if r.get("reactions") != []:
                        r["reactions"] = []
            if last_i < 0:
                self._rebuild_messages()
                return
        rows[last_i]["reactions"] = reactions
        idx = self._message_model.index(last_i, 0)
        # Also repaint earlier siblings that may have lost a stale badge.
        first_i = last_i
        for i in range(last_i, -1, -1):
            rk = str(rows[i].get("rowKey") or "")
            if rk.startswith(prefix) or (
                    msg.get("guid") and rows[i].get("guid") == msg.get("guid")):
                first_i = i
            else:
                break
        self._message_model.dataChanged.emit(
            self._message_model.index(first_i, 0),
            idx,
            [MessageListModel.ReactionsRole],
        )

    def _rebuild_messages(self) -> None:
        thread = self._threads.get(self._current)
        if thread is None:
            self._message_model.reload([])
            return
        rows, prev = [], None
        msgs = thread["messages"]
        for i, msg in enumerate(msgs):
            nxt = msgs[i + 1] if i + 1 < len(msgs) else None
            rows.extend(self._rows_for(msg, prev, nxt))
            prev = msg

        self._apply_delivery_captions(msgs, rows)
        self._message_model.reload(rows)

    @staticmethod
    def _apply_delivery_captions(msgs: list[dict], rows: list[dict]) -> None:
        """Mark at most two bubbles with a delivery caption, as iOS does.

        Messages.app shows "Read" under the newest message they've read, and
        "Delivered" under the newest message they haven't — so both can be on
        screen at once, on different bubbles.

        Crucially the caption is *not* dropped when the other person replies
        afterwards: an earlier version only painted one when the thread's last
        message was ours, which meant a "Read" vanished the moment they
        answered — which is most of the time.
        """
        newest_read = newest_delivered = None
        for msg in msgs:
            if not msg.get("outgoing"):
                continue
            state = msg.get("state")
            if state == "read":
                newest_read = msg
                # A read message supersedes any pending "Delivered" before it.
                newest_delivered = None
            elif state == "delivered":
                newest_delivered = msg

        # guid -> (state, stamp). Only "Read" carries a time, matching iOS.
        wanted = {}
        if newest_read is not None and newest_read.get("guid"):
            wanted[newest_read["guid"]] = (
                "read", receipt_ts(newest_read.get("state_ts")))
        if newest_delivered is not None and newest_delivered.get("guid"):
            wanted[newest_delivered["guid"]] = ("delivered", "")

        # Clear first: a caption that has moved to a newer bubble must not be
        # left behind on the old one.
        for row in rows:
            row["deliveryState"] = ""
            row["deliveryStamp"] = ""
        if not wanted:
            return

        # One message can produce several rows (a photo plus its caption), so
        # the marker goes on the last of them — the bubble it should sit
        # under.
        for guid, (state, stamp) in wanted.items():
            last_row = None
            for row in rows:
                if row.get("guid") == guid:
                    last_row = row
            if last_row is not None:
                last_row["deliveryState"] = state
                last_row["deliveryStamp"] = stamp

    # ---- QML API --------------------------------------------------------

    @Slot()
    def markCurrentRead(self) -> None:
        """Conversation → Mark as Read, for the thread that's open."""
        thread = self._threads.get(self._current or "")
        if thread is None:
            return
        self._mark_read(thread)
        self._refresh_threads()

    @Property(bool, notify=landAtEndSuppressedChanged)
    def suppressLandAtEnd(self) -> bool:
        return self._suppress_land_at_end

    @Slot(str)
    def openThread(self, key: str) -> None:
        self._highlight_event_id = 0
        self._highlight_guid = ""
        self._suppress_land_at_end = False
        self.landAtEndSuppressedChanged.emit()
        self._open(key, mark_read=True)

    @Slot(str, int, str)
    def openSearchResult(self, thread_key: str, event_id: int, guid: str = "") -> None:
        """Open a conversation and jump to the matching message."""
        if thread_key not in self._threads:
            # Result from a thread we haven't summarized yet — seed it.
            self._threads[thread_key] = {
                "key": thread_key,
                "name": thread_key,
                "phone": thread_key[4:] if thread_key.startswith("tel:") else None,
                "messages": [],
                "unread": 0,
                "is_group": thread_key.startswith("imessage-group:"),
                "last_ts": "",
                "last_preview": "",
                "messages_loaded": False,
                "has_older": True,
            }
        self._highlight_event_id = int(event_id or 0)
        self._highlight_guid = (guid or "").strip()
        self._suppress_land_at_end = bool(event_id)
        self.landAtEndSuppressedChanged.emit()
        self._current = thread_key
        thread = self._threads[thread_key]
        # Always reload around the hit so the target is in the window.
        self._load_thread_messages(
            thread_key, around_event_id=event_id or None
        )
        self._mark_read(thread)
        self._refresh_threads()
        self._rebuild_messages()
        self.currentChanged.emit()
        self.peerChanged.emit()
        self.typingChanged.emit()
        if event_id:
            # Defer so QML has applied the model rebuild before looking up
            # the row index. Keep suppressLandAtEnd true until after the jump
            # settle window — clearing it in the same tick let onCountChanged's
            # landAtEnd win the race and scroll to the newest message instead.
            eid = int(event_id)

            def _jump(e=eid) -> None:
                self.jumpToMessage.emit(e)

            def _release() -> None:
                self._suppress_land_at_end = False
                self.landAtEndSuppressedChanged.emit()

            QTimer.singleShot(0, _jump)
            # Match QML searchJumpRelease (3s hold) so suppressLandAtEnd
            # stays true for the whole re-center window.
            QTimer.singleShot(3200, _release)
        else:
            self._suppress_land_at_end = False
            self.landAtEndSuppressedChanged.emit()

    def _trim_thread_window(self, thread: dict) -> None:
        """Keep only the newest page in memory after accidental over-fetch.

        Auto-paging used to pull whole archives into RAM on open; switching
        away and back then rebuilt thousands of rows. Cap to one page so
        re-open is cheap; scroll-up still pages older history on demand.
        """
        msgs = thread.get("messages") or []
        cap = _MESSAGE_PAGE
        if len(msgs) <= cap * 2:
            return
        drop = msgs[:-cap]
        keep = msgs[-cap:]
        for m in drop:
            g = m.get("guid")
            if g and self._by_guid.get(g) is m:
                self._by_guid.pop(g, None)
        thread["messages"] = keep
        thread["has_older"] = True
        thread["_dupe_index"] = None

    def _open(self, key: str, *, mark_read: bool) -> None:
        self._current = key
        thread = self._threads.get(key)
        if thread is not None and not thread.get("messages_loaded"):
            self._load_thread_messages(key)
        elif thread is not None and thread.get("messages_loaded"):
            # Drop history that was bulk-paged earlier (or before the open
            # auto-page fix) so we don't rebuild multi-k rows every switch.
            self._trim_thread_window(thread)
        if thread is not None and mark_read:
            self._mark_read(thread)
            self._refresh_threads()
        self._rebuild_messages()
        self.currentChanged.emit()
        self.peerChanged.emit()
        # The indicator is per-conversation, so switching threads has to
        # re-evaluate it — otherwise it stays showing whatever the previous
        # thread's state was.
        self.typingChanged.emit()
        # So new messages for this conversation don't raise a desktop popup
        # while we are looking at it (and the window is focused).
        self._push_active_thread()

    @Slot(int, result=int)
    def indexOfEvent(self, event_id: int) -> int:
        """ListView row index for a store event id, or -1."""
        if not event_id:
            return -1
        for i, row in enumerate(self._message_model.rows()):
            if int(row.get("eventId") or 0) == event_id:
                return i
        return -1

    @Slot(str, result=int)
    def indexOfGuid(self, guid: str) -> int:
        """ListView row index for an iMessage guid, or -1.

        A single message can produce several rows (photo + caption); the first
        match is the one we land on, which is the top of that cluster.
        """
        if not guid:
            return -1
        for i, row in enumerate(self._message_model.rows()):
            if (row.get("guid") or "") == guid:
                return i
        return -1

    @Slot()
    def clearHighlight(self) -> None:
        """Drop the search / reply-quote accent rim without rebuilding.

        Called when the user clicks away or the window loses focus. In-place
        so the list does not jump; only the `highlight` role repaints.
        """
        if not getattr(self, "_highlight_event_id", 0) and not getattr(
                self, "_highlight_guid", ""):
            return
        self._highlight_event_id = 0
        self._highlight_guid = ""
        rows = self._message_model.rows()
        touched = False
        for r in rows:
            if r.get("highlight"):
                r["highlight"] = False
                touched = True
        if touched:
            self._message_model.highlight_changed()

    def _set_jump_highlight(self, *, event_id: int = 0, guid: str = "") -> None:
        """Stamp highlight identity and repaint matching rows in place."""
        self._highlight_event_id = int(event_id or 0)
        self._highlight_guid = (guid or "").strip()
        rows = self._message_model.rows()
        if not rows:
            return
        for r in rows:
            eid = int(r.get("eventId") or 0)
            g = r.get("guid") or ""
            r["highlight"] = bool(
                (self._highlight_event_id and eid == self._highlight_event_id)
                or (self._highlight_guid and g == self._highlight_guid)
            )
        self._message_model.highlight_changed()

    @Slot(str)
    def jumpToGuid(self, guid: str) -> None:
        """Scroll the open thread to the message with this guid.

        Used by the reply-quote tap. Mirrors search-hit jump: rim-highlight
        the target (blue stroke via the `highlight` role) and centre the
        view on it. If the target is already loaded, light the rim in place;
        if it isn't, page the store around its event id first.
        """
        if not guid:
            return
        idx = self.indexOfGuid(guid)
        if idx >= 0:
            row = self._message_model.rows()[idx]
            eid = int(row.get("eventId") or 0)
            # In-place rim paint — a full rebuild here was what made a
            # quote-tap flash the list. The click-away path already cleared
            # any previous highlight on press.
            self._set_jump_highlight(event_id=eid, guid=guid)
            if eid:
                def _jump_loaded(e: int = eid) -> None:
                    self.jumpToMessage.emit(e)

                QTimer.singleShot(0, _jump_loaded)
            return
        target = self._by_guid.get(guid)
        if target is None:
            return
        eid = int(target.get("event_id") or 0)
        key = self._current
        if not eid or not key:
            # Guid known but never persisted — paint if the row is already
            # on screen under that guid; otherwise nothing to scroll to.
            self._set_jump_highlight(guid=guid)
            return
        # Same shape as openSearchResult: load a window around the hit so
        # the row exists before QML aims at it.
        self._highlight_event_id = eid
        self._highlight_guid = guid
        self._suppress_land_at_end = True
        self.landAtEndSuppressedChanged.emit()
        try:
            self._load_thread_messages(key, around_event_id=eid)
        except Exception:
            log.exception("jumpToGuid load failed for %s", guid)
            self._suppress_land_at_end = False
            self.landAtEndSuppressedChanged.emit()
            return
        self._rebuild_messages()

        def _jump(e: int = eid) -> None:
            self.jumpToMessage.emit(e)

        def _release() -> None:
            self._suppress_land_at_end = False
            self.landAtEndSuppressedChanged.emit()

        QTimer.singleShot(0, _jump)
        QTimer.singleShot(3200, _release)

    # Properties, not slots: QML binds to these and re-evaluates whenever
    # peerChanged fires. As one-shot slots they were evaluated once at
    # delegate creation — before any thread was open — and never refreshed.

    @Property(str, notify=peerChanged)
    def currentKey(self) -> str:
        """Key of the open thread. The pinned grid is a Repeater, so it has
        no `currentIndex` to select on the way the thread list does."""
        return self._current or ""

    @Property(str, notify=peerChanged)
    def peerName(self) -> str:
        t = self._threads.get(self._current)
        return self._display_name(t) if t else ""

    @Property(str, notify=peerChanged)
    def peerAvatar(self) -> str:
        t = self._threads.get(self._current)
        if not t:
            return ""
        return _file_url(circular_avatar(
            self._contacts.resolve_photo(t.get("phone"))))

    @Property(bool, notify=peerChanged)
    def peerIsGroup(self) -> bool:
        t = self._threads.get(self._current)
        return bool(t and t.get("is_group"))

    @Property(str, notify=peerChanged)
    def peerInitials(self) -> str:
        t = self._threads.get(self._current)
        return self._initials(self._display_name(t)) if t else ""

    @Slot(result=bool)
    def hasThread(self) -> bool:
        return self._current is not None

    @Slot(str)
    def send(self, body: str) -> None:
        body = body.strip()
        thread = self._threads.get(self._current)
        if not body or thread is None:
            return
        # The outgoing bubble is added when the daemon's MessageSent signal
        # arrives — no optimistic append, so there's no chance of a duplicate.
        self._client.send_message(
            thread["phone"], body,
            lambda _t: None,
            lambda text: log.warning("send failed: %s", text))

    # ---- iMessage-only actions ------------------------------------------
    #
    # Each names its target by guid, so each is offered only on messages that
    # arrived natively. QML gates the menu items on `guid !== ""`; these
    # re-check anyway, since a stale delegate could outlive the check.

    def _peer(self) -> str | None:
        thread = self._threads.get(self._current or "")
        return thread["phone"] if thread else None

    def _act(self, what: str, fn, *args) -> None:
        peer = self._peer()
        if not peer:
            return
        fn(peer, *args,
           lambda _r: None,
           lambda text: log.warning("%s failed: %s", what, text))

    @Slot(str, str)
    def react(self, guid: str, kind: str) -> None:
        """Add a tapback. `kind` is a verb name or a literal emoji."""
        if not guid:
            return
        # The quoted snippet iOS renders on devices without real tapbacks.
        msg = self._by_guid.get(guid) or {}
        self._act("react", self._client.react, guid, kind,
                  (msg.get("body") or "")[:64])

    @Slot(str, str)
    def unreact(self, guid: str, kind: str) -> None:
        if not guid:
            return
        self._act("unreact", self._client.unreact, guid, kind)

    @Slot(str, str)
    def replyTo(self, guid: str, body: str) -> None:
        body = body.strip()
        if not guid or not body:
            return
        # The target's text goes with it: Apple's reply format encodes a
        # character range over the message being replied to, so without it the
        # reply is delivered as an ordinary message. Same reason `react`
        # passes the snippet.
        target = self._by_guid.get(guid) or {}
        self._act("reply", self._client.send_reply, body, guid,
                  target.get("body") or "")

    @Slot(str)
    def copyText(self, text: str) -> None:
        """Put a message on the clipboard.

        Here rather than in QML because QtQuick exposes no clipboard; the
        alternative is a hidden TextEdit to select-all and copy from, which is
        the trick this exists to avoid.
        """
        if not text:
            return
        clipboard = QGuiApplication.clipboard()
        if clipboard is None:  # no GUI app (tests)
            return
        clipboard.setText(text)

    @Slot(str, str)
    def editMessage(self, guid: str, new_text: str) -> None:
        new_text = new_text.strip()
        if not guid or not new_text:
            return
        self._act("edit", self._client.edit_message, guid, new_text)

    @Slot(str)
    def unsendMessage(self, guid: str) -> None:
        if not guid:
            return
        self._act("unsend", self._client.unsend_message, guid)

    @Slot(bool)
    def setTyping(self, typing: bool) -> None:
        """Tell the other side we're composing. Best-effort, never blocks."""
        peer = self._peer()
        if not peer:
            return
        # Synchronous, so the error comes back as a return value rather than
        # through a callback. Logged rather than swallowed: a silently
        # failing indicator looks identical to one that simply isn't wired.
        err = self._client.set_typing(peer, typing)
        if err:
            log.warning("set_typing(%s, %s) failed: %s", peer, typing, err)
