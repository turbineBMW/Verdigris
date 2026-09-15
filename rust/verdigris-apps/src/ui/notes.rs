use super::{launch, margins, task};
use crate::{
    icloud_push::{self, ChangeDomain, StreamEvent},
    notes::{self, Note, Snapshot},
    reminders::{Api, Connection},
};
use adw::prelude::*;
use gtk::glib;
use std::{
    cell::{Cell, RefCell},
    collections::HashMap,
    rc::Rc,
    time::Duration,
};

const SAFETY_REFRESH_SECONDS: u32 = 15 * 60;
const AUTOSAVE_DELAY: Duration = Duration::from_millis(900);

#[derive(Clone)]
struct TextDraft {
    base: notes::Detail,
    title: String,
    body: String,
}

struct NotesUi {
    window: adw::ApplicationWindow,
    rows: gtk::ListBox,
    folders: gtk::DropDown,
    folder_ids: RefCell<Vec<String>>,
    search: gtk::SearchEntry,
    title: gtk::Entry,
    detail: gtk::Label,
    text: gtk::TextView,
    body: gtk::Box,
    detail_request: Cell<u64>,
    detail_pending: Cell<bool>,
    status: gtk::Label,
    add: gtk::Button,
    reload: gtk::Button,
    current: gtk::TextView,
    current_scroll: gtk::ScrolledWindow,
    drafts: RefCell<HashMap<String, TextDraft>>,
    autosave: RefCell<Option<glib::SourceId>>,
    saving_note: RefCell<Option<String>>,
    latest: RefCell<Option<notes::Detail>>,
    rendering_editor: Cell<bool>,
    busy: Cell<bool>,
    online: Cell<bool>,
    server: RefCell<String>,
    selected: RefCell<Option<String>>,
    snapshot: RefCell<Snapshot>,
    push_server: RefCell<String>,
    push_cancel: RefCell<Option<tokio::sync::watch::Sender<bool>>>,
    push_dirty: Cell<bool>,
    live_updates: Cell<bool>,
    split: adw::NavigationSplitView,
}

