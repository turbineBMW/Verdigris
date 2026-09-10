//! Bubo's native split view and composer, adapted to Verdigris's message data.
use super::*;

pub(super) struct Layout {
    pub chats: gtk::ListBox,
    pub thread: gtk::ListBox,
    pub title: adw::WindowTitle,
    pub status: gtk::Label,
    pub entry: gtk::TextView,
    pub send: gtk::Button,
    pub attach: gtk::Button,
    pub gif: gtk::Button,
    pub scroll: gtk::ScrolledWindow,
    pub search: gtk::SearchEntry,
    pub side_stack: gtk::Stack,
    pub content_stack: gtk::Stack,
    pub composer: gtk::Box,
    pub split: adw::NavigationSplitView,
    pub new_chat: gtk::Button,
    pub search_bar: gtk::SearchBar,
}
fn loading_page(text: &str) -> gtk::Widget {
    let spinner = adw::Spinner::new();
    spinner.set_size_request(32, 32);
    let label = gtk::Label::builder()
        .label(text)
        .css_classes(["dim-label"])
        .build();
    let col = gtk::Box::builder()
        .orientation(gtk::Orientation::Vertical)
        .spacing(12)
        .halign(gtk::Align::Center)
        .valign(gtk::Align::Center)
        .vexpand(true)
        .hexpand(true)
        .build();
    col.append(&spinner);
    col.append(&label);
    col.upcast()
}

/// Build a stack of named pages, crossfading between them.
fn stack(pages: &[(&str, &gtk::Widget)]) -> gtk::Stack {
    let st = gtk::Stack::builder()
        .transition_type(gtk::StackTransitionType::Crossfade)
        .transition_duration(150)
        .vexpand(true)
        .build();
    for (name, w) in pages {
        st.add_named(*w, Some(name));
    }
    st
}

