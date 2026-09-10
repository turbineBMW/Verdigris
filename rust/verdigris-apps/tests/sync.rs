use serde_json::{Value, json};
use std::{
    io::{Read, Write},
    net::TcpListener,
    thread,
};
use verdigris_apps::{
    api::{Api, Chat, Message},
    config::Config,
    service::sync_into,
    store::Store,
};

fn message(guid: &str, date: i64) -> Message {
    serde_json::from_value(json!({"guid":guid,"dateCreated":date,"text":"hello","isFromMe":false,"chats":[{"guid":"chat-1"}]})).unwrap()
}
fn store() -> Store {
    Store::from_connection(rusqlite::Connection::open_in_memory().unwrap()).unwrap()
}

// A real local HTTP server exercises URL encoding, POST bodies, response errors and paging.
fn server(pages: Vec<(u16, Value)>) -> (Api, thread::JoinHandle<Vec<Value>>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let config = Config::normalized(&format!("http://{}", listener.local_addr().unwrap())).unwrap();
    let handle = thread::spawn(move || {
        let mut requests = Vec::new();
        for (status, data) in pages {
            let (mut stream, _) = listener.accept().unwrap();
            stream
                .set_read_timeout(Some(std::time::Duration::from_secs(10)))
                .unwrap();
            let mut buffer = Vec::new();
            let header_end = loop {
                let mut byte = [0];
                stream.read_exact(&mut byte).unwrap();
                buffer.push(byte[0]);
                if buffer.ends_with(b"\r\n\r\n") {
                    break buffer.len();
                }
            };
            let headers = String::from_utf8_lossy(&buffer).to_string();
            assert!(headers.contains("password=test%26secret"));
            let len: usize = headers
                .lines()
                .find_map(|line| {
                    line.to_lowercase()
                        .strip_prefix("content-length: ")
                        .and_then(|n| n.parse().ok())
                })
                .unwrap_or(0);
            buffer.resize(header_end + len, 0);
            stream.read_exact(&mut buffer[header_end..]).unwrap();
            requests.push(if len == 0 {
                Value::Null
            } else {
                serde_json::from_slice(&buffer[header_end..]).unwrap()
            });
            let body = json!({"status":status,"data":data}).to_string();
            write!(stream, "HTTP/1.1 {status} OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",body.len()).unwrap();
        }
        requests
    });
    (Api::new(config, "test&secret".into()).unwrap(), handle)
}

#[test]
fn repeated_sync_merges_by_guid_and_updates_receipts() {
    let mut store = store();
    let mut msg = message("same", 100);
    store.save(&[], &[msg.clone()], None, Some(100)).unwrap();
    msg.date_read = Some(200);
    store.save(&[], &[msg], None, Some(90)).unwrap();
    let rows = store.messages("chat-1", 100).unwrap();
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0].date_read, Some(200));
    assert_eq!(store.watermark(), Some(100));
}

#[test]
fn sent_response_without_chats_is_cached_in_target_thread() {
    let mut store = store();
    let mut msg = message("sent", 100);
    msg.chats.clear();
    store.save(&[], &[msg], Some("target"), None).unwrap();
    assert_eq!(store.messages("target", 100).unwrap().len(), 1);
    assert!(store.messages("chat-1", 100).unwrap().is_empty());
}

#[tokio::test]
async fn catch_up_pages_through_more_than_one_hundred_missed_messages() {
    let mut store = store();
    store.save(&[], &[], None, Some(400_000)).unwrap();
    let first: Vec<_> = (0..100)
        .map(|i| message(&format!("m{i}"), 500_000 + i))
        .collect();
    let (api, http) = server(vec![
        (200, json!([])),
        (200, json!(first)),
        (200, json!([message("last", 600_000)])),
    ]);
    sync_into(&api, &mut store).await.unwrap();
    assert_eq!(store.messages("chat-1", 1000).unwrap().len(), 101);
    assert_eq!(store.watermark(), Some(600_000));
    let queries = http.join().unwrap();
    assert_eq!(queries[1]["after"], 100_000);
    assert_eq!(queries[2]["offset"], 100);
    assert_eq!(queries[1]["sort"], "ASC");
}

#[tokio::test]
async fn failed_page_does_not_advance_cursor_or_partially_commit() {
    let mut store = store();
    store
        .save(&[], &[message("old", 400_000)], None, Some(400_000))
        .unwrap();
    let first: Vec<_> = (0..100)
        .map(|i| message(&format!("m{i}"), 500_000 + i))
        .collect();
    let (api, http) = server(vec![
        (200, json!([])),
        (200, json!(first)),
        (503, Value::Null),
    ]);
    assert!(sync_into(&api, &mut store).await.is_err());
    assert_eq!(store.watermark(), Some(400_000));
    assert_eq!(store.messages("chat-1", 1000).unwrap().len(), 1);
    http.join().unwrap();
}

#[tokio::test]
async fn first_sync_seeds_recent_messages_without_claiming_full_history() {
    let mut store = store();
    let (api, http) = server(vec![
        (200, json!([{"guid":"chat-1","displayName":"Friend"}])),
        (200, json!([message("recent", 900)])),
    ]);
    sync_into(&api, &mut store).await.unwrap();
    assert_eq!(store.chats().unwrap()[0].title(), "Friend");
    assert_eq!(store.watermark(), Some(900));
    assert_eq!(http.join().unwrap()[1]["sort"], "DESC");
}

