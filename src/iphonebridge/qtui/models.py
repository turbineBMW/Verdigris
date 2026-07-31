"""Qt list models backing the QML conversation views.

`ThreadStore` owns the actual conversation state (ported from the GTK
`ConversationsPage`); the two QAbstractListModels are thin projections of it
for QML — one over the thread list, one over the open thread's messages.
"""
from __future__ import annotations

import json
import logging
import time
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
# Two icon sets: the tail/orientation mirrors depending on which side of the
# thread the reacted-to message is on, same as native Messages.app.
_REACTION_ASSETS_INCOMING = _ASSETS_DIR / "reactions"
_REACTION_ASSETS_OUTGOING = _ASSETS_DIR / "reactions_outgoing"

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
# Live inline-media slot (U+F00A). The matching image attachment is drawn
# *inside* the bubble at this position, not as a free-standing row above
# the text. Distinct from U+FFFC — treating those the same wrongly ate
# Bitmoji into the caption bubble.
_INLINE_MEDIA = "\uf00a"
# Max edge for media drawn inside a text bubble. Free-standing stickers
# stay larger; these share a line with words.
_INLINE_MEDIA_PX = 80

# Backstop reconciliation against events.jsonl.
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
# Written by `iphonebridge backup-sync` — history MAP can't provide.
_BACKUP_EVENTS_FILE = config.STATE_DIR / "backup_events.jsonl"

# How many conversations to show before the user scrolls for more.
_THREAD_PAGE = 40

_REACTION_ICON_FILES = {
    "Loved": "Heart.svg", "Liked": "ThumbsUp.svg", "Disliked": "ThumbsDown.svg",
    "Laughed at": "Haha.svg", "Emphasized": "Emphasize.svg",
    "Questioned": "Question.svg",
    "Removed a heart from": "Heart.svg",
    "Removed a like from": "ThumbsUp.svg",
    "Removed a dislike from": "ThumbsDown.svg",
    "Removed a laugh from": "Haha.svg",
    "Removed an exclamation from": "Emphasize.svg",
    "Removed a question mark from": "Question.svg",
}

# Empty tapback bubble, used as the backdrop for arbitrary-emoji reactions.
_PLACEHOLDER_ICON = "Placeholder.svg"


def _thread_key(ev: dict) -> str:
    return (ev.get("contact_name") or ev.get("sender_phone")
            or ev.get("sender_phone_norm") or "(unknown)")


def _file_url(path: str | Path | None) -> str:
    return f"file://{path}" if path else ""


def _inline_img_html(att: dict) -> str:
    """Qt rich-text `<img>` for media sitting inside a text bubble."""
    url = _file_url(att.get("path"))
    w = int(att.get("w") or 0)
    h = int(att.get("h") or 0)
    max_px = _INLINE_MEDIA_PX
    if w > 0 and h > 0:
        scale = min(max_px / max(w, 1), max_px / max(h, 1), 1.0)
        dw = max(1, round(w * scale))
        dh = max(1, round(h * scale))
    else:
        dw = dh = max_px
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
    if att.get("is_sticker"):
        return True
    return (att.get("mime") or "").lower().startswith("image/")


