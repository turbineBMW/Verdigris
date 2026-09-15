use crate::notifications::{self, Rule, Rules};
use adw::prelude::*;
use gtk::{gio, glib};
use std::{cell::RefCell, rc::Rc};

pub fn settings_group(window: &adw::ApplicationWindow) -> adw::PreferencesGroup {
    let group = adw::PreferencesGroup::builder()
        .title("Notifications")
        .build();
    let row = adw::ActionRow::builder()
        .title("iPhone app notifications")
        .subtitle("Choose which apps notify you, what opens, and their icons")
        .activatable(true)
        .build();
    row.add_suffix(&gtk::Image::from_icon_name("go-next-symbolic"));
    let window = window.downgrade();
    row.connect_activated(move |_| {
        if let Some(window) = window.upgrade() {
            show(&window);
        }
    });
    group.add(&row);
    group
}

fn shell(title: &str) -> (adw::Dialog, gtk::Box, adw::HeaderBar) {
    let dialog = adw::Dialog::builder()
        .title(title)
        .content_width(560)
        .content_height(620)
        .build();
    let content = gtk::Box::new(gtk::Orientation::Vertical, 0);
    let header = adw::HeaderBar::new();
    content.append(&header);
    dialog.set_child(Some(&content));
    (dialog, content, header)
}

fn label() -> gtk::Label {
    let label = gtk::Label::new(None);
    label.set_wrap(true);
    label.set_margin_start(18);
    label.set_margin_end(18);
    label.set_margin_bottom(12);
    label
}

fn app_name(id: &str) -> String {
    gio::AppInfo::all()
        .into_iter()
        .find(|app| app.id().as_deref() == Some(id))
        .map(|app| app.display_name().to_string())
        .unwrap_or_else(|| format!("Unavailable: {id}"))
}

fn show(parent: &adw::ApplicationWindow) {
    let (dialog, content, header) = shell("iPhone notifications");
    let refresh = gtk::Button::from_icon_name("view-refresh-symbolic");
    refresh.set_tooltip_text(Some("Refresh iPhone apps"));
    header.pack_end(&refresh);
    let page = adw::PreferencesPage::new();
    page.set_vexpand(true);
    let group = adw::PreferencesGroup::builder()
        .description("Apps appear after they send a notification through the Verdigris Bluetooth service. Changes apply to future notifications.")
        .build();
    let list = gtk::ListBox::new();
    list.set_selection_mode(gtk::SelectionMode::None);
    list.add_css_class("boxed-list");
    let empty = gtk::Label::new(Some(
        "No iPhone apps yet. Connect your iPhone, receive a notification, then refresh.",
    ));
    empty.set_wrap(true);
    super::margins(&empty, 24);
    list.set_placeholder(Some(&empty));
    group.add(&list);
    page.add(&group);
    content.append(&page);
    let status = label();
    content.append(&status);
    let last = RefCell::new(None);
    let reload: Rc<dyn Fn()> = Rc::new({
        let list = list.clone();
        let dialog = dialog.downgrade();
        move || {
            let result = (|| -> anyhow::Result<()> {
                let rules = Rules::load()?;
                let mut apps: Vec<_> = notifications::apps()?.into_iter().collect();
                apps.sort_by_key(|(_, name)| name.to_lowercase());
                let snapshot: Vec<_> = apps
                    .iter()
                    .map(|(id, name)| {
                        (
                            id.clone(),
                            name.clone(),
                            rules.apps.get(id).cloned().unwrap_or_default(),
                        )
                    })
                    .collect();
                if last.borrow().as_ref() == Some(&snapshot) {
                    return Ok(());
                }
                *last.borrow_mut() = Some(snapshot);
                while let Some(child) = list.row_at_index(0) {
                    list.remove(&child);
                }
                for (id, name) in apps {
                    let rule = rules.apps.get(&id).cloned().unwrap_or_default();
                    let subtitle = if !rule.enabled {
                        "Notifications off".to_string()
                    } else if let Some(target) = &rule.desktop_id {
                        format!("Opens {}", app_name(target))
                    } else {
                        "Notifications on".to_string()
                    };
                    let row = adw::ActionRow::builder()
                        .title(&name)
                        .subtitle(&subtitle)
                        .activatable(true)
                        .build();
                    row.set_use_markup(false);
                    row.add_suffix(&gtk::Image::from_icon_name("go-next-symbolic"));
                    let dialog = dialog.clone();
                    row.connect_activated(move |_| {
                        if let Some(dialog) = dialog.upgrade() {
                            edit(&dialog, &id, &name);
                        }
                    });
                    list.append(&row);
                }
                Ok(())
            })();
            status.set_text(&result.err().map(|e| e.to_string()).unwrap_or_default());
        }
    });
    reload();
    refresh.connect_clicked({
        let reload = reload.clone();
        move |_| reload()
    });
    // Reload the summary after a detail dialog closes, and pick up new apps.
    let weak = dialog.downgrade();
    let timer = glib::timeout_add_local(std::time::Duration::from_secs(3), move || {
        if weak.upgrade().is_none() {
            return glib::ControlFlow::Break;
        }
        reload();
        glib::ControlFlow::Continue
    });
    let timer = RefCell::new(Some(timer));
    dialog.connect_closed(move |_| {
        if let Some(timer) = timer.borrow_mut().take() {
            timer.remove();
        }
    });
    dialog.present(Some(parent));
}

