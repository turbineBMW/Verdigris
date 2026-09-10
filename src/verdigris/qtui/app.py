"""verdigris-qt — Qt/QML desktop app entry point.

A separate process from the daemon, talking to it over D-Bus (see
`qtui/client.py`). The menu bar is exported to Plasma's global menu over
D-Bus; KWin provides background blur behind the window.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import (
    Q_ARG,
    Property,
    QEvent,
    QMetaObject,
    QObject,
    Qt,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QDesktopServices,
    QIcon,
    QKeySequence,
    QRegion,
    QSurfaceFormat,
)
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox

from verdigris.qtui.client import DaemonClient
from verdigris.qtui.models import EmojiCompleter, ThreadStore

log = logging.getLogger(__name__)

APP_ID = "dev.turbinebmw.Verdigris.Qt"
# Historical icon id used by older desktop entries and metainfo.
_ICON_NAME_LEGACY = "dev.turbinebmw.Verdigris.UI"
# Packaged Verdigris message-bubble artwork (ships with the Qt extra).
_PACKAGED_ICON = Path(__file__).parent / "assets" / "app-icon.svg"
_ICON_DIR = Path.home() / ".local/share/icons/hicolor/scalable/apps"
# Prefer the desktop-file id, then the legacy name, then the packaged asset.
_ICON_CANDIDATES = (
    _ICON_DIR / f"{APP_ID}.svg",
    _ICON_DIR / f"{_ICON_NAME_LEGACY}.svg",
    _PACKAGED_ICON,
)
_APPLICATIONS_DIR = Path.home() / ".local/share/applications"
_DESKTOP_FILE = _APPLICATIONS_DIR / f"{APP_ID}.desktop"
_QML_DIR = Path(__file__).parent / "qml"
_THEME_PLUGIN_DIR = Path.home() / ".local/lib/verdigris-qt/plugins"
_AUTOSTART_DIR = Path.home() / ".config" / "autostart"
_AUTOSTART_DESKTOP = _AUTOSTART_DIR / f"{APP_ID}.desktop"
# Legacy GTK autostart entry — remove if we find it when managing login start.
_LEGACY_AUTOSTART = _AUTOSTART_DIR / "verdigris-ui.desktop"

_PAGES = ("Messages", "Notifications", "Calls", "Setup")
_HELP_URL = QUrl.fromLocalFile(str(Path(__file__).with_name("README.md"))).toString()
_UPSTREAM_URL = "https://github.com/gutbash/blue"


class DaemonState(QObject):
    """Thin QML-facing view of the daemon's reachability + MAP health."""

    changed = Signal()

    def __init__(self, client: DaemonClient) -> None:
        super().__init__()
        self._client = client
        client.statusChanged.connect(self.changed)

    @Property(bool, notify=changed)
    def available(self) -> bool:
        return self._client.available

    @Property(bool, notify=changed)
    def healthy(self) -> bool:
        return self._client.healthy

    @Slot()
    def refresh(self) -> None:
        self._client.refresh_availability()


class WindowControls(QObject):
    """QML-facing window management for the frameless window.

    Dragging and resizing go through the compositor's own move/resize
    grabs (startSystemMove/startSystemResize) rather than manual geometry
    maths — that's the only approach that behaves correctly on Wayland,
    where a client can't position itself.
    """

    maximizedChanged = Signal()

    def __init__(self, window: QMainWindow) -> None:
        super().__init__()
        self._w = window

    @Slot()
    def close(self) -> None:
        self._w.close()

    @Slot()
    def minimize(self) -> None:
        self._w.showMinimized()

    @Slot()
    def toggleMaximize(self) -> None:
        if self._w.isMaximized():
            self._w.showNormal()
        else:
            self._w.showMaximized()
        self.maximizedChanged.emit()

    @Property(bool, notify=maximizedChanged)
    def maximized(self) -> bool:
        return self._w.isMaximized()

    @Slot()
    def startDrag(self) -> None:
        handle = self._w.windowHandle()
        if handle is not None:
            handle.startSystemMove()

    @Slot(int)
    def startResize(self, edges: int) -> None:
        handle = self._w.windowHandle()
        if handle is None:
            return
        # Build the flag explicitly rather than Qt.Edges(int): QML hands the
        # value over as a JS number, and relying on the implicit int→flag
        # conversion was giving the wrong edge (resizing the wrong axis).
        flags = Qt.Edge(0)
        if edges & 1:
            flags |= Qt.Edge.LeftEdge
        if edges & 2:
            flags |= Qt.Edge.RightEdge
        if edges & 4:
            flags |= Qt.Edge.TopEdge
        if edges & 8:
            flags |= Qt.Edge.BottomEdge
        log.debug("startSystemResize edges=%s flags=%s", edges, flags)
        handle.startSystemResize(flags)


