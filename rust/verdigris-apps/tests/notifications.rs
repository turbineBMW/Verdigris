use std::{fs, os::unix::fs::PermissionsExt};
use verdigris_apps::notifications::{self, Rule, Rules};

// A separate integration-test process owns these XDG variables. No other test
// in this process accesses the environment or GTK concurrently.
#[test]
fn settings_persist_rules_and_import_independent_icons() {
    let root = std::env::temp_dir().join(format!(
        "verdigris-notification-test-{}",
        uuid::Uuid::new_v4()
    ));
    fs::create_dir_all(&root).unwrap();
    unsafe {
        std::env::set_var("XDG_CONFIG_HOME", root.join("config"));
        std::env::set_var("XDG_DATA_HOME", root.join("data"));
    }
    let data = root.join("data/verdigris");
    fs::create_dir_all(&data).unwrap();
    fs::write(
        data.join("notification-apps.json"),
        br#"{"com.example.chat":"Chat"}"#,
    )
    .unwrap();
    let original = root.join("original.svg");
    fs::write(&original, r##"<svg xmlns="http://www.w3.org/2000/svg" width="400" height="400"><rect width="400" height="400" fill="#20ab8c"/></svg>"##).unwrap();
    let icon = notifications::import_icon(&original).unwrap();
    fs::remove_file(original).unwrap();
    let imported = notifications::icon_path(&icon).unwrap();
    let decoded = gtk::gdk_pixbuf::Pixbuf::from_file(&imported).unwrap();
    assert_eq!((decoded.width(), decoded.height()), (256, 256));
    assert_eq!(
        fs::metadata(imported).unwrap().permissions().mode() & 0o777,
        0o600
    );
    let rule = Rule {
        enabled: false,
        desktop_id: Some("org.example.Chat.desktop".into()),
        icon: Some(icon),
    };
    Rules::save_rule("com.example.chat", rule.clone()).unwrap();
    Rules::save_rule("com.example.other", Rule::default()).unwrap();
    assert_eq!(Rules::load().unwrap().apps["com.example.chat"], rule);
    let apps = notifications::apps().unwrap();
    assert_eq!(apps["com.example.chat"], "Chat");
    assert_eq!(apps["com.example.other"], "com.example.other");
    let path = root.join("config/verdigris/notification-rules.json");
    assert_eq!(
        fs::metadata(&path).unwrap().permissions().mode() & 0o777,
        0o600
    );
    let bytes = fs::read(&path).unwrap();
    let json: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(json["version"], 1);
    assert_eq!(json["apps"]["com.example.chat"]["enabled"], false);
    fs::write(&path, b"invalid").unwrap();
    assert!(Rules::save_rule("com.example.chat", Rule::default()).is_err());
    assert_eq!(fs::read(&path).unwrap(), b"invalid");
    fs::remove_dir_all(root).unwrap();
}
