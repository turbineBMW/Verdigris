use crate::{api::Api, config::Config, store::Store};
use anyhow::{Context, Result};
use futures_util::{FutureExt, StreamExt};
use std::sync::Arc;
use tokio::sync::{Mutex, Notify, watch};

pub const NAME: &str = "dev.turbinebmw.Verdigris.Sync";
pub const PATH: &str = "/dev/turbinebmw/Verdigris/Sync";
pub const IFACE: &str = "dev.turbinebmw.Verdigris.Sync1";
pub const BRIDGE_NAME: &str = "dev.turbinebmw.Verdigris.Bridge";
pub const BRIDGE_PATH: &str = "/dev/turbinebmw/Verdigris/Bridge";

#[derive(Default)]
pub struct Engine {
    lock: Mutex<()>,
    media_lock: Mutex<()>,
    pub wake: Notify,
    reload: Notify,
    status: Mutex<String>,
    changes: watch::Sender<u64>,
}
impl Engine {
    async fn api(&self) -> Result<Api> {
        let config = Config::load()?;
        if config.server.is_empty() {
            anyhow::bail!("Connect your Mac in Settings");
        }
        let password = config.password().await?;
        Api::new(config, password)
    }
    fn changed(&self) {
        self.changes.send_modify(|v| *v = v.wrapping_add(1));
    }
    pub async fn sync(&self) -> Result<()> {
        let _guard = self.lock.lock().await;
        let result = self.sync_inner().await;
        *self.status.lock().await = match &result {
            Ok(_) => "Up to date".into(),
            Err(e) => e.to_string(),
        };
        self.changed();
        result
    }
    async fn sync_inner(&self) -> Result<()> {
        let api = self.api().await?;
        let mut store = Store::open(&api.config.server)?;
        sync_into(&api, &mut store).await
    }
}

pub async fn sync_into(api: &Api, store: &mut Store) -> Result<()> {
    let chats = api.chats().await?;
    let after = store.watermark().map(|t| (t - 300_000).max(0));
    let mut messages = Vec::new();
    // First connection seeds recent history; each conversation can load older pages.
    // Subsequent catch-ups page through the entire gap, not just the last 100.
    loop {
        let page = api
            .messages(
                None,
                after,
                None,
                messages.len(),
                100,
                if after.is_some() { "ASC" } else { "DESC" },
            )
            .await?;
        let done = page.len() < 100 || after.is_none();
        messages.extend(page);
        if done {
            break;
        }
    }
    let mark = messages.iter().filter_map(|m| m.date_created).max();
    // Save the cursor only with the complete batch. A failed page leaves the old cursor intact.
    store.save(&chats, &messages, None, mark)?;
    Ok(())
}

pub struct SyncService(pub Arc<Engine>);
fn failure(e: impl std::fmt::Display) -> zbus::fdo::Error {
    zbus::fdo::Error::Failed(e.to_string())
}

