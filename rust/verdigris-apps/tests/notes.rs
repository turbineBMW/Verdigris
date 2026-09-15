use serde_json::{Value, json};
use std::{
    io::{Read, Write},
    net::TcpListener,
    os::unix::fs::PermissionsExt,
    thread,
};
use verdigris_apps::{
    notes::{self, Snapshot},
    reminders::{Api, Connection},
};

fn fixture(body: Value, status: u16) -> (Connection, thread::JoinHandle<(String, Value)>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let connection =
        Connection::normalized(&format!("http://{}/", listener.local_addr().unwrap())).unwrap();
    let worker = thread::spawn(move || {
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
            .find_map(|s| {
                s.to_lowercase()
                    .strip_prefix("content-length: ")
                    .map(|n| n.parse::<usize>().unwrap())
            })
            .unwrap_or(0);
        let mut bytes = vec![0; length];
        stream.read_exact(&mut bytes).unwrap();
        let request = if bytes.is_empty() {
            Value::Null
        } else {
            serde_json::from_slice(&bytes).unwrap()
        };
        let body = body.to_string();
        write!(
            stream,
            "HTTP/1.1 {status} Response\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        )
        .unwrap();
        (headers, request)
    });
    (connection, worker)
}
fn note(locked: bool) -> Value {
    json!({"id":"note-1","title":"Shopping <list>","folderId":"folder-1","text":"Buy CAFÉ beans\n<b>Literal text</b>","locked":locked,"attachments":["photo.jpg"]})
}
fn snapshot(locked: bool) -> Value {
    json!({"folders":[{"id":"folder-1","title":"iCloud / Notes"}],"notes":[note(locked)]})
}
fn checklist() -> Value {
    json!({"note":{"id":"one","title":"Groceries","folderId":"folder-1","text":"Groceries\nMilk 🥛\nBread\n","locked":false,"attachments":[]},
        "revision":"a".repeat(64),"editable":true,"reason":null,
        "items":[{"id":"milk","text":"Milk 🥛","checked":false,"indent":0,"location":10,"length":7,"canToggle":true,"canEdit":true},
                 {"id":"bread","text":"Bread","checked":false,"indent":0,"location":18,"length":5,"canToggle":true,"canEdit":true}]})
}

#[test]
fn checklist_ranges_and_protected_data_are_validated() {
    let mut good: notes::Detail = serde_json::from_value(checklist()).unwrap();
    good.validate("one").unwrap();
    assert_eq!(notes::utf16_offset(&good.note.text, 16), None); // middle of the emoji
    let mut bad = good.clone();
    bad.items[1].location = 16;
    assert!(bad.validate("one").is_err());
    let mut bad = good.clone();
    bad.items[1].id = "milk".into();
    assert!(bad.validate("one").is_err());
    let mut bad = good.clone();
    bad.items[0].text = "wrong".into();
    assert!(bad.validate("one").is_err());
    good.note.locked = true;
    good.validate("one").unwrap();
    assert!(
        good.note.text.is_empty()
            && good.items.is_empty()
            && good.revision.is_none()
            && !good.editable
    );
}

#[tokio::test]
async fn native_updates_send_desired_state_revision_and_unique_operation() {
    let mut response = checklist();
    response["items"][0]["checked"] = true.into();
    let (connection, worker) = fixture(response, 200);
    let api = Api::new(&connection, "fixture-token".into()).unwrap();
    let detail: notes::Detail = serde_json::from_value(checklist()).unwrap();
    assert!(
        notes::update_item(&api, &detail, "milk", None, Some("line\nbreak"))
            .await
            .is_err()
    );
    let result = notes::update_item(&api, &detail, "milk", Some(true), None)
        .await
        .unwrap();
    assert!(result.items[0].checked);
    let (headers, body) = worker.join().unwrap();
    assert!(headers.starts_with("PATCH /api/v1/notes/one/checklist/milk HTTP/1.1"));
    assert_eq!(body["checked"], true);
    assert_eq!(body["revision"], "a".repeat(64));
    assert!(body.get("text").is_none());
    assert!(uuid::Uuid::parse_str(body["operationId"].as_str().unwrap()).is_ok());
}

#[tokio::test]
async fn conflict_and_unverified_success_are_not_reported_as_saved() {
    let detail: notes::Detail = serde_json::from_value(checklist()).unwrap();
    for (status, expected) in [(409, "changed"), (200, "unconfirmed")] {
        let (connection, worker) = fixture(checklist(), status);
        let error = notes::update_item(
            &Api::new(&connection, String::new()).unwrap(),
            &detail,
            "milk",
            Some(true),
            None,
        )
        .await
        .unwrap_err();
        assert!(error.to_string().contains(expected));
        worker.join().unwrap();
    }
}
#[tokio::test]
async fn text_search_and_protected_contents_are_handled_before_caching() {
    for locked in [false, true] {
        let (connection, worker) = fixture(snapshot(locked), 200);
        let api = Api::new(&connection, "fixture-token".into()).unwrap();
        let fetched = notes::fetch(&api).await.unwrap();
        let item = &fetched.notes[0];
        assert!(item.matches("SHOPPING"));
        assert_eq!(item.matches("café"), !locked);
        assert_eq!(item.text.is_empty(), locked);
        assert_eq!(item.attachments.is_empty(), locked);
        let (headers, body) = worker.join().unwrap();
        assert!(headers.starts_with("GET /api/v1/notes HTTP/1.1"));
        assert!(
            headers
                .to_lowercase()
                .contains("authorization: bearer fixture-token")
        );
        assert_eq!(body, Value::Null);
    }
}
#[tokio::test]
async fn creation_sends_text_as_data_and_rejects_invalid_input() {
    let (connection, worker) = fixture(note(false), 200);
    let api = Api::new(&connection, String::new()).unwrap();
    assert!(notes::create(&api, "folder-1", "", "body").await.is_err());
    assert!(
        notes::create(&api, "folder-1", "line\nbreak", "body")
            .await
            .is_err()
    );
    assert!(
        notes::create(&api, "folder-1", "title", &"x".repeat(1_000_001))
            .await
            .is_err()
    );
    let text = "<b>not HTML</b> & \"quotes\"\nUnicode: café 📝";
    notes::create(&api, "folder-1", " Title ", text)
        .await
        .unwrap();
    let (headers, body) = worker.join().unwrap();
    assert!(headers.starts_with("POST /api/v1/notes HTTP/1.1"));
    assert_eq!(
        body,
        json!({"folderId":"folder-1","title":"Title","text":text})
    );
}
#[tokio::test]
async fn errors_identify_notes_and_do_not_echo_response_contents() {
    for status in [401, 403, 404, 423, 503] {
        let (connection, worker) = fixture(json!({"reason":"private-note-title"}), status);
        let error = notes::fetch(&Api::new(&connection, String::new()).unwrap())
            .await
            .unwrap_err()
            .to_string();
        assert!(error.contains("Notes"));
        assert!(!error.contains("private-note-title"));
        worker.join().unwrap();
    }
}
#[test]
fn invalid_snapshots_are_rejected_and_offline_files_are_private() {
    let mut bad: Snapshot = serde_json::from_value(snapshot(false)).unwrap();
    bad.folders.clear();
    assert!(bad.validate().is_err());
    let dir = std::env::temp_dir().join(format!("verdigris-notes-test-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir(&dir).unwrap();
    let a = Connection::normalized("http://127.0.0.1:31337/").unwrap();
    let b = Connection::normalized("http://127.0.0.1:31338/").unwrap();
    let path = Snapshot::cache_path(&a, &dir);
    let snapshot: Snapshot = serde_json::from_value(snapshot(true)).unwrap();
    snapshot.save(&path).unwrap();
    assert_eq!(
        std::fs::metadata(&path).unwrap().permissions().mode() & 0o777,
        0o600
    );
    assert!(!std::fs::read_to_string(&path).unwrap().contains("CAFÉ"));
    assert!(Snapshot::load(&path).unwrap().notes[0].text.is_empty());
    assert!(Snapshot::load(&Snapshot::cache_path(&b, &dir)).is_err());
    std::fs::remove_dir_all(dir).unwrap();
}

fn ordinary() -> Value {
    json!({"note":{"id":"text-note","title":"Title","folderId":"folder-1","text":"Title\nFirst bold Last\n","locked":false,"attachments":[]},
        "revision":"b".repeat(64),"editable":true,"items":[],"canEditText":true})
}

#[tokio::test]
async fn ordinary_text_edits_validate_capability_payload_and_readback() {
    let mut response = ordinary();
    let body = "<b>literal</b> & café 👨‍👩‍👧‍👦\n\nLast line";
    response["note"]["title"] = "Edited title".into();
    response["note"]["text"] = notes::complete_text("Edited title", body).unwrap().into();
    let (connection, worker) = fixture(response, 200);
    let api = Api::new(&connection, String::new()).unwrap();
    let mut detail: notes::Detail = serde_json::from_value(ordinary()).unwrap();
    detail.validate("text-note").unwrap();
    detail.can_edit_text = false;
    assert!(
        notes::update_text(&api, &detail, "Edited title", body)
            .await
            .is_err()
    );
    detail.can_edit_text = true;
    assert!(
        notes::update_text(&api, &detail, "bad\ntitle", body)
            .await
            .is_err()
    );
    assert!(
        notes::update_text(&api, &detail, "Title", "object\u{fffc}")
            .await
            .is_err()
    );
    assert!(
        notes::update_text(&api, &detail, "Title", &"x".repeat(65536))
            .await
            .is_err()
    );
    let result = notes::update_text(&api, &detail, "Edited title", body)
        .await
        .unwrap();
    assert_eq!(result.note.title, "Edited title");
    let (headers, sent) = worker.join().unwrap();
    assert!(headers.starts_with("PATCH /api/v1/notes/text-note/text HTTP/1.1"));
    assert_eq!(sent["text"], body);
    assert_eq!(sent["title"], "Edited title");
    assert_eq!(sent["revision"], "b".repeat(64));
    assert!(uuid::Uuid::parse_str(sent["operationId"].as_str().unwrap()).is_ok());
}

#[tokio::test]
async fn ordinary_text_edits_reject_conflicts_and_unconfirmed_results() {
    let detail: notes::Detail = serde_json::from_value(ordinary()).unwrap();
    for (status, message) in [(409, "changed"), (200, "unconfirmed")] {
        let (connection, worker) = fixture(ordinary(), status);
        let error = notes::update_text(
            &Api::new(&connection, String::new()).unwrap(),
            &detail,
            "New title",
            "New body",
        )
        .await
        .unwrap_err();
        assert!(error.to_string().contains(message));
        worker.join().unwrap();
    }
    let mut protected: notes::Detail = serde_json::from_value(ordinary()).unwrap();
    protected.note.locked = true;
    protected.validate("text-note").unwrap();
    assert!(!protected.can_edit_text && protected.note.text.is_empty());
    let mut malformed: notes::Detail = serde_json::from_value(checklist()).unwrap();
    malformed.can_edit_text = true;
    assert!(malformed.validate("one").is_err());
    let older: notes::Detail = serde_json::from_value(checklist()).unwrap();
    assert!(!older.can_edit_text);
}