class MainWindow(QMainWindow):
    """QWidget shell hosting the QML scene.

    It's a QMainWindow rather than a QML ApplicationWindow specifically so
    that setMenuBar() gives us a menu bar the KDE platform theme exports
    over dbusmenu to Plasma's global menu applet — the thing GTK4 had no
    mechanism for. The appmenu registrar keys off a real window, so a
    parentless QMenuBar would never be picked up.
    """

    def __init__(
        self,
        quick: QQuickWidget,
        state: DaemonState,
        store: ThreadStore | None = None,
    ) -> None:
        super().__init__()
        self._state = state
        self._store = store
        self.setWindowTitle("Verdigris")
        self.resize(980, 680)
        self.setMinimumSize(560, 380)
        # No server-side titlebar — the QML header draws its own controls,
        # so the window chrome is ours to style (see WindowControls).
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setCentralWidget(quick)
        self._quick = quick
        self._root = None
        # Must be set before the window is first shown.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)

    def showEvent(self, event) -> None:
        # The blur request needs a live surface, so it can only go out once
        # the window is actually on screen.
        super().showEvent(event)
        if not getattr(self, "_blur_done", False):
            self._blur_done = True
            self._enable_blur()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        # Tell the daemon whether we are the focused window so it can skip
        # desktop popups for the open conversation when the user is already
        # looking at it — and still notify when we are in the background.
        if event.type() == QEvent.Type.ActivationChange and self._store is not None:
            self._store.setWindowFocused(self.isActiveWindow())

    def bind_root(self) -> None:
        """Called once the QML scene has loaded."""
        self._root = self._quick.rootObject()
        self._build_menu()
        # Seed focus state: ActivationChange only fires on transitions, and
        # we start focused when first shown.
        if self._store is not None:
            self._store.setWindowFocused(self.isActiveWindow())

    def _build_menu(self) -> None:
        """The macOS Tahoe Messages menu bar, as far as we can honour it.

        Same menus in the same order — Messages, File, Edit, View,
        Conversation, Format, Window, Help — because on Plasma this *is* the
        global menu, and a familiar shape is the whole point. Items that
        would be dead here (New Message, rich-text formatting, iCloud) are
        left out rather than shown greyed: a menu that lies is worse than a
        shorter one.

        Everything that touches the scene goes through Main.qml's
        menuAction(); everything else is window/process level.
        """
        bar = self.menuBar()
        # Hand the menu to the desktop's menu bar instead of drawing a strip
        # inside the window; the dbusmenu export is unaffected.
        bar.setNativeMenuBar(True)

        # ---- Messages (the app menu) ------------------------------------
        app_menu = bar.addMenu("Messages")
        about = app_menu.addAction("About Messages")
        about.setMenuRole(QAction.MenuRole.AboutRole)
        about.triggered.connect(self._show_about)
        app_menu.addSeparator()
        self._autostart_action = app_menu.addAction("Launch at Login")
        self._autostart_action.setCheckable(True)
        self._autostart_action.setChecked(_autostart_enabled())
        self._autostart_action.toggled.connect(self._on_autostart_toggled)
        app_menu.addSeparator()
        # Connection state lives in the menu rather than as a dot in the
        # window, so the only in-window chrome is the traffic lights.
        self._status_action = app_menu.addAction("Checking…")
        self._status_action.setEnabled(False)
        recheck = app_menu.addAction("Recheck Connection")
        recheck.triggered.connect(self._state.refresh)
        self._state.changed.connect(self._update_status)
        self._update_status()
        app_menu.addSeparator()
        quit_act = app_menu.addAction("Quit Messages")
        quit_act.setMenuRole(QAction.MenuRole.QuitRole)
        quit_act.setShortcut(QKeySequence.StandardKey.Quit)
        quit_act.triggered.connect(QApplication.quit)

        # ---- File --------------------------------------------------------
        file_menu = bar.addMenu("File")
        clear = file_menu.addAction("Clear Conversation")
        clear.setShortcut(QKeySequence("Ctrl+Backspace"))
        clear.triggered.connect(lambda: self._menu("clearThread"))
        file_menu.addSeparator()
        close = file_menu.addAction("Close Window")
        close.setShortcut(QKeySequence.StandardKey.Close)
        close.triggered.connect(self.close)

        # ---- Edit --------------------------------------------------------
        # These act on whatever text field has focus inside the scene; QML
        # routes them by name (Main.qml). Standard keys so the bindings match
        # the platform rather than macOS literally.
        edit = bar.addMenu("Edit")
        for label, action, key in (
            ("Undo", "undo", QKeySequence.StandardKey.Undo),
            ("Redo", "redo", QKeySequence.StandardKey.Redo),
            (None, None, None),
            ("Cut", "cut", QKeySequence.StandardKey.Cut),
            ("Copy", "copy", QKeySequence.StandardKey.Copy),
            ("Paste", "paste", QKeySequence.StandardKey.Paste),
            ("Select All", "selectAll", QKeySequence.StandardKey.SelectAll),
        ):
            if label is None:
                edit.addSeparator()
                continue
            act = edit.addAction(label)
            act.setShortcut(key)
            act.triggered.connect(lambda _c=False, a=action: self._menu(a))
        # Ctrl+F focuses the sidebar search field in the main window.
        find = edit.addAction("Find…")
        find.setShortcut(QKeySequence.StandardKey.Find)
        find.triggered.connect(lambda: self._menu("find"))

        # ---- View --------------------------------------------------------
        view = bar.addMenu("View")
        group = QActionGroup(self)
        group.setExclusive(True)
        for i, name in enumerate(_PAGES):
            act = view.addAction(name)
            act.setCheckable(True)
            act.setChecked(i == 0)
            act.setActionGroup(group)
            act.setShortcut(QKeySequence(f"Ctrl+{i + 1}"))
            act.triggered.connect(lambda _c=False, idx=i: self._set("pageIndex", idx))

        view.addSeparator()
        sidebar = view.addAction("Show Conversation List")
        sidebar.setCheckable(True)
        sidebar.setChecked(True)
        sidebar.setShortcut(QKeySequence("Ctrl+Alt+S"))
        sidebar.toggled.connect(lambda on: self._set("sidebarVisible", on))
        times = view.addAction("Show Times")
        times.setCheckable(True)
        times.setShortcut(QKeySequence("Ctrl+Alt+T"))
        times.toggled.connect(lambda on: self._set("showTimes", on))
        self._show_times_action = times
        # The same setting is reachable from the conversation's context menus,
        # so the tick has to be read back rather than remembered here — a menu
        # that says "unchecked" while the times are on is worse than no tick.
        view.aboutToShow.connect(self._sync_view_menu)

        view.addSeparator()
        full = view.addAction("Enter Full Screen")
        full.setShortcut(QKeySequence.StandardKey.FullScreen)
        full.triggered.connect(self._toggle_fullscreen)
        self._fullscreen_action = full

        # ---- Conversation -------------------------------------------------
        convo = bar.addMenu("Conversation")
        compose = convo.addAction("Write a Message")
        compose.setShortcut(QKeySequence("Ctrl+Return"))
        compose.triggered.connect(lambda: self._menu("composer"))
        mark = convo.addAction("Mark as Read")
        mark.setShortcut(QKeySequence("Ctrl+Shift+R"))
        mark.triggered.connect(lambda: self._menu("markRead"))
        pin = convo.addAction("Pin Conversation")
        pin.setShortcut(QKeySequence("Ctrl+Shift+P"))
        pin.triggered.connect(lambda: self._menu("togglePin"))
        convo.addSeparator()
        convo.addAction(clear)

        # ---- Format -------------------------------------------------------
        # No rich text in the composer, so this is the text-size control —
        # the one Format item that means something here.
        fmt = bar.addMenu("Format")
        for label, action, key in (
            ("Bigger", "biggerText", QKeySequence.StandardKey.ZoomIn),
            ("Smaller", "smallerText", QKeySequence.StandardKey.ZoomOut),
            ("Actual Size", "actualSizeText", QKeySequence("Ctrl+0")),
        ):
            act = fmt.addAction(label)
            act.setShortcut(key)
            act.triggered.connect(lambda _c=False, a=action: self._menu(a))

        # ---- Window -------------------------------------------------------
        window = bar.addMenu("Window")
        minimize = window.addAction("Minimize")
        minimize.setShortcut(QKeySequence("Ctrl+M"))
        minimize.triggered.connect(self.showMinimized)
        zoom = window.addAction("Zoom")
        zoom.triggered.connect(self._toggle_maximize)

        # ---- Help ---------------------------------------------------------
        help_menu = bar.addMenu("Help")
        docs = help_menu.addAction("verdigris Help")
        docs.setShortcut(QKeySequence.StandardKey.HelpContents)
        docs.triggered.connect(lambda: QDesktopServices.openUrl(QUrl(_HELP_URL)))
        issue = help_menu.addAction("Upstream Blue Project")
        issue.triggered.connect(lambda: QDesktopServices.openUrl(QUrl(_UPSTREAM_URL)))

    def _menu(self, action: str) -> None:
        """Fire a menu action into the QML scene."""
        if self._root is None:
            return
        QMetaObject.invokeMethod(
            self._root, "menuAction", Qt.ConnectionType.DirectConnection,
            Q_ARG(str, action))

    def _toggle_maximize(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def _toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
            self._fullscreen_action.setText("Enter Full Screen")
        else:
            self.showFullScreen()
            self._fullscreen_action.setText("Exit Full Screen")

    def _show_about(self) -> None:
        QMessageBox.about(
            self, "About Verdigris",
            "<b>Verdigris</b><br><br>"
            "Your iPhone’s messages, calls, notifications, and contacts "
            "on Linux.<br>No Mac relay, no cloud service, no subscription."
            "<br><br>A fork of Blue.<br>GPL-2.0-or-later")

    def _update_status(self) -> None:
        if not self._state.available:
            text = "iPhone: daemon not running"
        elif self._state.healthy:
            text = "iPhone: connected"
        else:
            text = "iPhone: not reachable"
        self._status_action.setText(text)

    def _on_autostart_toggled(self, enabled: bool) -> None:
        try:
            if enabled:
                _enable_autostart()
            else:
                _disable_autostart()
        except OSError as e:
            log.warning("could not update autostart: %s", e)
            # Keep the checkbox honest if the filesystem write failed.
            self._autostart_action.blockSignals(True)
            self._autostart_action.setChecked(_autostart_enabled())
            self._autostart_action.blockSignals(False)

    def _sync_view_menu(self) -> None:
        """Reflect scene state that the menu isn't the only way to change."""
        if self._root is None:
            return
        act = self._show_times_action
        # Blocked, or setting the tick would fire toggled() and write the
        # value straight back into the scene — harmless today, a loop the
        # moment either side does anything more than store a bool.
        was = act.blockSignals(True)
        act.setChecked(bool(self._root.property("showTimes")))
        act.blockSignals(was)

    def _set(self, prop: str, value) -> None:
        if self._root is not None:
            self._root.setProperty(prop, value)

    #: Must match the QML root Rectangle's corner radius.
    CORNER_RADIUS = 10

    def _rounded_region(self) -> QRegion:
        """The window shape as a rounded rectangle.

        QRegion can't be built from a path, so compose it: a tall centre
        band, a wide centre band, and an ellipse in each corner.
        """
        w, h = self.width(), self.height()
        r = self.CORNER_RADIUS
        d = r * 2
        region = QRegion(r, 0, max(0, w - d), h)
        region = region.united(QRegion(0, r, w, max(0, h - d)))
        for cx, cy in ((0, 0), (w - d, 0), (0, h - d), (w - d, h - d)):
            region = region.united(
                QRegion(cx, cy, d, d, QRegion.RegionType.Ellipse))
        return region

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # The blur region is in window coordinates, so it has to be rebuilt
        # whenever the window changes size or the corners go square again.
        if getattr(self, "_blur_done", False):
            self._enable_blur()

    def _enable_blur(self) -> None:
        """Ask KWin to blur what's behind the window — real frosted glass.

        KWindowEffects has no Python binding, so we call the C++ symbol
        directly through ctypes. This only works because the process ends
        up with a single Qt: the distro PySide6 links the same system Qt
        that libKF6WindowSystem does. (The PyPI PySide6 wheel bundles its
        own Qt built without Qt_6.11_PRIVATE_API, and the library refuses
        to load against it.)

        Purely cosmetic — every failure path here is non-fatal.
        """
        # An empty QRegion would mean "the whole window" — a square, which
        # leaves blurred desktop filling the corners outside our rounded
        # rectangle and reads as a sharp corner drawn over a round one.
        self._blur_region = self._rounded_region()
        try:
            import ctypes

            import shiboken6

            lib = ctypes.CDLL("libKF6WindowSystem.so.6")
            fn = lib._ZN14KWindowEffects16enableBlurBehindEP7QWindowbRK7QRegion
            fn.restype = None
            fn.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_void_p]

            handle = self.windowHandle()
            if handle is None:
                self.winId()               # force native window creation
                handle = self.windowHandle()
            if handle is None:
                log.debug("no window handle yet; skipping blur")
                return

            fn(ctypes.c_void_p(shiboken6.getCppPointer(handle)[0]), True,
               ctypes.c_void_p(shiboken6.getCppPointer(self._blur_region)[0]))
            # Once, not every time. This runs on every resize and expose,
            # which buried every other line in the log under hundreds of
            # identical ones — including the warnings you need when something
            # in the UI isn't working.
            if not getattr(self, "_blur_logged", False):
                self._blur_logged = True
                log.info("KWin background blur enabled")
        except Exception as e:
            if not getattr(self, "_blur_logged", False):
                self._blur_logged = True
                log.info("background blur unavailable (%s)", e)


