//! Client for cleverdevil/iCloudBridge. Keep mutations sparse: date updates in
//! the upstream bridge remove alarms, so this client never sends date fields.
use anyhow::{Context, Result, bail};
use reqwest::{Method, Url};
use secret_service::{EncryptionType, SecretService};
use serde::{Deserialize, Serialize, de::DeserializeOwned};
use sha2::{Digest, Sha256};
use std::{
    collections::HashMap, io::Write, os::unix::fs::OpenOptionsExt, path::Path, time::Duration,
};

#[derive(Clone, Default, Serialize, Deserialize)]
pub struct Connection {
    pub server: String,
}

impl Connection {
    pub fn normalized(server: &str) -> Result<Self> {
        Ok(Self {
            server: crate::config::Config::normalized(server)?.server,
        })
    }

    pub fn load() -> Result<Self> {
        let path = crate::config::directory(false)?.join("reminders.json");
        match std::fs::read(path) {
            Ok(bytes) => Ok(serde_json::from_slice(&bytes)?),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(Self::default()),
            Err(e) => Err(e.into()),
        }
    }

    pub fn save(&self) -> Result<()> {
        write_private(
            &crate::config::directory(false)?.join("reminders.json"),
            self,
        )
    }

    pub fn is_loopback(&self) -> bool {
        let Ok(url) = Url::parse(&self.server) else {
            return false;
        };
        let host = url.host_str().unwrap_or("").trim_matches(['[', ']']);
        host == "localhost"
            || host
                .parse::<std::net::IpAddr>()
                .is_ok_and(|ip| ip.is_loopback())
    }

    fn attributes(&self) -> HashMap<&str, &str> {
        HashMap::from([
            ("application", "dev.turbinebmw.Verdigris.Reminders"),
            ("server", self.server.as_str()),
        ])
    }

    pub async fn token(&self) -> Result<String> {
        // A loopback connection is intended for an SSH tunnel to the Mac's
        // localhost-only server. It does not need a keyring or bearer token.
        if self.is_loopback() {
            return Ok(String::new());
        }
        let service = SecretService::connect(EncryptionType::Dh)
            .await
            .context("Could not open the desktop keyring")?;
        let items = service.search_items(self.attributes()).await?;
        let item = items
            .unlocked
            .first()
            .context("Reminders token is missing or the keyring is locked. Open Settings.")?;
        Ok(String::from_utf8(item.get_secret().await?)?)
    }

    pub async fn save_token(&self, token: &str) -> Result<()> {
        if token.is_empty() && self.is_loopback() {
            return Ok(());
        }
        if token.is_empty() {
            bail!("Enter the Reminders bridge API token");
        }
        let service = SecretService::connect(EncryptionType::Dh).await?;
        let collection = service.get_default_collection().await?;
        collection.unlock().await?;
        collection
            .create_item(
                "Verdigris Reminders — iCloudBridge",
                self.attributes(),
                token.as_bytes(),
                true,
                "text/plain",
            )
            .await?;
        Ok(())
    }

    pub fn cache_path(&self, directory: &Path) -> std::path::PathBuf {
        directory.join(format!(
            "reminders-{:x}.json",
            Sha256::digest(self.server.as_bytes())
        ))
    }
}

