use super::{margins, task, window};
use crate::{
    icloud_push::{self, ChangeDomain, StreamEvent},
    reminders::{Api, Connection, Reminder, Snapshot},
};
use adw::prelude::*;
use gtk::glib;
use std::{
    cell::{Cell, RefCell},
    rc::Rc,
};

const SAFETY_REFRESH_SECONDS: u32 = 15 * 60;

pub(super) fn settings_group(parent: &adw::ApplicationWindow) -> adw::PreferencesGroup {
    let group = adw::PreferencesGroup::builder()
        .title("Reminders and Notes")
        .build();
    let row = adw::ActionRow::builder()
        .title("Reminders and Notes connection")
        .subtitle("Connect to iCloudBridge on your Mac")
        .activatable(true)
        .build();
    row.add_suffix(&gtk::Image::from_icon_name("go-next-symbolic"));
    let parent = parent.downgrade();
    row.connect_activated(move |_| {
        if let Some(parent) = parent.upgrade() {
            connection_dialog(&parent);
        }
    });
    group.add(&row);
    group
}

fn connection_dialog(parent: &adw::ApplicationWindow) {
    let dialog = adw::Dialog::builder()
        .title("Reminders and Notes connection")
        .content_width(540)
        .content_height(460)
        .build();
    let content = gtk::Box::new(gtk::Orientation::Vertical, 0);
    content.append(&adw::HeaderBar::new());
    let page = adw::PreferencesPage::new();
    let group = adw::PreferencesGroup::builder().title("Mac bridge")
        .description("Use cleverdevil/iCloudBridge on your Mac. Select the lists you want to share and start its server.").build();
    let server = adw::EntryRow::builder().title("Server URL").build();
    let token = adw::PasswordEntryRow::builder().title("API token").build();
    match Connection::load() {
        Ok(connection) => server.set_text(&connection.server),
        Err(e) => server.set_tooltip_text(Some(&e.to_string())),
    }
    group.add(&server);
    group.add(&token);
    let status = gtk::Label::new(Some(
        "Use HTTPS or an encrypted tunnel. Tokens stay in your desktop keyring. Leave blank to keep a saved token; localhost tunnels need none.",
    ));
    status.set_wrap(true);
    margins(&status, 12);
    let save = gtk::Button::with_label("Save and connect");
    save.add_css_class("suggested-action");
    margins(&save, 12);
    group.add(&save);
    group.add(&status);
    page.add(&group);
    content.append(&page);
    dialog.set_child(Some(&content));
    save.connect_clicked(move |button| {
        let server_value = server.text().to_string();
        let secret = token.text().to_string();
        let button = button.clone();
        let status = status.clone();
        let token = token.clone();
        let server = server.clone();
        button.set_sensitive(false);
        server.set_sensitive(false);
        token.set_sensitive(false);
        status.set_text("Connecting…");
        task(async move {
            let connection = Connection::normalized(&server_value)?;
            let secret = if secret.is_empty() { connection.token().await? } else { secret };
            let lists = Api::new(&connection, secret.clone())?.lists().await?;
            connection.save_token(&secret).await?;
            connection.save()?;
            Ok(lists.len())
        }, move |result| {
            button.set_sensitive(true);
            server.set_sensitive(true);
            token.set_sensitive(true);
            match result {
                Ok(count) => {
                    token.set_text("");
                    status.set_text(&format!("Connected. {count} lists available. Open Reminders or Notes, then refresh."));
                },
                Err(e) => status.set_text(&e.to_string()),
            }
        });
    });
    dialog.present(Some(parent));
}

struct RemindersUi {
    window: adw::ApplicationWindow,
    list: gtk::ListBox,
    lists: gtk::DropDown,
    search: gtk::SearchEntry,
    completed: gtk::CheckButton,
    status: gtk::Label,
    empty: adw::StatusPage,
    refresh: gtk::Button,
    add: gtk::Button,
    busy: Cell<bool>,
    online: Cell<bool>,
    server: RefCell<String>,
    snapshot: RefCell<Snapshot>,
    list_ids: RefCell<Vec<String>>,
    push_server: RefCell<String>,
    push_cancel: RefCell<Option<tokio::sync::watch::Sender<bool>>>,
    push_dirty: Cell<bool>,
    live_updates: Cell<bool>,
}