def _use_kde_platform_theme() -> None:
    """Let PySide6's bundled Qt find the KDE platform theme plugin.

    That plugin is what exports our QMenuBar over dbusmenu to Plasma's
    global menu, but PySide6 ships its own Qt tree and doesn't bundle it.
    The system copy is ABI-compatible only when the versions match exactly,
    so check before opting in.

    Only a `platformthemes/` directory is added — pointing QT_PLUGIN_PATH at
    all of /usr/lib64/qt6/plugins instead makes Qt load the *system*
    platform (wayland/xcb) plugins into the bundled Qt and abort at startup.
    """
    if os.environ.get("QT_QPA_PLATFORMTHEME"):
        return  # user override wins
    try:
        from PySide6 import QtCore
        if QtCore.qVersion() != _system_qt_version():
            return
    except Exception:
        return
    if not (_THEME_PLUGIN_DIR / "platformthemes").is_dir():
        return
    existing = os.environ.get("QT_PLUGIN_PATH", "")
    os.environ["QT_PLUGIN_PATH"] = (
        f"{_THEME_PLUGIN_DIR}{os.pathsep}{existing}" if existing
        else str(_THEME_PLUGIN_DIR))
    os.environ["QT_QPA_PLATFORMTHEME"] = "KDEPlasmaPlatformTheme6"


