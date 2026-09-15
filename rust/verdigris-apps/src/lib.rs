pub mod api;
pub mod attachments;
pub mod config;
pub mod contacts;
pub mod gif;
pub mod icloud_push;
pub mod notes;
pub mod notifications;
pub mod reminders;
pub mod service;
pub mod store;
pub mod ui;

pub fn runtime() -> &'static tokio::runtime::Runtime {
    static RT: std::sync::OnceLock<tokio::runtime::Runtime> = std::sync::OnceLock::new();
    RT.get_or_init(|| tokio::runtime::Runtime::new().expect("Tokio runtime"))
}
