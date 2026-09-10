use crate::config::Config;
use anyhow::{Context, Result, bail};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Chat {
    pub guid: String,
    pub display_name: Option<String>,
    pub chat_identifier: Option<String>,
    #[serde(default)]
    pub participants: Vec<Value>,
    pub last_message: Option<Message>,
    #[serde(default)]
    pub messages: Vec<Message>,
}
impl Chat {
    pub fn title(&self) -> String {
        self.display_name
            .as_deref()
            .filter(|s| !s.is_empty())
            .map(str::to_owned)
            .or_else(|| {
                let names = self
                    .participants
                    .iter()
                    .filter_map(|p| p["address"].as_str())
                    .collect::<Vec<_>>()
                    .join(", ");
                (!names.is_empty()).then_some(names)
            })
            .or_else(|| self.chat_identifier.clone())
            .unwrap_or_else(|| self.guid.clone())
    }
}
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Message {
    pub guid: String,
    pub text: Option<String>,
    pub date_created: Option<i64>,
    #[serde(default)]
    pub is_from_me: bool,
    pub date_delivered: Option<i64>,
    pub date_read: Option<i64>,
    pub handle: Option<Value>,
    #[serde(default)]
    pub attachments: Vec<Value>,
    #[serde(default)]
    pub chats: Vec<Value>,
    pub associated_message_type: Option<AssociatedMessageType>,
}

// BlueBubbles transforms Apple's numeric association codes into names such as
// "love". Keep both representations so existing cache rows remain readable and
// newly introduced names do not prevent an entire history page from syncing.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum AssociatedMessageType {
    Name(String),
    Code(i64),
}
impl AssociatedMessageType {
    fn is_present(&self) -> bool {
        match self {
            Self::Name(name) => !matches!(name.as_str(), "" | "0" | "none"),
            Self::Code(code) => *code != 0,
        }
    }
}
impl Message {
    pub fn preview(&self) -> String {
        self.text
            .clone()
            .filter(|t| !t.is_empty())
            .unwrap_or_else(|| {
                if !self.attachments.is_empty() {
                    "Attachment".into()
                } else if self
                    .associated_message_type
                    .as_ref()
                    .is_some_and(AssociatedMessageType::is_present)
                {
                    "Reaction".into()
                } else {
                    "Message".into()
                }
            })
    }
}
#[derive(Deserialize)]
pub struct Envelope {
    pub status: u16,
    #[serde(default)]
    pub data: Value,
}

