//! `ib-imessage` — the iMessage transport for iphonebridge.
//!
//! iphonebridge's other transports read the phone over Bluetooth, which caps
//! what's possible: MAP delivers incoming text and nothing else — no messages
//! you sent, no attachments, no real tapback targets, no reply threading, and
//! no way to send anything richer than plain text. This helper removes that
//! ceiling by speaking to Apple directly, as the Apple ID itself, using the
//! registration OpenBubbles established.
//!
//! It exists as a separate process rather than a Python extension because
//! rustpush is the only workable iMessage implementation and it is Rust and
//! deeply async. Talking to it over a socket keeps the daemon free of a
//! native-extension build step, and keeps a panic in Apple-protocol parsing
//! from taking the UI down with it.
//!
//! ## Protocol
//!
//! Line-delimited JSON both ways on a unix socket; one line is one object.
//!
//! Requests carry an `id` echoed back on the reply:
//!     {"id":"1","cmd":"send","chat":{...},"text":"hi"}
//! Replies are `{"id":"1","ok":true,...}` or `{"id":"1","ok":false,"error":...}`.
//! Unsolicited events have no `id` and carry `{"event":"..."}`.
//!
//! Events embed rustpush's `MessageInst` verbatim under `inst`. That is a
//! deliberate choice: the Python side already has to understand iMessage
//! semantics, and re-projecting every message type here would silently drop
//! whatever rustpush learns to parse next.
//!
//! ## One connection at a time
//!
//! APNs permits a single live connection per push token, and this helper uses
//! the *same* token as OpenBubbles. Running both at once makes them displace
//! each other in a loop. The daemon is responsible for not doing that; see
//! `SINGLETON_NOTE` below.

mod migrate;
mod state;

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Arc;

use anyhow::{anyhow, bail, Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value as Json};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::{UnixListener, UnixStream};
use tokio::sync::{broadcast, Mutex};

use rustpush::{
    APSConnectionResource, ConversationData, EditMessage, IMClient, Message, MessageInst,
    IndexedMessagePart, MessagePart, MessageParts, MessageType, NormalMessage, ReactMessage,
    ReactMessageType, Reaction, RenameMessage, UnsendMessage, MADRID_SERVICE,
};

use state::{registration_age, StateDir};

/// Why the daemon must serialise access: see the module docs. Kept as a
/// constant so the reason travels with the code that would break.
const SINGLETON_NOTE: &str =
    "APNs allows one connection per push token; OpenBubbles must be closed \
     while this helper is running, or the two will fight over the token.";

/// Warn once a registration is this old. Apple's IDS certs run about a month;
/// warning at three weeks leaves time to re-register via OpenBubbles before
/// sending starts failing.
const REGISTRATION_WARN_SECS: u64 = 21 * 24 * 60 * 60;

// ---------------------------------------------------------------------------
// wire types
// ---------------------------------------------------------------------------

/// A conversation, as the Python side describes it.
///
/// `participants` is the identity of a chat in iMessage — there is no server
/// side chat id to quote back. For a group, `guid` pins the specific thread
/// so a rename or a participant change lands on the right one.
#[derive(Deserialize, Clone)]
struct ChatRef {
    participants: Vec<String>,
    #[serde(default)]
    name: Option<String>,
    #[serde(default)]
    guid: Option<String>,
    /// Newest known message in the thread. iMessage uses it to order
    /// messages; omitting it puts the message at the end, which is usually
    /// what we want.
    #[serde(default)]
    after_guid: Option<String>,
}

impl From<ChatRef> for ConversationData {
    fn from(c: ChatRef) -> Self {
        ConversationData {
            participants: c.participants,
            cv_name: c.name,
            sender_guid: c.guid,
            after_guid: c.after_guid,
        }
    }
}

