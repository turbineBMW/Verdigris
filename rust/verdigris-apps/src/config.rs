use anyhow::{Context, Result, bail};
use directories::ProjectDirs;
use secret_service::{EncryptionType, SecretService};
use serde::{Deserialize, Serialize};
use std::{
    collections::HashMap,
    io::Write,
    os::unix::fs::{OpenOptionsExt, PermissionsExt},
    path::PathBuf,
};

#[derive(Clone, Default, Serialize, Deserialize)]
pub struct Config {
    pub server: String,
}

pub fn directory(state: bool) -> Result<PathBuf> {
    let dirs = ProjectDirs::from("dev", "turbinebmw", "verdigris")
        .context("Home directory unavailable")?;
    let path = if state {
        dirs.data_local_dir()
    } else {
        dirs.config_dir()
    }
    .to_path_buf();
    std::fs::create_dir_all(&path)?;
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o700))?;
    Ok(path)
}

impl Config {
    pub fn load() -> Result<Self> {
        match std::fs::read(directory(false)?.join("connection.json")) {
            Ok(bytes) => Ok(serde_json::from_slice(&bytes)?),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(Self::default()),
            Err(e) => Err(e.into()),
        }
    }
    pub fn normalized(server: &str) -> Result<Self> {
        let mut url = reqwest::Url::parse(server.trim())
            .context("Enter a complete http:// or https:// server URL")?;
        if !matches!(url.scheme(), "http" | "https")
            || url.host_str().is_none()
            || !url.username().is_empty()
            || url.password().is_some()
            || url.query().is_some()
            || url.fragment().is_some()
        {
            bail!("Use the server base URL without a password, query, or fragment");
        }
        let path = format!("{}/", url.path().trim_end_matches('/'));
        url.set_path(&path);
        Ok(Self {
            server: url.to_string(),
        })
    }
    pub fn save(&self) -> Result<()> {
        let dir = directory(false)?;
        let temp = dir.join(format!("connection-{}.tmp", uuid::Uuid::new_v4()));
        let mut file = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&temp)?;
        file.write_all(&serde_json::to_vec(self)?)?;
        file.sync_all()?;
        std::fs::rename(temp, dir.join("connection.json"))?;
        Ok(())
    }
    pub async fn password(&self) -> Result<String> {
        let service = SecretService::connect(EncryptionType::Dh)
            .await
            .context("Could not open the desktop keyring")?;
        let mut items = service.search_items(self.attributes()).await?;
        if items.unlocked.is_empty() && items.locked.is_empty() {
            // Read the existing Blue credential on upgrade; saving in Settings writes
            // a Verdigris item. The original secret remains available to the old app.
            let legacy = HashMap::from([
                ("application", "dev.turbinebmw.Blue"),
                ("server", self.server.as_str()),
            ]);
            items = service.search_items(legacy).await?;
        }
        let item = items.unlocked.first().context(
            "Server password is missing or the desktop keyring is locked. Open Settings.",
        )?;
        Ok(String::from_utf8(item.get_secret().await?)?)
    }
    pub async fn save_password(&self, password: &str) -> Result<()> {
        if password.is_empty() {
            bail!("Enter the BlueBubbles server password");
        }
        let service = SecretService::connect(EncryptionType::Dh).await?;
        let collection = service.get_default_collection().await?;
        collection.unlock().await?;
        collection
            .create_item(
                "Verdigris Messages — BlueBubbles",
                self.attributes(),
                password.as_bytes(),
                true,
                "text/plain",
            )
            .await?;
        Ok(())
    }
    fn attributes(&self) -> HashMap<&str, &str> {
        HashMap::from([
            ("application", "dev.turbinebmw.Verdigris"),
            ("server", self.server.as_str()),
        ])
    }
}
