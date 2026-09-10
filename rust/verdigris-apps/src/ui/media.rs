use super::*;

fn image_picture(bytes: &[u8]) -> anyhow::Result<gtk::Picture> {
    let pic = gtk::Picture::new();
    pic.set_content_fit(gtk::ContentFit::Fill);
    pic.set_can_shrink(true);
    pic.set_overflow(gtk::Overflow::Hidden);
    pic.add_css_class("verdigris-image");
    let fit = |pic: &gtk::Picture, w: i32, h: i32| {
        let (w, h) = (w.max(1) as f64, h.max(1) as f64);
        let k = (360.0 / w).min(480.0 / h);
        pic.set_size_request((w * k).round() as i32, (h * k).round() as i32);
    };
    if bytes.starts_with(b"GIF8") {
        use gtk::gdk_pixbuf::{PixbufAnimation, PixbufLoader};
        let loader = PixbufLoader::new();
        loader.write(bytes)?;
        loader.close()?;
        let anim: PixbufAnimation = loader
            .animation()
            .ok_or_else(|| anyhow::anyhow!("no animation"))?;
        fit(&pic, anim.width(), anim.height());
        let iter = anim.iter(None);
        pic.set_paintable(Some(&gtk::gdk::Texture::for_pixbuf(&iter.pixbuf())));
        if !anim.is_static_image() {
            fn tick(pic: glib::WeakRef<gtk::Picture>, iter: gtk::gdk_pixbuf::PixbufAnimationIter) {
                let delay = iter
                    .delay_time()
                    .unwrap_or(std::time::Duration::from_millis(100))
                    .max(std::time::Duration::from_millis(20));
                glib::timeout_add_local_once(delay, move || {
                    let Some(p) = pic.upgrade() else { return };
                    iter.advance(std::time::SystemTime::now());
                    p.set_paintable(Some(&gtk::gdk::Texture::for_pixbuf(&iter.pixbuf())));
                    tick(pic, iter);
                });
            }
            tick(pic.downgrade(), iter);
        }
    } else {
        let tex = gtk::gdk::Texture::from_bytes(&glib::Bytes::from(bytes))?;
        fit(&pic, tex.width(), tex.height());
        pic.set_paintable(Some(&tex));
    }
    Ok(pic)
}

fn preview(
    container: &gtk::Box,
    path: &std::path::Path,
    window: glib::WeakRef<adw::ApplicationWindow>,
) -> bool {
    let Ok(bytes) = std::fs::read(path) else {
        return false;
    };
    let Ok(picture) = image_picture(&bytes) else {
        return false;
    };
    let file = gtk::gio::File::for_path(path);
    let gesture = gtk::GestureClick::new();
    gesture.connect_released(move |_, _, _, _| {
        let file = file.clone();
        let window = window.clone();
        glib::spawn_future_local(async move {
            let parent = window.upgrade();
            let _ = gtk::FileLauncher::new(Some(&file))
                .launch_future(parent.as_ref())
                .await;
        });
    });
    picture.add_controller(gesture);
    picture.set_cursor_from_name(Some("pointer"));
    container.prepend(&picture);
    true
}