pub(super) fn show(app: &adw::Application) {
    let (win, content, header) = window(app, "Reminders", 720, 700);
    let refresh = gtk::Button::from_icon_name("view-refresh-symbolic");
    refresh.set_tooltip_text(Some("Refresh reminders"));
    header.pack_start(&refresh);
    let add = gtk::Button::from_icon_name("list-add-symbolic");
    add.set_tooltip_text(Some("New reminder"));
    header.pack_end(&add);
    let controls = gtk::Box::new(gtk::Orientation::Vertical, 10);
    margins(&controls, 12);
    let search = gtk::SearchEntry::builder()
        .placeholder_text("Search reminders")
        .build();
    controls.append(&search);
    let filters = gtk::Box::new(gtk::Orientation::Horizontal, 12);
    let lists = gtk::DropDown::from_strings(&["All lists"]);
    lists.set_hexpand(true);
    filters.append(&lists);
    let completed = gtk::CheckButton::with_label("Show completed");
    filters.append(&completed);
    controls.append(&filters);
    content.append(&controls);
    let status = gtk::Label::new(None);
    status.set_wrap(true);
    status.set_selectable(true);
    margins(&status, 12);
    content.append(&status);
    let list = gtk::ListBox::new();
    list.set_selection_mode(gtk::SelectionMode::None);
    list.add_css_class("boxed-list");
    margins(&list, 12);
    let empty = adw::StatusPage::builder()
        .icon_name("view-list-symbolic")
        .title("Reminders")
        .description("Connect your Mac in Settings to get started.")
        .build();
    let body = gtk::Box::new(gtk::Orientation::Vertical, 0);
    body.append(&list);
    body.append(&empty);
    let scroll = gtk::ScrolledWindow::builder()
        .vexpand(true)
        .child(&body)
        .hscrollbar_policy(gtk::PolicyType::Never)
        .build();
    content.append(&scroll);
    let ui = Rc::new(RemindersUi {
        window: win,
        list,
        lists,
        search,
        completed,
        status,
        empty,
        refresh,
        add,
        busy: Cell::new(false),
        online: Cell::new(false),
        server: RefCell::new(String::new()),
        snapshot: RefCell::new(Snapshot::default()),
        list_ids: RefCell::new(Vec::new()),
        push_server: RefCell::new(String::new()),
        push_cancel: RefCell::new(None),
        push_dirty: Cell::new(false),
        live_updates: Cell::new(false),
    });
    let weak = Rc::downgrade(&ui);
    ui.refresh.connect_clicked(move |_| {
        if let Some(ui) = weak.upgrade() {
            refresh_data(&ui);
        }
    });
    let weak = Rc::downgrade(&ui);
    ui.add.connect_clicked(move |_| {
        if let Some(ui) = weak.upgrade() {
            edit_dialog(&ui, None);
        }
    });
    let weak = Rc::downgrade(&ui);
    ui.search.connect_search_changed(move |_| {
        if let Some(ui) = weak.upgrade() {
            render(&ui);
        }
    });
    let weak = Rc::downgrade(&ui);
    ui.completed.connect_toggled(move |_| {
        if let Some(ui) = weak.upgrade() {
            render(&ui);
        }
    });
    let weak = Rc::downgrade(&ui);
    ui.lists.connect_selected_notify(move |_| {
        if let Some(ui) = weak.upgrade() {
            render(&ui);
        }
    });
    let weak = Rc::downgrade(&ui);
    let action = gtk::gio::SimpleAction::new("refresh", None);
    action.connect_activate(move |_, _| {
        if let Some(ui) = weak.upgrade() {
            refresh_data(&ui);
        }
    });
    ui.window.add_action(&action);
    app.set_accels_for_action("win.refresh", &["<Control>r"]);
    let weak = Rc::downgrade(&ui);
    let action = gtk::gio::SimpleAction::new("new-reminder", None);
    action.connect_activate(move |_, _| {
        if let Some(ui) = weak.upgrade() {
            edit_dialog(&ui, None);
        }
    });
    ui.window.add_action(&action);
    app.set_accels_for_action("win.new-reminder", &["<Control>n"]);
    let weak = Rc::downgrade(&ui);
    glib::timeout_add_seconds_local(SAFETY_REFRESH_SECONDS, move || {
        let Some(ui) = weak.upgrade() else {
            return glib::ControlFlow::Break;
        };
        request_refresh(&ui);
        glib::ControlFlow::Continue
    });
    let weak = Rc::downgrade(&ui);
    ui.window.connect_close_request(move |_| {
        if let Some(ui) = weak.upgrade() {
            stop_change_stream(&ui);
        }
        glib::Propagation::Proceed
    });
    // Keep the controller alive exactly as long as the window; callbacks above
    // use weak references so the timer does not keep closed windows alive.
    let owner = Rc::new(RefCell::new(Some(ui.clone())));
    ui.window.connect_close_request(move |_| {
        owner.borrow_mut().take();
        glib::Propagation::Proceed
    });
    refresh_data(&ui);
    ui.window.present();
}

