use rusqlite::{Connection, params};
use serde_json::json;
use verdigris_apps::{api::Chat, contacts::Contacts};

fn fixture() -> Connection {
    let db = Connection::open_in_memory().unwrap();
    db.execute_batch("CREATE TABLE contacts(id INTEGER PRIMARY KEY,full_name TEXT,nickname TEXT,photo_path TEXT); CREATE TABLE phones(phone_norm TEXT,contact_id INTEGER);
        INSERT INTO contacts VALUES(1,'Alex Example','Alex',NULL),(2,'Blair Example',NULL,NULL);
        INSERT INTO phones VALUES('15555550100',1),('15555550200',2);").unwrap();
    db
}

#[test]
fn resolves_formatted_numbers_and_nicknames_without_matching_emails() {
    let contacts = Contacts::from_connection(&fixture()).unwrap();
    for address in ["+1 (555) 555-0100", "5555550100", "tel:+15555550100"] {
        assert_eq!(contacts.name(address), "Alex");
    }
    assert_eq!(
        contacts.name("15555550100@example.com"),
        "15555550100@example.com"
    );
    assert_eq!(contacts.name("+44 5555550100"), "+44 5555550100");
    assert_eq!(contacts.name("555"), "555");
}

#[test]
fn contact_search_returns_addresses_for_explicit_selection() {
    let contacts = Contacts::from_connection(&fixture()).unwrap();
    let results = contacts.search("aLeX", 10);
    assert_eq!(results.len(), 1);
    assert_eq!(results[0].0, "5555550100");
    assert_eq!(results[0].1.name, "Alex");
    assert_eq!(contacts.search("0200", 10)[0].1.name, "Blair Example");
    assert_eq!(contacts.search("", 1).len(), 1);
    assert!(contacts.search("no such contact", 10).is_empty());
}

#[test]
fn preserves_named_groups_and_resolves_unnamed_group_members() {
    let contacts = Contacts::from_connection(&fixture()).unwrap();
    let mut chat: Chat = serde_json::from_value(json!({"guid":"iMessage;+;group", "displayName":"Dinner", "participants":[{"address":"+15555550100"},{"address":"+15555550200"}]})).unwrap();
    assert_eq!(contacts.title(&chat), "Dinner");
    assert!(contacts.chat_photo(&chat).is_none());
    chat.display_name = None;
    assert_eq!(contacts.title(&chat), "Alex, Blair Example");
    let dm: Chat = serde_json::from_value(json!({"guid":"iMessage;-;+15555550100"})).unwrap();
    assert_eq!(contacts.title(&dm), "Alex");
}

#[test]
fn ambiguous_shared_numbers_do_not_select_someone_elses_identity() {
    let db = fixture();
    db.execute("INSERT INTO phones VALUES('5555550100',2)", [])
        .unwrap();
    let contacts = Contacts::from_connection(&db).unwrap();
    assert!(contacts.resolve("+15555550100").is_none());
}

#[test]
fn photos_are_optional_and_removed_files_fall_back_without_mutating_cache() {
    let db = fixture();
    let path = std::env::temp_dir().join(format!("verdigris-contact-{}.svg", uuid::Uuid::new_v4()));
    std::fs::write(&path, "<svg xmlns='http://www.w3.org/2000/svg' width='32' height='32'><circle cx='16' cy='16' r='16' fill='blue'/></svg>").unwrap();
    db.execute(
        "UPDATE contacts SET photo_path=?1 WHERE id=1",
        params![path.to_str()],
    )
    .unwrap();
    let contacts = Contacts::from_connection(&db).unwrap();
    assert_eq!(contacts.photo("5555550100"), Some(path.as_path()));
    assert!(contacts.photo("5555550200").is_none());
    std::fs::remove_file(&path).unwrap();
    let updated = Contacts::from_connection(&db).unwrap();
    assert!(updated.photo("5555550100").is_none());
    assert_eq!(updated.name("5555550100"), "Alex");
    assert_eq!(
        db.query_row("SELECT photo_path FROM contacts WHERE id=1", [], |r| r
            .get::<_, String>(
            0
        ))
        .unwrap(),
        path.to_str().unwrap()
    );
}
