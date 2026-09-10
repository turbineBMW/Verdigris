# Verdigris Flatpak packaging (archived draft)

Verdigris is a fork of [Blue](https://github.com/gutbash/blue). This directory
contains an inherited manifest for the removed Python GTK frontend. Its identifiers
have been renamed, but it is **not a working package for the native Rust apps**.

Use [the native installer](../../README.md#install) for Phone and Messages.
A future Flatpak package needs GTK 4.18+, libadwaita 1.7+, the Rust apps, separate
Phone/Messages launchers, and access to the shared sync service and desktop keyring.
Settings must remain accessible only through the apps.

The Bluetooth backend stays outside the sandbox: it needs access to BlueZ,
oFono, and privileged adapter setup. The fork uses
`dev.turbinebmw.Verdigris.Bridge` for its session-bus service. Do not ship the
inherited manifest as-is.