#[derive(Deserialize)]
#[serde(tag = "cmd", rename_all = "snake_case")]
enum Command {
    /// Liveness plus registration health, so the UI can show why sending is
    /// about to break rather than only that it broke.
    Status,
    /// Our own registered handles — the Python side needs them to tell an
    /// outgoing message from an incoming one.
    Handles,
    Send {
        chat: ChatRef,
        text: String,
        #[serde(default)]
        subject: Option<String>,
        /// Guid of the message being replied to, for real reply threading.
        #[serde(default)]
        reply_guid: Option<String>,
        #[serde(default)]
        reply_part: Option<String>,
        /// e.g. "invisibleink", "slam" — an iMessage screen/bubble effect.
        #[serde(default)]
        effect: Option<String>,
    },
    /// Fetch one attachment's bytes to a local file.
    ///
    /// Named by the message guid and the attachment's position within it,
    /// rather than by handing the attachment object back.
    ///
    /// The object cannot make the round trip: its MMCS locator and key are
    /// `Vec<u8>` fields serialized as plist `Data`, and JSON renders those as
    /// a sequence of numbers, which `bin_deserialize` rejects — "invalid
    /// type: sequence, expected a byte array". So the helper keeps the
    /// attachments of recent messages instead, and the caller refers to them.
    DownloadAttachment {
        guid: String,
        /// Index among that message's attachments, in the order they appear
        /// in its parts — the same order the Python side enumerates them.
        index: usize,
        /// Absolute path to write. The caller owns naming and cleanup.
        dest: String,
    },
    /// Add or remove a tapback. `emoji` selects an arbitrary-emoji tapback
    /// (iOS 18+); otherwise `kind` picks one of the six classic ones.
    React {
        chat: ChatRef,
        target_guid: String,
        /// Text of the target message. iMessage carries it in the tapback so
        /// recipients can render 'Loved "…"' without a lookup.
        #[serde(default)]
        target_text: String,
        #[serde(default)]
        target_part: Option<u64>,
        #[serde(default)]
        kind: Option<String>,
        #[serde(default)]
        emoji: Option<String>,
        #[serde(default = "yes")]
        enable: bool,
    },
    Edit {
        chat: ChatRef,
        target_guid: String,
        #[serde(default)]
        part: u64,
        text: String,
    },
    Unsend {
        chat: ChatRef,
        target_guid: String,
        #[serde(default)]
        part: u64,
    },
    Typing {
        chat: ChatRef,
        typing: bool,
    },
    /// Mark the thread read on every other device on the account.
    MarkRead {
        chat: ChatRef,
    },
    Rename {
        chat: ChatRef,
        name: String,
    },
    /// Replaces the participant list wholesale, which is how iMessage models
    /// both adding and removing.
    SetParticipants {
        chat: ChatRef,
        participants: Vec<String>,
        #[serde(default)]
        group_version: u64,
    },
}

fn yes() -> bool {
    true
}

/// A single unformatted text part.
///
/// `MessageParts::from_raw` does exactly this but is private, and the parts
/// vector is how iMessage represents a message body: one entry per run of
/// text, attachment or mention. An edit replaces one part, which is why edits
/// need this rather than a bare string.
fn plain_text(text: &str) -> MessageParts {
    MessageParts(vec![IndexedMessagePart {
        part: MessagePart::Text(text.to_string(), Default::default()),
        idx: None,
        ext: None,
    }])
}

#[derive(Serialize)]
struct Reply {
    id: Option<String>,
    ok: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<String>,
    #[serde(flatten)]
    data: Json,
}

// ---------------------------------------------------------------------------
// session
// ---------------------------------------------------------------------------

/// Recent messages' attachments, oldest evicted first.
///
/// Sized for "long enough that the daemon's download, which starts the moment
/// the message is published, still finds it" — not for history. A miss is
/// reported rather than papered over, because the alternative is a download
/// that silently produces nothing.
#[derive(Default)]
struct AttachmentCache {
    by_guid: HashMap<String, Vec<rustpush::Attachment>>,
    order: std::collections::VecDeque<String>,
}

