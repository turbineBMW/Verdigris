//! Shared on-disk contract with verdigris.ancs.preferences.
use crate::config::directory;
use anyhow::{Context, Result, bail};
use serde::{Deserialize, Serialize};
use std::{collections::BTreeMap, io::Write, os::unix::fs::OpenOptionsExt, path::Path};

fn enabled() -> bool {
    true
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Rule {
    #[serde(default = "enabled")]
    pub enabled: bool,
    #[serde(default)]
    pub desktop_id: Option<String>,
    #[serde(default)]
    pub icon: Option<String>,
}

impl Default for Rule {
    fn default() -> Self {
        Self {
            enabled: true,
            desktop_id: None,
            icon: None,
        }
    }
}

#[derive(Serialize, Deserialize)]
pub struct Rules {
    version: u32,
    pub apps: BTreeMap<String, Rule>,
}

impl Default for Rules {
    fn default() -> Self {
        Self {
            version: 1,
            apps: BTreeMap::new(),
        }
    }
}

impl Rules {
    fn decode(bytes: &[u8]) -> Result<Self> {
        let value: Self = serde_json::from_slice(bytes)?;
        if value.version != 1 {
            bail!("Unsupported notification preferences version");
        }
        Ok(value)
    }

    pub fn load() -> Result<Self> {
        match std::fs::read(directory(false)?.join("notification-rules.json")) {
            Ok(bytes) => Self::decode(&bytes).context("Cannot read notification preferences"),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(Self::default()),
            Err(e) => Err(e.into()),
        }
    }

    pub fn save_rule(app_id: &str, rule: Rule) -> Result<()> {
        let mut rules = Self::load()?;
        rules.apps.insert(app_id.to_owned(), rule);
        let root = directory(false)?;
        let temp = root.join(format!(".notification-rules-{}.tmp", uuid::Uuid::new_v4()));
        let result = (|| -> Result<()> {
            let mut file = std::fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(&temp)?;
            file.write_all(&serde_json::to_vec_pretty(&rules)?)?;
            file.sync_all()?;
            std::fs::rename(&temp, root.join("notification-rules.json"))?;
            Ok(())
        })();
        let _ = std::fs::remove_file(temp);
        result
    }
}

pub fn apps() -> Result<BTreeMap<String, String>> {
    let mut apps: BTreeMap<String, String> =
        match std::fs::read(directory(true)?.join("notification-apps.json")) {
            Ok(bytes) => {
                serde_json::from_slice(&bytes).context("Cannot read discovered iPhone apps")?
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => BTreeMap::new(),
            Err(e) => return Err(e.into()),
        };
    for id in Rules::load()?.apps.keys() {
        apps.entry(id.clone()).or_insert_with(|| id.clone());
    }
    Ok(apps)
}

pub fn icon_path(name: &str) -> Result<std::path::PathBuf> {
    if name.is_empty() || Path::new(name).file_name().and_then(|v| v.to_str()) != Some(name) {
        bail!("Invalid notification icon name");
    }
    Ok(directory(false)?.join("notification-icons").join(name))
}

pub fn import_icon(path: &Path) -> Result<String> {
    use std::os::unix::fs::PermissionsExt;
    if std::fs::metadata(path)?.len() > 8 * 1024 * 1024 {
        bail!("Choose an image smaller than 8 MB");
    }
    let pixbuf = gtk::gdk_pixbuf::Pixbuf::from_file_at_scale(path, 256, 256, true)
        .context("Cannot open this image")?;
    let name = format!("{}.png", uuid::Uuid::new_v4());
    let target = icon_path(&name)?;
    let root = target.parent().context("Missing icon folder")?;
    std::fs::create_dir_all(root)?;
    std::fs::set_permissions(root, std::fs::Permissions::from_mode(0o700))?;
    pixbuf.savev(&target, "png", &[])?;
    std::fs::set_permissions(&target, std::fs::Permissions::from_mode(0o600))?;
    Ok(name)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shared_rules_contract() {
        let rules = Rules::decode(br#"{"version":1,"apps":{"com.example.chat":{"enabled":false,"desktop_id":"org.example.Chat.desktop","icon":"custom.png"},"com.example.default":{}}}"#).unwrap();
        assert_eq!(rules.apps["com.example.default"], Rule::default());
        assert!(!rules.apps["com.example.chat"].enabled);
        let encoded = serde_json::to_vec(&rules).unwrap();
        assert_eq!(Rules::decode(&encoded).unwrap().apps, rules.apps);
        assert!(Rules::decode(br#"{"version":2,"apps":{}}"#).is_err());
        assert!(Rules::decode(br#"{"version":1,"apps":{"a":{"enabled":"false"}}}"#).is_err());
    }

    #[test]
    fn icon_paths_cannot_escape_folder() {
        for name in [
            "",
            "..",
            ".",
            "../outside.png",
            "/tmp/icon.png",
            "nested/icon.png",
        ] {
            assert!(icon_path(name).is_err(), "{name}");
        }
    }
}
