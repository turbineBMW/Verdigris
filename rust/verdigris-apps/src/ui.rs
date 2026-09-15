use crate::{
    api::{Api, Chat, Message},
    config::Config,
    contacts::Contacts,
    service,
};
use adw::prelude::*;
use anyhow::Result;
use gtk::glib;
use std::{
    cell::{Cell, RefCell},
    rc::Rc,
};
mod accent;
mod compose;
mod gif_picker;
mod layout;
mod media;
mod notes;
mod notifications;
mod reminders;

fn error_text(error: &anyhow::Error) -> String {
    if let Some(zbus::Error::MethodError(_, Some(message), _)) = error.downcast_ref::<zbus::Error>()
    {
        message.clone()
    } else {
        error.to_string()
    }
}

fn avatar(name: &str, photo: Option<&std::path::Path>, size: i32) -> adw::Avatar {
    let avatar = adw::Avatar::new(size, Some(name), true);
    if let Some(photo) = photo
        && let Ok(pixbuf) =
            gtk::gdk_pixbuf::Pixbuf::from_file_at_scale(photo, size * 2, size * 2, true)
    {
        avatar.set_custom_image(Some(&gtk::gdk::Texture::for_pixbuf(&pixbuf)));
    }
    avatar
}

/// Send blocking/network work to Tokio; GTK objects stay on the GLib thread.
fn task<T: Send + 'static>(
    future: impl std::future::Future<Output = Result<T>> + Send + 'static,
    done: impl FnOnce(Result<T>) + 'static,
) {
    let (tx, rx) = async_channel::bounded(1);
    crate::runtime().spawn(async move {
        let _ = tx.send(future.await).await;
    });
    glib::spawn_future_local(async move {
        if let Ok(value) = rx.recv().await {
            done(value);
        }
    });
}
fn margins(widget: &impl IsA<gtk::Widget>, size: i32) {
    widget.set_margin_top(size);
    widget.set_margin_bottom(size);
    widget.set_margin_start(size);
    widget.set_margin_end(size);
}
fn launch(name: &str) {
    if let Ok(path) = std::env::current_exe() {
        let _ = std::process::Command::new(path.with_file_name(name)).spawn();
    }
}
fn window(
    app: &adw::Application,
    title: &str,
    width: i32,
    height: i32,
) -> (adw::ApplicationWindow, gtk::Box, adw::HeaderBar) {
    let win = adw::ApplicationWindow::builder()
        .application(app)
        .title(title)
        .default_width(width)
        .default_height(height)
        .build();
    let content = gtk::Box::new(gtk::Orientation::Vertical, 0);
    let header = adw::HeaderBar::new();
    if title != "Settings" {
        let settings = gtk::Button::from_icon_name("emblem-system-symbolic");
        settings.set_tooltip_text(Some("Settings"));
        settings.connect_clicked(|_| launch("verdigris-settings"));
        header.pack_end(&settings);
    }
    content.append(&header);
    win.set_content(Some(&content));
    (win, content, header)
}

pub fn run(kind: &'static str) {
    let app = adw::Application::builder()
        .application_id(format!("dev.turbinebmw.Verdigris.{kind}"))
        .build();
    app.connect_activate(move |app| {
        if matches!(kind, "Messages" | "Phone" | "Reminders" | "Notes") {
            gtk::Window::set_default_icon_name(&format!("dev.turbinebmw.Verdigris.{kind}"));
        }
        if let Some(window) = app.active_window() {
            window.present();
            return;
        }
        let css = gtk::CssProvider::new();
        css.load_from_string(include_str!("ui/messages.css"));
        if let Some(display) = gtk::gdk::Display::default() {
            if kind == "Messages" {
                accent::install_fallback(&display);
            }
            gtk::style_context_add_provider_for_display(
                &display,
                &css,
                gtk::STYLE_PROVIDER_PRIORITY_APPLICATION,
            );
        }
        match kind {
            "Settings" => settings(app),
            "Phone" => phone(app),
            "Reminders" => reminders::show(app),
            "Notes" => notes::show(app),
            _ => messages(app),
        }
    });
    app.run();
}