pub(super) fn show(app: &adw::Application) {
    let win = adw::ApplicationWindow::builder()
        .application(app)
        .title("Notes")
        .default_width(1000)
        .default_height(720)
        .build();

    // Keep the controls and app state in the sidebar header so the sidebar
    // extends all the way to the top edge, matching Verdigris Messages.
    let sidebar_title = adw::WindowTitle::new("Notes", "");
    let sidebar_header = adw::HeaderBar::builder()
        .title_widget(&sidebar_title)
        .build();
    let add = gtk::Button::from_icon_name("document-new-symbolic");
    add.set_tooltip_text(Some("New note"));
    sidebar_header.pack_start(&add);
    let menu_model = gtk::gio::Menu::new();
    menu_model.append(Some("Refresh notes"), Some("win.refresh"));
    menu_model.append(Some("Settings"), Some("win.settings"));
    let menu = gtk::MenuButton::builder()
        .icon_name("open-menu-symbolic")
        .menu_model(&menu_model)
        .tooltip_text("Settings and note actions")
        .build();
    sidebar_header.pack_end(&menu);
    let status = gtk::Label::new(None);
    let header_title = sidebar_title.clone();
    status.connect_label_notify(move |label| {
        header_title.set_subtitle(&label.text());
        header_title.set_tooltip_text(Some(&label.text()));
    });

    let sidebar = gtk::Box::new(gtk::Orientation::Vertical, 10);
    margins(&sidebar, 12);
    let search = gtk::SearchEntry::builder()
        .placeholder_text("Search notes")
        .build();
    sidebar.append(&search);
    let folders = gtk::DropDown::from_strings(&["All folders"]);
    sidebar.append(&folders);
    let rows = gtk::ListBox::new();
    rows.set_selection_mode(gtk::SelectionMode::Single);
    rows.add_css_class("navigation-sidebar");
    let empty = gtk::Label::new(Some("No matching notes"));
    margins(&empty, 20);
    rows.set_placeholder(Some(&empty));
    sidebar.append(
        &gtk::ScrolledWindow::builder()
            .child(&rows)
            .vexpand(true)
            .hscrollbar_policy(gtk::PolicyType::Never)
            .build(),
    );
    let sidebar_view = adw::ToolbarView::new();
    sidebar_view.add_top_bar(&sidebar_header);
    sidebar_view.set_content(Some(&sidebar));
    let sidebar_page = adw::NavigationPage::builder()
        .title("Notes")
        .child(&sidebar_view)
        .build();

    let reader = gtk::Box::new(gtk::Orientation::Vertical, 10);
    margins(&reader, 24);
    let title = gtk::Entry::builder()
        .text("Notes")
        .editable(false)
        .can_focus(false)
        .has_frame(false)
        .build();
    title.add_css_class("title-1");
    reader.append(&title);
    let detail = gtk::Label::builder()
        .label("Choose a note to read it.")
        .xalign(0.0)
        .wrap(true)
        .selectable(true)
        .build();
    detail.add_css_class("dim-label");
    reader.append(&detail);
    let text = gtk::TextView::builder()
        .editable(false)
        .cursor_visible(false)
        .wrap_mode(gtk::WrapMode::WordChar)
        .top_margin(12)
        .bottom_margin(12)
        .build();
    let body = gtk::Box::new(gtk::Orientation::Vertical, 8);
    body.append(&text);
    let reload = gtk::Button::with_label("Review latest Mac version");
    reload.set_visible(false);
    body.append(&reload);
    let current = gtk::TextView::builder()
        .editable(false)
        .cursor_visible(false)
        .wrap_mode(gtk::WrapMode::WordChar)
        .left_margin(12)
        .right_margin(12)
        .build();
    let current_scroll = gtk::ScrolledWindow::builder()
        .child(&current)
        .min_content_height(95)
        .max_content_height(150)
        .build();
    current_scroll.set_visible(false);
    body.append(&current_scroll);
    reader.append(
        &gtk::ScrolledWindow::builder()
            .child(&body)
            .vexpand(true)
            .hscrollbar_policy(gtk::PolicyType::Never)
            .build(),
    );
    let content_view = adw::ToolbarView::new();
    content_view.add_top_bar(&adw::HeaderBar::new());
    content_view.set_content(Some(&reader));
    let content_page = adw::NavigationPage::builder().child(&content_view).build();
    let split = adw::NavigationSplitView::builder()
        .sidebar(&sidebar_page)
        .content(&content_page)
        .min_sidebar_width(260.0)
        .max_sidebar_width(360.0)
        .build();
    win.set_content(Some(&split));
    let breakpoint = adw::Breakpoint::new(adw::BreakpointCondition::new_length(
        adw::BreakpointConditionLengthType::MaxWidth,
        620.0,
        adw::LengthUnit::Sp,
    ));
    breakpoint.add_setter(&split, "collapsed", Some(&true.to_value()));
    win.add_breakpoint(breakpoint);

    let ui = Rc::new(NotesUi {
        window: win,
        rows,
        folders,
        folder_ids: RefCell::new(Vec::new()),
        search,
        title,
        detail,
        text,
        body,
        detail_request: Cell::new(0),
        detail_pending: Cell::new(false),
        status,
        add,
        reload,
        current,
        current_scroll,
        drafts: RefCell::new(HashMap::new()),
        autosave: RefCell::new(None),
        saving_note: RefCell::new(None),
        latest: RefCell::new(None),
        rendering_editor: Cell::new(false),
        busy: Cell::new(false),
        online: Cell::new(false),
        server: RefCell::new(String::new()),
        selected: RefCell::new(None),
        snapshot: RefCell::new(Snapshot::default()),
        push_server: RefCell::new(String::new()),
        push_cancel: RefCell::new(None),
        push_dirty: Cell::new(false),
        live_updates: Cell::new(false),
        split,
    });
    let weak = Rc::downgrade(&ui);
    ui.add.connect_clicked(move |_| {
        if let Some(ui) = weak.upgrade() {
            new_note(&ui);
        }
    });
    let weak = Rc::downgrade(&ui);
    ui.reload.connect_clicked(move |_| {
        if let Some(ui) = weak.upgrade() {
            accept_latest(&ui);
        }
    });
    let weak = Rc::downgrade(&ui);
    ui.search.connect_search_changed(move |_| {
        if let Some(ui) = weak.upgrade() {
            render(&ui);
        }
    });
    let weak = Rc::downgrade(&ui);
    ui.folders.connect_selected_notify(move |_| {
        if let Some(ui) = weak.upgrade() {
            render(&ui);
        }
    });
    let weak = Rc::downgrade(&ui);
    ui.rows.connect_row_selected(move |_, row| {
        if let (Some(ui), Some(row)) = (weak.upgrade(), row) {
            let id = row.widget_name().to_string();
            let changed = ui.selected.borrow().as_deref() != Some(id.as_str());
            if changed {
                let previous = { ui.selected.borrow().clone() };
                cancel_autosave(&ui);
                if let Some(previous) = previous.filter(|id| ui.drafts.borrow().contains_key(id)) {
                    autosave_note(&ui, &previous);
                }
                ui.detail_request
                    .set(ui.detail_request.get().wrapping_add(1));
                ui.detail_pending.set(false);
                *ui.latest.borrow_mut() = None;
            }
            *ui.selected.borrow_mut() = Some(id.clone());
            render_note(&ui);
            load_detail(&ui, false);
            if changed && ui.drafts.borrow().contains_key(&id) {
                schedule_autosave(&ui, id);
            }
        }
    });
    let weak = Rc::downgrade(&ui);
    ui.rows.connect_row_activated(move |_, _| {
        if let Some(ui) = weak.upgrade()
            && ui.selected.borrow().is_some()
        {
            ui.split.set_show_content(true);
        }
    });
    for name in ["refresh", "new-note", "edit-note", "review-note"] {
        let action = gtk::gio::SimpleAction::new(name, None);
        let weak = Rc::downgrade(&ui);
        action.connect_activate(move |_, _| {
            if let Some(ui) = weak.upgrade() {
                if name == "refresh" {
                    refresh_data(&ui);
                } else if name == "edit-note" {
                    focus_editor(&ui);
                } else if name == "review-note" {
                    accept_latest(&ui);
                } else {
                    new_note(&ui);
                }
            }
        });
        ui.window.add_action(&action);
    }
    let settings = gtk::gio::SimpleAction::new("settings", None);
    settings.connect_activate(|_, _| launch("verdigris-settings"));
    ui.window.add_action(&settings);
    app.set_accels_for_action("win.refresh", &["<Control>r"]);
    app.set_accels_for_action("win.new-note", &["<Control>n"]);
    app.set_accels_for_action("win.edit-note", &["<Control>e"]);
    app.set_accels_for_action("win.review-note", &["<Control><Shift>r"]);
    let weak = Rc::downgrade(&ui);
    ui.title.connect_changed(move |_| {
        if let Some(ui) = weak.upgrade() {
            capture_draft(&ui);
        }
    });
    let weak = Rc::downgrade(&ui);
    ui.text.buffer().connect_changed(move |_| {
        if let Some(ui) = weak.upgrade() {
            capture_draft(&ui);
        }
    });
    let weak = Rc::downgrade(&ui);
    glib::timeout_add_seconds_local(SAFETY_REFRESH_SECONDS, move || {
        let Some(ui) = weak.upgrade() else {
            return glib::ControlFlow::Break;
        };
        request_refresh(&ui);
        glib::ControlFlow::Continue
    });
    let weak = Rc::downgrade(&ui);
    ui.window.connect_is_active_notify(move |window| {
        if window.is_active() {
            if let Some(ui) = weak.upgrade() {
                sync_while_active(&ui);
            }
        }
    });
    let weak = Rc::downgrade(&ui);
    ui.window.connect_close_request(move |_| {
        if let Some(ui) = weak.upgrade() {
            stop_change_stream(&ui);
        }
        glib::Propagation::Proceed
    });
    let owner = Rc::new(RefCell::new(Some(ui.clone())));
    ui.window.connect_close_request(move |_| {
        owner.borrow_mut().take();
        glib::Propagation::Proceed
    });
    refresh_data(&ui);
    ui.window.present();
}