impl AttachmentCache {
    const LIMIT: usize = 256;

    fn insert(&mut self, guid: String, atts: Vec<rustpush::Attachment>) {
        if self.by_guid.contains_key(&guid) {
            return;
        }
        while self.order.len() >= Self::LIMIT {
            if let Some(old) = self.order.pop_front() {
                self.by_guid.remove(&old);
            }
        }
        self.order.push_back(guid.clone());
        self.by_guid.insert(guid, atts);
    }

    fn get(&self, guid: &str) -> Option<&Vec<rustpush::Attachment>> {
        self.by_guid.get(guid)
    }
}

/// The attachments of a message, in the order they appear among its parts —
/// which is the order the Python side enumerates them, and therefore what the
/// `index` on `DownloadAttachment` refers to.
fn attachments_of(inst: &MessageInst) -> Vec<rustpush::Attachment> {
    match &inst.message {
        Message::Message(normal) => normal
            .parts
            .0
            .iter()
            .filter_map(|p| match &p.part {
                rustpush::MessagePart::Attachment(a) => Some(a.clone()),
                _ => None,
            })
            .collect(),
        _ => Vec::new(),
    }
}

struct Session {
    client: IMClient,
    /// Kept alongside the client because attachment downloads need the APNs
    /// connection directly — `Attachment::get_attachment` talks to MMCS
    /// rather than going through `IMClient`.
    conn: rustpush::APSConnection,
    /// Attachments of recently published messages, by guid.
    ///
    /// Exists because the attachment object cannot survive a JSON round trip
    /// (see `DownloadAttachment`), so the daemon refers to one by position
    /// instead of holding it. Bounded: the helper is long-lived and every
    /// photo that arrives would otherwise be retained for good.
    attachments: Mutex<AttachmentCache>,
    handles: Vec<String>,
    /// Broadcast so every connected client sees every event; the daemon and a
    /// debugging `socat` can watch at the same time.
    events: broadcast::Sender<String>,
    /// Sends mutate shared identity state, so they go one at a time.
    send_lock: Mutex<()>,
    warnings: Vec<String>,
    /// Optional override for the sending identity; see `my_handle`.
    send_as: Option<String>,
}

impl Session {
    /// The handle we send as.
    ///
    /// Prefers the phone number over any email address. This is not cosmetic:
    /// the recipient's phone threads incoming messages by the address they
    /// came from, so sending as an email starts a *second* conversation
    /// alongside the one their iPhone already has with your number — the same
    /// person appearing twice on their device.
    ///
    /// Registration lists emails first, so taking `handles.first()` picked an
    /// email and did exactly that.
    ///
    /// `--send-as` overrides this when the registration has several numbers,
    /// or when sending from an email really is what's wanted.
    fn my_handle(&self) -> Result<&str> {
        if let Some(preferred) = self.send_as.as_deref() {
            if let Some(found) = self.handles.iter().find(|h| h.as_str() == preferred) {
                return Ok(found.as_str());
            }
            log::warn!(
                "--send-as {preferred} is not a registered handle; \
                 falling back to the default"
            );
        }
        self.handles
            .iter()
            .find(|h| h.starts_with("tel:"))
            .or_else(|| self.handles.first())
            .map(|s| s.as_str())
            .ok_or_else(|| anyhow!("no registered handles — registration has expired"))
    }