async fn sync_connection() -> Result<zbus::Connection> {
    let connection = zbus::connection::Builder::session()?
        .method_timeout(std::time::Duration::from_secs(195))
        .build()
        .await?;
    let dbus = zbus::fdo::DBusProxy::new(&connection).await?;
    if !dbus.name_has_owner(service::NAME.try_into()?).await? {
        if dbus
            .start_service_by_name(service::NAME.try_into()?, 0)
            .await
            .is_ok()
        {
            return Ok(connection);
        }
        let binary = std::env::current_exe()?.with_file_name("verdigris-sync");
        let _ = std::process::Command::new(binary)
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .spawn()?;
        for _ in 0..40 {
            if dbus.name_has_owner(service::NAME.try_into()?).await? {
                return Ok(connection);
            }
            tokio::time::sleep(std::time::Duration::from_millis(100)).await;
        }
        anyhow::bail!("The message service could not start");
    }
    Ok(connection)
}

fn settings(app: &adw::Application) {
    let (win, content, _) = window(app, "Settings", 600, 720);
    let page = adw::PreferencesPage::new();
    let group = adw::PreferencesGroup::builder()
        .title("Mac connection")
        .description("Connect to BlueBubbles on your Mac.")
        .build();
    let server = adw::EntryRow::builder().title("Server URL").build();
    let password = adw::PasswordEntryRow::builder()
        .title("Server password")
        .build();
    if let Ok(config) = Config::load() {
        server.set_text(&config.server);
    }
    group.add(&server);
    group.add(&password);
    let save = gtk::Button::with_label("Save and connect");
    save.add_css_class("suggested-action");
    margins(&save, 12);
    group.add(&save);
    let status = gtk::Label::new(Some(
        "Password is stored in your desktop keyring. Leave it blank to keep the saved password.",
    ));
    status.set_wrap(true);
    margins(&status, 12);
    group.add(&status);
    page.add(&group);
    let phone_group = adw::PreferencesGroup::builder().title("iPhone connection").description("Phone calls and Bluetooth notifications use the existing Verdigris service. Pairing and audio setup are still managed by Verdigris's CLI in this first version.").build();
    let check = gtk::Button::with_label("Check Bluetooth service");
    margins(&check, 12);
    phone_group.add(&check);
    let health = gtk::Label::new(None);
    health.set_wrap(true);
    phone_group.add(&health);
    page.add(&phone_group);
    page.add(&notifications::settings_group(&win));
    page.add(&reminders::settings_group(&win));
    check.connect_clicked(move |_| {
        let health = health.clone();
        task(
            async {
                let bus = zbus::Connection::session().await?;
                let p = service::bridge_proxy(&bus, "dev.turbinebmw.Verdigris.Bridge.Messages1")
                    .await?;
                Ok(p.call::<_, _, bool>("IsHealthy", &()).await?)
            },
            move |r| {
                health.set_text(match r {
                    Ok(true) => "iPhone message connection is active",
                    Ok(false) => "Verdigris is running; iPhone message connection is unavailable",
                    Err(_) => "Verdigris service is unavailable. Start verdigris.service.",
                });
            },
        );
    });
    save.connect_clicked(move |button| {
        let server_value = server.text().to_string();
        let secret = password.text().to_string();
        let button = button.clone();
        button.set_sensitive(false);
        status.set_text("Connecting…");
        let status = status.clone();
        let password = password.clone();
        task(
            async move {
                let config = Config::normalized(&server_value)?;
                let secret = if secret.is_empty() {
                    config.password().await?
                } else {
                    secret
                };
                Api::new(config.clone(), secret.clone())?
                    .request("server/info", None)
                    .await?;
                config.save_password(&secret).await?;
                config.save()?;
                let bus = sync_connection().await?;
                service::proxy(&bus)
                    .await?
                    .call::<_, _, ()>("Reload", &())
                    .await?;
                Ok(())
            },
            move |result| {
                button.set_sensitive(true);
                match result {
                    Ok(()) => {
                        status.set_text("Connected. Messages will now sync from your Mac.");
                        password.set_text("");
                    }
                    Err(e) => status.set_text(&error_text(&e)),
                }
            },
        );
    });
    content.append(&page);
    win.present();
}