fn busy(ui: &NotesUi, value: bool) {
    ui.busy.set(value);
    ui.title.set_sensitive(!value);
    ui.body.set_sensitive(!value);
    ui.add
        .set_sensitive(!value && ui.online.get() && !ui.snapshot.borrow().folders.is_empty());
}

fn sync_while_active(ui: &Rc<NotesUi>) {
    if ui.push_dirty.get() {
        request_refresh(ui);
    } else if ui.busy.get() || ui.saving_note.borrow().is_some() {
        return;
    } else if !ui.online.get() {
        refresh_data(ui);
    } else if ui.selected.borrow().is_some() {
        load_detail(ui, true);
    }
}

fn stop_change_stream(ui: &NotesUi) {
    if let Some(cancel) = ui.push_cancel.borrow_mut().take() {
        let _ = cancel.send(true);
    }
    ui.push_server.borrow_mut().clear();
    ui.live_updates.set(false);
}

fn sync_status_available(ui: &NotesUi) -> bool {
    ui.online.get()
        && !ui.busy.get()
        && ui.saving_note.borrow().is_none()
        && ui.drafts.borrow().is_empty()
}

fn ensure_change_stream(ui: &Rc<NotesUi>, connection: &Connection) {
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
                    if sync_status_available(&ui) {
                        ui.status.set_text("Up to date · Live updates active");
                    }
                }
                StreamEvent::Invalidated(ChangeDomain::Notes) => request_refresh(&ui),
                StreamEvent::Invalidated(ChangeDomain::Reminders) => {}
                StreamEvent::Disconnected => {
                    ui.live_updates.set(false);
                    if sync_status_available(&ui) {
                        ui.status.set_text(
                            "Up to date · Live updates reconnecting · Safety refresh every 15 minutes",
                        );
                    }
                }
            }
        }
    });
}

fn request_refresh(ui: &Rc<NotesUi>) {
    if !ui.window.is_active() || ui.busy.get() || ui.saving_note.borrow().is_some() {
        ui.push_dirty.set(true);
    } else {
        refresh_data(ui);
    }
}

fn refresh_if_pending(ui: &Rc<NotesUi>) -> bool {
    if ui.push_dirty.get()
        && ui.window.is_active()
        && !ui.busy.get()
        && ui.saving_note.borrow().is_none()
    {
        ui.push_dirty.set(false);
        refresh_data(ui);
        true
    } else {
        false
    }
}

fn update_folders(ui: &Rc<NotesUi>) {
    let selected = ui
        .folder_ids
        .borrow()
        .get(ui.folders.selected() as usize)
        .cloned();
    let snapshot = ui.snapshot.borrow();
    let mut ids = vec![String::new()];
    let mut names = vec!["All folders".to_string()];
    for folder in &snapshot.folders {
        ids.push(folder.id.clone());
        names.push(folder.title.clone());
    }
    drop(snapshot);
    let index = selected
        .and_then(|id| ids.iter().position(|v| v == &id))
        .unwrap_or(0);
    *ui.folder_ids.borrow_mut() = ids;
    ui.folders.set_model(Some(&gtk::StringList::new(
        &names.iter().map(String::as_str).collect::<Vec<_>>(),
    )));
    ui.folders.set_selected(index as u32);
}

fn refresh_data(ui: &Rc<NotesUi>) {
    if ui.busy.get() {
        ui.push_dirty.set(true);
        return;
    }
    ui.push_dirty.set(false);
    // A late detail response must not overwrite a newer snapshot (especially
    // one that now marks a note protected or moves to another server).
    ui.detail_request
        .set(ui.detail_request.get().wrapping_add(1));
    ui.detail_pending.set(false);
    let connection = match Connection::load().and_then(|c| Connection::normalized(&c.server)) {
        Ok(c) => c,
        Err(e) => {
            stop_change_stream(ui);
            ui.online.set(false);
            ui.server.borrow_mut().clear();
            *ui.snapshot.borrow_mut() = Snapshot::default();
            update_folders(ui);
            render(ui);
            busy(ui, false);
            ui.status.set_text(&format!(
                "Connect your Mac in Settings → Reminders and Notes connection. {e}"
            ));
            return;
        }
    };
    if *ui.server.borrow() != connection.server {
        *ui.server.borrow_mut() = connection.server.clone();
        *ui.selected.borrow_mut() = None;
        ui.drafts.borrow_mut().clear();
        cancel_autosave(ui);
        *ui.saving_note.borrow_mut() = None;
        *ui.latest.borrow_mut() = None;
        *ui.snapshot.borrow_mut() = crate::config::directory(true)
            .ok()
            .and_then(|dir| Snapshot::load(&Snapshot::cache_path(&connection, &dir)).ok())
            .unwrap_or_default();
        ui.online.set(false);
        update_folders(ui);
        render(ui);
    }
    ensure_change_stream(ui, &connection);
    busy(ui, true);
    ui.status.set_text("Loading notes from your Mac…");
    let weak = Rc::downgrade(ui);
    task(
        async move {
            let api = Api::new(&connection, connection.token().await?)?;
            let snapshot = notes::fetch(&api).await?;
            let cache_error = crate::config::directory(true)
                .and_then(|dir| snapshot.save(&Snapshot::cache_path(&connection, &dir)))
                .is_err();
            Ok((snapshot, cache_error))
        },
        move |result| {
            let Some(ui) = weak.upgrade() else {
                return;
            };
            match result {
                Ok((snapshot, cache_error)) => {
                    let count = snapshot.notes.len();
                    *ui.snapshot.borrow_mut() = snapshot;
                    ui.online.set(true);
                    update_folders(&ui);
                    ui.status.set_text(&if cache_error {
                        "Connected, but the offline copy could not be saved.".to_string()
                    } else if ui.live_updates.get() {
                        format!("{count} notes · Live updates active")
                    } else {
                        format!(
                            "{count} notes · Live updates reconnecting · Safety refresh every 15 minutes"
                        )
                    });
                }
                Err(e) => {
                    ui.online.set(false);
                    let fetched = ui.snapshot.borrow().fetched_at;
                    let saved = glib::DateTime::from_unix_local(fetched as i64)
                        .ok()
                        .and_then(|d| d.format("%b %e, %H:%M").ok())
                        .map(|s| s.to_string())
                        .unwrap_or_default();
                    ui.status.set_text(&if fetched > 0 {
                        format!("Offline · Saved {saved}. {e}")
                    } else {
                        format!("Could not load Notes. {e}")
                    });
                }
            }
            busy(&ui, false);
            render(&ui);
            if refresh_if_pending(&ui) {
                return;
            }
            let selected = { ui.selected.borrow().clone() };
            if let Some(id) = selected {
                resume_autosave(&ui, &id);
                load_detail(&ui, true);
            }
        },
    );
}