    async fn dispatch(&self, cmd: Command) -> Result<Json> {
        match cmd {
            Command::Status => Ok(json!({
                "handles": self.handles,
                // Which identity messages actually go out as. Surfaced
                // because sending from the wrong one silently splits the
                // conversation on the recipient's phone, and there is no
                // other way to notice from this side.
                "send_as": self.my_handle().unwrap_or("<none>"),
                "warnings": self.warnings,
                "note": SINGLETON_NOTE,
            })),
            Command::Handles => Ok(json!({ "handles": self.handles })),

            Command::Send {
                chat,
                text,
                subject,
                reply_guid,
                reply_part,
                effect,
            } => {
                let mut normal = NormalMessage::new(text, MessageType::IMessage);
                normal.subject = subject;
                // rustpush builds the reply field as `r:<part>:<guid>` and
                // unwraps `reply_part` unconditionally whenever `reply_guid`
                // is set (messages.rs:2261). Passing a guid without a part
                // therefore panics the worker task, and because the panic
                // kills the task before it can answer, the caller sees a
                // D-Bus timeout rather than an error. Default the part so
                // that combination can't be constructed here.
                if reply_guid.is_some() {
                    normal.reply_part = Some(reply_part.unwrap_or_else(|| "0".to_string()));
                } else {
                    normal.reply_part = None;
                }
                normal.reply_guid = reply_guid;
                normal.effect = effect;
                self.send(chat, Message::Message(normal)).await
            }

            Command::React {
                chat,
                target_guid,
                target_text,
                target_part,
                kind,
                emoji,
                enable,
            } => {
                // Case-folded: the verb names are also what iMessage shows in
                // its menu ("Heart", "Like"), so callers naturally send them
                // capitalised. Matching only lowercase rejected every tapback
                // the UI sent, with the error arriving too late to be visible.
                let kind = kind.map(|k| k.to_ascii_lowercase());
                let reaction = match (emoji, kind.as_deref()) {
                    (Some(e), _) => Reaction::Emoji(e),
                    (None, Some("heart") | Some("love") | Some("loved")) => Reaction::Heart,
                    (None, Some("like") | Some("liked")) => Reaction::Like,
                    (None, Some("dislike") | Some("disliked")) => Reaction::Dislike,
                    (None, Some("laugh") | Some("laughed")) => Reaction::Laugh,
                    (None, Some("emphasize") | Some("emphasized")) => Reaction::Emphasize,
                    (None, Some("question") | Some("questioned")) => Reaction::Question,
                    (None, other) => bail!(
                        "unknown tapback {:?}; expected one of heart/like/dislike/\
                         laugh/emphasize/question, or an `emoji`",
                        other.unwrap_or("<none>")
                    ),
                };
                let react = ReactMessage {
                    to_uuid: target_guid,
                    to_part: target_part,
                    reaction: ReactMessageType::React { reaction, enable },
                    to_text: target_text,
                    embedded_profile: None,
                };
                self.send(chat, Message::React(react)).await
            }

            Command::Edit {
                chat,
                target_guid,
                part,
                text,
            } => {
                self.send(
                    chat,
                    Message::Edit(EditMessage {
                        tuuid: target_guid,
                        edit_part: part,
                        new_parts: plain_text(&text),
                    }),
                )
                .await
            }

            Command::Unsend {
                chat,
                target_guid,
                part,
            } => {
                self.send(
                    chat,
                    Message::Unsend(UnsendMessage {
                        tuuid: target_guid,
                        edit_part: part,
                    }),
                )
                .await
            }

            Command::Typing { chat, typing } => {
                // The `Option<TypingApp>` names a third-party iMessage app
                // that's driving the indicator. None means Messages itself,
                // which is what we want to look like.
                self.send(chat, Message::Typing(typing, None)).await
            }

            Command::MarkRead { chat } => self.send(chat, Message::Read).await,

            Command::Rename { chat, name } => {
                self.send(chat, Message::RenameMessage(RenameMessage { new_name: name }))
                    .await
            }

            Command::DownloadAttachment { guid, index, dest } => {
                let attachment = {
                    let cache = self.attachments.lock().await;
                    cache
                        .get(&guid)
                        .and_then(|v| v.get(index))
                        .cloned()
                        .ok_or_else(|| {
                            anyhow!(
                                "no attachment {index} cached for {guid}; the \
                                 message is older than the helper or has been \
                                 evicted"
                            )
                        })?
                };
                let path = PathBuf::from(&dest);
                // Written through a temp file in the same directory and then
                // renamed: the daemon treats "the file exists" as "the image
                // is ready", so a half-written file would be handed to the UI
                // as a truncated, undecodable image.
                let tmp = path.with_extension("part");
                if let Some(parent) = path.parent() {
                    std::fs::create_dir_all(parent)
                        .with_context(|| format!("creating {}", parent.display()))?;
                }
                let size = {
                    let mut file = std::fs::File::create(&tmp)
                        .with_context(|| format!("creating {}", tmp.display()))?;
                    attachment
                        .get_attachment(&self.conn, &mut file, |_, _| {})
                        .await
                        .context("downloading attachment from MMCS")?;
                    file.metadata().map(|m| m.len()).unwrap_or(0)
                };
                std::fs::rename(&tmp, &path)
                    .with_context(|| format!("renaming into {}", path.display()))?;
                Ok(json!({ "path": dest, "bytes": size }))
            }

            Command::SetParticipants {
                chat,
                participants,
                group_version,
            } => {
                self.send(
                    chat,
                    Message::ChangeParticipants(rustpush::ChangeParticipantMessage {
                        new_participants: participants,
                        group_version,
                    }),
                )
                .await
            }
        }
    }