fn set_busy(ui: &RemindersUi, busy: bool) {
    ui.busy.set(busy);
    ui.refresh.set_sensitive(!busy);
    ui.add
        .set_sensitive(!busy && ui.online.get() && !ui.snapshot.borrow().lists.is_empty());
    ui.list.set_sensitive(!busy && ui.online.get());
}

fn stop_change_stream(ui: &RemindersUi) {
    if let Some(cancel) = ui.push_cancel.borrow_mut().take() {
        let _ = cancel.send(true);
    }
    ui.push_server.borrow_mut().clear();
    ui.live_updates.set(false);
}

fn ensure_change_stream(ui: &Rc<RemindersUi>, connection: &Connection) {
    if *ui.push_server.borrow() == connection.server {
        return;
    }
    stop_change_stream(ui);
    *ui.push_server.borrow_mut() = connection.server.clone();
    let expected_server = connection.server.clone();
    let (cancel_tx, cancel_rx) = tokio::sync::watch::channel(false);
    *ui.push_cancel.borrow_mut() = Some(cancel_tx);
    let (event_tx, event_rx) = async_channel::unbounded();
    let connection = connection.clone();
    crate::runtime().spawn(async move {
        match connection.token().await {
            Ok(token) => icloud_push::listen(connection, token, cancel_rx, event_tx).await,
            Err(_) => {
                let _ = event_tx.send(StreamEvent::Disconnected).await;
            }
        }
    });
    let weak = Rc::downgrade(ui);
    glib::spawn_future_local(async move {
        while let Ok(event) = event_rx.recv().await {
            let Some(ui) = weak.upgrade() else { break };
            if *ui.server.borrow() != expected_server {
                break;
            }
            match event {
                StreamEvent::Connected => {
                    ui.live_updates.set(true);
                    if ui.online.get() && !ui.busy.get() {
                        ui.status.set_text("Up to date · Live updates active");
                    }
                }
                StreamEvent::Invalidated(ChangeDomain::Reminders) => request_refresh(&ui),
                StreamEvent::Invalidated(ChangeDomain::Notes) => {}
                StreamEvent::Disconnected => {
                    ui.live_updates.set(false);
                    if ui.online.get() && !ui.busy.get() {
                        ui.status.set_text(
                            "Up to date · Live updates reconnecting · Safety refresh every 15 minutes",
                        );
                    }
                }
            }
        }
    });
}

fn request_refresh(ui: &Rc<RemindersUi>) {
    if ui.busy.get() {
        ui.push_dirty.set(true);
    } else {
        refresh_data(ui);
    }
}

fn update_lists(ui: &Rc<RemindersUi>) {
    let selected = ui
        .list_ids
        .borrow()
        .get(ui.lists.selected() as usize)
        .cloned();
    let snapshot = ui.snapshot.borrow();
    let mut names = vec!["All lists".to_string()];
    let mut ids = vec![String::new()];
    for list in &snapshot.lists {
        names.push(list.title.clone());
        ids.push(list.id.clone());
    }
    drop(snapshot);
    let index = selected
        .and_then(|id| ids.iter().position(|item| item == &id))
        .unwrap_or(0);
    *ui.list_ids.borrow_mut() = ids;
    ui.lists.set_model(Some(&gtk::StringList::new(
        &names.iter().map(String::as_str).collect::<Vec<_>>(),
    )));
    ui.lists.set_selected(index as u32);
}