pub(super) fn attachment_widget(ui: &MessagesUi, attachment: &serde_json::Value) -> gtk::Box {
    let container = gtk::Box::new(gtk::Orientation::Vertical, 4);
    let name = attachment["transferName"]
        .as_str()
        .unwrap_or("Attachment")
        .to_owned();
    let guid = attachment["guid"].as_str().unwrap_or("").to_owned();
    let is_image = attachment["mimeType"]
        .as_str()
        .is_some_and(|m| m.starts_with("image/"));
    let server = ui.server.borrow().clone();
    let cached = crate::attachments::cached(&server, &guid, &name);
    let rendered = Rc::new(Cell::new(
        is_image
            && cached
                .as_deref()
                .is_some_and(|path| preview(&container, path, ui.window.clone())),
    ));
    let button = gtk::Button::builder()
        .label(format!("Open {name}"))
        .css_classes(["flat"])
        .halign(gtk::Align::Start)
        .build();
    button.set_sensitive(!guid.is_empty());
    button.set_visible(!rendered.get());
    container.append(&button);
    let status = gtk::Label::builder()
        .wrap(true)
        .css_classes(["caption"])
        .visible(false)
        .build();
    container.append(&status);
    let window = ui.window.clone();
    let preview_box = container.downgrade();
    let button_weak = button.downgrade();
    let status_weak = status.downgrade();
    let load: Rc<dyn Fn(bool)> = Rc::new(move |open| {
        let Some(button) = button_weak.upgrade() else {
            return;
        };
        let Some(status) = status_weak.upgrade() else {
            return;
        };
        button.set_sensitive(false);
        status.set_visible(true);
        status.set_text("Downloading…");
        let (server, guid, name, window, preview_box, rendered) = (
            server.clone(),
            guid.clone(),
            name.clone(),
            window.clone(),
            preview_box.clone(),
            rendered.clone(),
        );
        task(
            async move {
                if let Some(path) = crate::attachments::cached(&server, &guid, &name) {
                    return Ok(path);
                }
                let bus = sync_connection().await?;
                let path: String = service::proxy(&bus)
                    .await?
                    .call("DownloadAttachment", &(server, guid, name))
                    .await?;
                Ok(std::path::PathBuf::from(path))
            },
            move |result| {
                button.set_sensitive(true);
                match result {
                    Ok(path) => {
                        status.set_visible(false);
                        if is_image
                            && !rendered.get()
                            && let Some(container) = preview_box.upgrade()
                        {
                            rendered.set(preview(&container, &path, window.clone()));
                            button.set_visible(!rendered.get());
                        }
                        if open {
                            glib::spawn_future_local(async move {
                                let parent = window.upgrade();
                                if let Err(e) =
                                    gtk::FileLauncher::new(Some(&gtk::gio::File::for_path(path)))
                                        .launch_future(parent.as_ref())
                                        .await
                                {
                                    status.set_text(&format!("Could not open file: {e}"));
                                    status.set_visible(true);
                                }
                            });
                        }
                    }
                    Err(e) => {
                        status.set_text(&error_text(&e));
                        status.set_visible(true);
                    }
                }
            },
        );
    });
    let loader = load.clone();
    button.connect_clicked(move |_| loader(true));
    // Fetch uncached images only when they approach the visible viewport.
    if is_image && cached.is_none() {
        let weak = container.downgrade();
        let scroll = ui.scroll.clone();
        let fired = Rc::new(Cell::new(false));
        let check: Rc<dyn Fn()> = Rc::new(move || {
            if fired.get() {
                return;
            }
            let Some(holder) = weak.upgrade() else {
                return;
            };
            let Some(bounds) = holder.compute_bounds(&scroll) else {
                return;
            };
            let height = scroll.height() as f32;
            if height > 0.0 && bounds.y() < height * 2.0 && bounds.y() + bounds.height() > -height {
                fired.set(true);
                load(false);
            }
        });
        let c = check.clone();
        let id = ui.scroll.vadjustment().connect_value_changed(move |_| c());
        let adjustment = ui.scroll.vadjustment();
        let handler = RefCell::new(Some(id));
        container.connect_destroy(move |_| {
            if let Some(id) = handler.borrow_mut().take() {
                adjustment.disconnect(id);
            }
        });
        glib::timeout_add_local_once(std::time::Duration::from_millis(100), move || check());
    }
    container
}

pub(super) fn choose_file(ui: Rc<MessagesUi>) {
    let Some(parent) = ui.window.upgrade() else {
        return;
    };
    let Some(chat) = ui.selected.borrow().clone() else {
        return;
    };
    if ui.sending.get() {
        return;
    }
    let server = ui.server.borrow().clone();
    let title = ui.title.title().to_string();
    glib::spawn_future_local(async move {
        let chooser = gtk::FileDialog::builder()
            .title("Choose a file to send")
            .accept_label("Choose")
            .build();
        let file = match chooser.open_future(Some(&parent)).await {
            Ok(file) => file,
            Err(e) if e.matches(gtk::DialogError::Dismissed) => return,
            Err(e) => {
                ui.status.set_text(&format!("Could not choose file: {e}"));
                return;
            }
        };
        let Some(path) = file.path() else {
            ui.status.set_text("Choose a local file");
            return;
        };
        let name = path.file_name().unwrap_or_default().to_string_lossy();
        let confirmation = adw::AlertDialog::builder()
            .heading("Send attachment")
            .body(format!("Send {name} to {title}?"))
            .build();
        confirmation.add_response("cancel", "Cancel");
        confirmation.add_response("send", "Send file");
        confirmation.set_response_appearance("send", adw::ResponseAppearance::Suggested);
        confirmation.set_default_response(Some("send"));
        confirmation.set_close_response("cancel");
        if confirmation.choose_future(Some(&parent)).await != "send" {
            return;
        }
        if ui.sending.replace(true) {
            return;
        }
        ui.composer.set_sensitive(false);
        ui.status.set_text(&format!("Sending {name}…"));
        task(
            async move {
                let bus = sync_connection().await?;
                service::proxy(&bus)
                    .await?
                    .call::<_, _, ()>(
                        "SendAttachment",
                        &(server, chat, path.to_string_lossy().to_string()),
                    )
                    .await?;
                Ok(())
            },
            move |result| {
                ui.sending.set(false);
                ui.composer.set_sensitive(true);
                match result {
                    Ok(()) => {
                        ui.status.set_text("File sent");
                        refresh_view(ui);
                    }
                    Err(e) => ui.status.set_text(&error_text(&e)),
                }
            },
        );
    });
}