    async fn send(&self, chat: ChatRef, message: Message) -> Result<Json> {
        let _guard = self.send_lock.lock().await;
        let conversation: ConversationData = chat.into();
        // `IMClient::to_message` doesn't exist for outbound traffic — that
        // helper hangs off `IDSRecvMessage`, for building a reply in the
        // context of something received. Composing fresh means constructing
        // the instance ourselves and naming the handle we're sending as.
        let mut inst = MessageInst::new(conversation, self.my_handle()?, message);
        // Report the guid before waiting on delivery: the UI wants to show the
        // bubble immediately, and per-recipient delivery is asynchronous.
        let guid = inst.id.clone();
        self.client
            .send(&mut inst)
            .await
            .context("sending to Apple")?;
        Ok(json!({ "guid": guid }))
    }
}

// ---------------------------------------------------------------------------
// event pump
// ---------------------------------------------------------------------------

/// Feed every APNs message through the client and publish what comes out.
///
/// `IMClient::handle` returns `Ok(None)` constantly — most APNs traffic is
/// keepalives and protocol chatter for other topics — so only real messages
/// are forwarded.
async fn pump(session: Arc<Session>, conn: rustpush::APSConnection) {
    let mut rx = conn.subscribe().await;
    loop {
        let aps = match rx.recv().await {
            Ok(m) => m,
            // Lagging means we fell behind a burst, not that we're done; the
            // alternative is exiting and losing the connection entirely.
            Err(broadcast::error::RecvError::Lagged(n)) => {
                log::warn!("dropped {n} APNs messages while catching up");
                continue;
            }
            Err(broadcast::error::RecvError::Closed) => {
                // Apple resets the APNs socket from time to time (seen as
                // "Connection reset by peer" from rustpush::aps). When the
                // broadcast channel closes with it, this pump is finished —
                // and simply returning left the helper *alive and serving*
                // with no connection behind it: sends still worked, the unix
                // socket stayed up, `IMessageStatus` still reported
                // available, and nothing inbound ever arrived again. Messages
                // sent from the iPhone stopped appearing on the desktop for
                // hours with nothing in the daemon's log to say why.
                //
                // Exit instead. The connection cannot be rebuilt in place —
                // it is owned by the client that was constructed around it at
                // startup — and the unit is Restart=always, so systemd brings
                // us back with a fresh one. The daemon sees the socket close,
                // drops the transport, and reconnects on its own retry timer.
                log::error!("APNs connection closed — exiting so the unit restarts us");
                let _ = session
                    .events
                    .send(json!({"event": "disconnected"}).to_string());
                // Long enough for the writer tasks to flush that event to any
                // connected client; the socket closing is the real signal, so
                // this is a courtesy rather than a guarantee.
                tokio::time::sleep(std::time::Duration::from_millis(200)).await;
                std::process::exit(1);
            }
        };
        match session.client.handle(aps).await {
            Ok(Some(inst)) => publish(&session, inst),
            Ok(None) => {}
            Err(e) => {
                log::warn!("could not parse an inbound message: {e}");
                let _ = session
                    .events
                    .send(json!({"event": "parse_error", "error": e.to_string()}).to_string());
            }
        }
    }
}