fn render(ui: &Rc<NotesUi>) {
    while let Some(row) = ui.rows.first_child() {
        ui.rows.remove(&row);
    }
    let folder = ui
        .folder_ids
        .borrow()
        .get(ui.folders.selected() as usize)
        .cloned()
        .unwrap_or_default();
    let query = ui.search.text();
    let snapshot = ui.snapshot.borrow();
    let old_id = ui.selected.borrow().clone();
    let mut selected_row = None;
    let mut first_row = None;
    for note in &snapshot.notes {
        if (!folder.is_empty() && note.folder_id != folder) || !note.matches(&query) {
            continue;
        }
        let row = adw::ActionRow::builder()
            .title(&note.title)
            .use_markup(false)
            .title_lines(2)
            .subtitle_lines(1)
            .build();
        row.set_widget_name(&note.id);
        row.set_subtitle(
            snapshot
                .folders
                .iter()
                .find(|f| f.id == note.folder_id)
                .map(|f| f.title.as_str())
                .unwrap_or(""),
        );
        if note.locked {
            row.add_prefix(&gtk::Image::from_icon_name("changes-prevent-symbolic"));
        }
        ui.rows.append(&row);
        if first_row.is_none() {
            first_row = Some(row.clone());
        }
        if old_id.as_deref() == Some(&note.id) {
            selected_row = Some(row);
        }
    }
    drop(snapshot);
    if let Some(row) = selected_row.or(first_row) {
        ui.rows.select_row(Some(&row));
    } else {
        *ui.selected.borrow_mut() = None;
        render_note(ui);
    }
}

fn render_note(ui: &Rc<NotesUi>) {
    ui.rendering_editor.set(true);
    ui.reload.set_visible(false);
    ui.current_scroll.set_visible(false);
    ui.title.set_editable(false);
    ui.title.set_can_focus(false);
    ui.title.set_has_frame(false);
    ui.text.set_editable(false);
    ui.text.set_cursor_visible(false);
    while let Some(child) = ui.body.first_child() {
        ui.body.remove(&child);
    }
    ui.body.append(&ui.text);
    ui.text.set_visible(true);
    let snapshot = ui.snapshot.borrow();
    let id = ui.selected.borrow();
    let Some(note) = snapshot.notes.iter().find(|n| Some(&n.id) == id.as_ref()) else {
        ui.title.set_text("Notes");
        ui.detail
            .set_text("Choose a note to read it, or create a new one.");
        ui.text.buffer().set_text("");
        ui.rendering_editor.set(false);
        return;
    };
    let draft = ui.drafts.borrow().get(&note.id).cloned();
    ui.title.set_text(
        draft
            .as_ref()
            .map(|value| value.title.as_str())
            .unwrap_or(&note.title),
    );
    if note.locked {
        ui.detail
            .set_text("Protected note · Open it in Apple Notes on your Mac or iPhone.");
        ui.text.buffer().set_text("");
    } else {
        ui.detail.set_text(&if note.attachments.is_empty() {
            "Text view".to_string()
        } else {
            format!(
                "Text view · Attachments available in Apple Notes: {}",
                note.attachments.join(", ")
            )
        });
        let body = draft
            .as_ref()
            .map(|value| value.body.as_str())
            .unwrap_or_else(|| note_body(note));
        ui.text
            .buffer()
            .set_text(&body.replace('\u{fffc}', "[Attachment]"));
        if let Some(detail) = snapshot.checklists.get(&note.id) {
            if detail.can_edit_text || draft.is_some() {
                let enabled = detail.can_edit_text && ui.online.get() && !ui.busy.get();
                let review_required = draft.as_ref().is_some_and(|draft| {
                    ui.latest.borrow().as_ref().is_some_and(|latest| {
                        latest.note.id == note.id && latest.revision != draft.base.revision
                    })
                });
                ui.title.set_editable(enabled);
                ui.title.set_can_focus(enabled);
                ui.title.set_has_frame(enabled);
                ui.text.set_editable(enabled);
                ui.text.set_cursor_visible(enabled);
                if review_required {
                    ui.reload.set_visible(true);
                    ui.current_scroll.set_visible(true);
                }
                ui.detail.set_text("Editable text note");
            }
            if !detail.items.is_empty() {
                ui.detail.set_text(&format!(
                    "{} of {} complete{}",
                    detail.items.iter().filter(|item| item.checked).count(),
                    detail.items.len(),
                    if note.attachments.is_empty() {
                        String::new()
                    } else {
                        format!(
                            " · Attachments in Apple Notes: {}",
                            note.attachments.join(", ")
                        )
                    }
                ));
                ui.text.set_visible(false);
                let mut cursor = if detail.items[0].location > 0 {
                    note.text
                        .strip_prefix(&format!("{}\n", note.title))
                        .map(|_| note.title.len() + 1)
                        .unwrap_or(0)
                } else {
                    0
                };
                let enabled = ui.online.get() && detail.editable && !ui.busy.get();
                for item in &detail.items {
                    let start = notes::utf16_offset(&note.text, item.location).unwrap_or(cursor);
                    let end = notes::utf16_offset(&note.text, item.location + item.length)
                        .unwrap_or(start);
                    if start > cursor {
                        append_text(&ui.body, &note.text[cursor..start]);
                    }
                    let row = gtk::Box::new(gtk::Orientation::Horizontal, 10);
                    row.set_margin_start((item.indent.min(8) * 18) as i32);
                    let check = gtk::CheckButton::new();
                    check.set_active(item.checked);
                    check.set_sensitive(enabled && item.can_toggle);
                    check.set_tooltip_text(Some(if item.can_toggle {
                        "Mark item complete or incomplete"
                    } else {
                        "Parent checklist items can be changed in Apple Notes"
                    }));
                    row.append(&check);
                    let label = gtk::Label::builder()
                        .label(&item.text)
                        .xalign(0.0)
                        .wrap(true)
                        .hexpand(true)
                        .selectable(true)
                        .build();
                    if item.checked {
                        label.add_css_class("dim-label");
                    }
                    row.append(&label);
                    let edit = gtk::Button::from_icon_name("document-edit-symbolic");
                    edit.add_css_class("flat");
                    edit.set_tooltip_text(Some("Edit item text"));
                    edit.set_sensitive(enabled && item.can_edit);
                    row.append(&edit);
                    let weak = Rc::downgrade(ui);
                    let value = detail.clone();
                    let item_id = item.id.clone();
                    check.connect_toggled(move |check| {
                        if let Some(ui) = weak.upgrade() {
                            toggle_item(&ui, value.clone(), item_id.clone(), check.is_active());
                        }
                    });
                    let weak = Rc::downgrade(ui);
                    let value = detail.clone();
                    let item_id = item.id.clone();
                    edit.connect_clicked(move |_| {
                        if let Some(ui) = weak.upgrade() {
                            edit_item(&ui, value.clone(), item_id.clone());
                        }
                    });
                    ui.body.append(&row);
                    cursor = end + usize::from(note.text.as_bytes().get(end) == Some(&b'\n'));
                }
                if cursor < note.text.len() {
                    append_text(&ui.body, &note.text[cursor..]);
                }
            }
            if let Some(reason) = detail.reason.as_ref().or(detail
                .text_reason
                .as_ref()
                .filter(|_| detail.items.is_empty()))
            {
                ui.detail.set_text(reason);
            }
            if detail.shared {
                ui.detail
                    .set_text(&format!("Shared · {}", ui.detail.text()));
            }
        }
    }
    ui.body.append(&ui.reload);
    ui.body.append(&ui.current_scroll);
    ui.rendering_editor.set(false);
}