def _body_with_inline_media(
    body: str, atts: list[dict]
) -> tuple[str, str, bool, list[dict]]:
    """Split a message into bubble text and free-standing attachment rows.

    U+F00A marks true *inline* media (words and image in one bubble). Each
    slot claims the next image attachment; with a path it becomes an `<img>`,
    without one the glyph is just dropped until download finishes. U+FFFC is
    only the free-standing attachment marker (Bitmoji, peels) — those stay
    their own rows and the glyph is stripped from the caption. Returns
    `(plain_body, rich_body, jumbo, free_standing_atts)`.
    """
    body = body or ""
    # No inline slots: free-standing attachments keep their rows; just scrub
    # the attributed-string placeholder out of the caption.
    if _INLINE_MEDIA not in body:
        plain = body.replace(_OBJ_REPLACEMENT, "")
        rich, jumbo = body_markup(plain, _BODY_PX)
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
        rich, jumbo = body_markup(plain, _BODY_PX)
        return plain, rich, jumbo, free

    chunks: list[str] = []
    for kind, val in segments:
        if kind == "t":
            em, _ = body_markup(val, _BODY_PX)
            chunks.append(em if em else escape(val).replace("\n", "<br>"))
        else:
            chunks.append(_inline_img_html(val))
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
        """
        if self._IDENTITY is not None and len(rows) == len(self._rows):
            same = all(a.get(self._IDENTITY) == b.get(self._IDENTITY)
                       for a, b in zip(rows, self._rows, strict=True))
            if same:
                self._rows = rows
                if rows:
                    self.dataChanged.emit(
                        self.index(0, 0), self.index(len(rows) - 1, 0),
                        list(self._ROLES.keys()))
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

    _ROLES = {
        KeyRole: "threadKey",
        NameRole: "name",
        PreviewRole: "preview",
        StampRole: "stamp",
        UnreadRole: "unread",
        PinnedRole: "pinned",
        AvatarRole: "avatar",
        InitialsRole: "initials",
    }

    # Rows keep their identity across refreshes by thread key.
    _IDENTITY = "threadKey"

    def __init__(self, store: ThreadStore) -> None:
        super().__init__()
        self._store = store


class MessageListModel(_DictListModel):
    BodyRole = Qt.ItemDataRole.UserRole + 1
    OutgoingRole = Qt.ItemDataRole.UserRole + 2
    ReactionRole = Qt.ItemDataRole.UserRole + 3
    DividerRole = Qt.ItemDataRole.UserRole + 4
    ReactionEmojiRole = Qt.ItemDataRole.UserRole + 5
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

    _ROLES = {
        BodyRole: "body",
        OutgoingRole: "outgoing",
        ReactionRole: "reaction",
        DividerRole: "divider",
        ReactionEmojiRole: "reactionEmoji",
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

    def __init__(self, client, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._client = client
        self._threads: dict[str, dict] = {}
        self._current: str | None = None
        self._seen_handles: set[str] = set()
        self._contacts = ContactsResolver()
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
        # normalized phone -> thread key, so one person is one thread
        # regardless of what name each event happened to carry.
        self._by_phone: dict[str, str] = {}
        self._deleted: dict[str, str] = self._load_deleted()
        # thread key -> monotonic deadline after which the typing bubble
        # disappears on its own. See _TYPING_TIMEOUT_SEC.
        self._typing_until: dict[str, float] = {}
        # Threads whose message list needs re-sorting after a bulk import.
        self._unsorted: set[int] = set()
        # Conversations are paged in: with a full backup imported
        # there can be hundreds, and building every row up front
        # is wasted work for a list that shows a dozen.
        self._thread_limit = _THREAD_PAGE
        self._refresh_pending = QTimer(self)
        self._refresh_pending.setSingleShot(True)
        self._refresh_pending.setInterval(60)
        self._refresh_pending.timeout.connect(self._refresh_threads_now)

        # Exposed to QML through Property accessors below — plain Python
        # attributes on a QObject are invisible to QML.
        self._thread_model = ThreadListModel(self)
        self._message_model = MessageListModel()

        self._load_history()
        client.messageReceived.connect(self._on_signal)
        client.messageSent.connect(self._on_signal)
        client.messageStateChanged.connect(self._on_state_signal)

        # Belt and braces: D-Bus signals give instant updates, but if one is
        # ever missed (daemon restart, UI launched mid-flight) the view would
        # stay stale forever. The daemon appends every event to events.jsonl,
        # so watch that file and reconcile from it. _ingest() dedupes by
        # handle, which makes re-reading harmless.
        self._watcher = QFileSystemWatcher(self)
        self._watch_events_file()
        self._watcher.fileChanged.connect(self._sync_from_disk)
        self._watcher.directoryChanged.connect(self._sync_from_disk)

        # Final fallback in case the watcher misses an inotify event (some
        # editors/rotations replace the inode).
        self._sync_timer = QTimer(self)
        self._sync_timer.timeout.connect(self._sync_from_disk)
        self._sync_timer.start(_SYNC_INTERVAL_MS)

        # Expire stale typing indicators. Cheap, and it runs regardless of
        # whether a matching "stopped" ever arrives.
        self._typing_timer = QTimer(self)
        self._typing_timer.timeout.connect(self._expire_typing)
        self._typing_timer.start(2000)

    def _expire_typing(self) -> None:
        now = time.monotonic()
        stale = [k for k, deadline in self._typing_until.items() if deadline <= now]
        for key in stale:
            self._typing_until.pop(key, None)
        if stale and self._current in stale:
            self.typingChanged.emit()

    @Property(bool, notify=typingChanged)
    def peerTyping(self) -> bool:
        """Whether the open conversation's other party is typing."""
        deadline = self._typing_until.get(self._current or "")
        return deadline is not None and deadline > time.monotonic()

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
        """A `message_state` line from events.jsonl (post-restart rebuild)."""
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
        # Full rebuild rather than a role-only dataChanged: the caption can
        # move between bubbles, and partial updates were silently no-oping
        # for some list identities. Rebuild keeps scroll via SmoothListView's
        # own pin-to-bottom when already at the end.
        self._rebuild_messages()

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

    def _set_typing(self, handle: str, typing: bool) -> None:
        """Show/hide the typing bubble for whoever's conversation this is."""
        key = self._by_phone.get(normalize_phone(handle) or "") or ""
        if not key:
            # Fall back to the raw handle: iMessage addresses can be emails,
            # which normalize_phone leaves alone and _by_phone keys directly.
            key = self._by_phone.get(handle, "")
        if not key:
            return
        if typing:
            self._typing_until[key] = time.monotonic() + _TYPING_TIMEOUT_SEC
        else:
            self._typing_until.pop(key, None)
        if key == self._current:
            self.typingChanged.emit()

    def _watch_events_file(self) -> None:
        path = str(config.EVENTS_JSONL)
        if config.EVENTS_JSONL.exists() and path not in self._watcher.files():
            self._watcher.addPath(path)
        parent = str(config.EVENTS_JSONL.parent)
        if parent not in self._watcher.directories():
            self._watcher.addPath(parent)

    @Slot()
    def _sync_from_disk(self) -> None:
        # Re-adding is required after a rewrite: QFileSystemWatcher drops a
        # path once the inode it was watching goes away.
        self._watch_events_file()
        for ev in self._client.read_events(kinds={"sms_received", "sms_sent"}):
            self._ingest(ev, outgoing=(ev.get("kind") == "sms_sent"))
        for ev in self._client.read_events(kinds={"message_state"}):
            self._ingest_state(ev)

    # ---- QML-visible models ---------------------------------------------

    @Property(QObject, constant=True)
    def threadModel(self) -> QObject:
        return self._thread_model

    @Property(QObject, constant=True)
    def messageModel(self) -> QObject:
        return self._message_model

    @Slot()
    def loadMoreThreads(self) -> None:
        """Extend the visible conversation window (infinite scroll)."""
        total = len(self._threads)
        if self._thread_limit >= total:
            return
        self._thread_limit = min(total, self._thread_limit + _THREAD_PAGE)
        self._refresh_threads()

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
        thread["unread"] = sum(
            1 for m in thread["messages"]
            if not m["outgoing"] and ts_epoch(m.get("ts")) > ts_epoch(mark))

    def _mark_read(self, thread: dict) -> None:
        if thread["messages"]:
            self._read_marks[thread["key"]] = thread["messages"][-1].get("ts") or ""
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
        # Messages first, then state records. Delivery/edit lines only make
        # sense once the target guid exists; applying them out of order
        # would silently no-op on a cold start.
        for ev in self._client.read_events(kinds={"sms_received", "sms_sent"}):
            self._ingest(ev, outgoing=(ev.get("kind") == "sms_sent"),
                         refresh=False)
        for ev in self._client.read_events(kinds={"message_state"}):
            self._ingest_state(ev, refresh=False)
        # Derive unread from the persisted read marks rather than from the
        # live path, which would count the entire backlog as unread.
        # Merge anything a `backup-sync` produced: messages sent from the
        # phone, attachments, exact tapback targets — none of which MAP can
        # deliver. Keyed by GUID, so this is idempotent across syncs.
        self._load_backup_events()

        for thread in self._threads.values():
            self._recount_unread(thread)
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

        # Older events on disk predate reaction tagging — re-derive from the
        # body if the daemon didn't already stamp it.
        verb, snippet = ev.get("reaction_verb"), ev.get("reaction_snippet")
        if verb is None:
            verb, snippet = _detect_reaction(ev.get("body"))

        if verb:
            # A tapback isn't a message of its own — attach it to whichever
            # existing bubble it targets (matched via the quoted snippet,
            # the only link MAP gives us) instead of adding a new row.
            target = self._find_reaction_target(thread, snippet)
            if target is not None:
                target["reaction"] = verb
                if refresh and self._current == key:
                    self._rebuild_messages()
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
               "outgoing": outgoing, "reaction": None,
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
               "state": ""}
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

        Matches on the normalized phone number first. Keying purely on the
        display name split conversations in two whenever the name changed —
        MAP events carry whatever name was resolved when they arrived, so
        after a nickname was picked up the same person appeared twice, once
        as "quinton johnson" and once as "q".
        """
        # A group conversation is identified by the chat itself. Keying on
        # the sender would scatter each participant's messages into their
        # own 1:1 thread — which is exactly why group chats were showing up
        # as ordinary direct messages.
        #
        # `group_key` (the participant set) before `chat_guid`, because only
        # the former is comparable across transports: the backup's guid is a
        # phone-local sqlite id, so keying on it filed an imported group and
        # the same group arriving live as two unrelated conversations. Live
        # events have no separate `group_key` — their `chat_guid` is already
        # that string.
        chat_guid = ev.get("group_key") or ev.get("chat_guid")
        if chat_guid:
            thread = self._threads.get(chat_guid)
            if thread is None:
                thread = {"key": chat_guid, "name": ev.get("chat_name")
                          or "Group message", "phone": None,
                          "messages": [], "unread": 0, "is_group": True}
                self._threads[chat_guid] = thread
            elif ev.get("chat_name") and thread.get("name") in (
                    None, "", "Group message"):
                thread["name"] = ev["chat_name"]
            return chat_guid, thread

        norm = ev.get("sender_phone_norm") or normalize_phone(
            ev.get("sender_phone"))
        if norm:
            existing = self._by_phone.get(norm)
            if existing is not None and existing in self._threads:
                return existing, self._threads[existing]

        key = _thread_key(ev)
        thread = self._threads.get(key)
        if thread is None:
            thread = {"key": key, "name": key,
                      "phone": ev.get("sender_phone")
                      or ev.get("sender_phone_norm") or key,
                      "messages": [], "unread": 0}
            self._threads[key] = thread
        if norm:
            self._by_phone.setdefault(norm, key)
        return key, thread

    # ---- backup import ---------------------------------------------------

    def _load_backup_events(self) -> None:
        """Ingest `backup_events.jsonl` written by `iphonebridge backup-sync`."""
        path = _BACKUP_EVENTS_FILE
        if not path.exists():
            return
        added = 0
        try:
            for line in path.read_text(errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if self._ingest_backup(ev):
                    added += 1
        except OSError as e:
            log.warning("could not read %s: %s", path, e)
            return
        self._sort_pending()
        log.info("merged %d messages from backup", added)

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

        # A tapback isn't a message. Unlike the MAP path — which can only
        # guess the target by matching quoted text — the backup gives the
        # exact GUID of the message being reacted to.
        if ev.get("reaction_verb"):
            target_guid = ev.get("reaction_target_guid")
            target = self._by_guid.get(target_guid) if target_guid else None
            if target is not None:
                target["reaction"] = ev["reaction_verb"]
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
               "reaction": None, "attachments": attachments,
               # Both spellings, as in the live path: `im_reply_to_guid` is
               # the D-Bus name, `reply_to_guid` the one that persists. Taking
               # only one meant a reply stopped reading as a reply after a
               # restart.
               "guid": guid, "reply_to": (ev.get("reply_to_guid")
                            or ev.get("im_reply_to_guid") or ""),
               "sender_name": ev.get("sender_name") or "",
               "sender_phone": ev.get("sender_phone") or ""}
        thread["messages"].append(msg)
        self._note_message(thread, msg)
        # Sorting here would re-sort the whole thread on every single insert —
        # quadratic, and it made a full-history import (223k messages) peg a
        # core for minutes without finishing. Threads are sorted once at the
        # end of import_backup_events instead.
        self._unsorted.add(id(thread))
        if ts_epoch(ts) > ts_epoch(thread.get("last_ts")):
            thread["last_ts"] = ts
        if guid:
            self._by_guid[guid] = msg
        self._seen_handles.add(handle)
        return True

    def _reply_snippet(self, msg: dict) -> str:
        """Text of the message `msg` replies to, or "" if it isn't a reply.

        Replies were being stored with their target all along but never shown,
        so a reply rendered as an ordinary bubble — indistinguishable from
        replying not working.
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
        return (target.get("body") or "")[:120]

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
        return sorted(self._threads.values(),
                      key=lambda t: ts_epoch(t.get("last_ts")), reverse=True)

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
            last = t["messages"][-1]["body"] if t["messages"] else ""
            rows.append({
                "threadKey": t["key"],
                "name": self._display_name(t),
                "preview": last.replace("\n", " "),
                "stamp": relative_ts(t.get("last_ts")),
                "unread": int(t.get("unread", 0)),
                "pinned": t["key"] in self._pinned,
                "avatar": _file_url(circular_avatar(
                    self._contacts.resolve_photo(t.get("phone")))),
                "initials": self._initials(self._display_name(t)),
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
        """
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
        True inline media — marked by U+F00A in the body — is drawn inside
        the text bubble. U+FFFC is only scrubbed out of captions; those
        attachments stay free-standing.
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

        verb = msg.get("reaction")
        icon_file = _REACTION_ICON_FILES.get(verb)
        reaction, reaction_emoji = "", ""
        if icon_file:
            base = (_REACTION_ASSETS_OUTGOING if msg["outgoing"]
                    else _REACTION_ASSETS_INCOMING)
            reaction = _file_url(base / icon_file)
        elif verb and verb.startswith("Reacted "):
            # Arbitrary-emoji tapback (iOS 18+). No per-emoji asset exists,
            # so use the empty tapback bubble and draw the emoji inside it.
            reaction_emoji = verb[len("Reacted "):].strip()
            base = (_REACTION_ASSETS_OUTGOING if msg["outgoing"]
                    else _REACTION_ASSETS_INCOMING)
            reaction = _file_url(base / _PLACEHOLDER_ICON)

        outgoing = bool(msg["outgoing"])
        atts = msg.get("attachments") or []
        # U+F00A slots become `<img>` tags inside the bubble; free-standing
        # stickers/photos (and U+FFFC Bitmoji) still get their own rows.
        body, rich_body, jumbo, free_atts = _body_with_inline_media(
            msg.get("body") or "", atts)
        body = body.strip()
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

        def base_row(kind: str) -> dict:
            return {"kind": kind, "body": "", "outgoing": outgoing,
                    "reaction": "", "reactionEmoji": "", "divider": "",
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
                    "replyBody": self._reply_snippet(msg)}

        rows: list[dict] = []
        for att in free_atts:
            mime = (att.get("mime") or "").lower()
            name = att.get("name") or "Attachment"
            url = _file_url(att.get("path"))
            if att.get("is_sticker"):
                r = base_row("sticker")
                r["image"] = url
                r["imageW"] = int(att.get("w") or 0)
                r["imageH"] = int(att.get("h") or 0)
            elif mime.startswith("image/"):
                # HEIC included — Qt decodes it via libheif on this system.
                r = base_row("image")
                r["image"] = url
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
        rows[-1]["reaction"] = reaction
        rows[-1]["reactionEmoji"] = reaction_emoji
        return rows

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

    @Slot(str)
    def openThread(self, key: str) -> None:
        self._open(key, mark_read=True)

    def _open(self, key: str, *, mark_read: bool) -> None:
        self._current = key
        thread = self._threads.get(key)
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