fn publish(session: &Session, inst: MessageInst) {
    // Remembered before the event goes out, so the download the daemon starts
    // on receiving it can't lose a race with this.
    let atts = attachments_of(&inst);
    if !atts.is_empty() {
        if let Ok(mut cache) = session.attachments.try_lock() {
            cache.insert(inst.id.clone(), atts);
        } else {
            log::warn!("attachment cache busy; {} may not be fetchable", inst.id);
        }
    }
    match serde_json::to_value(&inst) {
        Ok(inst_json) => {
            let line = json!({"event": "message", "inst": inst_json}).to_string();
            // An error here only means nobody is listening yet.
            let _ = session.events.send(line);
        }
        Err(e) => log::warn!("could not serialise message {}: {e}", inst.id),
    }
}

// ---------------------------------------------------------------------------
// socket
// ---------------------------------------------------------------------------

async fn serve(session: Arc<Session>, listener: UnixListener) {
    loop {
        match listener.accept().await {
            Ok((stream, _)) => {
                let session = session.clone();
                tokio::spawn(async move {
                    if let Err(e) = handle_client(session, stream).await {
                        log::debug!("client disconnected: {e}");
                    }
                });
            }
            Err(e) => {
                log::error!("accept failed: {e}");
                return;
            }
        }
    }
}

async fn handle_client(session: Arc<Session>, stream: UnixStream) -> Result<()> {
    let (read_half, mut write_half) = stream.into_split();
    let mut lines = BufReader::new(read_half).lines();

    // Push events to this client for as long as it stays connected.
    let mut events = session.events.subscribe();
    let (mut out_tx, mut out_rx) = tokio::sync::mpsc::channel::<String>(256);
    let writer = tokio::spawn(async move {
        while let Some(line) = out_rx.recv().await {
            if write_half.write_all(line.as_bytes()).await.is_err() {
                break;
            }
            if write_half.write_all(b"\n").await.is_err() {
                break;
            }
        }
    });

    let event_tx = out_tx.clone();
    let events_task = tokio::spawn(async move {
        loop {
            match events.recv().await {
                Ok(line) => {
                    if event_tx.send(line).await.is_err() {
                        break;
                    }
                }
                Err(broadcast::error::RecvError::Lagged(_)) => continue,
                Err(broadcast::error::RecvError::Closed) => break,
            }
        }
    });

    while let Some(line) = lines.next_line().await? {
        let line = line.trim().to_string();
        if line.is_empty() {
            continue;
        }
        // The request id is echoed even when the body is unparseable, so a
        // caller waiting on a specific id never hangs.
        let id = serde_json::from_str::<Json>(&line)
            .ok()
            .and_then(|v| v.get("id").and_then(|i| i.as_str().map(str::to_string)));

        let reply = match serde_json::from_str::<Command>(&line) {
            Ok(cmd) => match session.dispatch(cmd).await {
                Ok(data) => Reply { id, ok: true, error: None, data },
                Err(e) => Reply {
                    id,
                    ok: false,
                    // The chain matters here: "sending to Apple: …" alone
                    // doesn't say which of a dozen causes it was.
                    error: Some(format!("{e:#}")),
                    data: Json::Null,
                },
            },
            Err(e) => Reply {
                id,
                ok: false,
                error: Some(format!("bad request: {e}")),
                data: Json::Null,
            },
        };
        if out_tx
            .send(serde_json::to_string(&reply)?)
            .await
            .is_err()
        {
            break;
        }
    }

    events_task.abort();
    drop(out_tx);
    let _ = writer.await;
    Ok(())
}