pub(super) fn build(win: &adw::ApplicationWindow) -> Layout {
    // ── sidebar ──
    let list = gtk::ListBox::builder()
        .selection_mode(gtk::SelectionMode::Single)
        .css_classes(["navigation-sidebar", "verdigris-convs"])
        .build();
    let side_scroll = gtk::ScrolledWindow::builder()
        .child(&list)
        .hscrollbar_policy(gtk::PolicyType::Never)
        .vexpand(true)
        .build();
    let side_header = adw::HeaderBar::builder()
        .title_widget(&adw::WindowTitle::new("Verdigris", ""))
        .build();
    let menu = gtk::gio::Menu::new();
    menu.append(Some("Preferences"), Some("app.preferences"));
    menu.append(Some("Search conversations"), Some("app.search"));
    menu.append(Some("Refresh"), Some("app.refresh"));
    menu.append(Some("Load older messages"), Some("app.older"));
    side_header.pack_end(
        &gtk::MenuButton::builder()
            .icon_name("open-menu-symbolic")
            .menu_model(&menu)
            .build(),
    );
    let new_chat = gtk::Button::builder()
        .icon_name("list-add-symbolic")
        .tooltip_text("New conversation")
        .build();
    side_header.pack_start(&new_chat);
    let side_empty = adw::StatusPage::builder()
        .icon_name("chat-message-new-symbolic")
        .title("No conversations")
        .description("Messages from your Mac will show up here.")
        .build();
    let side_stack = stack(&[
        ("loading", &loading_page("Loading conversations…")),
        ("empty", side_empty.upcast_ref()),
        ("list", side_scroll.upcast_ref()),
    ]);
    let side = adw::ToolbarView::new();
    side.add_top_bar(&side_header);
    let search = gtk::SearchEntry::builder()
        .placeholder_text("Search conversations")
        .build();
    let search_bar = gtk::SearchBar::new();
    search_bar.set_child(Some(&search));
    search_bar.connect_entry(&search);
    let side_body = gtk::Box::new(gtk::Orientation::Vertical, 0);
    side_body.append(&search_bar);
    side_body.append(&side_stack);
    side.set_content(Some(&side_body));
    let sidebar = adw::NavigationPage::builder()
        .title("Chats")
        .child(&side)
        .build();

    // ── thread ──
    let thread = gtk::ListBox::builder()
        .selection_mode(gtk::SelectionMode::None)
        .css_classes(["boxed-list-separate"])
        .margin_start(12)
        .margin_end(12)
        .margin_top(8)
        .margin_bottom(8)
        .valign(gtk::Align::End)
        .build();
    thread.add_css_class("verdigris-thread");
    let thread_scroll = gtk::ScrolledWindow::builder()
        .child(&thread)
        .hscrollbar_policy(gtk::PolicyType::Never)
        .vexpand(true)
        .build();
    let thread_title = adw::WindowTitle::new("", "");
    // Multi-line composer: Enter inserts a newline, Ctrl+Enter sends. The text view grows with
    // its content up to a cap, then scrolls; the buttons sit at the bottom edge either way.
    let entry = gtk::TextView::builder()
        .wrap_mode(gtk::WrapMode::WordChar)
        .hexpand(true)
        .accepts_tab(false)
        .top_margin(7)
        .bottom_margin(7)
        .left_margin(10)
        .right_margin(10)
        .css_classes(["verdigris-entry"])
        .build();
    let placeholder = gtk::Label::builder()
        .label("Message")
        .halign(gtk::Align::Start)
        .valign(gtk::Align::Start)
        .margin_start(10)
        .margin_top(7)
        .can_target(false)
        .css_classes(["dim-label"])
        .build();
    let overlay = gtk::Overlay::builder().child(&entry).build();
    overlay.add_overlay(&placeholder);
    let entry_scroll = gtk::ScrolledWindow::builder()
        .child(&overlay)
        .hscrollbar_policy(gtk::PolicyType::Never)
        .propagate_natural_height(true)
        .max_content_height(160)
        .hexpand(true)
        .css_classes(["verdigris-entry-frame"])
        .build();
    let pl = placeholder.clone();
    entry
        .buffer()
        .connect_changed(move |b| pl.set_visible(b.char_count() == 0));
    let emoji_btn = gtk::Button::builder()
        .icon_name("emoji-people-symbolic")
        .css_classes(["circular"])
        .tooltip_text("Insert emoji")
        .valign(gtk::Align::End)
        .build();
    let send = gtk::Button::builder()
        .icon_name("mail-send-symbolic")
        .css_classes(["suggested-action", "circular"])
        .valign(gtk::Align::End)
        .tooltip_text("Send (Ctrl+Enter)")
        .build();
    let attach = gtk::Button::builder()
        .icon_name("mail-attachment-symbolic")
        .css_classes(["circular"])
        .tooltip_text("Attach a file")
        .valign(gtk::Align::End)
        .build();
    let gif_btn = gtk::Button::builder()
        .label("GIF")
        .css_classes(["circular", "verdigris-gif-btn"])
        .tooltip_text("Send a GIF")
        .valign(gtk::Align::End)
        .build();
    let composer = gtk::Box::builder()
        .orientation(gtk::Orientation::Horizontal)
        .spacing(6)
        .margin_start(12)
        .margin_end(12)
        .margin_top(6)
        .margin_bottom(12)
        .valign(gtk::Align::End)
        .build();
    composer.append(&attach);
    composer.append(&gif_btn);
    composer.append(&emoji_btn);
    composer.append(&entry_scroll);
    composer.append(&send);
    composer.set_visible(false);
    let status = gtk::Label::new(None);
    let banner = adw::Banner::builder().revealed(false).build();
    let b = banner.clone();
    status.connect_label_notify(move |label| {
        let text = label.text();
        b.set_title(&text);
        b.set_revealed(
            !text.is_empty() && text != "Up to date" && text != "Sent" && text != "File sent",
        );
    });
    let content_empty = adw::StatusPage::builder()
        .icon_name("user-available-symbolic")
        .title("Select a conversation")
        .description("Pick a chat from the list to start messaging.")
        .build();
    let content_stack = stack(&[
        ("empty", content_empty.upcast_ref()),
        ("loading", &loading_page("Loading messages…")),
        ("thread", thread_scroll.upcast_ref()),
    ]);
    let content_box = gtk::Box::new(gtk::Orientation::Vertical, 0);
    content_box.append(&banner);
    content_box.append(&content_stack);
    content_box.append(&composer);
    let content = adw::ToolbarView::new();
    content.add_top_bar(
        &adw::HeaderBar::builder()
            .title_widget(&thread_title)
            .build(),
    );
    content.set_content(Some(&content_box));
    let toast = adw::ToastOverlay::new();
    toast.set_child(Some(&content));
    let content_page = adw::NavigationPage::builder()
        .title("Conversation")
        .child(&toast)
        .build();

    let widget = adw::NavigationSplitView::builder()
        .sidebar(&sidebar)
        .content(&content_page)
        .min_sidebar_width(260.0)
        .max_sidebar_width(360.0)
        .build();

    win.set_content(Some(&widget));
    let breakpoint = adw::Breakpoint::new(adw::BreakpointCondition::new_length(
        adw::BreakpointConditionLengthType::MaxWidth,
        620.0,
        adw::LengthUnit::Sp,
    ));
    breakpoint.add_setter(&widget, "collapsed", Some(&true.to_value()));
    win.add_breakpoint(breakpoint);
    let chooser = gtk::EmojiChooser::new();
    chooser.set_parent(&emoji_btn);
    let input = entry.clone();
    chooser.connect_emoji_picked(move |_, emoji| {
        let b = input.buffer();
        b.delete_selection(true, true);
        b.insert_at_cursor(emoji);
        input.grab_focus();
    });
    let pop = chooser.clone();
    emoji_btn.connect_clicked(move |_| pop.popup());
    emoji_btn.connect_destroy(move |_| chooser.unparent());
    Layout {
        chats: list,
        thread,
        title: thread_title,
        status,
        entry,
        send,
        attach,
        gif: gif_btn,
        scroll: thread_scroll,
        search,
        side_stack,
        content_stack,
        composer,
        split: widget,
        new_chat,
        search_bar,
    }
}