fn edit(parent: &adw::Dialog, id: &str, name: &str) {
    let (dialog, content, header) = shell(name);
    let status = label();
    let rule = match Rules::load() {
        Ok(rules) => rules.apps.get(id).cloned().unwrap_or_default(),
        Err(error) => {
            status.set_text(&error.to_string());
            content.append(&status);
            dialog.present(Some(parent));
            return;
        }
    };
    let draft = Rc::new(RefCell::new(rule));
    let save = gtk::Button::with_label("Save");
    save.add_css_class("suggested-action");
    header.pack_end(&save);
    let page = adw::PreferencesPage::new();
    page.set_vexpand(true);
    let group = adw::PreferencesGroup::builder().description(id).build();
    let enabled = adw::SwitchRow::builder()
        .title("Allow notifications")
        .subtitle("When off, incoming notifications from this app are ignored")
        .active(draft.borrow().enabled)
        .build();
    group.add(&enabled);
    let target = adw::ActionRow::builder()
        .title("Open when clicked")
        .activatable(true)
        .build();
    target.set_use_markup(false);
    target.set_subtitle(
        &draft
            .borrow()
            .desktop_id
            .as_deref()
            .map(app_name)
            .unwrap_or_else(|| "Do nothing".into()),
    );
    target.add_suffix(&gtk::Image::from_icon_name("go-next-symbolic"));
    target.connect_activated({
        let dialog = dialog.downgrade();
        let draft = draft.clone();
        move |row| {
            if let Some(dialog) = dialog.upgrade() {
                let row = row.clone();
                let draft = draft.clone();
                pick_app(&dialog, move |id| {
                    row.set_subtitle(
                        &id.as_deref()
                            .map(app_name)
                            .unwrap_or_else(|| "Do nothing".into()),
                    );
                    draft.borrow_mut().desktop_id = id;
                });
            }
        }
    });
    group.add(&target);
    let icon_row = adw::ActionRow::builder().title("Notification icon").build();
    let preview = gtk::Image::new();
    preview.set_pixel_size(40);
    update_icon(&icon_row, &preview, &draft.borrow());
    icon_row.add_prefix(&preview);
    let choose = gtk::Button::with_label("Choose…");
    choose.set_valign(gtk::Align::Center);
    let reset = gtk::Button::from_icon_name("edit-clear-symbolic");
    reset.set_tooltip_text(Some("Use automatic icon"));
    reset.set_valign(gtk::Align::Center);
    icon_row.add_suffix(&choose);
    icon_row.add_suffix(&reset);
    choose.connect_clicked({
        let draft = draft.clone();
        let dialog = dialog.downgrade();
        let icon_row = icon_row.downgrade();
        let preview = preview.clone();
        let status = status.clone();
        move |_| {
            let Some(dialog) = dialog.upgrade() else {
                return;
            };
            let filter = gtk::FileFilter::new();
            filter.set_name(Some("Images"));
            filter.add_pixbuf_formats();
            let filters = gio::ListStore::new::<gtk::FileFilter>();
            filters.append(&filter);
            let chooser = gtk::FileDialog::builder()
                .title("Choose notification icon")
                .filters(&filters)
                .build();
            let window = dialog.root().and_downcast::<gtk::Window>();
            let draft = draft.clone();
            let Some(icon_row) = icon_row.upgrade() else {
                return;
            };
            let preview = preview.clone();
            let status = status.clone();
            chooser.open(
                window.as_ref(),
                gio::Cancellable::NONE,
                move |result| match result {
                    Ok(file) => {
                        let result = file
                            .path()
                            .ok_or_else(|| anyhow::anyhow!("Choose a local image"))
                            .and_then(|path| notifications::import_icon(&path));
                        match result {
                            Ok(name) => {
                                draft.borrow_mut().icon = Some(name);
                                update_icon(&icon_row, &preview, &draft.borrow());
                                status.set_text("");
                            }
                            Err(error) => status.set_text(&error.to_string()),
                        }
                    }
                    Err(error) if error.matches(gtk::DialogError::Dismissed) => {}
                    Err(error) => status.set_text(&error.to_string()),
                },
            );
        }
    });
    reset.connect_clicked({
        let draft = draft.clone();
        let icon_row = icon_row.downgrade();
        move |_| {
            draft.borrow_mut().icon = None;
            if let Some(icon_row) = icon_row.upgrade() {
                update_icon(&icon_row, &preview, &draft.borrow());
            }
        }
    });
    group.add(&icon_row);
    page.add(&group);
    content.append(&page);
    content.append(&status);
    save.connect_clicked({
        let id = id.to_string();
        let dialog = dialog.downgrade();
        move |_| {
            draft.borrow_mut().enabled = enabled.is_active();
            match Rules::save_rule(&id, draft.borrow().clone()) {
                Ok(()) => {
                    if let Some(dialog) = dialog.upgrade() {
                        dialog.close();
                    }
                }
                Err(error) => status.set_text(&error.to_string()),
            }
        }
    });
    dialog.present(Some(parent));
}

