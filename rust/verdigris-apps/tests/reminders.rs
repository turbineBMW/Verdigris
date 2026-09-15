use serde_json::{Value, json};
use std::{
    io::{Read, Write},
    net::TcpListener,
    os::unix::fs::PermissionsExt,
    thread,
};
use verdigris_apps::reminders::{Api, Connection, Snapshot};

fn fixture(responses: Vec<(u16, Value)>) -> (Connection, thread::JoinHandle<Vec<(String, Value)>>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let connection = Connection::normalized(&format!(
        "http://{}/bridge/",
        listener.local_addr().unwrap()
    ))
    .unwrap();
    let worker = thread::spawn(move || {
        let mut requests = Vec::new();
        for (status, body) in responses {
            let (mut stream, _) = listener.accept().unwrap();
            stream
                .set_read_timeout(Some(std::time::Duration::from_secs(5)))
                .unwrap();
            let mut bytes = Vec::new();
            while !bytes.ends_with(b"\r\n\r\n") {
                let mut byte = [0];
                stream.read_exact(&mut byte).unwrap();
                bytes.push(byte[0]);
            }
            let headers = String::from_utf8(bytes).unwrap();
            let length = headers
                .lines()
                .find_map(|line| {
                    line.to_lowercase()
                        .strip_prefix("content-length: ")
                        .map(|n| n.parse::<usize>().unwrap())
                })
                .unwrap_or(0);
            let mut payload = vec![0; length];
            stream.read_exact(&mut payload).unwrap();
            requests.push((
                headers,
                if payload.is_empty() {
                    Value::Null
                } else {
                    serde_json::from_slice(&payload).unwrap()
                },
            ));
            let body = body.to_string();
            write!(stream, "HTTP/1.1 {status} Response\r\nContent-Type: application/json\r\nLocation: http://127.0.0.1:1/leak\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len()).unwrap();
        }
        requests
    });
    (connection, worker)
}

fn reminder(completed: bool) -> Value {
    json!({"id":"reminder/with?reserved#chars", "title":"Call <Alex>", "notes":"Keep the alarm", "isCompleted":completed,
        "dueDate":"2026-09-12T15:00:00Z", "listId":"list/one", "priority":1})
}

#[tokio::test]
async fn bridge_contract_preserves_dates_and_uses_sparse_completion_updates() {
    let (connection, worker) = fixture(vec![
        (
            200,
            json!([{"id":"list/one", "title":"Personal", "reminderCount":1}]),
        ),
        (200, json!([reminder(false)])),
        (200, reminder(true)),
        (200, reminder(false)),
        (200, reminder(false)),
    ]);
    let api = Api::new(&connection, "fixture-token".into()).unwrap();
    let snapshot = api.snapshot().await.unwrap();
    assert_eq!(snapshot.reminders.len(), 1);
    let item = &snapshot.reminders[0];
    assert!(api.complete(&item.id, true).await.unwrap().is_completed);
    api.create("list/one", " Buy milk ", "2% milk")
        .await
        .unwrap();
    api.edit(item, "Call Sam", "Keep the alarm").await.unwrap();
    let requests = worker.join().unwrap();
    assert!(
        requests[0]
            .0
            .starts_with("GET /bridge/api/v1/lists HTTP/1.1")
    );
    assert!(requests[1].0.starts_with(
        "GET /bridge/api/v1/lists/list%2Fone/reminders?includeCompleted=true HTTP/1.1"
    ));
    assert!(
        requests[2].0.starts_with(
            "PUT /bridge/api/v1/reminders/reminder%2Fwith%3Freserved%23chars HTTP/1.1"
        )
    );
    assert_eq!(requests[2].1, json!({"isCompleted":true}));
    assert_eq!(
        requests[3].1,
        json!({"title":"Buy milk", "notes":"2% milk"})
    );
    assert_eq!(requests[4].1, json!({"title":"Call Sam"}));
    for (headers, body) in requests {
        assert!(
            headers
                .to_lowercase()
                .contains("authorization: bearer fixture-token\r\n")
        );
        assert!(!headers.lines().next().unwrap().contains("fixture-token"));
        assert!(body.get("dueDate").is_none());
        assert!(body.get("priority").is_none());
    }
}

#[tokio::test]
async fn redirects_and_failed_writes_are_reported_without_retry_or_echoing_server_secrets() {
    for status in [302, 401, 403, 404, 500] {
        let (connection, worker) = fixture(vec![(status, json!({"reason":"secret-from-server"}))]);
        let error = Api::new(&connection, "private-token".into())
            .unwrap()
            .create("list", "Test", "")
            .await
            .unwrap_err()
            .to_string();
        assert!(!error.contains("secret-from-server"));
        assert!(!error.contains("private-token"));
        assert_eq!(worker.join().unwrap().len(), 1);
    }
}

#[tokio::test]
async fn unexpected_list_data_does_not_produce_a_successful_snapshot() {
    let (connection, worker) = fixture(vec![
        (
            200,
            json!([{"id":"other-list", "title":"Other", "reminderCount":1}]),
        ),
        (200, json!([reminder(false)])),
    ]);
    assert!(
        Api::new(&connection, String::new())
            .unwrap()
            .snapshot()
            .await
            .is_err()
    );
    worker.join().unwrap();
}

#[test]
fn cache_is_private_and_partitioned_by_server() {
    let dir =
        std::env::temp_dir().join(format!("verdigris-reminders-test-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir(&dir).unwrap();
    let a = Connection::normalized("http://127.0.0.1:31337").unwrap();
    let b = Connection::normalized("http://127.0.0.1:31338").unwrap();
    let mut snapshot = Snapshot::default();
    snapshot
        .reminders
        .push(serde_json::from_value(reminder(false)).unwrap());
    snapshot.save(&a.cache_path(&dir)).unwrap();
    assert_eq!(
        std::fs::metadata(a.cache_path(&dir))
            .unwrap()
            .permissions()
            .mode()
            & 0o777,
        0o600
    );
    assert_eq!(
        Snapshot::load(&a.cache_path(&dir)).unwrap().reminders[0].title,
        "Call <Alex>"
    );
    assert!(Snapshot::load(&b.cache_path(&dir)).is_err());
    snapshot.reminders.clear();
    snapshot.save(&a.cache_path(&dir)).unwrap();
    assert!(
        Snapshot::load(&a.cache_path(&dir))
            .unwrap()
            .reminders
            .is_empty()
    );
    std::fs::remove_dir_all(dir).unwrap();
}

#[test]
fn only_loopback_connections_allow_an_empty_token() {
    for url in [
        "http://127.0.0.1:31337",
        "http://localhost:31337",
        "http://[::1]:31337",
    ] {
        assert!(Api::new(&Connection::normalized(url).unwrap(), String::new()).is_ok());
    }
    assert!(
        Api::new(
            &Connection::normalized("https://mac.example").unwrap(),
            String::new()
        )
        .is_err()
    );
    for url in [
        "file:///etc/passwd",
        "http://mac/?token=secret",
        "http://user:secret@mac/",
        "http://mac/#fragment",
    ] {
        assert!(Connection::normalized(url).is_err());
    }
}