fn note_body(note: &Note) -> &str {
    note.text
        .strip_prefix(&format!("{}\n", note.title))
        .unwrap_or(&note.text)
}

fn editor_body(ui: &NotesUi) -> String {
    let buffer = ui.text.buffer();
    buffer
        .text(&buffer.start_iter(), &buffer.end_iter(), false)
        .to_string()
}

fn cancel_autosave(ui: &NotesUi) {
    if let Some(source) = ui.autosave.borrow_mut().take() {
        source.remove();
    }
}

fn schedule_autosave(ui: &Rc<NotesUi>, id: String) {
    cancel_autosave(ui);
    let weak = Rc::downgrade(ui);
    let scheduled_id = id.clone();
    let source = glib::timeout_add_local_once(AUTOSAVE_DELAY, move || {
        let Some(ui) = weak.upgrade() else {
            return;
        };
        ui.autosave.borrow_mut().take();
        autosave_note(&ui, &scheduled_id);
    });
    *ui.autosave.borrow_mut() = Some(source);
}

fn resume_autosave(ui: &Rc<NotesUi>, id: &str) {
    let review_required = ui.latest.borrow().as_ref().is_some_and(|latest| {
        latest.note.id == id
            && ui
                .drafts
                .borrow()
                .get(id)
                .is_some_and(|draft| latest.revision != draft.base.revision)
    });
    if ui.selected.borrow().as_deref() == Some(id)
        && ui.online.get()
        && ui.drafts.borrow().contains_key(id)
        && ui.autosave.borrow().is_none()
        && ui.saving_note.borrow().is_none()
        && !review_required
    {
        schedule_autosave(ui, id.to_string());
    }
}

fn capture_draft(ui: &Rc<NotesUi>) {
    if ui.rendering_editor.get() || ui.busy.get() {
        return;
    }
    let Some(id) = ui.selected.borrow().clone() else {
        return;
    };
    let Some(detail) = ui
        .snapshot
        .borrow()
        .checklists
        .get(&id)
        .cloned()
        .filter(|value| value.can_edit_text)
    else {
        return;
    };
    let title = ui.title.text().to_string();
    let body = editor_body(ui);
    let mut drafts = ui.drafts.borrow_mut();
    let base = drafts
        .get(&id)
        .map(|value| value.base.clone())
        .unwrap_or(detail);
    let save_in_flight = ui.saving_note.borrow().as_deref() == Some(id.as_str());
    if notes::complete_text(&title, &body).is_ok_and(|text| text == base.note.text)
        && !save_in_flight
    {
        drafts.remove(&id);
        drop(drafts);
        cancel_autosave(ui);
        ui.status.set_text("All changes saved.");
    } else {
        drafts.insert(id.clone(), TextDraft { base, title, body });
        let review_required = ui.latest.borrow().as_ref().is_some_and(|latest| {
            latest.note.id == id
                && drafts
                    .get(&id)
                    .is_some_and(|draft| latest.revision != draft.base.revision)
        });
        drop(drafts);
        if review_required {
            cancel_autosave(ui);
            ui.status
                .set_text("Draft preserved · Review the latest Mac version to resume autosave");
        } else {
            schedule_autosave(ui, id);
            ui.status.set_text("Saving changes shortly…");
        }
    }
}

fn focus_editor(ui: &Rc<NotesUi>) {
    if ui.title.is_editable() {
        ui.title.grab_focus();
    } else if ui.text.is_editable() {
        ui.text.grab_focus();
    }
}