fn update_icon(row: &adw::ActionRow, preview: &gtk::Image, rule: &Rule) {
    if let Some(path) = rule
        .icon
        .as_deref()
        .and_then(|name| notifications::icon_path(name).ok())
        .filter(|p| p.is_file())
    {
        preview.set_from_file(Some(path));
        row.set_subtitle("Custom icon");
    } else {
        preview.set_icon_name(Some("preferences-system-notifications-symbolic"));
        row.set_subtitle("Automatic");
    }
}

fn pick_app(parent: &adw::Dialog, selected: impl Fn(Option<String>) + 'static) {
    let (dialog, content, _) = shell("Open Linux app");
    let search = gtk::SearchEntry::new();
    search.set_placeholder_text(Some("Search installed apps"));
    super::margins(&search, 12);
    content.append(&search);
    let list = gtk::ListBox::new();
    list.set_selection_mode(gtk::SelectionMode::None);
    list.add_css_class("boxed-list");
    super::margins(&list, 12);
    let scroll = gtk::ScrolledWindow::builder()
        .vexpand(true)
        .hscrollbar_policy(gtk::PolicyType::Never)
        .child(&list)
        .build();
    content.append(&scroll);
    let selected = Rc::new(selected);
    let mut apps: Vec<_> = gio::AppInfo::all()
        .into_iter()
        .filter(|a| a.should_show() && a.id().is_some())
        .collect();
    apps.sort_by_key(|app| app.display_name().to_lowercase());
    let none = adw::ActionRow::builder()
        .title("Do nothing")
        .activatable(true)
        .build();
    none.connect_activated({
        let selected = selected.clone();
        let dialog = dialog.downgrade();
        move |_| {
            selected(None);
            if let Some(dialog) = dialog.upgrade() {
                dialog.close();
            }
        }
    });
    list.append(&none);
    for app in apps {
        let Some(id) = app.id().filter(|id| id.ends_with(".desktop")) else {
            continue;
        };
        let row = adw::ActionRow::builder()
            .title(app.display_name())
            .subtitle(id.as_str())
            .activatable(true)
            .build();
        row.set_use_markup(false);
        if let Some(icon) = app.icon() {
            let image = gtk::Image::from_gicon(&icon);
            image.set_pixel_size(32);
            row.add_prefix(&image);
        }
        row.connect_activated({
            let selected = selected.clone();
            let dialog = dialog.downgrade();
            move |_| {
                selected(Some(id.to_string()));
                if let Some(dialog) = dialog.upgrade() {
                    dialog.close();
                }
            }
        });
        list.append(&row);
    }
    list.set_filter_func({
        let search = search.downgrade();
        move |row| {
            let Some(search) = search.upgrade() else {
                return true;
            };
            let Some(row) = row.downcast_ref::<adw::ActionRow>() else {
                return true;
            };
            let text =
                format!("{} {}", row.title(), row.subtitle().unwrap_or_default()).to_lowercase();
            text.contains(search.text().to_lowercase().as_str())
        }
    });
    search.connect_search_changed(move |_| list.invalidate_filter());
    dialog.present(Some(parent));
}