struct MessagesUi {
    chats: gtk::ListBox,
    thread: gtk::ListBox,
    title: adw::WindowTitle,
    status: gtk::Label,
    entry: gtk::TextView,
    send: gtk::Button,
    selected: RefCell<Option<String>>,
    rows: RefCell<Vec<Chat>>,
    contacts: RefCell<Contacts>,
    limit: Cell<u32>,
    sending: Cell<bool>,
    loading: Cell<bool>,
    pending: Cell<bool>,
    drafts: RefCell<std::collections::HashMap<String, String>>,
    server: RefCell<String>,
    scroll: gtk::ScrolledWindow,
    search: gtk::SearchEntry,
    attach: gtk::Button,
    window: glib::WeakRef<adw::ApplicationWindow>,
    side_stack: gtk::Stack,
    content_stack: gtk::Stack,
    composer: gtk::Box,
    split: adw::NavigationSplitView,
    rendered: RefCell<String>,
    stick_bottom: Cell<bool>,
    restoring_rows: Cell<bool>,
}
fn clear(widget: &gtk::Box) {
    while let Some(child) = widget.first_child() {
        widget.remove(&child);
    }
}
fn entry_text(ui: &MessagesUi) -> String {
    let b = ui.entry.buffer();
    b.text(&b.start_iter(), &b.end_iter(), true).to_string()
}
fn clear_thread(ui: &MessagesUi) {
    while let Some(child) = ui.thread.first_child() {
        ui.thread.remove(&child);
    }
    ui.rendered.borrow_mut().clear();
}
fn render_thread(ui: &MessagesUi, messages: &[Message]) {
    ui.content_stack.set_visible_child_name("thread");
    let serialized = serde_json::to_string(messages).unwrap_or_default();
    if *ui.rendered.borrow() == serialized {
        return;
    }
    clear_thread(ui);
    *ui.rendered.borrow_mut() = serialized;
    if messages.is_empty() {
        let empty = gtk::Label::new(Some("No cached messages yet"));
        empty.add_css_class("dim-label");
        ui.thread.append(&empty);
    }
    let group = ui
        .rows
        .borrow()
        .iter()
        .find(|c| Some(&c.guid) == ui.selected.borrow().as_ref())
        .is_some_and(|c| c.participants.len() > 1);
    for message in messages {
        let align = if message.is_from_me {
            gtk::Align::End
        } else {
            gtk::Align::Start
        };
        let col = gtk::Box::builder()
            .orientation(gtk::Orientation::Vertical)
            .spacing(4)
            .halign(align)
            .build();
        let bubble = gtk::Box::builder()
            .orientation(gtk::Orientation::Vertical)
            .spacing(6)
            .css_classes([
                "verdigris-bubble",
                if message.is_from_me {
                    "verdigris-me"
                } else {
                    "verdigris-them"
                },
            ])
            .halign(align)
            .build();
        if group
            && !message.is_from_me
            && let Some(address) = message.handle.as_ref().and_then(|h| h["address"].as_str())
        {
            let contacts = ui.contacts.borrow();
            let sender = gtk::Box::new(gtk::Orientation::Horizontal, 6);
            sender.append(&avatar(
                &contacts.name(address),
                contacts.photo(address),
                24,
            ));
            sender.append(
                &gtk::Label::builder()
                    .label(contacts.name(address))
                    .xalign(0.0)
                    .css_classes(["caption", "heading"])
                    .build(),
            );
            col.append(&sender);
        }
        for attachment in &message.attachments {
            let widget = media::attachment_widget(ui, attachment);
            if attachment["mimeType"]
                .as_str()
                .is_some_and(|m| m.starts_with("image/"))
            {
                widget.set_halign(align);
                col.append(&widget);
            } else {
                bubble.append(&widget);
            }
        }
        let text = if message.attachments.is_empty() {
            message.preview()
        } else {
            message.text.clone().unwrap_or_default()
        };
        if !text.trim().is_empty() {
            bubble.append(
                &gtk::Label::builder()
                    .label(&text)
                    .wrap(true)
                    .wrap_mode(gtk::pango::WrapMode::WordChar)
                    .max_width_chars(60)
                    .xalign(0.0)
                    .selectable(true)
                    .build(),
            );
        }
        if bubble.first_child().is_some() {
            col.append(&bubble);
        }
        let receipt = if message.is_from_me {
            if message.date_read.unwrap_or(0) > 0 {
                " · read"
            } else if message.date_delivered.unwrap_or(0) > 0 {
                " · delivered"
            } else {
                " · sent"
            }
        } else {
            ""
        };
        col.append(
            &gtk::Label::builder()
                .label(format!(
                    "{}{receipt}",
                    layout::fmt_time(message.date_created)
                ))
                .css_classes(["verdigris-meta"])
                .halign(align)
                .build(),
        );
        ui.thread.append(
            &gtk::ListBoxRow::builder()
                .child(&col)
                .activatable(false)
                .selectable(false)
                .build(),
        );
    }
}
fn refresh_view(ui: Rc<MessagesUi>) {
    if ui.loading.replace(true) {
        ui.pending.set(true);
        return;
    }
    let selected = ui.selected.borrow().clone();
    let requested = selected.clone();
    let limit = ui.limit.get();
    task(
        async move {
            let bus = sync_connection().await?;
            let proxy = service::proxy(&bus).await?;
            let (server, chats, messages, status): (String, String, String, String) = proxy
                .call("Snapshot", &(selected.unwrap_or_default(), limit))
                .await?;
            Ok((
                serde_json::from_str::<Vec<Chat>>(&chats)?,
                serde_json::from_str::<Vec<Message>>(&messages)?,
                status,
                server,
                Contacts::load().unwrap_or_default(),
            ))
        },
        move |result| {
            ui.loading.set(false);
            match result {
                Ok((chats, messages, status, server, contacts)) => {
                    if *ui.server.borrow() != server {
                        *ui.server.borrow_mut() = server;
                        *ui.selected.borrow_mut() = None;
                        ui.drafts.borrow_mut().clear();
                        ui.entry.buffer().set_text("");
                        ui.entry.set_sensitive(false);
                        ui.send.set_sensitive(false);
                        ui.attach.set_sensitive(false);
                        ui.title.set_title("");
                        clear_thread(&ui);
                        ui.composer.set_visible(false);
                        ui.content_stack.set_visible_child_name("empty");
                        ui.split.set_show_content(false);
                    }
                    ui.status.set_text(&status);
                    let contacts_changed = *ui.contacts.borrow() != contacts;
                    *ui.contacts.borrow_mut() = contacts;
                    ui.side_stack.set_visible_child_name(if chats.is_empty() {
                        "empty"
                    } else {
                        "list"
                    });
                    if contacts_changed {
                        ui.rendered.borrow_mut().clear();
                    }
                    // Update names/photos after a PBAP refresh, even if messages are unchanged.
                    if serde_json::to_string(&*ui.rows.borrow()).ok()
                        != serde_json::to_string(&chats).ok()
                        || contacts_changed
                    {
                        ui.restoring_rows.set(true);
                        while let Some(child) = ui.chats.first_child() {
                            ui.chats.remove(&child);
                        }
                        *ui.rows.borrow_mut() = chats.clone();
                        for chat in chats {
                            let contacts = ui.contacts.borrow();
                            let row = layout::conversation_row(&chat, &contacts);
                            ui.chats.append(&row);
                            if ui.selected.borrow().as_ref() == Some(&chat.guid) {
                                ui.title.set_title(&contacts.title(&chat));
                                ui.chats.select_row(Some(&row));
                            }
                        }
                        ui.restoring_rows.set(false);
                        filter_chats(&ui);
                    }
                    if *ui.selected.borrow() == requested && requested.is_some() {
                        render_thread(&ui, &messages);
                    }
                }
                Err(e) => {
                    ui.status.set_text(&error_text(&e));
                    if ui.rows.borrow().is_empty() {
                        ui.side_stack.set_visible_child_name("empty");
                    }
                    if ui.selected.borrow().is_some() {
                        ui.content_stack.set_visible_child_name("thread");
                    }
                }
            }
            if ui.pending.replace(false) {
                refresh_view(ui);
            }
        },
    );
}
fn sync(ui: Rc<MessagesUi>) {
    let selected = ui.selected.borrow().clone();
    let limit = ui.limit.get();
    task(
        async move {
            let bus = sync_connection().await?;
            let proxy = service::proxy(&bus).await?;
            proxy.call::<_, _, ()>("Refresh", &()).await?;
            if let Some(chat) = selected {
                proxy
                    .call::<_, _, ()>("FetchThread", &(chat, limit))
                    .await?;
            }
            Ok(())
        },
        move |result| {
            if let Err(e) = result {
                ui.status.set_text(&error_text(&e));
            }
            refresh_view(ui);
        },
    );
}
fn filter_chats(ui: &MessagesUi) {
    let query = ui.search.text().trim().to_lowercase();
    let contacts = ui.contacts.borrow();
    let visible: Vec<bool> = ui
        .rows
        .borrow()
        .iter()
        .map(|chat| {
            query.is_empty()
                || contacts.title(chat).to_lowercase().contains(&query)
                || Contacts::addresses(chat)
                    .iter()
                    .any(|a| a.to_lowercase().contains(&query))
                || chat
                    .last_message
                    .as_ref()
                    .is_some_and(|m| m.preview().to_lowercase().contains(&query))
        })
        .collect();
    ui.chats
        .set_filter_func(move |row| visible.get(row.index() as usize).copied().unwrap_or(false));
}

