//! Ported from Bubo’s keyless GIF provider.
//! Keyless GIF search over DuckDuckGo's image search (`f=type:gif`).
//!
//! DDG's image endpoint is undocumented: a page load yields a `vqd` token that unlocks `/i.js`
//! JSON results. The token is cached here and refreshed on the first failure. No API key,
//! no account — the trade is that a DDG-side change can break this without notice.
use anyhow::{Context, Result, anyhow};
use std::sync::{Mutex, OnceLock};

const UA: &str = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0";
/// Refuse GIFs larger than this — MMS carriers reject big attachments anyway.
pub const MAX_BYTES: usize = 8 * 1024 * 1024;

#[derive(Clone, Debug, serde::Deserialize)]
pub struct Gif {
    /// Direct URL to the animated GIF.
    #[serde(rename = "image")]
    pub url: String,
    /// Small static preview, good for a picker grid.
    pub thumbnail: String,
}

#[derive(serde::Deserialize)]
struct Page {
    results: Vec<Gif>,
}

fn http() -> &'static reqwest::Client {
    static C: OnceLock<reqwest::Client> = OnceLock::new();
    C.get_or_init(|| {
        reqwest::Client::builder()
            .user_agent(UA)
            .timeout(std::time::Duration::from_secs(20))
            .build()
            .expect("gif http client")
    })
}

fn vqd_cache() -> &'static Mutex<Option<String>> {
    static V: OnceLock<Mutex<Option<String>>> = OnceLock::new();
    V.get_or_init(|| Mutex::new(None))
}

async fn fetch_vqd(query: &str) -> Result<String> {
    let html = http()
        .get("https://duckduckgo.com/")
        .query(&[("q", query), ("iax", "images"), ("ia", "images")])
        .send()
        .await?
        .error_for_status()?
        .text()
        .await?;
    let start = html
        .find("vqd=")
        .ok_or_else(|| anyhow!("no vqd token in DDG page"))?
        + 4;
    // Appears both as `vqd="4-…"` and `vqd=4-…`; accept either.
    let tok: String = html[start..]
        .trim_start_matches(['"', '\''])
        .chars()
        .take_while(|c| c.is_ascii_digit() || *c == '-')
        .collect();
    if tok.is_empty() {
        return Err(anyhow!("empty vqd token"));
    }
    Ok(tok)
}

async fn vqd(query: &str, refresh: bool) -> Result<String> {
    if !refresh && let Some(v) = vqd_cache().lock().unwrap().clone() {
        return Ok(v);
    }
    let v = fetch_vqd(query).await?;
    *vqd_cache().lock().unwrap() = Some(v.clone());
    Ok(v)
}

/// Search GIFs; `page` is zero-based (DDG hands back ~50–100 per page).
pub async fn search(query: &str, page: u32) -> Result<Vec<Gif>> {
    let query = query.trim();
    if query.is_empty() {
        return Ok(vec![]);
    }
    let mut refresh = false;
    for _ in 0..2 {
        let tok = vqd(query, refresh).await?;
        let resp = http()
            .get("https://duckduckgo.com/i.js")
            .header("Referer", "https://duckduckgo.com/")
            .query(&[
                ("l", "us-en"),
                ("o", "json"),
                ("q", query),
                ("vqd", &tok),
                ("f", "type:gif"),
                ("p", "1"),
                ("s", &(page * 100).to_string()),
            ])
            .send()
            .await?;
        if resp.status().is_success() {
            let page: Page = resp.json().await.context("DDG results JSON")?;
            return Ok(page
                .results
                .into_iter()
                .filter(|g| valid_url(&g.url) && valid_url(&g.thumbnail))
                .collect());
        }
        // 403 means the token went stale (or we're rate-limited); one refresh, then give up.
        refresh = true;
    }
    Err(anyhow!(
        "DuckDuckGo refused the search (rate-limited?) — try again in a moment"
    ))
}

fn valid_url(url: &str) -> bool {
    reqwest::Url::parse(url)
        .is_ok_and(|u| matches!(u.scheme(), "http" | "https") && u.host_str().is_some())
}

async fn fetch_bytes(url: &str, limit: usize) -> Result<Vec<u8>> {
    if !valid_url(url) {
        return Err(anyhow!("Invalid image URL"));
    }
    let mut response = http().get(url).send().await?.error_for_status()?;
    if response.content_length().is_some_and(|n| n > limit as u64) {
        return Err(anyhow!(
            "Image is too large (maximum {} MiB)",
            limit / 1024 / 1024
        ));
    }
    let mut bytes = Vec::new();
    while let Some(chunk) = response.chunk().await? {
        if bytes.len() + chunk.len() > limit {
            return Err(anyhow!("Image is too large"));
        }
        bytes.extend_from_slice(&chunk);
    }
    Ok(bytes)
}

/// Download the animation, with a bounded response even when Content-Length is absent.
pub async fn download(url: &str) -> Result<Vec<u8>> {
    let bytes = fetch_bytes(url, MAX_BYTES).await?;
    if !bytes.starts_with(b"GIF87a") && !bytes.starts_with(b"GIF89a") {
        return Err(anyhow!("That link is not a GIF"));
    }
    Ok(bytes)
}

pub async fn thumbnail(url: &str) -> Result<Vec<u8>> {
    fetch_bytes(url, 2 * 1024 * 1024).await
}

/// Keep the download alive until Verdigris's sync service finishes the attachment upload.
pub struct StagedGif(pub std::path::PathBuf);
impl Drop for StagedGif {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.0);
    }
}
pub async fn stage(url: &str) -> Result<StagedGif> {
    use tokio::io::AsyncWriteExt;
    let bytes = download(url).await?;
    let dir = crate::config::directory(true)?.join("gif-outbox");
    tokio::fs::create_dir_all(&dir).await?;
    let staged = StagedGif(dir.join(format!("{}.gif", uuid::Uuid::new_v4())));
    let mut file = tokio::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&staged.0)
        .await?;
    file.write_all(&bytes).await?;
    file.sync_all().await?;
    Ok(staged)
}