fn refresh_data(ui: &Rc<RemindersUi>) {
    if ui.busy.get() {
        return;
    }
    ui.push_dirty.set(false);
    let connection = match Connection::load().and_then(|c| Connection::normalized(&c.server)) {
        Ok(c) => c,
        Err(e) => {
            stop_change_stream(ui);
            ui.online.set(false);
            ui.server.borrow_mut().clear();
            *ui.snapshot.borrow_mut() = Snapshot::default();
            update_lists(ui);
            render(ui);
            set_busy(ui, false);
            ui.status.set_text(&format!(
                "Open Settings → Reminders and Notes connection. {e}"
            ));
            return;
        }
    };
    if *ui.server.borrow() != connection.server {
        *ui.server.borrow_mut() = connection.server.clone();
        *ui.snapshot.borrow_mut() = crate::config::directory(true)
            .ok()
            .and_then(|dir| Snapshot::load(&connection.cache_path(&dir)).ok())
            .unwrap_or_default();
        ui.online.set(false);
        update_lists(ui);
        render(ui);
    }
    ensure_change_stream(ui, &connection);
    set_busy(ui, true);
    ui.status.set_text("Refreshing…");
    let weak = Rc::downgrade(ui);
    task(
        async move {
            let api = Api::new(&connection, connection.token().await?)?;
            let snapshot = api.snapshot().await?;
            let cache_result = crate::config::directory(true)
                .and_then(|dir| snapshot.save(&connection.cache_path(&dir)));
            Ok((snapshot, cache_result.err().map(|e| e.to_string())))
        },
        move |result| {
            let Some(ui) = weak.upgrade() else {
                return;
            };
            match result {
                Ok((snapshot, cache_error)) => {
                    *ui.snapshot.borrow_mut() = snapshot;
                    ui.online.set(true);
                    update_lists(&ui);
                    ui.status.set_text(if cache_error.is_some() {
                        "Connected, but the offline cache could not be saved."
                    } else if ui.live_updates.get() {
                        "Up to date · Live updates active"
                    } else {
                        "Up to date · Live updates reconnecting · Safety refresh every 15 minutes"
                    });
                }
                Err(e) => {
                    ui.online.set(false);
                    let fetched = ui.snapshot.borrow().fetched_at;
                    let age = glib::DateTime::from_unix_local(fetched as i64)
                        .ok()
                        .and_then(|date| date.format("%b %e, %H:%M").ok())
                        .map(|s| s.to_string());
                    ui.status.set_text(&if fetched > 0 {
                        format!(
                            "Offline · Saved {}. {e} · Reconnect to make changes.",
                            age.unwrap_or_default()
                        )
                    } else {
                        format!("Could not load reminders. {e}")
                    });
                }
            }
            set_busy(&ui, false);
            render(&ui);
            if ui.push_dirty.replace(false) {
                refresh_data(&ui);
            }
        },
    );
}