def _system_qt_version() -> str | None:
    for path in (Path("/usr/lib64/qt6/plugins/platformthemes"),):
        so = path / "KDEPlasmaPlatformTheme6.so"
        if not so.exists():
            continue
        # The plugin is built against the system libQt6Core; read its
        # soname's version off the real library.
        for lib in Path("/usr/lib64").glob("libQt6Core.so.6.*"):
            return lib.name.split("libQt6Core.so.", 1)[1]
    return None


def _enable_multisampling() -> None:
    """4x MSAA for the whole scene.

    Qt Quick renders without multisampling by default, so rounded corners,
    circular avatar masks and scaled SVGs come out with visibly stair-stepped
    edges. This has to be set before the QApplication exists.
    """
    fmt = QSurfaceFormat.defaultFormat()
    fmt.setSamples(4)
    QSurfaceFormat.setDefaultFormat(fmt)


def _qt_exec_path() -> str:
    """Absolute path to the Verdigris Qt launcher for .desktop Exec=."""
    bin_dir = Path(sys.executable).resolve().parent
    for name in ("verdigris-qt",):
        sibling = bin_dir / name
        if sibling.is_file():
            return str(sibling)
    for name in ("verdigris-qt",):
        found = shutil.which(name)
        if found:
            return found
    # Last resort: re-invoke this module (works for `python -m` style runs).
    return f"{sys.executable} -m verdigris.qtui.app"