fn accept_latest(ui: &Rc<NotesUi>) {
    let Some(id) = ui.selected.borrow().clone() else {
        return;
    };
    let Some(latest) = ui
        .latest
        .borrow()
        .clone()
        .filter(|value| value.note.id == id)
    else {
        return;
    };
    if !latest.can_edit_text {
        ui.status
            .set_text("This note can no longer be edited. Your draft remains available to copy.");
        return;
    }
    if let Some(draft) = ui.drafts.borrow_mut().get_mut(&id) {
        draft.base = latest;
    }
    *ui.latest.borrow_mut() = None;
    ui.reload.set_visible(false);
    ui.current_scroll.set_visible(false);
    ui.status
        .set_text("Latest Mac version reviewed. Your draft will save automatically.");
    schedule_autosave(ui, id);
}

fn append_text(body: &gtk::Box, value: &str) {
    let value = value
        .trim_end_matches('\n')
        .replace('\u{fffc}', "[Attachment]");
    if !value.is_empty() {
        body.append(
            &gtk::Label::builder()
                .label(&value)
                .xalign(0.0)
                .wrap(true)
                .selectable(true)
                .build(),
        );
    }
}

fn store_detail(ui: &Rc<NotesUi>, detail: notes::Detail) {
    let mut snapshot = ui.snapshot.borrow_mut();
    if let Some(note) = snapshot.notes.iter_mut().find(|n| n.id == detail.note.id) {
        *note = detail.note.clone();
    }
    if detail.note.locked {
        snapshot.checklists.remove(&detail.note.id);
    } else {
        snapshot.checklists.insert(detail.note.id.clone(), detail);
    }
    let connection = Connection {
        server: ui.server.borrow().clone(),
    };
    let _ = crate::config::directory(true)
        .and_then(|dir| snapshot.save(&Snapshot::cache_path(&connection, &dir)));
}

fn load_detail(ui: &Rc<NotesUi>, force: bool) {
    if ui.busy.get() || ui.detail_pending.get() || !ui.online.get() {
        return;
    }
    let Some(id) = ui.selected.borrow().clone() else {
        return;
    };
    if ui
        .snapshot
        .borrow()
        .notes
        .iter()
        .any(|n| n.id == id && n.locked)
    {
        return;
    }
    if !force && ui.snapshot.borrow().checklists.contains_key(&id) {
        return;
    }
    let serial = ui.detail_request.get().wrapping_add(1);
    ui.detail_request.set(serial);
    ui.detail_pending.set(true);
    let server = ui.server.borrow().clone();
    let expected_server = server.clone();
    let weak = Rc::downgrade(ui);
    task(
        async move {
            let connection = Connection::load()?;
            if connection.server != server {
                anyhow::bail!("Mac connection changed; refresh first.");
            }
            let api = Api::new(&connection, connection.token().await?)?;
            notes::detail(&api, &id).await
        },
        move |result| {
            let Some(ui) = weak.upgrade() else {
                return;
            };
            if ui.detail_request.get() != serial || *ui.server.borrow() != expected_server {
                return;
            }
            ui.detail_pending.set(false);
            if ui.busy.get() {
                return;
            }
            match result {
                Ok(detail) => {
                    let note_id = detail.note.id.clone();
                    let old = ui
                        .snapshot
                        .borrow()
                        .checklists
                        .get(&detail.note.id)
                        .cloned();
                    let remote_changed = ui
                        .drafts
                        .borrow()
                        .get(&detail.note.id)
                        .is_some_and(|draft| detail.revision != draft.base.revision);
                    if remote_changed {
                        ui.current.buffer().set_text(&detail.note.text);
                        *ui.latest.borrow_mut() = Some(detail.clone());
                        ui.status.set_text(
                            "This note changed on your Mac. Your draft is preserved; review the latest version before saving.",
                        );
                    } else if ui
                        .latest
                        .borrow()
                        .as_ref()
                        .is_some_and(|value| value.note.id == detail.note.id)
                    {
                        *ui.latest.borrow_mut() = None;
                    }
                    let changed = old.as_ref() != Some(&detail);
                    if changed {
                        store_detail(&ui, detail);
                        if remote_changed {
                            render_note(&ui);
                        } else {
                            render(&ui);
                        }
                    }
                    resume_autosave(&ui, &note_id);
                }
                Err(_) => { /* Keep the ordinary reader usable with older bridges/offline. */ }
            }
        },
    );
}

fn autosave_note(ui: &Rc<NotesUi>, id: &str) {
    if ui.selected.borrow().as_deref() != Some(id) {
        return;
    }
    if ui.busy.get() || ui.saving_note.borrow().is_some() {
        schedule_autosave(ui, id.to_string());
        return;
    }
    if !ui.online.get() {
        ui.status
            .set_text("Offline · Changes are kept locally until this note reconnects");
        return;
    }
    let Some(draft) = ui.drafts.borrow().get(id).cloned() else {
        return;
    };
    if ui
        .latest
        .borrow()
        .as_ref()
        .is_some_and(|latest| latest.note.id == id && latest.revision != draft.base.revision)
    {
        ui.status
            .set_text("Review the latest Mac version before saving this draft.");
        return;
    }
    if let Err(error) = notes::complete_text(&draft.title, &draft.body) {
        ui.status.set_text(&error.to_string());
        return;
    }
    *ui.saving_note.borrow_mut() = Some(id.to_string());
    ui.detail_request
        .set(ui.detail_request.get().wrapping_add(1));
    ui.detail_pending.set(false);
    ui.status.set_text("Saving note…");
    let server = ui.server.borrow().clone();
    let expected_server = server.clone();
    let weak = Rc::downgrade(ui);
    let sent = draft.clone();
    let note_id = id.to_string();
    task(
        async move {
            let connection = Connection::load()?;
            if connection.server != server {
                anyhow::bail!("Mac connection changed. Refresh first.");
            }
            let api = Api::new(&connection, connection.token().await?)?;
            notes::update_text(&api, &draft.base, &draft.title, &draft.body).await
        },
        move |result| {
            let Some(ui) = weak.upgrade() else {
                return;
            };
            if *ui.server.borrow() != expected_server {
                return;
            }
            *ui.saving_note.borrow_mut() = None;
            match result {
                Ok(detail) => {
                    let newer_changes =
                        ui.drafts.borrow().get(&note_id).is_some_and(|draft| {
                            draft.title != sent.title || draft.body != sent.body
                        });
                    if newer_changes {
                        if let Some(draft) = ui.drafts.borrow_mut().get_mut(&note_id) {
                            draft.base = detail.clone();
                        }
                    } else {
                        ui.drafts.borrow_mut().remove(&note_id);
                    }
                    if ui
                        .latest
                        .borrow()
                        .as_ref()
                        .is_some_and(|value| value.note.id == note_id)
                    {
                        *ui.latest.borrow_mut() = None;
                    }
                    store_detail(&ui, detail);
                    if newer_changes {
                        if ui.selected.borrow().as_deref() == Some(note_id.as_str()) {
                            ui.status.set_text("Saving newer changes shortly…");
                            schedule_autosave(&ui, note_id);
                        }
                    } else if ui.selected.borrow().as_deref() == Some(note_id.as_str()) {
                        ui.status.set_text("Saved to your Mac.");
                        render(&ui);
                    }
                }
                Err(error) => {
                    ui.status.set_text(&format!(
                        "{error} · Your draft is preserved while the latest Mac version loads."
                    ));
                    if ui.selected.borrow().as_deref() == Some(note_id.as_str()) {
                        render_note(&ui);
                        load_detail(&ui, true);
                    }
                }
            }
            refresh_if_pending(&ui);
        },
    );
}