#[zbus::interface(name = "dev.turbinebmw.Verdigris.Sync1")]
impl SyncService {
    async fn refresh(&self) {
        self.0.wake.notify_one();
    }
    async fn reload(&self) {
        self.0.reload.notify_one();
        self.0.wake.notify_one();
        self.0.changed();
    }
    async fn status(&self) -> String {
        self.0.status.lock().await.clone()
    }
    async fn snapshot(
        &self,
        chat: &str,
        limit: u32,
    ) -> zbus::fdo::Result<(String, String, String, String)> {
        let config = Config::load().map_err(failure)?;
        let store = Store::open(&config.server).map_err(failure)?;
        let chats = serde_json::to_string(&store.chats().map_err(failure)?).map_err(failure)?;
        let messages = serde_json::to_string(
            &store
                .messages(chat, limit.min(10000) as usize)
                .map_err(failure)?,
        )
        .map_err(failure)?;
        let status = self.0.status.lock().await.clone();
        Ok((config.server, chats, messages, status))
    }
    async fn chats(&self) -> zbus::fdo::Result<String> {
        let config = Config::load().map_err(failure)?;
        let rows = Store::open(&config.server)
            .and_then(|s| s.chats())
            .map_err(failure)?;
        serde_json::to_string(&rows).map_err(failure)
    }
    async fn messages(&self, chat: &str, limit: u32) -> zbus::fdo::Result<String> {
        let config = Config::load().map_err(failure)?;
        let rows = Store::open(&config.server)
            .and_then(|s| s.messages(chat, limit.min(10000) as usize))
            .map_err(failure)?;
        serde_json::to_string(&rows).map_err(failure)
    }
    async fn fetch_thread(&self, chat: &str, limit: u32) -> zbus::fdo::Result<()> {
        let _guard = self.0.lock.lock().await;
        let api = self.0.api().await.map_err(failure)?;
        // Re-query from the newest message, so deliveries/read changes are reconciled too.
        let messages = api
            .messages(
                Some(chat),
                None,
                None,
                0,
                limit.clamp(100, 1000) as usize,
                "DESC",
            )
            .await
            .map_err(failure)?;
        Store::open(&api.config.server)
            .and_then(|mut s| s.save(&[], &messages, Some(chat), None))
            .map_err(failure)?;
        self.0.changed();
        Ok(())
    }
    async fn send(&self, expected_server: &str, chat: &str, text: &str) -> zbus::fdo::Result<()> {
        let _guard = self.0.lock.lock().await;
        let api = self.0.api().await.map_err(failure)?;
        if api.config.server != expected_server {
            return Err(failure(
                "The Mac connection changed. Refresh and select the conversation again.",
            ));
        }
        let message = api.send(chat, text).await.map_err(|e| {
            failure(format!(
                "{e:#}. Delivery may be uncertain; check the conversation before retrying."
            ))
        })?;
        Store::open(&api.config.server)
            .and_then(|mut s| s.save(&[], &[message], Some(chat), None))
            .map_err(failure)?;
        self.0.changed();
        self.0.wake.notify_one();
        Ok(())
    }
    async fn create_chat(
        &self,
        expected_server: &str,
        recipient: &str,
        text: &str,
        transport: &str,
    ) -> zbus::fdo::Result<String> {
        let api = self.0.api().await.map_err(failure)?;
        if api.config.server != expected_server {
            return Err(failure(
                "The Mac connection changed. Close this composer and try again.",
            ));
        }
        let mut chat = api.create_chat(recipient,text,transport).await.map_err(|e| failure(format!("{e}. Check the conversation before retrying; the first message may already have sent.")))?;
        if chat.last_message.is_none() {
            chat.last_message = chat.messages.iter().max_by_key(|m| m.date_created).cloned();
        }
        Store::open(&api.config.server)
            .and_then(|mut s| {
                s.save(
                    std::slice::from_ref(&chat),
                    &chat.messages,
                    Some(&chat.guid),
                    None,
                )
            })
            .map_err(failure)?;
        self.0.changed();
        self.0.wake.notify_one();
        serde_json::to_string(&chat).map_err(failure)
    }
    async fn download_attachment(
        &self,
        expected_server: &str,
        guid: &str,
        name: &str,
    ) -> zbus::fdo::Result<String> {
        let _guard = self.0.media_lock.lock().await;
        let api = self.0.api().await.map_err(failure)?;
        if api.config.server != expected_server {
            return Err(failure(
                "The Mac connection changed. Refresh the conversation.",
            ));
        }
        let path = crate::attachments::download(
            &api,
            guid,
            name,
            &crate::config::directory(true).map_err(failure)?,
        )
        .await
        .map_err(failure)?;
        Ok(path.to_string_lossy().into_owned())
    }
    async fn send_attachment(
        &self,
        expected_server: &str,
        chat: &str,
        path: &str,
    ) -> zbus::fdo::Result<()> {
        let api = self.0.api().await.map_err(failure)?;
        if api.config.server != expected_server {
            return Err(failure(
                "The Mac connection changed. Select the conversation again.",
            ));
        }
        let message = api
            .send_attachment(chat, std::path::Path::new(path))
            .await
            .map_err(|e| {
                failure(format!(
                    "{e}. Check the conversation before retrying; the file may already have sent."
                ))
            })?;
        Store::open(&api.config.server)
            .and_then(|mut s| s.save(&[], &[message], Some(chat), None))
            .map_err(failure)?;
        self.0.changed();
        self.0.wake.notify_one();
        Ok(())
    }
    #[zbus(signal)]
    async fn changed(emitter: &zbus::object_server::SignalEmitter<'_>) -> zbus::Result<()>;
}

pub async fn proxy(connection: &zbus::Connection) -> Result<zbus::Proxy<'_>> {
    Ok(zbus::Proxy::new(connection, NAME, PATH, IFACE).await?)
}
pub async fn bridge_proxy<'a>(
    connection: &'a zbus::Connection,
    interface: &'a str,
) -> Result<zbus::Proxy<'a>> {
    let bus = zbus::fdo::DBusProxy::new(connection).await?;
    if !bus.name_has_owner(BRIDGE_NAME.try_into()?).await?
        && bus
            .name_has_owner("com.gabriel.iphonebridge".try_into()?)
            .await?
    {
        // Allow upgrading the native apps before restarting the Bluetooth backend.
        let legacy_interface = interface.replace(BRIDGE_NAME, "com.gabriel.iphonebridge");
        return Ok(zbus::Proxy::new(
            connection,
            "com.gabriel.iphonebridge",
            "/com/gabriel/iphonebridge",
            legacy_interface,
        )
        .await?);
    }
    Ok(zbus::Proxy::new(connection, BRIDGE_NAME, BRIDGE_PATH, interface).await?)
}