def _icon_path() -> Path | None:
    for path in _ICON_CANDIDATES:
        if path.is_file():
            return path
    return None


def _install_desktop_icon() -> Path | None:
    """Install the Verdigris message-bubble icon into the user hicolor theme.

    Plasma / GNOME taskbars resolve `Icon=` by theme name, not by file path.
    Copy the packaged bubble under both the current desktop-file id and the
    legacy UI id so pinned launchers and older .desktop files still work.
    """
    if not _PACKAGED_ICON.is_file():
        return _icon_path()
    try:
        _ICON_DIR.mkdir(parents=True, exist_ok=True)
        bubble = _PACKAGED_ICON.read_bytes()
        for name in (APP_ID, _ICON_NAME_LEGACY):
            dest = _ICON_DIR / f"{name}.svg"
            if not dest.is_file() or dest.read_bytes() != bubble:
                dest.write_bytes(bubble)
        # Nudge icon caches without failing hard if the tools are missing.
        for cmd in (
            ["gtk-update-icon-cache", "-f", "-t",
             str(Path.home() / ".local/share/icons/hicolor")],
            ["xdg-desktop-menu", "forceupdate"],
        ):
            try:
                subprocess.run(cmd, check=False, capture_output=True, timeout=5)
            except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
                pass
    except OSError as exc:
        log.warning("could not install desktop icon: %s", exc)
    return _icon_path()