fn toggle_item(ui: &Rc<NotesUi>, detail: notes::Detail, item: String, checked: bool) {
    if ui.busy.get() || !ui.online.get() {
        return;
    }
    busy(ui, true);
    ui.detail_request
        .set(ui.detail_request.get().wrapping_add(1));
    ui.detail_pending.set(false);
    ui.status.set_text("Saving checklist change…");
    let weak = Rc::downgrade(ui);
    let server = ui.server.borrow().clone();
    task(
        async move {
            let connection = Connection::load()?;
            if connection.server != server {
                anyhow::bail!("Mac connection changed. Refresh first.");
            }
            let api = Api::new(&connection, connection.token().await?)?;
            notes::update_item(&api, &detail, &item, Some(checked), None).await
        },
        move |result| {
            let Some(ui) = weak.upgrade() else {
                return;
            };
            match result {
                Ok(detail) => {
                    store_detail(&ui, detail);
                    ui.status
                        .set_text("Checklist saved and verified on your Mac.");
                }
                Err(error) => {
                    ui.online.set(false);
                    ui.status
                        .set_text(&format!("{error} · Refresh before another edit."));
                }
            }
            busy(&ui, false);
            render_note(&ui);
            refresh_if_pending(&ui);
        },
    );
}

fn edit_item(ui: &Rc<NotesUi>, detail: notes::Detail, item_id: String) {
    if ui.busy.get() || !ui.online.get() {
        return;
    }
    let Some(item) = detail.items.iter().find(|i| i.id == item_id) else {
        return;
    };
    busy(ui, true);
    ui.detail_request
        .set(ui.detail_request.get().wrapping_add(1));
    ui.detail_pending.set(false);
    let dialog = adw::Dialog::builder()
        .title("Edit checklist item")
        .content_width(580)
        .build();
    let content = gtk::Box::new(gtk::Orientation::Vertical, 12);
    content.append(&adw::HeaderBar::new());
    let entry = gtk::Entry::builder().text(&item.text).build();
    margins(&entry, 12);
    content.append(&entry);
    let status = gtk::Label::builder()
        .wrap(true)
        .selectable(true)
        .xalign(0.0)
        .build();
    margins(&status, 12);
    content.append(&status);
    let reload = gtk::Button::with_label("Reload current version");
    reload.set_visible(false);
    margins(&reload, 12);
    content.append(&reload);
    let save = gtk::Button::with_label("Save text");
    save.add_css_class("suggested-action");
    margins(&save, 12);
    content.append(&save);
    dialog.set_child(Some(&content));
    let base = Rc::new(RefCell::new(detail));
    let server = ui.server.borrow().clone();
    let weak = Rc::downgrade(ui);
    dialog.connect_closed(move |_| {
        if let Some(ui) = weak.upgrade() {
            busy(&ui, false);
            if !refresh_if_pending(&ui) {
                sync_while_active(&ui);
            }
        }
    });
    let weak = Rc::downgrade(ui);
    let weak_dialog = dialog.downgrade();
    let base_save = base.clone();
    let entry_save = entry.clone();
    let status_save = status.clone();
    let reload_save = reload.clone();
    let server_save = server.clone();
    let id_save = item_id.clone();
    save.connect_clicked(move |save| {
        let (Some(ui), Some(dialog)) = (weak.upgrade(), weak_dialog.upgrade()) else { return; };
        let text = entry_save.text().to_string();
        if text.is_empty() || text.len() >= 4096 || text.chars().any(|c| c.is_control() || ['\u{2028}','\u{2029}','\u{fffc}'].contains(&c)) {
            status_save.set_text("Use a single-line item under 4 KiB."); return;
        }
        save.set_sensitive(false); reload_save.set_sensitive(false); entry_save.set_sensitive(false); dialog.set_can_close(false);
        status_save.set_text("Saving item…");
        let value = base_save.borrow().clone(); let server = server_save.clone(); let item_id = id_save.clone();
        let entry = entry_save.clone(); let status = status_save.clone(); let reload = reload_save.clone();
        task(async move {
            let connection = Connection::load()?; if connection.server != server { anyhow::bail!("Mac connection changed. Close and refresh first."); }
            let api = Api::new(&connection, connection.token().await?)?;
            notes::update_item(&api, &value, &item_id, None, Some(&text)).await
        }, move |result| {
            dialog.set_can_close(true); entry.set_sensitive(true);
            match result {
                Ok(value) => { store_detail(&ui, value); dialog.close(); }
                Err(error) => {
                    status.set_text(&format!("{error}\nYour draft is still here. Reload to review the current item before saving again."));
                    reload.set_visible(true); reload.set_sensitive(true);
                }
            }
        });
    });
    let weak_dialog = dialog.downgrade();
    reload.connect_clicked(move |reload| {
        let Some(dialog) = weak_dialog.upgrade() else { return; };
        reload.set_sensitive(false); save.set_sensitive(false); dialog.set_can_close(false);
        let id = base.borrow().note.id.clone(); let server = server.clone();
        let base = base.clone(); let status = status.clone(); let save = save.clone(); let reload = reload.clone(); let item_id = item_id.clone();
        task(async move {
            let connection = Connection::load()?; if connection.server != server { anyhow::bail!("Mac connection changed. Close and refresh first."); }
            let api = Api::new(&connection, connection.token().await?)?; notes::detail(&api, &id).await
        }, move |result| {
            dialog.set_can_close(true); reload.set_sensitive(true);
            match result {
                Ok(value) => {
                    if let Some(item) = value.items.iter().find(|i| i.id == item_id && i.can_edit) {
                        status.set_text(&format!("Current item: {}\nYour draft above is unchanged. Review it, then save if you still want to apply it.", item.text));
                        save.set_sensitive(value.editable); *base.borrow_mut() = value;
                    } else { status.set_text("This item was removed or can no longer be edited. Your draft is still available to copy."); }
                }
                Err(error) => status.set_text(&format!("{error} · Your draft is unchanged.")),
            }
        });
    });
    dialog.present(Some(&ui.window));
}

