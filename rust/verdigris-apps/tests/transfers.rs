use std::{
    io::{Read, Write},
    net::TcpListener,
    path::PathBuf,
    thread,
};
use verdigris_apps::{
    api::Api,
    attachments::{self, MAX_BYTES},
    config::Config,
};

struct Scratch(PathBuf);
impl Scratch {
    fn new() -> Self {
        let path =
            std::env::temp_dir().join(format!("verdigris-transfer-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir(&path).unwrap();
        Self(path)
    }
}
impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn server(
    status: u16,
    declared_length: usize,
    response: Vec<u8>,
) -> (Api, thread::JoinHandle<(String, Vec<u8>)>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let config = Config::normalized(&format!(
        "http://{}/bridge/",
        listener.local_addr().unwrap()
    ))
    .unwrap();
    let worker = thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        stream
            .set_read_timeout(Some(std::time::Duration::from_secs(10)))
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
            .find_map(|l| {
                l.to_lowercase()
                    .strip_prefix("content-length: ")
                    .and_then(|s| s.parse::<usize>().ok())
            })
            .unwrap_or(0);
        let mut body = vec![0; length];
        stream.read_exact(&mut body).unwrap();
        write!(stream,"HTTP/1.1 {status} OK\r\nContent-Type: application/octet-stream\r\nContent-Length: {declared_length}\r\nConnection: close\r\n\r\n").unwrap();
        stream.write_all(&response).unwrap();
        (headers, body)
    });
    (Api::new(config, "private&password".into()).unwrap(), worker)
}

#[tokio::test]
async fn attachment_download_is_cached_and_dynamic_guid_cannot_change_url() {
    let root = Scratch::new();
    let (api, http) = server(200, 5, b"hello".to_vec());
    let guid = "id/with?reserved#characters";
    let path = attachments::download(&api, guid, "../../report.txt", &root.0)
        .await
        .unwrap();
    assert_eq!(std::fs::read(&path).unwrap(), b"hello");
    assert_eq!(path.file_name().unwrap(), "report.txt");
    let headers = http.join().unwrap().0;
    assert!(
        headers.starts_with(
            "GET /bridge/api/v1/attachment/id%2Fwith%3Freserved%23characters/download?"
        )
    );
    assert!(headers.contains("password=private%26password"));
    assert!(headers.contains("original=true"));
    // The one-shot server has closed; this can only succeed from disk.
    assert_eq!(
        attachments::download(&api, guid, "../../report.txt", &root.0)
            .await
            .unwrap(),
        path
    );
}

#[tokio::test]
async fn interrupted_download_leaves_no_cache_entry_or_partial_file() {
    let root = Scratch::new();
    let (api, http) = server(200, 100, b"short".to_vec());
    let path = attachments::cache_path(&root.0, &api.config.server, "guid", "file.bin");
    let error = attachments::download(&api, "guid", "file.bin", &root.0)
        .await
        .unwrap_err();
    assert!(!format!("{error:#}").contains("private&password"));
    assert!(!path.exists());
    assert_eq!(
        std::fs::read_dir(path.parent().unwrap()).unwrap().count(),
        0
    );
    http.join().unwrap();
}

#[tokio::test]
async fn oversized_download_is_rejected_before_writing() {
    let root = Scratch::new();
    let (api, http) = server(200, MAX_BYTES as usize + 1, vec![]);
    assert!(
        attachments::download(&api, "guid", "file.bin", &root.0)
            .await
            .is_err()
    );
    assert_eq!(std::fs::read_dir(&root.0).unwrap().count(), 0);
    http.join().unwrap();
}

#[tokio::test]
async fn failed_download_does_not_cache_server_error_page() {
    let root = Scratch::new();
    let (api, http) = server(404, 7, b"missing".to_vec());
    assert!(
        attachments::download(&api, "guid", "file.bin", &root.0)
            .await
            .is_err()
    );
    assert_eq!(std::fs::read_dir(&root.0).unwrap().count(), 0);
    http.join().unwrap();
}

#[tokio::test]
async fn sending_file_uses_multipart_bytes_and_explicit_chat() {
    let root = Scratch::new();
    let file = root.0.join("example.txt");
    std::fs::write(&file, b"fixture contents").unwrap();
    let response = serde_json::json!({"status":200,"data":{"guid":"sent-file"}})
        .to_string()
        .into_bytes();
    let (api, http) = server(200, response.len(), response);
    assert_eq!(
        api.send_attachment("chat-1", &file).await.unwrap().guid,
        "sent-file"
    );
    let (headers, body) = http.join().unwrap();
    let body = String::from_utf8(body).unwrap();
    assert!(headers.starts_with("POST /bridge/api/v1/message/attachment?"));
    assert!(headers.contains("multipart/form-data"));
    for expected in [
        "name=\"attachment\"",
        "filename=\"example.txt\"",
        "fixture contents",
        "name=\"chatGuid\"",
        "chat-1",
        "apple-script",
        "name=\"tempGuid\"",
    ] {
        assert!(body.contains(expected), "{expected}");
    }
}

#[test]
fn attachment_cache_separates_servers_and_sanitizes_names() {
    let root = Scratch::new();
    let a = attachments::cache_path(&root.0, "server-a", "guid", "..\\..\\file.txt");
    let b = attachments::cache_path(&root.0, "server-b", "guid", "file.txt");
    assert_ne!(a, b);
    assert!(a.starts_with(&root.0));
    assert_eq!(a.file_name().unwrap(), "file.txt");
    assert_eq!(attachments::safe_name(".."), "attachment");
    assert_eq!(attachments::safe_name("a\nb.txt"), "a_b.txt");
}
