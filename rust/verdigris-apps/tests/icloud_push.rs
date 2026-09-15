use futures_util::SinkExt;
use std::sync::{Arc, Mutex};
use tokio::net::TcpListener;
use tokio_tungstenite::tungstenite::{
    Message,
    handshake::server::{Request, Response},
};
use verdigris_apps::{
    icloud_push::{self, ChangeDomain, StreamEvent},
    reminders::Connection,
};

#[test]
fn change_url_preserves_a_reverse_proxy_prefix() {
    let connection = Connection {
        server: "https://mini.example/bridge/".into(),
    };
    assert_eq!(
        icloud_push::changes_url(&connection).unwrap().as_str(),
        "wss://mini.example/bridge/api/v1/changes"
    );
}

#[tokio::test]
async fn authenticated_state_invalidates_and_listener_stops() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let request_data = Arc::new(Mutex::new(None));
    let seen = request_data.clone();
    let server = tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let mut socket = tokio_tungstenite::accept_hdr_async(
            stream,
            move |request: &Request, response: Response| {
                *seen.lock().unwrap() = Some((
                    request.uri().path().to_string(),
                    request
                        .headers()
                        .get("authorization")
                        .unwrap()
                        .to_str()
                        .unwrap()
                        .to_string(),
                ));
                Ok(response)
            },
        )
        .await
        .unwrap();
        for (reminders, notes) in [(4, 7), (4, 7), (5, 7), (5, 8)] {
            socket
                .send(Message::Text(
                    format!(
                        r#"{{"version":1,"epoch":"67e55044-10b1-426f-9247-bb680e5fe0c8","reminders":{reminders},"notes":{notes}}}"#
                    )
                    .into(),
                ))
                .await
                .unwrap();
        }
        futures_util::future::pending::<()>().await;
    });

    let connection = Connection {
        server: format!("http://{address}/bridge/"),
    };
    let (cancel_tx, cancel_rx) = tokio::sync::watch::channel(false);
    let (event_tx, event_rx) = async_channel::unbounded();
    let client = tokio::spawn(icloud_push::listen(
        connection,
        "secret token".into(),
        cancel_rx,
        event_tx,
    ));

    assert_eq!(
        tokio::time::timeout(std::time::Duration::from_secs(2), event_rx.recv())
            .await
            .unwrap()
            .unwrap(),
        StreamEvent::Connected
    );
    assert_eq!(
        event_rx.recv().await.unwrap(),
        StreamEvent::Invalidated(ChangeDomain::Reminders)
    );
    assert_eq!(
        event_rx.recv().await.unwrap(),
        StreamEvent::Invalidated(ChangeDomain::Notes)
    );
    assert_eq!(
        event_rx.recv().await.unwrap(),
        StreamEvent::Invalidated(ChangeDomain::Reminders)
    );
    assert_eq!(
        event_rx.recv().await.unwrap(),
        StreamEvent::Invalidated(ChangeDomain::Notes)
    );
    assert!(
        event_rx.try_recv().is_err(),
        "duplicate generation invalidated"
    );
    assert_eq!(
        request_data.lock().unwrap().clone().unwrap(),
        (
            "/bridge/api/v1/changes".into(),
            "Bearer secret token".into()
        )
    );

    cancel_tx.send(true).unwrap();
    tokio::time::timeout(std::time::Duration::from_secs(2), client)
        .await
        .expect("listener ignored cancellation")
        .unwrap();
    server.abort();
}