pub(super) fn fmt_time(timestamp: Option<i64>) -> String {
    let Some(dt) = timestamp.and_then(|t| glib::DateTime::from_unix_local(t / 1000).ok()) else {
        return String::new();
    };
    let Ok(now) = glib::DateTime::now_local() else {
        return String::new();
    };
    let fmt = if dt.ymd() == now.ymd() {
        "%H:%M"
    } else if dt.year() == now.year() {
        "%-d %b"
    } else {
        "%-d %b %Y"
    };
    dt.format(fmt).map(|s| s.to_string()).unwrap_or_default()
}

pub(super) fn conversation_row(chat: &Chat, contacts: &Contacts) -> gtk::ListBoxRow {
    let av = avatar(&contacts.title(chat), contacts.chat_photo(chat), 40);
    if chat.participants.len() > 1 {
        av.set_icon_name(Some("system-users-symbolic"));
    }
    let name = gtk::Label::builder()
        .label(contacts.title(chat))
        .xalign(0.0)
        .ellipsize(gtk::pango::EllipsizeMode::End)
        .hexpand(true)
        .valign(gtk::Align::End)
        .build();
    let time = gtk::Label::builder()
        .label(fmt_time(
            chat.last_message.as_ref().and_then(|m| m.date_created),
        ))
        .css_classes(["verdigris-meta"])
        .valign(gtk::Align::End)
        .build();
    let top = gtk::Box::new(gtk::Orientation::Horizontal, 8);
    top.append(&name);
    top.append(&time);
    let preview = chat
        .last_message
        .as_ref()
        .map(|m| {
            let prefix = if m.is_from_me {
                "You: ".to_string()
            } else if chat.participants.len() > 1 {
                m.handle
                    .as_ref()
                    .and_then(|h| h["address"].as_str())
                    .map(|a| format!("{}: ", contacts.name(a)))
                    .unwrap_or_default()
            } else {
                String::new()
            };
            format!("{prefix}{}", m.preview()).replace('\n', " ")
        })
        .unwrap_or_default();
    let snippet = gtk::Label::builder()
        .label(&preview)
        .xalign(0.0)
        .ellipsize(gtk::pango::EllipsizeMode::End)
        .single_line_mode(true)
        .valign(gtk::Align::Start)
        .css_classes(["verdigris-snippet", "caption"])
        .build();
    let col = gtk::Box::builder()
        .orientation(gtk::Orientation::Vertical)
        .homogeneous(true)
        .hexpand(true)
        .height_request(40)
        .valign(gtk::Align::Center)
        .build();
    col.append(&top);
    col.append(&snippet);
    let row = gtk::Box::new(gtk::Orientation::Horizontal, 12);
    row.append(&av);
    row.append(&col);
    gtk::ListBoxRow::builder().child(&row).build()
}