// ---------------------------------------------------------------------------
// startup
// ---------------------------------------------------------------------------

struct Args {
    state_dir: PathBuf,
    /// Not required with `--import-only`, which never binds.
    socket: Option<PathBuf>,
    import_from: Option<PathBuf>,
    /// Copy the registration in and exit. This is the monthly renewal path:
    /// OpenBubbles re-registers, we re-import, the service restarts.
    import_only: bool,
    /// Send as this handle rather than the default. Full form, e.g.
    /// `tel:+12155550100` or `mailto:you@icloud.com`.
    send_as: Option<String>,
}

fn parse_args() -> Result<Args> {
    let mut state_dir = None;
    let mut socket = None;
    let mut import_from = None;
    let mut import_only = false;
    let mut send_as = None;
    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--state-dir" => state_dir = args.next().map(PathBuf::from),
            "--socket" => socket = args.next().map(PathBuf::from),
            "--import-from" => import_from = args.next().map(PathBuf::from),
            "--import-only" => import_only = true,
            "--send-as" => send_as = args.next(),
            "--help" | "-h" => {
                println!(
                    "ib-imessage --state-dir DIR --socket PATH \
                     [--import-from OPENBUBBLES_DIR] [--import-only] \
                     [--send-as HANDLE]\n\n\
                     --send-as picks the identity messages are sent from, \
                     e.g. tel:+12155550100. Defaults to the registered phone \
                     number, because sending from an email address starts a \
                     second conversation on the recipient's phone.\n\n\
                     --import-only copies the registration and exits without \
                     connecting; use it to adopt a renewed registration.\n\n\
                     {SINGLETON_NOTE}"
                );
                std::process::exit(0);
            }
            other => bail!("unknown argument {other}"),
        }
    }
    if import_only && import_from.is_none() {
        bail!("--import-only needs --import-from");
    }
    Ok(Args {
        state_dir: state_dir.context("--state-dir is required")?,
        // Only the serving path needs somewhere to listen.
        socket: match (socket, import_only) {
            (Some(s), _) => Some(s),
            (None, true) => None,
            (None, false) => bail!("--socket is required"),
        },
        import_from,
        import_only,
        send_as,
    })
}

