<div align="center">

<img src="rust/verdigris-apps/data/icons/dev.turbinebmw.Verdigris.Messages.png" alt="Verdigris Messages" width="128" height="128">

# Verdigris

**Messages and phone calls from your iPhone, on your Linux desktop.**

A fork of **[Blue](https://github.com/gutbash/blue)**.

</div>

Verdigris provides native Rust/GTK4/libadwaita **Messages** and **Phone** apps,
with shared settings and background message synchronization. Messages connects to
BlueBubbles Server on your Mac; Phone uses the inherited Bluetooth/HFP backend to
reach your nearby iPhone.

The native interface follows Bubo's conversation layout, message bubbles, growing
multiline composer, emoji picker, and searchable GIF grid. The inherited Python
backend also provides Bluetooth notifications, contact synchronization, a CLI,
and an optional legacy Qt frontend.

## Install

From this checkout:

```sh
./install.sh
```

This builds optimized binaries and installs them in `~/.local/bin` without root
privileges. Rerun the script to update. Installed copies work independently of the
checkout. Build requirements are Rust/Cargo, Python 3, pkg-config, GTK 4.18+ and
libadwaita 1.7+ development packages, plus a running systemd user session.

| Command | Desktop name | Purpose |
| --- | --- | --- |
| `verdigris-messages` | Messages | Conversations, text, attachments, emoji, and GIFs |
| `verdigris-phone` | Phone | Dial, answer, and end calls through the Bluetooth backend |
| `verdigris-settings` | No launcher | Shared settings, opened from either app |
| `verdigris-sync` | No launcher | Background message cache and synchronization |

Phone and Messages keep those short names in the app menu. Their app IDs and
icons use `dev.turbinebmw.Verdigris.*`. Settings opens from Messages' menu or
Phone's settings button and has no `.desktop` entry.

## Connect

1. Install and configure BlueBubbles Server on a Mac signed into Messages.
2. Open **Messages → Preferences** or the settings button in **Phone**. Enter the
   server URL and password; Settings tests the connection before saving.
3. For calls and iPhone contact photos, set up the
   [Verdigris Bluetooth backend](docs/backend.md).
4. Optionally enable synchronization at login:

   ```sh
   systemctl --user enable --now verdigris-sync.service
   ```

The native installer installs the four Rust programs. The Python CLI/backend is
installed separately with `python -m pip install -e .` inside a virtual environment
with system D-Bus/GObject bindings; see the backend guide for pairing and services.

[Native app setup and behavior](rust/verdigris-apps/README.md) covers Mac setup,
SMS forwarding, cache behavior, attachment limits, keyboard shortcuts, and remaining
features. Enter adds a newline in Messages; **Ctrl+Enter sends**.

## Upgrading from Blue

The installer copies missing native connection/cache files from `blue-native` to
`verdigris` under the XDG config/data directories. SQLite databases are copied with
a consistent backup, including committed WAL contents. Existing Verdigris files
win, and the original Blue data is retained. The desktop keyring can supply the old
Blue password until you save a Verdigris credential in Settings.

Installation replaces the old native Blue launchers/icons and stops/disables its
old native sync unit. Enable `verdigris-sync.service` afterward if you want startup
at login. It leaves the Bluetooth backend service alone; the native apps can use a
running Blue backend while you upgrade it separately.

The Python package and CLI are now `verdigris`, with `VERDIGRIS_*` settings and
`verdigris.service`. Existing `IPHONEBRIDGE_*` environment variables, configuration,
and backend state are recognized for compatibility. New installs use Verdigris
paths. Native contact lookup also recognizes the old read-only PBAP cache.

## Development

```sh
cargo build --manifest-path rust/verdigris-apps/Cargo.toml --bins --locked
cargo test --manifest-path rust/verdigris-apps/Cargo.toml --locked
cargo clippy --manifest-path rust/verdigris-apps/Cargo.toml --all-targets -- -D warnings
python -m pytest tests/
ruff check src/ tests/
```

The GTK smoke test uses an isolated D-Bus session, Xvfb, fixture data, Python GObject
bindings, and ImageMagick. It sends no messages to real recipients and places no calls:

```sh
dbus-run-session --config-file=rust/verdigris-apps/tests/dbus.conf -- \
  xvfb-run -a -s '-screen 0 1280x850x24' python3 rust/verdigris-apps/tests/smoke.py
```

See [the backlog](BACKLOG.md), [changelog](CHANGELOG.md), and
[historical protocol experiments](spike/README.md). Flatpak packaging is an
[archived, unsupported draft](packaging/flatpak/README.md).

## Credits and license

Verdigris is a fork of **[gutbash/blue](https://github.com/gutbash/blue)** and retains
its Git history. Blue is based on
**[gabrielmeir53/iphonebridge](https://github.com/gabrielmeir53/iphonebridge)** by Gabe
Shatunovsky. Blue's existing attribution to © 2026 Sebastian Gutierrez is retained.

The native layout and GIF implementation were adapted from **Bubo**. Messages
uses **BlueBubbles Server**; BlueBubbles and BlueZ are separate upstream projects,
and their names are unchanged.

The ANCS implementation derives from
[bmh129/ancs4linux](https://github.com/bmh129/ancs4linux) and the original
[pzmarzly/ancs4linux](https://github.com/pzmarzly/ancs4linux) reference. The optional
experimental direct Apple transport uses rustpush and OpenBubbles-compatible
registration; its vendored dependencies are not included in this repository.

See [LICENSE](LICENSE) and individual component/license notices. Original author,
asset, and dependency attributions are preserved.