#[tokio::test]
async fn send_uses_existing_chat_and_a_temporary_guid() {
    let (api, http) = server(vec![(200, json!(message("sent", 100)))]);
    api.send("iMessage;-;+15555550100", "test body")
        .await
        .unwrap();
    let body = &http.join().unwrap()[0];
    assert_eq!(body["chatGuid"], "iMessage;-;+15555550100");
    assert_eq!(body["method"], "apple-script");
    assert!(uuid::Uuid::parse_str(body["tempGuid"].as_str().unwrap()).is_ok());
}

#[tokio::test]
async fn new_conversation_sends_first_message_to_one_normalized_recipient() {
    let (api, http) = server(vec![(
        200,
        json!({"guid":"iMessage;-;+15555550100","messages":[message("first",100)]}),
    )]);
    let chat = api
        .create_chat("+1 (555) 555-0100", "Hello", "iMessage")
        .await
        .unwrap();
    assert_eq!(chat.messages.len(), 1);
    let body = &http.join().unwrap()[0];
    assert_eq!(body["addresses"], json!(["+15555550100"]));
    assert_eq!(body["message"], "Hello");
    assert_eq!(body["method"], "apple-script");
    assert_eq!(body["service"], "iMessage");
    assert!(uuid::Uuid::parse_str(body["tempGuid"].as_str().unwrap()).is_ok());
}

#[test]
fn recipient_validation_rejects_ambiguous_or_multiple_recipients() {
    use verdigris_apps::api::normalize_recipient;
    assert_eq!(
        normalize_recipient(" alex@example.com ").unwrap(),
        "alex@example.com"
    );
    assert_eq!(
        normalize_recipient("tel:+1 (555) 555-0100").unwrap(),
        "+15555550100"
    );
    for address in [
        "Alex",
        "123",
        "5555550100,5555550200",
        "a@@b.com",
        "a@b.com\nCc:other",
        "+1+5555550100",
    ] {
        assert!(normalize_recipient(address).is_err(), "{address}");
    }
}

#[tokio::test]
async fn network_errors_do_not_expose_credentials() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    drop(listener);
    let api = Api::new(
        Config::normalized(&format!("http://{addr}")).unwrap(),
        "never-print-this".into(),
    )
    .unwrap();
    let error = format!("{:#}", api.request("server/info", None).await.unwrap_err());
    assert!(!error.contains("never-print-this"));
    assert!(!error.contains("password="));
}

#[test]
fn url_validation_and_nullable_apple_fields() {
    for url in [
        "ftp://host",
        "http://user:password@host",
        "http://host?password=oops",
    ] {
        assert!(Config::normalized(url).is_err());
    }
    assert_eq!(
        Config::normalized(" https://mini.example/bridge ")
            .unwrap()
            .server,
        "https://mini.example/bridge/"
    );
    let msg: Message = serde_json::from_value(
        json!({"guid":"empty","text":null,"dateCreated":null,"handle":null}),
    )
    .unwrap();
    assert_eq!(msg.preview(), "Message");
    let chat: Chat = serde_json::from_value(json!({"guid":"group","displayName":null,"participants":[{"address":"Alice"},{"address":"Bob"}]})).unwrap();
    assert_eq!(chat.title(), "Alice, Bob");
}

#[tokio::test]
async fn named_reaction_in_chat_preview_and_history_does_not_abort_sync() {
    let mut store = store();
    let reaction = json!({"guid":"reaction","text":null,"dateCreated":900,
        "associatedMessageType":"love","chats":[{"guid":"chat-1"}]});
    let (api, http) = server(vec![
        (200, json!([{"guid":"chat-1","lastMessage":reaction}])),
        (200, json!([reaction])),
    ]);
    sync_into(&api, &mut store).await.unwrap();
    assert_eq!(store.watermark(), Some(900));
    assert_eq!(
        store.chats().unwrap()[0]
            .last_message
            .as_ref()
            .unwrap()
            .preview(),
        "Reaction"
    );
    let cached = store.messages("chat-1", 100).unwrap();
    assert_eq!(cached[0].preview(), "Reaction");
    assert_eq!(
        serde_json::to_value(&cached[0]).unwrap()["associatedMessageType"],
        "love"
    );
    http.join().unwrap();
}

#[test]
fn reaction_names_legacy_codes_and_empty_values_round_trip() {
    for value in [
        json!("love"),
        json!("-love"),
        json!("like"),
        json!("future-reaction"),
        json!(2000),
        json!(3000),
        json!(0),
        json!("none"),
        json!(""),
        Value::Null,
    ] {
        let message: Message =
            serde_json::from_value(json!({"guid":"reaction", "associatedMessageType":value}))
                .unwrap();
        assert_eq!(
            serde_json::to_value(&message).unwrap()["associatedMessageType"],
            value
        );
        let expected = if [json!(0), json!("none"), json!(""), Value::Null].contains(&value) {
            "Message"
        } else {
            "Reaction"
        };
        assert_eq!(message.preview(), expected);
    }
    let absent: Message = serde_json::from_value(json!({"guid":"plain"})).unwrap();
    assert_eq!(absent.preview(), "Message");
}