def _desktop_entry_body(*, autostart: bool = False) -> str:
    """XDG .desktop contents for the Verdigris Messages app."""
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        "Name=Verdigris",
        "GenericName=Messages",
        "Comment=Your iPhone's messages, calls, and notifications on Linux",
        f"Exec={_qt_exec_path()}",
        f"Icon={APP_ID}",
        "Terminal=false",
        "Categories=Network;InstantMessaging;Telephony;Qt;",
        "Keywords=iPhone;SMS;iMessage;Bluetooth;Calls;Notifications;Messages;Verdigris;",
        "StartupNotify=true",
        "StartupWMClass=verdigris-qt",
    ]
    if autostart:
        lines.extend([
            "X-GNOME-Autostart-enabled=true",
            "X-KDE-autostart-after=panel",
        ])
    return "\n".join(lines) + "\n"


def _install_desktop_entry() -> None:
    """Publish ~/.local/share/applications so Verdigris appears in the app menu."""
    try:
        _APPLICATIONS_DIR.mkdir(parents=True, exist_ok=True)
        body = _desktop_entry_body()
        if not _DESKTOP_FILE.is_file() or _DESKTOP_FILE.read_text() != body:
            _DESKTOP_FILE.write_text(body)
            log.info("desktop entry installed: %s", _DESKTOP_FILE)
    except OSError as exc:
        log.warning("could not install desktop entry: %s", exc)


