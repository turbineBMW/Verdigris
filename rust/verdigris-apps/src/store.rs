use crate::{
    api::{Chat, Message},
    config,
};
use anyhow::Result;
use rusqlite::{Connection, params};
use sha2::{Digest, Sha256};

pub struct Store(Connection);
impl Store {
    pub fn open(server: &str) -> Result<Self> {
        let name = format!("{:x}.sqlite", Sha256::digest(server.as_bytes()));
        Self::from_connection(Connection::open(config::directory(true)?.join(name))?)
    }
    pub fn from_connection(conn: Connection) -> Result<Self> {
        conn.busy_timeout(std::time::Duration::from_secs(5))?;
        conn.execute_batch("PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS chats(guid TEXT PRIMARY KEY, data TEXT NOT NULL, date INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS messages(chat TEXT NOT NULL, guid TEXT NOT NULL, data TEXT NOT NULL, date INTEGER NOT NULL, PRIMARY KEY(chat,guid));
            CREATE INDEX IF NOT EXISTS message_date ON messages(chat,date);
            CREATE TABLE IF NOT EXISTS sync(id INTEGER PRIMARY KEY CHECK(id=1), watermark INTEGER NOT NULL);")?;
        Ok(Self(conn))
    }
    pub fn watermark(&self) -> Option<i64> {
        self.0
            .query_row("SELECT watermark FROM sync WHERE id=1", [], |r| r.get(0))
            .ok()
    }
    pub fn save(
        &mut self,
        chats: &[Chat],
        messages: &[Message],
        chat_override: Option<&str>,
        watermark: Option<i64>,
    ) -> Result<()> {
        let tx = self.0.transaction()?;
        for chat in chats {
            tx.execute("INSERT INTO chats VALUES(?1,?2,?3) ON CONFLICT(guid) DO UPDATE SET data=excluded.data,date=excluded.date",
                params![chat.guid, serde_json::to_string(chat)?, chat.last_message.as_ref().and_then(|m| m.date_created).unwrap_or(0)])?;
        }
        for msg in messages {
            let guids: Vec<&str> = if let Some(chat) = chat_override {
                vec![chat]
            } else {
                msg.chats
                    .iter()
                    .filter_map(|c| c["guid"].as_str())
                    .collect()
            };
            for chat in guids {
                tx.execute("INSERT INTO messages VALUES(?1,?2,?3,?4) ON CONFLICT(chat,guid) DO UPDATE SET data=excluded.data,date=excluded.date",
                    params![chat, msg.guid, serde_json::to_string(msg)?, msg.date_created.unwrap_or(0)])?;
            }
        }
        if let Some(mark) = watermark {
            tx.execute("INSERT INTO sync VALUES(1,?1) ON CONFLICT(id) DO UPDATE SET watermark=MAX(watermark,excluded.watermark)", [mark])?;
        }
        tx.commit()?;
        Ok(())
    }
    pub fn chats(&self) -> Result<Vec<Chat>> {
        let mut stmt = self
            .0
            .prepare("SELECT data FROM chats ORDER BY date DESC,guid")?;
        let rows = stmt.query_map([], |r| r.get::<_, String>(0))?;
        rows.map(|r| Ok(serde_json::from_str(&r?)?)).collect()
    }
    pub fn messages(&self, chat: &str, limit: usize) -> Result<Vec<Message>> {
        let mut stmt = self.0.prepare("SELECT data FROM (SELECT data,date,guid FROM messages WHERE chat=?1 ORDER BY date DESC,guid DESC LIMIT ?2) ORDER BY date,guid")?;
        let rows = stmt.query_map(params![chat, limit as i64], |r| r.get::<_, String>(0))?;
        rows.map(|r| Ok(serde_json::from_str(&r?)?)).collect()
    }
}