fn select_chat(ui: Rc<MessagesUi>, chat: Chat) {
    if let Some(previous) = ui.selected.borrow().as_ref() {
        ui.drafts
            .borrow_mut()
            .insert(previous.clone(), entry_text(&ui));
    }
    ui.entry.buffer().set_text(
        ui.drafts
            .borrow()
            .get(&chat.guid)
            .map(String::as_str)
            .unwrap_or(""),
    );
    ui.title.set_title(&ui.contacts.borrow().title(&chat));
    *ui.selected.borrow_mut() = Some(chat.guid);
    ui.limit.set(100);
    clear_thread(&ui);
    ui.stick_bottom.set(true);
    ui.content_stack.set_visible_child_name("loading");
    ui.composer.set_visible(true);
    ui.split.set_show_content(true);
    ui.entry.set_sensitive(true);
    ui.send.set_sensitive(true);
    ui.attach.set_sensitive(true);
    refresh_view(ui.clone());
    sync(ui);
}

fn send(ui: Rc<MessagesUi>) {
    let Some(chat) = ui.selected.borrow().clone() else {
        return;
    };
    let text = entry_text(&ui);
    if text.trim().is_empty() || ui.sending.replace(true) {
        return;
    }
    ui.composer.set_sensitive(false);
    ui.status.set_text("Sending…");
    let expected_server = ui.server.borrow().clone();
    task(
        async move {
            let bus = sync_connection().await?;
            service::proxy(&bus)
                .await?
                .call::<_, _, ()>("Send", &(expected_server, chat, text))
                .await?;
            Ok(())
        },
        move |result| {
            ui.sending.set(false);
            ui.composer.set_sensitive(true);
            match result {
                Ok(()) => {
                    ui.entry.buffer().set_text("");
                    ui.status.set_text("Sent");
                    refresh_view(ui);
                }
                Err(e) => ui.status.set_text(&error_text(&e)),
            }
        },
    );
}
fn messages(app: &adw::Application) {
    let win = adw::ApplicationWindow::builder()
        .application(app)
        .title("Verdigris")
        .default_width(1000)
        .default_height(700)
        .build();
    let layout::Layout {
        chats,
        thread,
        title,
        status,
        entry,
        send: send_button,
        attach,
        gif,
        scroll,
        search,
        side_stack,
        content_stack,
        composer,
        split,
        new_chat,
        search_bar,
    } = layout::build(&win);
    let ui = Rc::new(MessagesUi {
        chats: chats.clone(),
        thread,
        title,
        status,
        entry: entry.clone(),
        send: send_button.clone(),
        selected: RefCell::new(None),
        rows: RefCell::new(Vec::new()),
        contacts: RefCell::new(Contacts::default()),
        limit: Cell::new(100),
        sending: Cell::new(false),
        loading: Cell::new(false),
        pending: Cell::new(false),
        drafts: RefCell::new(std::collections::HashMap::new()),
        server: RefCell::new(String::new()),
        scroll,
        search: search.clone(),
        attach: attach.clone(),
        window: win.downgrade(),
        side_stack,
        content_stack,
        composer,
        split,
        rendered: RefCell::new(String::new()),
        stick_bottom: Cell::new(true),
        restoring_rows: Cell::new(false),
    });
    let state = ui.clone();
    chats.connect_row_selected(move |_, row| {
        let Some(row) = row else { return };
        if state.sending.get() || state.restoring_rows.get() {
            return;
        }
        let chat = state.rows.borrow().get(row.index() as usize).cloned();
        if let Some(chat) = chat
            && state.selected.borrow().as_ref() != Some(&chat.guid)
        {
            select_chat(state.clone(), chat);
        }
    });
    let state = ui.clone();
    chats.connect_row_activated(move |_, _| {
        if state.selected.borrow().is_some() {
            state.split.set_show_content(true);
        }
    });
    let state = ui.clone();
    search.connect_search_changed(move |_| filter_chats(&state));
    let state = ui.clone();
    let compose_action = gtk::gio::SimpleAction::new("new-message", None);
    compose_action.connect_activate(move |_, _| compose::show(state.clone()));
    app.add_action(&compose_action);
    app.set_accels_for_action("app.new-message", &["<Primary>n"]);
    new_chat.set_action_name(Some("app.new-message"));
    let state = ui.clone();
    attach.connect_clicked(move |_| media::choose_file(state.clone()));
    let state = ui.clone();
    let refresh = gtk::gio::SimpleAction::new("refresh", None);
    refresh.connect_activate(move |_, _| sync(state.clone()));
    app.add_action(&refresh);
    app.set_accels_for_action("app.refresh", &["<Primary>r"]);
    let preferences = gtk::gio::SimpleAction::new("preferences", None);
    preferences.connect_activate(|_, _| launch("verdigris-settings"));
    app.add_action(&preferences);
    let search_action = gtk::gio::SimpleAction::new("search", None);
    search_action.connect_activate(move |_, _| {
        search_bar.set_search_mode(!search_bar.is_search_mode());
        if search_bar.is_search_mode() {
            search.grab_focus();
        }
    });
    app.add_action(&search_action);
    app.set_accels_for_action("app.search", &["<Primary>f"]);
    let state = ui.clone();
    let older = gtk::gio::SimpleAction::new("older", None);
    older.connect_activate(move |_, _| {
        if state.selected.borrow().is_none() {
            return;
        }
        state.limit.set((state.limit.get() + 100).min(1000));
        sync(state.clone());
    });
    app.add_action(&older);
    let state = ui.clone();
    send_button.connect_clicked(move |_| send(state.clone()));
    let state = ui.clone();
    let keys = gtk::EventControllerKey::new();
    keys.connect_key_pressed(move |_, key, _, modifiers| {
        if matches!(
            key,
            gtk::gdk::Key::Return | gtk::gdk::Key::KP_Enter | gtk::gdk::Key::ISO_Enter
        ) && modifiers.contains(gtk::gdk::ModifierType::CONTROL_MASK)
        {
            send(state.clone());
            glib::Propagation::Stop
        } else {
            glib::Propagation::Proceed
        }
    });
    entry.add_controller(keys);
    gif_picker::build(&ui, &gif);
    let weak = Rc::downgrade(&ui);
    ui.scroll.vadjustment().connect_value_changed(move |adj| {
        if let Some(ui) = weak.upgrade() {
            ui.stick_bottom
                .set(adj.value() + adj.page_size() >= adj.upper() - 48.0);
        }
    });
    let weak = Rc::downgrade(&ui);
    ui.scroll.vadjustment().connect_changed(move |adj| {
        if let Some(ui) = weak.upgrade()
            && ui.stick_bottom.get()
        {
            adj.set_value((adj.upper() - adj.page_size()).max(0.0));
        }
    });
    let state = ui.clone();
    win.connect_is_active_notify(move |win| {
        if win.is_active() {
            sync(state.clone());
        }
    });
    let (tx, rx) = async_channel::bounded(1);
    crate::runtime().spawn(async move {
        use futures_util::StreamExt;
        if let Ok(bus) = sync_connection().await
            && let Ok(proxy) = service::proxy(&bus).await
            && let Ok(mut signals) = proxy.receive_signal("Changed").await
        {
            while signals.next().await.is_some() {
                if tx.send(()).await.is_err() {
                    break;
                }
            }
        }
    });
    let weak = Rc::downgrade(&ui);
    glib::spawn_future_local(async move {
        while rx.recv().await.is_ok() {
            let Some(ui) = weak.upgrade() else {
                break;
            };
            refresh_view(ui);
        }
    });
    refresh_view(ui.clone());
    sync(ui);
    win.present();
}