async fn socket_loop(engine: Arc<Engine>) {
    loop {
        if let Ok(api) = engine.api().await
            && let Ok(url) = api.socket_url()
        {
            let changed = engine.clone();
            let opened = engine.clone();
            let client = rust_socketio::asynchronous::ClientBuilder::new(url)
                .reconnect(true)
                .reconnect_on_disconnect(true)
                .reconnect_delay(2000, 30000)
                .on("open", move |_, _| {
                    let engine = opened.clone();
                    async move {
                        engine.wake.notify_one();
                    }
                    .boxed()
                })
                .on_any(move |event, _, _| {
                    let engine = changed.clone();
                    async move {
                        if matches!(
                            event.as_str(),
                            "new-message"
                                | "updated-message"
                                | "message-send-error"
                                | "chat-read-status-changed"
                                | "group-name-change"
                                | "participant-added"
                                | "participant-removed"
                        ) {
                            engine.wake.notify_one();
                        }
                    }
                    .boxed()
                })
                .connect()
                .await;
            if let Ok(client) = client {
                engine.reload.notified().await;
                let _ = client.disconnect().await;
                continue;
            }
        }
        tokio::select! { _ = engine.reload.notified() => {}, _ = tokio::time::sleep(std::time::Duration::from_secs(30)) => {} }
    }
}

async fn bluetooth_loop(engine: Arc<Engine>) {
    loop {
        let result: Result<()> = async {
            let connection = zbus::Connection::session().await?;
            let proxy = bridge_proxy(&connection, "dev.turbinebmw.Verdigris.Bridge.Events1").await?;
            let mut signals = proxy.receive_all_signals().await?;
            while let Some(message) = signals.next().await {
                let member = message
                    .header()
                    .member()
                    .map(|m| m.to_string())
                    .unwrap_or_default();
                if member == "AncsNotification" {
                    let Ok((props,)) = message.body().deserialize::<(std::collections::HashMap<String, zbus::zvariant::OwnedValue>,)>() else { continue; };
                    let app = props.get("app_id").and_then(|value| <&str>::try_from(value).ok());
                    if app != Some("com.apple.MobileSMS") { continue; }
                }
                if matches!(
                    member.as_str(),
                    "MessageReceived" | "MessageSent" | "MessageSeen" | "AncsNotification"
                ) {
                    engine.wake.notify_one();
                    // iPhone notification can beat the Mac's database update.
                    let later = engine.clone();
                    tokio::spawn(async move {
                        tokio::time::sleep(std::time::Duration::from_secs(4)).await;
                        later.wake.notify_one();
                    });
                }
            }
            Ok(())
        }
        .await;
        let _ = result;
        tokio::time::sleep(std::time::Duration::from_secs(10)).await;
    }
}

pub async fn run() -> Result<()> {
    let engine = Arc::new(Engine::default());
    *engine.status.lock().await = "Connect your Mac in Settings".into();
    let connection = zbus::connection::Builder::session()?
        .name(NAME)?
        .serve_at(PATH, SyncService(engine.clone()))?
        .build()
        .await?;
    let notifier = engine.clone();
    let bus = connection.clone();
    tokio::spawn(async move {
        let mut changes = notifier.changes.subscribe();
        while changes.changed().await.is_ok() {
            if let Ok(iface) = bus.object_server().interface::<_, SyncService>(PATH).await {
                let _ = SyncService::changed(iface.signal_emitter()).await;
            }
        }
    });
    tokio::spawn(socket_loop(engine.clone()));
    tokio::spawn(bluetooth_loop(engine.clone()));
    let worker = engine.clone();
    tokio::spawn(async move {
        loop {
            let _ = worker.sync().await;
            // Periodic reconciliation recovers silent socket gaps and edits to older messages
            // when a conversation is opened; no tight polling loop is required.
            tokio::select! { _ = worker.wake.notified() => {}, _ = tokio::time::sleep(std::time::Duration::from_secs(120)) => {} }
            tokio::time::sleep(std::time::Duration::from_millis(350)).await;
        }
    });
    tokio::signal::ctrl_c().await.context("Signal handler")?;
    drop(connection);
    Ok(())
}
