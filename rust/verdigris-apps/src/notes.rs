//! Plain-text Apple Notes snapshots over the existing Mac companion connection.
use crate::reminders::{Api, Connection, write_private};
use anyhow::{Result, bail};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Folder {
    pub id: String,
    pub title: String,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Note {
    pub id: String,
    pub title: String,
    pub folder_id: String,
    pub text: String,
    pub locked: bool,
    pub attachments: Vec<String>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Snapshot {
    pub folders: Vec<Folder>,
    pub notes: Vec<Note>,
    #[serde(default, rename = "defaultFolderId")]
    pub default_folder_id: Option<String>,
    #[serde(default)]
    pub fetched_at: u64,
    #[serde(default)]
    pub checklists: std::collections::HashMap<String, Detail>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ChecklistItem {
    pub id: String,
    pub text: String,
    pub checked: bool,
    pub indent: usize,
    pub location: usize,
    pub length: usize,
    pub can_toggle: bool,
    pub can_edit: bool,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Detail {
    pub note: Note,
    pub revision: Option<String>,
    pub items: Vec<ChecklistItem>,
    pub editable: bool,
    pub reason: Option<String>,
    #[serde(default)]
    pub shared: bool,
    #[serde(default, rename = "canEditText")]
    pub can_edit_text: bool,
    #[serde(default, rename = "textReason")]
    pub text_reason: Option<String>,
}

pub fn utf16_offset(text: &str, offset: usize) -> Option<usize> {
    let mut units = 0;
    for (byte, character) in text.char_indices() {
        if units == offset {
            return Some(byte);
        }
        units += character.len_utf16();
        if units > offset {
            return None;
        }
    }
    (units == offset).then_some(text.len())
}

impl Detail {
    pub fn validate(&mut self, id: &str) -> Result<()> {
        if self.note.id != id {
            bail!("Notes returned the wrong document");
        }
        if self.note.locked {
            self.note.text.clear();
            self.note.attachments.clear();
            self.items.clear();
            self.revision = None;
            self.editable = false;
            self.can_edit_text = false;
            self.text_reason = None;
            self.reason = None;
            return Ok(());
        }
        if let Some(revision) = &self.revision {
            if revision.len() != 64 || !revision.bytes().all(|b| b.is_ascii_hexdigit()) {
                bail!("Notes returned an invalid revision");
            }
        } else if self.editable || !self.items.is_empty() {
            bail!("Notes omitted the checklist revision");
        }
        if self.can_edit_text
            && (!self.editable
                || !self.items.is_empty()
                || !self.note.attachments.is_empty()
                || self.note.text.split_once('\n').map(|v| v.0) != Some(&self.note.title))
        {
            bail!("Notes returned inconsistent text editing capabilities");
        }
        let mut ids = std::collections::HashSet::new();
        let mut previous_end = 0;
        for item in &self.items {
            let end = item
                .location
                .checked_add(item.length)
                .ok_or_else(|| anyhow::anyhow!("Invalid item range"))?;
            let start_byte = utf16_offset(&self.note.text, item.location)
                .ok_or_else(|| anyhow::anyhow!("Invalid item position"))?;
            let end_byte = utf16_offset(&self.note.text, end)
                .ok_or_else(|| anyhow::anyhow!("Invalid item length"))?;
            if !ids.insert(&item.id)
                || item.id.is_empty()
                || item.location < previous_end
                || item.indent > 32
                || self.note.text[start_byte..end_byte] != item.text
                || item.text.contains('\n')
            {
                bail!("Notes returned inconsistent checklist items");
            }
            previous_end = end;
        }
        Ok(())
    }
}

pub async fn detail(api: &Api, id: &str) -> Result<Detail> {
    let mut result: Detail = api
        .request(reqwest::Method::GET, &["notes", id, "checklist"], None)
        .await?;
    result.validate(id)?;
    Ok(result)
}

pub async fn update_item(
    api: &Api,
    detail: &Detail,
    item: &str,
    checked: Option<bool>,
    text: Option<&str>,
) -> Result<Detail> {
    if !detail.editable || detail.note.locked || checked.is_some() == text.is_some() {
        bail!("This note cannot be edited");
    }
    let target = detail
        .items
        .iter()
        .find(|i| i.id == item)
        .ok_or_else(|| anyhow::anyhow!("Item no longer exists"))?;
    if (checked.is_some() && !target.can_toggle) || (text.is_some() && !target.can_edit) {
        bail!("This item cannot be edited");
    }
    if let Some(text) = text {
        if text.is_empty()
            || text.len() >= 4096
            || text
                .chars()
                .any(|c| c.is_control() || ['\u{2028}', '\u{2029}', '\u{fffc}'].contains(&c))
        {
            bail!("Use a single-line item under 4 KiB");
        }
    }
    let mut body = serde_json::json!({"operationId": uuid::Uuid::new_v4().to_string(), "revision": detail.revision});
    if let Some(value) = checked {
        body["checked"] = value.into();
    }
    if let Some(value) = text {
        body["text"] = value.into();
    }
    let mut result: Detail = api
        .request(
            reqwest::Method::PATCH,
            &["notes", &detail.note.id, "checklist", item],
            Some(body),
        )
        .await?;
    result.validate(&detail.note.id)?;
    let updated = result
        .items
        .iter()
        .find(|i| i.id == item)
        .ok_or_else(|| anyhow::anyhow!("Edit outcome is unconfirmed. Reload the note."))?;
    if checked.is_some_and(|v| updated.checked != v) || text.is_some_and(|v| updated.text != v) {
        bail!("Edit outcome is unconfirmed. Reload the note.");
    }
    Ok(result)
}

pub fn complete_text(title: &str, body: &str) -> Result<String> {
    if title.trim().is_empty()
        || title.encode_utf16().count() > 1000
        || title
            .chars()
            .any(|c| c.is_control() || ['\u{2028}', '\u{2029}', '\u{fffc}'].contains(&c))
        || body.chars().any(|c| {
            (c.is_control() && c != '\n' && c != '\t')
                || ['\u{2028}', '\u{2029}', '\u{fffc}'].contains(&c)
        })
    {
        bail!("Use a single-line title and ordinary text without embedded objects.");
    }
    let mut text = format!("{title}\n{body}");
    if !text.ends_with('\n') {
        text.push('\n');
    }
    if text.len() > 64 * 1024 {
        bail!("Keep the note under 64 KiB.");
    }
    Ok(text)
}

pub async fn update_text(api: &Api, detail: &Detail, title: &str, body: &str) -> Result<Detail> {
    if !detail.can_edit_text || !detail.editable || detail.note.locked {
        bail!("This note cannot be edited as text.");
    }
    let expected = complete_text(title, body)?;
    let mut result: Detail = api.request(reqwest::Method::PATCH, &["notes", &detail.note.id, "text"],
        Some(serde_json::json!({"operationId": uuid::Uuid::new_v4().to_string(), "revision": detail.revision, "title": title, "text": body}))).await?;
    result.validate(&detail.note.id)?;
    if result.note.text != expected
        || result.note.title != title
        || result.note.folder_id != detail.note.folder_id
    {
        bail!("Edit outcome is unconfirmed. Reload the note.");
    }
    Ok(result)
}

impl Note {
    pub fn matches(&self, query: &str) -> bool {
        let query = query.to_lowercase();
        self.title.to_lowercase().contains(&query)
            || (!self.locked && self.text.to_lowercase().contains(&query))
    }
}

impl Snapshot {
    pub fn validate(&mut self) -> Result<()> {
        let folders: std::collections::HashSet<_> = self.folders.iter().map(|f| &f.id).collect();
        let mut ids = std::collections::HashSet::new();
        for note in &mut self.notes {
            if !folders.contains(&note.folder_id) || !ids.insert(note.id.clone()) {
                bail!("Notes returned inconsistent folders or duplicate notes. Refresh again.");
            }
            // Do not display or persist protected content, even if a future
            // bridge version mistakenly includes it in a protected-note DTO.
            if note.locked {
                note.text.clear();
                note.attachments.clear();
            }
        }
        self.checklists.retain(|id, detail| {
            self.notes
                .iter()
                .any(|note| &note.id == id && !note.locked && note.text == detail.note.text)
                && detail.validate(id).is_ok()
        });
        Ok(())
    }

    pub fn cache_path(connection: &Connection, directory: &Path) -> PathBuf {
        directory.join(format!(
            "notes-{:x}.json",
            Sha256::digest(connection.server.as_bytes())
        ))
    }

    pub fn load(path: &Path) -> Result<Self> {
        let mut snapshot: Self = serde_json::from_slice(&std::fs::read(path)?)?;
        snapshot.validate()?;
        Ok(snapshot)
    }

    pub fn save(&self, path: &Path) -> Result<()> {
        let mut snapshot = self.clone();
        snapshot.validate()?;
        write_private(path, &snapshot)
    }
}

pub async fn fetch(api: &Api) -> Result<Snapshot> {
    let mut snapshot: Snapshot = api.request(reqwest::Method::GET, &["notes"], None).await?;
    snapshot.validate()?;
    snapshot.fetched_at = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)?
        .as_secs();
    Ok(snapshot)
}

pub async fn create(api: &Api, folder: &str, title: &str, text: &str) -> Result<Note> {
    if title.trim().is_empty() || title.contains(['\n', '\r']) || title.chars().count() > 1000 {
        bail!("Enter a single-line title under 1,000 characters");
    }
    if text.len() > 1_000_000 {
        bail!("Keep the note under 1 MB");
    }
    let note: Note = api
        .request(
            reqwest::Method::POST,
            &["notes"],
            Some(serde_json::json!({"folderId": folder, "title": title.trim(), "text": text})),
        )
        .await?;
    if note.folder_id != folder {
        bail!("Mac returned an unexpected folder. Refresh before retrying.");
    }
    Ok(note)
}
