use std::{
    io::{Read, Write},
    net::TcpListener,
    thread,
};
use verdigris_apps::gif;

fn response(headers: &str, body: Vec<u8>) -> (String, thread::JoinHandle<()>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://{}/animation.gif", listener.local_addr().unwrap());
    let headers = headers.to_string();
    let worker = thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        stream
            .set_read_timeout(Some(std::time::Duration::from_secs(5)))
            .unwrap();
        stream
            .set_write_timeout(Some(std::time::Duration::from_secs(5)))
            .unwrap();
        let mut request = Vec::new();
        while !request.ends_with(b"\r\n\r\n") {
            let mut byte = [0];
            stream.read_exact(&mut byte).unwrap();
            request.push(byte[0]);
        }
        write!(
            stream,
            "HTTP/1.1 200 OK\r\nConnection: close\r\n{headers}\r\n"
        )
        .unwrap();
        let _ = stream.write_all(&body);
    });
    (url, worker)
}

#[tokio::test]
async fn gif_bytes_are_preserved_for_attachment_upload() {
    let bytes = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b";
    let (url, server) = response(
        &format!("Content-Length: {}\r\n", bytes.len()),
        bytes.to_vec(),
    );
    assert_eq!(gif::download(&url).await.unwrap(), bytes);
    server.join().unwrap();
}

#[tokio::test]
async fn html_disguised_as_gif_is_rejected() {
    let (url, server) = response(
        "Content-Type: image/gif\r\n",
        b"<html>blocked</html>".to_vec(),
    );
    assert!(
        gif::download(&url)
            .await
            .unwrap_err()
            .to_string()
            .contains("not a GIF")
    );
    server.join().unwrap();
}

#[tokio::test]
async fn oversized_gifs_are_rejected_with_and_without_content_length() {
    for declared in [true, false] {
        let mut bytes = b"GIF89a".to_vec();
        bytes.resize(gif::MAX_BYTES + 1, 0);
        let headers = if declared {
            format!("Content-Length: {}\r\n", bytes.len())
        } else {
            String::new()
        };
        let (url, server) = response(&headers, bytes);
        assert!(
            gif::download(&url)
                .await
                .unwrap_err()
                .to_string()
                .contains("too large")
        );
        server.join().unwrap();
    }
}

#[tokio::test]
async fn previews_are_bounded_and_non_http_urls_are_rejected() {
    let (url, server) = response("Content-Length: 2097153\r\n", Vec::new());
    assert!(gif::thumbnail(&url).await.is_err());
    server.join().unwrap();
    assert!(gif::download("file:///etc/passwd").await.is_err());
}

#[test]
fn staged_gif_is_removed_when_upload_scope_ends() {
    let path = std::env::temp_dir().join(format!("verdigris-gif-{}.gif", uuid::Uuid::new_v4()));
    std::fs::write(&path, b"GIF89a").unwrap();
    drop(gif::StagedGif(path.clone()));
    assert!(!path.exists());
}
