# Flatpak packaging — desktop UI

> **Status: outdated.** The GTK `iphonebridge-ui` front-end has been removed.
> The desktop app is now **`iphonebridge-qt`** (PySide6 / QML). This manifest
> still targets the old GTK entry point and needs a rewrite (likely KDE
> runtime + PySide6) before it can build.

This packages **only the desktop app**. The daemon stays a native install: it
needs privileged setup — `btmgmt` Class-of-Device, the `LastUsedBearer=le`
file edit, oFono — that a Flatpak sandbox cannot do. The sandboxed UI reaches
the native daemon over the session bus (`--talk-name=com.gabriel.iphonebridge`).

Do not ship this manifest as-is until it is retargeted at `iphonebridge-qt`.
