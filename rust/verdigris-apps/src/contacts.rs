//! Read Verdigris's PBAP cache without changing its schema or taking ownership of it.
use crate::api::Chat;
use anyhow::Result;
use rusqlite::{Connection, OpenFlags};
use std::{
    collections::BTreeMap,
    path::{Path, PathBuf},
};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Contact {
    pub id: i64,
    pub name: String,
    pub photo: Option<PathBuf>,
    pub photo_modified: Option<std::time::SystemTime>,
}

#[derive(Clone, Default, PartialEq, Eq)]
pub struct Contacts(BTreeMap<String, Option<Contact>>);

fn phone_key(address: &str) -> Option<String> {
    let address = address
        .trim()
        .strip_prefix("tel:")
        .unwrap_or(address.trim());
    if !address
        .chars()
        .all(|c| c.is_ascii_digit() || "+(). -".contains(c))
    {
        return None;
    }
    let digits: String = address.chars().filter(char::is_ascii_digit).collect();
    if digits.len() < 7 {
        return None;
    }
    // Match US/Canada's optional +1 without conflating unrelated country codes.
    Some(if digits.len() == 11 && digits.starts_with('1') {
        digits[1..].into()
    } else {
        digits
    })
}

impl Contacts {
    pub fn search(&self, query: &str, limit: usize) -> Vec<(String, Contact)> {
        let query = query.trim().to_lowercase();
        let digits: String = query.chars().filter(char::is_ascii_digit).collect();
        let mut rows: Vec<_> = self
            .0
            .iter()
            .filter_map(|(address, contact)| {
                let contact = contact.as_ref()?;
                (query.is_empty()
                    || contact.name.to_lowercase().contains(&query)
                    || (!digits.is_empty() && address.contains(&digits)))
                .then(|| {
                    (
                        if address.len() > 10 {
                            format!("+{address}")
                        } else {
                            address.clone()
                        },
                        contact.clone(),
                    )
                })
            })
            .collect();
        rows.sort_by(|a, b| {
            a.1.name
                .to_lowercase()
                .cmp(&b.1.name.to_lowercase())
                .then(a.0.cmp(&b.0))
        });
        rows.truncate(limit);
        rows
    }
    pub fn load() -> Result<Self> {
        let state = std::env::var_os("XDG_STATE_HOME")
            .filter(|v| !v.is_empty())
            .map(PathBuf::from)
            .or_else(|| directories::BaseDirs::new().map(|d| d.home_dir().join(".local/state")));
        let Some(state) = state else {
            return Ok(Self::default());
        };
        let mut path = state.join("verdigris/contacts.sqlite");
        if !path.exists() {
            // Blue's backend may still own the PBAP cache during an upgrade.
            path = state.join("iphonebridge/contacts.sqlite");
        }
        if !path.exists() {
            return Ok(Self::default());
        }
        let connection = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY)?;
        connection.busy_timeout(std::time::Duration::from_millis(300))?;
        Self::from_connection(&connection)
    }

    pub fn from_connection(connection: &Connection) -> Result<Self> {
        let mut book = Self::default();
        let mut query = connection.prepare("SELECT c.id, p.phone_norm, COALESCE(NULLIF(TRIM(c.nickname), ''), c.full_name), c.photo_path FROM phones p JOIN contacts c ON c.id=p.contact_id")?;
        let rows = query.query_map([], |r| {
            Ok((
                r.get::<_, i64>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, String>(2)?,
                r.get::<_, Option<String>>(3)?,
            ))
        })?;
        for row in rows {
            let (id, address, name, photo) = row?;
            let Some(key) = phone_key(&address) else {
                continue;
            };
            let photo = photo.map(PathBuf::from).filter(|p| p.is_file());
            let photo_modified = photo
                .as_deref()
                .and_then(|p| p.metadata().ok())
                .and_then(|m| m.modified().ok());
            let contact = Contact {
                id,
                name,
                photo,
                photo_modified,
            };
            book.0
                .entry(key)
                .and_modify(|existing| {
                    // Do not guess an identity when two distinct contacts share a number.
                    if existing.as_ref().is_some_and(|c| c.id != id) {
                        *existing = None;
                    }
                })
                .or_insert(Some(contact));
        }
        Ok(book)
    }

    pub fn resolve(&self, address: &str) -> Option<&Contact> {
        self.0.get(&phone_key(address)?)?.as_ref()
    }
    pub fn name(&self, address: &str) -> String {
        self.resolve(address)
            .map(|c| c.name.trim())
            .filter(|n| !n.is_empty())
            .unwrap_or(address)
            .to_owned()
    }
    pub fn photo(&self, address: &str) -> Option<&Path> {
        self.resolve(address)?.photo.as_deref()
    }

    pub fn addresses(chat: &Chat) -> Vec<&str> {
        let addresses: Vec<_> = chat
            .participants
            .iter()
            .filter_map(|p| p["address"].as_str())
            .collect();
        if !addresses.is_empty() {
            return addresses;
        }
        if chat.guid.contains(";+;") {
            return vec![];
        }
        chat.chat_identifier
            .as_deref()
            .or_else(|| chat.guid.split_once(";-;").map(|(_, a)| a))
            .into_iter()
            .collect()
    }
    pub fn title(&self, chat: &Chat) -> String {
        let addresses = Self::addresses(chat);
        if addresses.len() == 1
            && !chat.guid.contains(";+;")
            && let Some(contact) = self.resolve(addresses[0])
            && !contact.name.trim().is_empty()
        {
            return contact.name.clone();
        }
        if let Some(name) = chat
            .display_name
            .as_deref()
            .filter(|n| !n.trim().is_empty())
        {
            return name.into();
        }
        if !addresses.is_empty() {
            return addresses
                .iter()
                .map(|a| self.name(a))
                .collect::<Vec<_>>()
                .join(", ");
        }
        chat.title()
    }
    pub fn chat_photo(&self, chat: &Chat) -> Option<&Path> {
        let addresses = Self::addresses(chat);
        if addresses.len() != 1 || chat.guid.contains(";+;") {
            return None;
        }
        self.photo(addresses[0])
    }
}