#[tokio::main]
async fn main() -> Result<()> {
    pretty_env_logger::init_timed();
    let args = parse_args()?;

    let dir = StateDir::new(&args.state_dir);
    if let Some(src) = &args.import_from {
        dir.import_from(src)
            .with_context(|| format!("importing registration from {}", src.display()))?;
        log::info!("imported registration from {}", src.display());
    }
    if args.import_only {
        // Load once before declaring success: a copied-but-unloadable
        // registration would otherwise only fail at the next service start,
        // long after the person doing the renewal has walked away.
        let loaded = dir.load().context("verifying the imported registration")?;
        let handles: usize = loaded
            .users
            .iter()
            .filter_map(|u| u.registration.get("com.apple.madrid"))
            .map(|r| r.handles.len())
            .sum();
        log::info!("registration verified: {handles} handle(s) registered");
        return Ok(());
    }
    if !dir.is_seeded() {
        bail!(
            "{} has no registration; run once with --import-from \
             ~/.var/app/app.openbubbles.OpenBubbles/data/bluebubbles",
            args.state_dir.display()
        );
    }

    let loaded = dir.load().context("loading registration")?;

    // Surface staleness at startup rather than as a send failure later.
    let mut warnings = Vec::new();
    let ages = registration_age(&loaded.users);
    for (service, age) in &ages {
        if *age > REGISTRATION_WARN_SECS {
            warnings.push(format!(
                "{service} was registered {} days ago and will expire; open \
                 OpenBubbles to renew, then re-import",
                age / 86_400
            ));
        }
    }
    for w in &warnings {
        log::warn!("{w}");
    }

    let os_config = Arc::new(loaded.os_config);
    let (conn, err) = APSConnectionResource::new(os_config.clone(), Some(loaded.aps)).await;
    if let Some(e) = err {
        // Not fatal on its own: rustpush reconnects, and a transient DNS or
        // network failure at startup shouldn't stop the helper from coming up.
        log::warn!("APNs connect reported: {e}");
    }

    let save_dir = StateDir::new(&args.state_dir);
    let client = IMClient::new(
        conn.clone(),
        loaded.users.clone(),
        loaded.identity,
        &[&MADRID_SERVICE],
        loaded.dir.join("id_cache.plist"),
        os_config.clone(),
        Box::new(move |users| {
            // rustpush hands back refreshed IDS keys; losing these means the
            // next start re-registers, which this build cannot do.
            if let Err(e) = save_dir.save_users(&users) {
                log::error!("could not persist refreshed IDS keys: {e}");
            }
        }),
    )
    .await;

    let handles: Vec<String> = loaded
        .users
        .iter()
        .flat_map(|u| {
            u.registration
                .get("com.apple.madrid")
                .map(|r| r.handles.clone())
                .unwrap_or_default()
        })
        .collect();
    if handles.is_empty() {
        bail!(
            "no handles registered for com.apple.madrid — OpenBubbles has not \
             finished registering, or the registration lapsed"
        );
    }
    log::info!("registered handles: {}", handles.join(", "));

    let (events, _) = broadcast::channel(1024);
    let session = Arc::new(Session {
        client,
        conn: conn.clone(),
        attachments: Mutex::new(AttachmentCache::default()),
        handles,
        events,
        send_lock: Mutex::new(()),
        warnings,
        send_as: args.send_as.clone(),
    });
    // Worth a line of its own: if this is an email address, every message
    // will open a second thread beside the one the recipient's phone already
    // has with your number.
    match session.my_handle() {
        Ok(h) => log::info!("sending as {h}"),
        Err(e) => log::warn!("no sending identity: {e}"),
    }

    // A leftover socket from a crash would otherwise make bind fail forever.
    // Unwrapped here rather than earlier: --import-only returns above and
    // legitimately has no socket.
    let socket_path = args
        .socket
        .as_ref()
        .expect("socket is required unless --import-only");
    // A unix socket's inode outlives the process that bound it, so a crash or
    // a SIGKILL leaves a file that makes bind() fail with EADDRINUSE forever.
    // Unlink unconditionally rather than testing exists() first: the check is
    // racy, and a removal failure must be reported, not swallowed — silently
    // ignoring it turns a read-only or wrong-owner runtime directory into a
    // baffling "address already in use".
    match std::fs::remove_file(socket_path) {
        Ok(()) => log::debug!("removed stale socket {}", socket_path.display()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        Err(e) => {
            return Err(anyhow::Error::new(e).context(format!(
                "could not remove stale socket {}",
                socket_path.display()
            )))
        }
    }
    if let Some(parent) = socket_path.parent() {
        std::fs::create_dir_all(parent).ok();
    }
    let listener = UnixListener::bind(socket_path)
        .with_context(|| format!("binding {}", socket_path.display()))?;
    {
        // The socket sends messages as the user's Apple ID; nobody else on the
        // machine should be able to.
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(socket_path, std::fs::Permissions::from_mode(0o600))?;
    }
    log::info!("listening on {}", socket_path.display());

    tokio::spawn(pump(session.clone(), conn));
    serve(session, listener).await;
    Ok(())
}

// Silence an unused-import warning when the map type is only used indirectly.
#[allow(dead_code)]
type _Ages = HashMap<String, u64>;