fn render(ui: &Rc<RemindersUi>) {
    while let Some(row) = ui.list.first_child() {
        ui.list.remove(&row);
    }
    let selected = ui
        .list_ids
        .borrow()
        .get(ui.lists.selected() as usize)
        .cloned()
        .unwrap_or_default();
    let query = ui.search.text().to_lowercase();
    let snapshot = ui.snapshot.borrow();
    let mut count = 0;
    for reminder in &snapshot.reminders {
        if (!selected.is_empty() && selected != reminder.list_id)
            || (!ui.completed.is_active() && reminder.is_completed)
            || (!reminder.title.to_lowercase().contains(&query)
                && !reminder
                    .notes
                    .as_deref()
                    .unwrap_or("")
                    .to_lowercase()
                    .contains(&query))
        {
            continue;
        }
        count += 1;
        let row = adw::ActionRow::builder()
            .title(&reminder.title)
            .use_markup(false)
            .title_lines(2)
            .subtitle_lines(3)
            .build();
        let mut details = Vec::new();
        if let Some(list) = snapshot.lists.iter().find(|l| l.id == reminder.list_id) {
            details.push(list.title.clone());
        }
        if let Some(due) = &reminder.due_date {
            let date = glib::DateTime::from_iso8601(due, None)
                .ok()
                .and_then(|date| date.to_local().ok())
                .and_then(|date| date.format("%b %e, %Y %H:%M").ok());
            details.push(format!(
                "Due {}",
                date.map(|s| s.to_string()).unwrap_or_else(|| due.clone())
            ));
        }
        if let Some(notes) = &reminder.notes {
            if !notes.is_empty() {
                details.push(notes.clone());
            }
        }
        row.set_subtitle(&details.join(" · "));
        let check = gtk::CheckButton::builder()
            .active(reminder.is_completed)
            .valign(gtk::Align::Center)
            .tooltip_text("Mark complete or incomplete")
            .build();
        row.add_prefix(&check);
        let weak = Rc::downgrade(ui);
        let original = reminder.clone();
        check.connect_toggled(move |check| {
            let Some(ui) = weak.upgrade() else {
                return;
            };
            if ui.busy.get() || !ui.online.get() {
                return;
            }
            let completed = check.is_active();
            let id = original.id.clone();
            let server = ui.server.borrow().clone();
            set_busy(&ui, true);
            ui.status.set_text("Saving…");
            let weak = Rc::downgrade(&ui);
            task(
                async move {
                    let api = current_api(&server).await?;
                    api.complete(&id, completed).await
                },
                move |result| {
                    if let Some(ui) = weak.upgrade() {
                        mutation_done(&ui, result);
                        if ui.online.get() {
                            refresh_data(&ui);
                        }
                    }
                },
            );
        });
        let edit = gtk::Button::from_icon_name("document-edit-symbolic");
        edit.set_tooltip_text(Some("Edit title and notes"));
        edit.set_valign(gtk::Align::Center);
        edit.add_css_class("flat");
        let weak = Rc::downgrade(ui);
        let reminder = reminder.clone();
        edit.connect_clicked(move |_| {
            if let Some(ui) = weak.upgrade() {
                edit_dialog(&ui, Some(reminder.clone()));
            }
        });
        row.add_suffix(&edit);
        ui.list.append(&row);
    }
    ui.list.set_visible(count > 0);
    ui.empty.set_visible(count == 0);
    ui.empty.set_title(if snapshot.lists.is_empty() {
        "No lists available"
    } else {
        "No reminders to show"
    });
    ui.empty.set_description(Some(if snapshot.lists.is_empty() {
        "Connect in Settings and select the lists to share in iCloudBridge on your Mac."
    } else {
        "Try another list, change your search, or show completed reminders."
    }));
}

async fn current_api(server: &str) -> anyhow::Result<Api> {
    let connection = Connection::load()?;
    if connection.server != server {
        anyhow::bail!("Connection changed. Close this dialog and refresh.");
    }
    Api::new(&connection, connection.token().await?)
}

fn mutation_done(ui: &Rc<RemindersUi>, result: anyhow::Result<Reminder>) {
    match result {
        Ok(reminder) => {
            let mut snapshot = ui.snapshot.borrow_mut();
            snapshot.reminders.retain(|item| item.id != reminder.id);
            snapshot.reminders.push(reminder);
            let connection = Connection {
                server: ui.server.borrow().clone(),
            };
            let _ = crate::config::directory(true)
                .and_then(|dir| snapshot.save(&connection.cache_path(&dir)));
            drop(snapshot);
            render(ui);
            set_busy(ui, false);
            ui.status.set_text("Saved");
        }
        Err(e) => {
            ui.online.set(false);
            set_busy(ui, false);
            render(ui);
            ui.status.set_text(&format!(
                "{e} · The Mac may have applied the change. Refresh before trying again."
            ));
        }
    }
}