pub(crate) fn write_private(path: &Path, value: &impl Serialize) -> Result<()> {
    let temporary = path.with_extension(format!("{}.tmp", uuid::Uuid::new_v4()));
    let result = (|| -> Result<()> {
        let mut file = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&temporary)?;
        file.write_all(&serde_json::to_vec(value)?)?;
        file.sync_all()?;
        std::fs::rename(&temporary, path)?;
        Ok(())
    })();
    if result.is_err() {
        let _ = std::fs::remove_file(temporary);
    }
    result
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct List {
    pub id: String,
    pub title: String,
    #[serde(default)]
    pub reminder_count: u64,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Reminder {
    pub id: String,
    pub title: String,
    pub notes: Option<String>,
    pub is_completed: bool,
    pub due_date: Option<String>,
    pub list_id: String,
}

#[derive(Clone, Default, Deserialize, Serialize)]
pub struct Snapshot {
    pub lists: Vec<List>,
    pub reminders: Vec<Reminder>,
    pub fetched_at: u64,
}

impl Snapshot {
    pub fn load(path: &Path) -> Result<Self> {
        Ok(serde_json::from_slice(&std::fs::read(path)?)?)
    }

    pub fn save(&self, path: &Path) -> Result<()> {
        write_private(path, self)
    }
}

#[derive(Clone)]
pub struct Api {
    client: reqwest::Client,
    base: Url,
    token: String,
}

impl Api {
    pub fn new(connection: &Connection, token: String) -> Result<Self> {
        let normalized = Connection::normalized(&connection.server)?;
        if token.is_empty() && !normalized.is_loopback() {
            bail!("Enter the Reminders bridge API token");
        }
        Ok(Self {
            client: reqwest::Client::builder()
                .connect_timeout(Duration::from_secs(5))
                .timeout(Duration::from_secs(20))
                .redirect(reqwest::redirect::Policy::none())
                .retry(reqwest::retry::never())
                .build()?,
            base: Url::parse(&normalized.server)?,
            token,
        })
    }

    pub(crate) async fn request<T: DeserializeOwned>(
        &self,
        method: Method,
        segments: &[&str],
        body: Option<serde_json::Value>,
    ) -> Result<T> {
        let domain = if segments.first() == Some(&"notes") {
            "Notes"
        } else {
            "Reminders"
        };
        let mut url = self.base.clone();
        url.path_segments_mut()
            .map_err(|_| anyhow::anyhow!("Invalid server URL"))?
            .pop_if_empty()
            .extend(["api", "v1"])
            .extend(segments.iter().copied());
        if method == Method::GET && segments.last() == Some(&"reminders") {
            url.query_pairs_mut()
                .append_pair("includeCompleted", "true");
        }
        let mut request = self.client.request(method, url);
        if domain == "Notes" {
            request = request.timeout(Duration::from_secs(110));
        }
        if !self.token.is_empty() {
            request = request.bearer_auth(&self.token);
        }
        if let Some(body) = body {
            request = request.json(&body);
        }
        let mut response = request
            .send()
            .await
            .map_err(|_| anyhow::anyhow!("Could not reach the {domain} bridge"))?;
        match response.status().as_u16() {
            200..=299 => {}
            403 if domain == "Notes" => bail!(
                "Notes access denied. Check your edit permission for the shared note and the Mac bridge permissions, then reload."
            ),
            401 | 403 => bail!("{domain} access denied. Check the API token and Mac permissions."),
            404 if domain == "Notes" && segments.len() > 1 => {
                bail!(
                    "Note or checklist unavailable. Refresh the note and check the Mac bridge version."
                )
            }
            404 if domain == "Notes" => {
                bail!("Notes is unavailable. Install the Notes extension on your Mac bridge.")
            }
            404 => bail!(
                "Reminder or list unavailable. Check the selected lists on the Mac, then refresh."
            ),
            409 if domain == "Notes" => bail!(
                "The note changed or Notes is busy. Reload its current version before saving."
            ),
            422 if domain == "Notes" => {
                bail!(
                    "This Notes item or edit is not supported. Try a smaller text change, or open it on your Mac."
                )
            }
            423 if domain == "Notes" => {
                bail!("Unlock the Mac mini, then reload Notes before editing notes.")
            }
            503 if domain == "Notes" => bail!(
                "Notes is unavailable or could not confirm the edit. Check the Mac, then reload before retrying."
            ),
            code => bail!("{domain} bridge returned HTTP {code}"),
        }
        const MAX_BYTES: usize = 16 * 1024 * 1024;
        let mut bytes = Vec::new();
        while let Some(chunk) = response
            .chunk()
            .await
            .with_context(|| format!("Incomplete {domain} response"))?
        {
            if bytes.len() + chunk.len() > MAX_BYTES {
                bail!("{domain} response is too large");
            }
            bytes.extend_from_slice(&chunk);
        }
        serde_json::from_slice(&bytes).with_context(|| format!("Invalid {domain} bridge response"))
    }

    pub async fn lists(&self) -> Result<Vec<List>> {
        self.request(Method::GET, &["lists"], None).await
    }

    pub async fn snapshot(&self) -> Result<Snapshot> {
        tokio::time::timeout(Duration::from_secs(60), async {
            let lists = self.lists().await?;
            let mut reminders = Vec::new();
            for list in &lists {
                let items: Vec<Reminder> = self
                    .request(Method::GET, &["lists", &list.id, "reminders"], None)
                    .await?;
                if items.iter().any(|item| item.list_id != list.id) {
                    bail!("Bridge returned reminders from an unexpected list");
                }
                reminders.extend(items);
            }
            Ok(Snapshot {
                lists,
                reminders,
                fetched_at: std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)?
                    .as_secs(),
            })
        })
        .await
        .context("Refreshing Reminders timed out")?
    }

    pub async fn create(&self, list: &str, title: &str, notes: &str) -> Result<Reminder> {
        if title.trim().is_empty() {
            bail!("Enter a reminder title");
        }
        self.request(
            Method::POST,
            &["lists", list, "reminders"],
            Some(serde_json::json!({"title": title.trim(), "notes": notes})),
        )
        .await
    }

    pub async fn edit(&self, original: &Reminder, title: &str, notes: &str) -> Result<Reminder> {
        if title.trim().is_empty() {
            bail!("Enter a reminder title");
        }
        let mut patch = serde_json::Map::new();
        if title.trim() != original.title {
            patch.insert("title".into(), title.trim().into());
        }
        if notes != original.notes.as_deref().unwrap_or("") {
            patch.insert("notes".into(), notes.into());
        }
        if patch.is_empty() {
            return Ok(original.clone());
        }
        self.request(
            Method::PUT,
            &["reminders", &original.id],
            Some(patch.into()),
        )
        .await
    }

    pub async fn complete(&self, id: &str, completed: bool) -> Result<Reminder> {
        self.request(
            Method::PUT,
            &["reminders", id],
            Some(serde_json::json!({"isCompleted": completed})),
        )
        .await
    }
}