fn new_note(ui: &Rc<NotesUi>) {
    if ui.busy.get() || !ui.online.get() || ui.snapshot.borrow().folders.is_empty() {
        return;
    }
    busy(ui, true);
    let dialog = adw::Dialog::builder()
        .title("New note")
        .content_width(600)
        .content_height(590)
        .build();
    let content = gtk::Box::new(gtk::Orientation::Vertical, 0);
    content.append(&adw::HeaderBar::new());
    let page = adw::PreferencesPage::new();
    let group = adw::PreferencesGroup::new();
    let folders = ui.snapshot.borrow().folders.clone();
    let picker = adw::ComboRow::builder()
        .title("Folder")
        .model(&gtk::StringList::new(
            &folders.iter().map(|f| f.title.as_str()).collect::<Vec<_>>(),
        ))
        .build();
    let current = ui
        .folder_ids
        .borrow()
        .get(ui.folders.selected() as usize)
        .cloned()
        .unwrap_or_default();
    let preferred = if current.is_empty() {
        ui.snapshot
            .borrow()
            .default_folder_id
            .clone()
            .unwrap_or_default()
    } else {
        current
    };
    picker.set_selected(folders.iter().position(|f| f.id == preferred).unwrap_or(0) as u32);
    group.add(&picker);
    let title = adw::EntryRow::builder().title("Title").build();
    group.add(&title);
    let text = gtk::TextView::builder()
        .wrap_mode(gtk::WrapMode::WordChar)
        .top_margin(12)
        .bottom_margin(12)
        .left_margin(12)
        .right_margin(12)
        .build();
    group.add(
        &gtk::ScrolledWindow::builder()
            .child(&text)
            .min_content_height(240)
            .build(),
    );
    let status = gtk::Label::new(None);
    status.set_wrap(true);
    margins(&status, 12);
    group.add(&status);
    let save = gtk::Button::with_label("Create note");
    save.add_css_class("suggested-action");
    margins(&save, 12);
    group.add(&save);
    page.add(&group);
    content.append(&page);
    dialog.set_child(Some(&content));
    let weak = Rc::downgrade(ui);
    dialog.connect_closed(move |_| {
        if let Some(ui) = weak.upgrade() {
            busy(&ui, false);
            if !refresh_if_pending(&ui) {
                sync_while_active(&ui);
            }
        }
    });
    let weak = Rc::downgrade(ui);
    let weak_dialog = dialog.downgrade();
    let server = ui.server.borrow().clone();
    save.connect_clicked(move |save| {
        let (Some(ui), Some(dialog)) = (weak.upgrade(), weak_dialog.upgrade()) else { return; };
        let title_value = title.text().to_string(); if title_value.trim().is_empty() { status.set_text("Enter a title."); return; }
        let Some(folder) = folders.get(picker.selected() as usize) else { return; }; let folder = folder.id.clone();
        let buffer = text.buffer(); let body = buffer.text(&buffer.start_iter(), &buffer.end_iter(), false).to_string();
        if body.len() > 1_000_000 { status.set_text("Keep the note under 1 MB."); return; }
        let server = server.clone(); let status = status.clone(); let title = title.clone(); let text = text.clone(); let picker = picker.clone();
        save.set_sensitive(false); title.set_sensitive(false); text.set_sensitive(false); picker.set_sensitive(false);
        dialog.set_can_close(false); status.set_text("Creating note…");
        task(async move {
            let connection = Connection::load()?; if connection.server != server { anyhow::bail!("Mac connection changed. Close and refresh first."); }
            let api = Api::new(&connection, connection.token().await?)?; notes::create(&api, &folder, &title_value, &body).await
        }, move |result: anyhow::Result<Note>| {
            dialog.set_can_close(true);
            match result {
                Ok(note) => {
                    *ui.selected.borrow_mut() = Some(note.id.clone());
                    ui.snapshot.borrow_mut().notes.insert(0, note); ui.search.set_text(""); ui.folders.set_selected(0);
                    let connection = Connection { server: ui.server.borrow().clone() };
                    let _ = crate::config::directory(true).and_then(|dir| ui.snapshot.borrow().save(&Snapshot::cache_path(&connection, &dir)));
                    render(&ui); ui.split.set_show_content(true); dialog.close();
                }
                Err(e) => {
                    ui.online.set(false); title.set_sensitive(true); text.set_sensitive(true);
                    status.set_text(&format!("{e} · The Mac may have created the note. Copy your draft if needed, then close and refresh before retrying."));
                }
            }
        });
    });
    dialog.present(Some(&ui.window));
}
