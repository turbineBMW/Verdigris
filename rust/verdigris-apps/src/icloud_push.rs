//! Authenticated invalidation stream from the Mac iCloudBridge companion.
//! Payloads never contain reminder or note data; callers reconcile through REST.
use crate::reminders::Connection;
use anyhow::{Context, Result, bail};
use futures_util::StreamExt;
use serde::Deserialize;
use std::time::Duration;
use tokio::sync::watch;
use tokio_tungstenite::{connect_async, tungstenite};
use tungstenite::client::IntoClientRequest;

const MAX_EVENT_BYTES: usize = 4096;
const MAX_RECONNECT_SECONDS: u64 = 60;
const UNSUPPORTED_RETRY_SECONDS: u64 = 15 * 60;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ChangeDomain {
    Reminders,
    Notes,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum StreamEvent {
    Connected,
    Invalidated(ChangeDomain),
    Disconnected,
}

#[derive(Debug, Deserialize, Eq, PartialEq)]
struct ChangeState {
    version: u8,
    epoch: String,
    reminders: u64,
    #[serde(default)]
    notes: u64,
}

fn decode(text: &str) -> Result<ChangeState> {
    if text.len() > MAX_EVENT_BYTES {
        bail!("iCloudBridge change event is too large");
    }
    let state: ChangeState =
        serde_json::from_str(text).context("Invalid iCloudBridge change event")?;
    if state.version != 1 || uuid::Uuid::parse_str(&state.epoch).is_err() {
        bail!("Unsupported iCloudBridge change event");
    }
    Ok(state)
}

pub fn changes_url(connection: &Connection) -> Result<reqwest::Url> {
    let mut url = reqwest::Url::parse(&connection.server)?;
    let scheme = match url.scheme() {
        "http" => "ws",
        "https" => "wss",
        _ => bail!("The Mac bridge URL must use HTTP or HTTPS"),
    };
    url.set_scheme(scheme)
        .map_err(|_| anyhow::anyhow!("Invalid Mac bridge URL"))?;
    url.set_query(None);
    url.set_fragment(None);
    url.path_segments_mut()
        .map_err(|_| anyhow::anyhow!("Invalid Mac bridge URL"))?
        .pop_if_empty()
        .extend(["api", "v1", "changes"]);
    Ok(url)
}

async fn session(
    connection: &Connection,
    token: &str,
    cancel: &mut watch::Receiver<bool>,
    events: &async_channel::Sender<StreamEvent>,
    established: &mut bool,
) -> Result<()> {
    let url = changes_url(connection)?;
    let mut request = url.as_str().into_client_request()?;
    if !token.is_empty() {
        request.headers_mut().insert(
            tungstenite::http::header::AUTHORIZATION,
            format!("Bearer {token}").parse()?,
        );
    }
    let connection = connect_async(request);
    let (mut socket, _) = tokio::select! {
        result = connection => result.context("Could not open iCloudBridge live updates")?,
        _ = cancel.changed() => return Ok(()),
    };
    *established = true;
    let _ = events.send(StreamEvent::Connected).await;
    let mut previous: Option<ChangeState> = None;
    loop {
        let message = tokio::select! {
            value = socket.next() => value,
            _ = cancel.changed() => return Ok(()),
        };
        let Some(message) = message else {
            bail!("iCloudBridge live updates closed");
        };
        match message? {
            tungstenite::Message::Text(text) => {
                let state = decode(&text)?;
                // The initial state is also an invalidation. This closes the race
                // between the first REST refresh and WebSocket registration, and
                // guarantees a reconciliation after every reconnect.
                let reminders_changed = previous
                    .as_ref()
                    .is_none_or(|old| old.epoch != state.epoch || old.reminders != state.reminders);
                let notes_changed = previous
                    .as_ref()
                    .is_none_or(|old| old.epoch != state.epoch || old.notes != state.notes);
                previous = Some(state);
                if reminders_changed {
                    let _ = events
                        .send(StreamEvent::Invalidated(ChangeDomain::Reminders))
                        .await;
                }
                if notes_changed {
                    let _ = events
                        .send(StreamEvent::Invalidated(ChangeDomain::Notes))
                        .await;
                }
            }
            tungstenite::Message::Close(_) => bail!("iCloudBridge live updates closed"),
            tungstenite::Message::Binary(bytes) if bytes.len() > MAX_EVENT_BYTES => {
                bail!("iCloudBridge change event is too large")
            }
            _ => {}
        }
    }
}

async fn wait_to_retry(cancel: &mut watch::Receiver<bool>, seconds: u64) -> bool {
    if *cancel.borrow() {
        return false;
    }
    tokio::select! {
        _ = tokio::time::sleep(Duration::from_secs(seconds)) => true,
        _ = cancel.changed() => false,
    }
}

fn endpoint_is_unavailable(error: &anyhow::Error) -> bool {
    let Some(tungstenite::Error::Http(response)) = error.downcast_ref::<tungstenite::Error>()
    else {
        return matches!(
            error.downcast_ref::<tungstenite::Error>(),
            Some(tungstenite::Error::Protocol(
                tungstenite::error::ProtocolError::WrongHttpVersion
            ))
        );
    };
    matches!(response.status().as_u16(), 401 | 403 | 404)
}

/// Reconnect until cancelled. A current state is sent by the Mac after every
/// handshake, so reconnecting cannot silently leave the UI stale.
pub async fn listen(
    connection: Connection,
    token: String,
    mut cancel: watch::Receiver<bool>,
    events: async_channel::Sender<StreamEvent>,
) {
    let mut delay = 1;
    while !*cancel.borrow() {
        let mut established = false;
        let result = session(&connection, &token, &mut cancel, &events, &mut established).await;
        if *cancel.borrow() {
            break;
        }
        let _ = events.send(StreamEvent::Disconnected).await;
        if established || result.is_ok() {
            delay = 1;
        }
        let retry_after = if result.as_ref().err().is_some_and(endpoint_is_unavailable) {
            UNSUPPORTED_RETRY_SECONDS
        } else {
            delay
        };
        if !wait_to_retry(&mut cancel, retry_after).await {
            break;
        }
        if retry_after != UNSUPPORTED_RETRY_SECONDS {
            delay = (delay * 2).min(MAX_RECONNECT_SECONDS);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn builds_change_url_below_reverse_proxy_prefix() {
        let connection = Connection {
            server: "https://mini.example/bridge/".into(),
        };
        assert_eq!(
            changes_url(&connection).unwrap().as_str(),
            "wss://mini.example/bridge/api/v1/changes"
        );
    }

    #[test]
    fn validates_event_version_epoch_and_size() {
        let state =
            decode(r#"{"version":1,"epoch":"123e4567-e89b-12d3-a456-426614174000","reminders":9}"#)
                .unwrap();
        assert_eq!(state.reminders, 9);
        assert_eq!(state.notes, 0);
        assert!(decode(r#"{"version":2,"epoch":"bad","reminders":9}"#).is_err());
        assert!(decode(&"x".repeat(MAX_EVENT_BYTES + 1)).is_err());
    }

    #[test]
    fn backs_off_for_an_unsupported_or_unauthorized_endpoint() {
        for status in [401, 403, 404] {
            let response = tungstenite::http::Response::builder()
                .status(status)
                .body(None)
                .unwrap();
            let error = anyhow::Error::new(tungstenite::Error::Http(response))
                .context("Could not open live updates");
            assert!(endpoint_is_unavailable(&error));
        }
    }

    #[tokio::test]
    async fn recognizes_an_http_404_from_an_older_bridge() {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};

        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.unwrap();
            let mut request = [0; 2048];
            let _ = stream.read(&mut request).await.unwrap();
            stream
                .write_all(b"HTTP/1.0 404 Not Found\r\nContent-Length: 2\r\n\r\n{}")
                .await
                .unwrap();
        });
        let error = connect_async(format!("ws://{address}/api/v1/changes"))
            .await
            .unwrap_err();
        let error = anyhow::Error::new(error).context("Could not open live updates");
        assert!(endpoint_is_unavailable(&error), "{error:?}");
    }
}