fn edit_dialog(ui: &Rc<RemindersUi>, original: Option<Reminder>) {
    if ui.busy.get() || !ui.online.get() {
        return;
    }
    // Hold refreshes while editing, keeping the chosen account/list stable.
    set_busy(ui, true);
    let dialog = adw::Dialog::builder()
        .title(if original.is_some() {
            "Edit reminder"
        } else {
            "New reminder"
        })
        .content_width(520)
        .content_height(520)
        .build();
    let content = gtk::Box::new(gtk::Orientation::Vertical, 0);
    content.append(&adw::HeaderBar::new());
    let page = adw::PreferencesPage::new();
    let group = adw::PreferencesGroup::new();
    let title = adw::EntryRow::builder().title("Title").build();
    let snapshot = ui.snapshot.borrow();
    let lists: Vec<_> = snapshot
        .lists
        .iter()
        .map(|l| (l.id.clone(), l.title.clone()))
        .collect();
    drop(snapshot);
    let picker = adw::ComboRow::builder()
        .title("List")
        .model(&gtk::StringList::new(
            &lists.iter().map(|l| l.1.as_str()).collect::<Vec<_>>(),
        ))
        .build();
    let selected = original
        .as_ref()
        .map(|r| r.list_id.clone())
        .or_else(|| {
            ui.list_ids
                .borrow()
                .get(ui.lists.selected() as usize)
                .cloned()
        })
        .unwrap_or_default();
    picker.set_selected(lists.iter().position(|l| l.0 == selected).unwrap_or(0) as u32);
    picker.set_sensitive(original.is_none());
    group.add(&picker);
    group.add(&title);
    let notes_label = gtk::Label::builder().label("Notes").xalign(0.0).build();
    margins(&notes_label, 12);
    group.add(&notes_label);
    let notes = gtk::TextView::builder()
        .wrap_mode(gtk::WrapMode::WordChar)
        .top_margin(10)
        .bottom_margin(10)
        .left_margin(10)
        .right_margin(10)
        .build();
    let scroll = gtk::ScrolledWindow::builder()
        .child(&notes)
        .min_content_height(120)
        .build();
    group.add(&scroll);
    if let Some(original) = &original {
        title.set_text(&original.title);
        notes
            .buffer()
            .set_text(original.notes.as_deref().unwrap_or(""));
    }
    let status = gtk::Label::new(None);
    status.set_wrap(true);
    margins(&status, 12);
    group.add(&status);
    let save = gtk::Button::with_label(if original.is_some() {
        "Save"
    } else {
        "Add reminder"
    });
    save.add_css_class("suggested-action");
    margins(&save, 12);
    group.add(&save);
    page.add(&group);
    content.append(&page);
    dialog.set_child(Some(&content));
    let weak = Rc::downgrade(ui);
    dialog.connect_closed(move |_| {
        if let Some(ui) = weak.upgrade() {
            set_busy(&ui, false);
            refresh_data(&ui);
        }
    });
    let weak = Rc::downgrade(ui);
    let weak_dialog = dialog.downgrade();
    let server = ui.server.borrow().clone();
    save.connect_clicked(move |save| {
        let Some(ui) = weak.upgrade() else { return; };
        let Some(dialog) = weak_dialog.upgrade() else { return; };
        let text = title.text().to_string();
        if text.trim().is_empty() { status.set_text("Enter a reminder title."); return; }
        let Some((list, _)) = lists.get(picker.selected() as usize) else { return; };
        let list = list.clone();
        let buffer = notes.buffer();
        let body = buffer.text(&buffer.start_iter(), &buffer.end_iter(), false).to_string();
        let original = original.clone();
        let server = server.clone();
        let save = save.clone();
        let status = status.clone();
        let title = title.clone();
        let notes = notes.clone();
        let picker = picker.clone();
        save.set_sensitive(false);
        title.set_sensitive(false);
        notes.set_sensitive(false);
        picker.set_sensitive(false);
        dialog.set_can_close(false);
        status.set_text("Saving…");
        task(async move {
            let api = current_api(&server).await?;
            match original { Some(original) => api.edit(&original, &text, &body).await,
                None => api.create(&list, &text, &body).await }
        }, move |result| {
            dialog.set_can_close(true);
            match result {
                Ok(reminder) => {
                    mutation_done(&ui, Ok(reminder));
                    dialog.close();
                }
                Err(e) => {
                    ui.online.set(false);
                    title.set_sensitive(true);
                    notes.set_sensitive(true);
                    status.set_text(&format!("{e} · The Mac may have saved this reminder. Copy your draft if needed, then close and refresh before retrying."));
                    // No automatic or repeated POST after an ambiguous failure.
                }
            }
        });
    });
    dialog.present(Some(&ui.window));
}