fn phone(app: &adw::Application) {
    let (win, content, _) = window(app, "Phone", 440, 640);
    let body = gtk::Box::new(gtk::Orientation::Vertical, 16);
    margins(&body, 24);
    let number = gtk::Entry::builder()
        .placeholder_text("Phone number")
        .build();
    number.add_css_class("call-number");
    body.append(&number);
    let keypad = gtk::Grid::builder()
        .row_spacing(8)
        .column_spacing(8)
        .column_homogeneous(true)
        .build();
    for (i, key) in ["1", "2", "3", "4", "5", "6", "7", "8", "9", "*", "0", "#"]
        .iter()
        .enumerate()
    {
        let button = gtk::Button::with_label(key);
        button.set_height_request(48);
        let number = number.clone();
        let key = key.to_string();
        button.connect_clicked(move |_| {
            number.set_text(&format!("{}{key}", number.text()));
        });
        keypad.attach(&button, (i % 3) as i32, (i / 3) as i32, 1, 1);
    }
    body.append(&keypad);
    let call = gtk::Button::with_label("Call");
    call.add_css_class("suggested-action");
    body.append(&call);
    let status = gtk::Label::new(Some("Calls use your nearby iPhone"));
    status.set_wrap(true);
    body.append(&status);
    let calls = gtk::Box::new(gtk::Orientation::Vertical, 8);
    body.append(&calls);
    content.append(&body);
    let label = status.clone();
    call.connect_clicked(move |button| {
        let number = number.text().to_string();
        if number.trim().is_empty() {
            return;
        }
        let button = button.clone();
        button.set_sensitive(false);
        let label = label.clone();
        task(
            async move {
                let bus = zbus::Connection::session().await?;
                let proxy =
                    service::bridge_proxy(&bus, "dev.turbinebmw.Verdigris.Bridge.Calls1").await?;
                let _: String = proxy.call("Dial", &(number,)).await?;
                Ok(())
            },
            move |result| {
                button.set_sensitive(true);
                label.set_text(&match result {
                    Ok(()) => "Calling…".into(),
                    Err(e) => error_text(&e),
                });
            },
        );
    });
    let update: Rc<dyn Fn()> = Rc::new(move || {
        let calls = calls.clone();
        let status = status.clone();
        task(
            async {
                let bus = zbus::Connection::session().await?;
                let proxy =
                    service::bridge_proxy(&bus, "dev.turbinebmw.Verdigris.Bridge.Calls1").await?;
                let data: String = proxy.call("ListCalls", &()).await?;
                Ok((
                    serde_json::from_str::<Vec<serde_json::Value>>(&data)?,
                    Contacts::load().unwrap_or_default(),
                ))
            },
            move |result| {
                clear(&calls);
                match result {
                    Err(_) => status.set_text(
                        "Phone service unavailable. Open Settings to check the connection.",
                    ),
                    Ok((rows, contacts)) => {
                        if rows.is_empty() {
                            status.set_text("No active calls · iPhone must be nearby");
                        }
                        for row in rows {
                            let name = row["contact_name"]
                                .as_str()
                                .or(row["peer_name"].as_str())
                                .or(row["peer_phone"].as_str())
                                .unwrap_or("Unknown caller");
                            let state = row["state"].as_str().unwrap_or("");
                            let address = row["peer_phone"].as_str().unwrap_or("");
                            let name = contacts
                                .resolve(address)
                                .map(|c| c.name.as_str())
                                .filter(|n| !n.is_empty())
                                .unwrap_or(name);
                            calls.append(&avatar(name, contacts.photo(address), 64));
                            let label = gtk::Label::new(Some(&format!("{name} · {state}")));
                            calls.append(&label);
                            for (text, method) in
                                [("Answer", "AnswerCall"), ("End call", "HangupCall")]
                            {
                                if method == "AnswerCall"
                                    && !matches!(state, "incoming" | "waiting")
                                {
                                    continue;
                                }
                                let button = gtk::Button::with_label(text);
                                let path = row["call_path"].as_str().unwrap_or("").to_owned();
                                let status = status.clone();
                                button.connect_clicked(move |_| {
                                    let path = path.clone();
                                    let status = status.clone();
                                    task(
                                        async move {
                                            let bus = zbus::Connection::session().await?;
                                            service::bridge_proxy(
                                                &bus,
                                                "dev.turbinebmw.Verdigris.Bridge.Calls1",
                                            )
                                            .await?
                                            .call::<_, _, ()>(method, &(path,))
                                            .await?;
                                            Ok(())
                                        },
                                        move |result| {
                                            if let Err(e) = result {
                                                status.set_text(&error_text(&e));
                                            }
                                        },
                                    );
                                });
                                calls.append(&button);
                            }
                        }
                    }
                }
            },
        );
    });
    update();
    let updater = update.clone();
    let weak_win = win.downgrade();
    // A cheap local snapshot also recovers service restarts and missed D-Bus signals.
    glib::timeout_add_seconds_local(2, move || {
        if weak_win.upgrade().is_none() {
            return glib::ControlFlow::Break;
        }
        updater();
        glib::ControlFlow::Continue
    });
    win.present();
}
