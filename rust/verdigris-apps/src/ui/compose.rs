use super::*;

pub(super) fn show(ui: Rc<MessagesUi>) {
    if ui.sending.get() {
        return;
    }
    let Some(parent) = ui.window.upgrade() else {
        return;
    };
    if ui.server.borrow().is_empty() {
        ui.status.set_text("Connect your Mac in Settings first");
        return;
    }
    let dialog = adw::Dialog::builder()
        .title("New message")
        .content_width(480)
        .content_height(540)
        .build();
    let outer = gtk::Box::new(gtk::Orientation::Vertical, 0);
    outer.append(&adw::HeaderBar::new());
    let body = gtk::Box::new(gtk::Orientation::Vertical, 12);
    margins(&body, 20);
    let recipient = gtk::Entry::builder()
        .placeholder_text("Name, phone number, or email")
        .build();
    body.append(&recipient);
    let suggestions = gtk::ListBox::new();
    suggestions.add_css_class("boxed-list");
    let scroll = gtk::ScrolledWindow::builder()
        .min_content_height(140)
        .max_content_height(180)
        .child(&suggestions)
        .build();
    body.append(&scroll);
    let transport = gtk::DropDown::from_strings(&["iMessage", "SMS"]);
    body.append(&transport);
    let input = gtk::TextView::builder()
        .wrap_mode(gtk::WrapMode::WordChar)
        .top_margin(10)
        .bottom_margin(10)
        .left_margin(10)
        .right_margin(10)
        .build();
    let input_scroll = gtk::ScrolledWindow::builder()
        .min_content_height(100)
        .vexpand(true)
        .child(&input)
        .build();
    input_scroll.add_css_class("card");
    body.append(&input_scroll);
    let status = gtk::Label::new(Some("SMS requires text forwarding to your Mac."));
    status.set_wrap(true);
    status.add_css_class("dim-label");
    body.append(&status);
    let send_button = gtk::Button::with_label("Send message");
    send_button.add_css_class("suggested-action");
    body.append(&send_button);
    outer.append(&body);
    dialog.set_child(Some(&outer));
    let contacts = ui.contacts.borrow().clone();
    let recipient_entry = recipient.downgrade();
    let suggestions_list = suggestions.downgrade();
    let populate = Rc::new(move |query: &str| {
        let Some(suggestions_list) = suggestions_list.upgrade() else {
            return;
        };
        while let Some(child) = suggestions_list.first_child() {
            suggestions_list.remove(&child);
        }
        for (number, contact) in contacts.search(query, 6) {
            let row = adw::ActionRow::builder()
                .title(glib::markup_escape_text(&contact.name))
                .subtitle(glib::markup_escape_text(&number))
                .activatable(true)
                .build();
            row.add_prefix(&avatar(&contact.name, contact.photo.as_deref(), 32));
            let recipient = recipient_entry.clone();
            row.connect_activated(move |_| {
                if let Some(recipient) = recipient.upgrade() {
                    recipient.set_text(&number);
                }
            });
            suggestions_list.append(&row);
        }
    });
    populate("");
    recipient.connect_changed(move |entry| populate(&entry.text()));
    let expected_server = ui.server.borrow().clone();
    let modal = dialog.downgrade();
    send_button.connect_clicked(move |button| {
        let Some(modal) = modal.upgrade() else {
            return;
        };
        let recipient_value = recipient.text().to_string();
        let buffer = input.buffer();
        let message = buffer
            .text(&buffer.start_iter(), &buffer.end_iter(), true)
            .to_string();
        if let Err(e) = crate::api::normalize_recipient(&recipient_value) {
            status.set_text(&e.to_string());
            return;
        }
        if message.trim().is_empty() {
            status.set_text("Write a message before sending");
            return;
        }
        let service_name = if transport.selected() == 0 {
            "iMessage"
        } else {
            "SMS"
        };
        button.set_sensitive(false);
        recipient.set_sensitive(false);
        input.set_sensitive(false);
        transport.set_sensitive(false);
        modal.set_can_close(false);
        status.set_text("Sending…");
        let expected_server = expected_server.clone();
        let button = button.clone();
        let recipient = recipient.clone();
        let input = input.clone();
        let transport = transport.clone();
        let modal = modal.clone();
        let ui = ui.clone();
        let status = status.clone();
        task(
            async move {
                let bus = sync_connection().await?;
                let response: String = service::proxy(&bus)
                    .await?
                    .call(
                        "CreateChat",
                        &(expected_server, recipient_value, message, service_name),
                    )
                    .await?;
                Ok(serde_json::from_str::<Chat>(&response)?)
            },
            move |result| {
                modal.set_can_close(true);
                button.set_sensitive(true);
                recipient.set_sensitive(true);
                input.set_sensitive(true);
                transport.set_sensitive(true);
                match result {
                    Ok(chat) => {
                        select_chat(ui.clone(), chat);
                        modal.close();
                    }
                    Err(e) => status.set_text(&error_text(&e)),
                }
            },
        );
    });
    dialog.present(Some(&parent));
}
