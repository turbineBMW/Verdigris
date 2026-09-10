//! Bubo's three-column search popover, using Verdigris's attachment transport.
use super::*;

pub(super) fn build(ui: &Rc<MessagesUi>, button: &gtk::Button) {
    let pop = gtk::Popover::builder().width_request(372).build();
    pop.set_parent(button);
    let search = gtk::SearchEntry::builder()
        .placeholder_text("Search GIFs")
        .search_delay(350)
        .build();
    let grid = gtk::FlowBox::builder()
        .selection_mode(gtk::SelectionMode::None)
        .min_children_per_line(3)
        .max_children_per_line(3)
        .homogeneous(true)
        .row_spacing(4)
        .column_spacing(4)
        .valign(gtk::Align::Start)
        .build();
    let scroll = gtk::ScrolledWindow::builder()
        .child(&grid)
        .hscrollbar_policy(gtk::PolicyType::Never)
        .height_request(400)
        .build();
    let status = gtk::Label::builder()
        .label("Type to search")
        .css_classes(["dim-label"])
        .margin_top(12)
        .margin_bottom(12)
        .wrap(true)
        .max_width_chars(42)
        .build();
    let col = gtk::Box::new(gtk::Orientation::Vertical, 6);
    col.append(&search);
    col.append(&status);
    col.append(&scroll);
    pop.set_child(Some(&col));
    let s = search.clone();
    pop.connect_show(move |_| {
        s.grab_focus();
    });
    let p = pop.clone();
    button.connect_clicked(move |_| p.popup());
    let generation = Rc::new(Cell::new(0u64));
    let gen_changed = generation.clone();
    let grid_changed = grid.clone();
    let status_changed = status.clone();
    // Invalidate pending responses immediately, before SearchEntry's debounce expires.
    search.connect_changed(move |e| {
        gen_changed.set(gen_changed.get().wrapping_add(1));
        while let Some(child) = grid_changed.first_child() {
            grid_changed.remove(&child);
        }
        status_changed.set_visible(true);
        status_changed.set_label(if e.text().trim().is_empty() {
            "Type to search"
        } else {
            "Searching…"
        });
    });
    let weak = Rc::downgrade(ui);
    let p = pop.clone();
    search.connect_search_changed(move |entry| {
        let query = entry.text().trim().to_owned();
        if query.is_empty() {
            return;
        }
        let request = generation.get();
        let generation = generation.clone();
        let grid = grid.clone();
        let status = status.clone();
        let weak = weak.clone();
        let pop = p.clone();
        task(
            async move { crate::gif::search(&query, 0).await },
            move |result| {
                if generation.get() != request {
                    return;
                }
                match result {
                    Ok(gifs) if gifs.is_empty() => status.set_label("No GIFs found"),
                    Ok(gifs) => {
                        status.set_visible(false);
                        for gif in gifs.into_iter().take(45) {
                            grid.append(&tile(weak.clone(), &pop, gif));
                        }
                    }
                    Err(e) => status.set_label(&format!("GIF search failed: {e}")),
                }
            },
        );
    });
    button.connect_destroy(move |_| pop.unparent());
}

fn tile(ui: std::rc::Weak<MessagesUi>, pop: &gtk::Popover, gif: crate::gif::Gif) -> gtk::Button {
    let picture = gtk::Picture::builder()
        .content_fit(gtk::ContentFit::Cover)
        .can_shrink(true)
        .overflow(gtk::Overflow::Hidden)
        .width_request(112)
        .height_request(112)
        .build();
    let button = gtk::Button::builder()
        .child(&picture)
        .css_classes(["flat", "verdigris-gif-tile"])
        .tooltip_text("Send this GIF")
        .build();
    let weak_picture = picture.downgrade();
    task(
        async move { crate::gif::thumbnail(&gif.thumbnail).await },
        move |result| {
            if let Ok(bytes) = result
                && let Some(picture) = weak_picture.upgrade()
                && let Ok(texture) = gtk::gdk::Texture::from_bytes(&glib::Bytes::from(&bytes))
            {
                picture.set_paintable(Some(&texture));
            }
        },
    );
    let pop = pop.downgrade();
    button.connect_clicked(move |_| {
        if let Some(pop) = pop.upgrade() {
            pop.popdown();
        }
        if let Some(ui) = ui.upgrade() {
            send_gif(ui, gif.url.clone());
        }
    });
    button
}

fn send_gif(ui: Rc<MessagesUi>, url: String) {
    let Some(chat) = ui.selected.borrow().clone() else {
        return;
    };
    if ui.sending.replace(true) {
        return;
    }
    let server = ui.server.borrow().clone();
    ui.composer.set_sensitive(false);
    ui.status.set_text("Sending GIF…");
    task(
        async move {
            let staged = crate::gif::stage(&url).await?;
            let bus = sync_connection().await?;
            service::proxy(&bus)
                .await?
                .call::<_, _, ()>(
                    "SendAttachment",
                    &(server, chat, staged.0.to_string_lossy().to_string()),
                )
                .await?;
            Ok(())
        },
        move |result| {
            ui.sending.set(false);
            ui.composer.set_sensitive(true);
            match result {
                Ok(()) => {
                    ui.status.set_text("Sent");
                    refresh_view(ui);
                }
                Err(e) => ui
                    .status
                    .set_text(&format!("GIF send failed: {}", error_text(&e))),
            }
        },
    );
}