def _autostart_enabled() -> bool:
    return _AUTOSTART_DESKTOP.is_file()


def _enable_autostart() -> None:
    """Write an XDG autostart .desktop so the Qt app starts at login.

    Works for Plasma, GNOME, and any DE that honors ~/.config/autostart/.
    Also drops the legacy GTK autostart entry if present so both don't fire.
    """
    _AUTOSTART_DIR.mkdir(parents=True, exist_ok=True)
    _install_desktop_icon()
    _AUTOSTART_DESKTOP.write_text(_desktop_entry_body(autostart=True))
    # Prefer Qt over the retired GTK UI if both were enabled historically.
    if _LEGACY_AUTOSTART.is_file():
        _LEGACY_AUTOSTART.unlink()
        log.info("removed legacy GTK autostart %s", _LEGACY_AUTOSTART)
    log.info("autostart enabled: %s", _AUTOSTART_DESKTOP)


def _disable_autostart() -> None:
    if _AUTOSTART_DESKTOP.is_file():
        _AUTOSTART_DESKTOP.unlink()
        log.info("autostart disabled: removed %s", _AUTOSTART_DESKTOP)
    # Leave any leftover GTK entry alone on disable — user may have kept it
    # intentionally; enable path is what cleans it up.


def main() -> int:
    _enable_multisampling()
    _use_kde_platform_theme()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        stream=sys.stderr)

    # QApplication (not QGuiApplication) so QMenuBar — and therefore the
    # global menu export — is available.
    app = QApplication(sys.argv)
    # Product name is Verdigris; window title stays Messages-like for the chat UI.
    app.setApplicationName("Verdigris")
    app.setApplicationDisplayName("Verdigris")
    # Must match the .desktop basename so Plasma's taskbar maps the running
    # window onto the pinned launcher instead of showing a second icon.
    app.setDesktopFileName(APP_ID)
    icon = _install_desktop_icon()
    _install_desktop_entry()
    if icon is not None:
        app.setWindowIcon(QIcon(str(icon)))

    client = DaemonClient()
    store = ThreadStore(client)
    state = DaemonState(client)

    quick = QQuickWidget()
    quick.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
    quick.setClearColor(Qt.GlobalColor.transparent)
    # A transparent clear colour alone isn't enough: the widget still paints
    # its own opaque background, which showed as a square edge behind the
    # window's rounded corners.
    quick.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    quick.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
    quick.setAutoFillBackground(False)
    # The `verdigris` QML module lives in qml/verdigris/, so the
    # import path is qml/ itself.
    quick.engine().addImportPath(str(_QML_DIR))
    # The window must exist before the QML loads, since the scene binds to
    # its window controls.
    win = MainWindow(quick, state, store)
    controls = WindowControls(win)

    ctx = quick.rootContext()
    ctx.setContextProperty("threadStore", store)
    ctx.setContextProperty("daemon", state)
    ctx.setContextProperty("winctl", controls)
    emoji = EmojiCompleter()
    ctx.setContextProperty("emoji", emoji)

    quick.setSource(QUrl.fromLocalFile(str(_QML_DIR / "Main.qml")))
    if quick.status() == QQuickWidget.Status.Error:
        for err in quick.errors():
            log.error("QML: %s", err.toString())
        return 1

    win.bind_root()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