#[derive(Clone)]
pub struct Api {
    pub config: Config,
    password: String,
    http: reqwest::Client,
}
impl Api {
    pub fn new(config: Config, password: String) -> Result<Self> {
        Ok(Self {
            config,
            password,
            http: reqwest::Client::builder()
                .timeout(std::time::Duration::from_secs(45))
                .redirect(reqwest::redirect::Policy::none())
                .build()?,
        })
    }
    pub fn socket_url(&self) -> Result<String> {
        let mut url = reqwest::Url::parse(&self.config.server)?;
        url.query_pairs_mut().append_pair("guid", &self.password);
        Ok(url.to_string())
    }
    pub async fn request(&self, path: &str, body: Option<Value>) -> Result<Value> {
        let url = self.url(&[path])?;
        let req = match body {
            Some(body) => self.http.post(url).json(&body),
            None => self.http.get(url),
        };
        self.json_response(req).await
    }
    pub(crate) fn url(&self, segments: &[&str]) -> Result<reqwest::Url> {
        let mut url = reqwest::Url::parse(&self.config.server)?.join("api/v1/")?;
        for segment in segments {
            // Static route pieces may contain slashes; dynamic identifiers are appended
            // by callers with path_segments_mut so they cannot become query parameters.
            url = url.join(segment)?;
        }
        url.query_pairs_mut()
            .append_pair("password", &self.password);
        Ok(url)
    }
    pub(crate) fn http(&self) -> &reqwest::Client {
        &self.http
    }
    async fn json_response(&self, req: reqwest::RequestBuilder) -> Result<Value> {
        // reqwest errors can contain the password-bearing URL; never expose it.
        let response = req
            .send()
            .await
            .map_err(|e| e.without_url())
            .context("Could not reach the Mac")?;
        if !response.status().is_success() {
            bail!("BlueBubbles returned HTTP {}", response.status().as_u16());
        }
        let envelope: Envelope = response
            .json()
            .await
            .map_err(|e| e.without_url())
            .context("Invalid BlueBubbles response")?;
        if !(200..300).contains(&envelope.status) {
            bail!("BlueBubbles reported error {}", envelope.status);
        }
        Ok(envelope.data)
    }
    pub async fn create_chat(&self, recipient: &str, text: &str, service: &str) -> Result<Chat> {
        let recipient = normalize_recipient(recipient)?;
        if text.trim().is_empty() {
            bail!("Write a message before sending");
        }
        if !matches!(service, "iMessage" | "SMS") {
            bail!("Choose iMessage or SMS");
        }
        if service == "SMS" && recipient.contains('@') {
            bail!("SMS needs a phone number");
        }
        Ok(serde_json::from_value(
            self.request(
                "chat/new",
                Some(json!({
                    "addresses":[recipient], "message":text, "service":service,
                    "method":"apple-script", "tempGuid":uuid::Uuid::new_v4().to_string()
                })),
            )
            .await?,
        )?)
    }
    pub async fn send_attachment(&self, chat: &str, path: &std::path::Path) -> Result<Message> {
        let metadata = tokio::fs::metadata(path)
            .await
            .context("Could not read the selected file")?;
        if !metadata.is_file() {
            bail!("Choose a regular file");
        }
        if metadata.len() > crate::attachments::MAX_BYTES {
            bail!("Choose a file smaller than 100 MiB");
        }
        let name = path
            .file_name()
            .context("Missing filename")?
            .to_string_lossy()
            .to_string();
        let part = reqwest::multipart::Part::file(path)
            .await
            .context("Could not open the selected file")?;
        let form = reqwest::multipart::Form::new()
            .text("chatGuid", chat.to_owned())
            .text("name", name)
            .text("method", "apple-script")
            .text("tempGuid", uuid::Uuid::new_v4().to_string())
            .part("attachment", part);
        let req = self
            .http
            .post(self.url(&["message/attachment"])?)
            .multipart(form)
            .timeout(std::time::Duration::from_secs(180));
        Ok(serde_json::from_value(self.json_response(req).await?)?)
    }
    pub async fn chats(&self) -> Result<Vec<Chat>> {
        let mut all = Vec::new();
        loop {
            let page: Vec<Chat> = serde_json::from_value(self.request("chat/query", Some(json!({
                "limit":100,"offset":all.len(),"with":["participants","lastMessage"],"sort":"lastmessage"
            }))).await?)?;
            let done = page.len() < 100;
            all.extend(page);
            if done {
                return Ok(all);
            }
        }
    }
    pub async fn messages(
        &self,
        chat: Option<&str>,
        after: Option<i64>,
        before: Option<i64>,
        offset: usize,
        limit: usize,
        sort: &str,
    ) -> Result<Vec<Message>> {
        let mut query =
            json!({"limit":limit,"offset":offset,"sort":sort,"with":["chats","attachments"]});
        if let Some(chat) = chat {
            query["chatGuid"] = json!(chat);
        }
        if let Some(after) = after {
            query["after"] = json!(after);
        }
        if let Some(before) = before {
            query["before"] = json!(before);
        }
        Ok(serde_json::from_value(
            self.request("message/query", Some(query)).await?,
        )?)
    }
    pub async fn send(&self, chat: &str, text: &str) -> Result<Message> {
        if text.trim().is_empty() {
            bail!("The message is empty");
        }
        // A failed/timed-out send must not be automatically retried: Apple may have accepted it.
        Ok(serde_json::from_value(self.request("message/text", Some(json!({
            "chatGuid":chat,"message":text,"method":"apple-script","tempGuid":uuid::Uuid::new_v4().to_string()
        }))).await?)?)
    }
}

pub fn normalize_recipient(value: &str) -> Result<String> {
    let value = value.trim().strip_prefix("tel:").unwrap_or(value.trim());
    if value.contains('@') {
        let parts: Vec<_> = value.split('@').collect();
        if parts.len() == 2
            && !parts[0].is_empty()
            && !parts[1].is_empty()
            && !value
                .chars()
                .any(|c| c.is_whitespace() || c.is_control() || ",;<>".contains(c))
        {
            return Ok(value.to_owned());
        }
    } else if value
        .chars()
        .all(|c| c.is_ascii_digit() || "+(). -".contains(c))
    {
        let digits: String = value.chars().filter(char::is_ascii_digit).collect();
        if (7..=15).contains(&digits.len())
            && value.matches('+').count() <= 1
            && (!value.contains('+') || value.starts_with('+'))
        {
            return Ok(format!(
                "{}{digits}",
                if value.starts_with('+') { "+" } else { "" }
            ));
        }
    }
    bail!("Enter one phone number or email address, or choose a contact")
}
