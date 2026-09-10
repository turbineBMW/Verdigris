use crate::{api::Api, config};
use anyhow::{Context, Result, bail};
use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};
use tokio::io::AsyncWriteExt;

pub const MAX_BYTES: u64 = 100 * 1024 * 1024;

pub fn safe_name(name: &str) -> String {
    let last = name.rsplit(['/', '\\']).next().unwrap_or("");
    let mut clean = String::new();
    for c in last.chars().map(|c| if c.is_control() { '_' } else { c }) {
        if clean.len() + c.len_utf8() > 180 {
            break;
        }
        clean.push(c);
    }
    if clean.trim_matches('.').trim().is_empty() {
        "attachment".into()
    } else {
        clean
    }
}
pub fn cache_path(root: &Path, server: &str, guid: &str, name: &str) -> PathBuf {
    root.join("attachments")
        .join(format!("{:x}", Sha256::digest(server.as_bytes())))
        .join(format!("{:x}", Sha256::digest(guid.as_bytes())))
        .join(safe_name(name))
}
pub fn cached(server: &str, guid: &str, name: &str) -> Option<PathBuf> {
    let path = cache_path(&config::directory(true).ok()?, server, guid, name);
    path.is_file().then_some(path)
}
struct PartialFile(PathBuf);
impl Drop for PartialFile {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.0);
    }
}

pub async fn download(api: &Api, guid: &str, name: &str, root: &Path) -> Result<PathBuf> {
    if guid.is_empty() {
        bail!("Attachment has no identifier");
    }
    let destination = cache_path(root, &api.config.server, guid, name);
    if destination.is_file() {
        return Ok(destination);
    }
    let mut url = api.url(&["attachment/"])?;
    url.path_segments_mut()
        .map_err(|_| anyhow::anyhow!("Invalid server URL"))?
        .pop_if_empty()
        .push(guid)
        .push("download");
    url.query_pairs_mut().append_pair("original", "true");
    let mut response = api
        .http()
        .get(url)
        .timeout(std::time::Duration::from_secs(180))
        .send()
        .await
        .map_err(|e| e.without_url())
        .context("Could not download the attachment")?;
    if !response.status().is_success() {
        bail!(
            "Attachment download returned HTTP {}",
            response.status().as_u16()
        );
    }
    if response.content_length().is_some_and(|n| n > MAX_BYTES) {
        bail!("Attachment is larger than 100 MiB");
    }
    let parent = destination
        .parent()
        .context("Invalid attachment cache path")?;
    tokio::fs::create_dir_all(parent).await?;
    let partial = PartialFile(parent.join(format!(".{}.part", uuid::Uuid::new_v4())));
    let mut file = tokio::fs::OpenOptions::new()
        .create_new(true)
        .write(true)
        .mode(0o600)
        .open(&partial.0)
        .await?;
    let mut written = 0u64;
    while let Some(chunk) = response
        .chunk()
        .await
        .map_err(|e| e.without_url())
        .context("Attachment transfer was interrupted")?
    {
        written += chunk.len() as u64;
        if written > MAX_BYTES {
            bail!("Attachment is larger than 100 MiB");
        }
        file.write_all(&chunk).await?;
    }
    file.sync_all().await?;
    drop(file);
    tokio::fs::rename(&partial.0, &destination).await?;
    Ok(destination)
}
